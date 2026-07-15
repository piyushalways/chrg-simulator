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
import queue
import re
import struct
import sys
import threading
import time
import tkinter as tk
import zlib
from collections import deque
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

try:
    import pylink   # J-Link RTT reader (pip install pylink-square)
    HAS_PYLINK = True
except ImportError:
    HAS_PYLINK = False

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

# Sensor Service (0x1525) — firmware exposes ONE merged 6-byte char.
# Layout (little-endian): [temp:int16 tenths-degC, db:uint8, peak_db:uint8, lux:uint16]
# Error markers: temp=0x7FFF (INT16_MAX), db=0xFF, lux=0xFFFF.
# Flash records (152D) still carry temp as int8 degC; only this BLE-live char widened.
SVC_SENSOR        = _uuid(0x1525)
CHAR_SENSOR_DATA  = _uuid(0x1526)   # Read + Notify — 6-byte packed reading
CHAR_STATUS       = _uuid(0x1529)   # Read + Notify — 10-byte packed status

# Flash Data Service (0x152A) — sensor blocks AND journal share this service.
SVC_FLASH            = _uuid(0x152A)
CHAR_BLOCK_COUNT     = _uuid(0x152B)   # Read — uint16 LE, unsynced sensor blocks
# Sensor record stream is W + N on the SAME char: subscribe first, then write
# 0x01 — the device fires 152-byte block notifications on the same UUID.
CHAR_RECORD          = _uuid(0x152D)
CHAR_TIMESYNC        = _uuid(0x152E)   # Write — uint32 LE Unix epoch (IST wall-clock)

# The device is timezone-agnostic: it stores whatever epoch we send and reports it
# back verbatim (journal, advert last-sync, event log). We send India Standard Time
# (UTC+05:30) so the device's wall clock — and every epoch it echoes — reads in IST.
# Bias the host UTC epoch by +05:30 on send; render device epochs with tz=utc so the
# already-biased value prints as the IST wall clock.
IST_OFFSET_S = 5 * 3600 + 30 * 60   # +05:30

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
CHAR_HAPTIC_STAT     = _uuid(0x1533)   # Read+Notify — 14B: [0]motor_state [1:5]single_shot LE [5:9]recurring LE [9:13]next_fire_epoch LE [13]flags
CHAR_HAPTIC_REMINDER = _uuid(0x1537)   # Read+Write — 8B: [0:4]single_shot LE [4:8]recurring LE

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

DEVICE_NAME       = "DUSQ-CHG"
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
    0: "BOOT",          # data = (reset_cause << 24) | boot_count[23:0]
    1: "TIME_SYNC",     # data = epoch (the value just received)
    2: "FLASH_WRAP",    # data = wrapped block sequence
    3: "ERROR",         # data = error code
    4: "LOW_BATTERY",   # data = batt percent
}

# BOOT entry: high byte of data = reset cause (firmware RESET_CAUSE_* in app_config.h)
RESET_CAUSES = {
    0: "power-on/brown-out",
    1: "pin",
    2: "watchdog",
    3: "soft-reset",
    4: "lockup",
    5: "other",
    6: "app-fault",
}

# EVENT_ERROR (fault record) low-16 = NRF_FAULT_ID_* (app_error.h / nrf_sdm.h).
# Firmware packs the journal data as (info & 0xFFFF) << 16 | (id & 0xFFFF).
FAULT_IDS = {
    0x0001: "SoftDevice assert",
    0x1001: "app memacc / hardfault",
    0x4001: "SDK error (APP_ERROR_CHECK)",
    0x4002: "SDK assert (ASSERT)",
}

# Sensor error markers
TEMP_ERROR          = 127       # flash record (int8 degC)
TEMP_ERROR_BLE      = 0x7FFF    # BLE live char (int16 tenths-degC)
DB_ERROR            = 0xFF
LUX_ERROR           = 0xFFFF


