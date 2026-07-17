"""battery tab."""

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

from ..constants import *
from ..decoders import *
from ..util import *
from ..ble import BLEManager

BATT_HISTORY_MAX = 50    # rows kept in the history list


class BatteryTab(ttk.Frame):
    """Dedicated view for the SIG Battery Service (0x180F / 0x2A19).

    Shows the current battery percent in a large, threshold-coloured label,
    plus a rolling list of the last 50 readings with a signed delta column
    so charge-vs-drain trends are obvious at a glance.
    """

    def __init__(self, parent, ble: BLEManager):
        super().__init__(parent)
        self.ble = ble
        self._last_pct: Optional[int] = None
        self._subscribed = False
        # NOTE: "Battery % over session" chart removed per UX overhaul —
        # the visual icon on the left of the live readout + the 50-row
        # history list cover the same need with less clutter.

        # ── Live readout (battery icon on the LEFT) ──────────────────────
        live = ttk.LabelFrame(self, text="Live Battery Level", padding=10)
        live.pack(fill="x", padx=6, pady=6)

        # Visual battery — vertical rectangle with a tip cap; fill height
        # tracks percent.  Drawn on a Tk Canvas (no extra deps).
        # NOTE: don't try to read live.cget("background") — ttk widgets
        # don't expose `-background` (TclError). Default canvas bg is fine.
        self.batt_canvas = tk.Canvas(live, width=80, height=160,
                                       highlightthickness=0)
        self.batt_canvas.grid(row=0, column=0, rowspan=3, padx=(6, 18),
                              pady=4)
        self._draw_battery(pct=None)

        self.pct_var = tk.StringVar(value="--")
        self.pct_label = tk.Label(live, textvariable=self.pct_var,
                                   font=("Courier", 28, "bold"),
                                   foreground="gray", width=6, anchor="center")
        self.pct_label.grid(row=0, column=1, rowspan=2, padx=12, pady=4)

        self.unit_label = tk.Label(live, text="%", font=("Courier", 18, "bold"),
                                    foreground="gray")
        self.unit_label.grid(row=0, column=2, rowspan=2, sticky="w")

        ttk.Label(live, text="Last update:").grid(row=0, column=3, sticky="e", padx=8)
        self.last_var = tk.StringVar(value="(never)")
        ttk.Label(live, textvariable=self.last_var,
                  font=("Courier", 10)).grid(row=0, column=4, sticky="w")

        ttk.Label(live, text="Subscription:").grid(row=1, column=3, sticky="e", padx=8)
        self.sub_var = tk.StringVar(value="off")
        self.sub_label = ttk.Label(live, textvariable=self.sub_var,
                                    font=("Courier", 10, "bold"),
                                    foreground="gray")
        self.sub_label.grid(row=1, column=4, sticky="w")

        # ── Action buttons ────────────────────────────────────────────────
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=6, pady=2)
        ttk.Button(btns, text="Read Once",
                   command=self._do_read).pack(side="left", padx=4)
        ttk.Button(btns, text="Subscribe",
                   command=self._do_subscribe).pack(side="left", padx=4)
        ttk.Button(btns, text="Unsubscribe",
                   command=self._do_unsubscribe).pack(side="left", padx=4)
        ttk.Button(btns, text="Clear History",
                   command=self._do_clear).pack(side="left", padx=4)

        # ── History list ──────────────────────────────────────────────────
        hist_frame = ttk.LabelFrame(self, text="History (last 50 readings)",
                                     padding=4)
        hist_frame.pack(fill="both", expand=True, padx=6, pady=4)

        cols = ("time", "pct", "delta")
        self.tree = ttk.Treeview(hist_frame, columns=cols, show="headings",
                                  height=8)
        for c, w, label in [("time", 120, "Time"),
                              ("pct", 100, "Battery %"),
                              ("delta", 100, "Δ vs prev")]:
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="center")
        sb = ttk.Scrollbar(hist_frame, orient="vertical",
                           command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # ── Log pane ──────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(self, text="Log", padding=4)
        log_frame.pack(fill="both", expand=True, padx=6, pady=4)
        self.log_box = scrolledtext.ScrolledText(log_frame, height=5,
                                                  state="disabled",
                                                  font=("Courier", 9))
        self.log_box.pack(fill="both", expand=True)

    # ── helpers ───────────────────────────────────────────────────────────

    def _log(self, msg: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{ts()}] {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _colour_for(self, pct: int) -> str:
        if pct >= 80:
            return "#1b8c3a"     # green
        if pct >= 50:
            return "#1f5fbf"     # blue
        if pct >= 20:
            return "#cf8a17"     # orange
        return "#c0392b"         # red

    def _draw_battery(self, pct: Optional[int]):
        """Render a vertical battery icon on the canvas.  pct=None draws an
        empty (grey) battery; otherwise the inner rectangle fills from the
        bottom up to `pct` percent.  Color thresholds match `_colour_for`."""
        c = self.batt_canvas
        c.delete("all")
        w  = int(c.cget("width"))
        h  = int(c.cget("height"))
        # Battery body outline (with a small top cap).
        cap_w = w // 3
        cap_h = 10
        body_top    = cap_h
        body_left   = 6
        body_right  = w - 6
        body_bottom = h - 6
        # Cap (small rectangle at top)
        cap_left  = (w - cap_w) // 2
        cap_right = cap_left + cap_w
        c.create_rectangle(cap_left, 0, cap_right, cap_h,
                            fill="#7f8c8d", outline="#34495e", width=1)
        # Body outline
        c.create_rectangle(body_left, body_top, body_right, body_bottom,
                            outline="#34495e", width=2)
        # Fill — proportional from the bottom up
        if pct is not None:
            pct_clamped = max(0, min(100, int(pct)))
            inner_h = body_bottom - body_top - 4
            fill_h  = int(inner_h * pct_clamped / 100)
            if fill_h > 0:
                fill_top = body_bottom - 2 - fill_h
                c.create_rectangle(body_left + 2, fill_top,
                                    body_right - 2, body_bottom - 2,
                                    fill=self._colour_for(pct_clamped),
                                    outline="")
            # Centred percent label inside the body
            c.create_text((body_left + body_right) // 2,
                          (body_top + body_bottom) // 2,
                          text=f"{pct_clamped}%",
                          font=("Segoe UI", 14, "bold"),
                          fill="white" if pct_clamped >= 35 else "#2c3e50")
        else:
            c.create_text(w // 2, h // 2, text="--",
                          font=("Segoe UI", 14, "bold"),
                          fill="#95a5a6")

    def _apply_reading(self, pct: int):
        """Push a new reading into the live readout, icon, and history list."""
        delta = 0 if self._last_pct is None else pct - self._last_pct
        self._last_pct = pct

        colour = self._colour_for(pct)
        self.pct_var.set(f"{pct:>3}")
        self.pct_label.configure(foreground=colour)
        self.unit_label.configure(foreground=colour)
        self.last_var.set(ts())

        # Redraw the visual battery icon
        self._draw_battery(pct)

        delta_str = f"{delta:+d}" if delta != 0 else "0"
        self.tree.insert("", 0, values=(ts(), f"{pct} %", delta_str))

        # Cap history depth (newest at top, drop from bottom)
        children = self.tree.get_children()
        if len(children) > BATT_HISTORY_MAX:
            for row in children[BATT_HISTORY_MAX:]:
                self.tree.delete(row)

    # ── notification handler ──────────────────────────────────────────────

    def _on_batt_notify(self, _h, data: bytearray):
        if not data:
            return
        self._apply_reading(int(data[0]))

    # ── actions ───────────────────────────────────────────────────────────

    def _do_read(self):
        if not self.ble.connected:
            messagebox.showwarning("Battery", "Not connected.")
            return

        async def _read():
            try:
                raw = await self.ble.read(CHAR_BATT_LEVEL)
                if not raw:
                    self._log("Read returned empty payload.")
                    return
                self._apply_reading(int(raw[0]))
                self._log(f"Read once: {raw[0]} %.")
            except Exception as e:
                self._log(f"Read failed: {e}")
        run_async(_read())

    def _do_subscribe(self):
        if not self.ble.connected:
            messagebox.showwarning("Battery", "Not connected.")
            return
        if self._subscribed:
            self._log("Already subscribed.")
            return

        async def _sub():
            try:
                await self.ble.subscribe(CHAR_BATT_LEVEL, self._on_batt_notify)
                self._subscribed = True
                self.sub_var.set("on")
                self.sub_label.configure(foreground="#1b8c3a")
                self._log("Subscribed to BAS notifications.")
            except Exception as e:
                self._log(f"Subscribe failed: {e}")
        run_async(_sub())

    def auto_subscribe(self):
        """Public entry point used by AuthTab on successful authentication.
        Silently no-ops if disconnected or already subscribed — never raises
        a messagebox, since the caller is a callback in another tab."""
        if not self.ble.connected or self._subscribed:
            return

        async def _sub():
            try:
                await self.ble.subscribe(CHAR_BATT_LEVEL, self._on_batt_notify)
                self._subscribed = True
                self.sub_var.set("on")
                self.sub_label.configure(foreground="#1b8c3a")
                self._log("Auto-subscribed to BAS after auth.")
            except Exception as e:
                self._log(f"Auto-subscribe failed: {e}")
        run_async(_sub())

    def _on_tab_visible(self):
        """Fired by App._on_tab_changed when the user switches to this tab.
        Same idempotent subscribe as auto_subscribe()."""
        self.auto_subscribe()

    def _do_unsubscribe(self):
        if not self.ble.connected or not self._subscribed:
            return

        async def _unsub():
            try:
                await self.ble.unsubscribe(CHAR_BATT_LEVEL)
            finally:
                self._subscribed = False
                self.sub_var.set("off")
                self.sub_label.configure(foreground="gray")
                self._log("Unsubscribed.")
        run_async(_unsub())

    def _do_clear(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._last_pct = None
        self._draw_battery(None)
        self._log("History cleared.")

    def _on_disconnected(self):
        """Wipe live readout, icon, and history when the BLE link drops.
        Also resets _subscribed so the next auto_subscribe() actually fires
        instead of taking the 'already subscribed' early-return."""
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._last_pct = None
        self._draw_battery(None)

        self.pct_var.set("--")
        self.pct_label.configure(foreground="gray")
        self.unit_label.configure(foreground="gray")
        self.last_var.set("(never)")

        self._subscribed = False
        self.sub_var.set("off")
        self.sub_label.configure(foreground="gray")

        self._log("Cleared on disconnect.")
