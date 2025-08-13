# -*- coding: utf-8 -*-
from pathlib import Path
from typing import Any, Dict, List, Optional
import orjson

def load_json(path: Path) -> Any:
    return orjson.loads(path.read_bytes())

def dump_json_atomic(path: Path, obj: Any):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(orjson.dumps(obj, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS))
    tmp.replace(path)

def load_vocab_array(path: Path) -> List[Dict[str, str]]:
    """兼容两种输入：数组 或 {entries:[...]}"""
    data = load_json(path)
    if isinstance(data, dict) and "entries" in data:
        data = data["entries"]
    if not isinstance(data, list):
        raise ValueError("input JSON must be an array or {entries:[...]}")
    out = []
    for e in data:
        out.append({"word": (e.get("word") or "").strip(),
                    "meaning": (e.get("meaning") or e.get("meaning_zh") or "").strip()})
    return out

def try_load_resume(out_path: Path) -> Optional[Dict[str, Any]]:
    try:
        if out_path.exists():
            data = load_json(out_path)
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                return data
    except Exception:
        pass
    return None
