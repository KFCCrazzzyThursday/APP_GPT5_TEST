# -*- coding: utf-8 -*-
"""
记录本轮学习信号，并在回合结束时统一结算分数：
- record_signal(word, signal, note?)：回合内打点
- compute_score(word, override?)：根据打点给出 0~1 分
- clear/snapshot：清理/查看状态
"""

from __future__ import annotations
from collections import defaultdict
from typing import Dict, Any, Optional

_STATE: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
    "start": "unknown",   # "forgot" | "remember" | "unknown"
    "hint": 0,            # 提示次数
    "zh_revealed": 0,     # 揭示中文次数
    "correction": 0,      # 纠错次数
    "confusion": 0,       # 混淆出现次数
    "notes": []           # 备注堆栈
})


def clear(word: str) -> None:
    _STATE.pop(word.lower(), None)


def snapshot(word: str) -> Dict[str, Any]:
    return dict(_STATE[word.lower()])


def record_signal(word: str, signal: str, note: Optional[str] = None) -> Dict[str, Any]:
    w = word.lower().strip()
    s = _STATE[w]
    if signal in ("start_forgot", "start_remember"):
        s["start"] = "forgot" if signal == "start_forgot" else "remember"
    elif signal in ("hint", "zh_revealed", "correction", "confusion"):
        s[signal] = int(s.get(signal, 0) or 0) + 1
    elif signal == "note" and note:
        s["notes"].append(note.strip())
    return snapshot(w)


def compute_score(word: str, override: Optional[float] = None) -> float:
    if override is not None:
        return max(0.0, min(1.0, float(override)))

    s = snapshot(word)
    # 基准：开场自报
    base = 0.8 if s.get("start") == "remember" else (
        0.2 if s.get("start") == "forgot" else 0.6)

    # 惩罚（可调）
    score = base
    score -= 0.10 * int(s.get("hint", 0))
    score -= 0.15 * int(s.get("zh_revealed", 0))
    score -= 0.12 * int(s.get("correction", 0))
    score -= 0.15 * int(s.get("confusion", 0))

    return max(0.05, min(0.95, round(score, 3)))
