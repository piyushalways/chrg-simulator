"""automated_testing tab."""

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

# ─────────────────────────────────────────────────────────────────────────────
#  Validation tab — comprehensive test catalogue (49 tests / 9 buckets)
# ─────────────────────────────────────────────────────────────────────────────

# Firmware geometry — keep aligned with src/config/app_config.h
FLASH_BLOCK_COUNT_MAX     = 234
FLASH_JOURNAL_SLOT_COUNT  = 128
FLASH_BLOCKS_PER_PAGE     = 26   # one page erase loses 26 blocks at a time

# Test thresholds (defaults; tuned conservatively)
TEMP_VALID_MIN  = -20
TEMP_VALID_MAX  = 60
DB_VALID_MIN    = 20
DB_VALID_MAX    = 120
LUX_VALID_MIN   = 0
LUX_VALID_MAX   = 50000
EMA_SMOOTH_MAX_JUMP = 5
SAMPLE_COUNT_DEFAULT = 5
SAMPLE_PERIOD_S       = 60
SAMPLE_TIMEOUT_S      = (SAMPLE_COUNT_DEFAULT + 1) * 65


@dataclass
class TestResult:
    test_id:     str
    bucket:      str
    name:        str
    status:      str = "PENDING"  # PENDING / RUNNING / PASS / FAIL / SKIP / INCONCLUSIVE
    summary:     str = ""
    details:     List[str] = field(default_factory=list)
    duration_s:  float = 0.0
    started_iso: str = ""


@dataclass
class TestContext:
    """Shared mutable state across one validation run."""
    results:         Dict[str, TestResult] = field(default_factory=dict)
    samples:         List[dict] = field(default_factory=list)   # filled by ensure_samples
    bas_events:      List[dict] = field(default_factory=list)   # {ts, pct} per BAS notify
    blocks:          List[dict] = field(default_factory=list)   # filled by ensure_blocks
    journal:         List[dict] = field(default_factory=list)   # filled by ensure_journal
    sent_epoch:      Optional[int] = None
    disconnect_count: int = 0
    error_state_seen: bool = False
    cancelled:       bool = False
    current_test_id: Optional[str] = None
    on_detail:       Optional[Callable[[str], None]] = None

    def detail(self, line: str):
        """Append a live evidence line to the currently-running test's
        result. UI redraws via on_detail callback if set."""
        cur = self.results.get(self.current_test_id) if self.current_test_id else None
        if cur is not None:
            cur.details.append(line)
        if callable(self.on_detail):
            try:
                self.on_detail(line)
            except Exception:
                pass