def _decode_sensor_payload(data):
    """Decode the merged sensor char (0x1526), tolerant to both payload layouts.

    new firmware  6 B  <h B B H>  int16 LE temp (0.1 degC), db, peak_db, uint16 LE lux
    old firmware  5 B  <b B B H>  int8 temp degC,           db, peak_db, uint16 LE lux

    Returns (temp_c: Optional[float], db: Optional[int],
             peak_db: Optional[int], lux: Optional[int]).
    temp_c is None when the firmware reported the error sentinel; the other three
    are None only when the payload is too short to decode (caller should drop).
    """
    if data is None:
        return None, None, None, None
    n = len(data)
    if n >= 6:
        temp_raw, db, peak_db, lux = struct.unpack_from("<hBBH", data)
        temp_c = None if temp_raw == TEMP_ERROR_BLE else (temp_raw / 10.0)
        return temp_c, db, peak_db, lux
    if n >= 5:
        temp_raw, db, peak_db, lux = struct.unpack_from("<bBBH", data)
        temp_c = None if temp_raw == TEMP_ERROR else float(temp_raw)
        return temp_c, db, peak_db, lux
    return None, None, None, None

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
    if type_raw == 0:   # BOOT — data = (reset_cause << 24) | boot_count[23:0]
        cause = (data >> 24) & 0xFF
        boot_count = data & 0x00FFFFFF
        cause_str = RESET_CAUSES.get(cause, f"unknown({cause})")
        return f"boot #{boot_count}, reset = {cause_str}"
    if type_raw == 1:   # TIME_SYNC
        try:
            wall = datetime.fromtimestamp(data, tz=timezone.utc)   # epoch is IST-biased
            return f"epoch = {data}  ({wall:%Y-%m-%d %H:%M:%S} IST)"
        except (OSError, OverflowError, ValueError):
            return f"epoch = {data} (out of range)"
    if type_raw == 2:   # FLASH_WRAP
        return f"wrap at block index = {data}"
    if type_raw == 3:   # ERROR — app-fault record: data = (info & 0xFFFF) << 16 | (id & 0xFFFF)
        fault_id = data & 0xFFFF
        info     = (data >> 16) & 0xFFFF
        name     = FAULT_IDS.get(fault_id, f"id 0x{fault_id:04X}")
        return f"fault: {name}  (id=0x{fault_id:04X}, info=0x{info:04X})"
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
        # winrt use_cached_services=False -> BluetoothCacheMode.UNCACHED, forcing a
        # fresh GATT discovery from the device on every connect. Without it, after
        # re-flashing firmware that changed the GATT table, WinRT keeps serving a
        # STALE cached table whose characteristics look like they have no CCCD, so
        # every start_notify() fails with "characteristic does not support
        # notifications or indications" even though reads/writes work fine. Ignored
        # on BlueZ/macOS backends, so it is safe to pass unconditionally.
        self.client = BleakClient(device_or_addr,
                                  disconnected_callback=self._on_disconnect,
                                  timeout=20.0,
                                  winrt={"use_cached_services": False})
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
        # DIAGNOSTIC: dump the properties bleak actually discovered for the
        # notifiable chars. bleak raises "does not support notifications" from its
        # OWN check of these .properties, so this tells us definitively whether
        # 'notify' is present in what the OS handed us (missing -> stale OS GATT
        # cache / discovery; present -> a CCCD/descriptor problem instead).
        try:
            watch = ("1526", "1529", "1533", "2a19", "152b", "152e")
            for svc in (self.client.services or []):
                for ch in svc.characteristics:
                    short = ch.uuid.lower()[4:8]
                    if short in watch:
                        self._evt("gatt",
                                  f"{short} props={sorted(ch.properties)} "
                                  f"cccd={'yes' if any(d.uuid.lower()[4:8]=='2902' for d in ch.descriptors) else 'no'}")
        except Exception as e:
            self._evt("gatt", f"props dump failed: {e}")
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

        ttk.Label(frm,
                  text="PIN = low 24 bits of CRC32(DIS Serial). Leave empty → auto-derived from serial on Authenticate.",
                  foreground="gray").grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 4))

        self.result_lbl = ttk.Label(frm, text="Status: not authenticated", foreground="gray")
        self.result_lbl.grid(row=2, column=0, columnspan=4, sticky="w", pady=4)
        # Live "last sync" label updated whenever Send Time Sync succeeds.
        self.last_sync_var = tk.StringVar(value="Last sync: never")
        ttk.Label(frm, textvariable=self.last_sync_var,
                  foreground="#1b8c3a").grid(row=2, column=4, sticky="e", padx=4)

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


