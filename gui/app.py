# -*- coding: utf-8 -*-
import json
import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from datetime import datetime

from config import DEFAULT_BATCH_SIZE, DEFAULT_CHECKPOINT_EVERY, MODEL_NAME
from api_client import setup_client

from extractor.pdf_extract import extract_pdf, DEFAULT_LAYOUT
from enrich.enrich import enrich_file
from study.srs import score_priority, update, DEFAULT_SRS, ensure
from study.grader import grade_with_llm   # 仍保留 Study 页使用
from study.sampler import weighted_sample_without_replacement, anti_repeat_filter, mark_scheduled
from agent.propose import propose_from_text
from utils.jsonio import dump_json_atomic, load_json
from .widgets import LineEntry, LogBox, ProgBar, Worker

CHAT_DIR = Path("data/chats")
CHAT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------- 小工具 ----------------




def _normalize_query_word(w: str) -> str:
    if not w: return w
    wl = w.strip().lower()
    if len(wl) > 3 and wl.endswith("ies"): return wl[:-3] + "y"
    if len(wl) > 2 and wl.endswith("es"): return wl[:-2]
    if len(wl) > 2 and wl.endswith("s"): return wl[:-1]
    return wl


def _must_pick_store_or_raise(entry_widget: LineEntry) -> Path:
    p = (entry_widget.get() or "").strip()
    if not p: raise RuntimeError("请先选择词库 JSON（enriched.json）后再聊天或写库。")
    path = Path(p)
    if path.is_dir(): raise RuntimeError(f"选择的是目录：{path}。请选中一个 .json 文件。")
    if path.suffix.lower() != ".json": raise RuntimeError(
        f"词库文件需要是 .json，当前：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _trim_messages(messages, max_len=60):
    # 只保留最后 max_len 条，避免上下文过长
    return messages[-max_len:]

# ---------------- 主应用 ----------------


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Vocab Suite")
        self.geometry("1180x780")
        nb = ttk.Notebook(self); nb.pack(fill="both", expand=True)

        # 顺序：Chat 优先
        self.tab_chat = ttk.Frame(nb); nb.add(
            self.tab_chat, text="0) Chat (主模式)")
        self.tab_extract = ttk.Frame(nb); nb.add(
            self.tab_extract, text="1) Extract PDF")
        self.tab_enrich = ttk.Frame(nb); nb.add(
            self.tab_enrich,  text="2) Enrich")
        self.tab_study = ttk.Frame(nb); nb.add(
            self.tab_study,   text="3) Study")

        self._build_chat()
        self._build_extract()
        self._build_enrich()
        self._build_study()

    # ---------------- Chat ----------------
    def _redact_for_log(self, payload):
        """
        深度脱敏：隐藏中文释义字段，避免在日志里剧透。
        - 任何名为 'meaning_zh' 的键置为 '[hidden]'
        - 任何名为 'zh' 的键置为 '[hidden]'
        - 'example' 下的 'zh' 也会被隐藏
        """
        import copy
        if not self.var_hide_answers.get():
            return payload

        def _walk(x):
            if isinstance(x, dict):
                out = {}
                for k, v in x.items():
                    if k in ("meaning_zh", "zh"):
                        out[k] = "[hidden]"
                    elif k == "example" and isinstance(v, dict):
                        vv = v.copy()
                        if "zh" in vv:
                            vv["zh"] = "[hidden]"
                        out[k] = _walk(vv)
                    else:
                        out[k] = _walk(v)
                return out
            if isinstance(x, list):
                return [_walk(i) for i in x]
            return x
        return _walk(copy.deepcopy(payload))

    def _build_chat(self):
        f = self.tab_chat

        root = ttk.Frame(f)
        root.pack(fill="both", expand=True)
        # 左侧：会话列表 & 操作（保持你现有逻辑）
        left = ttk.Frame(root, width=260)
        left.pack(side="left", fill="y")
        ops = ttk.Frame(left)
        ops.pack(fill="x", padx=8, pady=8)
        ttk.Button(ops, text="新建会话", command=self._new_chat).pack(
            side="left", padx=4)
        ttk.Button(ops, text="保存会话", command=self._save_chat).pack(
            side="left", padx=4)
        ttk.Button(ops, text="刷新列表", command=self._refresh_chat_list).pack(
            side="left", padx=4)
        self.lst_chats = tk.Listbox(left, height=28)
        self.lst_chats.pack(fill="both", expand=True, padx=8, pady=4)
        self.lst_chats.bind("<<ListboxSelect>>", self._on_select_chat)

        # 右侧：聊天区域
        right = ttk.Frame(root)
        right.pack(side="left", fill="both", expand=True)

        # 词库路径
        self.e_store = LineEntry(right, "词库 JSON (enriched)")
        self.e_store.pack(fill="x", padx=10, pady=6)
        ttk.Button(right, text="选择词库", command=lambda: self._pick_json(
            self.e_store)).pack(padx=10, pady=2, anchor="w")

        # 会话显示（历史消息）
        frm = ttk.Frame(right)
        frm.pack(fill="both", expand=True, padx=10, pady=6)
        self.txt_chat = tk.Text(frm, height=18, wrap="word")
        self.txt_chat.pack(side="left", fill="both", expand=True)
        y = ttk.Scrollbar(frm, orient="vertical", command=self.txt_chat.yview)
        y.pack(side="right", fill="y")
        self.txt_chat.config(yscrollcommand=y.set)

        # 工具日志的选项（隐藏答案、折叠日志）
        opts = ttk.Frame(right)
        opts.pack(fill="x", padx=10, pady=2)
        self.var_hide_answers = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="隐藏参考答案(meaning_zh/zh)",
                        variable=self.var_hide_answers).pack(side="left")
        self.var_fold_log = tk.BooleanVar(value=False)

        def _toggle_log():
            if self.var_fold_log.get():
                self.log_tools.pack_forget()
            else:
                self.log_tools.pack(fill="x", padx=10, pady=4)
        ttk.Checkbutton(opts, text="折叠工具日志", variable=self.var_fold_log,
                        command=_toggle_log).pack(side="left", padx=12)

        # 工具调用日志
        self.log_tools = LogBox(right)
        self.log_tools.pack(fill="x", padx=10, pady=4)

        # 输入区（多行 Text + 动态高度 + 滚动条）
        entry_row = ttk.Frame(right)
        entry_row.pack(fill="x", padx=10, pady=6)
        input_wrap = ttk.Frame(entry_row)
        input_wrap.pack(side="left", fill="x", expand=True)

        self.txt_input = tk.Text(input_wrap, height=1, wrap="word", undo=True)
        self.txt_input.pack(side="left", fill="x", expand=True)
        # 只在行数>5时显示
        self.in_scroll = ttk.Scrollbar(
            input_wrap, orient="vertical", command=self.txt_input.yview)
        self.txt_input.configure(yscrollcommand=lambda *args: None)  # 我们手动控制显隐

        # 绑定键位：Enter 发送、Shift+Enter 换行、键释放后自适应高度
        self.txt_input.bind("<Return>", self._on_input_return)
        self.txt_input.bind("<Shift-Return>", self._on_input_shift_return)
        self.txt_input.bind("<KeyRelease>", self._on_input_keyrelease)

        ttk.Button(entry_row, text="发送", command=self._chat_send).pack(
            side="left", padx=6)

        # LLM 与工具
        self.client = setup_client()
        self.tools = self._tool_specs()

        # 会话历史（持久化 & 新建/保存）——保持你之前的实现
        self.chat_history = self._initial_system_messages()
        self.current_chat_path = None
        self._refresh_chat_list()
        self._new_chat()


    def _initial_system_messages(self):
        return [
            {"role":"system","content":(
                "You are an English vocabulary tutor and study coach.\n"
                "STYLE:\n"
                "- Default to ENGLISH unless the user writes mostly in Chinese.\n"
                "- Be concise, friendly, and interactive like a real tutor.\n"
                "- DO NOT reveal Chinese translations or answers unless the user says "
                "\"show answer\" or after they have attempted/declined.\n"
                "- Prefer short English hints (definition clues, synonyms, collocations) before revealing any answer.\n"
                "\n"
                "TOOLS POLICY:\n"
                "- Dictionary/KB management via tools; do not print tool JSON in normal replies.\n"
                "- When the user asks to \"study\" or \"test\" N words: call sample_study_items(k=N). "
                "For each word: 1) Ask if they remember it (y/n); 2) Ask them to write an English sentence using the word; "
                "3) Call grade_usage(word, sentence) to score and produce short feedback; 4) Call update_srs(word, score_0_1).\n"
                "- When adding/amending notes, confusions, phrases, etc., call the corresponding tool.\n"
                "- If the user asks for the translation explicitly (e.g., \"show answer\"), you may reveal it.\n"
            )}
        ]


    def _pick_json(self, entry: LineEntry):
        p = filedialog.askopenfilename(
            title="选择 JSON", filetypes=[("JSON", ".json")])
        if p: entry.set(p)

    # ---- 会话存取 ----
    def _refresh_chat_list(self):
        self.lst_chats.delete(0, "end")
        files = sorted(CHAT_DIR.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files:
            self.lst_chats.insert("end", p.name)

    def _new_chat(self):
        # 清空窗口与历史，建立新的未命名会话
        self.txt_chat.delete("1.0", "end")
        self.txt_chat.insert("end", "（新会话已创建）\n")
        self.chat_history = self._initial_system_messages()
        self.current_chat_path = None

    def _save_chat(self):
        # 第一句用户内容做标题
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
        self._refresh_chat_list()
        messagebox.showinfo("已保存", f"已保存到 {path}")

    def _on_select_chat(self, evt):
        try:
            sel = self.lst_chats.curselection()
            if not sel: return
            name = self.lst_chats.get(sel[0])
            path = CHAT_DIR / name
            data = json.loads(path.read_text(encoding="utf-8"))
            self.chat_history = data
            self.current_chat_path = path
            # 在窗口里渲染最近若干条消息
            self.txt_chat.delete("1.0", "end")
            for m in data[-40:]:
                if m["role"] == "user":
                    self.txt_chat.insert("end", f"[你] {m['content']}\n")
                elif m["role"] == "assistant":
                    self.txt_chat.insert(
                        "end", f"[助手] {m.get('content','')}\n")
            self.txt_chat.see("end")
        except Exception as ex:
            messagebox.showerror("错误", f"加载会话失败：{ex}")

    # ==== 输入框（Chat）辅助：取文本、清空、动态高度、键盘绑定 ====
    def _input_get(self) -> str:
        try:
            return self.txt_input.get("1.0", "end-1c")
        except Exception:
            return ""

    def _input_clear(self):
        try:
            self.txt_input.delete("1.0", "end")
            # 重置高度到 1 行
            self.txt_input.configure(height=1)
            # 隐藏滚动条
            if hasattr(self, "in_scroll"):
                self.in_scroll.pack_forget()
        except Exception:
            pass

    def _adjust_input_height(self, *_):
        """
        根据当前内容行数自动调整输入框高度（1~5 行），超过 5 行显示滚动条。
        """
        try:
            # 当前内容的行数
            lines = int(self.txt_input.index("end-1c").split(".")[0])
            new_h = max(1, min(5, lines))
            if int(self.txt_input.cget("height")) != new_h:
                self.txt_input.configure(height=new_h)

            # 超过 5 行显示滚动条，否则隐藏
            if lines > 5:
                if not getattr(self, "_in_scroll_shown", False):
                    self.in_scroll.pack(side="right", fill="y")
                    self._in_scroll_shown = True
            else:
                if getattr(self, "_in_scroll_shown", False):
                    self.in_scroll.pack_forget()
                    self._in_scroll_shown = False
        except Exception:
            pass

    def _on_input_return(self, event):
        """
        回车发送（阻止默认换行）
        """
        self._chat_send()
        return "break"

    def _on_input_shift_return(self, event):
        """
        Shift+Enter = 插入换行
        """
        try:
            self.txt_input.insert("insert", "\n")
            self._adjust_input_height()
        except Exception:
            pass
        return "break"

    def _on_input_keyrelease(self, event):
        """
        任意键释放后，尝试调整高度（避免按键过程中多次重绘）
        """
        self._adjust_input_height()

    # ---- 工具定义 ----
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

    # ---- 工具执行逻辑 ----
    def _apply_tool(self, name: str, args: dict):
        store_path = _must_pick_store_or_raise(self.e_store)
        store = load_json(store_path)
        self.log_tools.log(
            f"[tool-call] {json.dumps({'name':name,'args':args}, ensure_ascii=False)}")

        def norm_entry(e: dict):
            e.setdefault("word", ""); e.setdefault("meaning_zh", "")
            e.setdefault("pos", ""); e.setdefault("synonyms_en", [])
            e.setdefault("phrases", []); e.setdefault(
                "example", {"en": "", "zh": ""})
            e.setdefault("confusions", []); e.setdefault("model_notes", "")
            e.setdefault("user_notes", []); e.setdefault(
                "tags", []); e.setdefault("sources", [])
            e.setdefault("srs", dict(DEFAULT_SRS)); e.setdefault(
                "sched", {"times_scheduled": 0})
            return e

        def find_idx(w: str):
            wl = (w or "").strip().lower()
            entries = store.get("entries", [])
            for i, e in enumerate(entries):
                if (e.get("word") or "").lower() == wl: return i
            base = _normalize_query_word(wl)
            if base != wl:
                for i, e in enumerate(entries):
                    if (e.get("word") or "").lower() == base: return i
            return -1

        # ---- 现有工具（略微省略重复代码，逻辑同前版本）----
        if name == "get_word":
            w= args.get("word", ""); idx = find_idx(w)
            res = {"found": idx != -1, "entry": store["entries"][idx] if idx != -1 else None}
            self.log_tools.log(
                f"[tool-ret ] {json.dumps(res, ensure_ascii=False)}")
            return res

        if name == "upsert_word":
            from enrich.enrich import enrich_one as _enrich_one
            w= args.get("word", "") or ""
            wnorm = _normalize_query_word(w)
            hint= args.get("meaning_hint", "") or ""
            src = args.get("source", "chat"); note = args.get("note", "") or ""
            tags= args.get("tags", []) or []
            idx = find_idx(wnorm)
            if idx == -1:
                enriched = _enrich_one(wnorm, hint); e = norm_entry(enriched)
                if src: e["sources"].append(src)
                if note: e["user_notes"].append(note)
                for t in tags:
                    if t not in e["tags"]: e["tags"].append(t)
                store.setdefault("entries", []).append(e)
            else:
                e = norm_entry(store["entries"][idx])
                if not e.get("meaning_zh"):
                    enriched = _enrich_one(e.get("word", ""), e.get("meaning_zh", ""))
                    for k in ["meaning_zh", "pos", "synonyms_en", "phrases", "example", "confusions", "model_notes"]:
                        if k in enriched and enriched[k]: e[k]= enriched[k]
                if src and src not in e["sources"]: e["sources"].append(src)
                if note: e["user_notes"].append(note)
                store["entries"][idx] = e
            store.setdefault("meta", {})["count"] = len(store["entries"])
            dump_json_atomic(store_path, store)
            res = {"ok": True}
            self.log_tools.log(
                f"[tool-ret ] {json.dumps(self._redact_for_log(res), ensure_ascii=False)}")

            return res
        if name == "grade_usage":
            w = args.get("word",""); sent = (args.get("sentence","") or "").strip()
            idx = find_idx(w)
            if idx == -1:
                res = {"ok": False, "error": "not found"}
                self.log_tools.log(f"[tool-ret ] {json.dumps(self._redact_for_log(res), ensure_ascii=False)}")
                return res
            # 组装 card（传给 LLM 评分器）
            e = store["entries"][idx]
            card = {
                "word": e.get("word",""),
                "meaning_zh": e.get("meaning_zh",""),
                "pos": e.get("pos",""),
                "synonyms_en": e.get("synonyms_en",[]),
                "phrases": e.get("phrases",[]),
                "example": e.get("example",{"en":"","zh":""}),
                "confusions": e.get("confusions",[]),
                "model_notes": e.get("model_notes","")
            }
            from study.grader import grade_with_llm
            result = grade_with_llm("example_usage", card, sent, retry=1, force_tool=True)
            # 附上短评（英文，方便模型复述）
            score = float(result.get("score_0_1", 0.0))
            mistakes = result.get("mistakes",[])
            correction = result.get("correction","")
            expl = result.get("explanation","")
            short_fb = f"Score: {score:.2f}. " \
                    f"{('Issues: ' + '; '.join(mistakes) + '. ') if mistakes else ''}" \
                    f"{('Correction: ' + correction + '. ') if correction else ''}" \
                    f"{('Note: ' + expl) if expl else ''}"
            res = {"ok": True, "score_0_1": score, "short_feedback_en": short_fb, "raw": result}
            self.log_tools.log(f"[tool-ret ] {json.dumps(self._redact_for_log(res), ensure_ascii=False)}")
            return res

        if name == "update_user_note":
            w = args.get("word", ""); ap = args.get("append_note", "") or ""
            idx = find_idx(w)
            if idx == -1:
                res= {"ok": False, "error": "not found"}
            else:
                e = norm_entry(store["entries"][idx]); e["user_notes"].append(ap)
                store["entries"][idx]= e; dump_json_atomic(store_path, store); res = {"ok": True}
            self.log_tools.log(f"[tool-ret ] {json.dumps(res, ensure_ascii=False)}"); return res

        if name == "log_confusion":
            w= args.get("word", ""); cw = args.get("confused_with", ""); tip = args.get("tip_zh", "") or ""
            idx = find_idx(w)
            if idx == -1:
                res= {"ok": False, "error": "not found"}
            else:
                e= norm_entry(store["entries"][idx]); e["confusions"].append({"with": cw, "tip_zh": tip})
                store["entries"][idx]= e; dump_json_atomic(store_path, store); res = {"ok": True}
            self.log_tools.log(f"[tool-ret ] {json.dumps(res, ensure_ascii=False)}"); return res

        if name == "add_phrase":
            w= args.get("word", ""); ph = args.get("phrase", ""); mzh = args.get("meaning_zh", "") or ""
            idx = find_idx(w)
            if idx == -1:
                res= {"ok": False, "error": "not found"}
            else:
                e = norm_entry(store["entries"][idx])
                if not any((p.get("phrase", "").lower() == (ph or "").lower()) for p in e["phrases"]):
                    e["phrases"].append({"phrase": ph, "meaning_zh": mzh})
                store["entries"][idx]= e; dump_json_atomic(store_path, store); res = {"ok": True}
            self.log_tools.log(f"[tool-ret ] {json.dumps(res, ensure_ascii=False)}"); return res

        if name == "search_words":
            q= (args.get("query", "") or "").lower(); tg = args.get("tag", "") or ""; limit = int(args.get("limit", 20))
            out = []
            for e in store.get("entries", []):
                if q and q not in (e.get("word", "").lower()): continue
                if tg and tg not in (e.get("tags") or []): continue
                out.append({"word": e.get("word", ""), "meaning_zh": e.get(
                    "meaning_zh", ""), "tags": e.get("tags", [])})
                if len(out) >= limit: break
            res = {"items": out}; self.log_tools.log(f"[tool-ret ] {json.dumps(res, ensure_ascii=False)}"); return res

        if name == "set_attr":
            w= args.get("word", ""); k = args.get("key", ""); v = args.get("value", None)
            idx = find_idx(w)
            if idx == -1:
                res= {"ok": False, "error": "not found"}
            else:
                e= norm_entry(store["entries"][idx]); e[k] = v
                store["entries"][idx]= e; dump_json_atomic(store_path, store); res = {"ok": True}
            self.log_tools.log(f"[tool-ret ] {json.dumps(res, ensure_ascii=False)}"); return res

        # ---- 新增：加权抽样测验 ----
        if name == "sample_study_items":
            k = int(args.get("k", 20))
            min_gap = float(args.get("min_days_gap", 1.0))
            items = [norm_entry(e) for e in store.get("entries", [])]
            if not items:
                res = {"ok": False, "error": "empty store"}
                self.log_tools.log(f"[tool-ret ] {json.dumps(res, ensure_ascii=False)}"); return res
            pool = anti_repeat_filter(items, min_days_gap=min_gap)
            chosen = weighted_sample_without_replacement(pool, k)
            # 标记调度并写回
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
            # 写回 entries（通过 word 匹配）
            word2idx = {(items[i].get("word") or "").lower(): i for i in range(len(items))}
            for w in chosen:
                key = (w.get("word") or "").lower()
                idx = word2idx.get(key, -1)
                if idx != -1:
                    store["entries"][idx] = w
            dump_json_atomic(store_path, store)
            res = {"ok": True, "k": len(words), "items": words}
            self.log_tools.log(f"[tool-ret ] {json.dumps(res, ensure_ascii=False)}"); return res

        # ---- 新增：更新 SRS ----
        if name == "update_srs":
            w= args.get("word", ""); score = float(args.get("score_0_1", 0.0))
            idx = find_idx(w)
            if idx == -1:
                res= {"ok": False, "error": "not found"}
            else:
                e = norm_entry(store["entries"][idx])
                e["srs"] = update(ensure(e.get("srs") or {}), score)
                store["entries"][idx]= e; dump_json_atomic(store_path, store); res = {"ok": True, "next_due": e["srs"]["next_due"]}
            self.log_tools.log(f"[tool-ret ] {json.dumps(res, ensure_ascii=False)}"); return res

        if name == "propose_words_from_text":
            cands= args.get("candidates", [])
            res = {"ok": True, "count": len(cands)}; self.log_tools.log(f"[tool-ret ] {json.dumps(res, ensure_ascii=False)}"); return res

        res = {"ok": False, "error": f"unknown tool {name}"}; self.log_tools.log(f"[tool-ret ] {json.dumps(res, ensure_ascii=False)}"); return res

    # ---- Chat 循环（已修复 tool_calls 严格规范 & 会话持久化 & 上下文裁剪）----
    def _chat_send(self):
        content = (self._input_get() or "").strip()
        if not content:
            # 保持焦点，体验更顺滑
            try:
                self.txt_input.focus_set()
            except Exception:
                pass
            return

        # 确保词库路径已选择
        try:
            _ = _must_pick_store_or_raise(self.e_store)
        except Exception as ex:
            messagebox.showerror("错误", str(ex))
            try:
                self.txt_input.focus_set()
            except Exception:
                pass
            return

        # 清空输入 & 重置高度 & 聚焦
        self._input_clear()
        try:
            self.txt_input.focus_set()
        except Exception:
            pass

        # 将用户消息写入聊天窗口
        self.txt_chat.insert("end", f"\n[你] {content}\n")
        self.txt_chat.see("end")

        def job(progress, log):
            # 剪裁上下文长度（防上下文过长）

            def _trim_messages(messages, max_len=60):
                return messages[-max_len:]

            messages = _trim_messages(
                self.chat_history + [{"role": "user", "content": content}], max_len=60)

            # 工具循环（与你当前版本保持一致，含 tool_calls 规范修复）
            for _round in range(10):
                resp = self.client.chat.completions.create(
                    model=MODEL_NAME, temperature=0.2,
                    messages=messages, tools=self.tools, tool_choice="auto"
                )
                msg = resp.choices[0].message
                tool_calls = getattr(msg, "tool_calls", None)

                if not tool_calls:
                    final_text = msg.content or "(无回复)"
                    messages.append(
                        {"role": "assistant", "content": final_text})
                    self.chat_history = messages
                    # 自动保存快照（若开启了会话文件）
                    if self.current_chat_path:
                        self.current_chat_path.write_text(
                            json.dumps(self.chat_history, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                    return final_text

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

            self.chat_history = messages
            return "（工具调用轮次过多，已停止。请重试或简化请求。）"

        def done(text):
            self.txt_chat.insert("end", f"[助手] {text}\n")
            self.txt_chat.see("end")
            # 保持输入焦点，Enter 能继续发
            try:
                self.txt_input.focus_set()
            except Exception:
                pass

        Worker(job, on_done=done, on_log=self.log_tools.log).start()

    # ---------------- Extract ----------------（保持不变，略）
    def _build_extract(self):
        f = self.tab_extract
        self.e_pdf = LineEntry(f, "PDF 文件"); self.e_pdf.pack(fill="x", padx=10, pady=6)
        self.e_out = LineEntry(f, "输出 JSON"); self.e_out.pack(fill="x", padx=10, pady=6)
        self.e_workers = LineEntry(f, "并行进程数"); self.e_workers.set("4"); self.e_workers.pack(fill="x", padx=10, pady=6)

        conf = ttk.LabelFrame(f, text="布局参数（可调）"); conf.pack(fill="x", padx=10, pady=6)
        self.vars = {}
        for k in ["word_box_w","meaning_box_w","row_h","left_x0","left_y0","right_x0","right_y0","rows_per_page","number_w"]:
            fr = ttk.Frame(conf); fr.pack(fill="x", padx=6, pady=2)
            ttk.Label(fr, text=k, width=16).pack(side="left")
            v = tk.StringVar(value=str(DEFAULT_LAYOUT[k])); self.vars[k]=v
            ttk.Entry(fr, textvariable=v, width=12).pack(side="left")
        btns = ttk.Frame(f); btns.pack(fill="x", padx=10, pady=6)
        ttk.Button(btns, text="选择 PDF", command=self._pick_pdf).pack(side="left")
        ttk.Button(btns, text="选择输出", command=self._pick_json_out).pack(side="left")
        ttk.Button(btns, text="运行提取", command=self._run_extract).pack(side="left")
        self.pb1 = ProgBar(f); self.pb1.pack(padx=10, pady=6)
        self.log1 = LogBox(f); self.log1.pack(fill="both", expand=True, padx=10, pady=6)

    def _pick_pdf(self):
        p = filedialog.askopenfilename(title="选择 PDF", filetypes=[("PDF",".pdf")])
        if p: self.e_pdf.set(p)
    def _pick_json_out(self):
        p = filedialog.asksaveasfilename(title="保存为 JSON", defaultextension=".json", filetypes=[("JSON",".json")])
        if p: self.e_out.set(p)

    def _run_extract(self):
        try:
            pdf = Path(self.e_pdf.get()); out = Path(self.e_out.get())
            layout = {k:int(self.vars[k].get()) for k in self.vars}
            workers = max(1, int(self.e_workers.get()))
        except Exception:
            messagebox.showerror("错误","参数不合法"); return
        def job(progress, log):
            log(f"Extracting from {pdf} with {workers} processes...")
            rows = extract_pdf(pdf, layout, workers=workers, progress_cb=progress)
            payload = {"meta":{"source":str(pdf.resolve()),"count":len(rows)},
                       "entries": rows}
            dump_json_atomic(out, payload)
            log(f"✓ Saved {len(rows)} entries -> {out}")
            return out
        Worker(job, on_log=self.log1.log, on_progress=lambda d,t:self.pb1.update_ratio(d,t)).start()

    # ---------------- Enrich ----------------（保持不变，略）
    def _build_enrich(self):
        f = self.tab_enrich
        self.e_in  = LineEntry(f, "输入 JSON"); self.e_in.pack(fill="x", padx=10, pady=6)
        self.e_out2= LineEntry(f, "输出 JSON(enriched)"); self.e_out2.pack(fill="x", padx=10, pady=6)
        fr = ttk.Frame(f); fr.pack(fill="x", padx=10, pady=6)
        ttk.Label(fr, text="batch_size").pack(side="left"); self.v_batch=tk.StringVar(value=str(DEFAULT_BATCH_SIZE))
        ttk.Entry(fr, textvariable=self.v_batch, width=6).pack(side="left", padx=6)
        ttk.Label(fr, text="checkpoint_every").pack(side="left"); self.v_ck=tk.StringVar(value=str(DEFAULT_CHECKPOINT_EVERY))
        ttk.Entry(fr, textvariable=self.v_ck, width=6).pack(side="left", padx=6)
        self.only_fix = tk.BooleanVar(value=False)
        ttk.Checkbutton(fr, text="仅修复缺失释义", variable=self.only_fix).pack(side="left", padx=12)
        btns = ttk.Frame(f); btns.pack(fill="x", padx=10, pady=6)
        ttk.Button(btns, text="选择输入", command=lambda:self._pick_json(self.e_in)).pack(side="left")
        ttk.Button(btns, text="选择输出", command=self._pick_json_out2).pack(side="left")
        ttk.Button(btns, text="开始 Enrich", command=self._run_enrich).pack(side="left")
        self.txt_article = tk.Text(f, height=8, wrap="word"); self.txt_article.pack(fill="x", padx=10, pady=6)
        ttk.Button(f, text="从上文自动提词并加入词库（入 enriched.json 草稿）", command=self._propose_into_enriched).pack(padx=10, pady=4)
        self.pb2 = ProgBar(f); self.pb2.pack(padx=10, pady=6)
        self.log2 = LogBox(f); self.log2.pack(fill="both", expand=True, padx=10, pady=6)

    def _pick_json_out2(self):
        p = filedialog.asksaveasfilename(title="保存为 JSON", defaultextension=".json", filetypes=[("JSON",".json")])
        if p: self.e_out2.set(p)

    def _run_enrich(self):
        try:
            in_p = Path(self.e_in.get()); out_p = Path(self.e_out2.get())
            bs = max(1, int(self.v_batch.get())); ck = max(0, int(self.v_ck.get()))
            only_fix = bool(self.only_fix.get())
        except Exception:
            messagebox.showerror("错误","参数不合法"); return
        def job(progress, log):
            log(f"Enrich: input={in_p} -> {out_p}  batch={bs}  ck={ck}  only_fix={only_fix}")
            res = enrich_file(in_p, out_p, batch_size=bs, checkpoint_every=ck,
                              only_fix_missing=only_fix, progress_cb=progress, show_tqdm=False)
            log(f"✓ Enriched -> {res}")
            return res
        Worker(job, on_log=self.log2.log, on_progress=lambda d,t:self.pb2.update_ratio(d,t)).start()

    def _propose_into_enriched(self):
        article = self.txt_article.get("1.0","end").strip()
        if not article:
            messagebox.showinfo("提示","请先在文本框粘贴文章内容"); return
        try:
            out_p = Path(self.e_out2.get())
            if not out_p.exists():
                messagebox.showerror("错误","请先完成一次 enrich，确保 enriched.json 已创建"); return
            store = load_json(out_p)
        except Exception:
            messagebox.showerror("错误","读取 enriched.json 失败"); return
        def job(progress, log):
            log("LLM 正在从文章提词...")
            cands = propose_from_text(article)
            if not cands:
                log("未获得候选词"); return
            exists = { (e.get('word') or '').lower() for e in store.get("entries",[]) }
            appended = 0
            for c in cands:
                w = (c.get("word") or "").strip()
                if not w or w.lower() in exists: continue
                store["entries"].append({"word":w, "meaning_zh":"", "pos":"", "synonyms_en":[], "phrases":[],
                                         "example":{"en":"","zh":""}, "confusions":[], "model_notes":"", "srs":dict(DEFAULT_SRS)})
                exists.add(w.lower()); appended += 1
            store.setdefault("meta",{}); store["meta"]["count"]=len(store["entries"])
            dump_json_atomic(out_p, store)
            log(f"已写入 {appended} 个候选到 {out_p}（随后在 Enrich 面板修复释义）")
        Worker(job, on_log=self.log2.log).start()

    # ---------------- Study ----------------（保持你之前的“记得？+造句”流程不变）
    def _build_study(self):
        f = self.tab_study
        self.e_store2 = LineEntry(f, "词库 JSON (enriched)"); self.e_store2.pack(fill="x", padx=10, pady=6)
        btns = ttk.Frame(f); btns.pack(fill="x", padx=10, pady=6)
        ttk.Button(btns, text="选择词库", command=lambda:self._pick_json(self.e_store2)).pack(side="left")
        ttk.Button(btns, text="开始一轮复习", command=self._start_study).pack(side="left")
        self.frm_q = ttk.LabelFrame(f, text="题目"); self.frm_q.pack(fill="x", padx=10, pady=6)

        self.lbl_q = ttk.Label(self.frm_q, text="尚未开始"); self.lbl_q.pack(anchor="w", padx=8, pady=8)
        ans_row = ttk.Frame(self.frm_q); ans_row.pack(fill="x", padx=8, pady=4)
        ttk.Label(ans_row, text="是否记得该词？(y/n)").pack(side="left")
        self.var_mem = tk.StringVar(); ttk.Entry(ans_row, textvariable=self.var_mem, width=6).pack(side="left", padx=6)
        self.var_sent = tk.StringVar()
        ttk.Label(self.frm_q, text="请用该词写一个英文句子：").pack(anchor="w", padx=8)
        ttk.Entry(self.frm_q, textvariable=self.var_sent, width=90).pack(fill="x", padx=8, pady=4)
        ttk.Button(self.frm_q, text="提交", command=self._submit_answer).pack(padx=8, pady=4)

        self.log3 = LogBox(f); self.log3.pack(fill="both", expand=True, padx=10, pady=6)
        self.study_queue = []; self.cur = None; self.store = None; self.store_path = None

    def _start_study(self):
        p = (self.e_store2.get() or "").strip()
        if not p:
            messagebox.showerror("错误","请先选择 enriched.json"); return
        self.store_path = Path(p)
        if not self.store_path.exists():
            messagebox.showerror("错误","词库不存在"); return
        self.store = load_json(self.store_path)
        items = self.store.get("entries",[])
        if not items:
            messagebox.showerror("错误","词库为空"); return
        # 用与 Chat 相同的抽样逻辑，保证覆盖
        pool = anti_repeat_filter(items, min_days_gap=1.0)
        chosen = weighted_sample_without_replacement(pool, 15)
        self.study_queue = chosen
        self._next_question()

    def _next_question(self):
        if not self.study_queue:
            self.lbl_q.config(text="本轮完成 🎉"); return
        self.cur = self.study_queue.pop(0)
        self.lbl_q.config(text=f"回忆释义：{self.cur.get('word','')}")
        self.var_mem.set(""); self.var_sent.set("")

    def _submit_answer(self):
        if not self.cur: return
        remembered = (self.var_mem.get().strip().lower() in ["y","yes","是","记得"])
        mzh = self.cur.get("meaning_zh",""); phs = self.cur.get("phrases",[]); ex=(self.cur.get("example") or {})
        self.log3.log(f"[参考释义] {mzh}")
        if phs: self.log3.log("[常用词组] " + "; ".join([f"{p.get('phrase')}({p.get('meaning_zh','')})" for p in phs[:6]]))
        if ex.get("en"): self.log3.log(f"[参考例句] {ex.get('en')} ({ex.get('zh')})")

        sent = self.var_sent.get().strip()
        if sent:
            result = grade_with_llm("example_usage", {
                "word": self.cur.get("word",""),
                "meaning_zh": mzh, "pos": self.cur.get("pos",""),
                "synonyms_en": self.cur.get("synonyms_en",[]),
                "phrases": phs, "example": ex,
                "confusions": self.cur.get("confusions",[]),
                "model_notes": self.cur.get("model_notes","")
            }, sent)
            self.log3.log(json.dumps(result, ensure_ascii=False, indent=2))
            score = float(result.get("score_0_1", 0.0))
        else:
            score = 0.8 if remembered else 0.3
            self.log3.log(f"[自评] remembered={remembered} => score={score}")

        # 更新 SRS
        srs0 = ensure(self.cur.get("srs") or {})
        self.cur["srs"] = update(srs0, score)
        # 写回文件
        for i, e in enumerate(self.store["entries"]):
            if (e.get("word") or "").lower() == (self.cur.get("word") or "").lower():
                self.store["entries"][i] = self.cur; break
        dump_json_atomic(self.store_path, self.store)
        self._next_question()
def run():
    App().mainloop()

if __name__ == "__main__":
    run()
