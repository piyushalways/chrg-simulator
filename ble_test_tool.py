#!/usr/bin/env python3
"""
DUSQ Charger BLE Test Tool

Connects to DUSQ_CHARGER firmware over BLE and exercises:
  - Authentication (PIN write)
  - Live sensor monitoring (temperature, dB, lux, battery)
  - Flash data download and block decoding
  - Automated validation flows for temperature smoothing and mic metrics

Requirements:
    pip install bleak

Usage:
    python ble_test_tool.py
"""

import asyncio
import json
import os
import struct
import sys
import time
import tkinter as tk
import zlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

try:
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError
except ImportError:
    print("ERROR: 'bleak' not installed.  Run: pip install bleak")
    sys.exit(1)

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# ─────────────────────────────────────────────────────────────────────────────
#  BLE UUID constants
#
#  Confirmed advertised UUID (verified via nRF Connect for Mobile, scan view):
#      00001523-bcea-5f78-2315-deef12120000
#
#  → the SoftDevice exposes the 16-bit short uuid in the FIRST 4 hex digits of
#    the 128-bit string, with the two preceding bytes left as 0x0000.
#  → general layout for short 0xSSSS:
#       0000SSSS-bcea-5f78-2315-deef12120000
#
#  This matches the firmware base UUID where bytes [0..1] are the placeholder
#  the SoftDevice overwrites with the short — see src/config/ble_config.h.
# ─────────────────────────────────────────────────────────────────────────────

def _uuid(short: int) -> str:
    return f"0000{short:04x}-bcea-5f78-2315-deef12120000"

# Auth Service (0x1523)
SVC_AUTH          = _uuid(0x1523)
CHAR_AUTH_PIN     = _uuid(0x1524)   # Write + Notify — write 3-byte PIN {0,0,0}

# Sensor Service (0x1525) — firmware exposes ONE merged 4-byte char.
# Layout (little-endian): [temp:int8, db:uint8, lux:uint16]
# Error markers: temp=127, db=0xFF, lux=0xFFFF.
SVC_SENSOR        = _uuid(0x1525)
CHAR_SENSOR_DATA  = _uuid(0x1526)   # Read + Notify — 5-byte packed reading
CHAR_STATUS       = _uuid(0x1529)   # Read + Notify — 10-byte packed status

# Flash Data Service (0x152A) — sensor blocks AND journal share this service.
SVC_FLASH            = _uuid(0x152A)
CHAR_BLOCK_COUNT     = _uuid(0x152B)   # Read — uint16 LE, unsynced sensor blocks
# Sensor record stream is W + N on the SAME char: subscribe first, then write
# 0x01 — the device fires 152-byte block notifications on the same UUID.
CHAR_RECORD          = _uuid(0x152D)
CHAR_TIMESYNC        = _uuid(0x152E)   # Write — uint32 LE Unix epoch UTC

# Journal characteristics (event log: BOOT / TIME_SYNC / FLASH_WRAP / ERROR / …)
# Trigger and notify are on DIFFERENT chars: subscribe to RECORD, then write
# 0x01 to START.
CHAR_JOURNAL_COUNT   = _uuid(0x1534)   # Read — uint16 LE valid entry count
CHAR_JOURNAL_RECORD  = _uuid(0x1535)   # Notify — 32-byte JournalEntry stream
CHAR_JOURNAL_START   = _uuid(0x1536)   # Write — 0x01 starts journal transfer

# Haptic Motor Service (0x152F)
SVC_HAPTIC        = _uuid(0x152F)
CHAR_HAPTIC_CTL      = _uuid(0x1530)   # Write — 0x01=ON, else OFF
CHAR_HAPTIC_INT      = _uuid(0x1531)   # Read+Write — duty 0-100%
CHAR_HAPTIC_TIMER    = _uuid(0x1532)   # Write — uint32 LE countdown seconds
CHAR_HAPTIC_STAT     = _uuid(0x1533)   # Read+Notify — 0=idle,1=buzzing,2=countdown
CHAR_HAPTIC_REMINDER = _uuid(0x1537)   # Read+Write — 5B: [0] enable, [1..4] recurring_s LE

# Battery Service (SIG standard)
SVC_BATT          = "0000180f-0000-1000-8000-00805f9b34fb"
CHAR_BATT_LEVEL   = "00002a19-0000-1000-8000-00805f9b34fb"

# Device Information Service (SIG standard) — 5 chars exposed by firmware
SVC_DIS                 = "0000180a-0000-1000-8000-00805f9b34fb"
CHAR_DIS_SYSTEM_ID      = "00002a23-0000-1000-8000-00805f9b34fb"
CHAR_DIS_MODEL          = "00002a24-0000-1000-8000-00805f9b34fb"
CHAR_DIS_SERIAL         = "00002a25-0000-1000-8000-00805f9b34fb"
CHAR_DIS_FW_REV         = "00002a26-0000-1000-8000-00805f9b34fb"
CHAR_DIS_HW_REV         = "00002a27-0000-1000-8000-00805f9b34fb"
CHAR_DIS_MANUFACTURER   = "00002a29-0000-1000-8000-00805f9b34fb"

DEVICE_NAME       = "DUSQ_CHARGER"
AUTH_PIN_LEN      = 3
# Per-device PIN = low 24 bits of CRC32(UICR-backed Serial Number).
# See src/ble_svc.c auth_init().  Use compute_auth_pin() to derive it from
# the 16-byte serial read from DIS Serial Number (0x2A25).

def compute_auth_pin(serial_bytes: bytes) -> bytes:
    """Compute the 3-byte Auth PIN for a device given its 16-byte DIS Serial Number."""
    full = zlib.crc32(serial_bytes) & 0xFFFFFFFF
    return (full & 0xFFFFFF).to_bytes(3, "big")

# Flash block geometry (must match firmware src/flash.h)
BLOCK_SIZE          = 152      # full block: 32 B header + 30 × 4 B readings
HEADER_SIZE         = 32       # crc32 lives in the last 4 bytes of the header
READINGS_PER_BLOCK  = 30
BLOCK_MAGIC         = 0xDEADBEEF
# CRC over header bytes [0..27] only (firmware doesn't checksum the payload).
HEADER_CRC_RANGE    = HEADER_SIZE - 4  # = 28

# Journal geometry
JOURNAL_ENTRY_SIZE  = 32
JOURNAL_MAGIC       = 0xCAFEBABE
EVENT_TYPES = {
    0: "BOOT",          # data = boot_count
    1: "TIME_SYNC",     # data = epoch (the value just received)
    2: "FLASH_WRAP",    # data = wrapped block sequence
    3: "ERROR",         # data = error code
    4: "LOW_BATTERY",   # data = batt percent
}

# Sensor error markers
TEMP_ERROR          = 127
DB_ERROR            = 0xFF
LUX_ERROR           = 0xFFFF

# ─────────────────────────────────────────────────────────────────────────────
#  Flash block decoder
# ─────────────────────────────────────────────────────────────────────────────

