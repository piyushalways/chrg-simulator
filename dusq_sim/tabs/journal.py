"""Journal tab — standalone event-log download (promoted out of the Flash tab).

Streams 32-byte JournalEntries from the device (subscribe CHAR_JOURNAL_RECORD,
then write 0x01 to CHAR_JOURNAL_START) and renders them with human-readable
timestamps.  The raw epoch/boot-seconds value is kept in a secondary column and
in the detail pane.
"""

import asyncio
import struct
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import List, Optional

from ..constants import *
from ..decoders import *
from ..util import *
from ..ble import BLEManager


class JournalTab(ttk.Frame):
    """Download + decode the firmware event journal (BOOT / TIME_SYNC / WRAP / ERROR / …)."""

    def __init__(self, parent, ble: BLEManager):
        super().__init__(parent)
        self.ble = ble

        self._journal: List[dict] = []
        self._jnl_task: Optional[asyncio.Task] = None
        self._jnl_idle_evt: Optional[asyncio.Event] = None
        self._jnl_session_count = 0
        self._jnl_connection_count = 0

        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=6, pady=4)
        ttk.Button(ctrl, text="Get Journal Count",
                   command=self._do_journal_count).pack(side="left", padx=4)
        self.jnl_dl_btn = ttk.Button(ctrl, text="Download Journal",
                                      command=self._do_journal_download)
        self.jnl_dl_btn.pack(side="left", padx=4)
        self.jnl_cancel_btn = ttk.Button(ctrl, text="Cancel",
                                          command=self._cancel_journal,
                                          state="disabled")
        self.jnl_cancel_btn.pack(side="left", padx=4)
        ttk.Button(ctrl, text="Clear List",
                   command=self._do_clear_journal).pack(side="left", padx=4)

        self.jnl_count_lbl = ttk.Label(
            ctrl,
            text="On device: ?   |   This download: 0   |   This connection: 0")
        self.jnl_count_lbl.pack(side="left", padx=12)

        lf = ttk.LabelFrame(self, text="Journal Entries", padding=4)
        lf.pack(fill="both", expand=True, padx=6, pady=2)

        # "When" = human-readable timestamp; "Raw" keeps the underlying epoch /
        # boot_seconds int the firmware actually stored (req: keep raw available).
        cols = ("idx", "seq", "type", "when", "raw", "data", "crc")
        self.jnl_tree = ttk.Treeview(lf, columns=cols, show="headings",
                                      height=8)
        for col, w, hdr, anchor in [
            ("idx",   50,  "#",         "center"),
            ("seq",   55,  "Seq",       "center"),
            ("type",  110, "Event",     "w"),
            ("when",  160, "When",      "w"),
            ("raw",   100, "Raw ts",    "center"),
            ("data",  220, "Decoded",   "w"),
            ("crc",   55,  "CRC",       "center"),
        ]:
            self.jnl_tree.heading(col, text=hdr)
            self.jnl_tree.column(col, width=w, anchor=anchor)
        self.jnl_tree.tag_configure("bad", background="#fadbd8",
                                     foreground="black")
        sb = ttk.Scrollbar(lf, orient="vertical",
                           command=self.jnl_tree.yview)
        self.jnl_tree.configure(yscrollcommand=sb.set)
        self.jnl_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.jnl_tree.bind("<<TreeviewSelect>>", self._on_journal_select)

        df = ttk.LabelFrame(self, text="Entry Detail (select a row above)",
                             padding=4)
        df.pack(fill="both", expand=True, padx=6, pady=4)
        self.jnl_detail_box = scrolledtext.ScrolledText(df, height=10,
                                                         state="disabled",
                                                         font=("Courier", 9))
        self.jnl_detail_box.pack(fill="both", expand=True)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _set_box(self, box: scrolledtext.ScrolledText, text: str):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("end", text)
        box.configure(state="disabled")

    # ── actions ──────────────────────────────────────────────────────────────

    def _do_journal_count(self):
        if not self.ble.connected:
            messagebox.showwarning("Journal", "Not connected.")
            return
        async def _count():
            try:
                raw = await self.ble.read(CHAR_JOURNAL_COUNT)
                n = struct.unpack("<H", raw)[0]
                self.jnl_count_lbl.configure(
                    text=f"On device: {n}   |   "
                         f"This download: {self._jnl_session_count}   |   "
                         f"This connection: {self._jnl_connection_count}")
            except Exception as e:
                messagebox.showerror("Journal Count", str(e))
        run_async(_count())

    def _do_clear_journal(self):
        self._journal.clear()
        self._jnl_session_count = 0
        self._jnl_connection_count = 0
        for row in self.jnl_tree.get_children():
            self.jnl_tree.delete(row)
        self._set_box(self.jnl_detail_box, "")
        self.jnl_count_lbl.configure(
            text="On device: ?   |   This download: 0   |   This connection: 0")

    def _do_journal_download(self):
        """Subscribe to JOURNAL_RECORD (notify-only), THEN write 0x01 to
        JOURNAL_START. Firmware streams 32-byte JournalEntries on RECORD."""
        if not self.ble.connected:
            messagebox.showwarning("Journal", "Not connected.")
            return
        # Reset per-download counter; per-connection counter stays.
        self._jnl_session_count = 0
        if self._jnl_task and not self._jnl_task.done():
            return

        self._jnl_idle_evt = asyncio.Event()
        buf = bytearray()
        loop = asyncio.get_event_loop()

        def _on_jnl(_handle, data: bytearray):
            buf.extend(data)
            while len(buf) >= JOURNAL_ENTRY_SIZE:
                raw = bytes(buf[:JOURNAL_ENTRY_SIZE])
                del buf[:JOURNAL_ENTRY_SIZE]
                e = decode_journal_entry(raw)
                if e is None:
                    continue
                self._journal.append(e)
                self._jnl_session_count += 1
                self._jnl_connection_count += 1
                tag = "" if e["crc_ok"] else "bad"
                self.jnl_tree.insert(
                    "", "end",
                    values=(self._jnl_connection_count,
                            e["sequence"], e["type_str"],
                            journal_timestamp_str(e), e["timestamp"],
                            format_event_data(e["type_raw"], e["data"]),
                            "OK" if e["crc_ok"] else "FAIL"),
                    tags=(tag,) if tag else (),
                )
                self.jnl_count_lbl.configure(
                    text=f"On device: ?   |   "
                         f"This download: {self._jnl_session_count}   |   "
                         f"This connection: {self._jnl_connection_count}")
            loop.call_soon_threadsafe(self._jnl_idle_evt.set)

        async def _dl():
            self.jnl_dl_btn.configure(state="disabled")
            self.jnl_cancel_btn.configure(state="normal")
            try:
                # Subscribe FIRST — JOURNAL_RECORD is notify-only.
                await self.ble.subscribe(CHAR_JOURNAL_RECORD, _on_jnl)
                # Then trigger the transfer on JOURNAL_START.
                await self.ble.write(CHAR_JOURNAL_START, bytes([0x01]))
                first = True
                while True:
                    self._jnl_idle_evt.clear()
                    timeout = 15.0 if first else DOWNLOAD_IDLE_TIMEOUT
                    try:
                        await asyncio.wait_for(self._jnl_idle_evt.wait(),
                                                timeout=timeout)
                    except asyncio.TimeoutError:
                        break
                    first = False
            except asyncio.CancelledError:
                pass
            except Exception as e:
                messagebox.showerror("Journal Download", str(e))
            finally:
                try:
                    await self.ble.unsubscribe(CHAR_JOURNAL_RECORD)
                except Exception:
                    pass
                self.jnl_dl_btn.configure(state="normal")
                self.jnl_cancel_btn.configure(state="disabled")

        self._jnl_task = run_async(_dl())

    def _cancel_journal(self):
        if self._jnl_task and not self._jnl_task.done():
            self._jnl_task.cancel()

    def _on_journal_select(self, _evt):
        sel = self.jnl_tree.selection()
        if not sel:
            return
        idx = self.jnl_tree.index(sel[0])
        if idx >= len(self._journal):
            return
        e = self._journal[idx]
        crc_mark = "OK" if e["crc_ok"] else "FAIL"
        ss = e.get("sync_status", 0xFF)
        sync_str = {0: "pre-sync (boot_seconds)",
                    1: "post-sync (epoch)"}.get(ss, f"legacy/unknown (0x{ss:02X})")

        lines = []
        lines.append("─── Header ─────────────────────────────────────────────")
        lines.append(f"  magic        : 0xCAFEBABE (✓)")
        lines.append(f"  sequence     : {e['sequence']}")
        lines.append(f"  when         : {journal_timestamp_str(e)}")
        lines.append(f"  raw timestamp: {e['timestamp']}  ({sync_str})")
        lines.append(f"  type         : {e['type_raw']}  ({e['type_str']})")
        lines.append(f"  data         : 0x{e['data']:08X}  ({e['data']})")
        lines.append(f"  decoded      : {format_event_data(e['type_raw'], e['data'])}")
        lines.append(f"  crc32 stored : 0x{e['crc_stored']:08X}")
        lines.append(f"  crc32 host   : 0x{e['crc_computed']:08X}  → {crc_mark}")
        lines.append("")
        lines.append("─── Raw 32 bytes ───────────────────────────────────────")
        lines.append(hex_dump(e["raw"]))
        self._set_box(self.jnl_detail_box, "\n".join(lines))

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _on_disconnected(self):
        """Cancel any in-flight download and clear the Treeview + detail."""
        if self._jnl_task and not self._jnl_task.done():
            self._jnl_task.cancel()
        self._do_clear_journal()
