# -*- coding: utf-8 -*-
import json
from typing import List, Dict, Any
from config import MODEL_NAME
from api_client import setup_client

SYSTEM = (
    "你将阅读一段英文文章，挑选 5~20 个『值得学习』的英文词或短语。"
    "优先：罕见/学术/政策/金融/易混/重要搭配。"
    "通过函数工具返回候选列表（含理由与难度 1~5）。"
)

TOOLS = [{
    "type":"function",
    "function":{
        "name":"propose_words_from_text",
        "description":"从长文本中挑选值得学习的候选词并给出理由。",
        "parameters":{
            "type":"object",
            "properties":{
                "candidates":{
                    "type":"array",
                    "items":{"type":"object",
                             "properties":{"word":{"type":"string"},
                                           "reason":{"type":"string"},
                                           "difficulty_1_5":{"type":"integer"}},
                             "required":["word"]}
                }
            },
            "required":["candidates"]
        }
    }
}]

def propose_from_text(text: str, k_min:int=5, k_max:int=20) -> List[Dict[str, Any]]:
    client = setup_client()
    user = {"role":"user","content": f"文章：\n{text}\n请挑选 {k_min}~{k_max} 个候选并调用 propose_words_from_text。"}
    resp = client.chat.completions.create(
        model=MODEL_NAME, temperature=0.0,
        messages=[{"role":"system","content":SYSTEM}, user],
        tools=TOOLS, tool_choice="auto"
    )
    msg = resp.choices[0].message
    calls = getattr(msg, "tool_calls", None)
    if not calls: return []
    args = calls[0].function.arguments or "{}"
    try:
        data = json.loads(args)
        return data.get("candidates", [])
    except Exception:
        return []
