"""Haptic tab — three sections mapped to the haptic characteristics:

  1. Manual motor test   — 0x1530 control (ON / OFF / STOP-ALL) + 0x1531 intensity.
  2. Reminder schedule   — 0x1537 [single_shot, recurring] ([N, 0] = one-shot);
                           quick presets.
  3. Live status         — 0x1533 snapshot, decoded to a 5-state view.

The 0x1533 motor_state byte is only 0/1 on the wire; the 5-state view
(idle / buzzing / single-shot-waiting / recurring-waiting / pending-sync) is
synthesized here from the schedule fields + flags byte for the operator's benefit.
"""

import asyncio
import struct
import tkinter as tk
from tkinter import scrolledtext, ttk
from typing import Optional

from ..constants import *
from ..decoders import *
from ..util import *
from ..ble import BLEManager

# Synthesized 0x1533 states (motor byte 0/1 on the wire; 2..4 host-derived).
HAPTIC_STATES = {
    0: "IDLE",
    1: "BUZZING",
    2: "SINGLE-SHOT WAITING",
    3: "RECURRING WAITING",
    4: "PENDING SYNC",
}


class HapticTab(ttk.Frame):
    def __init__(self, parent, ble: BLEManager):
        super().__init__(parent)
        self.ble = ble
        self._subscribed = False        # idempotent tab-open auto-subscribe
        self._int_sync = False          # guards the slider<->entry feedback loop

        self._build_motor()
        self._build_schedule()
        self._build_status()

        lf = ttk.LabelFrame(self, text="Log", padding=4)
        lf.pack(fill="both", expand=True, padx=6, pady=4)
        self.log_box = scrolledtext.ScrolledText(lf, height=8, state="disabled",
                                                  font=("Courier", 9))
        self.log_box.pack(fill="both", expand=True)

    # ── section 1: manual motor (0x1530 / 0x1531) ────────────────────────────
    def _build_motor(self):
        man = ttk.LabelFrame(self, text="Manual motor test  ·  0x1530 / 0x1531",
                             padding=10)
        man.pack(fill="x", padx=6, pady=(6, 3))

        ttk.Label(man, text="Buzz:", width=11).grid(row=0, column=0, sticky="w")
        ttk.Button(man, text="Motor ON",
                   command=lambda: self._write_ctl(0x01)).grid(row=0, column=1, padx=4, sticky="w")
        ttk.Button(man, text="Motor OFF",
                   command=lambda: self._write_ctl(0x00)).grid(row=0, column=2, padx=4, sticky="w")

        ttk.Label(man, text="Intensity:", width=11).grid(row=1, column=0, sticky="w", pady=(6, 0))
        # IntVar is authoritative; the slider pushes rounded ints into it via a
        # command callback (binding variable= directly writes floats -> "83.56").
        self.int_var = tk.IntVar(value=50)
        self.int_scale = ttk.Scale(man, from_=0, to=100, orient="horizontal",
                                   length=200, command=self._on_int_slide)
        self.int_scale.set(50)
        self.int_scale.grid(row=1, column=1, columnspan=2, sticky="w", padx=4, pady=(6, 0))
        ttk.Entry(man, textvariable=self.int_var, width=4, justify="right"
                 ).grid(row=1, column=3, padx=(6, 0), pady=(6, 0))
        ttk.Label(man, text="%").grid(row=1, column=4, sticky="w")
        ttk.Button(man, text="Send",
                   command=self._set_intensity).grid(row=1, column=5, padx=6, pady=(6, 0))
        self.int_var.trace_add("write", self._on_int_entry)

    # ── section 2: schedule (0x1537) ─────────────────────────────────────────
    def _build_schedule(self):
        sch = ttk.LabelFrame(self, text="Reminder schedule  ·  0x1537", padding=10)
        sch.pack(fill="x", padx=6, pady=3)

        ttk.Label(sch,
                  text="[single_shot, recurring] seconds.  First fire = single_shot "
                       "(or recurring if 0).  recurring 0 = one-shot; else repeat every N s.  "
                       "[0,0] cancels.",
                  foreground="gray", wraplength=600, justify="left"
                 ).grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 6))

        ttk.Label(sch, text="single_shot (s):").grid(row=1, column=0, sticky="w")
        self.ss_var = tk.IntVar(value=10)
        ttk.Entry(sch, textvariable=self.ss_var, width=10
                 ).grid(row=1, column=1, padx=4, sticky="w")
        ttk.Label(sch, text="recurring (s):").grid(row=1, column=2, sticky="e", padx=(12, 0))
        self.rec_var = tk.IntVar(value=0)
        ttk.Entry(sch, textvariable=self.rec_var, width=10
                 ).grid(row=1, column=3, padx=4, sticky="w")

        # Set | Cancel | Read Current, with the current-schedule status in the same row.
        bf = ttk.Frame(sch)
        bf.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(6, 0))
        ttk.Button(bf, text="Set Schedule",
                   command=self._set_schedule).pack(side="left", padx=(0, 4))
        ttk.Button(bf, text="Cancel schedule",
                   command=self._disable_schedule).pack(side="left", padx=4)
        ttk.Button(bf, text="Read Current",
                   command=self._read_schedule).pack(side="left", padx=4)
        self.sched_state_var = tk.StringVar(value="(unknown — click Read Current)")
        ttk.Label(bf, textvariable=self.sched_state_var, font=("Courier", 10)
                 ).pack(side="left", padx=(10, 0))

        # STOP-ALL is a control write (0x1530=0x02) that also stops the motor.
        stopf = ttk.Frame(sch)
        stopf.grid(row=3, column=0, columnspan=6, sticky="w", pady=(8, 0))
        ttk.Button(stopf, text="STOP-ALL",
                   command=lambda: self._write_ctl(0x02)).pack(side="left")
        ttk.Label(stopf, text="motor off + cancel every alarm  (0x1530 = 0x02)",
                  foreground="gray").pack(side="left", padx=8)

    # ── section 3: live status (0x1533) ──────────────────────────────────────
    def _build_status(self):
        st = ttk.LabelFrame(self, text="Live status  ·  0x1533", padding=10)
        st.pack(fill="x", padx=6, pady=3)

        self.st_state_var = tk.StringVar(value="--")
        self.st_sched_var = tk.StringVar(value="--")
        self.st_next_var  = tk.StringVar(value="--")

        ttk.Label(st, text="State:", width=10).grid(row=0, column=0, sticky="w")
        ttk.Label(st, textvariable=self.st_state_var,
                  font=("Courier", 10, "bold")).grid(row=0, column=1, sticky="w")
        ttk.Button(st, text="Subscribe / Refresh",
                   command=self._sub_status).grid(row=0, column=2, padx=8)
        ttk.Label(st, text="Schedule:", width=10).grid(row=1, column=0, sticky="w")
        ttk.Label(st, textvariable=self.st_sched_var,
                  font=("Courier", 10)).grid(row=1, column=1, columnspan=2, sticky="w")
        ttk.Label(st, text="Next buzz:", width=10).grid(row=2, column=0, sticky="w")
        ttk.Label(st, textvariable=self.st_next_var,
                  font=("Courier", 10)).grid(row=2, column=1, columnspan=2, sticky="w")

    # ── logging ──────────────────────────────────────────────────────────────
    def _log(self, msg: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{ts()}] {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ── intensity slider<->entry sync (fixes the float "83.56" bug) ──────────
    def _on_int_slide(self, v):
        if self._int_sync:
            return
        self._int_sync = True
        try:
            self.int_var.set(int(round(float(v))))
        except (TypeError, ValueError):
            pass
        finally:
            self._int_sync = False

    def _on_int_entry(self, *_):
        if self._int_sync:
            return
        self._int_sync = True
        try:
            self.int_scale.set(max(0, min(100, self.int_var.get())))
        except (tk.TclError, ValueError):
            pass    # entry mid-edit / empty
        finally:
            self._int_sync = False

    # ── control / intensity ──────────────────────────────────────────────────
    def _write_ctl(self, val: int):
        if not self.ble.connected:
            return
        async def _w():
            try:
                await self.ble.write(CHAR_HAPTIC_CTL, bytes([val]))
                label = {0: "OFF / abort buzz", 1: "ON",
                         2: "STOP-ALL (cancel every alarm + stop)"}.get(val, f"0x{val:02X}")
                self._log(f"Control: {label}")
            except Exception as e:
                self._log(f"Error: {e}")
        run_async(_w())

    def _set_intensity(self):
        if not self.ble.connected:
            return
        try:
            val = max(0, min(100, int(self.int_var.get())))
        except (tk.TclError, ValueError):
            self._log("Intensity: enter 0-100")
            return
        async def _w():
            try:
                await self.ble.write(CHAR_HAPTIC_INT, bytes([val]))
                self._log(f"Intensity set to {val}%")
            except Exception as e:
                self._log(f"Error: {e}")
        run_async(_w())

    # ── schedule (0x1537) ────────────────────────────────────────────────────
    def _write_schedule(self, single_shot: int, recurring: int, label: str):
        payload = struct.pack("<I", single_shot) + struct.pack("<I", recurring)
        async def _w():
            try:
                await self.ble.write(CHAR_HAPTIC_REMINDER, payload)
                self._log(f"Schedule set: {label}  ({payload.hex(' ').upper()})")
                self.sched_state_var.set(label)
            except Exception as e:
                self._log(f"Schedule write error: {e}")
        run_async(_w())

    def _set_schedule(self):
        if not self.ble.connected:
            return
        try:
            ss  = max(0, int(self.ss_var.get()))
            rec = max(0, int(self.rec_var.get()))
        except (tk.TclError, ValueError):
            self._log("Schedule: enter integer seconds")
            return
        self._write_schedule(ss, rec, f"single_shot={ss}s recurring={rec}s")

    def _disable_schedule(self):
        if not self.ble.connected:
            return
        self._write_schedule(0, 0, "DISABLED")

    def _read_schedule(self):
        """Read 0x1537 = [single_shot][recurring] (post-reboot = persisted rule)."""
        if not self.ble.connected:
            return
        async def _r():
            try:
                raw = bytes(await self.ble.read(CHAR_HAPTIC_REMINDER))
                if len(raw) < 8:
                    self._log(f"Schedule read: short ({len(raw)} bytes): {raw.hex(' ')}")
                    return
                ss  = struct.unpack("<I", raw[0:4])[0]
                rec = struct.unpack("<I", raw[4:8])[0]
                state = f"single_shot={ss}s  recurring={rec}s"
                self.sched_state_var.set(state)
                self._log(f"Schedule read: {raw.hex(' ').upper()}  ->  {state}")
            except Exception as e:
                self._log(f"Schedule read error: {e}")
        run_async(_r())

    # ── status (0x1533) ──────────────────────────────────────────────────────
    def _sub_status(self):
        if not self.ble.connected:
            return
        def _on_stat(_h, data: bytearray):
            self._decode_status(bytes(data))
        async def _s():
            try:
                await self.ble.subscribe(CHAR_HAPTIC_STAT, _on_stat)
                self._subscribed = True
                # Explicit read so the current snapshot shows immediately (0x1533
                # is R+N) rather than waiting for the first transition notify.
                try:
                    raw = bytes(await self.ble.read(CHAR_HAPTIC_STAT))
                    self._decode_status(raw)
                except Exception:
                    pass
                self._log("Subscribed to haptic status (+ read current).")
            except Exception as e:
                self._log(f"Error: {e}")
        run_async(_s())

    def _decode_status(self, data: bytes):
        """0x1533 snapshot (14 B): [0]motor_state [1:5]single_shot LE [5:9]recurring LE
        [9:13]next_fire_epoch LE [13]flags(bit0 pending_sync, bit1 fire_pending)."""
        if len(data) < 14:
            self.st_state_var.set(f"?({len(data)} B)")
            return
        motor  = data[0]
        ss     = struct.unpack("<I", data[1:5])[0]
        rec    = struct.unpack("<I", data[5:9])[0]
        epoch  = struct.unpack("<I", data[9:13])[0]
        flags  = data[13]
        pending = bool(flags & 0x01)
        firing  = bool(flags & 0x02)

        if motor == 1:
            state = 1
        elif pending:
            state = 4
        elif firing:
            state = 3 if rec > 0 else 2
        else:
            state = 0
        self.st_state_var.set(f"{state}  {HAPTIC_STATES.get(state, '?')}")

        if ss == 0 and rec == 0:
            self.st_sched_var.set("no schedule")
        else:
            self.st_sched_var.set(f"single_shot={ss}s  recurring={rec}s")

        if pending:
            self.st_next_var.set("pending time-sync")
        elif epoch != 0:
            self.st_next_var.set(f"{fmt_epoch_local(epoch)}   (epoch {epoch})")
        elif firing:
            self.st_next_var.set("scheduled (sync for exact time)")
        else:
            self.st_next_var.set("—")

    # ── lifecycle ────────────────────────────────────────────────────────────
    def _on_tab_visible(self):
        """Auto-subscribe to the status char when the tab is shown (idempotent)."""
        if not self.ble.connected or self._subscribed:
            return
        self._sub_status()

    def _on_disconnected(self):
        """Reset the subscribe flag so a reconnect + tab-revisit re-subscribes."""
        self._subscribed = False
        self.st_state_var.set("--")
        self.st_sched_var.set("--")
        self.st_next_var.set("--")