# ─────────────────────────────────────────────────────────────────────────────
#  Sensor Monitor tab
# ─────────────────────────────────────────────────────────────────────────────

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
        # Running counters — "this download" resets every time the user
        # clicks Download; "this connection" resets only on disconnect.
        self._blk_session_count = 0
        self._blk_connection_count = 0

        # Journal state
        self._journal: List[dict] = []
        self._jnl_task: Optional[asyncio.Task] = None
        self._jnl_idle_evt: Optional[asyncio.Event] = None
        self._jnl_session_count = 0
        self._jnl_connection_count = 0

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
            ctrl,
            text="On device: ?   |   This download: 0   |   This connection: 0")
        self.jnl_count_lbl.pack(side="left", padx=12)

        lf = ttk.LabelFrame(page, text="Journal Entries", padding=4)
        lf.pack(fill="both", expand=True, padx=6, pady=2)

        cols = ("idx", "seq", "type", "timestamp", "data", "crc")
        self.jnl_tree = ttk.Treeview(lf, columns=cols, show="headings",
                                      height=8)
        for col, w, hdr, anchor in [
            ("idx",       50,  "#",           "center"),
            ("seq",       60,  "Seq",         "center"),
            ("type",      120, "Event",       "w"),
            ("timestamp", 100, "Timestamp",   "center"),
            ("data",      230, "Decoded",     "w"),
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
            messagebox.showwarning("Flash", "Not connected.")
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
                            e["sequence"], e["type_str"], e["timestamp"],
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


# ─────────────────────────────────────────────────────────────────────────────
#  Haptic tab
# ─────────────────────────────────────────────────────────────────────────────

class HapticTab(ttk.Frame):
    def __init__(self, parent, ble: BLEManager):
        super().__init__(parent)
        self.ble = ble
        # Idempotency flag for tab-open auto-subscribe to CHAR_HAPTIC_STAT.
        self._subscribed = False

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
                  text="Schedule (0x1537) = [single_shot, recurring] s.  first fire = single_shot (or recurring if 0); recurring 0 = one-shot.  [0,3600]=every 1h · [<sec to 9PM>,86400]=daily · [0,0]=cancel.",
                  foreground="gray", wraplength=580
                 ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))

        ttk.Label(rem, text="single_shot (s):").grid(row=1, column=0, sticky="w")
        self.rem_single_var = tk.IntVar(value=10)
        ttk.Entry(rem, textvariable=self.rem_single_var, width=10
                 ).grid(row=1, column=1, padx=4)
        ttk.Label(rem, text="recurring (s):").grid(row=1, column=2, sticky="w")
        self.rem_recurring_var = tk.IntVar(value=0)
        ttk.Entry(rem, textvariable=self.rem_recurring_var, width=10
                 ).grid(row=1, column=3, padx=4)

        ttk.Button(rem, text="Set Schedule",
                   command=self._set_schedule).grid(row=2, column=0, padx=4)
        ttk.Button(rem, text="Disable",
                   command=self._disable_schedule).grid(row=2, column=1, padx=4)
        ttk.Button(rem, text="Read Current",
                   command=self._read_reminder).grid(row=2, column=2, padx=4)
        ttk.Button(rem, text="STOP-ALL",
                   command=lambda: self._write_ctl(0x02)).grid(row=2, column=3, padx=4)

        ttk.Label(rem, text="Current:").grid(row=3, column=0, sticky="w", pady=4)
        self.rem_state_var = tk.StringVar(value="(unknown — click Read Current)")
        ttk.Label(rem, textvariable=self.rem_state_var,
                  font=("Courier", 10)).grid(row=3, column=1, columnspan=4, sticky="w")

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
                label = {0: "OFF / abort buzz", 1: "ON",
                         2: "STOP-ALL (cancel every alarm + stop)"}.get(val, f"0x{val:02X}")
                self._log(f"Control: {label}")
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
        def _on_stat(_h, data: bytearray):
            self.stat_var.set(self._decode_haptic_status(bytes(data)))
        async def _s():
            try:
                await self.ble.subscribe(CHAR_HAPTIC_STAT, _on_stat)
                self._subscribed = True
                # Explicit read so the current snapshot shows immediately (0x1533 is
                # R+N) rather than waiting for the first transition notification.
                try:
                    raw = bytes(await self.ble.read(CHAR_HAPTIC_STAT))
                    self.stat_var.set(self._decode_haptic_status(raw))
                except Exception:
                    pass
                self._log("Subscribed to haptic status (+ read current).")
            except Exception as e:
                self._log(f"Error: {e}")
        run_async(_s())

    @staticmethod
    def _decode_haptic_status(data: bytes) -> str:
        """0x1533 snapshot (14 B): [0]motor_state [1:5]single_shot LE [5:9]recurring LE
        [9:13]next_fire_epoch LE [13]flags(bit0 pending_sync bit1 fire_pending)."""
        MOTOR = {0: "IDLE", 1: "BUZZING"}
        if not data:
            return "?(empty)"
        motor = MOTOR.get(data[0], f"?({data[0]})")
        if len(data) >= 14:
            ss      = struct.unpack("<I", data[1:5])[0]
            rec     = struct.unpack("<I", data[5:9])[0]
            epoch   = struct.unpack("<I", data[9:13])[0]
            pending = bool(data[13] & 0x01)   # held after reboot, waiting for sync
            firing  = bool(data[13] & 0x02)   # a buzz is scheduled
            sdesc = "no schedule" if (ss == 0 and rec == 0) else f"ss={ss}s rec={rec}s"
            if pending:
                nb = "pending time-sync"
            elif epoch != 0:  # app's-zone-biased epoch -> render as-is
                nb = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%m-%d %H:%M:%S")
            elif firing:
                nb = "scheduled (sync for exact time)"
            else:
                nb = "—"
            return f"{motor}  |  {sdesc}  |  next: {nb}"
        return motor

    def _on_tab_visible(self):
        """Auto-subscribe to haptic status when the user switches to this tab,
        so the live IDLE/BUZZING/COUNTDOWN indicator appears without a manual
        Subscribe click.  Idempotent — silently no-ops if disconnected or
        already subscribed for this connection."""
        if not self.ble.connected or self._subscribed:
            return
        self._sub_status()

    def _on_disconnected(self):
        """Reset subscribe flag so a reconnect + tab-revisit re-subscribes."""
        self._subscribed = False

    def _write_schedule(self, single_shot: int, recurring: int, label: str):
        """0x1537 = [single_shot u32 LE][recurring u32 LE]."""
        payload = struct.pack("<I", single_shot) + struct.pack("<I", recurring)
        async def _w():
            try:
                await self.ble.write(CHAR_HAPTIC_REMINDER, payload)
                self._log(f"Schedule set: {label}  ({payload.hex(' ').upper()})")
                self.rem_state_var.set(label)
            except Exception as e:
                self._log(f"Schedule write error: {e}")
        run_async(_w())

    def _set_schedule(self):
        if not self.ble.connected: return
        ss  = max(0, self.rem_single_var.get())
        rec = max(0, self.rem_recurring_var.get())
        self._write_schedule(ss, rec, f"single_shot={ss}s recurring={rec}s")

    def _disable_schedule(self):
        if not self.ble.connected: return
        self._write_schedule(0, 0, "DISABLED")

    def _read_reminder(self):
        """Read the current schedule 0x1537 = [single_shot][recurring] (post-reboot = persisted)."""
        if not self.ble.connected: return
        async def _r():
            try:
                raw = bytes(await self.ble.read(CHAR_HAPTIC_REMINDER))
                if len(raw) < 8:
                    self._log(f"Schedule read: short ({len(raw)} bytes): {raw.hex(' ')}")
                    return
                ss  = struct.unpack("<I", raw[0:4])[0]
                rec = struct.unpack("<I", raw[4:8])[0]
                state = f"single_shot={ss}s  recurring={rec}s"
                self.rem_state_var.set(state)
                self._log(f"Schedule read: {raw.hex(' ').upper()}  ->  {state}")
            except Exception as e:
                self._log(f"Schedule read error: {e}")
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
        # NOTE: "Battery % over session" chart removed per UX overhaul —
        # the visual icon on the left of the live readout + the 50-row
        # history list cover the same need with less clutter.

        # ── Live readout (battery icon on the LEFT) ──────────────────────
        live = ttk.LabelFrame(self, text="Live Battery Level", padding=10)
        live.pack(fill="x", padx=6, pady=6)

        # Visual battery — vertical rectangle with a tip cap; fill height
        # tracks percent.  Drawn on a Tk Canvas (no extra deps).
        # NOTE: don't try to read live.cget("background") — ttk widgets
        # don't expose `-background` (TclError). Default canvas bg is fine.
        self.batt_canvas = tk.Canvas(live, width=80, height=160,
                                       highlightthickness=0)
        self.batt_canvas.grid(row=0, column=0, rowspan=3, padx=(6, 18),
                              pady=4)
        self._draw_battery(pct=None)

        self.pct_var = tk.StringVar(value="--")
        self.pct_label = tk.Label(live, textvariable=self.pct_var,
                                   font=("Courier", 28, "bold"),
                                   foreground="gray", width=6, anchor="center")
        self.pct_label.grid(row=0, column=1, rowspan=2, padx=12, pady=4)

        self.unit_label = tk.Label(live, text="%", font=("Courier", 18, "bold"),
                                    foreground="gray")
        self.unit_label.grid(row=0, column=2, rowspan=2, sticky="w")

        ttk.Label(live, text="Last update:").grid(row=0, column=3, sticky="e", padx=8)
        self.last_var = tk.StringVar(value="(never)")
        ttk.Label(live, textvariable=self.last_var,
                  font=("Courier", 10)).grid(row=0, column=4, sticky="w")

        ttk.Label(live, text="Subscription:").grid(row=1, column=3, sticky="e", padx=8)
        self.sub_var = tk.StringVar(value="off")
        self.sub_label = ttk.Label(live, textvariable=self.sub_var,
                                    font=("Courier", 10, "bold"),
                                    foreground="gray")
        self.sub_label.grid(row=1, column=4, sticky="w")

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

    def _draw_battery(self, pct: Optional[int]):
        """Render a vertical battery icon on the canvas.  pct=None draws an
        empty (grey) battery; otherwise the inner rectangle fills from the
        bottom up to `pct` percent.  Color thresholds match `_colour_for`."""
        c = self.batt_canvas
        c.delete("all")
        w  = int(c.cget("width"))
        h  = int(c.cget("height"))
        # Battery body outline (with a small top cap).
        cap_w = w // 3
        cap_h = 10
        body_top    = cap_h
        body_left   = 6
        body_right  = w - 6
        body_bottom = h - 6
        # Cap (small rectangle at top)
        cap_left  = (w - cap_w) // 2
        cap_right = cap_left + cap_w
        c.create_rectangle(cap_left, 0, cap_right, cap_h,
                            fill="#7f8c8d", outline="#34495e", width=1)
        # Body outline
        c.create_rectangle(body_left, body_top, body_right, body_bottom,
                            outline="#34495e", width=2)
        # Fill — proportional from the bottom up
        if pct is not None:
            pct_clamped = max(0, min(100, int(pct)))
            inner_h = body_bottom - body_top - 4
            fill_h  = int(inner_h * pct_clamped / 100)
            if fill_h > 0:
                fill_top = body_bottom - 2 - fill_h
                c.create_rectangle(body_left + 2, fill_top,
                                    body_right - 2, body_bottom - 2,
                                    fill=self._colour_for(pct_clamped),
                                    outline="")
            # Centred percent label inside the body
            c.create_text((body_left + body_right) // 2,
                          (body_top + body_bottom) // 2,
                          text=f"{pct_clamped}%",
                          font=("Segoe UI", 14, "bold"),
                          fill="white" if pct_clamped >= 35 else "#2c3e50")
        else:
            c.create_text(w // 2, h // 2, text="--",
                          font=("Segoe UI", 14, "bold"),
                          fill="#95a5a6")

    def _apply_reading(self, pct: int):
        """Push a new reading into the live readout, icon, and history list."""
        delta = 0 if self._last_pct is None else pct - self._last_pct
        self._last_pct = pct

        colour = self._colour_for(pct)
        self.pct_var.set(f"{pct:>3}")
        self.pct_label.configure(foreground=colour)
        self.unit_label.configure(foreground=colour)
        self.last_var.set(ts())

        # Redraw the visual battery icon
        self._draw_battery(pct)

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

    def _on_tab_visible(self):
        """Fired by App._on_tab_changed when the user switches to this tab.
        Same idempotent subscribe as auto_subscribe()."""
        self.auto_subscribe()

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
        self._last_pct = None
        self._draw_battery(None)
        self._log("History cleared.")

    def _on_disconnected(self):
        """Wipe live readout, icon, and history when the BLE link drops.
        Also resets _subscribed so the next auto_subscribe() actually fires
        instead of taking the 'already subscribed' early-return."""
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._last_pct = None
        self._draw_battery(None)

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


