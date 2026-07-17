"""device_info tab."""

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

class DeviceInfoTab(ttk.Frame):
    """Displays the BLE Device Information Service characteristics that the
    firmware exposes:
       Manufacturer / Model / HW Rev / FW Rev / Serial Number (UICR) / System ID (FICR).

    Read once on connect (auto), and on demand via the Read Now button.

    Two identity sources are exposed:
      - Serial Number (0x2A25)   ←  UICR.CUSTOMER[0..3]  (written at factory,
                                    customer-facing, e.g. "DUSQ2620A0000001")
      - System ID    (0x2A23)   ←  FICR.DEVICEID + Nordic OUI (immutable chip
                                    identity, anti-tamper anchor)"""

    PLACEHOLDER = "—"

    # Display order: identity at top, revision/model after.
    # 4th tuple element is a decoder callback (bytes → display string); when
    # None, raw bytes are decoded as UTF-8 (the default for DIS strings).
    _FIELDS = [
        ("Manufacturer:",        "manufacturer", CHAR_DIS_MANUFACTURER, None),
        ("Model:",               "model",        CHAR_DIS_MODEL,        None),
        ("Hardware Rev:",        "hw_rev",       CHAR_DIS_HW_REV,       None),
        ("Firmware Rev:",        "fw_rev",       CHAR_DIS_FW_REV,       None),
        ("Serial Number (UICR):", "serial",      CHAR_DIS_SERIAL,       None),
        ("System ID (FICR):",    "system_id",    CHAR_DIS_SYSTEM_ID,    "_decode_system_id"),
    ]

    @staticmethod
    def _decode_system_id(raw: bytes) -> str:
        """Decode the 8-byte System ID = the full 64-bit FICR DEVICEID.
            bytes 0..3 = DEVICEID[0] (LE), bytes 4..7 = DEVICEID[1] (LE).
        Matches the FICR advertised in the scan-response MSD byte-for-byte."""
        if len(raw) < 8:
            return f"<short read: {raw.hex(' ').upper()}>"
        id0 = int.from_bytes(raw[0:4], "little")
        id1 = int.from_bytes(raw[4:8], "little")
        return f"FICR={id1:08X}{id0:08X}  ({raw.hex(' ').upper()})"

    def __init__(self, parent, ble: BLEManager):
        super().__init__(parent)
        self.ble = ble

        frm = ttk.LabelFrame(self, text="Device Information", padding=10)
        frm.pack(fill="x", padx=6, pady=6)

        # One Label + StringVar-driven value Label per field, in a 2-column grid.
        self._vars: Dict[str, tk.StringVar] = {}
        for row, (label, key, _uuid, _dec) in enumerate(self._FIELDS):
            ttk.Label(frm, text=label, width=22, anchor="w"
                     ).grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=self.PLACEHOLDER)
            self._vars[key] = var
            ttk.Label(frm, textvariable=var, font=("Courier", 9),
                      foreground="#1b3a8c"
                     ).grid(row=row, column=1, sticky="w", padx=6, pady=2)

        # Read Now button — manual refresh
        btn_row = len(self._FIELDS)
        self.read_btn = ttk.Button(frm, text="Read Now", command=self._do_read)
        self.read_btn.grid(row=btn_row, column=1, sticky="e", pady=(8, 0))

        # Tip / status line
        self.status_lbl = ttk.Label(frm, text="Connect & authenticate first.",
                                     foreground="gray")
        self.status_lbl.grid(row=btn_row + 1, column=0, columnspan=2,
                              sticky="w", pady=(4, 0))

    # ------------------------------------------------------------------ public

    def refresh(self):
        """Async fire-and-forget: read all 5 chars, populate fields.
        Called automatically from auth_tab.on_auth_attempt and from the button."""
        if not self.ble.connected:
            return
        run_async(self._read_all_async())

    # ----------------------------------------------------------------- private

    def _do_read(self):
        if not self.ble.connected:
            messagebox.showwarning("Device Info", "Not connected.")
            return
        self.refresh()

    async def _read_all_async(self):
        self.status_lbl.configure(text="Reading…", foreground="gray")
        ok_count = 0
        for _, key, uuid, decoder in self._FIELDS:
            try:
                raw = bytes(await self.ble.read(uuid))
                if decoder is None:
                    self._vars[key].set(raw.decode("utf-8", errors="replace").strip())
                else:
                    fn = getattr(self, decoder)
                    self._vars[key].set(fn(raw))
                ok_count += 1
            except Exception as e:
                self._vars[key].set(f"<error: {e}>")
        if ok_count == len(self._FIELDS):
            self.status_lbl.configure(
                text=f"Last read OK ({ok_count}/{len(self._FIELDS)} chars).",
                foreground="#1b8c3a")
        else:
            self.status_lbl.configure(
                text=f"Read partial: {ok_count}/{len(self._FIELDS)} chars.",
                foreground="#c0392b")

    def _on_disconnected(self):
        for var in self._vars.values():
            var.set(self.PLACEHOLDER)
        self.status_lbl.configure(text="Disconnected.", foreground="gray")