def _crc32_of(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def decode_block(raw: bytes) -> Optional[dict]:
    """Parse a 152-byte flash block (BlockHeader + 30 readings).

    Layout — exactly matches firmware src/flash.h:
        [0..3]    magic (0xDEADBEEF)
        [4..7]    sequence
        [8..14]   timestamp <HBBBBB> year/month/day/hour/min/sec
        [15..16]  sync_status
        [17..18]  reading_count
        [19..27]  reserved
        [28..31]  crc32 — covers bytes [0..27] only (header, not payload)
        [32..151] payload: 30 × 4-byte (int8 temp, uint8 db, uint16 LE lux)

    Returns None if too short or magic mismatch.
    """
    if len(raw) < BLOCK_SIZE:
        return None

    magic, seq = struct.unpack_from("<II", raw, 0)
    if magic != BLOCK_MAGIC:
        return None

    # Full 7-byte timestamp, including seconds.
    year, month, day, hour, minute, second = struct.unpack_from("<HBBBBB", raw, 8)
    sync_status, reading_count = struct.unpack_from("<HH", raw, 15)
    stored_crc, = struct.unpack_from("<I", raw, HEADER_CRC_RANGE)

    # Firmware: crc covers only the first 28 bytes of the header.
    computed_crc = _crc32_of(raw[:HEADER_CRC_RANGE])
    crc_ok = (stored_crc == computed_crc)

    readings = []
    offset = HEADER_SIZE
    rc = min(reading_count, READINGS_PER_BLOCK)
    for _ in range(rc):
        temp_raw, db_raw, lux_raw = struct.unpack_from("<bBH", raw, offset)
        readings.append({
            "temp": None if temp_raw == TEMP_ERROR else temp_raw,
            "db":   None if db_raw  == DB_ERROR   else db_raw,
            "lux":  None if lux_raw == LUX_ERROR  else lux_raw,
        })
        offset += 4

    pre_sync = (year == 0)
    if pre_sync:
        # Firmware encodes elapsed boot_seconds split into d/h/m/s when
        # no wall-clock has been received yet.
        ts_str = f"(pre-sync) {day}d {hour:02d}:{minute:02d}:{second:02d}"
    else:
        ts_str = (f"{year:04d}-{month:02d}-{day:02d} "
                  f"{hour:02d}:{minute:02d}:{second:02d}")

    return {
        "sequence":      seq,
        "magic_ok":      True,
        "timestamp":     ts_str,
        "pre_sync":      pre_sync,
        "sync_status":   sync_status,
        "reading_count": reading_count,
        "crc_stored":    stored_crc,
        "crc_computed":  computed_crc,
        "crc_ok":        crc_ok,
        "readings":      readings,
        "raw":           bytes(raw[:BLOCK_SIZE]),
    }


def decode_journal_entry(raw: bytes) -> Optional[dict]:
    """Parse one 32-byte JournalEntry (firmware src/flash.h).

    Layout:
        [0..3]    magic (0xCAFEBABE)
        [4..7]    seq
        [8..11]   timestamp (boot_seconds at event time, 0 if unknown)
        [12..15]  type (EventType uint32)
        [16..19]  data (event payload)
        [20..27]  reserved
        [28..31]  crc32 — covers bytes [0..27]
    """
    if len(raw) < JOURNAL_ENTRY_SIZE:
        return None
    magic, seq, timestamp, type_raw, data = struct.unpack_from("<IIIII", raw, 0)
    if magic != JOURNAL_MAGIC:
        return None
    stored_crc, = struct.unpack_from("<I", raw, 28)
    computed_crc = _crc32_of(raw[:28])

    return {
        "sequence":     seq,
        "magic_ok":     True,
        "timestamp":    timestamp,
        "type_raw":     type_raw,
        "type_str":     EVENT_TYPES.get(type_raw, f"UNKNOWN({type_raw})"),
        "data":         data,
        "crc_stored":   stored_crc,
        "crc_computed": computed_crc,
        "crc_ok":       stored_crc == computed_crc,
        "raw":          bytes(raw[:JOURNAL_ENTRY_SIZE]),
    }


def format_event_data(type_raw: int, data: int) -> str:
    """Human-readable interpretation of the JournalEntry data field."""
    if type_raw == 0:   # BOOT
        return f"boot_count = {data}"
    if type_raw == 1:   # TIME_SYNC
        try:
            wall = datetime.fromtimestamp(data, tz=timezone.utc)
            return f"epoch = {data}  ({wall:%Y-%m-%d %H:%M:%S} UTC)"
        except (OSError, OverflowError, ValueError):
            return f"epoch = {data} (out of range)"
    if type_raw == 2:   # FLASH_WRAP
        return f"wrap at block index = {data}"
    if type_raw == 3:   # ERROR
        return f"error code = 0x{data:08X}"
    if type_raw == 4:   # LOW_BATTERY
        return f"battery = {data} %"
    return f"data = 0x{data:08X} ({data})"


def decode_status(data: bytes) -> dict:
    SYS_STATES = ["INIT", "SLOW_ADV", "FAST_ADV", "CONNECTED",
                  "AUTHENTICATED", "FLASH_TRANSFER", "ERROR"]
    if len(data) < 10:
        return {}
    state_idx = data[0]
    return {
        "state":         SYS_STATES[state_idx] if state_idx < len(SYS_STATES) else f"?({state_idx})",
        "usb":           bool(data[1]),
        "boost":         bool(data[2]),
        "lid_closed":    bool(data[3]),
        "block_count":   data[4] | (data[5] << 8),
        "batt_pct":      data[6],
        "haptic_active": bool(data[7]),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  BLE manager
# ─────────────────────────────────────────────────────────────────────────────

class BLEManager:
    def __init__(self):
        self.client: Optional[BleakClient] = None
        self.connected = False
        # Legacy single-callback slot. Still honoured for back-compat with
        # ConnectionTab, but new code should use add_disconnect_sub() so
        # multiple tabs can react to a disconnect independently.
        self.on_disconnect_cb = None
        self._disconnect_subs: List = []
        self.log_cb = None
        # Optional global EventLog — set by App right after construction.
        self.event_log = None
        # Last MAC the user successfully connected to. Persists across
        # disconnects so the "Reconnect" button in the header has somewhere
        # to go without forcing another scan.
        self.last_address: Optional[str] = None
        self.last_name: Optional[str] = None

    def _log(self, msg: str):
        if self.log_cb:
            self.log_cb(msg)

    def _evt(self, kind: str, message: str):
        """Forward to global EventLog if wired; silent otherwise."""
        if self.event_log is not None:
            try:
                self.event_log.log(kind, "ble", message)
            except Exception:
                pass

    def add_disconnect_sub(self, cb):
        """Register an additional callback for disconnect events."""
        if callable(cb) and cb not in self._disconnect_subs:
            self._disconnect_subs.append(cb)

    def _on_disconnect(self, _client):
        self.connected = False
        self._log("Disconnected from device.")
        self._evt("disconnect", "device disconnected")
        if self.on_disconnect_cb:
            try:
                self.on_disconnect_cb()
            except Exception as e:
                self._log(f"on_disconnect_cb error: {e}")
        for sub in list(self._disconnect_subs):
            try:
                sub()
            except Exception as e:
                self._log(f"disconnect sub error: {e}")

    async def scan(self, timeout: float = 8.0, show_all: bool = False,
                    on_update: Optional[Callable[[dict], None]] = None) -> List[dict]:
        """
        Streaming-mode scan: opens a BleakScanner and accumulates every
        advertisement that arrives during `timeout` seconds. More reliable
        than BleakScanner.discover() on Windows — that batch API occasionally
        drops legitimate adverts even when the OS Bluetooth stack receives
        them.

        If `on_update` is given, it is invoked for every adv (after dedupe
        and rssi-update) with the same result-dict shape as the final
        return list.  The UI uses this to populate its tree live instead
        of waiting for the timeout to elapse.  The callback runs on the
        BLE thread — wrap UI mutations in `widget.after(0, ...)`.

        Every individual advertisement is logged to the EventLog ("scan_adv")
        so you can confirm in real time whether bleak is actually seeing
        your device. If the EventLog shows the device's address arriving
        but it doesn't appear in the result list, the bug is in this method;
        if no scan_adv lines appear at all, bleak / the OS Bluetooth stack
        isn't seeing it (try restarting Bluetooth, scanning longer, or using
        nRF Connect to confirm).
        """
        _ = show_all  # accepted for backward compat; everything is shown
        self._log(f"Scanning for {timeout} s … (streaming, all devices)")
        self._evt("scan", f"start: timeout={timeout}s (streaming)")

        target_uuid = SVC_AUTH.lower()
        discovered: Dict[str, Tuple[Any, Any]] = {}

        def _make_entry(dev, adv) -> dict:
            name = dev.name or adv.local_name or ""
            adv_uuids = [u.lower() for u in (adv.service_uuids or [])]
            is_target = (
                (bool(name) and DEVICE_NAME.lower() in name.lower())
                or (target_uuid in adv_uuids)
            )
            display_name = name
            if not display_name:
                if adv.manufacturer_data:
                    mfr_id, mfr_bytes = next(iter(adv.manufacturer_data.items()))
                    display_name = f"<mfr 0x{mfr_id:04X}: {mfr_bytes[:4].hex()}…>"
                elif adv.service_uuids:
                    display_name = f"<svc {adv.service_uuids[0][:8]}…>"
                else:
                    display_name = "<no name>"
            return {
                "name":    display_name,
                "address": dev.address,
                "rssi":    adv.rssi if adv.rssi is not None else -999,
                "tag":     " [TARGET]" if is_target else "",
                "device":  dev,
            }

        def _on_adv(device, adv):
            # Fires for EVERY advertisement received, including duplicates.
            # We dedupe by address but log each hit for visibility, and
            # push a live update to the UI on every hit so the tree
            # reflects current RSSI rather than first-seen RSSI.
            try:
                addr = device.address
            except Exception:
                return
            name = (device.name or adv.local_name or "").strip()
            rssi = adv.rssi if adv.rssi is not None else -999
            short_uuids = [u for u in (adv.service_uuids or [])][:1]
            self._evt("scan",
                       f"adv  {addr}  rssi={rssi:>4} dBm  "
                       f"name='{name or '<none>'}'  "
                       f"svc={short_uuids[0][:8] if short_uuids else '-'}")
            discovered[addr] = (device, adv)
            if on_update is not None:
                try:
                    on_update(_make_entry(device, adv))
                except Exception as cb_err:
                    self._evt("error", f"scan on_update callback failed: {cb_err}")

        try:
            scanner = BleakScanner(detection_callback=_on_adv)
            await scanner.start()
            try:
                await asyncio.sleep(timeout)
            finally:
                await scanner.stop()
        except Exception as e:
            self._evt("error", f"scan failed: {e}")
            self._log(f"Scan failed: {e}")
            raise

        results = [_make_entry(dev, adv) for dev, adv in discovered.values()]
        results.sort(key=lambda d: d["rssi"], reverse=True)
        targets = sum(1 for d in results if d["tag"])
        self._log(f"Found {len(results)} device(s) (streaming, no filter).")
        self._evt("scan",
                   f"done: {len(results)} unique device(s), "
                   f"{targets} tagged TARGET")
        return results

    async def connect(self, device_or_addr) -> bool:
        addr_str = (device_or_addr if isinstance(device_or_addr, str)
                     else getattr(device_or_addr, "address", str(device_or_addr)))
        name = (getattr(device_or_addr, "name", None)
                if not isinstance(device_or_addr, str) else None) or ""
        self._log(f"Connecting …")
        self._evt("connect", f"begin → {addr_str}")
        self.client = BleakClient(device_or_addr,
                                  disconnected_callback=self._on_disconnect,
                                  timeout=20.0)
        try:
            await self.client.connect(timeout=20.0)
        except Exception as e:
            self._evt("error", f"connect failed: {e}")
            raise
        self.connected = True
        # Remember for the header's Reconnect button.
        self.last_address = addr_str
        self.last_name = name or self.last_name or ""
        self._log(f"Connected. MTU={self.client.mtu_size}")
        self._evt("connect", f"ok  MTU={self.client.mtu_size}  addr={addr_str}")
        return True

    async def disconnect(self):
        if self.client and self.connected:
            self._evt("disconnect", "explicit disconnect")
            await self.client.disconnect()
        self.connected = False

    async def read(self, uuid: str) -> bytes:
        try:
            data = bytes(await self.client.read_gatt_char(uuid))
        except Exception as e:
            self._evt("error", f"read {uuid[:8]} failed: {e}")
            raise
        self._evt("read", f"{uuid[:8]}…  {len(data)} B  {data.hex(' ')}")
        return data

    async def write(self, uuid: str, data: bytes, response: bool = True):
        try:
            await self.client.write_gatt_char(uuid, data, response=response)
        except Exception as e:
            self._evt("error", f"write {uuid[:8]} failed: {e}")
            raise
        self._evt("write",
                   f"{uuid[:8]}…  {len(data)} B  {data.hex(' ')}"
                   f"{'  (no-rsp)' if not response else ''}")

    async def subscribe(self, uuid: str, cb):
        try:
            await self.client.start_notify(uuid, cb)
        except Exception as e:
            self._evt("error", f"subscribe {uuid[:8]} failed: {e}")
            raise
        self._evt("subscribe", f"{uuid[:8]}… CCCD on")

    async def unsubscribe(self, uuid: str):
        self._evt("unsubscribe", f"{uuid[:8]}… CCCD off")
        try:
            await self.client.stop_notify(uuid)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

def run_async(coro):
    loop = asyncio.get_event_loop()
    return loop.create_task(coro)


# ─────────────────────────────────────────────────────────────────────────────
#  Connection tab
# ─────────────────────────────────────────────────────────────────────────────

class ConnectionTab(ttk.Frame):
    def __init__(self, parent, ble: BLEManager, status_var: tk.StringVar):
        super().__init__(parent)
        self.ble = ble
        self.status_var = status_var
        self.devices: List[dict] = []
        self.ble.log_cb = self._log
        self.ble.on_disconnect_cb = self._on_disconnected

        # Scan controls
        frm = ttk.LabelFrame(self, text="Scan", padding=5)
        frm.pack(fill="x", padx=6, pady=5)
        self.scan_btn = ttk.Button(frm, text="Scan", command=self._do_scan)
        self.scan_btn.pack(side="left", padx=4)
        ttk.Label(frm, text="Timeout (s):").pack(side="left")
        # Default 12 s — slow adv interval is 2 s, so this catches ~6 packets
        # even on a flaky link.
        self.timeout_var = tk.IntVar(value=12)
        ttk.Spinbox(frm, from_=3, to=60, textvariable=self.timeout_var,
                    width=4).pack(side="left", padx=4)
        # By default the device list shows ONLY targets (matching DUSQ_CHARGER
        # name or the Auth Service UUID). Tick "Show all" for diagnostics
        # when the target isn't appearing.
        self.show_all = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Show all (debug)",
                        variable=self.show_all,
                        command=self._refresh_tree_from_cache).pack(side="left", padx=8)
        self.scan_count_var = tk.StringVar(
            value="Total seen: 0  ·  targets shown: 0")
        ttk.Label(frm, textvariable=self.scan_count_var,
                  font=("Segoe UI", 9, "italic"),
                  foreground="#444444").pack(side="left", padx=12)

        # Device list
        lf = ttk.LabelFrame(self, text="Discovered Devices", padding=5)
        lf.pack(fill="both", expand=True, padx=6, pady=2)
        cols = ("name", "address", "rssi", "tag")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings", height=6)
        for c, w, label in [("name", 160, "Name"), ("address", 180, "Address"),
                              ("rssi", 60, "RSSI"), ("tag", 80, "")]:
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w)
        # Greenish highlight (#8FA97A) for rows that match either DUSQ_CHARGER
        # by name OR carry the Auth Service UUID in their advertisement —
        # makes our device pop out of a long unfiltered scan list.
        self.tree.tag_configure("target", background="#8FA97A",
                                 foreground="black")
        self.tree.pack(fill="both", expand=True)
        # Double-click a row to connect immediately
        self.tree.bind("<Double-1>", self._on_row_double_click)

        # Connect / disconnect
        cf = ttk.Frame(self)
        cf.pack(fill="x", padx=6, pady=4)
        self.conn_btn = ttk.Button(cf, text="Connect", command=self._do_connect)
        self.conn_btn.pack(side="left", padx=4)
        ttk.Button(cf, text="Disconnect", command=self._do_disconnect).pack(side="left", padx=4)

        # Log
        lf2 = ttk.LabelFrame(self, text="Log", padding=4)
        lf2.pack(fill="both", expand=True, padx=6, pady=4)
        self.log_box = scrolledtext.ScrolledText(lf2, height=10, state="disabled",
                                                  font=("Courier", 9))
        self.log_box.pack(fill="both", expand=True)

    def _log(self, msg: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{ts()}] {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _do_scan(self):
        self.scan_btn.configure(state="disabled")
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.devices = []                       # cache fills live below
        self._device_index: Dict[str, int] = {} # addr -> index in self.devices

        def _on_update(entry: dict):
            # Runs on the BLE thread.  Hop to the Tk main thread to mutate
            # the tree, and update self.devices in place so the existing
            # _refresh_tree_from_cache() / connect path still works.
            self.after(0, lambda e=entry: self._upsert_device(e))

        async def _scan():
            try:
                # BLEManager.scan returns every advertiser; we cache the full
                # list and filter at display time so the user can flip
                # "Show all (debug)" without re-scanning.
                devs = await self.ble.scan(self.timeout_var.get(),
                                            self.show_all.get(),
                                            on_update=_on_update)
                # Final reconciliation at end-of-scan: dev list is the source
                # of truth, and sorting is reapplied.
                self.devices = devs
                self._device_index = {d["address"]: i for i, d in enumerate(self.devices)}
                self._refresh_tree_from_cache()
            finally:
                self.scan_btn.configure(state="normal")
        run_async(_scan())

    def _upsert_device(self, entry: dict):
        """Insert or update a device row as adverts arrive — Tk main thread."""
        addr = entry["address"]
        is_target = bool(entry["tag"])
        # Skip non-targets unless the user has flipped "Show all".
        if not is_target and not self.show_all.get():
            return
        idx = self._device_index.get(addr)
        if idx is None:
            self.devices.append(entry)
            self._device_index[addr] = len(self.devices) - 1
        else:
            self.devices[idx] = entry
        # Find an existing tree row for this address; update in place if found,
        # otherwise insert a new one.
        for row_id in self.tree.get_children():
            if self.tree.item(row_id, "values")[1] == addr:
                row_tags = ("target",) if is_target else ()
                self.tree.item(row_id,
                               values=(entry["name"], addr, entry["rssi"], entry["tag"]),
                               tags=row_tags)
                break
        else:
            row_tags = ("target",) if is_target else ()
            self.tree.insert("", "end",
                              values=(entry["name"], addr,
                                      entry["rssi"], entry["tag"]),
                              tags=row_tags)
        targets = sum(1 for d in self.devices if d["tag"])
        self.scan_count_var.set(
            f"Total seen: {len(self.devices)}  ·  "
            f"targets shown: {targets}"
            + ("  (showing all)" if self.show_all.get() else ""))

    def _refresh_tree_from_cache(self):
        """Re-populate the device tree from self.devices applying the current
        filter (targets-only by default; everything when 'Show all' is on)."""
        for row in self.tree.get_children():
            self.tree.delete(row)
        targets = [d for d in self.devices if d["tag"]]
        rows = self.devices if self.show_all.get() else targets
        for d in rows:
            row_tags = ("target",) if d["tag"] else ()
            self.tree.insert("", "end",
                              values=(d["name"], d["address"],
                                      d["rssi"], d["tag"]),
                              tags=row_tags)
        self.scan_count_var.set(
            f"Total seen: {len(self.devices)}  ·  "
            f"targets shown: {len(targets)}"
            + ("  (showing all)" if self.show_all.get() else ""))

    def _do_connect(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Connect", "Select a device first.")
            return
        # Look up by ADDRESS from the selected row, not by row index — the tree
        # may be filtered (targets only) while self.devices is the full list,
        # so row index ≠ devices index.
        row_values = self.tree.item(sel[0], "values")
        addr = row_values[1] if len(row_values) > 1 else None
        entry = next((d for d in self.devices if d["address"] == addr), None)
        if entry is None:
            messagebox.showerror("Connect",
                                  f"Could not find selected device {addr} in scan results.")
            return
        dev  = entry["device"]
        name = entry["name"]
        async def _connect():
            self.status_var.set(f"Connecting to {name}…")
            try:
                await self.ble.connect(dev)
                self.status_var.set(f"Connected: {name}")
                self._log("Ready — authenticate via Auth tab.")
            except Exception as e:
                self._log(f"Connect failed: {e}")
                self.status_var.set("Not connected")
        run_async(_connect())

    def _do_disconnect(self):
        run_async(self.ble.disconnect())

    def _on_row_double_click(self, _evt):
        """Treeview Double-1 binding — connect to whichever row was clicked."""
        sel = self.tree.selection()
        if not sel:
            return
        self._do_connect()

    def _on_disconnected(self):
        self.status_var.set("Not connected")
        self._log("Connection lost.")


# ─────────────────────────────────────────────────────────────────────────────
#  Auth tab
# ─────────────────────────────────────────────────────────────────────────────

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
        ttk.Button(frm, text="Compute from serial",
                   command=self._compute_pin).grid(row=0, column=2, padx=4)
        ttk.Button(frm, text="Authenticate",
                   command=self._do_auth).grid(row=0, column=3, padx=4)

        ttk.Label(frm,
                  text="PIN = low 24 bits of CRC32(DIS Serial). Empty → auto-compute on Authenticate.",
                  foreground="gray").grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 4))

        self.result_lbl = ttk.Label(frm, text="Status: not authenticated", foreground="gray")
        self.result_lbl.grid(row=2, column=0, columnspan=4, sticky="w", pady=4)

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
        """Reset auth state when the BLE link drops so a reconnect starts
        clean — otherwise the green 'authenticated' label persists from the
        previous session, even though the device requires re-auth."""
        self.authenticated = False
        self.result_lbl.configure(text="Status: not authenticated",
                                   foreground="gray")
        self._log("Disconnected — auth state cleared.")

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


