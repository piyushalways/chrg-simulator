"""RTT flow monitor engine (J-Link SWD): line classification, flow milestones,
anomaly detection, reset-loop tracking, and the background reader thread.
"""

import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

try:
    import pylink   # J-Link RTT reader (pip install pylink-square)
    HAS_PYLINK = True
except ImportError:
    HAS_PYLINK = False
    pylink = None

from .constants import *

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


__all__ = [
    "RttEvent", "FlowEngine", "RttReader", "RTT_TARGET_DEFAULT", "HAS_PYLINK",
]
