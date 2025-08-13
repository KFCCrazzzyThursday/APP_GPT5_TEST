# -*- coding: utf-8 -*-
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional, Callable
import json
import random
import time
import threading
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

from config import MODEL_NAME
from api_client import setup_client
from utils.jsonio import dump_json_atomic, load_vocab_array, try_load_resume
from utils.textops import has_chinese

SYSTEM_ENRICH = (
    "你是一名双语英语词汇教练，负责『修复并丰富』给定单词的数据。"
    "输出必须是严格的 JSON 对象（不要额外文字），字段："
    "{"
    "\"word\":\"英文原词\","
    "\"meaning_zh\":\"用简体中文给出完整释义（必要时补全与改进；精炼且覆盖常见义项）\","
    "\"pos\":\"词性，若多词性可用数组或用 / 分隔\","
    "\"synonyms_en\":[\"近义词(英文)\"],"
    "\"phrases\":[{\"phrase\":\"常用搭配/短语\",\"meaning_zh\":\"中文释义\"}],"
    "\"example\":{\"en\":\"地道例句(含该词)\",\"zh\":\"上句的中文翻译\"},"
    "\"confusions\":[{\"with\":\"易混词\",\"tip_zh\":\"如何区分（中文）\"}],"
    "\"model_notes\":\"写给LLM的备注（多义/用法/考试高频等）\""
    "}"
)


def need_fix(m: str) -> bool:
    if not m:
        return True
    if len(m.replace(" ", "")) < 2:
        return True
    if not has_chinese(m):
        return True
    return False


def enrich_one(word: str, meaning_hint: str = "") -> Dict[str, Any]:
    client = setup_client()
    user = {"role": "user",
            "content": f"单词：{word}\n原始释义（可能不完整或为空）：{meaning_hint or '（空）'}\n请按指定 JSON 模板返回。"}
    resp = client.chat.completions.create(model=MODEL_NAME, temperature=0.2,
                                          messages=[{"role": "system", "content": SYSTEM_ENRICH}, user])
    txt = (resp.choices[0].message.content or "{}")
    s, e = txt.find("{"), txt.rfind("}")
    if s != -1 and e != -1 and e > s:
        txt = txt[s:e+1]
    try:
        obj = json.loads(txt)
    except Exception:
        obj = {"word": word, "meaning_zh": meaning_hint or "", "pos": "", "synonyms_en": [],
               "phrases": [], "example": {"en": "", "zh": ""}, "confusions": [], "model_notes": "LLM解析失败，保留原始释义。"}
    for k, v in [("word", word), ("meaning_zh", meaning_hint or ""), ("pos", ""),
                 ("synonyms_en", []), ("phrases", []
                                       ), ("example", {"en": "", "zh": ""}),
                 ("confusions", []), ("model_notes", "")]:
        obj.setdefault(k, v)
    return obj


def _backoff(i: int): time.sleep(min(8.0, 0.6*(2**i)+random.uniform(0, 0.25)))


def enrich_file(
    input_json: Path,
    output_json: Path,
    batch_size: int = 4,
    checkpoint_every: int = 20,
    only_fix_missing: bool = False,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    show_tqdm: bool = False
) -> Path:
    items = load_vocab_array(input_json)
    total = len(items)

    resume = try_load_resume(output_json)
    resume_entries = None
    if resume and resume.get("meta", {}).get("source") == str(input_json.resolve()):
        resume_entries = resume.get("entries", [])
        print(
            f"[resume] Found {len(resume_entries)}/{total} done; resume from there.")

    results: Dict[int, Dict[str, Any]] = {}
    done = 0
    if resume_entries:
        for i, e in enumerate(resume_entries):
            results[i] = e
        done = len(resume_entries)
        if progress_cb:
            progress_cb(done, total)

    lock = threading.Lock()
    pbar = tqdm(total=total, initial=done, disable=not show_tqdm)

    def worker(i: int):
        word = items[i]["word"]
        meaning = items[i]["meaning"]
        if only_fix_missing and not need_fix(meaning):
            return i, {"word": word, "meaning_zh": meaning, "pos": "", "synonyms_en": [],
                       "phrases": [], "example": {"en": "", "zh": ""}, "confusions": [], "model_notes": "原释义较完整，未调用LLM。"}
        for attempt in range(5):
            try:
                return i, enrich_one(word, meaning)
            except Exception as ex:
                if attempt == 4:
                    return i, {"word": word, "meaning_zh": meaning, "pos": "", "synonyms_en": [],
                               "phrases": [], "example": {"en": "", "zh": ""}, "confusions": [],
                               "model_notes": f"调用失败：{ex}"}
                _backoff(attempt)

    # 提交任务
    with ThreadPoolExecutor(max_workers=max(1, batch_size)) as ex:
        futs = [ex.submit(worker, i) for i in range(done, total)]
        for fut in as_completed(futs):
            i, enriched = fut.result()
            with lock:
                results[i] = enriched
                done += 1
                if checkpoint_every and (done % checkpoint_every == 0 or done == total):
                    entries = [results[j]
                               for j in range(0, done) if j in results]
                    payload = {"meta": {
                        "source": str(input_json.resolve()),
                        "model": MODEL_NAME,
                        "count": len(entries),
                        "status": "partial" if done < total else "complete",
                        "progress": {"done": done, "total": total, "checkpoint_every": checkpoint_every},
                        "batch_size": batch_size
                    },
                        "entries": entries}
                    dump_json_atomic(output_json, payload)
                if progress_cb:
                    progress_cb(done, total)
                pbar.update(1)

    pbar.close()

    entries = [results[j] for j in range(total) if j in results]
    payload = {"meta": {"source": str(input_json.resolve()), "model": MODEL_NAME,
                        "count": len(entries), "status": "complete", "batch_size": batch_size},
               "entries": entries}
    dump_json_atomic(output_json, payload)
    return output_json