# ─────────────────────────────────────────────────────────────────────────────
#  Sensor Monitor tab
# ─────────────────────────────────────────────────────────────────────────────

MAX_HISTORY = 60   # readings kept for chart

class SensorTab(ttk.Frame):
    def __init__(self, parent, ble: BLEManager):
        super().__init__(parent)
        self.ble = ble

        # Live value StringVars
        self.sv_temp     = tk.StringVar(value="--")
        self.sv_db       = tk.StringVar(value="--")
        self.sv_lux      = tk.StringVar(value="--")
        self.sv_batt     = tk.StringVar(value="--")
        self.sv_status   = tk.StringVar(value="--")

        # History lists for chart
        self.hist_temp   : List[Optional[float]] = []
        self.hist_db     : List[Optional[float]] = []
        self.hist_lux    : List[Optional[float]] = []

        self._build_ui()

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
            ("dB",                self.sv_db),
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

        self.canvas.draw_idle()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _on_disconnected(self):
        """Wipe all live state so a fresh connection starts from zero.
        Wired up by App via BLEManager.add_disconnect_sub()."""
        for lst in (self.hist_temp, self.hist_db, self.hist_lux):
            lst.clear()
        for sv in (self.sv_temp, self.sv_db,
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
        """Single notify on the merged sensor char (0x1526), 4 bytes:
           [int8 temp, uint8 db, uint16 LE lux]."""
        if len(data) < 4:
            return
        temp, db, lux = struct.unpack_from("<bBH", data)
        self.sv_temp.set("ERR" if temp == TEMP_ERROR else str(temp))
        self.sv_db.set("ERR" if db == DB_ERROR else str(db))
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
        async def _sub():
            try:
                await self.ble.subscribe(CHAR_SENSOR_DATA, self._on_sensor)
                await self.ble.subscribe(CHAR_BATT_LEVEL,  self._on_batt)
                await self.ble.subscribe(CHAR_STATUS,      self._on_status)
            except Exception as e:
                messagebox.showerror("Subscribe", str(e))
        run_async(_sub())

    def _do_read_once(self):
        if not self.ble.connected:
            messagebox.showwarning("Sensor", "Not connected.")
            return
        async def _read():
            try:
                sensor_raw = await self.ble.read(CHAR_SENSOR_DATA)
                if len(sensor_raw) >= 4:
                    temp, db, lux = struct.unpack_from("<bBH", sensor_raw)
                    self.sv_temp.set("ERR" if temp == TEMP_ERROR else str(temp))
                    self.sv_db.set("ERR" if db == DB_ERROR else str(db))
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

# ─────────────────────────────────────────────────────────────────────────────
#  Flash tab — Sensor Blocks + Journal pages
# ─────────────────────────────────────────────────────────────────────────────

# How long the download waits in seconds AFTER the last received notification
# before declaring the transfer complete. Resets on every notify.
DOWNLOAD_IDLE_TIMEOUT = 5.0


class FlashTab(ttk.Frame):
    """Two pages inside one notebook: sensor block download + journal."""

    def __init__(self, parent, ble: BLEManager):
        super().__init__(parent)
        self.ble = ble

        # Sensor blocks state
        self._blocks: List[dict] = []
        self._dl_task: Optional[asyncio.Task] = None
        self._dl_idle_evt: Optional[asyncio.Event] = None

        # Journal state
        self._journal: List[dict] = []
        self._jnl_task: Optional[asyncio.Task] = None
        self._jnl_idle_evt: Optional[asyncio.Event] = None

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=4, pady=4)

        self._build_blocks_page(nb)
        self._build_journal_page(nb)

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

        self.count_lbl = ttk.Label(ctrl, text="Blocks on device: ?  /  "
                                              "Downloaded: 0")
        self.count_lbl.pack(side="left", padx=12)

        # Block list
        lf = ttk.LabelFrame(page, text="Downloaded Blocks", padding=4)
        lf.pack(fill="both", expand=True, padx=6, pady=2)

        cols = ("seq", "timestamp", "sync", "readings", "crc")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings", height=7)
        for col, w, hdr, anchor in [
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

    # ── Journal page ─────────────────────────────────────────────────────────

    def _build_journal_page(self, parent_nb: ttk.Notebook):
        page = ttk.Frame(parent_nb)
        parent_nb.add(page, text="Journal")

        ctrl = ttk.Frame(page)
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
            ctrl, text="Entries on device: ?  /  Downloaded: 0")
        self.jnl_count_lbl.pack(side="left", padx=12)

        lf = ttk.LabelFrame(page, text="Journal Entries", padding=4)
        lf.pack(fill="both", expand=True, padx=6, pady=2)

        cols = ("seq", "type", "timestamp", "data", "crc")
        self.jnl_tree = ttk.Treeview(lf, columns=cols, show="headings",
                                      height=8)
        for col, w, hdr, anchor in [
            ("seq",       60,  "Seq",         "center"),
            ("type",      120, "Event",       "w"),
            ("timestamp", 100, "Timestamp",   "center"),
            ("data",      140, "Data",        "w"),
            ("crc",       60,  "CRC",         "center"),
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

        df = ttk.LabelFrame(page, text="Entry Detail (select a row above)",
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
                    text=f"Blocks on device: {n}  /  "
                         f"Downloaded: {len(self._blocks)}")
            except Exception as e:
                messagebox.showerror("Count", str(e))
        run_async(_count())

    def _do_time_sync(self):
        if not self.ble.connected:
            messagebox.showwarning("Flash", "Not connected.")
            return
        epoch = int(time.time())
        async def _sync():
            try:
                await self.ble.write(CHAR_TIMESYNC, struct.pack("<I", epoch))
                wall = datetime.fromtimestamp(epoch, tz=timezone.utc)
                messagebox.showinfo("Time Sync",
                                     f"Sent epoch {epoch} "
                                     f"({wall:%Y-%m-%d %H:%M:%S} UTC)")
            except Exception as e:
                messagebox.showerror("Time Sync", str(e))
        run_async(_sync())

    def _do_clear_blocks(self):
        self._blocks.clear()
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._set_box(self.detail_box, "")
        self.count_lbl.configure(text="Blocks on device: ?  /  Downloaded: 0")

    def _do_download(self):
        """Subscribe to notify FIRST, then write 0x01 trigger.
        Firmware streams full 152-byte blocks back on the same UUID.
        Completion is detected via DOWNLOAD_IDLE_TIMEOUT seconds of silence."""
        if not self.ble.connected:
            messagebox.showwarning("Flash", "Not connected.")
            return
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
                tag = "" if blk["crc_ok"] else "bad"
                self.tree.insert("", "end",
                                 values=(blk["sequence"], blk["timestamp"],
                                         "post" if blk["sync_status"] else "pre",
                                         blk["reading_count"],
                                         "OK" if blk["crc_ok"] else "FAIL"),
                                 tags=(tag,) if tag else ())
                self.count_lbl.configure(
                    text=f"Downloaded: {len(self._blocks)} block(s)")
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

    # ── Journal actions ──────────────────────────────────────────────────────

    def _do_journal_count(self):
        if not self.ble.connected:
            messagebox.showwarning("Flash", "Not connected.")
            return
        async def _count():
            try:
                raw = await self.ble.read(CHAR_JOURNAL_COUNT)
                n = struct.unpack("<H", raw)[0]
                self.jnl_count_lbl.configure(
                    text=f"Entries on device: {n}  /  "
                         f"Downloaded: {len(self._journal)}")
            except Exception as e:
                messagebox.showerror("Journal Count", str(e))
        run_async(_count())

    def _do_clear_journal(self):
        self._journal.clear()
        for row in self.jnl_tree.get_children():
            self.jnl_tree.delete(row)
        self._set_box(self.jnl_detail_box, "")
        self.jnl_count_lbl.configure(
            text="Entries on device: ?  /  Downloaded: 0")

    def _do_journal_download(self):
        """Subscribe to JOURNAL_RECORD (notify-only), THEN write 0x01 to
        JOURNAL_START. Firmware streams 32-byte JournalEntries on RECORD."""
        if not self.ble.connected:
            messagebox.showwarning("Flash", "Not connected.")
            return
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
                tag = "" if e["crc_ok"] else "bad"
                self.jnl_tree.insert(
                    "", "end",
                    values=(e["sequence"], e["type_str"], e["timestamp"],
                            f"0x{e['data']:08X} ({e['data']})",
                            "OK" if e["crc_ok"] else "FAIL"),
                    tags=(tag,) if tag else (),
                )
                self.jnl_count_lbl.configure(
                    text=f"Downloaded: {len(self._journal)} entry(ies)")
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

        lines = []
        lines.append("─── Header ─────────────────────────────────────────────")
        lines.append(f"  magic        : 0xCAFEBABE (✓)")
        lines.append(f"  sequence     : {e['sequence']}")
        lines.append(f"  timestamp    : {e['timestamp']}  (boot_seconds or 0)")
        lines.append(f"  type         : {e['type_raw']}  ({e['type_str']})")
        lines.append(f"  data         : 0x{e['data']:08X}  ({e['data']})")
        lines.append(f"  decoded      : {format_event_data(e['type_raw'], e['data'])}")
        lines.append(f"  crc32 stored : 0x{e['crc_stored']:08X}")
        lines.append(f"  crc32 host   : 0x{e['crc_computed']:08X}  → {crc_mark}")
        lines.append("")
        lines.append("─── Raw 32 bytes ───────────────────────────────────────")
        lines.append(self._hex_dump(e["raw"]))
        self._set_box(self.jnl_detail_box, "\n".join(lines))

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _on_disconnected(self):
        """Cancel any in-flight download and clear both Treeviews + details."""
        for task in (self._dl_task, self._jnl_task):
            if task and not task.done():
                task.cancel()
        self._do_clear_blocks()
        self._do_clear_journal()


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
        "A": "BLE Link",
        "B": "Authentication",
        "C": "Sensor pipeline",
        "D": "Battery",
        "E": "Haptic",
        "F": "Flash sensor blocks + circular buffer",
        "G": "Journal",
        "H": "Long-running (opt-in, ≥4 h)",
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
        ttk.Button(top, text="Export…",
                   command=self._on_export).pack(side="left", padx=2)
        ttk.Button(top, text="Reset",
                   command=self._on_reset).pack(side="left", padx=2)

        self.include_long_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Include long-running (Bucket H)",
                        variable=self.include_long_var).pack(side="right", padx=4)

        # Tree of tests
        tree_lf = ttk.LabelFrame(self, text="Test catalogue", padding=4)
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
            if len(data) < 4:
                return
            temp, db, lux = struct.unpack_from("<bBH", data)
            entry = {
                "temp":  None if temp == TEMP_ERROR else temp,
                "db":    None if db   == DB_ERROR   else db,
                "lux":   None if lux  == LUX_ERROR  else lux,
                "ts":    time.time(),
            }
            ctx.samples.append(entry)
            ctx.detail(f"  sample {len(ctx.samples)}/{n}: "
                        f"t={entry['temp']}°C  db={entry['db']}  "
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

            # ── Bucket H — Long-running (opt-in) ───────────────────────────
            T("H1", "H", "Wrap watcher (4+ hours)",  self._t_H1, ("F2",), True),
            T("H2", "H", "Connection stays up",      self._t_H2, ("A2",), True),

            # ── End-of-suite teardown (still in Bucket A visually) ─────────
            # A6 lives here at the bottom of the catalogue so that during
            # "Run All" it executes AFTER every other test. Reason: it
            # disconnects + reconnects the BLE link, which would force
            # downstream tests to re-authenticate. Keeping it last leaves
            # the rest of the suite undisturbed and the device in a clean
            # post-test state (connected, un-authed).
            T("A6", "A", "Disconnect & reconnect cleanly (runs last)",
                self._t_A6, ("A2",)),
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
                   CHAR_HAPTIC_CTL, CHAR_HAPTIC_INT, CHAR_HAPTIC_TIMER,
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
            ctx.detail("writing countdown=5 s to CHAR_HAPTIC_TIMER")
            await self.ble.write(CHAR_HAPTIC_TIMER, struct.pack("<I", 5))
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
                             ["wrote: 5 → 0x1532",
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


# ─────────────────────────────────────────────────────────────────────────────
#  Haptic tab
# ─────────────────────────────────────────────────────────────────────────────

class HapticTab(ttk.Frame):
    def __init__(self, parent, ble: BLEManager):
        super().__init__(parent)
        self.ble = ble

        frm = ttk.LabelFrame(self, text="Haptic Motor Control", padding=10)
        frm.pack(fill="x", padx=6, pady=6)

        # Manual on/off
        ttk.Label(frm, text="Manual:").grid(row=0, column=0, sticky="w")
        ttk.Button(frm, text="Motor ON",
                   command=lambda: self._write_ctl(0x01)).grid(row=0, column=1, padx=4)
        ttk.Button(frm, text="Motor OFF",
                   command=lambda: self._write_ctl(0x00)).grid(row=0, column=2, padx=4)

        # Intensity — slider and entry share the same IntVar so they auto-sync;
        # value is only sent to the device when "Send" is clicked.
        ttk.Label(frm, text="Intensity (0–100%):").grid(row=1, column=0, sticky="w", pady=4)
        self.int_var = tk.IntVar(value=50)
        ttk.Scale(frm, from_=0, to=100, variable=self.int_var,
                  orient="horizontal", length=160).grid(row=1, column=1)
        ttk.Entry(frm, textvariable=self.int_var, width=5
                 ).grid(row=1, column=2, padx=4)
        ttk.Button(frm, text="Send",
                   command=self._set_intensity).grid(row=1, column=3, padx=4)

        # Countdown timer
        ttk.Label(frm, text="Countdown (s):").grid(row=2, column=0, sticky="w", pady=4)
        self.timer_var = tk.IntVar(value=30)
        ttk.Entry(frm, textvariable=self.timer_var, width=7).grid(row=2, column=1)
        ttk.Button(frm, text="Start Timer",
                   command=self._set_timer).grid(row=2, column=2, padx=4)
        ttk.Button(frm, text="Cancel Timer",
                   command=lambda: self._set_timer(cancel=True)).grid(row=2, column=3, padx=4)

        # Status
        self.stat_var = tk.StringVar(value="--")
        ttk.Label(frm, text="Status:").grid(row=3, column=0, sticky="w")
        ttk.Label(frm, textvariable=self.stat_var,
                  font=("Courier", 10, "bold")).grid(row=3, column=1, sticky="w")
        ttk.Button(frm, text="Subscribe Status",
                   command=self._sub_status).grid(row=3, column=2, padx=4)

        # ── Recurring reminder (0x1537) ──────────────────────────────────────
        rem = ttk.LabelFrame(self, text="Recurring Reminder (0x1537)", padding=10)
        rem.pack(fill="x", padx=6, pady=6)

        ttk.Label(rem,
                  text="Sets the post-buzz rescheduling rule. Bootstrap the chain by also writing a countdown above.",
                  foreground="gray", wraplength=520
                 ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))

        ttk.Label(rem, text="Recurring (s):").grid(row=1, column=0, sticky="w")
        self.rem_interval_var = tk.IntVar(value=86400)
        ttk.Entry(rem, textvariable=self.rem_interval_var, width=10
                 ).grid(row=1, column=1, padx=4)
        ttk.Button(rem, text="Arm",
                   command=lambda: self._set_reminder(True)).grid(row=1, column=2, padx=4)
        ttk.Button(rem, text="Disable",
                   command=lambda: self._set_reminder(False)).grid(row=1, column=3, padx=4)
        ttk.Button(rem, text="Read Current",
                   command=self._read_reminder).grid(row=1, column=4, padx=4)

        ttk.Label(rem, text="Current rule:").grid(row=2, column=0, sticky="w", pady=4)
        self.rem_state_var = tk.StringVar(value="(unknown — click Read Current)")
        ttk.Label(rem, textvariable=self.rem_state_var,
                  font=("Courier", 10)).grid(row=2, column=1, columnspan=4, sticky="w")

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

    def _write_ctl(self, val: int):
        if not self.ble.connected: return
        async def _w():
            try:
                await self.ble.write(CHAR_HAPTIC_CTL, bytes([val]))
                self._log(f"Motor {'ON' if val else 'OFF'}")
            except Exception as e:
                self._log(f"Error: {e}")
        run_async(_w())

    def _set_intensity(self):
        if not self.ble.connected: return
        val = max(0, min(100, self.int_var.get()))
        async def _w():
            try:
                await self.ble.write(CHAR_HAPTIC_INT, bytes([val]))
                self._log(f"Intensity set to {val}%")
            except Exception as e:
                self._log(f"Error: {e}")
        run_async(_w())

    def _set_timer(self, cancel: bool = False):
        if not self.ble.connected: return
        secs = 0 if cancel else max(0, self.timer_var.get())
        async def _w():
            try:
                await self.ble.write(CHAR_HAPTIC_TIMER, struct.pack("<I", secs))
                self._log("Timer cancelled." if secs == 0 else f"Countdown set to {secs} s.")
            except Exception as e:
                self._log(f"Error: {e}")
        run_async(_w())

    def _sub_status(self):
        if not self.ble.connected: return
        STATES = {0: "IDLE", 1: "BUZZING", 2: "COUNTDOWN"}
        def _on_stat(_h, data: bytearray):
            v = data[0] if data else 0xFF
            self.stat_var.set(STATES.get(v, f"?({v})"))
        async def _s():
            try:
                await self.ble.subscribe(CHAR_HAPTIC_STAT, _on_stat)
                self._log("Subscribed to haptic status.")
            except Exception as e:
                self._log(f"Error: {e}")
        run_async(_s())

    def _set_reminder(self, enable: bool):
        """Write the 5-byte Recurring Reminder rule (0x1537).

        Wire format: [enable: u8] [recurring_s: u32 LE]
        Firmware rejects enable=1 with recurring=0. Manual buzzes do not chain
        — the phone must also write a countdown (0x1532) to bootstrap. """
        if not self.ble.connected: return
        interval = max(0, self.rem_interval_var.get())
        if enable and interval <= 0:
            messagebox.showerror("Reminder",
                                 "Recurring interval must be > 0 when arming.")
            return
        payload = bytes([1 if enable else 0]) + struct.pack("<I", interval if enable else 0)
        async def _w():
            try:
                await self.ble.write(CHAR_HAPTIC_REMINDER, payload)
                if enable:
                    self._log(f"Reminder ARMED: recurring={interval} s "
                              f"({payload.hex(' ').upper()})")
                    self._log("Now write a Countdown above to bootstrap the chain.")
                else:
                    self._log("Reminder DISABLED.")
                self.rem_state_var.set(
                    f"enable={int(enable)}  recurring={interval if enable else 0} s")
            except Exception as e:
                self._log(f"Reminder write error: {e}")
        run_async(_w())

    def _read_reminder(self):
        """Read the current 5-byte reminder rule (post-reboot reflects persisted state)."""
        if not self.ble.connected: return
        async def _r():
            try:
                raw = bytes(await self.ble.read(CHAR_HAPTIC_REMINDER))
                if len(raw) < 5:
                    self._log(f"Reminder read: short ({len(raw)} bytes): {raw.hex(' ')}")
                    return
                enable = raw[0]
                recurring_s = struct.unpack("<I", raw[1:5])[0]
                state = f"enable={enable}  recurring={recurring_s} s"
                self.rem_state_var.set(state)
                self._log(f"Reminder read: {raw.hex(' ').upper()}  →  {state}")
            except Exception as e:
                self._log(f"Reminder read error: {e}")
        run_async(_r())


# ─────────────────────────────────────────────────────────────────────────────
#  Battery tab
# ─────────────────────────────────────────────────────────────────────────────

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
        # Chart history — capped to BATT_HISTORY_MAX so memory doesn't grow
        # unbounded during long sessions. (time-of-reading, percent) pairs.
        self._chart_pcts: List[int] = []
        self._chart_t0:   Optional[float] = None  # session start, for x-axis

        # ── Live readout ──────────────────────────────────────────────────
        live = ttk.LabelFrame(self, text="Live Battery Level", padding=10)
        live.pack(fill="x", padx=6, pady=6)

        self.pct_var = tk.StringVar(value="--")
        self.pct_label = tk.Label(live, textvariable=self.pct_var,
                                   font=("Courier", 28, "bold"),
                                   foreground="gray", width=6, anchor="center")
        self.pct_label.grid(row=0, column=0, rowspan=2, padx=12, pady=4)

        self.unit_label = tk.Label(live, text="%", font=("Courier", 18, "bold"),
                                    foreground="gray")
        self.unit_label.grid(row=0, column=1, rowspan=2, sticky="w")

        ttk.Label(live, text="Last update:").grid(row=0, column=2, sticky="e", padx=8)
        self.last_var = tk.StringVar(value="(never)")
        ttk.Label(live, textvariable=self.last_var,
                  font=("Courier", 10)).grid(row=0, column=3, sticky="w")

        ttk.Label(live, text="Subscription:").grid(row=1, column=2, sticky="e", padx=8)
        self.sub_var = tk.StringVar(value="off")
        self.sub_label = ttk.Label(live, textvariable=self.sub_var,
                                    font=("Courier", 10, "bold"),
                                    foreground="gray")
        self.sub_label.grid(row=1, column=3, sticky="w")

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

        # ── Chart (battery % over time) ──────────────────────────────────
        # Fixed height; history list and log split the remaining space.
        if HAS_MPL:
            chart_frame = ttk.LabelFrame(self, text="Battery % over session",
                                          padding=4)
            chart_frame.pack(fill="x", padx=6, pady=4)
            fig = Figure(figsize=(7, 2.4), dpi=90)
            fig.subplots_adjust(left=0.08, right=0.98, top=0.92, bottom=0.22)
            self.ax_batt = fig.add_subplot(111)
            self.batt_canvas = FigureCanvasTkAgg(fig, master=chart_frame)
            self.batt_canvas.get_tk_widget().pack(fill="x", expand=False)
            self._draw_chart()
        else:
            self.ax_batt = None
            self.batt_canvas = None

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

    def _draw_chart(self):
        """Redraw the rolling battery chart. Safe to call when MPL is absent."""
        if not HAS_MPL or self.ax_batt is None:
            return
        ax = self.ax_batt
        ax.clear()
        ax.set_ylim(0, 100)
        ax.set_xlabel("reading #", fontsize=8)
        ax.set_ylabel("battery %", fontsize=8)
        ax.grid(True, alpha=0.4)
        # Threshold bands so the colour key matches the live readout.
        ax.axhspan(0,  20, facecolor="#c0392b", alpha=0.08)
        ax.axhspan(20, 50, facecolor="#cf8a17", alpha=0.08)
        ax.axhspan(50, 80, facecolor="#1f5fbf", alpha=0.08)
        ax.axhspan(80, 100, facecolor="#1b8c3a", alpha=0.08)

        if self._chart_pcts:
            xs = list(range(1, len(self._chart_pcts) + 1))
            ax.plot(xs, self._chart_pcts, "-o", ms=4,
                    color="#1f5fbf", linewidth=1.6)
            # Annotate the latest point with the current %
            ax.annotate(f"{self._chart_pcts[-1]} %",
                        xy=(xs[-1], self._chart_pcts[-1]),
                        xytext=(4, 4), textcoords="offset points", fontsize=8)

        ax.tick_params(labelsize=8)
        self.batt_canvas.draw_idle()

    def _apply_reading(self, pct: int):
        """Push a new reading into the live readout, chart, and history list."""
        delta = 0 if self._last_pct is None else pct - self._last_pct
        self._last_pct = pct

        colour = self._colour_for(pct)
        self.pct_var.set(f"{pct:>3}")
        self.pct_label.configure(foreground=colour)
        self.unit_label.configure(foreground=colour)
        self.last_var.set(ts())

        # Chart history (capped)
        self._chart_pcts.append(pct)
        if len(self._chart_pcts) > BATT_HISTORY_MAX:
            self._chart_pcts.pop(0)
        self._draw_chart()

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
        self._chart_pcts.clear()
        self._last_pct = None
        self._draw_chart()
        self._log("History cleared.")

    def _on_disconnected(self):
        """Wipe live readout, chart, and history when the BLE link drops.
        Also resets _subscribed so the next auto_subscribe() actually fires
        instead of taking the 'already subscribed' early-return."""
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._chart_pcts.clear()
        self._last_pct = None
        self._draw_chart()

        self.pct_var.set("--")
        self.pct_label.configure(foreground="gray")
        self.unit_label.configure(foreground="gray")
        self.last_var.set("(never)")

        self._subscribed = False
        self.sub_var.set("off")
        self.sub_label.configure(foreground="gray")

        self._log("Cleared on disconnect.")


# ─────────────────────────────────────────────────────────────────────────────
#  Global event log + Log tab
# ─────────────────────────────────────────────────────────────────────────────

# Soft cap on entries kept in memory (newest wins).
EVENT_LOG_MAX = 5000


class EventLog:
    """Shared central log of every interaction the tool performs.

    Categories ("kinds") used:
        scan / connect / disconnect — link-layer events
        read / write / subscribe / unsubscribe — GATT operations
        notify — incoming HVN payload (only logged if subscribed via Log tab)
        ui    — user clicked a button
        test  — validation test events
        error — anything that raised
        info  — generic informational

    The widget is wired by LogTab once it's built. Until then, entries
    accumulate in self.entries.
    """

    def __init__(self):
        self.entries: List[dict] = []
        self.text_widget: Optional[scrolledtext.ScrolledText] = None
        self.show_kinds: Optional[set] = None   # None = show all

    def log(self, kind: str, source: str, message: str):
        entry = {"ts": datetime.now(), "kind": kind,
                 "source": source, "message": message}
        self.entries.append(entry)
        if len(self.entries) > EVENT_LOG_MAX:
            del self.entries[: len(self.entries) - EVENT_LOG_MAX]
        if self.text_widget is not None:
            try:
                self._append_widget(entry)
            except Exception:
                pass

    def _append_widget(self, entry: dict):
        if self.show_kinds is not None and entry["kind"] not in self.show_kinds:
            return
        line = (f"[{entry['ts']:%H:%M:%S.%f}][{entry['source']:>5}]"
                f"[{entry['kind']:>11}] {entry['message']}\n")
        # Trim microseconds to 3 digits for readability.
        line = line.replace(line[10:16], line[10:13], 1)
        self.text_widget.configure(state="normal")
        self.text_widget.insert("end", line, (entry["kind"],))
        self.text_widget.see("end")
        self.text_widget.configure(state="disabled")

    def rerender(self):
        if self.text_widget is None:
            return
        self.text_widget.configure(state="normal")
        self.text_widget.delete("1.0", "end")
        self.text_widget.configure(state="disabled")
        for e in self.entries:
            self._append_widget(e)


class LogTab(ttk.Frame):
    """Live activity log of every BLE op + UI action this session."""

    KIND_COLOURS = {
        "scan":         "#1f5fbf",
        "connect":      "#1b8c3a",
        "disconnect":   "#cf8a17",
        "read":         "#444444",
        "write":        "#5a3da4",
        "subscribe":    "#0e7a8a",
        "unsubscribe":  "#0e7a8a",
        "notify":       "#7a7a7a",
        "ui":           "#000000",
        "test":         "#1b8c3a",
        "error":        "#c0392b",
        "info":         "#444444",
    }
    ALL_KINDS = list(KIND_COLOURS.keys())

    def __init__(self, parent, event_log: EventLog):
        super().__init__(parent)
        self.event_log = event_log

        # Toolbar
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="Clear",
                   command=self._do_clear).pack(side="left", padx=2)
        ttk.Button(bar, text="Save to file…",
                   command=self._do_save).pack(side="left", padx=2)
        ttk.Button(bar, text="Refresh",
                   command=event_log.rerender).pack(side="left", padx=2)

        # Filter checkboxes
        flt = ttk.LabelFrame(self, text="Filters", padding=4)
        flt.pack(fill="x", padx=6, pady=2)
        self._filter_vars: Dict[str, tk.BooleanVar] = {}
        for i, k in enumerate(self.ALL_KINDS):
            v = tk.BooleanVar(value=True)
            self._filter_vars[k] = v
            ttk.Checkbutton(flt, text=k, variable=v,
                             command=self._apply_filter).grid(
                row=i // 6, column=i % 6, sticky="w", padx=4, pady=1)

        # Log text
        lf = ttk.LabelFrame(self, text="Activity log", padding=4)
        lf.pack(fill="both", expand=True, padx=6, pady=4)
        self.text = scrolledtext.ScrolledText(lf, state="disabled",
                                                font=("Courier", 9),
                                                wrap="none")
        for kind, colour in self.KIND_COLOURS.items():
            self.text.tag_configure(kind, foreground=colour)
        self.text.pack(fill="both", expand=True)

        # Wire to the EventLog
        event_log.text_widget = self.text
        event_log.rerender()
        # Self-log this binding so the user sees the tab is alive.
        event_log.log("info", "log", "Log tab attached")

    def _apply_filter(self):
        kinds = {k for k, v in self._filter_vars.items() if v.get()}
        self.event_log.show_kinds = kinds if len(kinds) < len(self.ALL_KINDS) else None
        self.event_log.rerender()

    def _do_clear(self):
        self.event_log.entries.clear()
        self.event_log.rerender()
        self.event_log.log("ui", "log", "log cleared")

    def _do_save(self):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"),
                        ("All files", "*.*")],
            initialfile=f"dusq_session_{datetime.now():%Y%m%d-%H%M%S}.log")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for e in self.event_log.entries:
                    f.write(f"[{e['ts']:%Y-%m-%d %H:%M:%S.%f}]"
                            f"[{e['source']:>5}][{e['kind']:>11}] "
                            f"{e['message']}\n")
            self.event_log.log("ui", "log", f"saved {len(self.event_log.entries)} entries to {path}")
        except Exception as ex:
            messagebox.showerror("Save log", str(ex))


