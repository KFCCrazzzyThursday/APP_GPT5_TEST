# -*- coding: utf-8 -*-
import json
import time
import copy
from pathlib import Path
from typing import Any, Dict, List

from study.srs import ensure_state, commit
from study.sampler import (
    sample_study_items as _sample_study_items,
    plan_daily_new as _plan_daily_new,
    sample_by_priority as _sample_by_priority,
)

# ---------- IO ----------


def _load_store(path: Path) -> Dict[str, Any]:
    if not path.exists():
        path.write_text(json.dumps(
            {"entries": []}, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        # tolerate top-level list
        if isinstance(obj, list):
            return {"entries": obj}
        return obj
    except Exception:
        return {"entries": []}


def _save_store(path: Path, data: Dict[str, Any]):
    path.write_text(json.dumps(data, ensure_ascii=False,
                    indent=2), encoding="utf-8")


# ---------- utils ----------

def _find_entry(entries, word: str):
    for e in entries:
        if isinstance(e, dict):
            if e.get("word") == word:
                return e
            ent = e.get("entry")
            if isinstance(ent, dict) and ent.get("word") == word:
                return e
    return None


def _entry_word(e: dict) -> str | None:
    if e.get("word"):
        return e["word"]
    if isinstance(e.get("entry"), dict):
        return e["entry"].get("word")
    return None


def _safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        try:
            return float(str(x).strip())
        except Exception:
            return default


def _safe_int(x, default=0) -> int:
    try:
        return int(x)
    except Exception:
        try:
            return int(float(str(x).strip()))
        except Exception:
            return default


_NUM_KEYS_SRS = [
    "avg_score", "score", "interval_days", "interval", "last_score",
    "reviews", "review_count", "n",
]
_NUM_KEYS_ENTRY = ["priority", "weight"]


def _normalize_one_entry(e: dict) -> dict:
    """return a deep-copied, numeric-safe version of a single entry"""
    ee = copy.deepcopy(e) if isinstance(e, dict) else {}
    # pick the logical "entry" dict
    ent = ee.get("entry") if isinstance(ee.get("entry"), dict) else ee

    # normalize SRS block
    srs = ee.get("srs") or ent.get("srs") or {}
    if not isinstance(srs, dict):
        srs = {}
    for k in _NUM_KEYS_SRS:
        if k in srs:
            if k in ("review_count", "reviews", "n"):
                srs[k] = _safe_int(srs.get(k, 0), 0)
            else:
                srs[k] = _safe_float(srs.get(k, 0.0), 0.0)
    # write back srs consistently
    ent["srs"] = srs
    if "entry" in ee:
        ee["entry"] = ent
    else:
        # top-level structure where entry fields live at top
        ee.update(ent)

    # normalize common numeric fields at both levels
    for tgt in (ent, ee):
        for k in _NUM_KEYS_ENTRY:
            if k in tgt:
                if k == "priority":
                    tgt[k] = _safe_float(tgt.get(k, 0.0), 0.0)
                elif k == "weight":
                    tgt[k] = _safe_float(tgt.get(k, 1.0), 1.0)
    return ee


def _normalize_entries(entries: List[dict]) -> List[dict]:
    return [_normalize_one_entry(e) for e in entries if isinstance(e, dict)]


def _fallback_first_k(entries: List[dict], k: int) -> List[dict]:
    out = []
    for e in entries:
        w = _entry_word(e)
        if not w:
            continue
        ent = (e.get("entry") if isinstance(e.get("entry"), dict) else e) or {}
        out.append({"word": w, "entry": ent})
        if len(out) >= k:
            break
    return out


# ---------- tool entry ----------

def apply_tool(name: str, args: Dict[str, Any], store_path: Path) -> Dict[str, Any]:
    data = _load_store(store_path)
    entries_raw = data.get("entries") or data.get("words") or []
    # numeric-safe copy for samplers; keep raw for read/write ops
    entries_norm = _normalize_entries(entries_raw)
    name = (name or "").strip()

    if name == "record_signal_tool":
        # lightweight telemetry; no persistence
        return {"ok": True}

    if name == "get_word":
        w = (args or {}).get("word") or ""
        e = _find_entry(entries_raw, w)
        if not e:
            return {"ok": False, "error": f"word not found: {w}"}
        ent = e.get("entry") if isinstance(e.get("entry"), dict) else e
        return {"ok": True, "entry": ent}

    if name == "commit_review":
        w = (args or {}).get("word") or ""
        override = (args or {}).get("override_score", None)
        e = _find_entry(entries_raw, w)
        if not e:
            return {"ok": False, "error": f"word not found: {w}"}
        payload = e.get("srs") or e.get("review") or {}
        s = ensure_state(payload)
        outcome = 1.0 if (override is None) else _safe_float(override, 1.0)
        s = commit(s, outcome=outcome)
        e["srs"] = s
        _save_store(store_path, data)
        return {"ok": True, "srs": s}

    if name == "sample_study_items":
        k = _safe_int((args or {}).get("k", 20), 20)
        min_days_gap = _safe_float((args or {}).get("min_days_gap", 1.0), 1.0)
        try:
            items = _sample_study_items(
                entries_norm, k=k, min_days_gap=min_days_gap)
        except Exception as ex:
            # robust fallback
            items = _fallback_first_k(entries_norm, k)
        return {"ok": True, "items": items}

    if name == "plan_daily_new":
        k = _safe_int((args or {}).get("k", 100), 100)
        try:
            items = _plan_daily_new(entries_norm, k=k)
        except Exception as ex:
            # Fix for: TypeError: bad operand type for unary -: 'str'
            items = _fallback_first_k(entries_norm, k)
        return {"ok": True, "items": items}

    if name == "sample_by_priority":
        k = _safe_int((args or {}).get("k", 100), 100)
        try:
            items = _sample_by_priority(entries_norm, k=k)
        except Exception:
            items = _fallback_first_k(entries_norm, k)
        return {"ok": True, "items": items}

    return {"ok": False, "error": f"unknown tool: {name}"}


def redact_for_log(x: dict) -> dict:
    # hide bulky entry bodies in logs
    if not isinstance(x, dict):
        return x
    y = dict(x)
    if "entry" in y and isinstance(y["entry"], dict):
        y["entry"] = {"word": y["entry"].get("word")}
    if "items" in y and isinstance(y["items"], list):
        y["items"] = [{"word": it.get("word")} if isinstance(
            it, dict) else it for it in y["items"]]
    return y
