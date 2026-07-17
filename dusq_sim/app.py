"""Main application window: header, notebook, tab wiring, tab-visible dispatch,
latency probe, and lifecycle (state persistence + clean shutdown).
"""

import asyncio
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

from .constants import *
from .util import run_async, _load_state, _save_state
from .ble import BLEManager

from .tabs.devices import AdvTab
from .tabs.auth import AuthTab
from .tabs.device_info import DeviceInfoTab
from .tabs.sensors import SensorTab
from .tabs.flash import FlashTab
from .tabs.journal import JournalTab
from .tabs.haptic import HapticTab
from .tabs.battery import BatteryTab
from .tabs.automated_testing import ValidationTab
from .tabs.log import EventLog, LogTab
from .tabs.rtt_flow import RttTab


class App(tk.Tk):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.loop = loop
        self.title("DUSQ Charger BLE Test Tool")
        self.resizable(True, True)

        # Restore persisted UI state (window geometry, last device MAC,
        # last-viewed tab).  Falls back to defaults on first run.
        self._state = _load_state()
        geom = self._state.get("geometry", "820x680")
        # Drop saved +X+Y if the top-left falls outside the current screen
        # (e.g. user disconnected the second monitor the window was last on).
        # Tk would otherwise open the window invisibly off-screen.
        import re as _re
        _m = _re.match(r'^(\d+x\d+)([+-]\d+)([+-]\d+)$', geom)
        if _m:
            _x, _y = int(_m.group(2)), int(_m.group(3))
            _sw, _sh = self.winfo_screenwidth(), self.winfo_screenheight()
            if not (-50 <= _x <= _sw - 100 and -50 <= _y <= _sh - 100):
                geom = _m.group(1)   # keep WxH, drop the off-screen offset
        try:
            self.geometry(geom)
        except Exception:
            self.geometry("820x680")

        self.ble = BLEManager()
        # Restore last-connected MAC + name so the Reconnect button is
        # active immediately on launch.
        last_mac = self._state.get("last_mac")
        if last_mac:
            self.ble.last_address = last_mac
            self.ble.last_name    = self._state.get("last_name") or last_mac

        # Shared global event log — every BLE op + UI action lands here, and
        # the LogTab renders it. Wire to BLEManager so reads/writes/scans get
        # captured before the LogTab is even instantiated.
        self.event_log = EventLog()
        self.ble.event_log = self.event_log
        self.event_log.log("info", "app", "tool started")

        # Shared status string — drives BOTH the bottom status bar (full
        # text) and the new top-right connection indicator (red/green).
        self.status_var = tk.StringVar(value="Not connected")

        # ── Top header (always visible, indicator pinned to the right) ──
        header = ttk.Frame(self)
        header.pack(side="top", fill="x", padx=6, pady=(4, 0))
        ttk.Label(header, text="DUSQ Charger BLE Test Tool",
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        # Reconnect button — only enabled when (a) we know the last address
        # AND (b) we're not currently connected. Hidden otherwise.
        self.reconnect_btn = ttk.Button(header, text="↻ Reconnect last",
                                         command=self._do_reconnect_last,
                                         state="disabled")
        self.reconnect_btn.pack(side="right", padx=6)
        self.indicator_var = tk.StringVar(value="● Disconnected")
        self.indicator = ttk.Label(header, textvariable=self.indicator_var,
                                    font=("Segoe UI", 10, "bold"),
                                    foreground="#c0392b")
        self.indicator.pack(side="right")
        # Latency probe: when connected, pings Status char every 5 s and
        # displays the round-trip time next to the indicator.  Colour
        # threshold: green < 100 ms, yellow 100–300 ms, red > 300 ms.
        self.latency_var = tk.StringVar(value="")
        self.latency_lbl = ttk.Label(header, textvariable=self.latency_var,
                                      font=("Segoe UI", 9))
        self.latency_lbl.pack(side="right", padx=(0, 6))
        # When status_var changes anywhere in the app, recompute the
        # top-right indicator.
        self.status_var.trace_add("write", lambda *_: self._update_indicator())

        # Bottom status bar (full text, unchanged)
        ttk.Label(self, textvariable=self.status_var, relief="sunken",
                  anchor="w").pack(side="bottom", fill="x", padx=2, pady=1)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=4, pady=4)

        # Devices tab combines the previous Connect + Adv Data — continuous
        # passive scan, double-click row to connect, with Show-all toggle for
        # non-DUSQ adverts.
        self.adv_tab     = AdvTab(nb, self.ble, self.status_var)
        self.auth_tab    = AuthTab(nb, self.ble)
        self.devinfo_tab = DeviceInfoTab(nb, self.ble)
        self.sens_tab    = SensorTab(nb, self.ble)
        self.flash_tab   = FlashTab(nb, self.ble)
        self.jnl_tab     = JournalTab(nb, self.ble)
        self.hap_tab     = HapticTab(nb, self.ble)
        self.batt_tab    = BatteryTab(nb, self.ble)
        self.val_tab     = ValidationTab(nb, self.ble)
        self.log_tab     = LogTab(nb, self.event_log)
        self.rtt_tab     = RttTab(nb)

        # NOTE: Tab vertical scrollability removed — the canvas-wrap fought
        # Tk's geometry manager at App-init time (window unmapped, canvas
        # width returns 1) and left content right-shifted.  If a future tab
        # actually overflows, wrap that ONE tab's overflowing section
        # locally rather than re-introducing the global wrapper.

        # On successful PIN write, auto-subscribe Battery AND auto-read the
        # 5 DIS characteristics so the Device Info tab is populated as soon as
        # the user authenticates — no extra clicks.
        def _on_auth_ok():
            self.batt_tab.auto_subscribe()
            self.devinfo_tab.refresh()
        self.auth_tab.on_auth_attempt = _on_auth_ok

        # Reset Sensor / Battery / Flash / Validation panels whenever the BLE
        # link drops, so each session starts visually clean and any in-flight
        # download or test run is cancelled.
        self.ble.add_disconnect_sub(self.auth_tab._on_disconnected)
        self.ble.add_disconnect_sub(self.devinfo_tab._on_disconnected)
        self.ble.add_disconnect_sub(self.sens_tab._on_disconnected)
        self.ble.add_disconnect_sub(self.batt_tab._on_disconnected)
        self.ble.add_disconnect_sub(self.flash_tab._on_disconnected)
        self.ble.add_disconnect_sub(self.jnl_tab._on_disconnected)
        self.ble.add_disconnect_sub(self.val_tab._on_disconnected)
        # HapticTab also resets its own _subscribed flag on disconnect so the
        # next tab-revisit re-subscribes to CHAR_HAPTIC_STAT.
        if hasattr(self.hap_tab, "_on_disconnected"):
            self.ble.add_disconnect_sub(self.hap_tab._on_disconnected)

        # When the user switches to ANY tab that defines _on_tab_visible(),
        # dispatch to it.  Lets Battery / Sensor / Haptic auto-subscribe to
        # their primary notify chars without a manual Subscribe click —
        # idempotent inside each tab so repeated tab focus does no harm.
        def _on_tab_changed(_evt):
            try:
                current_tab = nb.nametowidget(nb.select())
                if hasattr(current_tab, "_on_tab_visible"):
                    current_tab._on_tab_visible()
            except Exception:
                pass
        nb.bind("<<NotebookTabChanged>>", _on_tab_changed)

        nb.add(self.adv_tab,     text="Devices")
        nb.add(self.auth_tab,    text="Auth")
        nb.add(self.devinfo_tab, text="Device Info")
        nb.add(self.sens_tab,    text="Sensors")
        nb.add(self.flash_tab,   text="Flash")
        nb.add(self.jnl_tab,     text="Journal")
        nb.add(self.hap_tab,     text="Haptic")
        nb.add(self.batt_tab,    text="Battery")
        nb.add(self.val_tab,     text="Automated Testing")
        nb.add(self.log_tab,     text="Log")
        nb.add(self.rtt_tab,     text="RTT / Flow")

        # Initial state of the indicator + Reconnect button.
        self._update_indicator()

        # Kick off the latency probe — fires every 5 s while connected.
        self._latency_task: Optional[asyncio.Task] = None
        self._start_latency_probe()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_loop()

    def _start_latency_probe(self):
        """Schedule a periodic Status-char read every 5 s while connected.
        Updates the header latency label with the round-trip time.
        Uses a Tk after() loop (no asyncio task lifecycle to manage); the
        actual BLE read is dispatched via run_async."""
        async def _probe():
            if not self.ble.connected:
                return None
            t0 = time.perf_counter()
            try:
                await self.ble.read(CHAR_STATUS)
                return int((time.perf_counter() - t0) * 1000)
            except Exception:
                return None

        def _on_done(task: asyncio.Task):
            try:
                rtt = task.result()
            except Exception:
                rtt = None
            if rtt is None:
                self.latency_var.set("")
            else:
                self.latency_var.set(f"· {rtt} ms")
                if rtt < 100:
                    self.latency_lbl.configure(foreground="#1b8c3a")  # green
                elif rtt < 300:
                    self.latency_lbl.configure(foreground="#d4a017")  # amber
                else:
                    self.latency_lbl.configure(foreground="#c0392b")  # red

        def _tick():
            if self.ble.connected:
                task = run_async(_probe())
                task.add_done_callback(
                    lambda t: self.after(0, lambda: _on_done(t)))
            else:
                self.latency_var.set("")
            self.after(5000, _tick)

        self.after(5000, _tick)

    def _update_indicator(self):
        """Mirror the shared status string into the top-right indicator
        with a coloured bullet, and morph the right-side button to match
        the current state:
          Connected   → green   "● Connected: name  MAC"   button = ✕ Disconnect
          Connecting  → yellow  "● Connecting …"           button = disabled
          Disconnected→ red     "● Disconnected"           button = ↻ Reconnect …
        """
        text = self.status_var.get()
        lo   = text.lower()
        if lo.startswith("connected"):
            # Append the MAC alongside the name so the user can prove which
            # physical unit is on the wire. status_var carries "Connected: <name>"
            # and BLEManager.last_address holds the MAC of the active link.
            mac = getattr(self.ble, "last_address", None) or ""
            label = f"● {text}  {mac}" if mac else f"● {text}"
            self.indicator_var.set(label)
            self.indicator.configure(foreground="#1b8c3a")  # green
            self.reconnect_btn.configure(
                state="normal",
                text="✕ Disconnect",
                command=self._do_disconnect,
            )
        elif lo.startswith("connecting") or lo.startswith("reconnecting"):
            self.indicator_var.set(f"● {text}")
            self.indicator.configure(foreground="#d4a017")  # amber/yellow
            self.reconnect_btn.configure(state="disabled")
        else:
            self.indicator_var.set("● Disconnected")
            self.indicator.configure(foreground="#c0392b")  # red
            if self.ble.last_address:
                last_label = (self.ble.last_name or self.ble.last_address)
                self.reconnect_btn.configure(
                    state="normal",
                    text=f"↻ Reconnect {last_label[:18]}",
                    command=self._do_reconnect_last,
                )
            else:
                self.reconnect_btn.configure(
                    state="disabled",
                    text="↻ Reconnect last",
                    command=self._do_reconnect_last,
                )

    def _do_disconnect(self):
        """Header-button disconnect — drops the BLE link; the BLEManager's
        on_disconnect callback chain resets status_var to 'Not connected'
        which then re-runs _update_indicator and morphs the button back."""
        run_async(self.ble.disconnect())

    def _do_reconnect_last(self):
        """Reconnect to the last MAC the user successfully connected to.
        No scan needed — bleak can dial an address directly."""
        addr = self.ble.last_address
        if not addr:
            return
        async def _reconn():
            try:
                self.status_var.set(f"Reconnecting to {addr}…")
                await self.ble.connect(addr)
                name = self.ble.last_name or addr
                self.status_var.set(f"Connected: {name}")
            except Exception as e:
                self.status_var.set("Not connected")
                messagebox.showerror("Reconnect", f"Failed: {e}")
        run_async(_reconn())

    def _poll_loop(self):
        """
        Drive the asyncio event loop from the tkinter main loop.

        Schedule a stop, then call run_forever(): the loop will execute every
        ready callback (bleak continuations, task wake-ups, timers) and only
        exit when our stop() callback fires. The previous
        run_until_complete(asyncio.sleep(0)) returned on the very next loop
        step and starved tasks created by run_async().
        """
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()
        self.after(20, self._poll_loop)

    def _on_close(self):
        # Persist UI state for next launch — window geometry, last-connected
        # device MAC, last-viewed tab.  Best-effort; never blocks shutdown.
        try:
            state = dict(self._state)
            state["geometry"] = self.geometry()
            if self.ble.last_address:
                state["last_mac"]  = self.ble.last_address
                state["last_name"] = self.ble.last_name or ""
            _save_state(state)
        except Exception:
            pass

        self.adv_tab.stop()   # shut down passive scanner cleanly
        self.rtt_tab.stop()   # stop the J-Link RTT reader thread
        async def _cleanup():
            await self.ble.disconnect()
        self.loop.run_until_complete(_cleanup())
        self.destroy()


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = App(loop)
    app.mainloop()
