#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
只为缺失的词补充 IPA（国际音标），尽量离线，不浪费 token。
优先用离线库 eng_to_ipa；可选 --use-llm 仅对离线失败的词做极简 LLM 兜底。

用法：
  python add_ipa.py path/to/enrich_0.json --out path/to/enrich_0_ipa.json
  # 就地覆盖（谨慎）：
  python add_ipa.py path/to/enrich_0.json --overwrite
  # 对离线失败的极少数词启用 LLM 兜底：
  python add_ipa.py enrich_0.json --use-llm --limit 50 --overwrite

依赖：
  pip install eng_to_ipa orjson
（如用 LLM 兜底，还需你已有的 api_client.py / config.py）
"""
from __future__ import annotations
import argparse
import re
from pathlib import Path
import orjson

# --- 尝试离线库（不存在也不报错，稍后给提示）
try:
    import eng_to_ipa as e2i  # pip install eng_to_ipa
except Exception:  # noqa
    e2i = None

# 可选：LLM 兜底


def _llm_ipa(word: str) -> str | None:
    try:
        from api_client import setup_client
        from config import MODEL_NAME
        client = setup_client()
        sys = {"role": "system",
               "content": "Return ONLY the IPA (no slashes), American English if possible. No extra text."}
        usr = {"role": "user", "content": f"Give IPA for: {word}"}
        resp = client.chat.completions.create(
            model=MODEL_NAME, messages=[sys, usr], temperature=0)
        txt = (resp.choices[0].message.content or "").strip()
        # 取出中间 {...} 之外的，去掉 /[..]/ 或 [..]
        txt = txt.strip().strip("/").strip("[]").strip()
        # 合法性：必须含元音/拉丁字母或 IPA 符号
        if len(txt) >= 1 and len(txt) <= 64:
            return txt
    except Exception:
        return None
    return None


def _safe_load(path: Path):
    raw = path.read_bytes()
    if not raw:
        return {"entries": []}
    try:
        data = orjson.loads(raw)
    except Exception:
        # 尝试 utf-8-sig 情况
        data = orjson.loads(raw.lstrip(b"\xef\xbb\xbf"))
    if isinstance(data, list):
        return {"entries": data}
    if isinstance(data, dict):
        data.setdefault("entries", [])
        return data
    raise ValueError("JSON must be list or dict with 'entries'.")


def _save(path: Path, obj: dict):
    path.write_bytes(orjson.dumps(
        obj, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS))


_VOWELS = set("aeiou")


def _looks_like_word(w: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z\-']*", w or ""))


def _norm_ipa(s: str) -> str:
    s = (s or "").strip()
    s = s.strip("/").strip("[]").strip()
    # 简单清洗多余空格
    s = re.sub(r"\s+", " ", s)
    return s


def _offline_ipa(word: str) -> str | None:
    if not e2i:
        return None
    try:
        txt = e2i.convert(word)  # 可能返回含斜杠或空
        txt = _norm_ipa(txt)
        # eng_to_ipa 有时返回原词或空；做下过滤
        if not txt or txt.lower() == word.lower():
            return None
        # 简单有效性检查：包含元音/IPA 符号
        if re.search(r"[aeiouɑæɐɜʊʌəɪɛɒɔɚɝɾːˈˌ]", txt):
            return txt
    except Exception:
        return None
    return None


def add_ipa(in_path: Path, out_path: Path, use_llm: bool = False, limit: int | None = None):
    data = _safe_load(in_path)
    entries = data.get("entries", [])
    patched = 0
    llm_used = 0

    for e in entries:
        ent = e.get("entry") if isinstance(e.get("entry"), dict) else e
        word = ent.get("word") or e.get("word")
        if not word:
            continue

        # 已有 ipa 且非空 → 跳过
        ipa_now = ent.get("ipa") or e.get("ipa") or ""
        if isinstance(ipa_now, str) and ipa_now.strip():
            continue

        if not _looks_like_word(word):
            continue

        # 1) 离线尝试
        ipa = _offline_ipa(word)

        # 2)（可选）LLM 兜底
        if not ipa and use_llm:
            if (limit is None) or (llm_used < int(limit)):
                ipa = _llm_ipa(word)
                if ipa:
                    llm_used += 1

        if ipa:
            ipa = _norm_ipa(ipa)
            # 写入到 entry（如果有嵌套）
            if isinstance(e.get("entry"), dict):
                e["entry"]["ipa"] = ipa
            else:
                e["ipa"] = ipa
            patched += 1

    data["meta"] = data.get("meta", {})
    data["meta"]["ipa_added"] = {"patched": patched, "llm_used": llm_used}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _save(out_path, data)
    print(
        f"✓ patched: {patched}, llm_used: {llm_used}, saved → {out_path.resolve()}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json", type=Path)
    g = ap.add_mutually_exclusive_group(required=False)
    g.add_argument("--out", type=Path, help="输出到新文件")
    g.add_argument("--overwrite", action="store_true", help="就地覆盖（谨慎）")
    ap.add_argument("--use-llm", action="store_true", help="仅对离线失败的词用 LLM 兜底")
    ap.add_argument("--limit", type=int, default=None,
                    help="LLM 兜底词数上限（防止 token 浪费）")
    args = ap.parse_args()

    out = args.json if args.overwrite else (
        args.out or args.json.with_name(args.json.stem + "_ipa.json"))
    add_ipa(args.json, out, use_llm=args.use_llm, limit=args.limit)


if __name__ == "__main__":
    main()
