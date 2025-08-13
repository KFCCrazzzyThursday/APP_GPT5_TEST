# -*- coding: utf-8 -*-
import re
CN_RE = re.compile(r"[\u3400-\u9FFF\uF900-\uFAFF]")

def has_chinese(s: str) -> bool:
    return bool(CN_RE.search(s or ""))

def normalize_spaces(s: str) -> str:
    return re.sub(r"\s{2,}", " ", (s or "").replace("\r", "\n").replace("\n", " ")).strip()
