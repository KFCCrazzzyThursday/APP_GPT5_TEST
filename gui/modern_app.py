# -*- coding: utf-8 -*-
import json
import os
import re
from pathlib import Path
from datetime import datetime

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox

from api_client import setup_client
from config import MODEL_NAME
from utils.jsonio import load_json, dump_json_atomic
from study.sampler import weighted_sample_without_replacement, anti_repeat_filter, mark_scheduled
from study.srs import DEFAULT_SRS, ensure, update
from agent.propose import propose_from_text  # 若暂时不用，可忽略
# 你已有的其它后端：extract/enrich/study/grader 等仍可复用

CHAT_DIR = Path("data/chats")
CHAT_DIR.mkdir(parents=True, exist_ok=True)

# ----------------- 小工具 -----------------


def _normalize_word(w: str) -> str:
    if not w:
        return w
    w = w.strip().lower()
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 2 and w.endswith("es"):
        return w[:-2]
    if len(w) > 2 and w.endswith("s"):
        return w[:-1]
    return w


def _must_pick_store_or_raise(path_str: str) -> Path:
    p = (path_str or "").strip()
    if not p:
        raise RuntimeError("请选择词库 JSON（enriched.json）后再聊天或写库。")
    path = Path(p)
    if path.is_dir():
        raise RuntimeError(f"选择的是目录：{path}。请选中一个 .json 文件。")
    if path.suffix.lower() != ".json":
        raise RuntimeError(f"词库文件需要是 .json，当前：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _trim_messages(messages, max_len=60):
    return messages[-max_len:]

# -------------- 消息气泡 --------------


class Bubble(ctk.CTkFrame):
    def __init__(self, master, text, me=False):
        super().__init__(master, fg_color="transparent")
        bg = "#E9EEF6" if me else "#2B2B2B"      # 我方浅底 / 助手深底
        fg = "#111111" if me else "#EDEDED"      # 我方黑字 / 助手浅字
        wrap = ctk.CTkFrame(self, corner_radius=14, fg_color=bg)
        wrap.grid(column=(1 if me else 0), row=0,
                  sticky="e" if me else "w", padx=6, pady=4)
        lbl = ctk.CTkLabel(wrap, text=text, wraplength=760,
                           justify="left", text_color=fg)
        lbl.pack(padx=10, pady=8)

# -------------- 可折叠日志 --------------


class Collapsible(ctk.CTkFrame):
    def __init__(self, master, title="Logs", opened=False):
        super().__init__(master)
        self.opened = opened
        self.btn = ctk.CTkButton(self, text=("▾ " if opened else "▸ ") + title,
                                 fg_color="transparent", hover=False,
                                 command=self.toggle, anchor="w")
        self.btn.pack(fill="x", padx=4, pady=2)
        self.body = ctk.CTkFrame(self)
        if opened:
            self.body.pack(fill="both", expand=True, padx=4, pady=2)
        self.txt = ctk.CTkTextbox(self.body, height=160)
        if opened:
            self.txt.pack(fill="both", expand=True)

    def toggle(self):
        self.opened = not self.opened
        self.btn.configure(
            text=("▾ " if self.opened else "▸ ") + self.btn.cget("text")[2:])
        if self.opened:
            self.body.pack(fill="both", expand=True, padx=4, pady=2)
            self.txt.pack(fill="both", expand=True)
        else:
            self.txt.pack_forget()
            self.body.pack_forget()

    def log(self, s: str):
        if not self.opened:
            return
        self.txt.insert("end", s + "\n")
        self.txt.see("end")

    def clear(self):
        self.txt.delete("1.0", "end")

# -------------- 主应用 --------------


class ModernApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("Vocab Suite — Modern")
        self.geometry("1200x800")

        # 左侧：会话栏
        left = ctk.CTkFrame(self, corner_radius=0, width=260)
        left.pack(side="left", fill="y")
        topbar = ctk.CTkFrame(left, fg_color="transparent")
        topbar.pack(fill="x", padx=8, pady=8)
        ctk.CTkButton(topbar, text="New chat", width=80,
                      command=self.new_chat).pack(side="left", padx=4)
        ctk.CTkButton(topbar, text="Save", width=60,
                      command=self.save_chat).pack(side="left", padx=4)
        ctk.CTkButton(topbar, text="Refresh", width=80,
                      command=self.refresh_chat_list).pack(side="left", padx=4)
        self.listbox = tk.Listbox(left, height=28)
        self.listbox.pack(fill="both", expand=True, padx=8, pady=4)
        self.listbox.bind("<<ListboxSelect>>", self.on_select_chat)

        # 右侧：聊天页
        right = ctk.CTkFrame(self, corner_radius=0)
        right.pack(side="left", fill="both", expand=True)

        # 词库路径行
        row1 = ctk.CTkFrame(right, fg_color="transparent")
        row1.pack(fill="x", padx=10, pady=(10, 0))
        ctk.CTkLabel(row1, text="词库 (enriched.json)：").pack(
            side="left", padx=(0, 6))
        self.var_store = tk.StringVar()
        self.ent_store = ctk.CTkEntry(
            row1, textvariable=self.var_store, width=520)
        self.ent_store.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row1, text="选择", width=70,
                      command=self.pick_store).pack(side="left", padx=8)

        # 对话区域（滚动）
        self.scroll = ctk.CTkScrollableFrame(right, height=520)
        self.scroll.pack(fill="both", expand=True, padx=10, pady=10)

        # 日志与选项（可折叠）
        opt = ctk.CTkFrame(right, fg_color="transparent")
        opt.pack(fill="x", padx=10, pady=(0, 4))
        self.hide_ans = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(opt, text="隐藏参考答案(meaning_zh/zh)",
                        variable=self.hide_ans).pack(side="left", padx=6)
        self.logs = Collapsible(right, title="工具调用日志", opened=False)
        self.logs.pack(fill="x", padx=10, pady=(0, 8))

        # 输入区：多行 + Enter 发送 / Shift+Enter 换行 + 自动增高≤5行
        bottom = ctk.CTkFrame(right)
        bottom.pack(fill="x", padx=10, pady=10)
        self.txt_input = tk.Text(bottom, height=1, wrap="word", undo=True, bg="#1f1f1f", fg="#eaeaea",
                                 insertbackground="#eaeaea", relief="flat")
        self.txt_input.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.in_scroll = tk.Scrollbar(bottom, command=self.txt_input.yview)
        self.txt_input.configure(yscrollcommand=self._on_text_scroll)
        ctk.CTkButton(bottom, text="发送", width=80,
                      command=self.chat_send).pack(side="left")

        self.txt_input.bind("<Return>", self._on_input_return)
        self.txt_input.bind("<Shift-Return>", self._on_input_shift_return)
        self.txt_input.bind("<KeyRelease>", self._adjust_input_height)

        # LLM & tools
        self.client = setup_client()
        self.tools = self._tool_specs()

        # 会话历史
        self.chat_history = self._initial_system_messages()
        self.current_chat_path = None
        self.refresh_chat_list()
        self.new_chat()

    # ---------- 输入框行为 ----------
    def _on_text_scroll(self, *args):
        # 超过5行才显示滚动条
        lines = int(self.txt_input.index("end-1c").split(".")[0])
        if lines > 5:
            if not getattr(self, "_in_scroll_shown", False):
                self.in_scroll.pack(side="right", fill="y")
                self._in_scroll_shown = True
        else:
            if getattr(self, "_in_scroll_shown", False):
                self.in_scroll.pack_forget()
                self._in_scroll_shown = False

    def _adjust_input_height(self, *_):
        try:
            lines = int(self.txt_input.index("end-1c").split(".")[0])
            new_h = max(1, min(5, lines))
            if int(self.txt_input.cget("height")) != new_h:
                self.txt_input.configure(height=new_h)
            self._on_text_scroll()
        except Exception:
            pass

    def _on_input_return(self, event):
        self.chat_send()
        return "break"

    def _on_input_shift_return(self, event):
        self.txt_input.insert("insert", "\n")
        self._adjust_input_height()
        return "break"

    def _input_text(self) -> str:
        return self.txt_input.get("1.0", "end-1c")

    def _input_clear(self):
        self.txt_input.delete("1.0", "end")
        self.txt_input.configure(height=1)
        self._on_text_scroll()
        self.txt_input.focus_set()

    # ---------- 会话管理 ----------
    def refresh_chat_list(self):
        self.listbox.delete(0, "end")
        files = sorted(CHAT_DIR.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files:
            self.listbox.insert("end", p.name)

    def new_chat(self):
        self.chat_history = self._initial_system_messages()
        self.current_chat_path = None
        for w in self.scroll.winfo_children():
            w.destroy()
        Bubble(self.scroll, "（新对话已创建）", me=False).pack(anchor="w", fill="x")

    def save_chat(self):
        title = "chat"
        for m in self.chat_history:
            if m["role"] == "user":
                title = (m["content"] or "chat").strip().splitlines()[0][:40]
                break
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"{ts}_{title}.json"
        path = CHAT_DIR / fname
        path.write_text(json.dumps(self.chat_history,
                        ensure_ascii=False, indent=2), encoding="utf-8")
        self.current_chat_path = path
        self.refresh_chat_list()
        messagebox.showinfo("Saved", f"Saved to {path}")

    def on_select_chat(self, evt):
        sel = self.listbox.curselection()
        if not sel:
            return
        name = self.listbox.get(sel[0])
        path = CHAT_DIR / name
        data = json.loads(path.read_text(encoding="utf-8"))
        self.chat_history = data
        self.current_chat_path = path
        for w in self.scroll.winfo_children():
            w.destroy()
        for m in data[-40:]:
            if m["role"] == "user":
                Bubble(self.scroll, m["content"], me=True).pack(
                    anchor="e", fill="x")
            elif m["role"] == "assistant":
                Bubble(self.scroll, m.get("content", ""),
                       me=False).pack(anchor="w", fill="x")
        self.scroll.update_idletasks()
        self.scroll._parent_canvas.yview_moveto(1.0)

    # ---------- 系统提示 ----------
    def _initial_system_messages(self):
        return [{
            "role": "system", "content": (
                "You are an English vocabulary tutor and study coach.\n"
                "STYLE:\n"
                "- Default to ENGLISH unless the user writes mostly in Chinese.\n"
                "- Be concise, friendly, interactive like a real tutor.\n"
                "- DO NOT reveal Chinese translations unless the user says 'show answer' or after they tried/declined.\n"
                "- Offer short English hints before revealing answers.\n"
                "TOOLS POLICY:\n"
                "- Maintain user lexicon via tools; do not print tool JSON in normal replies.\n"
                "- When user says 'study N words' or 'test me N': call sample_study_items(k=N). "
                "For each word: ask remember? (y/n) -> ask for an English sentence -> call grade_usage(word, sentence) -> "
                "call update_srs(word, score_0_1). Keep going until done.\n"
            )
        }]

    # ---------- 工具定义 ----------
    def _tool_specs(self):
        return [
            {"type": "function", "function": {
                "name": "get_word", "description": "按英文单词精确查询词条；带微弱词形归一。",
                "parameters": {"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]}
            }},
            {"type": "function", "function": {
                "name": "upsert_word", "description": "若词条不存在或释义不完整则 enrich 后写入；可追加来源与备注。",
                "parameters": {"type": "object", "properties": {
                    "word": {"type": "string"}, "meaning_hint": {"type": "string"},
                    "source": {"type": "string"}, "note": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}}
                }, "required": ["word"]}}
             },
            {"type": "function", "function": {
                "name": "update_user_note", "description": "为单词追加一条用户注意事项。",
                "parameters": {"type": "object", "properties": {
                    "word": {"type": "string"}, "append_note": {"type": "string"}
                }, "required": ["word", "append_note"]}}
             },
            {"type": "function", "function": {
                "name": "log_confusion", "description": "记录与另一个单词的混淆及区分提示。",
                "parameters": {"type": "object", "properties": {
                    "word": {"type": "string"}, "confused_with": {"type": "string"}, "tip_zh": {"type": "string"}
                }, "required": ["word", "confused_with"]}}
             },
            {"type": "function", "function": {
                "name": "add_phrase", "description": "为单词添加常用短语/搭配。",
                "parameters": {"type": "object", "properties": {
                    "word": {"type": "string"}, "phrase": {"type": "string"}, "meaning_zh": {"type": "string"}
                }, "required": ["word", "phrase"]}}
             },
            {"type": "function", "function": {
                "name": "search_words", "description": "按子串或标签搜索词条。",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string"}, "tag": {"type": "string"}, "limit": {"type": "integer", "default": 20}
                }}}
             },
            {"type": "function", "function": {
                "name": "set_attr", "description": "设置词条任意属性（如 model_notes/priority_boost）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "word": {"type": "string"},
                        "key": {"type": "string"},
                        "value": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "number"},
                                {"type": "boolean"},
                                {"type": "null"},
                                {"type": "object", "additionalProperties": {}},
                                {"type": "array", "items": {}}
                            ]
                        }
                    },
                    "required": ["word", "key", "value"]
                }
            }},
            # === 新增：按重要性×覆盖保障 加权抽样测验 ===
            {"type": "function", "function": {
                "name": "sample_study_items", "description": "从词库加权抽样若干词用于测验，自动写回调度计数与时间。",
                "parameters": {"type": "object", "properties": {
                    "k": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    "min_days_gap": {"type": "number", "minimum": 0, "default": 1.0}
                }, "required": []}
            }},
            # === 新增：更新 SRS（供模型在测验后调用）===
            {"type": "function", "function": {
                "name": "update_srs", "description": "根据评分更新指定单词的 SRS。",
                "parameters": {"type": "object", "properties": {
                    "word": {"type": "string"},
                    "score_0_1": {"type": "number", "minimum": 0, "maximum": 1}
                }, "required": ["word", "score_0_1"]}}
             },
            {"type": "function", "function": {
                "name": "grade_usage",
                "description": "Grade the user's example sentence for a given word and return a structured score and short feedback in English.",
                "parameters": {"type": "object", "properties": {
                    "word": {"type": "string"},
                    "sentence": {"type": "string"}
                }, "required": ["word", "sentence"]}
            }},

            {"type": "function", "function": {
                "name": "propose_words_from_text", "description": "从长文本中挑选值得学习的候选词并给出理由。",
                "parameters": {"type": "object", "properties": {
                    "source": {"type": "string"},
                    "candidates": {"type": "array", "items": {"type": "object", "properties": {
                        "word": {"type": "string"}, "reason": {"type": "string"}, "difficulty_1_5": {"type": "integer"}},
                        "required": ["word"]}},
                    "tags": {"type": "array", "items": {"type": "string"}}
                }, "required": ["candidates"]}}
             }
        ]
    # ---------- 日志脱敏 ----------

    def _redact_for_log(self, payload):
        if not self.hide_ans.get():
            return payload

        def walk(x):
            if isinstance(x, dict):
                out = {}
                for k, v in x.items():
                    if k in ("meaning_zh", "zh"):
                        out[k] = "[hidden]"
                    elif k == "example" and isinstance(v, dict):
                        vv = dict(v)
                        if "zh" in vv:
                            vv["zh"] = "[hidden]"
                        out[k] = walk(vv)
                    else:
                        out[k] = walk(v)
                return out
            if isinstance(x, list):
                return [walk(i) for i in x]
            return x
        return walk(payload)

    # ---------- 工具执行 ----------
    def _apply_tool(self, name: str, args: dict):
        # 读取词库
        store_path = _must_pick_store_or_raise(self.var_store.get())
        store = load_json(store_path)
        self.logs.log(
            "[tool-call] " + json.dumps({"name": name, "args": args}, ensure_ascii=False))

        def norm(e: dict):
            e.setdefault("word", "")
            e.setdefault("meaning_zh", "")
            e.setdefault("pos", "")
            e.setdefault("synonyms_en", [])
            e.setdefault("phrases", [])
            e.setdefault("example", {"en": "", "zh": ""})
            e.setdefault("confusions", [])
            e.setdefault("model_notes", "")
            e.setdefault("user_notes", [])
            e.setdefault("tags", [])
            e.setdefault("sources", [])
            e.setdefault("srs", dict(DEFAULT_SRS))
            e.setdefault("sched", {"times_scheduled": 0})
            return e

        def find_idx(w: str):
            wl = (w or "").strip().lower()
            for i, e in enumerate(store.get("entries", [])):
                if (e.get("word", "").lower() == wl):
                    return i
            base = _normalize_word(wl)
            if base != wl:
                for i, e in enumerate(store.get("entries", [])):
                    if (e.get("word", "").lower() == base):
                        return i
            return -1

        # 具体工具
        if name == "get_word":
            w = args.get("word", "")
            idx = find_idx(w)
            res = {"found": idx != -1,
                   "entry": store["entries"][idx] if idx != -1 else None}
            self.logs.log(
                "[tool-ret ] " + json.dumps(self._redact_for_log(res), ensure_ascii=False))
            return res

        if name == "upsert_word":
            from enrich.enrich import enrich_one
            w = args.get("word", "") or ""
            idx = find_idx(w)
            if idx == -1:
                enriched = enrich_one(setup_client(), _normalize_word(
                    w), args.get("meaning_hint", "") or "")
                e = norm(enriched)
                src = args.get("source", "chat")
                note = args.get("note", "") or ""
                if src:
                    e["sources"].append(src)
                if note:
                    e["user_notes"].append(note)
                for t in (args.get("tags") or []):
                    if t not in e["tags"]:
                        e["tags"].append(t)
                store.setdefault("entries", []).append(e)
            else:
                e = norm(store["entries"][idx])
                if not e.get("meaning_zh"):
                    enriched = enrich_one(setup_client(), e.get(
                        "word", ""), e.get("meaning_zh", ""))
                    for k in ["meaning_zh", "pos", "synonyms_en", "phrases", "example", "confusions", "model_notes"]:
                        if k in enriched and enriched[k]:
                            e[k] = enriched[k]
                store["entries"][idx] = e
            store.setdefault("meta", {})["count"] = len(store["entries"])
            dump_json_atomic(store_path, store)
            res = {"ok": True}
            self.logs.log(
                "[tool-ret ] " + json.dumps(self._redact_for_log(res), ensure_ascii=False))
            return res

        if name == "update_user_note":
            w = args.get("word", "")
            ap = args.get("append_note", "") or ""
            idx = find_idx(w)
            if idx == -1:
                res = {"ok": False, "error": "not found"}
            else:
                e = norm(store["entries"][idx])
                e["user_notes"].append(ap)
                store["entries"][idx] = e
                dump_json_atomic(store_path, store)
                res = {"ok": True}
            self.logs.log(
                "[tool-ret ] " + json.dumps(self._redact_for_log(res), ensure_ascii=False))
            return res

        if name == "log_confusion":
            w = args.get("word", "")
            cw = args.get("confused_with", "")
            tip = args.get("tip_zh", "") or ""
            idx = find_idx(w)
            if idx == -1:
                res = {"ok": False, "error": "not found"}
            else:
                e = norm(store["entries"][idx])
                e["confusions"].append({"with": cw, "tip_zh": tip})
                store["entries"][idx] = e
                dump_json_atomic(store_path, store)
                res = {"ok": True}
            self.logs.log(
                "[tool-ret ] " + json.dumps(self._redact_for_log(res), ensure_ascii=False))
            return res

        if name == "add_phrase":
            w = args.get("word", "")
            ph = args.get("phrase", "")
            mzh = args.get("meaning_zh", "") or ""
            idx = find_idx(w)
            if idx == -1:
                res = {"ok": False, "error": "not found"}
            else:
                e = norm(store["entries"][idx])
                if not any((p.get("phrase", "").lower() == (ph or "").lower()) for p in e["phrases"]):
                    e["phrases"].append({"phrase": ph, "meaning_zh": mzh})
                store["entries"][idx] = e
                dump_json_atomic(store_path, store)
                res = {"ok": True}
            self.logs.log(
                "[tool-ret ] " + json.dumps(self._redact_for_log(res), ensure_ascii=False))
            return res

        if name == "search_words":
            q = (args.get("query", "") or "").lower()
            tg = args.get("tag", "") or ""
            limit = int(args.get("limit", 20))
            out = []
            for e in store.get("entries", []):
                if q and q not in (e.get("word", "").lower()):
                    continue
                if tg and tg not in (e.get("tags") or []):
                    continue
                out.append({"word": e.get("word", ""), "meaning_zh": e.get(
                    "meaning_zh", ""), "tags": e.get("tags", [])})
                if len(out) >= limit:
                    break
            res = {"items": out}
            self.logs.log(
                "[tool-ret ] "+json.dumps(self._redact_for_log(res), ensure_ascii=False))
            return res

        if name == "set_attr":
            w = args.get("word", "")
            k = args.get("key", "")
            v = args.get("value", None)
            idx = find_idx(w)
            if idx == -1:
                res = {"ok": False, "error": "not found"}
            else:
                e = norm(store["entries"][idx])
                e[k] = v
                store["entries"][idx] = e
                dump_json_atomic(store_path, store)
                res = {"ok": True}
            self.logs.log(
                "[tool-ret ] "+json.dumps(self._redact_for_log(res), ensure_ascii=False))
            return res

        if name == "sample_study_items":
            k = int(args.get("k", 20))
            min_gap = float(args.get("min_days_gap", 1.0))
            items = [norm(e) for e in store.get("entries", [])]
            pool = anti_repeat_filter(items, min_days_gap=min_gap)
            chosen = weighted_sample_without_replacement(pool, k)
            words = []
            for e in chosen:
                mark_scheduled(e)
                words.append({
                    "word": e.get("word", ""),
                    "meaning_zh": e.get("meaning_zh", ""),
                    "pos": e.get("pos", ""),
                    "phrases": e.get("phrases", []),
                    "example": e.get("example", {"en": "", "zh": ""})
                })
            dump_json_atomic(store_path, store)  # 写回 sched
            res = {"ok": True, "k": len(words), "items": words}
            self.logs.log(
                "[tool-ret ] "+json.dumps(self._redact_for_log(res), ensure_ascii=False))
            return res

        if name == "update_srs":
            w = args.get("word", "")
            score = float(args.get("score_0_1", 0.0))
            idx = find_idx(w)
            if idx == -1:
                res = {"ok": False, "error": "not found"}
            else:
                e = norm(store["entries"][idx])
                e["srs"] = update(ensure(e.get("srs") or {}), score)
                store["entries"][idx] = e
                dump_json_atomic(store_path, store)
                res = {"ok": True, "next_due": e["srs"]["next_due"]}
            self.logs.log(
                "[tool-ret ] "+json.dumps(self._redact_for_log(res), ensure_ascii=False))
            return res

        if name == "grade_usage":
            w = args.get("word", "")
            sent = (args.get("sentence", "") or "").strip()
            idx = find_idx(w)
            if idx == -1:
                res = {"ok": False, "error": "not found"}
                self.logs.log(
                    "[tool-ret ] "+json.dumps(self._redact_for_log(res), ensure_ascii=False))
                return res
            from study.grader import grade_with_llm
            e = store["entries"][idx]
            card = {
                "word": e.get("word", ""), "meaning_zh": e.get("meaning_zh", ""),
                "pos": e.get("pos", ""), "synonyms_en": e.get("synonyms_en", []),
                "phrases": e.get("phrases", []), "example": e.get("example", {"en": "", "zh": ""}),
                "confusions": e.get("confusions", []), "model_notes": e.get("model_notes", "")
            }
            result = grade_with_llm(
                "example_usage", card, sent, retry=1, force_tool=True)
            score = float(result.get("score_0_1", 0.0))
            mistakes = result.get("mistakes", [])
            correction = result.get("correction", "")
            expl = result.get("explanation", "")
            short_fb = f"Score: {score:.2f}. " \
                       f"{('Issues: ' + '; '.join(mistakes) + '. ') if mistakes else ''}" \
                       f"{('Correction: ' + correction + '. ') if correction else ''}" \
                       f"{('Note: ' + expl) if expl else ''}"
            res = {"ok": True, "score_0_1": score,
                   "short_feedback_en": short_fb, "raw": result}
            self.logs.log(
                "[tool-ret ] "+json.dumps(self._redact_for_log(res), ensure_ascii=False))
            return res

        if name == "propose_words_from_text":
            cands = args.get("candidates", [])
            res = {"ok": True, "count": len(cands)}
            self.logs.log(
                "[tool-ret ] "+json.dumps(self._redact_for_log(res), ensure_ascii=False))
            return res

        res = {"ok": False, "error": f"unknown tool {name}"}
        self.logs.log(
            "[tool-ret ] "+json.dumps(self._redact_for_log(res), ensure_ascii=False))
        return res

    # ---------- 发送 ----------
    def chat_send(self):
        content = (self._input_text() or "").strip()
        if not content:
            self.txt_input.focus_set()
            return
        try:
            _ = _must_pick_store_or_raise(self.var_store.get())
        except Exception as ex:
            messagebox.showerror("错误", str(ex))
            return

        self._input_clear()
        Bubble(self.scroll, content, me=True).pack(anchor="e", fill="x")
        self.scroll.update_idletasks()
        self.scroll._parent_canvas.yview_moveto(1.0)

        def do_round(messages):
            resp = self.client.chat.completions.create(
                model=MODEL_NAME, temperature=0.2,
                messages=messages, tools=self.tools, tool_choice="auto"
            )
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)
            return msg, tool_calls

        # 循环处理工具
        messages = _trim_messages(
            self.chat_history + [{"role": "user", "content": content}], max_len=60)
        for _ in range(10):
            msg, tool_calls = do_round(messages)
            if not tool_calls:
                text = msg.content or "(no reply)"
                messages.append({"role": "assistant", "content": text})
                self.chat_history = messages
                if self.current_chat_path:
                    self.current_chat_path.write_text(json.dumps(self.chat_history, ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
                Bubble(self.scroll, text, me=False).pack(anchor="w", fill="x")
                self.scroll.update_idletasks()
                self.scroll._parent_canvas.yview_moveto(1.0)
                return

            assistant_msg = {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [{
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}
                } for tc in tool_calls]
            }
            messages.append(assistant_msg)

            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                result = self._apply_tool(tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.function.name,
                    "content": json.dumps(result, ensure_ascii=False)
                })

        # 兜底
        self.chat_history = messages
        Bubble(self.scroll, "（工具调用轮次过多，已停止。请重试或简化请求。）", me=False)\
            .pack(anchor="w", fill="x")

    # ---------- 选择文件 ----------
    def pick_store(self):
        p = filedialog.askopenfilename(
            title="选择 JSON", filetypes=[("JSON", ".json")])
        if p:
            self.var_store.set(p)


def run():
    app = ModernApp()
    app.mainloop()