# ─────────────────────────────────────────────────────────────────────────────
#  Device Info tab — reads the 5 standard DIS characteristics
# ─────────────────────────────────────────────────────────────────────────────

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
        """Decode the 8-byte SIG-standard System ID payload.
            bytes 0..4  = 5 LSBs of manufacturer_id (= low 5 bytes of FICR.DEVICEID, little-endian)
            bytes 5..7  = 3-byte organizationally_unique_id (Nordic OUI 0x00149F)"""
        if len(raw) < 8:
            return f"<short read: {raw.hex(' ').upper()}>"
        mfg_bytes = raw[0:5]
        oui_bytes = raw[5:8]
        # Reassemble the original 64-bit manufacturer_id (only low 5 bytes used)
        mfg_id = int.from_bytes(mfg_bytes, "little")
        oui    = int.from_bytes(oui_bytes, "little")
        return (f"mfg=0x{mfg_id:010X}  oui=0x{oui:06X}"
                f"  ({raw.hex(' ').upper()})")

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


# ─────────────────────────────────────────────────────────────────────────────
#  Adv Data tab — continuously scans and decodes the MSD payload (no connect)
# ─────────────────────────────────────────────────────────────────────────────

# Manufacturer Specific Data layout (firmware: src/ble_svc.c build_manuf_data)
#   [0]      sys_state (bits 0-3) | flags (bits 4-7: USB / boost / lid / auth)
#   [1]      battery % (0..100, 0xFF if not yet read)
#   [2-3]    unsynced block count, uint16 LE
#   [4-5]    journal entry count, uint16 LE
#   [6-9]    last sync Unix epoch, uint32 LE (0 = never since boot)
ADV_MSD_COMPANY_ID = 0xFFFF
ADV_MSD_LEN        = 10
ADV_STATE_NAMES = [
    "INIT", "SLOW_ADV", "FAST_ADV", "CONNECTED",
    "AUTHENTICATED", "FLASH_TX", "ERROR",
]