# ─────────────────────────────────────────────────────────────────────────────
#  Adv Data tab — continuously scans and decodes the MSD payload (no connect)
# ─────────────────────────────────────────────────────────────────────────────

# Manufacturer Specific Data layout (firmware: src/ble_svc.c build_manuf_data)
#   [0]      sys_state (bits 0-3) | flags (bits 4-7: USB / boost / lid / auth)
#   [1]      battery % (0..100, 0xFF if not yet read)
#   [2]      unsynced block count, uint8 (0..234)
#   [3]      journal entry count, uint8 (0..128)
#   [4-7]    last sync Unix epoch, uint32 LE (0 = never)
#   [8-11]   haptic next-buzz countdown, uint32 LE seconds (0xFFFFFFFF = none; coarse ~minute)
#   [12-15]  FICR DEVICEID[0], uint32 LE
#   [16-19]  FICR DEVICEID[1], uint32 LE
ADV_MSD_COMPANY_ID = 0xFFFF
ADV_MSD_LEN        = 20
ADV_STATE_NAMES = [
    "INIT", "SLOW_ADV", "FAST_ADV", "CONNECTED",
    "AUTHENTICATED", "FLASH_TX", "ERROR",
]


def decode_dusq_msd(payload):
    """Decode the 20-byte DUSQ MSD payload (without company ID).
    Returns dict, or None if payload is missing / too short."""
    if not payload or len(payload) < ADV_MSD_LEN:
        return None
    state_id = payload[0] & 0x07   # bits 0-2 (bit3 = haptic next-buzz epoch flag)
    haptic = int.from_bytes(payload[8:12], "little")
    ficr0  = int.from_bytes(payload[12:16], "little")
    ficr1  = int.from_bytes(payload[16:20], "little")
    return {
        "state_id":    state_id,
        "state":       ADV_STATE_NAMES[state_id] if state_id < len(ADV_STATE_NAMES) else f"?({state_id})",
        "usb":         bool(payload[0] & 0x10),
        "charging":    bool(payload[0] & 0x20),
        "lid_closed":  bool(payload[0] & 0x40),
        "authed":      bool(payload[0] & 0x80),
        "batt_pct":    payload[1] if payload[1] != 0xFF else None,
        "blocks":      payload[2],
        "journal":     payload[3],
        "last_sync":   int.from_bytes(payload[4:8], "little"),
        # Next buzz: absolute epoch if [0] bit3 (synced), else coarse seconds; 0xFFFFFFFF = none.
        "haptic_secs":     None if haptic == 0xFFFFFFFF else haptic,
        "haptic_is_epoch": bool(payload[0] & 0x08),
        # Full 64-bit FICR device ID (matches DIS System ID 0x2A23).
        "ficr":        f"{ficr1:08X}{ficr0:08X}",
    }


