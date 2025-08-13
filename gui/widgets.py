# -*- coding: utf-8 -*-
import threading
import tkinter as tk
from tkinter import ttk


class LineEntry(ttk.Frame):
    def __init__(self, master, label, width=50, **kw):
        super().__init__(master, **kw)
        ttk.Label(self, text=label, width=18).pack(side="left")
        self.var = tk.StringVar()
        ttk.Entry(self, textvariable=self.var, width=width).pack(
            side="left", fill="x", expand=True)

    def get(self): return self.var.get()
    def set(self, v): self.var.set(v or "")


class LogBox(ttk.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.text = tk.Text(self, height=12, wrap="word")
        y = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.config(yscrollcommand=y.set)
        self.text.pack(side="left", fill="both", expand=True)
        y.pack(side="right", fill="y")

    def log(self, s): self.text.insert("end", s+"\n"); self.text.see("end")


class ProgBar(ttk.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.pb = ttk.Progressbar(
            self, length=320, mode="determinate", maximum=100)
        self.lbl = ttk.Label(self, text="0%")
        self.pb.pack(side="left", padx=6)
        self.lbl.pack(side="left")

    def update_ratio(self, done, total):
        pct = 0 if total <= 0 else int(done*100/total)
        self.pb["value"] = pct
        self.lbl.config(text=f"{pct}%")


class Worker:
    """
    后台线程执行 job；job(progress_cb, log_cb) 两个回调可用。
    """

    def __init__(self, fn, on_done=None, on_log=None, on_progress=None):
        self.fn = fn
        self.on_done = on_done
        self.on_log = on_log
        self.on_progress = on_progress

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _run(self):
        try:
            res = self.fn(self._progress, self._log)
            if self.on_done:
                self.on_done(res)
        except Exception as ex:
            if self.on_log:
                self.on_log(f"[ERROR] {ex}")

    def _log(self, s):
        if self.on_log:
            self.on_log(str(s))

    def _progress(self, done, total):
        if self.on_progress:
            self.on_progress(done, total)
