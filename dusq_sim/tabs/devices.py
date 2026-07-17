"""devices tab."""

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

from bleak import BleakScanner   # AdvTab runs its own passive scanner (separate from BLEManager)

from ..constants import *
from ..decoders import *
from ..util import *
from ..ble import BLEManager


class AdvTab(ttk.Frame):
    """Continuously scans for BLE advertisers, decodes the DUSQ MSD payload
    where present, and lets the user connect by double-clicking any row.

    Default filter: device name contains "DUSQ_CHARGER" OR a valid DUSQ MSD
    decodes. Tick "Show all (debug)" to also list non-DUSQ devices (with
    name / MAC / RSSI only — no decoded columns)."""

    REFRESH_MS = 500   # period for "Seen N s ago" refresh

    def __init__(self, parent, ble: BLEManager, status_var: tk.StringVar):
        super().__init__(parent)
        self.ble = ble
        self.status_var = status_var
        self._scanner = None
        self._scanning = False
        # addr -> {'name', 'rssi', 'raw', 'decoded', 'seen', 'is_target'}
        self._devices = {}
        # Wire BLE log/disconnect callbacks (ported from old ConnectionTab)
        self.ble.log_cb = self._log
        self.ble.on_disconnect_cb = self._on_disconnected

        # --- Controls -------------------------------------------------------
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=6, pady=6)
        self.scan_btn = ttk.Button(ctrl, text="▶ Start scanning",
                                    command=self._toggle_scan)
        self.scan_btn.pack(side="left")
        ttk.Button(ctrl, text="Clear", command=self._clear
                  ).pack(side="left", padx=6)
        # Show-all toggle: when on, accept non-DUSQ adverts too and render
        # them with `—` in all decoded columns. Useful to confirm the BLE
        # stack is alive when the target isn't appearing.
        self.show_all = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="Show all (debug)",
                        variable=self.show_all,
                        command=self._refresh_tree).pack(side="left", padx=8)
        self.scan_status_var = tk.StringVar(
            value="Idle. Press Start scanning to begin.")
        ttk.Label(ctrl, textvariable=self.scan_status_var,
                  foreground="gray").pack(side="left", padx=10)

        # --- Device table ---------------------------------------------------
        cols = ("name", "address", "rssi", "state", "usb", "charging", "lid",
                "auth", "batt", "blocks", "journal", "last_sync", "next_buzz",
                "ficr", "seen")
        self.tree = ttk.Treeview(self, columns=cols, show="headings",
                                  selectmode="browse", height=12)
        for col, label, width in [
            ("name",      "Name",         150),
            ("address",   "MAC",          160),
            ("rssi",      "RSSI",         60),
            ("state",     "State",        110),
            ("usb",       "USB",          50),
            ("charging",  "Boost EN",     70),
            ("lid",       "Hall",         60),
            ("auth",      "Auth",         50),
            ("batt",      "Batt %",       60),
            ("blocks",    "Blocks",       60),
            ("journal",   "Journal",      60),
            ("last_sync", "Last flash sync", 140),
            ("next_buzz", "Next buzz",    90),
            ("ficr",      "FICR",         150),
            ("seen",      "Seen (s ago)", 80),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        # Highlight DUSQ targets so they pop out of a long Show-all list.
        self.tree.tag_configure("target", background="#8FA97A",
                                 foreground="black")
        self.tree.pack(fill="both", expand=True, padx=6, pady=4)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        # Double-click any row → connect immediately.
        self.tree.bind("<Double-1>", self._on_row_double_click)

        # --- Connection log (ported from old ConnectionTab) ----------------
        lf2 = ttk.LabelFrame(self, text="Connection log", padding=4)
        lf2.pack(fill="both", expand=False, padx=6, pady=(0, 6))
        self.log_box = scrolledtext.ScrolledText(lf2, height=6, state="disabled",
                                                  font=("Courier", 9))
        self.log_box.pack(fill="both", expand=True)

        # Periodic redraw for "Seen N s ago" so silent devices visibly age
        self.after(self.REFRESH_MS, self._redraw_periodic)
        # Auto-start scanning so the user sees devices the moment they
        # open the app — no need to click Start.
        self.after(200, self._start_scan)

    # ------------------------------------------------------------------ public
    def stop(self):
        """Called on app close — shut down the scanner cleanly."""
        if self._scanning:
            self._scanning = False
            if self._scanner:
                run_async(self._scanner.stop())

    # ---------------------------------------------------------------- internal
    def _toggle_scan(self):
        if self._scanning:
            self._stop_scan()
        else:
            self._start_scan()

    def _start_scan(self):
        self._scanning = True
        self.scan_btn.configure(text="■ Stop scanning")
        self.scan_status_var.set("Scanning… (passive, no connection)")
        self._log("Scan started (continuous, passive).")
        run_async(self._scan_loop())

    def _stop_scan(self):
        self._scanning = False
        self.scan_btn.configure(text="▶ Start scanning")
        self.scan_status_var.set("Stopped.")
        self._log("Scan stopped.")
        if self._scanner:
            run_async(self._scanner.stop())

    async def _scan_loop(self):
        try:
            self._scanner = BleakScanner(detection_callback=self._on_adv)
            await self._scanner.start()
            while self._scanning:
                await asyncio.sleep(0.5)
            await self._scanner.stop()
        except Exception as e:
            self.scan_status_var.set(f"Scan error: {e}")
            self._log(f"Scan error: {e}")
            self._scanning = False
            self.scan_btn.configure(text="▶ Start scanning")

    def _on_adv(self, device, adv):
        """Detection callback. Targets are matched by NAME only — company ID
        0xFFFF is shared by many non-production devices, so a decodable MSD is
        NOT a reliable filter (it matched foreign advertisers and decoded their
        bytes as garbage). Non-DUSQ devices are captured only when Show-all is
        enabled, with decoded=None."""
        name = (device.name or adv.local_name or "").strip() or "(no name)"
        is_target = (name != "(no name)" and "dusq" in name.lower())
        # Show-all gate: non-targets only enter the cache when enabled, so
        # toggling it off later just hides them without re-scanning.
        if not is_target and not self.show_all.get():
            return
        mfd = adv.manufacturer_data or {}
        payload = mfd.get(ADV_MSD_COMPANY_ID)
        # Decode only for our devices — a foreign 0xFFFF MSD would be garbage.
        decoded = decode_dusq_msd(payload) if (payload and is_target) else None
        # Bleak occasionally publishes a new transient `device` instance for
        # the same address; we hold the latest reference for connect().
        self._devices[device.address] = {
            "name":      name,
            "device":    device,
            "rssi":      adv.rssi if adv.rssi is not None else -999,
            "raw":       bytes(payload) if payload else b"",
            "decoded":   decoded,
            "is_target": is_target,
            "seen":      datetime.now(),
        }
        self.after(0, self._refresh_tree)

    def _refresh_tree(self):
        # Preserve selection across rebuild — keyed by MAC (column index 1
        # now that Name is the first column).
        sel = self.tree.selection()
        selected_addr = self.tree.item(sel[0], "values")[1] if sel else None
        for row in self.tree.get_children():
            self.tree.delete(row)
        # Apply Show-all filter at render time so toggling the box doesn't
        # require a re-scan.
        rows = [(a, i) for a, i in self._devices.items()
                if self.show_all.get() or i.get("is_target")]
        # Sort: targets first, then by RSSI desc. Negative key = strongest signal.
        rows.sort(key=lambda kv: (not kv[1].get("is_target"), -kv[1]["rssi"]))
        now = datetime.now()
        target_count = 0
        for addr, info in rows:
            d = info["decoded"]
            secs_ago = int((now - info["seen"]).total_seconds())
            name = info.get("name", "(no name)")
            if info.get("is_target"):
                target_count += 1
            if d:
                last_sync_disp = ("never" if d["last_sync"] == 0
                                   else datetime.fromtimestamp(d["last_sync"])
                                        .strftime("%Y-%m-%d %H:%M:%S"))
                values = (
                    name, addr,
                    f"{info['rssi']} dBm",
                    d["state"],
                    "yes" if d["usb"] else "no",
                    "yes" if d["charging"] else "no",
                    "closed" if d["lid_closed"] else "open",
                    "yes" if d["authed"] else "no",
                    "—" if d["batt_pct"] is None else f"{d['batt_pct']}",
                    d["blocks"],
                    d["journal"],
                    last_sync_disp,
                    fmt_next_buzz(d["haptic_secs"], d["haptic_is_epoch"]),
                    d["ficr"],
                    f"{secs_ago}s",
                )
            else:
                # Either: DUSQ-named device without MSD payload (old firmware),
                # or a non-DUSQ device shown in Show-all mode.
                values = (name, addr, f"{info['rssi']} dBm",
                          "—", "—", "—", "—", "—", "—", "—", "—", "—", "—", "—",
                          f"{secs_ago}s")
            row_tags = ("target",) if info.get("is_target") else ()
            iid = self.tree.insert("", "end", values=values, tags=row_tags)
            if addr == selected_addr:
                self.tree.selection_set(iid)
        # Live counter so the user can see Show-all is doing something.
        self.scan_status_var.set(
            f"{'Scanning' if self._scanning else 'Stopped'} · "
            f"{target_count} target(s) · {len(self._devices)} total"
            + ("  (showing all)" if self.show_all.get() else ""))

    def _redraw_periodic(self):
        if self._devices:
            self._refresh_tree()
        self.after(self.REFRESH_MS, self._redraw_periodic)

    def _on_select(self, _evt):
        # Row selection no longer drives any side-panel since Raw MSD was
        # removed.  Kept as a no-op binding so future details can hang here.
        pass

    def _clear(self):
        self._devices.clear()
        for row in self.tree.get_children():
            self.tree.delete(row)

    # ----------------------------------------------------------- connect log
    def _log(self, msg: str):
        """BLEManager log callback + internal status — drops into the tab's
        scrolledtext log panel."""
        try:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"[{ts()}] {msg}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        except Exception:
            pass

    # ----------------------------------------------------------- connect flow
    def _on_row_double_click(self, _evt):
        """Treeview <Double-1> binding — connect to whichever row was just clicked."""
        sel = self.tree.selection()
        if not sel:
            return
        # MAC is column 1
        row_values = self.tree.item(sel[0], "values")
        if len(row_values) < 2:
            return
        addr = row_values[1]
        self._do_connect(addr)

    def _do_connect(self, addr: str):
        info = self._devices.get(addr)
        if info is None:
            messagebox.showerror("Connect",
                                  f"Could not find {addr} in scan results.")
            return
        dev  = info["device"]
        name = info.get("name", addr)
        async def _connect():
            self.status_var.set(f"Connecting to {name}…")
            self._log(f"Connecting to {name}  {addr}…")
            try:
                await self.ble.connect(dev)
                self.status_var.set(f"Connected: {name}")
                self._log("Ready — authenticate via Auth tab.")
            except Exception as e:
                self._log(f"Connect failed: {e}")
                self.status_var.set("Not connected")
        run_async(_connect())

    def _on_disconnected(self):
        """BLEManager on_disconnect callback (chained via App)."""
        self.status_var.set("Not connected")
        self._log("Connection lost.")
