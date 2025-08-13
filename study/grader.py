# -*- coding: utf-8 -*-
import json
from typing import Dict, Any, Optional
from config import MODEL_NAME
from api_client import setup_client

_SYSTEM = (
    "你是严格但友好的双语词汇考官。"
    "你必须通过名为 grade_vocab_response 的函数工具返回结构化评分；"
    "除了工具调用，不要输出任何自然语言。"
    "如果你没有调用该工具，你的回答将被丢弃并要求重试。"
)

_TOOLS = [{
    "type": "function",
    "function": {
        "name": "grade_vocab_response",
        "description": "根据参考与用户作答返回评分（0~1）与纠错。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_type": {"type": "string", "enum": ["meaning_recall", "example_usage"]},
                "is_correct": {"type": "boolean"},
                "score_0_1": {"type": "number", "minimum": 0, "maximum": 1},
                "mistakes": {"type": "array", "items": {"type": "string"}},
                "correction": {"type": "string"},
                "explanation": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1}
            },
            "required": ["task_type", "is_correct", "score_0_1"]
        }
    }
}]


def _build_user_msg(task_type: str, card: Dict[str, Any], user_answer: str) -> Dict[str, str]:
    return {
        "role": "user",
        "content": (
            f"Task type: {task_type}\n"
            f"User answer: {user_answer}\n"
            f"Reference (JSON): {json.dumps(card, ensure_ascii=False)}\n"
            "仅通过函数工具 grade_vocab_response 返回结果。"
        )
    }


def _fallback(task_type: str, reason: str) -> Dict[str, Any]:
    return {
        "task_type": task_type,
        "is_correct": False,
        "score_0_1": 0.0,
        "mistakes": [reason],
        "correction": "",
        "explanation": "",
        "confidence": 0.0
    }


def grade_with_llm(task_type: str, card: Dict[str, Any], user_answer: str,
                   retry: int = 1, force_tool: bool = True) -> Dict[str, Any]:
    """
    强制工具调用；若未触发则自动重试；仍失败则尝试解析正文 JSON，最后返回兜底结果。
    """
    client = setup_client()
    messages = [{"role": "system", "content": _SYSTEM},
                _build_user_msg(task_type, card, user_answer)]

    # 第一次请求（强制工具调用）
    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            temperature=0.0,
            messages=messages,
            tools=_TOOLS,
            tool_choice={"type": "function", "function": {
                "name": "grade_vocab_response"}} if force_tool else "auto"
        )
    except Exception as ex:
        return _fallback(task_type, f"调用异常：{ex}")

    msg = resp.choices[0].message
    calls = getattr(msg, "tool_calls", None)
    if calls:
        try:
            return json.loads(calls[0].function.arguments or "{}")
        except Exception:
            pass  # 继续下面的重试/解析

    # 自动重试（再强制一次）
    for _ in range(max(0, retry)):
        try:
            resp = client.chat.completions.create(
                model=MODEL_NAME,
                temperature=0.0,
                messages=messages,
                tools=_TOOLS,
                tool_choice={"type": "function", "function": {
                    "name": "grade_vocab_response"}}
            )
            msg = resp.choices[0].message
            calls = getattr(msg, "tool_calls", None)
            if calls:
                try:
                    return json.loads(calls[0].function.arguments or "{}")
                except Exception:
                    pass
        except Exception as ex:
            return _fallback(task_type, f"重试异常：{ex}")

    # 备用：如果模型把 JSON 填在正文里（不规范），尝试解析正文
    try:
        if msg and msg.content:
            return json.loads(msg.content)
    except Exception:
        pass

    # 仍失败 -> 兜底
    return _fallback(task_type, "模型未调用工具")