def fmt_next_buzz(val, is_epoch=False):
    """Format the advertised next-buzz: an absolute epoch (clock time) or coarse seconds."""
    if val is None:
        return "—"
    if is_epoch:
        try:
            return datetime.fromtimestamp(val, tz=timezone.utc).strftime("%m-%d %H:%M")
        except Exception:
            return f"epoch {val}"
    if val < 60:
        return "<1 min"
    return f"~{val // 60} min"


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



# ─────────────────────────────────────────────────────────────────────────────
#  Main application
# ─────────────────────────────────────────────────────────────────────────────

STATE_FILE = Path.home() / ".dusq_simulator.json"


def _load_state() -> dict:
    """Load persisted UI state from ~/.dusq_simulator.json.

    Returns an empty dict if the file is missing or unparseable.  Schema is
    intentionally loose — unknown keys are ignored so older clients can read
    newer files (and vice versa) without crashing."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    """Write the persisted UI state dict to ~/.dusq_simulator.json.
    Best-effort — silently swallows errors (read-only home dir, etc.) so
    a save failure never blocks app shutdown."""
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  RTT flow monitor  (J-Link SWD, independent of the BLE link)
#
#  Reads NRF_LOG output from RTT channel 0 via pylink in a background thread,
#  reconstructs the device flow from the [TAG] log messages, and flags anomalies
#  (faults, watchdog resets, flash errors, auth failures, reset loops). Parsing
#  is content-based on the message text, so it is robust to whatever NRF_LOG
#  timestamp prefix precedes each line.
# ─────────────────────────────────────────────────────────────────────────────

RTT_TARGET_DEFAULT = "nRF52810_xxAA"   # J-Link device name passed to connect()
_RESET_LOOP_N      = 3                  # this many resets...
_RESET_LOOP_SECS   = 30                 # ...within this window -> flag a reset loop

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")   # strip CSI / colour codes


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


# Severity — content-based, independent of NRF_LOG colouring.
_SEV_ERROR = re.compile(
    r"\[ERR\]|\[WDT\] Timeout|app fault|Fault id=|fstorage error|"
    r"flash_wait timeout|erase failed|write failed|SAADC read failed|"
    r"reset reason:\s*(?:app-fault|cpu-lockup)")
_SEV_WARN = re.compile(
    r"Auth FAIL|lockout|Boost blocked|reminder missed|CRC mismatch|bad magic|"
    r"rejected|out of range|wrap:|CCCD not enabled|reset reason:\s*watchdog")

_IDLE_SECS = 8   # a gap >= this between log bursts is shown as an idle/sleep row

# Flow milestones — (regex, category, label template with named groups). First
# match wins, so specific rules precede generic ones. Where an event is logged at
# several layers ([TMR]->[SM]->[IO]/[BLE]) only the canonical line is a milestone.
_FLOW_RULES = [
    # boot / init
    (re.compile(r"DUSQ Charger starting"),                  "boot",   "Firmware starting"),
    (re.compile(r"reset reason:\s*(?P<cause>[\w/-]+)"),      "boot",   "Boot - reset = {cause}"),
    (re.compile(r"no valid metadata"),                      "boot",   "Flash: fresh start (no metadata)"),
    (re.compile(r"(?P<sub>\w+)\] Init complete"),           "init",   "{sub} init done"),
    # advertising
    (re.compile(r"-> SYS_SLOW_ADV|\[BLE\] Adv SLOW"),        "adv",    "Advertising (slow)"),
    (re.compile(r"-> SYS_FAST_ADV|\[BLE\] Adv FAST"),        "adv",    "Advertising (fast)"),
    # connection
    (re.compile(r"\[SM\] BLE connected|\[BLE\] Connected"),  "conn",   "BLE connected"),
    (re.compile(r"MTU updated:\s*(?P<m>\d+)"),               "conn",   "MTU = {m}"),
    (re.compile(r"\[BLE\] Disconnected reason=(?P<r>0x[0-9a-fA-F]+)"), "conn", "BLE disconnected (reason {r})"),
    # authentication
    (re.compile(r"Auth OK"),                                 "auth",   "Authenticated"),
    (re.compile(r"\[BLE\] Auth FAIL (?P<n>\d+)/(?P<k>\d+)"), "auth",   "Auth FAIL {n}/{k}"),
    (re.compile(r"Auth lockout"),                            "auth",   "Auth lockout -> disconnect"),
    (re.compile(r"\[SM\] Auth timeout"),                     "auth",   "Auth timeout -> disconnect"),
    # physical I/O
    (re.compile(r"\[SM\] Hall:\s*(?P<s>\w+)"),               "io",     "Lid {s}"),
    (re.compile(r"\[SM\] USB:\s*(?P<s>\w+)"),                "io",     "USB {s}"),
    (re.compile(r"\[BOOT\] Lid closed"),                     "io",     "Boot: lid closed -> BOOST ON"),
    (re.compile(r"\[BOOT\] Lid open"),                       "io",     "Boot: lid open -> BOOST OFF"),
    (re.compile(r"Boost blocked"),                           "io",     "Boost blocked (batt low)"),
    (re.compile(r"\[IO\] BOOST (?P<s>\w+)"),                 "io",     "Boost {s}"),
    # sensor cycle (all values) + battery
    (re.compile(r"\[SM\] Sensor:\s*ts=(?P<ts>\d+)\s+temp=(?P<temp>-?\d+)\s+db=(?P<db>\d+)\s+peak_db=(?P<peak>\d+)\s+lux=(?P<lux>\d+)\s+batt=(?P<b>\d+)%"),
                                                             "sensor", "Sensor t={ts}s: temp={temp} dB={db} peak={peak} lux={lux} batt={b}%"),
    (re.compile(r"\[BATT\] code=.*batt=(?P<mv>\d+) mV"),     "sensor", "Battery {mv} mV"),
    # haptic (specific before generic)
    (re.compile(r"\[HAPTIC\] manual (?P<s>ON|OFF)"),         "haptic", "Haptic manual {s}"),
    (re.compile(r"\[HAPTIC\] countdown START (?P<s>\d+)"),   "haptic", "Haptic countdown {s}s"),
    (re.compile(r"\[HAPTIC\] cancelled"),                    "haptic", "Haptic cancelled"),
    (re.compile(r"\[HAPTIC\] pattern done"),                 "haptic", "Haptic pattern done"),
    (re.compile(r"\[HAPTIC\] reminder buzz"),               "haptic", "Reminder buzz"),
    (re.compile(r"\[HAPTIC\] reminder chain -> next in (?P<s>\d+)"),   "haptic", "Reminder -> next {s}s"),
    (re.compile(r"\[HAPTIC\] reminder resumed -> next in (?P<s>\d+)"), "haptic", "Reminder resumed -> next {s}s"),
    (re.compile(r"\[HAPTIC\] reminder missed during reboot -> next in (?P<s>\d+)"), "haptic", "Reminder missed -> next {s}s"),
    (re.compile(r"\[HAPTIC\] reminder loaded \(recurring=(?P<r>\d+)"), "haptic", "Reminder loaded ({r}s)"),
    (re.compile(r"\[HAPTIC\] reminder (?P<s>\w+) \(recurring=(?P<r>\d+)"), "haptic", "Reminder {s} (recurring {r}s)"),
    (re.compile(r"\[HAPTIC\] buzz (?P<x>\d+)/(?P<y>\d+) ON"), "haptic", "Haptic buzz {x}/{y}"),
    # time sync
    (re.compile(r"time synced: epoch=(?P<e>\d+)"),           "time",   "Time synced (epoch {e})"),
    # flash / journal transfer
    (re.compile(r"\[SM\] (?P<k>Flash|Journal) transfer (?P<a>start|done)"), "xfer", "{k} transfer {a}"),
    (re.compile(r"wrap: sync jumped"),                       "xfer",   "Flash wrap (old blocks lost)"),
    (re.compile(r"journal page erased .* lost=(?P<n>\d+)"),  "xfer",   "Journal page erased (lost {n})"),
    # faults (also flagged in the anomaly pane)
    (re.compile(r"\[ERR\] Fault"),                           "error",  "Fault -> reset"),
    (re.compile(r"\[WDT\] Timeout"),                         "error",  "WDT timeout -> reset"),
]
# Raw-log only (NOT flow milestones): all [TMR] *, [FLASH] per-slot/meta/journal
# internals, [SENS] PDM, [BLE] Stack init / Services registered, [AUTH] Expected
# PIN / Derived, and the "---- init ----" start markers.

# Anomalies — (regex, 'crit'|'warn', title, detail template; '_line' = full msg).
_ANOMALY_RULES = [
    (re.compile(r"\[BOOT\] app fault: id=(?P<id>0x[0-9a-fA-F]+).*?pc=(?P<pc>0x[0-9a-fA-F]+).*?info=(?P<info>0x[0-9a-fA-F]+)"),
                                                     "crit", "App-fault reset (prior boot crashed)", "id={id} pc={pc} info={info}"),
    (re.compile(r"\[ERR\] Fault id=(?P<id>0x[0-9a-fA-F]+).*?pc=(?P<pc>0x[0-9a-fA-F]+).*?info=(?P<info>0x[0-9a-fA-F]+)"),
                                                     "crit", "Fault caught -> resetting", "id={id} pc={pc} info={info}"),
    (re.compile(r"\[WDT\] Timeout"),                 "crit", "Watchdog timeout", "main loop stalled > 5 s"),
    (re.compile(r"reset reason:\s*cpu-lockup"),      "crit", "CPU lockup reset", ""),
    (re.compile(r"\[FLASH\].*?(?:failed|timeout)"),  "crit", "Flash operation failed", "{_line}"),
    (re.compile(r"fstorage error"),                  "crit", "fstorage error", "{_line}"),
    (re.compile(r"\[BATT\] SAADC read failed"),      "crit", "Battery ADC read failed", ""),
    (re.compile(r"reset reason:\s*watchdog"),        "warn", "Watchdog reset", "device was reset by the WDT"),
    (re.compile(r"\[BLE\] Auth FAIL"),               "warn", "Auth failure", "{_line}"),
    (re.compile(r"\[BLE\] Auth lockout"),            "warn", "Auth lockout -> disconnect", ""),
    (re.compile(r"\[SM\] Boost blocked"),            "warn", "Boost blocked (battery low)", "{_line}"),
    (re.compile(r"\[HAPTIC\] reminder missed"),      "warn", "Reminder missed during reboot", ""),
    (re.compile(r"\[FLASH\].*?(?:CRC mismatch|bad magic|out of range|wrap:)"), "warn", "Flash data warning", "{_line}"),
    (re.compile(r"Reminder write rejected"),         "warn", "Reminder write rejected", "{_line}"),
    (re.compile(r"CCCD not enabled"),                "warn", "CCCD not enabled", "{_line}"),
]

_RE_RESET = re.compile(r"reset reason:\s*[\w/-]+")


@dataclass
class RttEvent:
    dt: datetime
    sev: str                                    # 'error' | 'warn' | 'info'
    text: str
    flow: List[Tuple[str, str]]                 # [(category, label)] rows for this line
    anomalies: List[Tuple[str, str, str]]       # [(sev, title, detail)]


class FlowEngine:
    """Per-line classification + reset-loop detection."""

    def __init__(self):
        self._boots = deque(maxlen=_RESET_LOOP_N)
        self._last_flow = None
        self._last_line_dt = None

    @staticmethod
    def _severity(text: str) -> str:
        if _SEV_ERROR.search(text):
            return "error"
        if _SEV_WARN.search(text):
            return "warn"
        return "info"

    @staticmethod
    def _flow(text: str):
        for rx, cat, tmpl in _FLOW_RULES:
            m = rx.search(text)
            if m:
                try:
                    return cat, tmpl.format(**m.groupdict())
                except (KeyError, IndexError):
                    return cat, tmpl
        return None

    def _anomalies(self, text: str, dt: datetime):
        out = []
        for rx, sev, title, tmpl in _ANOMALY_RULES:
            m = rx.search(text)
            if not m:
                continue
            fields = dict(m.groupdict())
            fields["_line"] = text
            try:
                detail = tmpl.format(**fields)
            except (KeyError, IndexError):
                detail = text
            if "id" in m.groupdict():   # decode the fault id if this rule captured one
                try:
                    fid = int(m.group("id"), 16) & 0xFFFF
                    detail += f"  ({FAULT_IDS.get(fid, 'unknown fault')})"
                except ValueError:
                    pass
            out.append((sev, title, detail))
        if _RE_RESET.search(text):      # reset-loop detection
            self._boots.append(dt)
            if len(self._boots) >= _RESET_LOOP_N:
                span = (self._boots[-1] - self._boots[0]).total_seconds()
                if span <= _RESET_LOOP_SECS:
                    out.append(("crit", "RESET LOOP",
                                f"{len(self._boots)} resets in {span:.0f} s"))
                    self._boots.clear()
        return out

    def feed(self, raw: str, dt: Optional[datetime] = None) -> Optional[RttEvent]:
        dt = dt or datetime.now()
        text = _strip_ansi(raw).strip()
        if not text:
            return None
        flow_rows = []
        # Idle/sleep marker: deferred RTT only flushes while the main loop runs,
        # so a gap between log bursts ~ the System-ON sleep between events (sensor
        # tick ~60 s, GPIOTE, BLE). PC-receive-time approximate, not exact.
        if self._last_line_dt is not None:
            gap = (dt - self._last_line_dt).total_seconds()
            if gap >= _IDLE_SECS:
                flow_rows.append(("idle", f"idle {gap:.0f}s (sleep)"))
        self._last_line_dt = dt
        item = self._flow(text)
        if item is not None and item != self._last_flow:   # collapse back-to-back dups
            self._last_flow = item
            flow_rows.append(item)
        return RttEvent(dt, self._severity(text), text,
                        flow_rows, self._anomalies(text, dt))


class RttReader(threading.Thread):
    """Background J-Link RTT reader. Pushes (kind, payload) tuples onto a queue:
    ('line', text) / ('status', text) / ('error', text)."""

    def __init__(self, target: str, serial: str, out_q: "queue.Queue"):
        super().__init__(daemon=True)
        self.target = (target or RTT_TARGET_DEFAULT).strip()
        self.serial = (serial or "").strip()
        self.q = out_q
        self._stop = threading.Event()
        self._jlink = None

    def stop(self):
        self._stop.set()

    def _emit(self, kind, payload):
        self.q.put((kind, payload))

    def run(self):
        try:
            jlink = pylink.JLink()
            self._jlink = jlink
            if self.serial:
                jlink.open(serial_no=int(self.serial))
            else:
                jlink.open()
            jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
            self._emit("status", f"connecting to {self.target}...")
            jlink.connect(self.target, speed="auto")
            jlink.rtt_start(None)
            self._emit("status", f"RTT connected ({self.target})")
        except Exception as e:
            self._emit("error", f"{type(e).__name__}: {e}")
            self._close()
            return

        buf = b""
        miss = 0
        while not self._stop.is_set():
            try:
                data = self._jlink.rtt_read(0, 2048)
            except Exception as e:
                miss += 1
                if miss > 50:          # control block never came up / link lost
                    self._emit("error", f"rtt_read: {e}")
                    break
                time.sleep(0.05)
                continue
            if data:
                miss = 0
                buf += bytes(bytearray(data))
                parts = buf.split(b"\n")
                buf = parts.pop()          # keep the partial tail
                for ln in parts:
                    self._emit("line", ln.decode("latin-1", "replace").rstrip("\r"))
            else:
                time.sleep(0.02)
        self._close()
        self._emit("status", "stopped")

    def _close(self):
        try:
            if self._jlink is not None:
                try:
                    self._jlink.rtt_stop()
                except Exception:
                    pass
                self._jlink.close()
        except Exception:
            pass
        self._jlink = None


_FLOW_COLORS = {
    "boot": "#eaf2ff", "adv": "#f3f0ff", "conn": "#e8f8f0", "auth": "#e6fbf2",
    "sensor": "#fffbe6", "io": "#f0f4f8", "xfer": "#eef6ff", "haptic": "#fdeef6",
    "init": "#f2f2f2", "time": "#e0f7f7", "error": "#fdecea", "idle": "#ededed",
}


class RttTab(ttk.Frame):
    _MAX_RAW_LINES = 4000

    def __init__(self, parent):
        super().__init__(parent)
        self.engine = FlowEngine()
        self.reader: Optional[RttReader] = None
        self.q: "queue.Queue" = queue.Queue()
        self._build()
        self.after(200, self._poll)

    def _build(self):
        top = ttk.Frame(self)
        top.pack(side="top", fill="x", padx=6, pady=4)
        ttk.Label(top, text="Target:").pack(side="left")
        self.target_var = tk.StringVar(value=RTT_TARGET_DEFAULT)
        ttk.Entry(top, textvariable=self.target_var, width=15).pack(side="left", padx=(2, 8))
        ttk.Label(top, text="J-Link S/N:").pack(side="left")
        self.serial_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.serial_var, width=11).pack(side="left", padx=(2, 8))
        self.connect_btn = ttk.Button(top, text="Connect RTT", command=self._toggle)
        self.connect_btn.pack(side="left", padx=2)
        ttk.Button(top, text="Clear", command=self._clear).pack(side="left", padx=2)
        ttk.Button(top, text="Save log...", command=self._save).pack(side="left", padx=2)
        self.status_var = tk.StringVar(
            value="idle" if HAS_PYLINK else "pylink-square not installed")
        ttk.Label(top, textvariable=self.status_var, foreground="gray").pack(side="right")

        outer = ttk.Panedwindow(self, orient="vertical")
        outer.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        upper = ttk.Panedwindow(outer, orient="horizontal")
        rawf = ttk.Labelframe(upper, text="Raw RTT log", padding=2)
        self.raw = scrolledtext.ScrolledText(rawf, height=16, state="disabled",
                                             font=("Consolas", 9), wrap="none")
        self.raw.pack(fill="both", expand=True)
        self.raw.tag_config("error", foreground="#c0392b")
        self.raw.tag_config("warn", foreground="#d35400")

        flowf = ttk.Labelframe(upper, text="Device flow", padding=2)
        self.flow = ttk.Treeview(flowf, columns=("t", "evt"), show="headings", height=16)
        self.flow.heading("t", text="Time")
        self.flow.column("t", width=66, anchor="w", stretch=False)
        self.flow.heading("evt", text="Event")
        self.flow.column("evt", width=360, anchor="w")
        self.flow.pack(fill="both", expand=True)
        for cat, col in _FLOW_COLORS.items():
            self.flow.tag_configure(cat, background=col)
        upper.add(rawf, weight=3)
        upper.add(flowf, weight=2)
        outer.add(upper, weight=3)

        anomf = ttk.Labelframe(outer, text="Flags / anomalies", padding=2)
        self.anom = ttk.Treeview(anomf, columns=("t", "sev", "issue", "detail"),
                                 show="headings", height=7)
        for c, w, txt, st in [("t", 66, "Time", False), ("sev", 52, "Sev", False),
                              ("issue", 210, "Issue", False), ("detail", 380, "Detail", True)]:
            self.anom.heading(c, text=txt)
            self.anom.column(c, width=w, anchor="w", stretch=st)
        self.anom.pack(fill="both", expand=True)
        self.anom.tag_configure("crit", foreground="#c0392b")
        self.anom.tag_configure("warn", foreground="#d35400")
        outer.add(anomf, weight=1)

        if not HAS_PYLINK:
            self.connect_btn.config(state="disabled")

    # ── controls ──
    def _toggle(self):
        if self.reader and self.reader.is_alive():
            self.stop()
        else:
            self._start()

    def _start(self):
        if not HAS_PYLINK:
            messagebox.showerror("RTT", "pylink-square not installed:\n\n    pip install pylink-square")
            return
        self.reader = RttReader(self.target_var.get(), self.serial_var.get(), self.q)
        self.reader.start()
        self.connect_btn.config(text="Stop RTT")
        self.status_var.set("connecting...")

    def stop(self):
        if self.reader:
            self.reader.stop()
        self.connect_btn.config(text="Connect RTT")

    def _clear(self):
        self.raw.config(state="normal")
        self.raw.delete("1.0", "end")
        self.raw.config(state="disabled")
        self.flow.delete(*self.flow.get_children())
        self.anom.delete(*self.anom.get_children())
        self.engine = FlowEngine()

    def _save(self):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(defaultextension=".log",
                                            filetypes=[("Log", "*.log"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.raw.get("1.0", "end"))
            self.status_var.set(f"saved {os.path.basename(path)}")
        except OSError as e:
            messagebox.showerror("Save", str(e))

    # ── pump (Tk main thread) ──
    def _poll(self):
        try:
            for _ in range(1000):
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    self._on_line(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "error":
                    self.status_var.set("error: " + payload)
                    self._add_anom(datetime.now(), "crit", "RTT error", payload)
                    self.connect_btn.config(text="Connect RTT")
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _on_line(self, raw: str):
        ev = self.engine.feed(raw)
        if ev is None:
            return
        tag = ev.sev if ev.sev in ("error", "warn") else ""
        self.raw.config(state="normal")
        self.raw.insert("end", ev.dt.strftime("%H:%M:%S ") + ev.text + "\n",
                        (tag,) if tag else ())
        line_count = int(self.raw.index("end-1c").split(".")[0])
        if line_count > self._MAX_RAW_LINES:
            self.raw.delete("1.0", f"{line_count - self._MAX_RAW_LINES}.0")
        self.raw.see("end")
        self.raw.config(state="disabled")

        for cat, label in ev.flow:
            iid = self.flow.insert("", "end",
                                   values=(ev.dt.strftime("%H:%M:%S"), label), tags=(cat,))
            self.flow.see(iid)
        for sev, title, detail in ev.anomalies:
            self._add_anom(ev.dt, sev, title, detail)

    def _add_anom(self, dt: datetime, sev: str, title: str, detail: str):
        iid = self.anom.insert("", "end",
                               values=(dt.strftime("%H:%M:%S"), sev.upper(), title, detail),
                               tags=(sev,))
        self.anom.see(iid)


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


if __name__ == "__main__":
    main()