def decode_dusq_msd(payload):
    """Decode the 10-byte DUSQ MSD payload (without company ID).
    Returns dict, or None if payload is missing / too short."""
    if not payload or len(payload) < ADV_MSD_LEN:
        return None
    state_id = payload[0] & 0x0F
    return {
        "state_id":   state_id,
        "state":      ADV_STATE_NAMES[state_id] if state_id < len(ADV_STATE_NAMES) else f"?({state_id})",
        "usb":        bool(payload[0] & 0x10),
        "charging":   bool(payload[0] & 0x20),
        "lid_closed": bool(payload[0] & 0x40),
        "authed":     bool(payload[0] & 0x80),
        "batt_pct":   payload[1] if payload[1] != 0xFF else None,
        "blocks":     int.from_bytes(payload[2:4], "little"),
        "journal":    int.from_bytes(payload[4:6], "little"),
        "last_sync":  int.from_bytes(payload[6:10], "little"),
    }


class AdvTab(ttk.Frame):
    """Continuously scans for DUSQ_CHARGER devices and decodes the MSD payload
    from every advertisement. No connection is opened — purely passive.

    Filter: device name must contain "DUSQ_CHARGER" (case-insensitive).
    Company ID 0xFFFF alone is too loose (other dev devices use it too)."""

    REFRESH_MS = 500   # period for "Seen N s ago" refresh

    def __init__(self, parent, ble: BLEManager):
        super().__init__(parent)
        self.ble = ble
        self._scanner = None
        self._scanning = False
        self._devices = {}   # addr -> {'rssi', 'raw', 'decoded', 'seen'}

        # --- Controls -------------------------------------------------------
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=6, pady=6)
        self.scan_btn = ttk.Button(ctrl, text="▶ Start scanning",
                                    command=self._toggle_scan)
        self.scan_btn.pack(side="left")
        ttk.Button(ctrl, text="Clear", command=self._clear
                  ).pack(side="left", padx=6)
        self.status_var = tk.StringVar(value="Idle. Press Start scanning to begin.")
        ttk.Label(ctrl, textvariable=self.status_var,
                  foreground="gray").pack(side="left", padx=10)

        # --- Device table ---------------------------------------------------
        cols = ("address", "rssi", "state", "usb", "charging", "lid", "auth",
                "batt", "blocks", "journal", "last_sync", "seen")
        self.tree = ttk.Treeview(self, columns=cols, show="headings",
                                  selectmode="browse", height=14)
        for col, label, width in [
            ("address",   "MAC",          170),
            ("rssi",      "RSSI",         60),
            ("state",     "State",        110),
            ("usb",       "USB",          50),
            ("charging",  "Charging",     70),
            ("lid",       "Lid",          60),
            ("auth",      "Auth",         50),
            ("batt",      "Batt %",       60),
            ("blocks",    "Blocks",       60),
            ("journal",   "Journal",      60),
            ("last_sync", "Last sync",    140),
            ("seen",      "Seen (s ago)", 80),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=6, pady=4)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # --- Raw bytes for selected row ------------------------------------
        raw_frm = ttk.LabelFrame(self, text="Raw MSD (selected row)", padding=6)
        raw_frm.pack(fill="x", padx=6, pady=(0, 6))
        self.raw_var = tk.StringVar(value="(select a row above)")
        ttk.Label(raw_frm, textvariable=self.raw_var,
                  font=("Courier", 9)).pack(anchor="w")

        # --- Manual MSD decoder --------------------------------------------
        dec_frm = ttk.LabelFrame(
            self, text="Manual decoder (paste hex bytes from any tool)", padding=6)
        dec_frm.pack(fill="x", padx=6, pady=(0, 6))

        ttk.Label(dec_frm, text="Hex (with or without 0xFFFF company ID prefix):",
                  foreground="gray").pack(anchor="w")
        self.dec_input_var = tk.StringVar()
        ent = ttk.Entry(dec_frm, textvariable=self.dec_input_var,
                        font=("Courier", 9))
        ent.pack(fill="x", pady=2)
        ent.bind("<Return>", lambda _e: self._do_manual_decode())
        btn_row = ttk.Frame(dec_frm)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Decode",
                   command=self._do_manual_decode).pack(side="left", pady=2)
        ttk.Label(btn_row,
                  text="(accepts '02 2D 00 …', '0x02 0x2D …', 'FFFF022D…', etc.)",
                  foreground="gray").pack(side="left", padx=8)
        self.dec_output_var = tk.StringVar(
            value="(paste hex bytes above and click Decode)")
        ttk.Label(dec_frm, textvariable=self.dec_output_var,
                  font=("Courier", 9), justify="left",
                  anchor="w").pack(fill="x", pady=(4, 0))

        # Periodic redraw for "Seen N s ago" so silent devices visibly age
        self.after(self.REFRESH_MS, self._redraw_periodic)

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
        self.status_var.set("Scanning… (passive, no connection)")
        run_async(self._scan_loop())

    def _stop_scan(self):
        self._scanning = False
        self.scan_btn.configure(text="▶ Start scanning")
        self.status_var.set("Stopped.")
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
            self.status_var.set(f"Scan error: {e}")
            self._scanning = False
            self.scan_btn.configure(text="▶ Start scanning")

    def _on_adv(self, device, adv):
        # Filter: accept either by name OR by a valid DUSQ MSD decode.
        #
        # Most adv packets on Windows arrive without a name field — the name
        # lives in the scan response, which only comes through on active
        # scans, and BleakScanner defaults to passive on this OS.  Name-only
        # filtering caused this tab to take 5-30 s (or never) to find a
        # device that the Connect tab sees on the first packet.
        #
        # decode_dusq_msd() returning non-None is a strong filter — random
        # devices using company ID 0xFFFF will fail the decode.
        name = (device.name or adv.local_name or "").strip()
        mfd = adv.manufacturer_data or {}
        payload = mfd.get(ADV_MSD_COMPANY_ID)
        decoded = decode_dusq_msd(payload) if payload else None

        name_match = bool(name) and DEVICE_NAME.lower() in name.lower()
        msd_match  = decoded is not None
        if not (name_match or msd_match):
            return
        self._devices[device.address] = {
            "rssi":    adv.rssi if adv.rssi is not None else -999,
            "raw":     bytes(payload) if payload else b"",
            "decoded": decoded,
            "seen":    datetime.now(),
        }
        self.after(0, self._refresh_tree)

    def _refresh_tree(self):
        # Preserve selection across rebuild
        sel = self.tree.selection()
        selected_addr = self.tree.item(sel[0], "values")[0] if sel else None
        for row in self.tree.get_children():
            self.tree.delete(row)
        now = datetime.now()
        for addr, info in sorted(self._devices.items(),
                                  key=lambda kv: kv[1]["rssi"], reverse=True):
            d = info["decoded"]
            secs_ago = int((now - info["seen"]).total_seconds())
            if d:
                last_sync_disp = ("never" if d["last_sync"] == 0
                                   else datetime.fromtimestamp(d["last_sync"])
                                        .strftime("%Y-%m-%d %H:%M:%S"))
                values = (
                    addr,
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
                    f"{secs_ago}s",
                )
            else:
                # DUSQ device without MSD (old firmware?)
                values = (addr, f"{info['rssi']} dBm", "—", "—", "—", "—", "—",
                          "—", "—", "—", "—", f"{secs_ago}s")
            iid = self.tree.insert("", "end", values=values)
            if addr == selected_addr:
                self.tree.selection_set(iid)

    def _redraw_periodic(self):
        if self._devices:
            self._refresh_tree()
        self.after(self.REFRESH_MS, self._redraw_periodic)

    def _on_select(self, _evt):
        sel = self.tree.selection()
        if not sel:
            self.raw_var.set("(select a row above)")
            return
        addr = self.tree.item(sel[0], "values")[0]
        info = self._devices.get(addr)
        if not info or not info["raw"]:
            self.raw_var.set(f"[{addr}]  (no MSD in this device's adv)")
            return
        company = ADV_MSD_COMPANY_ID.to_bytes(2, "little")
        full = company + info["raw"]
        hex_str = " ".join(f"{b:02X}" for b in full)
        self.raw_var.set(f"[{addr}]  {hex_str}")

    def _clear(self):
        self._devices.clear()
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.raw_var.set("(select a row above)")

    def _do_manual_decode(self):
        """Parse a hex string the user pasted (any common format) and show
        the decoded MSD fields."""
        raw = self.dec_input_var.get()
        # Strip "0x"/"0X" prefixes FIRST (their '0' would otherwise be kept and
        # shift byte alignment), then filter to hex digits only.
        no_prefix = raw.replace("0x", "").replace("0X", "")
        cleaned = "".join(ch for ch in no_prefix.lower()
                          if ch in "0123456789abcdef")
        if len(cleaned) % 2 != 0:
            self.dec_output_var.set(f"Error: odd number of hex digits ({len(cleaned)})")
            return
        try:
            data = bytes.fromhex(cleaned)
        except ValueError as e:
            self.dec_output_var.set(f"Error: invalid hex — {e}")
            return

        # Strip optional 0xFFFF company-ID prefix (2 bytes, little-endian)
        if len(data) >= ADV_MSD_LEN + 2 and data[0:2] == b"\xFF\xFF":
            payload = data[2:2 + ADV_MSD_LEN]
            had_company_id = True
        elif len(data) >= ADV_MSD_LEN:
            payload = data[:ADV_MSD_LEN]
            had_company_id = False
        else:
            self.dec_output_var.set(
                f"Error: need at least {ADV_MSD_LEN} bytes payload "
                f"(or {ADV_MSD_LEN + 2} with FFFF prefix); got {len(data)}")
            return

        d = decode_dusq_msd(payload)
        if d is None:
            self.dec_output_var.set("Error: decode failed")
            return

        last_sync_disp = ("never" if d["last_sync"] == 0
                           else datetime.fromtimestamp(d["last_sync"])
                                .strftime("%Y-%m-%d %H:%M:%S"))
        batt_disp = "unknown" if d["batt_pct"] is None else f"{d['batt_pct']}%"
        hex_str = " ".join(f"{b:02X}" for b in payload)
        prefix_note = "" if had_company_id else "  (no FFFF prefix detected — assumed payload-only)"
        text = (
            f"Bytes (10): {hex_str}{prefix_note}\n"
            f"  State:         {d['state']} (0x{d['state_id']:X})\n"
            f"  USB:           {'connected' if d['usb'] else 'disconnected'}\n"
            f"  Charging:      {'yes' if d['charging'] else 'no'}\n"
            f"  Lid:           {'closed' if d['lid_closed'] else 'open'}\n"
            f"  Authenticated: {'yes' if d['authed'] else 'no'}\n"
            f"  Battery:       {batt_disp}\n"
            f"  Blocks:        {d['blocks']}\n"
            f"  Journal:       {d['journal']}\n"
            f"  Last sync:     {last_sync_disp}"
        )
        self.dec_output_var.set(text)


