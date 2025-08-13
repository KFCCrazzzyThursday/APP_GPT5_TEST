# -*- coding: utf-8 -*-
import json
import datetime
import threading
from pathlib import Path
import webview

from gui_web.backend_tools import apply_tool, redact_for_log
from api_client import setup_client            # 你的零参工厂，里头已连 NUWA
from config import MODEL_NAME                  # 模型名

APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR if (APP_DIR / "index.html").exists() else APP_DIR / "web"

DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_STORE = DATA_DIR / "store.json"
PROGRESS_PATH = DATA_DIR / "progress.json"

_lock = threading.Lock()


def _ensure_store(path: Path):
    if not path.exists():
        path.write_text(json.dumps(
            {"entries": []}, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_progress():
    if not PROGRESS_PATH.exists():
        PROGRESS_PATH.write_text(json.dumps(
            {"stores": {}}, ensure_ascii=False, indent=2), encoding="utf-8")


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

# ---------- 新增：安全读取 JSON，容错 BOM / 空文件 / 顶层 list ----------


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


def _getattr_or(item, name, default=None):
    try:
        return getattr(item, name)
    except Exception:
        return default


class ApiBridge:
    def __init__(self):
        self.store_path = DEFAULT_STORE
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
            dlg_type = webview.OPEN_DIALOG if mode == "open" else webview.SAVE_DIALOG
            files = w.create_file_dialog(dlg_type, allow_multiple=False, file_types=(
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
            return {"ok": True, "path": str(self.store_path)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- Chat：带工具调用的闭环 ----------
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
            tool_calls = _getattr_or(msg, "tool_calls", None) or []

            if tool_calls:
                tc_list = []
                for tc in tool_calls:
                    fn = _getattr_or(tc, "function", None)
                    tc_list.append({
                        "id": _getattr_or(tc, "id", None),
                        "type": "function",
                        "function": {
                            "name": _getattr_or(fn, "name", None) if fn else None,
                            "arguments": _getattr_or(fn, "arguments", "") if fn else ""
                        }
                    })
                self.chat.append({
                    "role": "assistant",
                    "content": _getattr_or(msg, "content", "") or "",
                    "tool_calls": tc_list
                })
            else:
                self.chat.append({
                    "role": "assistant",
                    "content": _getattr_or(msg, "content", "") or ""
                })

            assistant_text = _getattr_or(msg, "content", "") or ""

            if tool_calls:
                for tc in tool_calls:
                    fn = _getattr_or(tc, "function", None)
                    name = _getattr_or(fn, "name", None) if fn else None
                    args_str = _getattr_or(
                        fn, "arguments", "{}") if fn else "{}"
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
                        "tool_call_id": _getattr_or(tc, "id", "") or "",
                        "name": name,
                        "content": json.dumps(r, ensure_ascii=False)
                    })

                resp2 = self.client.chat.completions.create(
                    model=self.model,
                    messages=self.chat,
                    temperature=0.2
                )
                msg2 = resp2.choices[0].message
                assistant_text = _getattr_or(
                    msg2, "content", "") or assistant_text

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
        return apply_tool("sample_study_items", {"k": int(k), "min_days_gap": float(min_days_gap)}, self.store_path)

    def get_word(self, word: str):
        return apply_tool("get_word", {"word": word}, self.store_path)

    def plan_daily_new(self, k: int = 100):
        return apply_tool("plan_daily_new", {"k": int(k)}, self.store_path)

    def sample_by_priority(self, k: int = 100):
        return apply_tool("sample_by_priority", {"k": int(k)}, self.store_path)

    # ---------- 新增：按分数抽样（供“Review by Score”） ----------
    def sample_by_score(self, k: int = 100):
        raw = _safe_read_json(self.store_path) or {}
        entries = raw.get("entries") or raw.get("words") or []
        items = []
        for e in entries:
            ent = e.get("entry") or e
            w = ent.get("word")
            if not w:
                continue
            srs = e.get("srs") or ent.get("srs") or {}
            avg = float(srs.get("avg_score", srs.get("score", 0.5) or 0.5))
            n = int(srs.get("review_count", srs.get("n", 0) or 0))
            items.append({"word": w, "entry": ent, "score": avg, "n": n})
        items.sort(key=lambda x: (x["score"], x["n"]))  # 低分 & 低复习次数优先
        items = items[:max(1, int(k))]
        # strip helper fields
        return {"ok": True, "items": [{"word": it["word"], "entry": it["entry"]} for it in items]}

    # ---------- 新增：更新分数（供 verify/next 按钮） ----------
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
            # 历史计数与均值
            try:
                n = int(srs.get("review_count", srs.get("n", 0) or 0))
            except Exception:
                n = 0
            try:
                avg = float(srs.get("avg_score", srs.get("score", 0.5) or 0.5))
            except Exception:
                avg = 0.5
            new_n = n + 1
            new_avg = (avg * n + float(value)) / max(1, new_n)
            srs["review_count"] = new_n
            srs["avg_score"] = new_avg
            srs["last_ts"] = now_ts
            # 写回到 e 或 ent
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
            rc = srs.get("review_count") or srs.get(
                "n") or srs.get("reviews") or 0
            try:
                rc = int(rc)
            except Exception:
                rc = 0
            if rc > 0:
                learned += 1
                last_score = float(srs.get("avg_score", srs.get("score", 0.5)))
                interval = float(
                    srs.get("interval_days", srs.get("interval", 0.0)))
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
