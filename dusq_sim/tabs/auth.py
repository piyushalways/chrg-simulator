"""auth tab."""

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

class AuthTab(ttk.Frame):
    def __init__(self, parent, ble: BLEManager):
        super().__init__(parent)
        self.ble = ble
        self.authenticated = False
        # Optional callback fired right after the PIN write succeeds. App wires
        # this to BatteryTab so battery notifications start automatically once
        # the firmware has accepted the PIN. Set by App after construction.
        self.on_auth_attempt = None

        frm = ttk.LabelFrame(self, text="PIN Authentication", padding=10)
        frm.pack(fill="x", padx=6, pady=6)

        ttk.Label(frm, text="PIN (hex, 3 bytes):").grid(row=0, column=0, sticky="w")
        self.pin_var = tk.StringVar(value="")
        ttk.Entry(frm, textvariable=self.pin_var, width=14).grid(row=0, column=1, padx=4)
        # Single Authenticate button — auto-derives PIN from DIS Serial if the
        # entry is empty (preserves manual override for fault-injection).
        ttk.Button(frm, text="Authenticate",
                   command=self._do_auth).grid(row=0, column=2, padx=4)
        # Time-sync button — red when not synced (initial / after every
        # fresh connect — the device may have lost its RTC), yellow after a
        # successful sync write so the user sees confirmation but knows it's
        # still a one-shot action.  tk.Button (vs ttk) for reliable bg color
        # across Windows themes.
        self.timesync_btn = tk.Button(frm, text="Send Time Sync (IST)",
                                       command=self._do_time_sync,
                                       bg="#c0392b", fg="white",
                                       activebackground="#a02818",
                                       font=("Segoe UI", 9, "bold"),
                                       relief="raised", bd=1)
        self.timesync_btn.grid(row=0, column=3, padx=(12, 4))
        # Read-back the device's current epoch (0x152E gained a Read property in
        # firmware B3) and show its drift from the host clock.
        self.readtime_btn = ttk.Button(frm, text="Read Device Time",
                                       command=self._do_read_time)
        self.readtime_btn.grid(row=0, column=4, padx=(4, 4))

        ttk.Label(frm,
                  text="PIN = low 24 bits of CRC32(DIS Serial). Leave empty → auto-derived from serial on Authenticate.",
                  foreground="gray").grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 4))

        self.result_lbl = ttk.Label(frm, text="Status: not authenticated", foreground="gray")
        self.result_lbl.grid(row=2, column=0, columnspan=4, sticky="w", pady=4)
        # Live "last sync" label updated whenever Send Time Sync succeeds.
        self.last_sync_var = tk.StringVar(value="Last sync: never")
        ttk.Label(frm, textvariable=self.last_sync_var,
                  foreground="#1b8c3a").grid(row=2, column=4, sticky="e", padx=4)
        # Device time read-back (0x152E) + drift vs the host clock.
        self.device_time_var = tk.StringVar(value="Device time: —")
        ttk.Label(frm, textvariable=self.device_time_var,
                  foreground="#2c3e50").grid(row=3, column=0, columnspan=5,
                                             sticky="w", pady=(2, 0))

        lf = ttk.LabelFrame(self, text="Log", padding=4)
        lf.pack(fill="both", expand=True, padx=6, pady=4)
        self.log_box = scrolledtext.ScrolledText(lf, height=12, state="disabled",
                                                  font=("Courier", 9))
        self.log_box.pack(fill="both", expand=True)

    def _log(self, msg: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{ts()}] {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _on_disconnected(self):
        """Reset auth + time-sync state when the BLE link drops so a reconnect
        starts visually clean — otherwise the green 'authenticated' label and
        the synced 'Time synced' button persist from the previous session,
        even though the device requires re-auth + re-sync after reconnect."""
        self.authenticated = False
        self.result_lbl.configure(text="Status: not authenticated",
                                   foreground="gray")
        # Reset time-sync button to red / unsynced state — every fresh
        # connection starts un-synced and the device must be re-told.
        try:
            self.timesync_btn.configure(
                bg="#c0392b", fg="white",
                activebackground="#a02818",
                text="Send Time Sync (IST)")
            self.last_sync_var.set("Last sync: never")
            self.device_time_var.set("Device time: —")
        except Exception:
            pass
        self._log("Disconnected — auth + time-sync state cleared.")

    def _compute_pin(self):
        """Read DIS Serial Number from the device and fill the PIN field with
        the matching auto-derived PIN (low 24 bits of CRC32 of the serial).
        Provided so the user can see the computed value before hitting
        Authenticate, and still hand-edit it for fault-injection testing."""
        if not self.ble.connected:
            messagebox.showwarning("Compute PIN", "Not connected.")
            return

        async def _read():
            try:
                serial = bytes(await self.ble.read(CHAR_DIS_SERIAL))
                pin = compute_auth_pin(serial)
                self.pin_var.set(pin.hex(" ").upper())
                self._log(f"Read DIS Serial = {serial.decode('ascii', 'replace')!r}")
                self._log(f"Computed PIN = {pin.hex(' ').upper()}")
            except Exception as e:
                self._log(f"Compute-PIN error: {e}")
                messagebox.showerror("Compute PIN", str(e))
        run_async(_read())

    def _do_auth(self):
        if not self.ble.connected:
            messagebox.showwarning("Auth", "Not connected.")
            return

        pin_text = self.pin_var.get().strip()
        if not pin_text:
            # Empty field → auto-compute from DIS Serial Number, then write.
            async def _autoauth():
                try:
                    serial = bytes(await self.ble.read(CHAR_DIS_SERIAL))
                    pin = compute_auth_pin(serial)
                    self.pin_var.set(pin.hex(" ").upper())
                    self._log(f"Auto-derived PIN from serial "
                              f"{serial.decode('ascii', 'replace')!r}: "
                              f"{pin.hex(' ').upper()}")
                    await self._send_pin_and_verify(pin)
                except Exception as e:
                    self.result_lbl.configure(text="Status: error during auth",
                                              foreground="red")
                    self._log(f"Auth error: {e}")
            run_async(_autoauth())
            return

        try:
            raw = bytes(int(b, 16) for b in pin_text.split())
        except ValueError:
            messagebox.showerror("Auth",
                                 "Invalid PIN — enter 3 hex bytes (e.g. '1A B3 7F') "
                                 "or leave empty to auto-compute from serial.")
            return

        run_async(self._send_pin_and_verify(raw))

    async def _send_pin_and_verify(self, pin: bytes):
        """Send the PIN to the firmware and verify success via the Status char.

        The firmware does NOT notify back on the auth char (0x1524) — see
        src/ble_svc.c on_write().  Status char (0x1529) is the device's own
        state-machine truth: state == AUTHENTICATED confirms PIN accepted.
        """
        try:
            self._log(f"Writing PIN {pin.hex(' ').upper()} to {CHAR_AUTH_PIN} …")
            self.result_lbl.configure(text="Status: writing PIN…",
                                        foreground="gray")

            # 1. Send the PIN.
            await self.ble.write(CHAR_AUTH_PIN, pin)

            # 2. Trigger any external observers (e.g. BatteryTab
            #    auto-subscribe) so they kick off as soon as the PIN
            #    write returns successfully.
            if callable(self.on_auth_attempt):
                try:
                    self.on_auth_attempt()
                except Exception as cb_err:
                    self._log(f"Auth callback error: {cb_err}")

            # 3. Give the firmware a moment to flip its state machine.
            await asyncio.sleep(0.7)

            # 4. Read STATUS char and check the device's view.
            status_raw = await self.ble.read(CHAR_STATUS)
            s = decode_status(bytes(status_raw))
            state = s.get("state", "?")
            self._log(f"Status char read: state={state}  full={s}")

            if state == "AUTHENTICATED":
                self.authenticated = True
                self.result_lbl.configure(
                    text="Status: AUTHENTICATED",
                    foreground="green")
                self._log("Authentication successful "
                          "(verified via Status char 0x1529).")
            elif state == "CONNECTED":
                self.authenticated = False
                self.result_lbl.configure(
                    text="Status: REJECTED — wrong PIN",
                    foreground="red")
                self._log("Authentication rejected — device stayed "
                          "in CONNECTED state.")
            else:
                self.authenticated = False
                self.result_lbl.configure(
                    text=f"Status: unexpected state = {state}",
                    foreground="darkorange")
                self._log(f"Unexpected device state after auth: {state}")
        except Exception as e:
            self.result_lbl.configure(
                text="Status: error during auth",
                foreground="red")
            self._log(f"Auth error: {e}")

    def _do_time_sync(self):
        """Push the current IST (UTC+05:30) wall-clock epoch to CHAR_TIMESYNC.
        Same logic as the Flash tab's Send Time Sync button — exposed here
        as well so users can sync immediately after authenticating without
        navigating away.  Updates the inline 'Last sync' label on success."""
        if not self.ble.connected:
            messagebox.showwarning("Time Sync", "Not connected.")
            return
        epoch = int(time.time()) + IST_OFFSET_S   # device runs on IST (UTC+05:30)
        async def _sync():
            try:
                await self.ble.write(CHAR_TIMESYNC, struct.pack("<I", epoch))
                wall = datetime.fromtimestamp(epoch, tz=timezone.utc)   # IST-biased -> renders IST
                self.last_sync_var.set(f"Last sync: {wall:%H:%M:%S} IST")
                # Flip the button to yellow/synced state — sync is one-shot,
                # the yellow is a soft "confirmed" indicator (not green so the
                # user knows they CAN re-sync without alarm).
                try:
                    self.timesync_btn.configure(
                        bg="#f1c40f", fg="black",
                        activebackground="#d4a017",
                        text="Time synced ✓")
                except Exception:
                    pass
                self._log(f"Time sync sent: epoch={epoch} "
                          f"({wall:%Y-%m-%d %H:%M:%S} IST)")
            except Exception as e:
                self._log(f"Time sync error: {e}")
                messagebox.showerror("Time Sync", str(e))
        run_async(_sync())

    def _do_read_time(self):
        """Read CHAR_TIMESYNC (0x152E — Read added in firmware B3) and show the
        device's current epoch as an IST wall-clock, plus its drift from the host.
        Both epochs are IST-biased, so the +05:30 offset cancels and the delta is
        the true device-vs-host clock skew. The device clock is 60 s-coarse, so
        expect up to ~60 s of quantization on top of any real RC drift."""
        if not self.ble.connected:
            messagebox.showwarning("Read Device Time", "Not connected.")
            return

        async def _read():
            try:
                raw = bytes(await self.ble.read(CHAR_TIMESYNC))
                if len(raw) < 4:
                    self.device_time_var.set(f"Device time: short read ({len(raw)} B)")
                    self._log(f"Read Device Time: short read ({len(raw)} B)")
                    return
                dev_epoch = struct.unpack("<I", raw[:4])[0]
                # Plausible Unix epoch (>= ~2001) => synced wall time; below that the
                # device is unsynced and the value is uptime seconds since boot.
                if dev_epoch >= 1_000_000_000:
                    host_epoch = int(time.time()) + IST_OFFSET_S   # host IST wall-clock
                    drift = dev_epoch - host_epoch                 # +ve: device ahead
                    self.device_time_var.set(
                        f"Device time: {fmt_epoch_local(dev_epoch)} IST   |   "
                        f"drift vs host: {drift:+d} s "
                        f"({'ahead' if drift >= 0 else 'behind'})")
                    self._log(f"Device time read: epoch={dev_epoch} "
                              f"({fmt_epoch_local(dev_epoch)} IST), host={host_epoch}, "
                              f"drift={drift:+d} s")
                else:
                    self.device_time_var.set(
                        f"Device time: unsynced (uptime {dev_epoch} s since boot)")
                    self._log(f"Device time read: epoch={dev_epoch} "
                              f"(< 1e9 -> unsynced, uptime seconds)")
            except Exception as e:
                self._log(f"Read Device Time error: {e}")
                messagebox.showerror("Read Device Time", str(e))
        run_async(_read())