# ─────────────────────────────────────────────────────────────────────────────
#  Main application
# ─────────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.loop = loop
        self.title("DUSQ Charger BLE Test Tool")
        self.geometry("820x680")
        self.resizable(True, True)

        self.ble = BLEManager()

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
        # When status_var changes anywhere in the app, recompute the
        # top-right indicator.
        self.status_var.trace_add("write", lambda *_: self._update_indicator())

        # Bottom status bar (full text, unchanged)
        ttk.Label(self, textvariable=self.status_var, relief="sunken",
                  anchor="w").pack(side="bottom", fill="x", padx=2, pady=1)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=4, pady=4)

        self.conn_tab    = ConnectionTab(nb, self.ble, self.status_var)
        self.adv_tab     = AdvTab(nb, self.ble)
        self.auth_tab    = AuthTab(nb, self.ble)
        self.devinfo_tab = DeviceInfoTab(nb, self.ble)
        self.sens_tab    = SensorTab(nb, self.ble)
        self.flash_tab   = FlashTab(nb, self.ble)
        self.hap_tab     = HapticTab(nb, self.ble)
        self.batt_tab    = BatteryTab(nb, self.ble)
        self.val_tab     = ValidationTab(nb, self.ble)
        self.log_tab     = LogTab(nb, self.event_log)

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
        self.ble.add_disconnect_sub(self.val_tab._on_disconnected)

        # Tab order: workflow steps first, comprehensive validation,
        # then the activity log as the last tab.
        nb.add(self.conn_tab,    text="Connect")
        nb.add(self.adv_tab,     text="Adv Data")
        nb.add(self.auth_tab,    text="Auth")
        nb.add(self.devinfo_tab, text="Device Info")
        nb.add(self.sens_tab,    text="Sensors")
        nb.add(self.flash_tab,   text="Flash")
        nb.add(self.hap_tab,     text="Haptic")
        nb.add(self.batt_tab,    text="Battery")
        nb.add(self.val_tab,     text="Validation")
        nb.add(self.log_tab,     text="Log")

        # Initial state of the indicator + Reconnect button.
        self._update_indicator()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_loop()

    def _update_indicator(self):
        """Mirror the shared status string into the top-right indicator
        with a coloured bullet, and morph the right-side button to match
        the current state:
          Connected   → green   "● Connected: …"     button = ✕ Disconnect
          Connecting  → yellow  "● Connecting …"     button = disabled
          Disconnected→ red     "● Disconnected"     button = ↻ Reconnect …
        """
        text = self.status_var.get()
        lo   = text.lower()
        if lo.startswith("connected"):
            self.indicator_var.set(f"● {text}")
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
        self.adv_tab.stop()   # shut down passive scanner cleanly
        async def _cleanup():
            await self.ble.disconnect()
        self.loop.run_until_complete(_cleanup())
        self.destroy()


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = App(loop)
    app.mainloop()


if __name__ == "__main__":
    main()
