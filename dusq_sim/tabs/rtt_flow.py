"""RTT / Flow monitor tab."""

import asyncio
import json
import os
import queue
import re
import struct
import sys
import threading
import time
import zlib
import tkinter as tk
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..rtt import *


_FLOW_COLORS = {
    "boot": "#eaf2ff", "adv": "#f3f0ff", "conn": "#e8f8f0", "auth": "#e6fbf2",
    "sensor": "#fffbe6", "io": "#f0f4f8", "xfer": "#eef6ff", "haptic": "#fdeef6",
    "init": "#f2f2f2", "time": "#e0f7f7", "error": "#fdecea", "idle": "#ededed",
}


class RttTab(ttk.Frame):
    _MAX_RAW_LINES = 4000

    def __init__(self, parent):
        super().__init__(parent)
        self.engine = FlowEngine()
        self.reader: Optional[RttReader] = None
        self.q: "queue.Queue" = queue.Queue()
        self._build()
        self.after(200, self._poll)

    def _build(self):
        top = ttk.Frame(self)
        top.pack(side="top", fill="x", padx=6, pady=4)
        ttk.Label(top, text="Target:").pack(side="left")
        self.target_var = tk.StringVar(value=RTT_TARGET_DEFAULT)
        ttk.Entry(top, textvariable=self.target_var, width=15).pack(side="left", padx=(2, 8))
        ttk.Label(top, text="J-Link S/N:").pack(side="left")
        self.serial_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.serial_var, width=11).pack(side="left", padx=(2, 8))
        self.connect_btn = ttk.Button(top, text="Connect RTT", command=self._toggle)
        self.connect_btn.pack(side="left", padx=2)
        ttk.Button(top, text="Clear", command=self._clear).pack(side="left", padx=2)
        ttk.Button(top, text="Save log...", command=self._save).pack(side="left", padx=2)
        self.status_var = tk.StringVar(
            value="idle" if HAS_PYLINK else "pylink-square not installed")
        ttk.Label(top, textvariable=self.status_var, foreground="gray").pack(side="right")

        outer = ttk.Panedwindow(self, orient="vertical")
        outer.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        upper = ttk.Panedwindow(outer, orient="horizontal")
        rawf = ttk.Labelframe(upper, text="Raw RTT log", padding=2)
        self.raw = scrolledtext.ScrolledText(rawf, height=16, state="disabled",
                                             font=("Consolas", 9), wrap="none")
        self.raw.pack(fill="both", expand=True)
        self.raw.tag_config("error", foreground="#c0392b")
        self.raw.tag_config("warn", foreground="#d35400")

        flowf = ttk.Labelframe(upper, text="Device flow", padding=2)
        self.flow = ttk.Treeview(flowf, columns=("t", "evt"), show="headings", height=16)
        self.flow.heading("t", text="Time")
        self.flow.column("t", width=66, anchor="w", stretch=False)
        self.flow.heading("evt", text="Event")
        self.flow.column("evt", width=360, anchor="w")
        self.flow.pack(fill="both", expand=True)
        for cat, col in _FLOW_COLORS.items():
            self.flow.tag_configure(cat, background=col)
        upper.add(rawf, weight=3)
        upper.add(flowf, weight=2)
        outer.add(upper, weight=3)

        anomf = ttk.Labelframe(outer, text="Flags / anomalies", padding=2)
        self.anom = ttk.Treeview(anomf, columns=("t", "sev", "issue", "detail"),
                                 show="headings", height=7)
        for c, w, txt, st in [("t", 66, "Time", False), ("sev", 52, "Sev", False),
                              ("issue", 210, "Issue", False), ("detail", 380, "Detail", True)]:
            self.anom.heading(c, text=txt)
            self.anom.column(c, width=w, anchor="w", stretch=st)
        self.anom.pack(fill="both", expand=True)
        self.anom.tag_configure("crit", foreground="#c0392b")
        self.anom.tag_configure("warn", foreground="#d35400")
        outer.add(anomf, weight=1)

        if not HAS_PYLINK:
            self.connect_btn.config(state="disabled")

    # ── controls ──
    def _toggle(self):
        if self.reader and self.reader.is_alive():
            self.stop()
        else:
            self._start()

    def _start(self):
        if not HAS_PYLINK:
            messagebox.showerror("RTT", "pylink-square not installed:\n\n    pip install pylink-square")
            return
        self.reader = RttReader(self.target_var.get(), self.serial_var.get(), self.q)
        self.reader.start()
        self.connect_btn.config(text="Stop RTT")
        self.status_var.set("connecting...")

    def stop(self):
        if self.reader:
            self.reader.stop()
        self.connect_btn.config(text="Connect RTT")

    def _clear(self):
        self.raw.config(state="normal")
        self.raw.delete("1.0", "end")
        self.raw.config(state="disabled")
        self.flow.delete(*self.flow.get_children())
        self.anom.delete(*self.anom.get_children())
        self.engine = FlowEngine()

    def _save(self):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(defaultextension=".log",
                                            filetypes=[("Log", "*.log"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.raw.get("1.0", "end"))
            self.status_var.set(f"saved {os.path.basename(path)}")
        except OSError as e:
            messagebox.showerror("Save", str(e))

    # ── pump (Tk main thread) ──
    def _poll(self):
        try:
            for _ in range(1000):
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    self._on_line(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "error":
                    self.status_var.set("error: " + payload)
                    self._add_anom(datetime.now(), "crit", "RTT error", payload)
                    self.connect_btn.config(text="Connect RTT")
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _on_line(self, raw: str):
        ev = self.engine.feed(raw)
        if ev is None:
            return
        tag = ev.sev if ev.sev in ("error", "warn") else ""
        self.raw.config(state="normal")
        self.raw.insert("end", ev.dt.strftime("%H:%M:%S ") + ev.text + "\n",
                        (tag,) if tag else ())
        line_count = int(self.raw.index("end-1c").split(".")[0])
        if line_count > self._MAX_RAW_LINES:
            self.raw.delete("1.0", f"{line_count - self._MAX_RAW_LINES}.0")
        self.raw.see("end")
        self.raw.config(state="disabled")

        for cat, label in ev.flow:
            iid = self.flow.insert("", "end",
                                   values=(ev.dt.strftime("%H:%M:%S"), label), tags=(cat,))
            self.flow.see(iid)
        for sev, title, detail in ev.anomalies:
            self._add_anom(ev.dt, sev, title, detail)

    def _add_anom(self, dt: datetime, sev: str, title: str, detail: str):
        iid = self.anom.insert("", "end",
                               values=(dt.strftime("%H:%M:%S"), sev.upper(), title, detail),
                               tags=(sev,))
        self.anom.see(iid)
