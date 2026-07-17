"""Log tab + the shared EventLog model."""

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

from ..util import ts


# Soft cap on entries kept in memory (newest wins).
EVENT_LOG_MAX = 5000


class EventLog:
    """Shared central log of every interaction the tool performs.

    Categories ("kinds") used:
        scan / connect / disconnect — link-layer events
        read / write / subscribe / unsubscribe — GATT operations
        notify — incoming HVN payload (only logged if subscribed via Log tab)
        ui    — user clicked a button
        test  — validation test events
        error — anything that raised
        info  — generic informational

    The widget is wired by LogTab once it's built. Until then, entries
    accumulate in self.entries.
    """

    def __init__(self):
        self.entries: List[dict] = []
        self.text_widget: Optional[scrolledtext.ScrolledText] = None
        self.show_kinds: Optional[set] = None   # None = show all

    def log(self, kind: str, source: str, message: str):
        entry = {"ts": datetime.now(), "kind": kind,
                 "source": source, "message": message}
        self.entries.append(entry)
        if len(self.entries) > EVENT_LOG_MAX:
            del self.entries[: len(self.entries) - EVENT_LOG_MAX]
        if self.text_widget is not None:
            try:
                self._append_widget(entry)
            except Exception:
                pass

    def _append_widget(self, entry: dict):
        if self.show_kinds is not None and entry["kind"] not in self.show_kinds:
            return
        line = (f"[{entry['ts']:%H:%M:%S.%f}][{entry['source']:>5}]"
                f"[{entry['kind']:>11}] {entry['message']}\n")
        # Trim microseconds to 3 digits for readability.
        line = line.replace(line[10:16], line[10:13], 1)
        self.text_widget.configure(state="normal")
        self.text_widget.insert("end", line, (entry["kind"],))
        self.text_widget.see("end")
        self.text_widget.configure(state="disabled")

    def rerender(self):
        if self.text_widget is None:
            return
        self.text_widget.configure(state="normal")
        self.text_widget.delete("1.0", "end")
        self.text_widget.configure(state="disabled")
        for e in self.entries:
            self._append_widget(e)


class LogTab(ttk.Frame):
    """Live activity log of every BLE op + UI action this session."""

    KIND_COLOURS = {
        "scan":         "#1f5fbf",
        "connect":      "#1b8c3a",
        "disconnect":   "#cf8a17",
        "read":         "#444444",
        "write":        "#5a3da4",
        "subscribe":    "#0e7a8a",
        "unsubscribe":  "#0e7a8a",
        "notify":       "#7a7a7a",
        "ui":           "#000000",
        "test":         "#1b8c3a",
        "error":        "#c0392b",
        "info":         "#444444",
    }
    ALL_KINDS = list(KIND_COLOURS.keys())

    def __init__(self, parent, event_log: EventLog):
        super().__init__(parent)
        self.event_log = event_log

        # Toolbar
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="Clear",
                   command=self._do_clear).pack(side="left", padx=2)
        ttk.Button(bar, text="Save to file…",
                   command=self._do_save).pack(side="left", padx=2)
        ttk.Button(bar, text="Refresh",
                   command=event_log.rerender).pack(side="left", padx=2)

        # Filter checkboxes
        flt = ttk.LabelFrame(self, text="Filters", padding=4)
        flt.pack(fill="x", padx=6, pady=2)
        self._filter_vars: Dict[str, tk.BooleanVar] = {}
        for i, k in enumerate(self.ALL_KINDS):
            v = tk.BooleanVar(value=True)
            self._filter_vars[k] = v
            ttk.Checkbutton(flt, text=k, variable=v,
                             command=self._apply_filter).grid(
                row=i // 6, column=i % 6, sticky="w", padx=4, pady=1)

        # Log text
        lf = ttk.LabelFrame(self, text="Activity log", padding=4)
        lf.pack(fill="both", expand=True, padx=6, pady=4)
        self.text = scrolledtext.ScrolledText(lf, state="disabled",
                                                font=("Courier", 9),
                                                wrap="none")
        for kind, colour in self.KIND_COLOURS.items():
            self.text.tag_configure(kind, foreground=colour)
        self.text.pack(fill="both", expand=True)

        # Wire to the EventLog
        event_log.text_widget = self.text
        event_log.rerender()
        # Self-log this binding so the user sees the tab is alive.
        event_log.log("info", "log", "Log tab attached")

    def _apply_filter(self):
        kinds = {k for k, v in self._filter_vars.items() if v.get()}
        self.event_log.show_kinds = kinds if len(kinds) < len(self.ALL_KINDS) else None
        self.event_log.rerender()

    def _do_clear(self):
        self.event_log.entries.clear()
        self.event_log.rerender()
        self.event_log.log("ui", "log", "log cleared")

    def _do_save(self):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"),
                        ("All files", "*.*")],
            initialfile=f"dusq_session_{datetime.now():%Y%m%d-%H%M%S}.log")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for e in self.event_log.entries:
                    f.write(f"[{e['ts']:%Y-%m-%d %H:%M:%S.%f}]"
                            f"[{e['source']:>5}][{e['kind']:>11}] "
                            f"{e['message']}\n")
            self.event_log.log("ui", "log", f"saved {len(self.event_log.entries)} entries to {path}")
        except Exception as ex:
            messagebox.showerror("Save log", str(ex))
