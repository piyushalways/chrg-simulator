"""Shared helpers: async pump, timestamps, hex dump, UI-state persistence, matplotlib.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    HAS_MPL = True
except ImportError:
    HAS_MPL = False
    Figure = None
    FigureCanvasTkAgg = None

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

def run_async(coro):
    loop = asyncio.get_event_loop()
    return loop.create_task(coro)


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


def hex_dump(data: bytes, width: int = 16) -> str:
    """Offset-prefixed hex dump (moved verbatim from FlashTab._hex_dump)."""
    out = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        out.append(f"  {i:04x}  {hexs:<{width*3}}")
    return "\n".join(out)


def fmt_epoch_local(epoch: int) -> str:
    """Render a device epoch as its wall-clock string. Device epochs are IST-biased
    (see constants.IST_OFFSET_S), so rendering with tz=utc prints the intended local
    (IST) wall clock, e.g. "2026-07-17 21:00:00". Falls back to the raw int if out of range."""
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return str(epoch)


__all__ = [
    "ts", "run_async", "hex_dump", "fmt_epoch_local",
    "STATE_FILE", "_load_state", "_save_state",
    "HAS_MPL", "Figure", "FigureCanvasTkAgg",
]
