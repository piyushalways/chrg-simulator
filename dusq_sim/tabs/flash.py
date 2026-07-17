"""flash tab."""

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

class FlashTab(ttk.Frame):
    """Sensor block download + decode. (Journal was promoted to its own tab.)"""

    def __init__(self, parent, ble: BLEManager):
        super().__init__(parent)
        self.ble = ble

        # Sensor blocks state
        self._blocks: List[dict] = []
        self._dl_task: Optional[asyncio.Task] = None
        self._dl_idle_evt: Optional[asyncio.Event] = None
        # Running counters — "this download" resets every time the user
        # clicks Download; "this connection" resets only on disconnect.
        self._blk_session_count = 0
        self._blk_connection_count = 0

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=4, pady=4)

        self._build_blocks_page(nb)

    # ── Sensor blocks page ───────────────────────────────────────────────────

    def _build_blocks_page(self, parent_nb: ttk.Notebook):
        page = ttk.Frame(parent_nb)
        parent_nb.add(page, text="Sensor Blocks")

        # Controls + counters
        ctrl = ttk.Frame(page)
        ctrl.pack(fill="x", padx=6, pady=4)
        ttk.Button(ctrl, text="Get Block Count",
                   command=self._do_count).pack(side="left", padx=4)
        self.dl_btn = ttk.Button(ctrl, text="Download Blocks",
                                  command=self._do_download)
        self.dl_btn.pack(side="left", padx=4)
        self.dl_cancel_btn = ttk.Button(ctrl, text="Cancel",
                                         command=self._cancel_download,
                                         state="disabled")
        self.dl_cancel_btn.pack(side="left", padx=4)
        ttk.Button(ctrl, text="Send Time Sync",
                   command=self._do_time_sync).pack(side="left", padx=4)
        ttk.Button(ctrl, text="Clear List",
                   command=self._do_clear_blocks).pack(side="left", padx=4)

        self.count_lbl = ttk.Label(
            ctrl,
            text="On device: ?   |   This download: 0   |   This connection: 0")
        self.count_lbl.pack(side="left", padx=12)

        # Block list
        lf = ttk.LabelFrame(page, text="Downloaded Blocks", padding=4)
        lf.pack(fill="both", expand=True, padx=6, pady=2)

        # "#" is a running index in this list — first row received in the
        # current connection = 1, increments per row.  Distinct from device
        # "seq" which is the firmware's block sequence number.
        cols = ("idx", "seq", "timestamp", "sync", "readings", "crc")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings", height=7)
        for col, w, hdr, anchor in [
            ("idx",       50,  "#",         "center"),
            ("seq",       60,  "Seq",       "center"),
            ("timestamp", 200, "Timestamp", "w"),
            ("sync",      70,  "Sync",      "center"),
            ("readings",  70,  "Readings",  "center"),
            ("crc",       60,  "CRC",       "center"),
        ]:
            self.tree.heading(col, text=hdr)
            self.tree.column(col, width=w, anchor=anchor)
        # Visual cue on bad-CRC rows.
        self.tree.tag_configure("bad", background="#fadbd8", foreground="black")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_block_select)

        # Detail pane
        df = ttk.LabelFrame(page, text="Block Detail (select a row above)",
                             padding=4)
        df.pack(fill="both", expand=True, padx=6, pady=4)
        self.detail_box = scrolledtext.ScrolledText(df, height=12,
                                                     state="disabled",
                                                     font=("Courier", 9))
        self.detail_box.pack(fill="both", expand=True)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _set_box(self, box: scrolledtext.ScrolledText, text: str):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("end", text)
        box.configure(state="disabled")

    def _hex_dump(self, data: bytes, width: int = 16) -> str:
        out = []
        for i in range(0, len(data), width):
            chunk = data[i:i + width]
            hexs = " ".join(f"{b:02x}" for b in chunk)
            out.append(f"  {i:04x}  {hexs:<{width*3}}")
        return "\n".join(out)

    # ── Sensor block actions ─────────────────────────────────────────────────

    def _do_count(self):
        if not self.ble.connected:
            messagebox.showwarning("Flash", "Not connected.")
            return
        async def _count():
            try:
                raw = await self.ble.read(CHAR_BLOCK_COUNT)
                n = struct.unpack("<H", raw)[0]
                self.count_lbl.configure(
                    text=f"On device: {n}   |   "
                         f"This download: {self._blk_session_count}   |   "
                         f"This connection: {self._blk_connection_count}")
            except Exception as e:
                messagebox.showerror("Count", str(e))
        run_async(_count())

    def _do_time_sync(self):
        if not self.ble.connected:
            messagebox.showwarning("Flash", "Not connected.")
            return
        epoch = int(time.time()) + IST_OFFSET_S   # device runs on IST (UTC+05:30)
        async def _sync():
            try:
                await self.ble.write(CHAR_TIMESYNC, struct.pack("<I", epoch))
                wall = datetime.fromtimestamp(epoch, tz=timezone.utc)   # IST-biased -> renders IST
                messagebox.showinfo("Time Sync",
                                     f"Sent epoch {epoch} "
                                     f"({wall:%Y-%m-%d %H:%M:%S} IST)")
            except Exception as e:
                messagebox.showerror("Time Sync", str(e))
        run_async(_sync())

    def _do_clear_blocks(self):
        self._blocks.clear()
        self._blk_session_count = 0
        self._blk_connection_count = 0
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._set_box(self.detail_box, "")
        self.count_lbl.configure(
            text="On device: ?   |   This download: 0   |   This connection: 0")

    def _do_download(self):
        """Subscribe to notify FIRST, then write 0x01 trigger.
        Firmware streams full 152-byte blocks back on the same UUID.
        Completion is detected via DOWNLOAD_IDLE_TIMEOUT seconds of silence."""
        if not self.ble.connected:
            messagebox.showwarning("Flash", "Not connected.")
            return
        # Reset the per-download counter; per-connection counter stays.
        self._blk_session_count = 0
        if self._dl_task and not self._dl_task.done():
            return  # already running

        self._dl_idle_evt = asyncio.Event()
        buf = bytearray()
        loop = asyncio.get_event_loop()

        def _on_record(_handle, data: bytearray):
            buf.extend(data)
            while len(buf) >= BLOCK_SIZE:
                raw = bytes(buf[:BLOCK_SIZE])
                del buf[:BLOCK_SIZE]
                blk = decode_block(raw)
                if blk is None:
                    continue
                self._blocks.append(blk)
                self._blk_session_count += 1
                self._blk_connection_count += 1
                tag = "" if blk["crc_ok"] else "bad"
                self.tree.insert("", "end",
                                 values=(self._blk_connection_count,
                                         blk["sequence"], blk["timestamp"],
                                         "post" if blk["sync_status"] else "pre",
                                         blk["reading_count"],
                                         "OK" if blk["crc_ok"] else "FAIL"),
                                 tags=(tag,) if tag else ())
                self.count_lbl.configure(
                    text=f"On device: ?   |   "
                         f"This download: {self._blk_session_count}   |   "
                         f"This connection: {self._blk_connection_count}")
            # Reset the silence timer on every chunk; thread-safe call.
            loop.call_soon_threadsafe(self._dl_idle_evt.set)

        async def _dl():
            self.dl_btn.configure(state="disabled")
            self.dl_cancel_btn.configure(state="normal")
            try:
                await self.ble.subscribe(CHAR_RECORD, _on_record)
                await self.ble.write(CHAR_RECORD, bytes([0x01]))
                # Loop: wait up to DOWNLOAD_IDLE_TIMEOUT for a notify; if none
                # arrives in that window, transfer is done. The first wait is
                # a bit longer because the device may need a moment to start.
                first = True
                while True:
                    self._dl_idle_evt.clear()
                    timeout = 15.0 if first else DOWNLOAD_IDLE_TIMEOUT
                    try:
                        await asyncio.wait_for(self._dl_idle_evt.wait(),
                                                timeout=timeout)
                    except asyncio.TimeoutError:
                        break
                    first = False
            except asyncio.CancelledError:
                pass
            except Exception as e:
                messagebox.showerror("Download", str(e))
            finally:
                try:
                    await self.ble.unsubscribe(CHAR_RECORD)
                except Exception:
                    pass
                self.dl_btn.configure(state="normal")
                self.dl_cancel_btn.configure(state="disabled")

        self._dl_task = run_async(_dl())

    def _cancel_download(self):
        if self._dl_task and not self._dl_task.done():
            self._dl_task.cancel()

    def _on_block_select(self, _evt):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if idx >= len(self._blocks):
            return
        blk = self._blocks[idx]
        crc_mark = "OK" if blk["crc_ok"] else "FAIL"
        sync_str = "post-sync" if blk["sync_status"] else "pre-sync"

        lines = []
        lines.append("─── Header ─────────────────────────────────────────────")
        lines.append(f"  magic        : 0xDEADBEEF (✓)")
        lines.append(f"  sequence     : {blk['sequence']}")
        lines.append(f"  timestamp    : {blk['timestamp']}")
        lines.append(f"  sync_status  : {blk['sync_status']}  ({sync_str})")
        lines.append(f"  reading_count: {blk['reading_count']} / {READINGS_PER_BLOCK}")
        lines.append(f"  crc32 stored : 0x{blk['crc_stored']:08X}")
        lines.append(f"  crc32 host   : 0x{blk['crc_computed']:08X}  → {crc_mark}")
        lines.append("")
        lines.append("─── Payload (30 readings, ERR = sensor error marker) ───")
        lines.append(f"  {'#':>3}  {'Temp(°C)':>10}  {'Avg dB':>8}  {'Lux':>8}")
        for i, r in enumerate(blk["readings"]):
            t = "  ERR" if r["temp"] is None else f"{r['temp']:>+5}°C"
            d = " ERR"  if r["db"]   is None else f"{r['db']:>3} dB"
            l = "   ERR" if r["lux"]  is None else f"{r['lux']:>6}"
            lines.append(f"  {i+1:>3}  {t:>10}  {d:>8}  {l:>8}")
        lines.append("")
        lines.append("─── Raw 152 bytes ──────────────────────────────────────")
        lines.append(self._hex_dump(blk["raw"]))
        self._set_box(self.detail_box, "\n".join(lines))

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _on_disconnected(self):
        """Cancel any in-flight download and clear the Treeview + detail."""
        if self._dl_task and not self._dl_task.done():
            self._dl_task.cancel()
        self._do_clear_blocks()
