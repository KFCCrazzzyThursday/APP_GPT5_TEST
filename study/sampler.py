# -*- coding: utf-8 -*-
import time
from typing import List, Dict, Tuple
from .srs import now_ts, ensure_state, priority


def _word_of(entry: dict) -> str | None:
    if "word" in entry and entry["word"]:
        return entry["word"]
    if "entry" in entry and isinstance(entry["entry"], dict):
        return entry["entry"].get("word")
    return None


def _srs_of(entry: dict) -> dict:
    if "srs" in entry and isinstance(entry["srs"], dict):
        return entry["srs"]
    if "review" in entry and isinstance(entry["review"], dict):
        return entry["review"]
    return {}


def _normalize_item(entry: dict) -> dict:
    # 为前端兼容，返回 {word, entry}
    w = _word_of(entry)
    return {"word": w, "entry": entry}


def sample_study_items(entries: List[dict], k: int = 20, min_days_gap: float = 1.0) -> List[dict]:
    """
    统一取样逻辑：
    1) 先取“到期/过期”的卡；
    2) 不足则取“从未复习过”的卡；
    3) 再不足则按统一优先级（小者优先）补齐。
    """
    tnow = now_ts()
    pool_due: List[Tuple[float, dict]] = []
    pool_new: List[dict] = []
    pool_rest: List[Tuple[float, dict]] = []

    for e in entries:
        w = _word_of(e)
        if not w:
            continue
        srs = ensure_state(_srs_of(e))
        rc = int(srs.get("review_count", 0))
        due_ts = float(srs.get("next_due_ts", 0.0))

        if due_ts and tnow >= due_ts:
            pool_due.append((priority(srs, tnow), e))
        elif rc == 0:
            pool_new.append(e)
        else:
            # 距上次 >= min_days_gap 的卡才纳入候选（避免太快重复）
            last_ts = float(srs.get("last_ts", 0.0))
            days_gap = (tnow - last_ts) / 86400.0 if last_ts else 999.0
            if days_gap >= min_days_gap:
                pool_rest.append((priority(srs, tnow), e))

    pool_due.sort(key=lambda t: t[0])
    pool_rest.sort(key=lambda t: t[0])

    out: List[dict] = []
    # 1) 到期优先
    for pr, e in pool_due:
        if len(out) >= k:
            break
        out.append(_normalize_item(e))
    # 2) 新卡
    for e in pool_new:
        if len(out) >= k:
            break
        out.append(_normalize_item(e))
    # 3) 其它
    for pr, e in pool_rest:
        if len(out) >= k:
            break
        out.append(_normalize_item(e))

    return out


def plan_daily_new(entries: List[dict], k: int = 100) -> List[dict]:
    """
    每日计划：优先选“从未复习过”的词；不够则选 review_count 最低、最近很久没碰的。
    """
    tnow = now_ts()
    new_items = []
    rest = []

    for e in entries:
        w = _word_of(e)
        if not w:
            continue
        srs = ensure_state(_srs_of(e))
        rc = int(srs.get("review_count", 0))
        if rc == 0:
            new_items.append(e)
        else:
            rest.append((rc, -(srs.get("last_ts") or 0.0), e))

    out = []
    new_items = new_items[:k]
    out.extend([_normalize_item(e) for e in new_items])

    if len(out) < k and rest:
        rest.sort(key=lambda t: (t[0], t[1]))   # rc 小优先，last_ts 越久越优先
        for _, __, e in rest:
            if len(out) >= k:
                break
            out.append(_normalize_item(e))
    return out


def sample_by_priority(entries: List[dict], k: int = 100) -> List[dict]:
    """
    全库按照统一优先级（越小越紧急）排序，取前 k 个。
    """
    tnow = now_ts()
    ranked: List[Tuple[float, dict]] = []
    for e in entries:
        w = _word_of(e)
        if not w:
            continue
        srs = ensure_state(_srs_of(e))
        ranked.append((priority(srs, tnow), e))
    ranked.sort(key=lambda t: t[0])
    return [_normalize_item(e) for _, e in ranked[:k]]
