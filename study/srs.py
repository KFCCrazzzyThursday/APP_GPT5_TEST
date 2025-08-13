# -*- coding: utf-8 -*-
import time
import math
from copy import deepcopy
from .srs_policy_14day import POLICY as P

SECONDS_PER_DAY = 86400.0


def now_ts() -> float:
    return time.time()


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def ensure_state(raw: dict | None) -> dict:
    """
    统一的 SRS 状态结构。兼容老数据并补默认值。
    """
    s = deepcopy(raw) if isinstance(raw, dict) else {}
    s.setdefault("review_count", 0)
    s.setdefault("ease", P["sm2_init_ease"])
    s.setdefault("interval_days", 0.0)
    s.setdefault("next_due_ts", 0.0)
    s.setdefault("last_ts", 0.0)
    # 统一的连续强度分（EMA），用于艾宾浩斯优先级
    s.setdefault("score", 0.5)
    # 兼容旧字段（progress_snapshot 会读取）
    s["last_score"] = s.get("last_score", s["score"])
    s["avg_score"] = s.get("avg_score",  s["score"])
    # 历史
    if not isinstance(s.get("history"), list):
        s["history"] = []
    return s


def _effective_ease(ease: float, score: float) -> float:
    """
    将 SM-2 的 ease 与连续强度分 score 结合：
    score 越高，实际等效 ease 越大（更快拉长间隔）。
    """
    e = ease * (0.8 + 0.4 * clamp(score, 0.0, 1.0))  # 0.8x ~ 1.2x
    return clamp(e, P["sm2_min_ease"], P["sm2_max_ease"])


def commit(state_in: dict, outcome: float, now: float | None = None) -> dict:
    """
    统一提交一次复习（正确=1.0 / 错误=0.0）：
    - 更新 EMA 强度分、last_ts、history
    - 更新 SM-2 的 ease / interval_days / next_due_ts
    """
    tnow = now or now_ts()
    s = ensure_state(state_in)

    # 1) EMA 强度分
    alpha = P["ema_alpha"]
    old_score = float(s.get("score", 0.5))
    new_score = (1.0 - alpha) * old_score + alpha * float(outcome)
    s["score"] = clamp(new_score, 0.0, 1.0)
    s["last_score"] = float(outcome)
    s["avg_score"] = s["score"]

    # 2) 记录历史
    s["history"].append(
        {"ts": tnow, "outcome": float(outcome), "score": s["score"]})
    s["last_ts"] = tnow

    # 3) SM-2 风格更新
    ease = float(s.get("ease", P["sm2_init_ease"]))
    rc = int(s.get("review_count", 0))
    interval = float(s.get("interval_days", 0.0))

    if outcome < 0.5:
        # 错误：降低 ease（表现越差影响越大），并重置为较短间隔
        ease = clamp(
            ease - (P["delta_ease_wrong"] + (1.0 - s["score"]) * 0.4),
            P["sm2_min_ease"], P["sm2_max_ease"]
        )
        # 错误后一律回到较短间隔（给跨天排期；组内即时强化由前端“弱循环”负责）
        if rc <= 1:
            interval = P["first_interval_days"]
        else:
            interval = max(1.0, interval * 0.5)
    else:
        # 正确：略升 ease（高分更容易升）
        ease = clamp(
            ease + (P["delta_ease_right"] + s["score"] * 0.1),
            P["sm2_min_ease"], P["sm2_max_ease"]
        )
        # 过期奖励：如果这次复习已晚于安排，下次间隔略放大
        overdue_mul = 1.0
        if s.get("next_due_ts", 0.0) and tnow > float(s["next_due_ts"]):
            overdue_mul = P["overdue_factor"]

        if rc == 0:
            interval = P["first_interval_days"]
        elif rc == 1:
            interval = P["second_interval_days"]
        else:
            # SM-2 递推：下次间隔 = 上次间隔 * 等效 ease
            interval = max(1.0, interval * _effective_ease(ease, s["score"]))
        interval *= overdue_mul

    s["ease"] = ease
    s["interval_days"] = float(interval)
    s["review_count"] = rc + 1
    s["next_due_ts"] = tnow + interval * SECONDS_PER_DAY

    return s


def days_since_last(state: dict, now: float | None = None) -> float:
    tnow = now or now_ts()
    last = float(ensure_state(state).get("last_ts", 0.0))
    if not last:
        return 999.0
    return max(0.0, (tnow - last) / SECONDS_PER_DAY)


def retention(state: dict, now: float | None = None) -> float:
    """
    艾宾浩斯保留率 R(t) = exp( - t / (tau * strength) )
    strength ≈ score（0.05~1.0）。返回值越小 → 越容易忘 → 越紧急。
    """
    s = ensure_state(state)
    tnow = now or now_ts()
    tdays = days_since_last(s, tnow)
    strength = clamp(s.get("score", 0.5), 0.05, 1.0)
    tau = float(P["tau_days"])
    return math.exp(- tdays / (tau * strength))


def priority(state: dict, now: float | None = None) -> float:
    """
    统一优先级：先看“是否到期/过期”，再看艾宾浩斯保留率。
    返回值越小越优先。
    """
    s = ensure_state(state)
    tnow = now or now_ts()
    due = float(s.get("next_due_ts", 0.0))
    # 过期/到期的卡片优先因子（更小）
    due_factor = 0.2 if (due and tnow >= due) else 1.0
    return due_factor * retention(s, tnow)
