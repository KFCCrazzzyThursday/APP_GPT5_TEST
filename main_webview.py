# -*- coding: utf-8 -*-
import json
import datetime
import threading
from pathlib import Path
import hashlib
import webview

from gui_web.backend_tools import apply_tool, redact_for_log
from api_client import setup_client
from config import MODEL_NAME

APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR if (APP_DIR / "index.html").exists() else APP_DIR / "web"

DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_STORE = DATA_DIR / "store.json"
PROGRESS_PATH = DATA_DIR / "progress.json"

_lock = threading.Lock()


# ---------- helpers ----------
def _ensure_store(path: Path):
    if not path.exists():
        path.write_text(json.dumps(
            {"entries": []}, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_progress():
    if not PROGRESS_PATH.exists():
        PROGRESS_PATH.write_text(json.dumps(
            {"stores": {}, "settings": {}}, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_read_json(path: Path):
    if not path.exists():
        return None
    try:
        txt = path.read_text(encoding="utf-8-sig", errors="ignore")
    except Exception:
        try:
            txt = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None
    if not txt or not txt.strip():
        return None
    try:
        obj = json.loads(txt)
        if isinstance(obj, list):
            return {"entries": obj}
        return obj
    except Exception:
        return None


def _progress_for_store(store: Path) -> dict:
    _ensure_progress()
    data = _safe_read_json(PROGRESS_PATH) or {}
    stores = data.setdefault("stores", {})
    key = str(store.resolve())
    prof = stores.get(key)
    if not prof:
        prof = {"days": {}, "last_session": None}
        stores[key] = prof
        PROGRESS_PATH.write_text(json.dumps(
            data, ensure_ascii=False, indent=2), encoding="utf-8")
    return prof


def _write_progress(store: Path, prof: dict):
    _ensure_progress()
    data = _safe_read_json(PROGRESS_PATH) or {}
    stores = data.setdefault("stores", {})
    key = str(store.resolve())
    stores[key] = prof
    PROGRESS_PATH.write_text(json.dumps(
        data, ensure_ascii=False, indent=2), encoding="utf-8")


def _today_key():
    return datetime.date.today().isoformat()


# ---- settings: remember last store & recent list ----
def _get_settings() -> dict:
    _ensure_progress()
    data = _safe_read_json(PROGRESS_PATH) or {}
    return data.setdefault("settings", {})


def _write_settings(settings: dict):
    _ensure_progress()
    data = _safe_read_json(PROGRESS_PATH) or {}
    data["settings"] = settings or {}
    PROGRESS_PATH.write_text(json.dumps(
        data, ensure_ascii=False, indent=2), encoding="utf-8")


def _remember_store(path: Path, max_keep: int = 10):
    settings = _get_settings()
    p = str(path.resolve())
    recent = settings.get("recent_stores") or []
    recent = [x for x in recent if x != p]
    recent.insert(0, p)
    settings["recent_stores"] = recent[:max_keep]
    settings["last_store"] = p
    _write_settings(settings)


def _recent_stores() -> list[str]:
    settings = _get_settings()
    return settings.get("recent_stores") or []


def _last_store_path() -> Path | None:
    settings = _get_settings()
    p = settings.get("last_store")
    if not p:
        return None
    pt = Path(p)
    return pt if pt.exists() else None


def _ever_learned_words(store: Path) -> set[str]:
    """Union of words ever recorded as learned in progress.json."""
    prof = _progress_for_store(store)
    days = prof.get("days", {}) or {}
    out = set()
    for rec in days.values():
        for w in rec.get("words", []) or []:
            out.add(w)
    return out


def _today_learned_set(store: Path) -> set[str]:
    prof = _progress_for_store(store)
    return set(prof.get("days", {}).get(_today_key(), {}).get("words", []) or [])


def _safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        try:
            return float(str(x).strip())
        except Exception:
            return default


def _safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        try:
            return int(float(str(x).strip()))
        except Exception:
            return default


def _stable_hash(val: str) -> int:
    """Deterministic hash for per-day stable shuffle."""
    return int(hashlib.sha1(val.encode("utf-8")).hexdigest(), 16)


# ---------- API bridge ----------
class ApiBridge:
    def __init__(self):
        # load last store if remembered
        last = _last_store_path()
        self.store_path = last if last else DEFAULT_STORE
        _ensure_store(self.store_path)

        self.chat = [{
            "role": "system",
            "content": (
                "You are a helpful bilingual vocab tutor. "
                "Reply concisely with Chinese+English, include IPA, stress, POS, collocations, and 1–2 short examples."
            )
        }]

        try:
            self.client = setup_client()
        except Exception:
            self.client = None

        self.model = MODEL_NAME or "gpt-5-chat"

    # ---------- 文件对话框 ----------
    def open_store_dialog(self, mode: str = "open"):
        try:
            w = webview.windows[0] if webview.windows else None
            if not w:
                return {"ok": False, "error": "window not ready"}
            dlg = webview.OPEN_DIALOG if mode == "open" else webview.SAVE_DIALOG
            files = w.create_file_dialog(dlg, allow_multiple=False, file_types=(
                'JSON files (*.json)', 'All files (*.*)'))
            if not files:
                return {"ok": False, "error": "cancelled"}
            path = Path(files if isinstance(files, str) else files[0])
            if mode == "open":
                if not path.exists():
                    return {"ok": False, "error": f"file not found: {path}"}
            else:
                if not path.suffix.lower().endswith(".json"):
                    path = path.with_suffix(".json")
                if not path.exists():
                    path.write_text(json.dumps(
                        {"entries": []}, ensure_ascii=False, indent=2), encoding="utf-8")
            self.store_path = path
            _ensure_store(self.store_path)
            _remember_store(self.store_path)
            return {"ok": True, "path": str(self.store_path)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # 快速切换：最近词库
    def list_recent_stores(self):
        return {"ok": True, "items": _recent_stores(), "current": str(self.store_path)}

    def switch_store(self, path: str):
        try:
            p = Path(path)
            if not p.exists():
                return {"ok": False, "error": f"file not found: {path}"}
            self.store_path = p
            _ensure_store(self.store_path)
            _remember_store(self.store_path)
            return {"ok": True, "path": str(self.store_path)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_current_store_path(self):
        return {"ok": True, "path": str(self.store_path)}

    # ---------- Chat（保持你的原逻辑） ----------
    def send_message(self, text: str) -> dict:
        with _lock:
            self.chat.append({"role": "user", "content": text})
        logs = []

        if not self.client:
            reply = "(LLM 未配置)。左侧记忆卡功能完全可用。"
            with _lock:
                self.chat.append({"role": "assistant", "content": reply})
            return {"ok": True, "assistant": reply, "logs": logs}

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=self.chat,
                tool_choice="auto",
                tools=[
                    {"type": "function", "function": {"name": "record_signal_tool", "parameters": {"type": "object", "properties": {
                        "word": {"type": "string"}, "signal": {"type": "string"}, "note": {"type": "string"}}}}},
                    {"type": "function", "function": {"name": "commit_review", "parameters": {
                        "type": "object", "properties": {"word": {"type": "string"}, "override_score": {"type": "number"}}}}},
                    {"type": "function", "function": {"name": "sample_study_items", "parameters": {
                        "type": "object", "properties": {"k": {"type": "integer"}, "min_days_gap": {"type": "number"}}}}},
                    {"type": "function", "function": {"name": "get_word", "parameters": {
                        "type": "object", "properties": {"word": {"type": "string"}}}}},
                    {"type": "function", "function": {"name": "plan_daily_new", "parameters": {
                        "type": "object", "properties": {"k": {"type": "integer"}}}}},
                    {"type": "function", "function": {"name": "sample_by_priority", "parameters": {
                        "type": "object", "properties": {"k": {"type": "integer"}}}}},
                ],
                temperature=0.2
            )
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []

            if tool_calls:
                tc_list = []
                for tc in tool_calls:
                    fn = getattr(tc, "function", None)
                    tc_list.append({
                        "id": getattr(tc, "id", None),
                        "type": "function",
                        "function": {
                            "name": getattr(fn, "name", None) if fn else None,
                            "arguments": getattr(fn, "arguments", "") if fn else ""
                        }
                    })
                self.chat.append({
                    "role": "assistant",
                    "content": getattr(msg, "content", "") or "",
                    "tool_calls": tc_list
                })
            else:
                self.chat.append(
                    {"role": "assistant", "content": getattr(msg, "content", "") or ""})

            assistant_text = getattr(msg, "content", "") or ""

            if tool_calls:
                for tc in tool_calls:
                    fn = getattr(tc, "function", None)
                    name = getattr(fn, "name", None) if fn else None
                    args_str = getattr(fn, "arguments", "{}") if fn else "{}"
                    if not isinstance(args_str, str) or not args_str.strip():
                        args_str = "{}"
                    try:
                        args = json.loads(args_str)
                    except Exception:
                        args = {}

                    r = apply_tool(name, args, self.store_path)
                    logs.append({"tool": name, "args": args,
                                "result": redact_for_log(r)})

                    self.chat.append({
                        "role": "tool",
                        "tool_call_id": getattr(tc, "id", "") or "",
                        "name": name,
                        "content": json.dumps(r, ensure_ascii=False)
                    })

                resp2 = self.client.chat.completions.create(
                    model=self.model,
                    messages=self.chat,
                    temperature=0.2
                )
                msg2 = resp2.choices[0].message
                assistant_text = getattr(msg2, "content", "") or assistant_text

            with _lock:
                self.chat.append(
                    {"role": "assistant", "content": assistant_text})

            return {"ok": True, "assistant": assistant_text, "logs": logs}

        except Exception as e:
            return {"ok": False, "assistant": f"(LLM error: {e})", "logs": logs}

    # ---------- 前端直连工具 ----------
    def record_signal_tool(self, word: str, signal: str, note: str = ""):
        return apply_tool("record_signal_tool", {"word": word, "signal": signal, "note": note}, self.store_path)

    def commit_review(self, word: str, outcome: float | None = None):
        return apply_tool("commit_review", {"word": word, "override_score": outcome}, self.store_path)

    def sample_study_items(self, k: int = 20, min_days_gap: float = 1.0):
        return apply_tool("sample_study_items", {"k": _safe_int(k, 20), "min_days_gap": _safe_float(min_days_gap, 1.0)}, self.store_path)

    def get_word(self, word: str):
        return apply_tool("get_word", {"word": word}, self.store_path)

    # ---- FIXED: plan_daily_new chooses NEW words with per-day stable shuffle
    def plan_daily_new(self, k: int = 100):
        """
        选择“新词”为主：review_count == 0 且从未在 progress.json 里出现过，
        并且排除今天已经学习过的词。使用“按天稳定的乱序”让每天不同但当天多次一致。
        若新词不足，按 (avg_score升序, review_count升序) 进行补充。
        """
        k = _safe_int(k, 100)
        raw = _safe_read_json(self.store_path) or {}
        entries = raw.get("entries") or raw.get("words") or []

        # 构建集合
        ever = _ever_learned_words(self.store_path)
        today_set = _today_learned_set(self.store_path)
        # 稳定种子（同一天固定、不同天变化；与具体词库绑定）
        seed_str = f"{_today_key()}|{str(self.store_path.resolve())}|{len(entries)}"

        def entry_iter():
            for e in entries:
                ent = e.get("entry") if isinstance(e, dict) else None
                if ent is None:
                    ent = e if isinstance(e, dict) else {}
                w = (e.get("word") if isinstance(e, dict)
                     else None) or ent.get("word")
                if not w:
                    continue
                srs = e.get("srs") or ent.get("srs") or {}
                rc = _safe_int(srs.get("review_count", srs.get("n", 0)), 0)
                avg = _safe_float(
                    srs.get("avg_score", srs.get("score", 1.0)), 1.0)
                yield w, ent, rc, avg

        # 新词候选
        fresh = []
        for w, ent, rc, avg in entry_iter():
            if rc == 0 and (w not in ever) and (w not in today_set):
                h = _stable_hash(w + "|" + seed_str)
                fresh.append((h, w, ent))

        fresh.sort(key=lambda t: t[0])
        picked = [{"word": w, "entry": ent} for _, w, ent in fresh[:k]]

        # 若不够，补充“弱项/低复习次数”，排除今天已学
        if len(picked) < k:
            remain = k - len(picked)
            supplement = []
            for w, ent, rc, avg in entry_iter():
                if w in today_set:
                    continue
                if any(x["word"] == w for x in picked):
                    continue
                h = _stable_hash("S|" + w + "|" + seed_str)
                supplement.append((avg, rc, h, w, ent))
            supplement.sort(key=lambda t: (t[0], t[1], t[2]))
            picked.extend([{"word": w, "entry": ent}
                          for _, _, _, w, ent in supplement[:remain]])

        return {"ok": True, "items": picked}

    def sample_by_priority(self, k: int = 100):
        return apply_tool("sample_by_priority", {"k": _safe_int(k, 100)}, self.store_path)

    def sample_by_score(self, k: int = 100, learned_only: bool = True):
        """
        Review-by-score: 默认只抽“已学/已复习”的词（learned_only=True）。
        已学判定：srs.review_count > 0 或在 progress.json 的 days[*].words 出现过。
        """
        raw = _safe_read_json(self.store_path) or {}
        entries = raw.get("entries") or raw.get("words") or []
        ever = _ever_learned_words(self.store_path) if learned_only else set()

        items = []
        for e in entries:
            ent = e.get("entry") or e
            w = ent.get("word")
            if not w:
                continue
            srs = e.get("srs") or ent.get("srs") or {}
            avg = _safe_float(srs.get("avg_score", srs.get("score", 1.0)), 1.0)
            n = _safe_int(srs.get("review_count", srs.get("n", 0)), 0)

            if learned_only and n <= 0 and (w not in ever):
                continue

            items.append({"word": w, "entry": ent, "score": avg, "n": n})

        items.sort(key=lambda x: (x["score"], x["n"]))
        items = items[:max(1, _safe_int(k, 100))]
        return {"ok": True, "items": [{"word": it["word"], "entry": it["entry"]} for it in items]}

    def update_score(self, word: str, value: float):
        raw = _safe_read_json(self.store_path) or {"entries": []}
        entries = raw.get("entries") or raw.get("words") or []
        updated = False
        now_ts = datetime.datetime.utcnow().isoformat()

        for e in entries:
            ent = e.get("entry") or e
            w = ent.get("word")
            if w != word:
                continue
            srs = e.get("srs") or ent.get("srs") or {}
            n = _safe_int(srs.get("review_count", srs.get("n", 0)), 0)
            avg = _safe_float(srs.get("avg_score", srs.get("score", 0.5)), 0.5)
            new_n = n + 1
            new_avg = (avg * n + _safe_float(value, 0.0)) / max(1, new_n)
            srs["review_count"] = new_n
            srs["avg_score"] = new_avg
            srs["last_ts"] = now_ts
            if "srs" in e:
                e["srs"] = srs
            else:
                ent["srs"] = srs
                if "entry" in e:
                    e["entry"] = ent
            updated = True
            break

        if updated:
            raw["entries"] = entries
            self.store_path.write_text(json.dumps(
                raw, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": updated}

    # ---------- 会话持久化 ----------
    def save_session_state(self, state: dict | None):
        prof = _progress_for_store(self.store_path)
        prof["last_session"] = state
        _write_progress(self.store_path, prof)
        return {"ok": True}

    def load_session_state(self):
        prof = _progress_for_store(self.store_path)
        return {"ok": True, "state": prof.get("last_session")}

    def clear_session_state(self):
        prof = _progress_for_store(self.store_path)
        prof["last_session"] = None
        _write_progress(self.store_path, prof)
        return {"ok": True}

    def note_learn_event(self, word: str):
        prof = _progress_for_store(self.store_path)
        days = prof.setdefault("days", {})
        today = _today_key()
        drec = days.setdefault(today, {"words": []})
        if word not in drec["words"]:
            drec["words"].append(word)
            _write_progress(self.store_path, prof)
        return {"ok": True, "today": len(set(drec["words"]))}

    def sample_today_all(self):
        prof = _progress_for_store(self.store_path)
        words = list(dict.fromkeys(prof.get("days", {}).get(
            _today_key(), {}).get("words", [])))
        if not words:
            return {"ok": True, "items": []}
        raw = _safe_read_json(self.store_path) or {}
        entries = raw.get("entries") or raw.get("words") or []
        out = []
        for e in entries:
            ent = e.get("entry") or e
            w = (e.get("word") if isinstance(e, dict)
                 else None) or ent.get("word")
            if w in words:
                out.append({"word": w, "entry": ent})
        return {"ok": True, "items": out}

    def progress_snapshot(self):
        raw = _safe_read_json(self.store_path) or {}
        entries = raw.get("entries") or raw.get("words") or []
        total = len(entries)
        learned = 0
        mastered = 0
        for e in entries:
            srs = e.get("srs") or e.get("review") or {}
            rc = _safe_int(srs.get("review_count") or srs.get(
                "n") or srs.get("reviews") or 0, 0)
            if rc > 0:
                learned += 1
                last_score = _safe_float(
                    srs.get("avg_score", srs.get("score", 0.5)), 0.5)
                interval = _safe_float(
                    srs.get("interval_days", srs.get("interval", 0.0)), 0.0)
                if rc >= 3 and (last_score >= 0.6 or interval >= 3.0):
                    mastered += 1
        prof = _progress_for_store(self.store_path)
        today_cnt = len(set(prof.get("days", {}).get(
            _today_key(), {}).get("words", [])))
        return {"ok": True, "total": total, "learned": learned, "mastered": mastered, "today_learned": today_cnt}


def run():
    index_path = (WEB_DIR / "index.html")
    url = index_path.as_uri()
    print(f"[main_webview] Loading URL: {url}")
    window = webview.create_window(
        title="Vocab Suite — WebView",
        url=url,
        js_api=ApiBridge(),
        width=1320, height=880, resizable=True
    )
    try:
        webview.start(debug=True, gui="edgechromium")
    except Exception:
        webview.start(debug=True)


if __name__ == "__main__":
    run()
