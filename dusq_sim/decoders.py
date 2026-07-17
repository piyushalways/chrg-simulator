"""Pure decoders for BLE payloads: auth PIN, sensor char, flash blocks, journal
entries, status, and the advertising MSD.
"""

import struct
import zlib
from datetime import datetime, timezone
from typing import Optional

from .constants import *
from .util import fmt_epoch_local

def compute_auth_pin(serial_bytes: bytes) -> bytes:
    """Compute the 3-byte Auth PIN for a device given its 16-byte DIS Serial Number."""
    full = zlib.crc32(serial_bytes) & 0xFFFFFFFF
    return (full & 0xFFFFFF).to_bytes(3, "big")


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
    # Firmware stores the sync_status flag in reserved[0] (offset 20): 0 = pre-sync
    # (timestamp is boot_seconds), 1 = post-sync (timestamp is a Unix epoch), 0xFF = legacy.
    sync_status = raw[20]

    return {
        "sequence":     seq,
        "magic_ok":     True,
        "timestamp":    timestamp,
        "sync_status":  sync_status,
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


def journal_timestamp_str(entry: dict) -> str:
    """Human-readable timestamp for a journal entry.
    Uses the entry's sync_status byte (firmware reserved[0]): post-sync -> the
    stored value is an epoch, rendered as a local wall-clock string; pre-sync ->
    the value is boot_seconds, rendered as "boot+Ns". Unknown/legacy (0xFF) falls
    back to a magnitude heuristic so a real epoch never prints as a bogus 1970 date."""
    ts_val = entry.get("timestamp", 0)
    ss = entry.get("sync_status", 0xFF)
    if ss == 1:                       # SYNC_STATUS_POST_SYNC -> epoch
        return fmt_epoch_local(ts_val)
    if ss == 0:                       # SYNC_STATUS_PRE_SYNC  -> boot_seconds
        return f"boot+{ts_val}s"
    # 0xFF / legacy: a plausible Unix epoch (>= 2001) is post-sync, else boot_seconds.
    if ts_val >= 1_000_000_000:
        return fmt_epoch_local(ts_val)
    return f"boot+{ts_val}s" if ts_val else "-"


__all__ = [
    "compute_auth_pin", "_decode_sensor_payload", "_crc32_of",
    "decode_block", "decode_journal_entry", "format_event_data", "decode_status",
    "decode_dusq_msd", "fmt_next_buzz", "journal_timestamp_str",
]
