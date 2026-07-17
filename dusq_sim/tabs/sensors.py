"""sensors tab."""

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

MAX_HISTORY = 60   # readings kept for chart

class SensorTab(ttk.Frame):
    def __init__(self, parent, ble: BLEManager):
        super().__init__(parent)
        self.ble = ble
        # Tracks whether the 3 notify chars (sensor/batt/status) are already
        # subscribed for this connection — used by _on_tab_visible to avoid
        # re-subscribing on every tab focus.  Reset on disconnect.
        self._subscribed = False

        # Live value StringVars
        self.sv_temp     = tk.StringVar(value="--")
        self.sv_db       = tk.StringVar(value="--")
        self.sv_peak_db  = tk.StringVar(value="--")
        self.sv_lux      = tk.StringVar(value="--")
        self.sv_batt     = tk.StringVar(value="--")
        self.sv_status   = tk.StringVar(value="--")

        # History lists for chart
        self.hist_temp   : List[Optional[float]] = []
        self.hist_db     : List[Optional[float]] = []
        self.hist_lux    : List[Optional[float]] = []

        self._build_ui()

    def _on_tab_visible(self):
        """Called by App._on_tab_changed when this tab becomes visible.
        Subscribes to the 3 notify chars if connected and not already
        subscribed.  Silently no-ops otherwise — disconnect path resets the
        flag so the next tab visit re-subscribes."""
        if not self.ble.connected or self._subscribed:
            return
        self._do_subscribe()

    def _build_ui(self):
        # Control bar
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=6, pady=4)
        ttk.Button(ctrl, text="Subscribe to Notifications",
                   command=self._do_subscribe).pack(side="left", padx=4)
        ttk.Button(ctrl, text="Read Once (all)",
                   command=self._do_read_once).pack(side="left", padx=4)

        # Live values grid
        vf = ttk.LabelFrame(self, text="Live Values", padding=8)
        vf.pack(fill="x", padx=6, pady=2)
        fields = [
            ("Temperature (°C)",  self.sv_temp),
            ("dB (avg)",          self.sv_db),
            ("dB (peak)",         self.sv_peak_db),
            ("Lux",               self.sv_lux),
            ("Battery %",         self.sv_batt),
            ("System Status",     self.sv_status),
        ]
        for i, (label, sv) in enumerate(fields):
            r, c = divmod(i, 2)
            ttk.Label(vf, text=label + ":").grid(row=r, column=c*2,     sticky="w", padx=4, pady=2)
            ttk.Label(vf, textvariable=sv, font=("Courier", 11, "bold"),
                      width=22).grid(row=r, column=c*2+1, sticky="w")

        # Chart (matplotlib) or text history fallback
        if HAS_MPL:
            cf = ttk.LabelFrame(self, text="History (last 60 readings)", padding=4)
            cf.pack(fill="both", expand=True, padx=6, pady=4)
            fig = Figure(figsize=(9, 2.6), dpi=90)
            fig.subplots_adjust(wspace=0.35, left=0.07, right=0.98,
                                 top=0.85, bottom=0.18)
            self.ax_temp = fig.add_subplot(131)
            self.ax_db   = fig.add_subplot(132)
            self.ax_lux  = fig.add_subplot(133)
            self.canvas  = FigureCanvasTkAgg(fig, master=cf)
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            # Per-axes hover artifacts (faint horizontal line + left-edge value
            # label).  Re-created on every _update_chart() because ax.clear()
            # wipes them.
            self._hover_artifacts = {}   # ax -> (Line2D, Text)
            self._hover_fmt = {}         # ax -> str format spec
            fig.canvas.mpl_connect("motion_notify_event", self._on_chart_hover)
            fig.canvas.mpl_connect("axes_leave_event",    self._on_chart_leave)
            fig.canvas.mpl_connect("figure_leave_event",  self._on_chart_leave)
            self._update_chart()
        else:
            lf = ttk.LabelFrame(self, text="Reading Log", padding=4)
            lf.pack(fill="both", expand=True, padx=6, pady=4)
            self.hist_box = scrolledtext.ScrolledText(lf, height=10, state="disabled",
                                                       font=("Courier", 9))
            self.hist_box.pack(fill="both", expand=True)

    # ── chart ────────────────────────────────────────────────────────────────

    def _update_chart(self):
        if not HAS_MPL:
            return
        self.ax_temp.clear()
        self.ax_db.clear()
        self.ax_lux.clear()
        xs = list(range(len(self.hist_temp)))
        ys_t = [v if v is not None else float("nan") for v in self.hist_temp]
        self.ax_temp.plot(xs, ys_t, "b-o", ms=3, label="Temp °C")
        self.ax_temp.set_title("Temperature (°C)")
        self.ax_temp.set_ylabel("°C")
        self.ax_temp.grid(True, alpha=0.4)

        ys_db = [v if v is not None else float("nan") for v in self.hist_db]
        self.ax_db.plot(xs, ys_db, "g-o", ms=3, label="dB")
        self.ax_db.set_title("Sound Level (dB)")
        self.ax_db.set_ylabel("dB")
        self.ax_db.legend(fontsize=7)
        self.ax_db.grid(True, alpha=0.4)

        ys_lux = [v if v is not None else float("nan") for v in self.hist_lux]
        self.ax_lux.plot(xs, ys_lux, "-o", color="#cc8a17", ms=3, label="Lux")
        self.ax_lux.set_title("Light (Lux)")
        self.ax_lux.set_ylabel("lux")
        self.ax_lux.grid(True, alpha=0.4)

        # Re-install hover artifacts (ax.clear() above removed the previous ones).
        self._hover_artifacts.clear()
        self._hover_fmt = {
            self.ax_temp: "{:.1f}",
            self.ax_db:   "{:.0f}",
            self.ax_lux:  "{:.0f}",
        }
        for ax in (self.ax_temp, self.ax_db, self.ax_lux):
            line = ax.axhline(0, color="#888", lw=0.6, alpha=0.35, visible=False,
                              zorder=1)
            label = ax.text(
                0.015, 0.0, "",
                transform=ax.get_yaxis_transform(),     # x in axes coords, y in data
                ha="left", va="center",
                fontsize=11, fontweight="bold", color="#222",
                bbox=dict(boxstyle="round,pad=0.25",
                          fc="white", ec="#888", alpha=0.85),
                visible=False, zorder=5,
            )
            self._hover_artifacts[ax] = (line, label)

        self.canvas.draw_idle()

    # ── hover crosshair ──────────────────────────────────────────────────────

    def _on_chart_hover(self, event):
        if not HAS_MPL or not self._hover_artifacts:
            return
        ax = event.inaxes
        if ax is None or event.ydata is None or ax not in self._hover_artifacts:
            self._on_chart_leave(event)
            return
        y = event.ydata
        line, label = self._hover_artifacts[ax]
        line.set_ydata([y, y])
        line.set_visible(True)
        label.set_position((0.015, y))
        label.set_text(self._hover_fmt.get(ax, "{:.2f}").format(y))
        label.set_visible(True)
        # Hide artifacts on the other axes so only the hovered one shows.
        for other_ax, (other_line, other_label) in self._hover_artifacts.items():
            if other_ax is ax:
                continue
            if other_line.get_visible() or other_label.get_visible():
                other_line.set_visible(False)
                other_label.set_visible(False)
        self.canvas.draw_idle()

    def _on_chart_leave(self, _event):
        if not HAS_MPL or not self._hover_artifacts:
            return
        dirty = False
        for line, label in self._hover_artifacts.values():
            if line.get_visible() or label.get_visible():
                line.set_visible(False)
                label.set_visible(False)
                dirty = True
        if dirty:
            self.canvas.draw_idle()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _on_disconnected(self):
        """Wipe all live state so a fresh connection starts from zero.
        Wired up by App via BLEManager.add_disconnect_sub()."""
        self._subscribed = False
        for lst in (self.hist_temp, self.hist_db, self.hist_lux):
            lst.clear()
        for sv in (self.sv_temp, self.sv_db, self.sv_peak_db,
                   self.sv_lux, self.sv_batt, self.sv_status):
            sv.set("--")
        if HAS_MPL:
            self._update_chart()
        elif hasattr(self, "hist_box"):
            try:
                self.hist_box.configure(state="normal")
                self.hist_box.insert("end", f"[{ts()}] -- disconnected --\n")
                self.hist_box.see("end")
                self.hist_box.configure(state="disabled")
            except Exception:
                pass

    # ── notification handlers ────────────────────────────────────────────────

    def _push_reading(self):
        """Append current live values to history lists and refresh chart."""
        def safe_float(sv: tk.StringVar) -> Optional[float]:
            try:
                return float(sv.get())
            except ValueError:
                return None

        self.hist_temp.append(safe_float(self.sv_temp))
        self.hist_db.append(safe_float(self.sv_db))
        self.hist_lux.append(safe_float(self.sv_lux))

        for lst in (self.hist_temp, self.hist_db, self.hist_lux):
            if len(lst) > MAX_HISTORY:
                lst.pop(0)

        if HAS_MPL:
            self._update_chart()
        else:
            try:
                line = (f"temp={self.sv_temp.get():>5}°C  "
                        f"db={self.sv_db.get():>3}  "
                        f"lux={self.sv_lux.get():>6}  "
                        f"batt={self.sv_batt.get():>3}%")
                self.hist_box.configure(state="normal")
                self.hist_box.insert("end", f"[{ts()}] {line}\n")
                self.hist_box.see("end")
                self.hist_box.configure(state="disabled")
            except Exception:
                pass

    def _on_sensor(self, _h, data: bytearray):
        """Merged sensor char (0x1526).  Two firmware payload layouts supported:
           new (6 B):  <h B B H>  int16 LE temp (0.1 degC), db, peak_db, uint16 LE lux
           old (5 B):  <b B B H>  int8 temp degC,           db, peak_db, uint16 LE lux
        """
        temp_c, db, peak_db, lux = _decode_sensor_payload(data)
        if temp_c is None and (not data):
            return  # truly empty packet
        self.sv_temp.set("ERR" if temp_c is None else f"{temp_c:.1f}")
        self.sv_db.set("ERR" if db == DB_ERROR else str(db))
        self.sv_peak_db.set("ERR" if peak_db == DB_ERROR else str(peak_db))
        self.sv_lux.set("ERR" if lux == LUX_ERROR else str(lux))
        self._push_reading()

    def _on_batt(self, _h, data: bytearray):
        self.sv_batt.set(str(data[0]))

    def _on_status(self, _h, data: bytearray):
        s = decode_status(bytes(data))
        if s:
            self.sv_status.set(
                f"{s['state']}  USB={int(s['usb'])}  "
                f"Boost={int(s['boost'])}  Lid={'C' if s['lid_closed'] else 'O'}"
            )

    # ── actions ──────────────────────────────────────────────────────────────

    def _do_subscribe(self):
        if not self.ble.connected:
            messagebox.showwarning("Sensor", "Not connected.")
            return
        # Re-entrancy guard: the awaits below take a few ms each, and during
        # that window auto_subscribe() (on tab focus) or a second button click
        # would also see self._subscribed == False and queue another _sub().
        # That registered two start_notify callbacks on CHAR_SENSOR_DATA, so
        # every notify pushed twice — visible as 2 points per 5 s on the chart.
        # Flip the flag synchronously here; reset it in the failure path.
        if self._subscribed:
            return
        self._subscribed = True
        async def _sub():
            # Subscribe to each char INDEPENDENTLY: a failure on one (e.g. a stale
            # Windows GATT cache after re-flashing makes a char look non-notifiable)
            # must not stop the others. Report exactly which char failed.
            targets = (
                ("Sensor Data (0x1526)", CHAR_SENSOR_DATA, self._on_sensor),
                ("Battery (0x2A19)",     CHAR_BATT_LEVEL,  self._on_batt),
                ("Status (0x1529)",      CHAR_STATUS,      self._on_status),
            )
            ok = 0
            failed = []
            for label, uuid, cb in targets:
                try:
                    await self.ble.subscribe(uuid, cb)
                    ok += 1
                except Exception as e:
                    failed.append(f"  • {label}: {e}")
            if ok == 0:
                # Nothing subscribed — allow a retry (don't leave the guard set).
                self._subscribed = False
            if failed:
                messagebox.showerror(
                    "Subscribe",
                    "Some characteristics could not be subscribed "
                    f"({ok}/{len(targets)} succeeded):\n\n"
                    + "\n".join(failed)
                    + "\n\nAll three ARE notify-capable in firmware. If you just "
                      "re-flashed, Windows is likely serving a STALE GATT cache — "
                      "remove the device under Settings > Bluetooth (or toggle the "
                      "adapter), then rescan.")
        run_async(_sub())

    def _do_read_once(self):
        if not self.ble.connected:
            messagebox.showwarning("Sensor", "Not connected.")
            return
        async def _read():
            try:
                sensor_raw = await self.ble.read(CHAR_SENSOR_DATA)
                temp_c, db, peak_db, lux = _decode_sensor_payload(sensor_raw)
                if db is not None:
                    self.sv_temp.set("ERR" if temp_c is None else f"{temp_c:.1f}")
                    self.sv_db.set("ERR" if db == DB_ERROR else str(db))
                    self.sv_peak_db.set("ERR" if peak_db == DB_ERROR else str(peak_db))
                    self.sv_lux.set("ERR" if lux == LUX_ERROR else str(lux))

                batt = (await self.ble.read(CHAR_BATT_LEVEL))[0]
                self.sv_batt.set(str(batt))

                stat = decode_status(await self.ble.read(CHAR_STATUS))
                if stat:
                    self.sv_status.set(
                        f"{stat['state']}  USB={int(stat['usb'])}  "
                        f"Boost={int(stat['boost'])}  Lid={'C' if stat['lid_closed'] else 'O'}"
                    )
                self._push_reading()
            except Exception as e:
                messagebox.showerror("Read", str(e))
        run_async(_read())