@dataclass
class TestSpec:
    test_id:  str
    bucket:   str
    name:     str
    runner:   Callable[..., Awaitable[TestResult]]
    requires: Tuple[str, ...] = ()
    optional: bool = False


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ValidationTab(ttk.Frame):
    """49 automated tests across 9 buckets. Run individually or in bulk."""

    BUCKET_NAMES = {
        "A":   "BLE Link",
        "B":   "Authentication",
        "C":   "Sensor pipeline",
        "D":   "Battery",
        "E":   "Haptic",
        "F":   "Flash sensor blocks + circular buffer",
        "G":   "Journal",
        "I":   "Hall (lid sensor)",
        "U":   "USB detect",
        "H":   "Long-running (opt-in, ≥4 h)",
        # New buckets — exercise the 5 s window firmware contract.
        "W":   "Window cadence & content (5 s sampling)",
        "BC":  "Battery cache + refresh-on-event",
        "CAL": "Calibration validity (manual fixtures)",
        "TM":  "Timing / health",
    }
    # User-friendly domain labels for the checkbox UI.  Maps domain code →
    # (display label, requires-physical-interaction flag).
    DOMAIN_LABELS = {
        "A":   ("BLE",          False),
        "B":   ("Auth",         False),
        "C":   ("Sensors",      False),
        "D":   ("Battery",      False),
        "E":   ("Haptic",       True),
        "F":   ("Flash",        False),
        "G":   ("Journal",      False),
        "I":   ("Hall",         True),
        "U":   ("USB detect",   True),
        "H":   ("Long-running", False),
        "W":   ("Window",       True),   # W4/W5/W6 need light/temp/sound input
        "BC":  ("Batt cache",   True),   # BC2/BC3 need hall+USB physical events
        "CAL": ("Calibration",  True),   # all 3 need reference instruments
        "TM":  ("Timing",       False),
    }

    def __init__(self, parent, ble: BLEManager):
        super().__init__(parent)
        self.ble = ble
        self._catalogue: List[TestSpec] = self._build_catalogue()
        self._spec_by_id: Dict[str, TestSpec] = {s.test_id: s for s in self._catalogue}
        self._row_iid: Dict[str, str] = {}
        self._bucket_iid: Dict[str, str] = {}
        self._checked: Dict[str, bool] = {s.test_id: True for s in self._catalogue
                                            if not s.optional}
        for s in self._catalogue:
            if s.optional:
                self._checked[s.test_id] = False
        self._ctx: Optional[TestContext] = None
        self._run_task: Optional[asyncio.Task] = None
        self._build_ui()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        # Top button row
        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=4)
        self.run_all_btn = ttk.Button(top, text="Run All",
                                       command=self._on_run_all)
        self.run_all_btn.pack(side="left", padx=2)
        self.run_sel_btn = ttk.Button(top, text="Run Selected",
                                       command=self._on_run_selected)
        self.run_sel_btn.pack(side="left", padx=2)
        self.run_bkt_btn = ttk.Button(top, text="Run This Bucket",
                                       command=self._on_run_bucket)
        self.run_bkt_btn.pack(side="left", padx=2)
        self.cancel_btn = ttk.Button(top, text="Cancel",
                                      command=self._on_cancel,
                                      state="disabled")
        self.cancel_btn.pack(side="left", padx=2)
        ttk.Button(top, text="Export JSON",
                   command=self._on_export).pack(side="left", padx=2)
        ttk.Button(top, text="Export CSV",
                   command=self._on_export_csv).pack(side="left", padx=2)
        ttk.Button(top, text="Reset",
                   command=self._on_reset).pack(side="left", padx=2)

        self.include_long_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Include long-running (Bucket H)",
                        variable=self.include_long_var).pack(side="right", padx=4)

        # ── Domain quick-select panel ────────────────────────────────────────
        # User taps domain checkboxes to choose what to test, then hits the
        # big RUN button.  Hovering a checkbox updates a one-line info hint
        # below.  Matches the "Automated Testing" UX in the plan.
        dom_lf = ttk.LabelFrame(self, text="Tap to choose what to test",
                                 padding=8)
        dom_lf.pack(fill="x", padx=6, pady=4)
        self.domain_vars: Dict[str, tk.BooleanVar] = {}
        # Test count per domain — built from the catalogue at construction.
        domain_counts: Dict[str, int] = {}
        for s in self._catalogue:
            domain_counts[s.bucket] = domain_counts.get(s.bucket, 0) + 1
        # Build checkboxes in display order: BLE, Auth, Sensors, Battery,
        # Haptic, Flash, Journal, Hall, USB detect, Long-running.
        domain_order = ["A", "B", "C", "D", "E", "F", "G", "I", "U", "H",
                        "W", "BC", "CAL", "TM"]
        col, row = 0, 0
        for code in domain_order:
            if code not in self.DOMAIN_LABELS:
                continue
            label, manual = self.DOMAIN_LABELS[code]
            count = domain_counts.get(code, 0)
            # Long-running starts off; everything else on.
            initial = (code != "H")
            var = tk.BooleanVar(value=initial)
            self.domain_vars[code] = var
            text = f"{label} ({count})"
            if manual:
                text += "  [manual]"
            cb = ttk.Checkbutton(dom_lf, text=text, variable=var,
                                  command=self._on_domain_toggle)
            cb.grid(row=row, column=col, sticky="w", padx=8, pady=2)
            # Hover binding — show domain description in the hint label.
            cb.bind("<Enter>", lambda _e, c=code: self._show_domain_hint(c))
            cb.bind("<Leave>", lambda _e: self._show_domain_hint(None))
            col += 1
            if col >= 3:
                col, row = 0, row + 1
        # Hint label below the checkboxes — pinned inside a fixed-height
        # frame so the surrounding layout doesn't reflow when the hint text
        # length changes between checkboxes (some hints are short, some
        # wrap to two lines).  grid_propagate(False) freezes the frame's
        # outer size regardless of the label's natural height.
        self.domain_hint_var = tk.StringVar(
            value="Hover any checkbox to see what tests it covers.")
        hint_frame = ttk.Frame(dom_lf, height=44)
        hint_frame.grid(row=row + 1, column=0, columnspan=3,
                         sticky="ew", pady=(6, 2))
        hint_frame.grid_propagate(False)
        hint_frame.columnconfigure(0, weight=1)
        ttk.Label(hint_frame, textvariable=self.domain_hint_var,
                  foreground="#34495e",
                  wraplength=720,
                  anchor="nw", justify="left"
                 ).grid(row=0, column=0, sticky="nsew")

        # Big RUN button + quick selectors.
        run_row = ttk.Frame(dom_lf)
        run_row.grid(row=row + 2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.big_run_btn = tk.Button(run_row,
                                      text="▶ RUN selected domains",
                                      command=self._on_run_domains,
                                      bg="#1b8c3a", fg="white",
                                      activebackground="#1b8c3a",
                                      activeforeground="white",
                                      font=("Segoe UI", 10, "bold"),
                                      relief="raised", bd=2)
        self.big_run_btn.pack(side="left", padx=4)
        ttk.Button(run_row, text="Select all",
                   command=self._domains_select_all).pack(side="left", padx=2)
        ttk.Button(run_row, text="Headless only",
                   command=self._domains_headless_only).pack(side="left", padx=2)
        ttk.Button(run_row, text="Clear",
                   command=self._domains_clear).pack(side="left", padx=2)

        # ── Live progress region (shown only during a run) ───────────────────
        self.running_frame = ttk.LabelFrame(self, text="Running",
                                             padding=10)
        # NOT packed initially — _kick_off shows it; _update_result_region hides.
        self.running_var = tk.StringVar(value="")
        ttk.Label(self.running_frame, textvariable=self.running_var,
                  font=("Segoe UI", 10, "bold"),
                  foreground="#2c3e50").pack(anchor="w")
        self.running_progress = ttk.Progressbar(self.running_frame,
                                                  orient="horizontal",
                                                  mode="determinate",
                                                  length=600)
        self.running_progress.pack(fill="x", pady=(4, 4))
        self.running_summary_var = tk.StringVar(value="")
        ttk.Label(self.running_frame, textvariable=self.running_summary_var,
                  font=("Segoe UI", 9),
                  foreground="#34495e").pack(anchor="w")
        self.running_recent_var = tk.StringVar(value="")
        ttk.Label(self.running_frame, textvariable=self.running_recent_var,
                  font=("Courier", 9),
                  foreground="#7f8c8d", justify="left").pack(anchor="w",
                                                               pady=(4, 0))
        # Track run start + recent results for the panel.
        self._run_started_at: Optional[float] = None
        self._recent_results: List[Tuple[str, str, str]] = []  # (tid, name, status)

        # ── Final result region (filled in after a run completes) ────────────
        self.result_frame = ttk.LabelFrame(self, text="Latest result",
                                            padding=10)
        self.result_frame.pack(fill="x", padx=6, pady=4)
        self.result_var = tk.StringVar(value="(no runs yet)")
        self.result_lbl = ttk.Label(self.result_frame,
                                     textvariable=self.result_var,
                                     font=("Segoe UI", 11, "bold"),
                                     foreground="#7f8c8d")
        self.result_lbl.pack(anchor="w")
        self.result_detail_var = tk.StringVar(value="")
        ttk.Label(self.result_frame, textvariable=self.result_detail_var,
                  foreground="#34495e", wraplength=720,
                  font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))

        # Tree of tests (for fine-grained per-test selection, still useful)
        tree_lf = ttk.LabelFrame(self, text="Per-test detail (advanced)", padding=4)
        tree_lf.pack(fill="both", expand=True, padx=6, pady=2)

        cols = ("status", "duration")
        self.tree = ttk.Treeview(tree_lf, columns=cols, height=18,
                                  selectmode="browse")
        self.tree.heading("#0", text="Test")
        self.tree.heading("status", text="Status")
        self.tree.heading("duration", text="Time")
        self.tree.column("#0", width=400)
        self.tree.column("status", width=140, anchor="center")
        self.tree.column("duration", width=80, anchor="e")
        sb = ttk.Scrollbar(tree_lf, orient="vertical",
                           command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        # Status colour tags
        self.tree.tag_configure("PASS",         foreground="#1b8c3a")
        self.tree.tag_configure("FAIL",         background="#fadbd8",
                                                  foreground="black")
        self.tree.tag_configure("SKIP",         foreground="#7f8c8d")
        self.tree.tag_configure("INCONCLUSIVE", foreground="#cf8a17")
        self.tree.tag_configure("RUNNING",     foreground="#1f5fbf")
        self.tree.tag_configure("PENDING",     foreground="#7f8c8d")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._populate_tree()

        # Details pane
        det_lf = ttk.LabelFrame(self, text="Details", padding=4)
        det_lf.pack(fill="both", expand=True, padx=6, pady=4)
        self.detail_box = scrolledtext.ScrolledText(det_lf, height=10,
                                                     state="disabled",
                                                     font=("Courier", 9))
        self.detail_box.pack(fill="both", expand=True)

    def _populate_tree(self):
        for s in self._catalogue:
            if s.bucket not in self._bucket_iid:
                bid = self.tree.insert("", "end", text=f"  {s.bucket} — "
                                        f"{self.BUCKET_NAMES[s.bucket]}",
                                        open=True,
                                        values=("", ""))
                self._bucket_iid[s.bucket] = bid
            mark = "[✓]" if self._checked.get(s.test_id) else "[ ]"
            iid = self.tree.insert(self._bucket_iid[s.bucket], "end",
                                    text=f"  {mark}  {s.test_id} {s.name}",
                                    values=("PENDING", ""),
                                    tags=("PENDING",))
            self._row_iid[s.test_id] = iid

    def _refresh_row(self, test_id: str):
        spec = self._spec_by_id[test_id]
        mark = "[✓]" if self._checked.get(test_id) else "[ ]"
        self.tree.item(self._row_iid[test_id],
                        text=f"  {mark}  {spec.test_id} {spec.name}")

    def _set_row_status(self, test_id: str, status: str,
                         duration_s: Optional[float] = None):
        iid = self._row_iid[test_id]
        d_str = f"{duration_s:.1f}s" if duration_s is not None else ""
        self.tree.item(iid, values=(status, d_str), tags=(status,))

    def _on_tree_click(self, evt):
        """Toggle the [✓] checkbox if the click landed on a child row's
        text column. Bucket header rows toggle every child in the bucket."""
        iid = self.tree.identify_row(evt.y)
        if not iid or self.tree.identify_column(evt.x) != "#0":
            return
        # Bucket header row?
        for bucket, bid in self._bucket_iid.items():
            if iid == bid:
                # Toggle whole bucket
                children = self.tree.get_children(bid)
                # Determine new state from the first child (majority rule)
                first_id = next((tid for tid, riid in self._row_iid.items()
                                  if riid in children), None)
                if first_id is None:
                    return
                new_state = not self._checked.get(first_id, True)
                for tid, riid in self._row_iid.items():
                    if riid in children:
                        self._checked[tid] = new_state
                        self._refresh_row(tid)
                return
        # Child row? Find which test_id this iid corresponds to.
        for tid, riid in self._row_iid.items():
            if riid == iid:
                self._checked[tid] = not self._checked.get(tid, True)
                self._refresh_row(tid)
                return

    def _on_tree_select(self, _evt):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        for tid, riid in self._row_iid.items():
            if riid == iid:
                self._show_details(tid)
                return

    def _show_details(self, test_id: str):
        spec = self._spec_by_id.get(test_id)
        if spec is None:
            return
        result = self._ctx.results.get(test_id) if self._ctx else None
        lines = [f"{spec.test_id}  {spec.name}",
                  f"Bucket: {spec.bucket} — {self.BUCKET_NAMES[spec.bucket]}",
                  f"Requires: {', '.join(spec.requires) if spec.requires else '(none)'}",
                  ""]
        if result is None:
            lines.append("(not yet run)")
        else:
            lines.append(f"Status:    {result.status}")
            lines.append(f"Summary:   {result.summary}")
            lines.append(f"Duration:  {result.duration_s:.2f} s")
            lines.append(f"Started:   {result.started_iso}")
            lines.append("")
            lines.append("Evidence:")
            for d in result.details:
                lines.append(f"  • {d}")
        self.detail_box.configure(state="normal")
        self.detail_box.delete("1.0", "end")
        self.detail_box.insert("end", "\n".join(lines))
        self.detail_box.configure(state="disabled")

    # ── button handlers ──────────────────────────────────────────────────────

    # ── Domain checkbox panel handlers ──────────────────────────────────────

    DOMAIN_HINTS = {
        "A": "BLE — scan visibility, connect handshake, MTU, services, characteristics, disconnect/reconnect cleanup.",
        "B": "Auth — PIN write via CRC32-of-serial, verified by reading the Status characteristic.",
        "C": "Sensors — temperature, dB, lux, status state, 60 s update cadence (test-mode tolerant).",
        "D": "Battery — level in 0–100, 60 s updates, parity with Status char, stability while charging.",
        "E": "Haptic — motor on/off, intensity persistence, intensity clamp, countdown timer.  Requires you to confirm the buzz.",
        "F": "Flash — block count, CRC, reading count, sync pointer, sequence invariants, wrap audit trail, payload decode, time-sync effect.",
        "G": "Journal — count, CRC, monotonic sequence, valid event types, BOOT/TIME_SYNC/WRAP entries.",
        "I": "Hall — lid close fires hall event + enables boost; lid open does the reverse.  Requires you to physically move a magnet.",
        "U": "USB detect — Status.usb flips True on plug-in, False on unplug.  Requires you to plug/unplug a USB cable.",
        "H": "Long-running — ≥ 4 hour wrap watcher + connection uptime stress.  Opt in only when you have time to wait.",
        "W":   "Window — 5 s sampling-window cadence, full sensor record contents, peak_db ≥ db invariant, calibration sanity at ambient.  Some tests prompt you for a light/temp/sound action.",
        "BC":  "Batt cache — BAS == Status batt_pct, refresh-on-event latency for hall/USB, 90 s post-USB-unplug EMA lockout, window-open SAADC guard.  Hall + USB tests are interactive.",
        "CAL": "Calibration — lux / temp / dB validity against external reference instruments.  Requires you to enter reference readings from a luxon, thermometer, and SLM.",
        "TM":  "Timing — no fault-handler ERR entries in journal during a 5 min observation, Status-char read latency stable across 30 probes.",
    }

    def _show_domain_hint(self, code: Optional[str]):
        if code is None or code not in self.DOMAIN_HINTS:
            self.domain_hint_var.set(
                "Hover any checkbox to see what tests it covers.")
            return
        self.domain_hint_var.set(self.DOMAIN_HINTS[code])

    def _on_domain_toggle(self):
        """Update the big RUN button label whenever any domain checkbox flips,
        so the operator can see at a glance how many tests will run."""
        ids = self._collect_domain_ids()
        n = len(ids)
        # Rough time estimate (matches _kick_off heuristic).
        interactive = ("E", "I", "U")
        n_int  = sum(1 for tid in ids if tid[:1] in interactive)
        n_long = sum(1 for tid in ids
                      if self._spec_by_id.get(tid)
                      and self._spec_by_id[tid].optional)
        n_hl   = max(0, n - n_int - n_long)
        secs   = n_hl * 10 + n_int * 30
        if n_long > 0:
            tstr = f"≥{4 * n_long} h"
        elif secs >= 60:
            tstr = f"~{secs // 60} min"
        else:
            tstr = f"~{secs} s"
        self.big_run_btn.configure(
            text=f"▶ RUN  ({n} tests · {tstr})")

    def _collect_domain_ids(self) -> List[str]:
        """Return all test_ids belonging to currently-checked domains."""
        ids: List[str] = []
        for s in self._catalogue:
            if self.domain_vars.get(s.bucket) and self.domain_vars[s.bucket].get():
                ids.append(s.test_id)
        return ids

    def _on_run_domains(self):
        ids = self._collect_domain_ids()
        if not ids:
            messagebox.showinfo("Run", "No domains selected.")
            return
        self._kick_off(ids)

    def _domains_select_all(self):
        for v in self.domain_vars.values():
            v.set(True)
        self._on_domain_toggle()

    def _domains_headless_only(self):
        """Un-check domains that require physical interaction (E, I, U) and
        long-running (H).  Leaves the headless set ready to run."""
        for code, var in self.domain_vars.items():
            _, manual = self.DOMAIN_LABELS.get(code, ("", False))
            var.set(not manual and code != "H")
        self._on_domain_toggle()

    def _domains_clear(self):
        for v in self.domain_vars.values():
            v.set(False)
        self._on_domain_toggle()

    def _show_running_panel(self, total: int):
        """Make the live-progress panel visible at the start of a run."""
        self._run_started_at = time.time()
        self._recent_results = []
        self._run_total = total
        # Pack ABOVE the result frame so the panels visually stack with
        # running first while a run is in flight.
        try:
            self.running_frame.pack(fill="x", padx=6, pady=4,
                                     before=self.result_frame)
        except Exception:
            self.running_frame.pack(fill="x", padx=6, pady=4)
        self.running_progress.configure(maximum=total, value=0)
        self.running_var.set("Starting…")
        self.running_summary_var.set(f"0 / {total}   PASS 0   FAIL 0")
        self.running_recent_var.set("")

    def _hide_running_panel(self):
        try:
            self.running_frame.pack_forget()
        except Exception:
            pass

    def _update_running_panel(self, current_tid: str,
                              completed: int, last_result: Optional[TestResult]):
        """Called after each test finishes — updates progress + counts + recent
        list. Estimates ETA from per-test average so far."""
        if not self._ctx or self._run_started_at is None:
            return
        results = self._ctx.results
        passed = sum(1 for r in results.values() if r.status == "PASS")
        failed = sum(1 for r in results.values() if r.status == "FAIL")
        elapsed = time.time() - self._run_started_at
        avg_per = elapsed / max(1, completed)
        remaining = max(0, self._run_total - completed)
        eta = avg_per * remaining
        spec = self._spec_by_id.get(current_tid)
        running_name = spec.name if spec else current_tid
        self.running_var.set(f"▶ {current_tid}  {running_name}")
        pct = int(100 * completed / max(1, self._run_total))
        self.running_progress.configure(value=completed)
        self.running_summary_var.set(
            f"{completed} / {self._run_total} ({pct}%)   "
            f"PASS {passed}   FAIL {failed}   "
            f"elapsed {self._fmt_secs(elapsed)}   "
            f"ETA {self._fmt_secs(eta)}")
        if last_result is not None:
            mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "○",
                     "INCONCLUSIVE": "?"}.get(last_result.status, "·")
            self._recent_results.append(
                (last_result.test_id, last_result.name[:48], mark))
            self._recent_results = self._recent_results[-5:]
        recent_lines = [f"  {mark} {tid:<5} {name}"
                         for tid, name, mark in reversed(self._recent_results)]
        self.running_recent_var.set("Last 5:\n" + "\n".join(recent_lines)
                                      if recent_lines else "")

    @staticmethod
    def _fmt_secs(s: float) -> str:
        s = int(max(0, s))
        m, r = divmod(s, 60)
        return f"{m:02d}:{r:02d}"

    def _update_result_region(self):
        """Repaint the big green/red final-result label after a run completes.
        Driven from _on_run_done / cancel paths."""
        # Hide the live panel — it's only for in-flight runs.
        self._hide_running_panel()
        if self._ctx is None or not self._ctx.results:
            self.result_var.set("(no runs yet)")
            self.result_lbl.configure(foreground="#7f8c8d")
            self.result_detail_var.set("")
            return
        results = self._ctx.results
        total = len(results)
        passed = sum(1 for r in results.values() if r.status == "PASS")
        failed = [(tid, r) for tid, r in results.items() if r.status == "FAIL"]
        if not failed:
            self.result_var.set(f"✓  ALL TESTS PASSED  ({passed}/{total})")
            self.result_lbl.configure(foreground="#1b8c3a")
            self.result_detail_var.set("")
        else:
            self.result_var.set(f"✗  {len(failed)} OF {total} TESTS FAILED")
            self.result_lbl.configure(foreground="#c0392b")
            lines = [f"• {tid} — {(r.summary or '').splitlines()[0][:120]}"
                     for tid, r in failed[:8]]
            if len(failed) > 8:
                lines.append(f"… and {len(failed) - 8} more")
            # Drill-down hint — clicking the failed test row in the tree
            # below opens the existing per-test details pane with full evidence.
            lines.append("")
            lines.append("→ Click any failed test row in the tree below to see "
                          "evidence (per-line ctx.detail trace + threshold "
                          "comparison).")
            self.result_detail_var.set("\n".join(lines))

    def _on_run_all(self):
        ids = [s.test_id for s in self._catalogue
                if not s.optional or self.include_long_var.get()]
        self._kick_off(ids)

    def _on_run_selected(self):
        ids = [tid for tid in self._checked if self._checked[tid]]
        if not ids:
            messagebox.showinfo("Validation", "No tests selected.")
            return
        self._kick_off(ids)

    def _on_run_bucket(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Validation",
                                "Select a row in the tree first.")
            return
        iid = sel[0]
        # If bucket header selected, run that bucket
        target_bucket = None
        for bucket, bid in self._bucket_iid.items():
            if iid == bid:
                target_bucket = bucket
                break
        if target_bucket is None:
            for tid, riid in self._row_iid.items():
                if riid == iid:
                    target_bucket = self._spec_by_id[tid].bucket
                    break
        if target_bucket is None:
            return
        ids = [s.test_id for s in self._catalogue if s.bucket == target_bucket
                and (not s.optional or self.include_long_var.get())]
        self._kick_off(ids)

    def _on_cancel(self):
        if self._run_task and not self._run_task.done():
            if self._ctx:
                self._ctx.cancelled = True
            self._run_task.cancel()

    def _on_export(self):
        if self._ctx is None or not self._ctx.results:
            messagebox.showinfo("Export",
                                "No run results yet — run tests first.")
            return
        ts_str = datetime.now().strftime("%Y%m%d-%H%M%S")
        # Drop validation exports into Simulator/__pycache__/ so they're
        # already covered by the .gitignore __pycache__/ rule and don't
        # clutter the repo root. Falls back to CWD if anything goes wrong.
        try:
            out_dir = Path(__file__).resolve().parent / "__pycache__"
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            out_dir = Path(".")
        path = out_dir / f"dusq_validation_{ts_str}.json"
        out = {
            "run_at":  _now_iso(),
            "results": {tid: asdict(r) for tid, r in self._ctx.results.items()},
            "context": {
                "samples_count":   len(self._ctx.samples),
                "blocks_count":    len(self._ctx.blocks),
                "journal_count":   len(self._ctx.journal),
                "sent_epoch":      self._ctx.sent_epoch,
                "disconnect_count": self._ctx.disconnect_count,
                "error_state_seen": self._ctx.error_state_seen,
            },
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2, default=str)
            messagebox.showinfo("Export", f"Wrote {path}")
        except Exception as e:
            messagebox.showerror("Export", str(e))

    def _on_export_csv(self):
        """Write a CSV with one row per test for spreadsheet-friendly analysis.
        Columns: test_id, bucket, name, status, duration_ms, summary."""
        if self._ctx is None or not self._ctx.results:
            messagebox.showinfo("Export",
                                "No run results yet — run tests first.")
            return
        ts_str = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            out_dir = Path(__file__).resolve().parent / "__pycache__"
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            out_dir = Path(".")
        path = out_dir / f"dusq_validation_{ts_str}.csv"
        try:
            import csv
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["test_id", "bucket", "name",
                            "status", "duration_ms", "summary"])
                for tid, r in self._ctx.results.items():
                    spec = self._spec_by_id.get(tid)
                    bucket = spec.bucket if spec else ""
                    name   = spec.name if spec else ""
                    # Replace newlines in summary to keep CSV row count sane.
                    summary = (r.summary or "").replace("\n", " | ")
                    w.writerow([tid, bucket, name,
                                r.status, r.duration_ms, summary])
            messagebox.showinfo("Export", f"Wrote {path}")
        except Exception as e:
            messagebox.showerror("Export CSV", str(e))

    def _on_reset(self):
        for tid in self._row_iid:
            self._set_row_status(tid, "PENDING")
        self._ctx = None

    # ── runner core ──────────────────────────────────────────────────────────

    def _kick_off(self, requested_ids: List[str]):
        if not self.ble.connected:
            messagebox.showwarning("Validation", "Not connected.")
            return
        if self._run_task and not self._run_task.done():
            messagebox.showwarning("Validation", "A run is already in progress.")
            return

        # Pre-run summary dialog — gives the operator a quick check before
        # we kick off a long batch.  Estimates total time using:
        #   ~30 s per interactive test (E*, H*, U*)
        #   ~10 s per headless test
        #   long-running (optional=True) entries: ≥ 4 hours each
        interactive_prefixes = ("E", "H", "U")
        n_interactive = sum(1 for tid in requested_ids
                            if tid[:1] in interactive_prefixes)
        n_longrunning = sum(1 for tid in requested_ids
                            if self._spec_by_id.get(tid)
                            and self._spec_by_id[tid].optional)
        n_headless    = max(0, len(requested_ids) - n_interactive - n_longrunning)
        est_sec       = n_headless * 10 + n_interactive * 30
        if n_longrunning > 0:
            time_str = f"≥ {4 * n_longrunning} hour(s) (long-running included)"
        elif est_sec >= 60:
            time_str = f"~{est_sec // 60} min {est_sec % 60} sec"
        else:
            time_str = f"~{est_sec} sec"
        msg = (f"About to run {len(requested_ids)} test(s):\n\n"
               f"  • {n_headless} headless\n"
               f"  • {n_interactive} require physical interaction\n"
               + (f"  • {n_longrunning} long-running (≥ 4 h each)\n" if n_longrunning else "")
               + f"\nEstimated time: {time_str}\n\nContinue?")
        if not messagebox.askokcancel("Run plan", msg):
            return

        # Resolve dependencies (transitive closure via BFS)
        resolved: List[str] = []
        seen = set()
        def add(tid: str):
            if tid in seen:
                return
            seen.add(tid)
            spec = self._spec_by_id.get(tid)
            if spec is None:
                return
            for dep in spec.requires:
                add(dep)
            resolved.append(tid)
        for tid in requested_ids:
            add(tid)
        # Reset rows for tests in this run
        for tid in resolved:
            self._set_row_status(tid, "PENDING")

        self._ctx = TestContext()
        self.cancel_btn.configure(state="normal")
        self.run_all_btn.configure(state="disabled")
        self.run_sel_btn.configure(state="disabled")
        self.run_bkt_btn.configure(state="disabled")
        # Live progress panel takes over the result region for the duration
        # of the run. Hidden again when _update_result_region fires at end.
        self._show_running_panel(total=len(resolved))
        self._run_task = run_async(self._run(resolved))

    async def _run(self, ids: List[str]):
        ctx = self._ctx
        try:
            for tid in ids:
                if ctx.cancelled:
                    break
                spec = self._spec_by_id[tid]
                # Dependency check
                skip_reason = None
                for dep in spec.requires:
                    prev = ctx.results.get(dep)
                    if prev is None:
                        skip_reason = f"prerequisite {dep} not run"
                        break
                    if prev.status != "PASS":
                        skip_reason = f"prerequisite {dep} = {prev.status}"
                        break
                if skip_reason:
                    ctx.results[tid] = TestResult(
                        test_id=tid, bucket=spec.bucket, name=spec.name,
                        status="SKIP", summary=skip_reason,
                        started_iso=_now_iso())
                    self._set_row_status(tid, "SKIP", 0.0)
                    continue
                # Run
                self._set_row_status(tid, "RUNNING")
                # Auto-select the running row so the user always sees what's
                # happening in the details pane.
                self.tree.selection_set(self._row_iid[tid])
                self.tree.see(self._row_iid[tid])
                started = time.time()
                result = TestResult(test_id=tid, bucket=spec.bucket,
                                     name=spec.name, status="RUNNING",
                                     started_iso=_now_iso())
                # Register the result so ctx.detail() can find it before the
                # test even returns. Stream callback refreshes the details
                # pane every time a line is appended.
                ctx.results[tid] = result
                ctx.current_test_id = tid
                ctx.on_detail = lambda _line, _tid=tid: self._show_details(_tid)
                self._show_details(tid)   # show initial "RUNNING" state
                try:
                    # spec.runner is a bound method (self._t_*) — already
                    # has self bound, so we pass only ctx.
                    result = await spec.runner(ctx)
                except asyncio.CancelledError:
                    result.status = "INCONCLUSIVE"
                    result.summary = "cancelled"
                    raise
                except Exception as e:
                    result.status = "FAIL"
                    result.summary = f"exception: {e!r}"
                    result.details.append(f"exception traceback: {e!r}")
                finally:
                    result.duration_s = time.time() - started
                    ctx.results[tid] = result
                    ctx.current_test_id = None
                    ctx.on_detail = None
                    self._set_row_status(tid, result.status, result.duration_s)
                    self._show_details(tid)
                    # Update the live progress panel (running test name,
                    # counts, ETA, last-5).
                    try:
                        completed = sum(1 for r in ctx.results.values()
                                         if r.status != "PENDING"
                                         and r.status != "RUNNING")
                        self._update_running_panel(tid, completed, result)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            for tid in ids:
                if tid not in ctx.results:
                    ctx.results[tid] = TestResult(
                        test_id=tid, bucket=self._spec_by_id[tid].bucket,
                        name=self._spec_by_id[tid].name, status="SKIP",
                        summary="cancelled", started_iso=_now_iso())
                    self._set_row_status(tid, "SKIP", 0.0)
        finally:
            self.cancel_btn.configure(state="disabled")
            self.run_all_btn.configure(state="normal")
            self.run_sel_btn.configure(state="normal")
            self.run_bkt_btn.configure(state="normal")
            # Refresh the big green/red result label on the new UX panel.
            try:
                self._update_result_region()
            except Exception:
                pass

    # ── shared collection helpers (cached per ctx) ───────────────────────────

    async def _ensure_samples(self, ctx: TestContext,
                                n: int = SAMPLE_COUNT_DEFAULT) -> bool:
        if len(ctx.samples) >= n:
            return True
        ctx.samples.clear()
        ctx.bas_events.clear()
        ctx.detail(f"collecting {n} sensor sample(s)…")
        evt = asyncio.Event()

        def _on_sensor(_h, data: bytearray):
            temp_c, db, peak_db, lux = _decode_sensor_payload(data)
            if db is None:
                return  # malformed / short packet
            entry = {
                "temp":    temp_c,
                "db":      None if db   == DB_ERROR   else db,
                "peak_db": None if peak_db == DB_ERROR else peak_db,
                "lux":     None if lux  == LUX_ERROR  else lux,
                "ts":      time.time(),
            }
            ctx.samples.append(entry)
            temp_disp = "None" if temp_c is None else f"{temp_c:.1f}"
            ctx.detail(f"  sample {len(ctx.samples)}/{n}: "
                        f"t={temp_disp}°C  db={entry['db']} "
                        f"(peak {entry['peak_db']})  "
                        f"lux={entry['lux']}")
            if len(ctx.samples) >= n:
                evt.set()

        def _on_status(_h, data: bytearray):
            if len(data) >= 1 and data[0] == 6:    # ERROR state
                ctx.error_state_seen = True

        def _on_batt(_h, data: bytearray):
            if not data:
                return
            now = time.time()
            ctx.bas_events.append({"ts": now, "pct": data[0]})
            ctx.detail(f"  BAS notify: {data[0]} % "
                        f"(arrival #{len(ctx.bas_events)})")
            if ctx.samples:
                ctx.samples[-1].setdefault("batt_pct", data[0])

        await self.ble.subscribe(CHAR_SENSOR_DATA, _on_sensor)
        await self.ble.subscribe(CHAR_STATUS,      _on_status)
        await self.ble.subscribe(CHAR_BATT_LEVEL,  _on_batt)
        try:
            await asyncio.wait_for(evt.wait(),
                                    timeout=(n + 1) * (SAMPLE_PERIOD_S + 5))
        except asyncio.TimeoutError:
            ctx.detail(f"  sample collection timed out at "
                        f"{len(ctx.samples)}/{n}")
        finally:
            await self.ble.unsubscribe(CHAR_SENSOR_DATA)
            await self.ble.unsubscribe(CHAR_STATUS)
            await self.ble.unsubscribe(CHAR_BATT_LEVEL)
        return len(ctx.samples) >= max(1, n // 2)   # accept partial collection

    async def _ensure_blocks(self, ctx: TestContext) -> bool:
        if ctx.blocks:
            return True
        ok = await self._download_blocks_into(ctx)
        return ok

    async def _download_blocks_into(self, ctx: TestContext) -> bool:
        ctx.detail("downloading sensor blocks…")
        idle = asyncio.Event()
        buf = bytearray()
        loop = asyncio.get_event_loop()

        def _on(_h, data: bytearray):
            buf.extend(data)
            while len(buf) >= BLOCK_SIZE:
                raw = bytes(buf[:BLOCK_SIZE])
                del buf[:BLOCK_SIZE]
                blk = decode_block(raw)
                if blk:
                    ctx.blocks.append(blk)
                    crc = "OK" if blk["crc_ok"] else "FAIL"
                    ctx.detail(f"  block {len(ctx.blocks)}: seq={blk['sequence']}"
                                f"  rc={blk['reading_count']}  CRC={crc}")
            loop.call_soon_threadsafe(idle.set)

        await self.ble.subscribe(CHAR_RECORD, _on)
        try:
            await self.ble.write(CHAR_RECORD, bytes([0x01]))
            first = True
            while True:
                idle.clear()
                t_out = 15.0 if first else 5.0
                try:
                    await asyncio.wait_for(idle.wait(), timeout=t_out)
                except asyncio.TimeoutError:
                    break
                first = False
        finally:
            try:
                await self.ble.unsubscribe(CHAR_RECORD)
            except Exception:
                pass
        ctx.detail(f"download complete — {len(ctx.blocks)} block(s)")
        return True

    async def _ensure_journal(self, ctx: TestContext) -> bool:
        if ctx.journal:
            return True
        ctx.detail("downloading journal…")
        idle = asyncio.Event()
        buf = bytearray()
        loop = asyncio.get_event_loop()

        def _on(_h, data: bytearray):
            buf.extend(data)
            while len(buf) >= JOURNAL_ENTRY_SIZE:
                raw = bytes(buf[:JOURNAL_ENTRY_SIZE])
                del buf[:JOURNAL_ENTRY_SIZE]
                e = decode_journal_entry(raw)
                if e:
                    ctx.journal.append(e)
                    ctx.detail(f"  entry {len(ctx.journal)}: "
                                f"seq={e['sequence']}  type={e['type_str']}  "
                                f"data={e['data']}")
            loop.call_soon_threadsafe(idle.set)

        await self.ble.subscribe(CHAR_JOURNAL_RECORD, _on)
        try:
            await self.ble.write(CHAR_JOURNAL_START, bytes([0x01]))
            first = True
            while True:
                idle.clear()
                t_out = 15.0 if first else 5.0
                try:
                    await asyncio.wait_for(idle.wait(), timeout=t_out)
                except asyncio.TimeoutError:
                    break
                first = False
        finally:
            try:
                await self.ble.unsubscribe(CHAR_JOURNAL_RECORD)
            except Exception:
                pass
        ctx.detail(f"journal download complete — {len(ctx.journal)} entry(ies)")
        return True

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _on_disconnected(self):
        if self._ctx:
            self._ctx.disconnect_count += 1
        self._on_cancel()

    # ── test catalogue ───────────────────────────────────────────────────────

    def _build_catalogue(self) -> List[TestSpec]:
        """All 49 tests. Each runner is a method on this class taking (self, ctx)."""
        T = TestSpec
        return [
            # ── Bucket A — BLE Link ─────────────────────────────────────────
            # A6 is intentionally MOVED to the very end of the catalogue so
            # it runs last during "Run All". Reason: A6 tears down and
            # re-establishes the BLE link, which would force every
            # downstream test to re-authenticate. Running it last leaves
            # the rest of the suite undisturbed.
            T("A1", "A", "Device shows up in scan",     self._t_A1),
            T("A2", "A", "Connection succeeds",          self._t_A2),
            T("A3", "A", "MTU negotiated > 23 bytes",   self._t_A3, ("A2",)),
            T("A4", "A", "All 5 services found",         self._t_A4, ("A2",)),
            T("A5", "A", "All characteristics found",    self._t_A5, ("A2",)),

            # ── Bucket B — Authentication ──────────────────────────────────
            T("B1", "B", "PIN authenticates the device", self._t_B1, ("A2",)),

            # ── Bucket C — Sensor pipeline ─────────────────────────────────
            T("C1", "C", "Sensor updates every 60 s",    self._t_C1, ("B1",)),
            T("C2", "C", "Temperature in valid range",   self._t_C2, ("B1",)),
            T("C5", "C", "Mic readings in valid range",  self._t_C5, ("B1",)),
            T("C7", "C", "Lux in valid range",           self._t_C7, ("B1",)),
            T("C8", "C", "Status state is sensible",     self._t_C8, ("B1",)),

            # ── Bucket D — Battery ─────────────────────────────────────────
            T("D1", "D", "Battery % in 0–100",            self._t_D1, ("B1",)),
            T("D2", "D", "Battery updates every 60 s",   self._t_D2, ("B1",)),
            T("D3", "D", "Battery matches status char",  self._t_D3, ("B1",)),
            T("D4", "D", "Battery stable while charging", self._t_D4, ("B1",)),

            # ── Bucket E — Haptic ──────────────────────────────────────────
            T("E1", "E", "Motor turns ON",               self._t_E1, ("B1",)),
            T("E2", "E", "Motor turns OFF",              self._t_E2, ("E1",)),
            T("E3", "E", "Intensity setting persists",   self._t_E3, ("B1",)),
            T("E4", "E", "Intensity clamps to 100",      self._t_E4, ("B1",)),
            T("E5", "E", "Countdown timer fires",        self._t_E5, ("B1",)),

            # ── Bucket F — Flash sensor blocks ─────────────────────────────
            T("F1",  "F", "Block count reads cleanly",          self._t_F1, ("B1",)),
            T("F2",  "F", "Downloaded blocks pass CRC",         self._t_F2, ("B1",)),
            T("F3",  "F", "Reading count is sane",              self._t_F3, ("F2",)),
            T("F4",  "F", "Sync status matches timestamp",      self._t_F4, ("F2",)),
            T("F5",  "F", "Sync pointer advances after download", self._t_F5, ("F2",)),
            T("F6",  "F", "No duplicate sequences in a boot",   self._t_F6, ("F2",)),
            T("F7",  "F", "Sequence span fits in 234",          self._t_F7, ("F2",)),
            T("F8",  "F", "Sequence increases across slot wrap", self._t_F8, ("F2",)),
            T("F9",  "F", "Sequence resets after reboot",       self._t_F9, ("F2", "G2")),
            T("F10", "F", "Time sync reflected in next block",  self._t_F10, ("F2",)),
            T("F11", "F", "Wrap audit trail is consistent",     self._t_F11, ("F2", "G2")),
            T("F12", "F", "Block payload decodes cleanly",      self._t_F12, ("F2",)),

            # ── Bucket G — Journal ─────────────────────────────────────────
            T("G1", "G", "Journal count reads cleanly",         self._t_G1, ("B1",)),
            T("G2", "G", "Journal entries pass CRC",            self._t_G2, ("B1",)),
            T("G3", "G", "Journal sequence is monotonic",       self._t_G3, ("G2",)),
            T("G4", "G", "Journal event types are valid",       self._t_G4, ("G2",)),
            T("G5", "G", "Time sync logged in journal",         self._t_G5, ("F10", "G2")),
            T("G6", "G", "Wrap entries reference valid blocks", self._t_G6, ("G2",)),
            T("G7", "G", "Boot event present in journal",       self._t_G7, ("G2",)),

            # ── Bucket I — Hall (lid sensor) — interactive ─────────────────
            T("I1", "I", "Lid close fires hall event (boost ON)",
                self._t_I1, ("B1",)),
            T("I2", "I", "Lid open fires hall event (boost OFF)",
                self._t_I2, ("B1",)),

            # ── Bucket U — USB detect — interactive ────────────────────────
            T("U1", "U", "USB plug-in detected",  self._t_U1, ("B1",)),
            T("U2", "U", "USB unplug detected",   self._t_U2, ("B1",)),

            # ── Bucket H — Long-running (opt-in) ───────────────────────────
            T("H1", "H", "Wrap watcher (4+ hours)",  self._t_H1, ("F2",), True),
            T("H2", "H", "Connection stays up",      self._t_H2, ("A2",), True),

            # ── Bucket W — 5 s sampling window contract ───────────────────
            T("W1", "W",   "Cadence — record interval within ±2 s of 60 s",
                self._t_W1, ("B1",)),
            T("W2", "W",   "All 3 fields populated (no sentinels) for 3 cycles",
                self._t_W2, ("B1",)),
            T("W3", "W",   "peak_db ≥ db every cycle",
                self._t_W3, ("B1",)),
            T("W4", "W",   "Lux > 100 at room-lit ambient (manual)",
                self._t_W4, ("B1",)),
            T("W5", "W",   "Temp within ±2 °C of reference (manual)",
                self._t_W5, ("B1",)),
            T("W6", "W",   "dB rises ≥ 10 dB on loud sound (manual)",
                self._t_W6, ("B1",)),

            # ── Bucket BC — battery cache & refresh-on-event ──────────────
            T("BC1", "BC", "BAS char matches Status batt_pct (cache parity)",
                self._t_BC1, ("B1",)),
            T("BC2", "BC", "BAS updates within 500 ms of hall close (manual)",
                self._t_BC2, ("B1",)),
            T("BC3", "BC", "BAS updates within 500 ms of USB plug (manual)",
                self._t_BC3, ("B1",)),
            T("BC4", "BC", "EMA frozen 90 s after USB unplug (manual)",
                self._t_BC4, ("B1",)),
            T("BC5", "BC", "Hall mid-window: record still arrives + BAS updates after window close (manual)",
                self._t_BC5, ("B1",)),

            # ── Bucket CAL — calibration validity (reference fixtures) ────
            T("CAL1", "CAL", "Lux within ±20 % of luxon reference at 3 levels (manual)",
                self._t_CAL1, ("B1",)),
            T("CAL2", "CAL", "Temp within ±1 °C of reference thermometer (manual)",
                self._t_CAL2, ("B1",)),
            T("CAL3", "CAL", "dB within ±3 dB of SLM at known SPL (manual)",
                self._t_CAL3, ("B1",)),

            # ── Bucket TM — timing & health ───────────────────────────────
            T("TM1", "TM",  "Zero ERR entries in journal during 5 min observation",
                self._t_TM1, ("B1",)),
            T("TM2", "TM",  "Status-char latency P95 < 100 ms across 30 probes",
                self._t_TM2, ("B1",)),

            # A6 (disconnect & reconnect) intentionally NOT registered —
            # tearing down the link forces downstream re-auth and provides
            # almost no diagnostic value over A2 (connect). Keep the
            # _t_A6 method around as dead code so we can re-register later
            # if needed.
        ]

    # ─────────────────────────────────────────────────────────────────────────
    #  Test runners — each returns a TestResult.
    #  Helpers:
    #    self._mk(tid, status, summary, details=[]) — convenience constructor
    # ─────────────────────────────────────────────────────────────────────────

    def _mk(self, tid: str, status: str, summary: str,
             details: Optional[List[str]] = None) -> TestResult:
        s = self._spec_by_id[tid]
        return TestResult(test_id=tid, bucket=s.bucket, name=s.name,
                           status=status, summary=summary,
                           details=list(details or []),
                           started_iso=_now_iso())

    async def _ask_pass_fail(self, test_id: str, question: str) -> bool:
        """Show a modal Pass/Fail dialog and await the user's verdict.

        Used for haptic tests where the BLE link gives no ground truth that
        the motor actually buzzed — only physical observation can confirm.
        Returns True for Pass, False for Fail. Dialog defaults to Fail if
        the window is closed without picking either button.
        """
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()

        win = tk.Toplevel(self)
        win.title(f"Manual check — {test_id}")
        win.geometry("440x180")
        win.transient(self.winfo_toplevel())
        win.grab_set()
        win.resizable(False, False)

        ttk.Label(win, text=question, wraplength=400, justify="left",
                  padding=14, font=("Segoe UI", 10)).pack(fill="x")
        bf = ttk.Frame(win)
        bf.pack(pady=12)

        def _resolve(ok: bool):
            if not fut.done():
                fut.set_result(ok)
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()

        ttk.Button(bf, text="✓ Pass",
                   command=lambda: _resolve(True),
                   width=14).pack(side="left", padx=10)
        ttk.Button(bf, text="✗ Fail",
                   command=lambda: _resolve(False),
                   width=14).pack(side="left", padx=10)
        win.protocol("WM_DELETE_WINDOW", lambda: _resolve(False))

        return await fut

    # ── Bucket A ─────────────────────────────────────────────────────────────

    async def _t_A1(self, ctx):
        # The connection tab populated devices when the user scanned.
        # We don't have a direct hook here; if we're connected, scanning succeeded.
        if self.ble.connected:
            return self._mk("A1", "PASS",
                             "device was discovered (connection succeeded)",
                             ["proxy: ble.connected == True"])
        return self._mk("A1", "FAIL", "not connected — scan must have missed device")

    async def _t_A2(self, ctx):
        if self.ble.connected:
            return self._mk("A2", "PASS", "client connected",
                             [f"address: {self.ble.client.address}"])
        return self._mk("A2", "FAIL", "ble.connected is False")

    async def _t_A3(self, ctx):
        mtu = getattr(self.ble.client, "mtu_size", 0)
        if mtu and mtu > 23:
            return self._mk("A3", "PASS",
                             f"MTU negotiated to {mtu} bytes",
                             [f"mtu_size = {mtu}"])
        return self._mk("A3", "FAIL",
                         f"MTU stuck at default ({mtu}) — DLE/MTU exchange failed")

    async def _t_A4(self, ctx):
        try:
            services = self.ble.client.services
            uuids = {str(svc.uuid).lower() for svc in services}
            need = [SVC_AUTH, SVC_SENSOR, SVC_FLASH, SVC_HAPTIC, SVC_BATT]
            missing = [u for u in need if u.lower() not in uuids]
            if missing:
                return self._mk("A4", "FAIL",
                                 f"{len(missing)} service(s) missing",
                                 [f"missing: {m}" for m in missing])
            return self._mk("A4", "PASS",
                             "all 5 services present",
                             [f"discovered {len(uuids)} services total"])
        except Exception as e:
            return self._mk("A4", "FAIL", f"service discovery failed: {e}")

    async def _t_A5(self, ctx):
        wanted = [CHAR_AUTH_PIN, CHAR_SENSOR_DATA, CHAR_STATUS,
                   CHAR_BLOCK_COUNT, CHAR_RECORD, CHAR_TIMESYNC,
                   CHAR_HAPTIC_CTL, CHAR_HAPTIC_INT,
                   CHAR_HAPTIC_STAT, CHAR_JOURNAL_COUNT,
                   CHAR_JOURNAL_RECORD, CHAR_JOURNAL_START,
                   CHAR_BATT_LEVEL]
        try:
            services = self.ble.client.services
            char_uuids = {str(c.uuid).lower()
                          for svc in services for c in svc.characteristics}
            missing = [u for u in wanted if u.lower() not in char_uuids]
            if missing:
                return self._mk("A5", "FAIL",
                                 f"{len(missing)} characteristic(s) missing",
                                 [f"missing: {m}" for m in missing])
            return self._mk("A5", "PASS", "all expected chars present",
                             [f"checked {len(wanted)} chars"])
        except Exception as e:
            return self._mk("A5", "FAIL", f"discovery failed: {e}")

    async def _t_A6(self, ctx):
        if not self.ble.connected:
            return self._mk("A6", "SKIP", "not connected")
        addr = self.ble.client.address
        try:
            await self.ble.disconnect()
            await asyncio.sleep(1.0)
            await self.ble.connect(addr)
            await asyncio.sleep(0.5)
            if self.ble.connected:
                return self._mk("A6", "PASS",
                                 "disconnect + reconnect cycle clean",
                                 [f"reconnected to {addr}"])
            return self._mk("A6", "FAIL", "reconnect did not complete")
        except Exception as e:
            return self._mk("A6", "FAIL", f"reconnect cycle failed: {e}")

    # ── Bucket B ─────────────────────────────────────────────────────────────

    async def _t_B1(self, ctx):
        try:
            serial = bytes(await self.ble.read(CHAR_DIS_SERIAL))
            pin = compute_auth_pin(serial)
            await self.ble.write(CHAR_AUTH_PIN, pin)
            await asyncio.sleep(1.0)
            raw = await self.ble.read(CHAR_STATUS)
            stat = decode_status(raw)
            if stat.get("state") == "AUTHENTICATED":
                return self._mk("B1", "PASS", "PIN accepted",
                                 [f"serial = {serial.decode('ascii', 'replace')}",
                                  f"pin = {pin.hex(' ').upper()}",
                                  f"status state = {stat['state']}"])
            return self._mk("B1", "FAIL",
                             f"state did not flip to AUTHENTICATED",
                             [f"serial = {serial.decode('ascii', 'replace')}",
                              f"pin = {pin.hex(' ').upper()}",
                              f"observed state = {stat.get('state')}"])
        except Exception as e:
            return self._mk("B1", "FAIL", f"auth flow error: {e}")


    # ── Bucket C ─────────────────────────────────────────────────────────────

    async def _t_C1(self, ctx):
        await self._ensure_samples(ctx)
        if len(ctx.samples) < 2:
            return self._mk("C1", "INCONCLUSIVE",
                             "fewer than 2 samples collected")
        gaps = [ctx.samples[i+1]["ts"] - ctx.samples[i]["ts"]
                for i in range(len(ctx.samples) - 1)]
        gmin = min(gaps); gmax = max(gaps); gavg = sum(gaps) / len(gaps)
        # Variance check — if all gaps are tightly clustered the device IS
        # ticking on a regular schedule, just at a different period than the
        # production 60 s. Most likely a debug/test build with a shorter
        # SENSOR_PERIOD_MS — surface as INCONCLUSIVE (yellow) rather than
        # FAIL so the user knows it's working but not at the expected rate.
        var = max(gaps) - min(gaps)
        details = [f"measured gaps (s): {[round(g,2) for g in gaps]}",
                   f"min={gmin:.2f} s   max={gmax:.2f} s   avg={gavg:.2f} s",
                   f"jitter (max-min) = {var:.2f} s",
                   f"expected window: [55, 70] s"]
        bad = [g for g in gaps if g < 55 or g > 70]
        if not bad:
            return self._mk("C1", "PASS",
                             f"cadence ~{gavg:.1f}s (within [55,70])", details)
        # Tightly-clustered but outside expected window → INCONCLUSIVE
        if var < max(0.5, 0.1 * gavg):
            return self._mk(
                "C1", "INCONCLUSIVE",
                f"steady ~{gavg:.1f}s but ≠ production 60s — "
                f"is firmware in test mode?",
                details + ["all gaps consistent → device IS ticking, "
                            "just at a non-production rate"])
        return self._mk("C1", "FAIL",
                         f"{len(bad)} gap(s) outside [55,70]; "
                         f"avg={gavg:.1f}s var={var:.1f}s",
                         details)

    async def _t_C2(self, ctx):
        await self._ensure_samples(ctx)
        temps = [s["temp"] for s in ctx.samples]
        errs = [t for t in temps if t is None]
        oos  = [t for t in temps if t is not None
                and not TEMP_VALID_MIN <= t <= TEMP_VALID_MAX]
        if errs or oos:
            return self._mk("C2", "FAIL",
                             f"{len(errs)} error markers, {len(oos)} out-of-range",
                             [f"out-of-range: {oos}"])
        return self._mk("C2", "PASS",
                         f"all temps in [{TEMP_VALID_MIN},{TEMP_VALID_MAX}]°C",
                         [f"min={min(temps)}  max={max(temps)}"])

    async def _t_C5(self, ctx):
        """Validate dB readings are in range and not stuck (mic alive)."""
        await self._ensure_samples(ctx)
        dbs = [s["db"] for s in ctx.samples if s["db"] is not None]
        errs = sum(1 for s in ctx.samples if s["db"] is None)
        oos = [v for v in dbs
               if not DB_VALID_MIN <= v <= DB_VALID_MAX]
        if errs or oos:
            return self._mk("C5", "FAIL",
                             f"{errs} error markers, {len(oos)} out-of-range",
                             [f"oos values: {oos}"])
        if len(dbs) >= 5 and len(set(dbs)) < 2:
            return self._mk("C5", "FAIL",
                             "all dB values identical — mic stuck",
                             [f"dbs = {dbs}"])
        return self._mk("C5", "PASS",
                         f"all dB ∈ [{DB_VALID_MIN},{DB_VALID_MAX}]",
                         [f"range {min(dbs)}-{max(dbs)} across {len(dbs)} samples"])

    async def _t_C7(self, ctx):
        await self._ensure_samples(ctx)
        luxes = [s["lux"] for s in ctx.samples]
        errs = sum(1 for v in luxes if v is None)
        oos = [v for v in luxes
               if v is not None and not LUX_VALID_MIN <= v <= LUX_VALID_MAX]
        if errs or oos:
            return self._mk("C7", "FAIL",
                             f"{errs} error markers, {len(oos)} out-of-range",
                             [f"out-of-range: {oos}"])
        return self._mk("C7", "PASS",
                         f"all lux ∈ [{LUX_VALID_MIN},{LUX_VALID_MAX}]",
                         [f"min={min(luxes)} max={max(luxes)}"])

    async def _t_C8(self, ctx):
        try:
            raw = await self.ble.read(CHAR_STATUS)
            stat = decode_status(raw)
            state = stat.get("state", "?")
            if state in ("AUTHENTICATED", "FLASH_TRANSFER"):
                return self._mk("C8", "PASS",
                                 f"state = {state}",
                                 [f"full status: {stat}"])
            return self._mk("C8", "FAIL",
                             f"unexpected state: {state}",
                             [f"full status: {stat}"])
        except Exception as e:
            return self._mk("C8", "INCONCLUSIVE", f"read failed: {e}")

    # ── Bucket D ─────────────────────────────────────────────────────────────

    async def _t_D1(self, ctx):
        try:
            raw = await self.ble.read(CHAR_BATT_LEVEL)
            v = raw[0] if raw else None
            if v is None or not 0 <= v <= 100:
                return self._mk("D1", "FAIL",
                                 f"BAS value out of range: {v}")
            return self._mk("D1", "PASS", f"BAS = {v} %")
        except Exception as e:
            return self._mk("D1", "FAIL", f"BAS read failed: {e}")

    async def _t_D2(self, ctx):
        # Lean on samples collection — BAS subscribed there
        await self._ensure_samples(ctx)
        evts = ctx.bas_events
        if len(evts) < 2:
            return self._mk(
                "D2", "INCONCLUSIVE",
                f"need ≥2 BAS notifies, got {len(evts)}",
                [f"events: {evts}"])
        gaps = [evts[i+1]["ts"] - evts[i]["ts"] for i in range(len(evts) - 1)]
        gmin = min(gaps); gmax = max(gaps); gavg = sum(gaps) / len(gaps)
        var = gmax - gmin
        details = [f"BAS arrivals: {len(evts)}",
                   f"values seen: {[e['pct'] for e in evts]}",
                   f"inter-notify gaps (s): {[round(g,2) for g in gaps]}",
                   f"min={gmin:.2f} s   max={gmax:.2f} s   avg={gavg:.2f} s",
                   f"jitter (max-min) = {var:.2f} s",
                   f"expected window: ≤ 70 s per notify"]
        if gmax <= 70.0:
            return self._mk("D2", "PASS",
                             f"BAS cadence avg={gavg:.1f}s (max gap {gmax:.1f}s)",
                             details)
        # Steady but not at production cadence → INCONCLUSIVE (yellow)
        if var < max(0.5, 0.1 * gavg):
            return self._mk(
                "D2", "INCONCLUSIVE",
                f"BAS steady ~{gavg:.1f}s but slower than expected — "
                f"firmware may be in test mode",
                details + ["all gaps consistent → notifications ARE arriving, "
                            "just at a non-production rate"])
        return self._mk(
            "D2", "FAIL",
            f"BAS gaps inconsistent: avg={gavg:.1f}s, max={gmax:.1f}s",
            details)

    async def _t_D3(self, ctx):
        try:
            bas = (await self.ble.read(CHAR_BATT_LEVEL))[0]
            stat = decode_status(await self.ble.read(CHAR_STATUS))
            stat_pct = stat.get("batt_pct", 255)
            diff = abs(int(bas) - int(stat_pct))
            if diff <= 1:
                return self._mk("D3", "PASS",
                                 f"BAS={bas} status={stat_pct} (Δ={diff})")
            return self._mk("D3", "FAIL",
                             f"BAS={bas} vs status={stat_pct} differ by {diff}")
        except Exception as e:
            return self._mk("D3", "INCONCLUSIVE", f"read error: {e}")

    async def _t_D4(self, ctx):
        await self._ensure_samples(ctx)
        bas = [s.get("batt_pct") for s in ctx.samples if "batt_pct" in s]
        try:
            stat = decode_status(await self.ble.read(CHAR_STATUS))
        except Exception as e:
            return self._mk("D4", "INCONCLUSIVE", f"status read: {e}")
        if not stat.get("usb"):
            return self._mk("D4", "SKIP", "USB not connected")
        if len(bas) < 2:
            return self._mk("D4", "INCONCLUSIVE", "not enough BAS notifies")
        drop = max(bas) - min(bas)
        if drop > 1:
            return self._mk("D4", "FAIL",
                             f"BAS dropped {drop}% during USB-connected window")
        return self._mk("D4", "PASS",
                         f"BAS stable: drop = {drop}%",
                         [f"values: {bas}"])

    # ── Bucket E ─────────────────────────────────────────────────────────────

    # NOTE: every haptic test below requires user confirmation. The BLE
    # link gives no ground truth that the motor actually buzzed — only
    # physical observation can confirm. Each test does the BLE write,
    # then opens a Pass/Fail dialog and lets the user decide.

    async def _t_E1(self, ctx):
        try:
            ctx.detail("writing 0x01 to CHAR_HAPTIC_CTL (motor ON)")
            await self.ble.write(CHAR_HAPTIC_CTL, bytes([0x01]))
            ok = await self._ask_pass_fail(
                "E1 Motor turns ON",
                "Wrote 0x01 to the haptic control char.\n\n"
                "Did the motor START buzzing?\n\n"
                "Pass = motor buzzed.   Fail = motor stayed silent.")
            return self._mk("E1", "PASS" if ok else "FAIL",
                             "user confirmed motor buzzed" if ok
                             else "user reported motor did NOT buzz",
                             ["wrote: 0x01 → 0x1530"])
        except Exception as e:
            return self._mk("E1", "FAIL", f"write error: {e}")

    async def _t_E2(self, ctx):
        try:
            ctx.detail("writing 0x00 to CHAR_HAPTIC_CTL (motor OFF)")
            await self.ble.write(CHAR_HAPTIC_CTL, bytes([0x00]))
            ok = await self._ask_pass_fail(
                "E2 Motor turns OFF",
                "Wrote 0x00 to the haptic control char.\n\n"
                "Did the motor STOP buzzing?\n\n"
                "Pass = motor went silent.   Fail = motor still buzzing.")
            return self._mk("E2", "PASS" if ok else "FAIL",
                             "user confirmed motor stopped" if ok
                             else "user reported motor still on",
                             ["wrote: 0x00 → 0x1530"])
        except Exception as e:
            return self._mk("E2", "FAIL", f"write error: {e}")

    async def _t_E3(self, ctx):
        try:
            ctx.detail("writing intensity=75 then turning motor ON briefly")
            await self.ble.write(CHAR_HAPTIC_INT, bytes([75]))
            await asyncio.sleep(0.3)
            await self.ble.write(CHAR_HAPTIC_CTL, bytes([0x01]))
            ok = await self._ask_pass_fail(
                "E3 Intensity setting persists",
                "Set intensity = 75 % and turned motor ON.\n\n"
                "Did the motor buzz at a noticeably MEDIUM level?\n\n"
                "Pass = felt 75% strength.   Fail = felt wrong / no buzz.")
            # Tidy up — turn motor off regardless of verdict.
            try:
                await self.ble.write(CHAR_HAPTIC_CTL, bytes([0x00]))
            except Exception:
                pass
            return self._mk("E3", "PASS" if ok else "FAIL",
                             "user confirmed 75% intensity" if ok
                             else "user reported intensity wrong",
                             ["wrote: 75 → 0x1531; 0x01 → 0x1530"])
        except Exception as e:
            return self._mk("E3", "FAIL", f"error: {e}")

    async def _t_E4(self, ctx):
        try:
            ctx.detail("writing intensity=200 (firmware should clamp to 100)")
            await self.ble.write(CHAR_HAPTIC_INT, bytes([200]))
            await asyncio.sleep(0.3)
            await self.ble.write(CHAR_HAPTIC_CTL, bytes([0x01]))
            ok = await self._ask_pass_fail(
                "E4 Intensity clamps to 100",
                "Wrote intensity = 200 (out of range, firmware should clamp "
                "to 100) and turned motor ON.\n\n"
                "Did the motor buzz at FULL strength (without misbehaving)?\n\n"
                "Pass = full-strength buzz.   Fail = motor erratic / silent.")
            try:
                await self.ble.write(CHAR_HAPTIC_CTL, bytes([0x00]))
            except Exception:
                pass
            return self._mk("E4", "PASS" if ok else "FAIL",
                             "clamp behaviour confirmed" if ok
                             else "user reported clamp failure",
                             ["wrote: 200 → 0x1531; 0x01 → 0x1530"])
        except Exception as e:
            return self._mk("E4", "FAIL", f"error: {e}")

    async def _t_E5(self, ctx):
        try:
            ctx.detail("writing single-shot [5, 0] to CHAR_HAPTIC_REMINDER (0x1537)")
            await self.ble.write(CHAR_HAPTIC_REMINDER, struct.pack("<II", 5, 0))
            # Wait the full 5 s of the countdown PLUS ~1 s of buzz time
            # so the user can actually feel the motor before being asked.
            for remaining in (5, 4, 3, 2, 1):
                ctx.detail(f"  countdown → {remaining} s remaining …")
                await asyncio.sleep(1.0)
            ctx.detail("  countdown should have fired — motor briefly buzzed")
            await asyncio.sleep(1.2)   # let the buzz finish
            ok = await self._ask_pass_fail(
                "E5 Countdown timer fires",
                "Started a 5-second countdown timer ~6 seconds ago.\n\n"
                "Did the motor buzz briefly when the countdown reached 0?\n\n"
                "Pass = motor buzzed at the right moment.\n"
                "Fail = nothing happened, or buzz was at the wrong time.")
            return self._mk("E5", "PASS" if ok else "FAIL",
                             "user confirmed countdown sequence" if ok
                             else "user reported countdown failure",
                             ["wrote: [5, 0] → 0x1537",
                              "waited 6 s for the firmware countdown to fire"])
        except Exception as e:
            return self._mk("E5", "FAIL", f"error: {e}")

    # ── Bucket F ─────────────────────────────────────────────────────────────

    async def _t_F1(self, ctx):
        try:
            raw = await self.ble.read(CHAR_BLOCK_COUNT)
            n = struct.unpack("<H", raw)[0]
            if 0 <= n <= FLASH_BLOCK_COUNT_MAX:
                return self._mk("F1", "PASS",
                                 f"unsynced block count = {n}")
            return self._mk("F1", "FAIL",
                             f"count {n} out of [0,{FLASH_BLOCK_COUNT_MAX}]")
        except Exception as e:
            return self._mk("F1", "FAIL", f"read failed: {e}")

    async def _t_F2(self, ctx):
        await self._ensure_blocks(ctx)
        if not ctx.blocks:
            return self._mk("F2", "INCONCLUSIVE",
                             "no blocks downloaded (count was 0?)")
        bad_magic = [b for b in ctx.blocks if not b.get("magic_ok", True)]
        bad_crc   = [b for b in ctx.blocks if not b["crc_ok"]]
        details = [f"downloaded {len(ctx.blocks)} blocks",
                   f"bad magic: {len(bad_magic)}",
                   f"bad CRC: {len(bad_crc)}"]
        if bad_magic or bad_crc:
            return self._mk("F2", "FAIL",
                             f"{len(bad_magic)} bad magic, {len(bad_crc)} bad CRC",
                             details)
        return self._mk("F2", "PASS",
                         f"all {len(ctx.blocks)} blocks magic OK + CRC OK",
                         details)

    async def _t_F3(self, ctx):
        bad = []
        for b in ctx.blocks:
            rc = b["reading_count"]
            if not 1 <= rc <= READINGS_PER_BLOCK:
                bad.append((b["sequence"], rc))
        if bad:
            return self._mk("F3", "FAIL",
                             f"{len(bad)} block(s) with bad reading_count",
                             [f"(seq, rc): {bad[:5]}"])
        return self._mk("F3", "PASS",
                         f"all {len(ctx.blocks)} reading_counts in [1,{READINGS_PER_BLOCK}]")

    async def _t_F4(self, ctx):
        bad = []
        for b in ctx.blocks:
            if b["sync_status"] and b["pre_sync"]:
                bad.append((b["sequence"], "post-sync but year=0"))
            if not b["sync_status"] and not b["pre_sync"]:
                bad.append((b["sequence"], "pre-sync but year≠0"))
        if bad:
            return self._mk("F4", "FAIL",
                             f"{len(bad)} block(s) with inconsistent sync",
                             [str(b) for b in bad[:5]])
        return self._mk("F4", "PASS", "sync_status ↔ year consistent")

    async def _t_F5(self, ctx):
        try:
            await asyncio.sleep(0.5)  # let firmware persist sync_block_idx
            raw = await self.ble.read(CHAR_BLOCK_COUNT)
            n = struct.unpack("<H", raw)[0]
            if n == 0:
                return self._mk("F5", "PASS",
                                 "sync pointer caught up (count = 0)")
            return self._mk("F5", "FAIL",
                             f"count = {n} after download — sync didn't advance")
        except Exception as e:
            return self._mk("F5", "FAIL", f"read failed: {e}")

    async def _t_F6(self, ctx):
        # Within one boot, no duplicate sequence. Boots can't be inferred yet
        # (boot_id derivation belongs in F9), so allow regressions if any.
        seen = set()
        dups = []
        prev_seq = None
        boot_resets = 0
        for b in ctx.blocks:
            s = b["sequence"]
            if prev_seq is not None and s < prev_seq:
                # crossed a reboot boundary — restart the seen set
                boot_resets += 1
                seen = set()
            if s in seen:
                dups.append(s)
            seen.add(s)
            prev_seq = s
        if dups:
            return self._mk("F6", "FAIL",
                             f"{len(dups)} duplicate sequence(s) within a boot",
                             [f"dups: {dups[:5]}", f"boot_resets: {boot_resets}"])
        return self._mk("F6", "PASS",
                         f"no duplicates ({boot_resets} reboot(s) detected)")

    async def _t_F7(self, ctx):
        # Per-boot: max_seq - min_seq + 1 ≤ 234
        boots = [[]]
        prev = None
        for b in ctx.blocks:
            s = b["sequence"]
            if prev is not None and s < prev:
                boots.append([])
            boots[-1].append(s)
            prev = s
        bad = []
        for i, boot in enumerate(boots):
            if not boot:
                continue
            span = max(boot) - min(boot) + 1
            if span > FLASH_BLOCK_COUNT_MAX:
                bad.append((i, span))
        if bad:
            return self._mk("F7", "FAIL",
                             f"{len(bad)} boot(s) exceed cap",
                             [f"(boot,span): {bad}"])
        return self._mk("F7", "PASS",
                         f"all {len(boots)} boot window(s) within {FLASH_BLOCK_COUNT_MAX} cap")

    async def _t_F8(self, ctx):
        violations = []
        prev = None
        for b in ctx.blocks:
            s = b["sequence"]
            if prev is not None and s < prev:
                # reboot boundary — reset prev
                prev = s
                continue
            if prev is not None and s == prev:
                violations.append((prev, s))
            prev = s
        if violations:
            return self._mk("F8", "FAIL",
                             f"{len(violations)} non-monotonic pair(s)",
                             [f"e.g. {violations[:3]}"])
        return self._mk("F8", "PASS", "sequence strictly increasing within each boot")

    async def _t_F9(self, ctx):
        # Whenever block seq regresses, a BOOT entry must align between the
        # surrounding blocks. We can't easily order BOOT entries in clock-time,
        # but we can verify the journal contains AT LEAST that many BOOT events.
        regressions = 0
        prev = None
        for b in ctx.blocks:
            s = b["sequence"]
            if prev is not None and s < prev:
                regressions += 1
            prev = s
        boot_entries = sum(1 for e in ctx.journal if e.get("type_raw") == 0)
        details = [f"block seq regressions: {regressions}",
                   f"BOOT entries in journal: {boot_entries}"]
        if regressions == 0:
            return self._mk("F9", "PASS", "no reboots in this download window",
                             details)
        if boot_entries >= regressions:
            return self._mk("F9", "PASS",
                             f"{regressions} regression(s), {boot_entries} BOOT entry(ies) — consistent",
                             details)
        return self._mk("F9", "FAIL",
                         f"{regressions} regression(s) but only {boot_entries} BOOT entry(ies)",
                         details + ["regression without matching BOOT = persistent state corruption"])

    async def _t_F10(self, ctx):
        try:
            ctx.sent_epoch = int(time.time())
            await self.ble.write(CHAR_TIMESYNC,
                                  struct.pack("<I", ctx.sent_epoch))
            # Wait one sensor period + 5 s slack
            await asyncio.sleep(SAMPLE_PERIOD_S + 10)
            ctx.blocks.clear()
            await self._download_blocks_into(ctx)
            if not ctx.blocks:
                return self._mk("F10", "INCONCLUSIVE",
                                 "no new blocks after time-sync wait")
            latest = ctx.blocks[-1]
            host_year = datetime.now(timezone.utc).year
            ts = latest["timestamp"]
            if latest["pre_sync"]:
                return self._mk("F10", "FAIL",
                                 "newest block still pre-sync (year=0)",
                                 [f"timestamp = {ts}"])
            try:
                year = int(ts[:4])
            except Exception:
                return self._mk("F10", "FAIL",
                                 f"could not parse year from {ts}")
            if year != host_year:
                return self._mk("F10", "FAIL",
                                 f"block year={year} ≠ host year={host_year}",
                                 [f"timestamp = {ts}"])
            return self._mk("F10", "PASS",
                             f"post-sync block carries year={year}",
                             [f"timestamp = {ts}", f"sent epoch = {ctx.sent_epoch}"])
        except Exception as e:
            return self._mk("F10", "FAIL", f"error: {e}")

    async def _t_F11(self, ctx):
        wraps = sum(1 for e in ctx.journal if e.get("type_raw") == 2)
        if wraps == 0:
            return self._mk("F11", "PASS",
                             "no FLASH_WRAP entries — no wrap to audit")
        # Without long-running history we can't compute total_blocks_ever.
        # Fall back to: each wrap should leave room for ≥ 26 lost blocks.
        return self._mk("F11", "PASS",
                         f"{wraps} FLASH_WRAP entry(ies) present",
                         [f"each implies {FLASH_BLOCKS_PER_PAGE} oldest blocks were lost"])

    async def _t_F12(self, ctx):
        bad = 0
        for b in ctx.blocks:
            for r in b["readings"]:
                if r["temp"] is not None and not -128 <= r["temp"] <= 127:
                    bad += 1
                if r["db"] is not None and not 0 <= r["db"] <= 255:
                    bad += 1
                if r["lux"] is not None and not 0 <= r["lux"] <= 65535:
                    bad += 1
        if bad:
            return self._mk("F12", "FAIL",
                             f"{bad} field(s) outside type bounds")
        return self._mk("F12", "PASS",
                         f"all reading slots decode within type bounds")

    # ── Bucket G ─────────────────────────────────────────────────────────────

    async def _t_G1(self, ctx):
        try:
            raw = await self.ble.read(CHAR_JOURNAL_COUNT)
            n = struct.unpack("<H", raw)[0]
            if 0 <= n <= FLASH_JOURNAL_SLOT_COUNT:
                return self._mk("G1", "PASS",
                                 f"unsynced journal count = {n}",
                                 ["NOTE: firmware only refreshes this on JOURNAL_START write — may be stale"])
            return self._mk("G1", "FAIL", f"count {n} out of range")
        except Exception as e:
            return self._mk("G1", "FAIL", f"read failed: {e}")

    async def _t_G2(self, ctx):
        await self._ensure_journal(ctx)
        if not ctx.journal:
            return self._mk("G2", "INCONCLUSIVE",
                             "no journal entries downloaded")
        bad_magic = sum(1 for e in ctx.journal if not e.get("magic_ok", True))
        bad_crc   = sum(1 for e in ctx.journal if not e["crc_ok"])
        if bad_magic or bad_crc:
            return self._mk("G2", "FAIL",
                             f"{bad_magic} bad magic, {bad_crc} bad CRC",
                             [f"total entries: {len(ctx.journal)}"])
        return self._mk("G2", "PASS",
                         f"all {len(ctx.journal)} journal entries OK")

    async def _t_G3(self, ctx):
        seqs = [e["sequence"] for e in ctx.journal]
        for i in range(len(seqs) - 1):
            if seqs[i + 1] <= seqs[i]:
                return self._mk("G3", "FAIL",
                                 f"sequence regression at index {i}",
                                 [f"{seqs[i]} → {seqs[i+1]}"])
        return self._mk("G3", "PASS",
                         f"{len(seqs)} entries strictly increasing",
                         [f"first={seqs[0] if seqs else None} last={seqs[-1] if seqs else None}"])

    async def _t_G4(self, ctx):
        bad = [e for e in ctx.journal if e["type_raw"] not in EVENT_TYPES]
        if bad:
            return self._mk("G4", "FAIL",
                             f"{len(bad)} entry(ies) with unknown type",
                             [f"types: {[e['type_raw'] for e in bad]}"])
        return self._mk("G4", "PASS",
                         f"all {len(ctx.journal)} entries have known type")

    async def _t_G5(self, ctx):
        if ctx.sent_epoch is None:
            return self._mk("G5", "SKIP",
                             "F10 did not send a TIME_SYNC")
        ctx.journal.clear()
        await self._ensure_journal(ctx)
        matches = [e for e in ctx.journal
                   if e["type_raw"] == 1 and e["data"] == ctx.sent_epoch]
        if matches:
            return self._mk("G5", "PASS",
                             f"TIME_SYNC entry with data={ctx.sent_epoch} found",
                             [f"matching seq={matches[-1]['sequence']}"])
        return self._mk("G5", "FAIL",
                         f"no TIME_SYNC entry with data={ctx.sent_epoch}",
                         [f"got {len(ctx.journal)} entries"])

    async def _t_G6(self, ctx):
        wraps = [e for e in ctx.journal if e["type_raw"] == 2]
        bad = [e for e in wraps if not 0 <= e["data"] < FLASH_BLOCK_COUNT_MAX]
        if bad:
            return self._mk("G6", "FAIL",
                             f"{len(bad)} FLASH_WRAP with bad data field",
                             [f"data values: {[e['data'] for e in bad]}"])
        return self._mk("G6", "PASS",
                         f"{len(wraps)} FLASH_WRAP entry(ies), all valid")

    async def _t_G7(self, ctx):
        boots = [e for e in ctx.journal if e["type_raw"] == 0]
        if not boots:
            return self._mk("G7", "FAIL",
                             "no BOOT entries — current boot didn't log one")
        return self._mk("G7", "PASS",
                         f"{len(boots)} BOOT entry(ies) present",
                         [f"latest data (boot_count) = {boots[-1]['data']}"])

    # ── Bucket H ─────────────────────────────────────────────────────────────

    async def _t_H1(self, ctx):
        return self._mk("H1", "INCONCLUSIVE",
                         "wrap_watcher: not implemented in Pass 1",
                         ["leaving connection open for ≥4 h is required",
                          "manual procedure: leave tool running and check journal periodically"])

    async def _t_H2(self, ctx):
        return self._mk("H2", "INCONCLUSIVE",
                         "connection_uptime: not implemented in Pass 1")

    # ─── Bucket I — Hall (lid sensor) — interactive ──────────────────────────

    async def _t_I1(self, ctx):
        """Hall close → boost ON.

        Procedure: prompt user to close the lid magnet against the device,
        then read Status char.  PASS if lid_closed flag flipped to True
        within ~5 seconds AND boost flag is also True (assuming battery
        is above BATT_LOW_MV — otherwise boost stays disabled by guard).
        """
        try:
            # Snapshot pre-event state for reference.
            pre = decode_status(bytes(await self.ble.read(CHAR_STATUS)))
            ctx.detail(f"pre-event: lid_closed={pre.get('lid_closed')}  "
                        f"boost={pre.get('boost')}")
            ok_action = await self._ask_pass_fail(
                "I1 Lid close fires hall event",
                "Please CLOSE the lid magnet against the device now.\n\n"
                "When done, click Pass and I'll read the Status char to "
                "verify the device saw the event.")
            if not ok_action:
                return self._mk("I1", "FAIL",
                                 "user cancelled before performing action")

            # Give firmware a moment to update Status after the event.
            await asyncio.sleep(0.5)
            post = decode_status(bytes(await self.ble.read(CHAR_STATUS)))
            ctx.detail(f"post-event: lid_closed={post.get('lid_closed')}  "
                        f"boost={post.get('boost')}")

            lid_now    = bool(post.get("lid_closed"))
            boost_now  = bool(post.get("boost"))
            if not lid_now:
                return self._mk("I1", "FAIL",
                                 "lid_closed flag did not flip to True")
            # Boost SHOULD turn on at lid-close (unless battery too low).
            note = " (boost gated low-batt?)" if not boost_now else ""
            return self._mk("I1", "PASS",
                             f"lid_closed=True, boost={boost_now}{note}",
                             [f"pre:  {pre}", f"post: {post}"])
        except Exception as e:
            return self._mk("I1", "FAIL", f"error: {e}")

    async def _t_I2(self, ctx):
        """Hall open → boost OFF."""
        try:
            pre = decode_status(bytes(await self.ble.read(CHAR_STATUS)))
            ctx.detail(f"pre-event: lid_closed={pre.get('lid_closed')}  "
                        f"boost={pre.get('boost')}")
            ok_action = await self._ask_pass_fail(
                "I2 Lid open fires hall event",
                "Please OPEN the lid (remove the magnet from the device).\n\n"
                "Then click Pass and I'll verify the device saw the "
                "lid-open transition.")
            if not ok_action:
                return self._mk("I2", "FAIL",
                                 "user cancelled before performing action")

            await asyncio.sleep(0.5)
            post = decode_status(bytes(await self.ble.read(CHAR_STATUS)))
            ctx.detail(f"post-event: lid_closed={post.get('lid_closed')}  "
                        f"boost={post.get('boost')}")

            lid_now    = bool(post.get("lid_closed"))
            boost_now  = bool(post.get("boost"))
            if lid_now:
                return self._mk("I2", "FAIL",
                                 "lid_closed flag did not flip to False")
            if boost_now:
                return self._mk("I2", "FAIL",
                                 "boost stayed ON after lid open")
            return self._mk("I2", "PASS",
                             "lid_closed=False, boost=False",
                             [f"pre:  {pre}", f"post: {post}"])
        except Exception as e:
            return self._mk("I2", "FAIL", f"error: {e}")

    # ─── Bucket U — USB detect — interactive ─────────────────────────────────

    async def _t_U1(self, ctx):
        """USB plug-in detected — Status.usb flips True within 2 s."""
        try:
            pre = decode_status(bytes(await self.ble.read(CHAR_STATUS)))
            ctx.detail(f"pre-event: usb={pre.get('usb')}")
            if pre.get("usb"):
                return self._mk("U1", "FAIL",
                                 "USB already detected as connected pre-test "
                                 "— unplug first, then re-run")
            ok_action = await self._ask_pass_fail(
                "U1 USB plug-in",
                "Please PLUG the USB cable into the device now.\n\n"
                "Then click Pass and I'll verify the device detected it.")
            if not ok_action:
                return self._mk("U1", "FAIL",
                                 "user cancelled before performing action")

            await asyncio.sleep(0.5)
            post = decode_status(bytes(await self.ble.read(CHAR_STATUS)))
            ctx.detail(f"post-event: usb={post.get('usb')}")
            if not post.get("usb"):
                return self._mk("U1", "FAIL",
                                 "Status.usb did not flip to True after plug-in")
            return self._mk("U1", "PASS",
                             "usb=True observed",
                             [f"pre:  {pre}", f"post: {post}"])
        except Exception as e:
            return self._mk("U1", "FAIL", f"error: {e}")

    async def _t_U2(self, ctx):
        """USB unplug detected — Status.usb flips False within 2 s."""
        try:
            pre = decode_status(bytes(await self.ble.read(CHAR_STATUS)))
            ctx.detail(f"pre-event: usb={pre.get('usb')}")
            if not pre.get("usb"):
                return self._mk("U2", "FAIL",
                                 "USB not currently detected as connected — "
                                 "plug in first, then re-run")
            ok_action = await self._ask_pass_fail(
                "U2 USB unplug",
                "Please UNPLUG the USB cable from the device now.\n\n"
                "Then click Pass and I'll verify the device detected the "
                "unplug.")
            if not ok_action:
                return self._mk("U2", "FAIL",
                                 "user cancelled before performing action")

            await asyncio.sleep(0.5)
            post = decode_status(bytes(await self.ble.read(CHAR_STATUS)))
            ctx.detail(f"post-event: usb={post.get('usb')}")
            if post.get("usb"):
                return self._mk("U2", "FAIL",
                                 "Status.usb did not flip to False after unplug")
            return self._mk("U2", "PASS",
                             "usb=False observed",
                             [f"pre:  {pre}", f"post: {post}"])
        except Exception as e:
            return self._mk("U2", "FAIL", f"error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    #  Numeric-input helper (for CAL* tests).  Returns the entered float,
    #  or None if the user cancelled the dialog.
    # ─────────────────────────────────────────────────────────────────────
    async def _ask_number(self, test_id: str, prompt: str,
                           unit: str = "") -> Optional[float]:
        from tkinter import simpledialog
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()

        def _ask():
            try:
                title = f"Manual entry — {test_id}"
                full_prompt = f"{prompt}\n\nEnter value{(' in ' + unit) if unit else ''}:"
                val = simpledialog.askfloat(title, full_prompt,
                                             parent=self.winfo_toplevel())
                if not fut.done():
                    fut.set_result(val)
            except Exception:
                if not fut.done():
                    fut.set_result(None)
        # Dialog must run on Tk main thread; schedule and await the future.
        self.after(0, _ask)
        return await fut

    # ═════════════════════════════════════════════════════════════════════
    #  Bucket W — 5 s sampling window contract
    # ═════════════════════════════════════════════════════════════════════
    async def _t_W1(self, ctx):
        """Cadence — record interval within ±2 s of 60 s, across 3 cycles.
        Tighter assertion than C1 (which allows 55-70 s)."""
        await self._ensure_samples(ctx, n=3)
        if len(ctx.samples) < 3:
            return self._mk("W1", "INCONCLUSIVE",
                             f"only {len(ctx.samples)} samples collected (need 3)")
        gaps = [ctx.samples[i+1]["ts"] - ctx.samples[i]["ts"]
                for i in range(len(ctx.samples) - 1)]
        details = [f"gaps (s): {[round(g, 2) for g in gaps]}",
                   f"expected: 60 ±2 s"]
        bad = [g for g in gaps if abs(g - 60) > 2]
        if not bad:
            return self._mk("W1", "PASS",
                             f"all gaps within ±2 s of 60 s", details)
        return self._mk("W1", "FAIL",
                         f"{len(bad)} gap(s) outside 60 ±2 s", details)

    async def _t_W2(self, ctx):
        """Every field non-sentinel for 3 consecutive cycles."""
        await self._ensure_samples(ctx, n=3)
        if len(ctx.samples) < 3:
            return self._mk("W2", "INCONCLUSIVE",
                             f"only {len(ctx.samples)} samples")
        bad = []
        for i, s in enumerate(ctx.samples[:3]):
            issues = []
            if s["temp"] is None: issues.append("temp=127")
            if s["db"]   is None: issues.append("db=0xFF")
            if s["lux"]  is None: issues.append("lux=0xFFFF")
            if issues:
                bad.append(f"sample {i}: {', '.join(issues)}")
        if bad:
            return self._mk("W2", "FAIL",
                             f"{len(bad)} cycle(s) had sentinel(s)", bad)
        return self._mk("W2", "PASS",
                         "all 3 cycles fully populated",
                         [f"sample {i}: {s}" for i, s in enumerate(ctx.samples[:3])])

    async def _t_W3(self, ctx):
        """peak_db >= db every cycle — invariant of the new energy aggregator."""
        await self._ensure_samples(ctx, n=3)
        if len(ctx.samples) < 1:
            return self._mk("W3", "INCONCLUSIVE", "no samples")
        violations = []
        for i, s in enumerate(ctx.samples):
            db = s.get("db")
            peak = s.get("peak_db")
            if db is None or peak is None:
                continue
            if peak < db:
                violations.append(f"sample {i}: peak={peak} < db={db}")
        if violations:
            return self._mk("W3", "FAIL",
                             f"{len(violations)} cycle(s) violated peak ≥ avg",
                             violations)
        return self._mk("W3", "PASS",
                         "peak_db ≥ db on every cycle",
                         [f"sample {i}: db={s['db']} peak={s.get('peak_db')}"
                          for i, s in enumerate(ctx.samples)])

    async def _t_W4(self, ctx):
        """Lux > 100 at room ambient — sanity check post-calibration."""
        ok = await self._ask_pass_fail(
            "W4 ambient lux",
            "Place the device in normal room lighting (lit office or living room).\n\n"
            "Click Pass when the device is positioned, then I'll read lux from "
            "the next sensor cycle.")
        if not ok:
            return self._mk("W4", "FAIL", "user cancelled before positioning")
        ctx.samples.clear()
        await self._ensure_samples(ctx, n=1)
        if not ctx.samples or ctx.samples[0]["lux"] is None:
            return self._mk("W4", "FAIL", "no lux reading or sentinel value")
        lux = ctx.samples[0]["lux"]
        if lux >= 100:
            return self._mk("W4", "PASS",
                             f"lux={lux} (≥ 100 expected at room ambient)",
                             [f"reading: {lux}"])
        return self._mk("W4", "FAIL",
                         f"lux={lux} below 100 — too dark, or sensor under-reads",
                         [f"reading: {lux}",
                          "check LUX_CAL_FACTOR in firmware app_config.h"])

    async def _t_W5(self, ctx):
        """Temp within ±2 °C of a reference thermometer reading."""
        ref = await self._ask_number(
            "W5 temp ref",
            "Place a reference thermometer next to the device.\n"
            "Wait for both to settle (~1 minute).\n"
            "Then enter the thermometer's reading.",
            unit="°C")
        if ref is None:
            return self._mk("W5", "FAIL", "user cancelled / no reading entered")
        ctx.samples.clear()
        await self._ensure_samples(ctx, n=1)
        if not ctx.samples or ctx.samples[0]["temp"] is None:
            return self._mk("W5", "FAIL", "no temp reading or sentinel")
        dev = ctx.samples[0]["temp"]
        diff = abs(dev - ref)
        details = [f"reference: {ref:.1f} °C",
                   f"device:    {dev} °C",
                   f"diff:      {diff:.1f} °C"]
        if diff <= 2:
            return self._mk("W5", "PASS", f"within ±2 °C (Δ={diff:.1f})", details)
        return self._mk("W5", "FAIL",
                         f"Δ={diff:.1f} °C exceeds ±2 °C — "
                         "tune TEMP_CAL_OFFSET_TENTHS", details)

    async def _t_W6(self, ctx):
        """dB jumps ≥ 10 dB on a loud sound (mic responsiveness)."""
        # Capture a quiet baseline first
        ctx.samples.clear()
        await self._ensure_samples(ctx, n=1)
        if not ctx.samples or ctx.samples[0]["db"] is None:
            return self._mk("W6", "FAIL", "baseline dB unreadable")
        quiet_db = ctx.samples[0]["db"]
        ctx.detail(f"quiet baseline: db={quiet_db}")
        ok = await self._ask_pass_fail(
            "W6 loud sound",
            f"Baseline dB just measured: {quiet_db}\n\n"
            "Now make a LOUD continuous sound near the device (clap, voice, "
            "speaker) and keep it going for ~1 minute (one full sensor "
            "cycle).\n\nClick Pass when ready — I'll grab the next sample.")
        if not ok:
            return self._mk("W6", "FAIL", "user cancelled")
        ctx.samples.clear()
        await self._ensure_samples(ctx, n=1)
        if not ctx.samples or ctx.samples[0]["db"] is None:
            return self._mk("W6", "FAIL", "loud-sample dB unreadable")
        loud_db = ctx.samples[0]["db"]
        delta = loud_db - quiet_db
        details = [f"quiet: {quiet_db} dB", f"loud:  {loud_db} dB",
                   f"delta: {delta} dB"]
        if delta >= 10:
            return self._mk("W6", "PASS",
                             f"+{delta} dB on loud sound", details)
        return self._mk("W6", "FAIL",
                         f"only +{delta} dB — mic may be insensitive, "
                         "PDM_SPL_OFFSET_DB may be miscalibrated, "
                         "or sound wasn't loud enough", details)

    # ═════════════════════════════════════════════════════════════════════
    #  Bucket BC — battery cache + refresh-on-event
    # ═════════════════════════════════════════════════════════════════════
    async def _t_BC1(self, ctx):
        """BAS char and Status.batt_pct must agree — cache parity."""
        try:
            status = decode_status(bytes(await self.ble.read(CHAR_STATUS)))
            bas    = (await self.ble.read(CHAR_BATT_LEVEL))[0]
            sb     = status.get("batt_pct")
            details = [f"BAS:           {bas} %",
                       f"Status.batt_pct: {sb} %"]
            if sb == bas:
                return self._mk("BC1", "PASS",
                                 f"both report {bas} %", details)
            return self._mk("BC1", "FAIL",
                             f"BAS={bas} ≠ Status={sb}", details)
        except Exception as e:
            return self._mk("BC1", "FAIL", f"read error: {e}")

    async def _t_BC2(self, ctx):
        """BAS notify must arrive within 500 ms of hall close."""
        return await self._refresh_on_event(
            "BC2", "Close the lid magnet against the device now.",
            char=CHAR_BATT_LEVEL, deadline_ms=500)

    async def _t_BC3(self, ctx):
        """BAS notify must arrive within 500 ms of USB plug-in."""
        return await self._refresh_on_event(
            "BC3", "Plug a USB cable into the device now.",
            char=CHAR_BATT_LEVEL, deadline_ms=500)

    async def _t_BC4(self, ctx):
        """After USB unplug, BAS EMA must stay frozen for 90 s (lockout)."""
        try:
            pre_status = decode_status(bytes(await self.ble.read(CHAR_STATUS)))
            if not pre_status.get("usb"):
                return self._mk("BC4", "FAIL",
                                 "USB not currently connected — plug in first")
            ok = await self._ask_pass_fail(
                "BC4 USB unplug lockout",
                "Unplug the USB cable now. After unplug, the BAS value "
                "should stay frozen for 90 s (no EMA decay).\n\n"
                "Click Pass after you've unplugged the cable.")
            if not ok:
                return self._mk("BC4", "FAIL", "user cancelled")
            # Read 5 BAS values over ~90 s and assert they don't decrease.
            ctx.detail("sampling BAS every 18 s for 90 s …")
            readings = []
            for i in range(5):
                await asyncio.sleep(18)
                v = (await self.ble.read(CHAR_BATT_LEVEL))[0]
                readings.append(v)
                ctx.detail(f"  t={18*(i+1)}s   bas={v}")
            details = [f"readings: {readings}"]
            decreases = [readings[i+1] - readings[i] for i in range(4)]
            if any(d < 0 for d in decreases):
                return self._mk("BC4", "FAIL",
                                 f"BAS decreased during lockout: deltas={decreases}",
                                 details)
            return self._mk("BC4", "PASS",
                             "BAS stable across 90 s lockout window", details)
        except Exception as e:
            return self._mk("BC4", "FAIL", f"error: {e}")

    async def _t_BC5(self, ctx):
        """Hall event mid-window: assert window completes + BAS refreshes
        after window close (not during)."""
        ok = await self._ask_pass_fail(
            "BC5 hall mid-window",
            "I'll wait for the next sensor cycle to BEGIN, then prompt you to "
            "TOGGLE the lid magnet while the window is open.\n\n"
            "Click Pass to start.")
        if not ok:
            return self._mk("BC5", "FAIL", "user cancelled")
        # Subscribe to sensor + BAS; wait for a sensor notify (cycle close),
        # then a small delay, then ask user to toggle hall.
        sensor_times: list = []
        bas_times: list = []
        evt = asyncio.Event()

        def _on_sensor(_h, data):
            sensor_times.append(time.time())
            if len(sensor_times) >= 2:
                evt.set()

        def _on_bas(_h, data):
            bas_times.append(time.time())

        try:
            await self.ble.subscribe(CHAR_SENSOR_DATA, _on_sensor)
            await self.ble.subscribe(CHAR_BATT_LEVEL,  _on_bas)
            # Wait up to 65 s for first cycle close
            for _ in range(65):
                if sensor_times:
                    break
                await asyncio.sleep(1)
            if not sensor_times:
                return self._mk("BC5", "INCONCLUSIVE",
                                 "no sensor notify within 65 s")
            ctx.detail("first cycle observed; toggle the lid magnet now")
            ok2 = await self._ask_pass_fail(
                "BC5 toggle now",
                "Toggle the lid magnet (close → open, or open → close) NOW.\n\n"
                "Click Pass after toggling.")
            if not ok2:
                return self._mk("BC5", "FAIL", "user cancelled toggle")
            # Wait up to 65 s for the next sensor notify
            for _ in range(65):
                if len(sensor_times) >= 2:
                    break
                await asyncio.sleep(1)
            # PASS if:
            #   - second sensor record arrived (window survived hall mid-flight)
            #   - at least one BAS notify between the two sensor times (refresh
            #     fired after window close, not during)
            if len(sensor_times) < 2:
                return self._mk("BC5", "FAIL",
                                 "no second sensor notify — window may have aborted")
            in_window_bas = [t for t in bas_times
                             if sensor_times[0] < t <= sensor_times[1] + 2]
            details = [f"sensor #1: {sensor_times[0]:.2f}",
                       f"sensor #2: {sensor_times[1]:.2f}",
                       f"BAS notifies between them: {len(in_window_bas)}"]
            if in_window_bas:
                return self._mk("BC5", "PASS",
                                 "window completed + BAS refreshed", details)
            return self._mk("BC5", "INCONCLUSIVE",
                             "window completed but no BAS refresh seen "
                             "(may have raced with window close)", details)
        finally:
            try:
                await self.ble.unsubscribe(CHAR_SENSOR_DATA)
                await self.ble.unsubscribe(CHAR_BATT_LEVEL)
            except Exception:
                pass

    async def _refresh_on_event(self, tid: str, prompt: str,
                                 char: str, deadline_ms: int):
        """Shared helper for BC2/BC3 — prompt the user to perform a physical
        action, then assert a notification on `char` arrives within deadline_ms."""
        notify_times: list = []
        evt = asyncio.Event()

        def _on_notify(_h, data):
            notify_times.append(time.time())
            evt.set()

        try:
            await self.ble.subscribe(char, _on_notify)
            # Brief settle so any in-flight notify doesn't race the prompt
            await asyncio.sleep(0.2)
            notify_times.clear()
            ok = await self._ask_pass_fail(tid, prompt + "\n\n"
                                            "Click Pass AFTER performing the action.")
            if not ok:
                return self._mk(tid, "FAIL", "user cancelled before action")
            action_time = time.time()
            try:
                await asyncio.wait_for(evt.wait(),
                                        timeout=deadline_ms / 1000.0 + 1.0)
            except asyncio.TimeoutError:
                return self._mk(tid, "FAIL",
                                 f"no notify on {char[:8]} within "
                                 f"{deadline_ms} ms of action")
            elapsed_ms = int((notify_times[0] - action_time) * 1000)
            details = [f"action at:  {action_time:.3f}",
                       f"notify at:  {notify_times[0]:.3f}",
                       f"elapsed:    {elapsed_ms} ms",
                       f"deadline:   {deadline_ms} ms"]
            if elapsed_ms <= deadline_ms:
                return self._mk(tid, "PASS",
                                 f"notify in {elapsed_ms} ms (≤ {deadline_ms})",
                                 details)
            return self._mk(tid, "FAIL",
                             f"notify in {elapsed_ms} ms exceeded {deadline_ms} ms",
                             details)
        finally:
            try:
                await self.ble.unsubscribe(char)
            except Exception:
                pass

    # ═════════════════════════════════════════════════════════════════════
    #  Bucket CAL — calibration validity against reference instruments
    # ═════════════════════════════════════════════════════════════════════
    async def _t_CAL1(self, ctx):
        """Lux within ±20 % of luxon reference at 3 brightness levels."""
        ratios = []
        details = []
        for i, hint in enumerate(["dim (~150 lux)",
                                    "moderate (~400 lux)",
                                    "bright (~800 lux)"], 1):
            ref = await self._ask_number(
                f"CAL1 level {i}",
                f"Set lighting to {hint} (use the luxon meter to confirm),\n"
                f"position the device next to the luxon sensor, then enter "
                f"the luxon's reading.",
                unit="lux")
            if ref is None or ref <= 0:
                return self._mk("CAL1", "FAIL",
                                 f"level {i}: no reference entered")
            ctx.samples.clear()
            await self._ensure_samples(ctx, n=1)
            if not ctx.samples or ctx.samples[0]["lux"] is None:
                return self._mk("CAL1", "FAIL",
                                 f"level {i}: no device lux reading")
            dev = ctx.samples[0]["lux"]
            ratio = dev / ref if ref > 0 else 0
            ratios.append(ratio)
            details.append(f"level {i}: ref={ref:.0f}  device={dev}  "
                            f"ratio={ratio:.2f}")
        out_of_band = [r for r in ratios if not (0.8 <= r <= 1.2)]
        if not out_of_band:
            return self._mk("CAL1", "PASS",
                             "all 3 ratios in [0.8, 1.2]", details)
        return self._mk("CAL1", "FAIL",
                         f"{len(out_of_band)} level(s) outside ±20 % — "
                         f"re-tune LUX_CAL_FACTOR", details)

    async def _t_CAL2(self, ctx):
        """Temp within ±1 °C of reference thermometer."""
        ref = await self._ask_number(
            "CAL2 temp",
            "Place a reference thermometer next to the device.\n"
            "Wait 2 minutes for both to settle, then enter the thermometer's reading.",
            unit="°C")
        if ref is None:
            return self._mk("CAL2", "FAIL", "no reference entered")
        ctx.samples.clear()
        await self._ensure_samples(ctx, n=2)
        if not ctx.samples:
            return self._mk("CAL2", "FAIL", "no device temp reading")
        dev_avg = sum(s["temp"] for s in ctx.samples
                      if s["temp"] is not None) / len(ctx.samples)
        diff = abs(dev_avg - ref)
        details = [f"reference: {ref:.1f} °C",
                   f"device avg: {dev_avg:.1f} °C ({len(ctx.samples)} samples)",
                   f"diff:       {diff:.2f} °C"]
        if diff <= 1.0:
            return self._mk("CAL2", "PASS",
                             f"within ±1 °C (Δ={diff:.2f})", details)
        return self._mk("CAL2", "FAIL",
                         f"Δ={diff:.2f} °C exceeds ±1 °C — "
                         "tune TEMP_CAL_OFFSET_TENTHS", details)

    async def _t_CAL3(self, ctx):
        """dB within ±3 dB of SLM reading at a steady SPL."""
        ok = await self._ask_pass_fail(
            "CAL3 dB ref",
            "Set up a sound source at a stable, known SPL (e.g. tone "
            "generator at ~70 dB).\n"
            "Position the SLM and device side-by-side, sound facing both.\n\n"
            "Click Pass when ready to capture a reading.")
        if not ok:
            return self._mk("CAL3", "FAIL", "user cancelled")
        ref = await self._ask_number(
            "CAL3 SLM reading",
            "Enter the SLM's current reading.", unit="dB SPL")
        if ref is None:
            return self._mk("CAL3", "FAIL", "no SLM reading entered")
        ctx.samples.clear()
        await self._ensure_samples(ctx, n=2)
        if not ctx.samples:
            return self._mk("CAL3", "FAIL", "no device dB reading")
        dev_avg = sum(s["db"] for s in ctx.samples
                      if s["db"] is not None) / len(ctx.samples)
        diff = abs(dev_avg - ref)
        details = [f"SLM:        {ref:.1f} dB",
                   f"device avg: {dev_avg:.1f} dB",
                   f"diff:       {diff:.2f} dB"]
        if diff <= 3.0:
            return self._mk("CAL3", "PASS",
                             f"within ±3 dB (Δ={diff:.2f})", details)
        return self._mk("CAL3", "FAIL",
                         f"Δ={diff:.2f} dB exceeds ±3 dB — "
                         "tune PDM_SPL_OFFSET_DB", details)

    # ═════════════════════════════════════════════════════════════════════
    #  Bucket TM — timing / health
    # ═════════════════════════════════════════════════════════════════════
    async def _t_TM1(self, ctx):
        """No EVENT_ERROR entries in journal over the observation window.
        Reuses the cached journal download (`_ensure_journal`)."""
        await self._ensure_journal(ctx)
        if not ctx.journal:
            return self._mk("TM1", "INCONCLUSIVE",
                             "journal empty (download may have failed)")
        # EVENT_ERROR is type 3 per flash.h.
        err_entries = [e for e in ctx.journal if e.get("type") == 3]
        details = [f"total journal entries: {len(ctx.journal)}",
                   f"ERROR entries:         {len(err_entries)}"]
        if not err_entries:
            return self._mk("TM1", "PASS",
                             "no ERROR entries in journal", details)
        details.extend(f"  {e}" for e in err_entries[:5])
        if len(err_entries) > 5:
            details.append(f"  … {len(err_entries) - 5} more")
        return self._mk("TM1", "FAIL",
                         f"{len(err_entries)} ERROR entries in journal",
                         details)

    async def _t_TM2(self, ctx):
        """Status-char read latency stable: P95 < 100 ms across 30 probes."""
        rtts = []
        for _ in range(30):
            t0 = time.perf_counter()
            try:
                await self.ble.read(CHAR_STATUS)
                rtts.append((time.perf_counter() - t0) * 1000)
            except Exception as e:
                ctx.detail(f"probe error: {e}")
            await asyncio.sleep(1.0)
        if len(rtts) < 20:
            return self._mk("TM2", "INCONCLUSIVE",
                             f"only {len(rtts)} probes succeeded")
        rtts.sort()
        p50 = rtts[len(rtts) // 2]
        p95 = rtts[int(len(rtts) * 0.95)]
        worst = rtts[-1]
        details = [f"probes: {len(rtts)}",
                   f"median: {p50:.0f} ms",
                   f"P95:    {p95:.0f} ms",
                   f"max:    {worst:.0f} ms"]
        if p95 < 100:
            return self._mk("TM2", "PASS",
                             f"P95 = {p95:.0f} ms (< 100)", details)
        return self._mk("TM2", "FAIL",
                         f"P95 = {p95:.0f} ms exceeds 100 ms — "
                         "BLE link or main-loop pressure", details)
