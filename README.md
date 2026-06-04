# DUSQ Charger BLE Test Tool

Host-side BLE client for exercising the DUSQ Charger firmware
(`nRF52810 + S112 SoftDevice + nRF5 SDK 17.x`). Scans for the device,
connects, authenticates, streams live sensor data, downloads logged flash
blocks, drives the haptic motor, and runs an automated validation suite.

It uses [bleak](https://github.com/hbldh/bleak) for cross-platform BLE and
[tkinter](https://docs.python.org/3/library/tkinter.html) for the GUI.

## What's new in this release

- **Validation tab is now "Automated Testing"** — three-region layout:
  domain checkboxes at top, big RUN button, prominent green/red final
  result panel. Hover any domain checkbox for a one-line summary of what it
  tests. The legacy per-test tree view is still available below for
  fine-grained control. Pre-run dialog shows the estimated time and asks
  for confirmation before kicking off.
- **Two new testing domains**:
  - **Hall** (lid sensor) — I1 verifies lid-close fires the hall event and
    enables boost; I2 verifies the symmetric lid-open path. Interactive.
  - **USB detect** — U1 verifies Status.usb flips True on plug-in; U2
    verifies it flips False on unplug. Interactive.
- **Live BLE round-trip latency** in the header next to the connection
  indicator (green < 100 ms, amber < 300 ms, red ≥ 300 ms; sampled every
  5 s).
- **Time-sync button on the Auth tab** — mirror of the existing one in the
  Flash tab. Sync UTC epoch without navigating tabs after authenticating.
- **Index counters on Flash + Journal downloads**: per-row running index
  column, plus a tri-counter label showing "On device", "This download",
  and "This connection" counts.
- **CSV export** alongside the existing JSON validation export.
- **Persistent UI state** — window geometry, last-connected device MAC, and
  device name persist across launches in `~/.dusq_simulator.json`.

---

## Index

| # | Topic                                                                 |
| - | --------------------------------------------------------------------- |
| 1 | [Requirements](#1-requirements)                                       |
| 2 | [Installation](#2-installation)                                       |
| 3 | [Running the tool](#3-running-the-tool)                               |
| 4 | [Tab-by-tab guide](#4-tab-by-tab-guide)                               |
|   | [4.1 Connect tab](#41-connect-tab)                                    |
|   | [4.2 Auth tab](#42-auth-tab)                                          |
|   | [4.3 Sensors tab](#43-sensors-tab)                                    |
|   | [4.4 Flash tab](#44-flash-tab)                                        |
|   | [4.5 Haptic tab](#45-haptic-tab)                                      |
|   | [4.6 Battery tab](#46-battery-tab)                                    |
|   | [4.7 Automated Testing tab](#47-automated-testing-tab)                |
|   | [4.8 Log tab](#48-log-tab)                                            |
| 5 | [What gets tested](#5-what-gets-tested)                               |
| 6 | [BLE protocol reference](#6-ble-protocol-reference)                   |
| 7 | [Typical test session walkthrough](#7-typical-test-session-walkthrough) |
| 8 | [Troubleshooting](#8-troubleshooting)                                 |
| 9 | [Known limitations](#9-known-limitations)                             |
| 10 | [Roadmap](#10-roadmap)                                               |

---

## 1. Requirements

- **Python**: 3.9 or newer (3.11+ recommended).
- **OS**: Windows 10/11, macOS 11+, or Linux with BlueZ ≥ 5.50.
- **BLE adapter**: any built-in or USB Bluetooth 4.0+ adapter.
- **Firmware**: DUSQ Charger built from this repo, flashed and powered on.
  The device must be advertising — confirm with nRF Connect for Mobile if
  unsure.

Python packages:

| Package      | Required | Why                                                |
| ------------ | -------- | -------------------------------------------------- |
| `bleak`      | yes      | Cross-platform BLE client.                         |
| `matplotlib` | optional | Live charts in the Sensors tab. Falls back to a    |
|              |          | text reading log if not installed.                 |
| `tkinter`    | yes      | Bundled with the standard Python installer on      |
|              |          | Windows/macOS. On Linux: `sudo apt install         |
|              |          | python3-tk`.                                       |

[↑ Back to index](#index)

---

## 2. Installation

```bash
# 1. (Recommended) create a virtual env
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 2. Install dependencies
pip install bleak matplotlib
```

If you skip `matplotlib`, the Sensors tab still works — it just shows a
scrolling text log instead of charts.

[↑ Back to index](#index)

---

## 3. Running the tool

From the repo root:

```bash
python Simulator/ble_test_tool.py
```

An **eight-tab** Tk window opens, in this order:

```
Connect | Auth | Sensors | Flash | Haptic | Battery | Validation | Log
```

Two persistent UI elements always visible at the top of the window:

- **Header bar** with the app title on the left and a colour-coded
  **`● Connected: NAME`** / **`● Disconnected`** indicator on the right.
- **`↻ Reconnect <NAME>`** button next to the indicator — appears when
  disconnected and the tool remembers the last MAC you connected to,
  so you can dial it directly without scanning again.

A bottom status bar mirrors the full connection text.

[↑ Back to index](#index)

---

## 4. Tab-by-tab guide

### 4.1 Connect tab

Discover and connect to the device.

| Control                | What it does                                                            |
| ---------------------- | ----------------------------------------------------------------------- |
| **Scan** button        | Streaming BLE discovery for `Timeout` seconds. Each advertisement is   |
|                        | logged in real-time to the Log tab as it arrives. By default the list  |
|                        | shows ONLY targets — devices whose name contains `DUSQ_CHARGER` or    |
|                        | advertise the Auth Service UUID `0x1523`. They're highlighted green   |
|                        | (`#8FA97A`) and tagged `[TARGET]`.                                     |
| **Timeout** spin       | How long to scan, in seconds. Default **12** (slow adv = 2 s, so      |
|                        | this catches ≥ 6 packets). Range 3–60 s.                                |
| **Show all (debug)**   | Tick to display *every* advertiser in range, not just targets.        |
|                        | Re-renders from cache without re-scanning.                             |
| **Total seen / shown** | Live counter next to the controls: *"Total seen: 12 · targets         |
|                        | shown: 1"*. Confirms whether the radio is healthy even when your      |
|                        | device isn't visible.                                                  |
| **Connect**            | Select a row, then click. Negotiates MTU + service discovery. After   |
|                        | a successful connect, the address is remembered for the header's     |
|                        | Reconnect button. Shortcut: **double-click** any row to connect.      |
| **Disconnect**         | Tears down the GATT link.                                             |
| **Log pane**           | Timestamped trace of scan / connect activity (in addition to the      |
|                        | global Log tab).                                                       |

**Tip:** if the device list is empty after a scan but `Total seen > 0`,
the radio is fine — your device just isn't advertising the right name
or UUID. Tick **Show all (debug)** to see every device by MAC. If
`Total seen = 0`, see [Troubleshooting](#8-troubleshooting).

[↑ Back to index](#index)

### 4.2 Auth tab

Send the 3-byte PIN to unlock authenticated characteristics.

- Default PIN field is `00 00 00` — matches `AUTH_PIN` in firmware
  [src/config/app_config.h](../src/config/app_config.h).
- Click **Authenticate** → the tool writes the PIN bytes to `0x1524`,
  waits ~700 ms, then **reads the Status char (`0x1529`)** and inspects
  the device's actual `state` field.
- The status label colours by the device's state-machine truth:
  - **Green: AUTHENTICATED** — `state == 4`.
  - **Red: REJECTED — wrong PIN** — `state == CONNECTED` (PIN didn't match).
  - **Orange: unexpected state = …** — anything else (rare).

The tool also fires `on_auth_attempt` after the PIN write succeeds, which
auto-subscribes the Battery tab so BAS notifications start flowing
immediately after auth.

> **Why a Status read instead of a notify?** The firmware does not emit
> a notify on the auth char — `m_authenticated` is flipped silently
> ([src/ble_svc.c:381-389](../src/ble_svc.c#L381-L389)). Reading the
> Status char is the only honest way to confirm auth from the host.

[↑ Back to index](#index)

### 4.3 Sensors tab

Live readout of every periodic measurement the firmware streams.

All four sensor values come from a single merged characteristic
`0x1526` (5 bytes, little-endian):

| Offset | Bytes | Field   | Units / encoding         |
| ------ | ----- | ------- | ------------------------ |
| 0      | 1     | temp    | int8 °C; `127` = error   |
| 1      | 1     | db_avg  | uint8 dB; `255` = error  |
| 2      | 1     | db_peak | uint8 dB; `255` = error  |
| 3..4   | 2     | lux     | uint16 LE; `0xFFFF` = err|

The other two fields come from separate characteristics:

| Field           | Characteristic                     | Units / encoding         |
| --------------- | ---------------------------------- | ------------------------ |
| Battery %       | Standard BAS (`0x180F` / `0x2A19`) | uint8 0–100              |
| System Status   | Status char `0x1529` (10 B packed) | state, USB, Boost, Lid…  |

Buttons:

- **Subscribe to Notifications** — turn on continuous streaming. Sensor
  readings come every 60 s (per `SENSOR_PERIOD_MS` in firmware).
- **Read Once (all)** — manual one-shot read of every characteristic.

> Time Sync moved to the Flash tab — it controls the timestamps written
> into flash blocks, so it lives where you'd download those blocks from.

If `matplotlib` is installed, the bottom panel shows a rolling 60-reading
chart with three side-by-side panes: temperature, dB (avg + peak), and lux.

[↑ Back to index](#index)

### 4.4 Flash tab

The Flash tab is split into **two sub-pages** via an inner notebook:
**Sensor Blocks** (the 30-minute aggregated readings) and **Journal**
(boot / time-sync / wrap / error events).

#### Sensor Blocks page

| Control             | Behaviour                                                  |
| ------------------- | ---------------------------------------------------------- |
| **Get Block Count** | Reads `0x152B` → number of unsynced blocks on the device.  |
| **Download Blocks** | Subscribes to `0x152D` (notify) **then** writes `0x01` to  |
|                     | `0x152D` (the same UUID — write triggers, notify carries   |
|                     | the data). Transfer ends after 5 s of notify silence.      |
| **Cancel**          | Aborts an in-flight download. Cleanly unsubscribes.        |
| **Send Time Sync**  | Writes the host's Unix epoch (UTC) to `0x152E`. Blocks     |
|                     | logged after this carry real wall-clock timestamps.        |
| **Clear List**      | Drops the local copy (does not erase the device).          |

Each row shows seq, timestamp, sync state (pre/post), reading count, and
CRC OK/FAIL. Bad-CRC rows are highlighted pink. Click any row to see in
the detail pane:

- Decoded **header** (magic, sequence, timestamp, sync_status,
  reading_count, stored vs computed CRC32).
- **Payload** — all 30 reading slots with temperature / avg dB / lux
  values, `ERR` for sensor error markers.
- **Raw 152-byte hex dump** for forensic debugging.

Block layout (matches firmware `src/flash.h`):

```
[0..3]    magic 0xDEADBEEF
[4..7]    sequence number
[8..14]   timestamp <HBBBBB>: year, month, day, hour, minute, second
[15..16]  sync_status (uint16, 0 = pre-sync, 1 = post-sync)
[17..18]  reading_count (uint16)
[19..27]  reserved (9 B)
[28..31]  CRC32 over header bytes [0..27] only (payload NOT covered)
[32..151] 30 readings × 4 B: int8 temp, uint8 db, uint16 LE lux
```

> ⚠ The block CRC only covers the 28-byte header. Payload corruption is
> not currently detectable on the host — see the firmware integrity
> roadmap for an extension to include the payload in a checksum.

#### Journal page

| Control                | Behaviour                                                |
| ---------------------- | -------------------------------------------------------- |
| **Get Journal Count**  | Reads `0x1534` → unsynced journal entry count.           |
| **Download Journal**   | Subscribes to `0x1535` (notify-only) **then** writes     |
|                        | `0x01` to `0x1536` (separate trigger UUID for journal).  |
| **Cancel**             | Aborts an in-flight journal download.                    |
| **Clear List**         | Drops the local journal copy.                            |

Journal entries are 32 bytes each. Decoded events:

| Type | Name         | `data` field meaning                       |
| ---- | ------------ | ------------------------------------------ |
| 0    | BOOT         | `boot_count` at boot time                  |
| 1    | TIME_SYNC    | `epoch` value just received from host      |
| 2    | FLASH_WRAP   | block index that triggered the wrap        |
| 3    | ERROR        | error code (treat as `0x%08X`)             |
| 4    | LOW_BATTERY  | battery percent at time of event           |

The detail pane shows magic, sequence, raw boot_seconds timestamp,
event type name, decoded data interpretation (`TIME_SYNC` decodes the
epoch into a UTC date), and the raw 32-byte hex dump.

Use the journal to answer questions like *"did the device reboot in the
last 24 h?"* or *"when did the flash wrap and how many blocks were
lost?"* — every reset and every wrap is logged as one entry.

[↑ Back to index](#index)

### 4.5 Haptic tab

Manual control of the vibration motor (firmware service `0x152F`).

| Control          | Effect on firmware                                              |
| ---------------- | --------------------------------------------------------------- |
| **Motor ON/OFF** | Writes `0x01` / `0x00` to `0x1530`. Requires authenticated.     |
| **Set Intensity**| Writes 0–100 % duty to `0x1531`. Persists for next ON.          |
| **Start Timer**  | Writes a uint32 LE seconds value to `0x1532`. Device buzzes     |
|                  | when the countdown reaches zero.                                |
| **Cancel Timer** | Writes `0` to `0x1532`.                                         |
| **Subscribe Status** | Listens on `0x1533` for `0=IDLE / 1=BUZZING / 2=COUNTDOWN`. |

[↑ Back to index](#index)

### 4.6 Battery tab

Dedicated focus view for the SIG Battery Service (`0x180F` / `0x2A19`).
Useful when you want to leave a charge / drain test running without the
distraction of sensor charts.

| Control               | Behaviour                                                |
| --------------------- | -------------------------------------------------------- |
| **Read Once**         | One-shot read of `0x2A19`. Updates the live readout and  |
|                       | adds a row to the history list.                          |
| **Subscribe**         | Turns on BAS notifications. Firmware emits one update    |
|                       | per 60 s sensor cycle plus a one-off on auth-OK.         |
| **Unsubscribe**       | Turns notifications off without disconnecting.           |
| **Clear History**     | Empties the history list and the previous-percent state. |

Threshold colours on the big readout:

| Range       | Colour  |
| ----------- | ------- |
| ≥ 80 %      | green   |
| 50 – 79 %   | blue    |
| 20 – 49 %   | orange  |
| < 20 %      | red     |

Layout of the tab, top to bottom:

1. **Live readout** — big colour-coded percent, last-update timestamp,
   subscription state.
2. **Action buttons** — Read Once / Subscribe / Unsubscribe / Clear History.
3. **Chart** — battery % over the current session (capped at the last 50
   readings). Background bands match the threshold colours so you can see
   at a glance which band the device is in. Requires `matplotlib`.
4. **History list** — same 50 readings as a Treeview with `time | % | Δ`,
   newest first.
5. **Log pane** — timestamped events (subscribe / unsubscribe / errors).

The Δ column shows the signed change vs the previous reading, so a flip
from charging to draining (or vice versa) is instantly visible.

> **Note:** firmware only emits BAS notifications when authenticated. The
> tool **auto-subscribes** to BAS the moment the Auth tab's PIN write
> succeeds, so most users never need to click **Subscribe** here manually.
> The button is still available for the rare case where you unsubscribed
> mid-session and want it back.

[↑ Back to index](#index)

### 4.7 Automated Testing tab

Comprehensive automated test suite — **53 tests across 10 domains**, all
runnable individually, by domain, or as a single bulk pass.

**Domain layout** (top of tab — checkboxes for what to test):

| Domain | Tests | Notes |
|---|---|---|
| BLE          | A1–A6     | scan, connect, MTU, services, disconnect/reconnect cleanup |
| Auth         | B1        | PIN write + Status-char verify |
| Sensors      | C1, C2, C5, C7, C8 | cadence, temp, dB, lux, status state |
| Battery      | D1–D4     | level, cadence, parity, stable during charge |
| Haptic       | E1–E5     | **interactive** — confirms buzz visually |
| Flash        | F1–F12    | block count, CRC, sequence, wrap, time-sync effect |
| Journal      | G1–G7     | count, CRC, monotonic sequence, event types |
| Hall         | I1, I2    | **interactive** — open/close lid magnet |
| USB detect   | U1, U2    | **interactive** — plug/unplug USB cable |
| Long-running | H1, H2    | ≥ 4 h; opt-in only |

The big RUN button below the checkboxes runs whatever is selected.
"Headless only" pre-selects the non-interactive domains for batch runs.
Before the run starts, a confirmation modal shows the test count and an
estimated time so you can abort large jobs.

After the run, the **Latest result** panel turns green ("✓ ALL TESTS
PASSED") or red (lists failures inline with one-line summaries).

**Layout**

```
┌──────────────────────────────────────────────────────────────────┐
│ [Run All] [Run Selected] [Run This Bucket] [Cancel] [Export]    │
│ [Reset]   [☑ Include long-running (Bucket H, ≥4 h)]             │
├──────────────────────────────────────────────────────────────────┤
│ Tree of tests (bucket headers collapsible):                     │
│   ▼ A — BLE Link                                                │
│      [✓] A1 scan_finds_target                  PASS   0.2 s     │
│      [✓] A2 connect_completes                  PASS   1.4 s     │
│      …                                                          │
│      [✓] A6 disconnect_reconnect_clean (runs last)  PASS  3.1 s │
│   ▼ E — Haptic                                                  │
│      [✓] E1 manual_on_status                   PASS   2.5 s     │
│      [✓] E5 countdown_runs                     FAIL   8.0 s     │
│   ▼ F — Flash sensor blocks + circular buffer                   │
│      [✓] F5 ring_pointer_advance               PASS   0.6 s     │
│      …                                                          │
├──────────────────────────────────────────────────────────────────┤
│ Details pane (selected test's evidence — streams during run)    │
└──────────────────────────────────────────────────────────────────┘
```

**A6 runs last** — even though it lives under Bucket A's header in the
tree, A6 (`disconnect_reconnect_clean`) executes after every other test
during *Run All*. It would otherwise force the rest of the suite to
re-authenticate.

**Buckets** (click any row to view full details):

| Bucket | Coverage                                                        | Wall time | # tests |
| ------ | --------------------------------------------------------------- | --------- | ------- |
| A      | BLE link (scan, connect, MTU, services). A6 runs **last**.      | ~30 s     | 6       |
| B      | Authentication (valid PIN write + Status-char verification)    | ~5 s      | 1       |
| C      | Sensor pipeline (cadence, ranges, mic invariant, lux, status)  | ~5 min    | 7       |
| D      | Battery (range, cadence, status sync, USB stability)            | ~5 min    | 4       |
| E      | Haptic (manual ON/OFF, intensity, countdown) — **user Pass/Fail** | ~30 s   | 5       |
| F      | Flash sensor blocks + circular buffer                           | ~7 min    | 12      |
| G      | Journal (count, integrity, types, time-sync logging)            | ~2 min    | 7       |
| H      | Long-running (opt-in, ≥ 4 h)                                    | hours     | 2       |

**Buttons**

- **Run All** — runs every test except optional Bucket H (toggle the
  checkbox to include it).
- **Run Selected** — runs only the tests you've ticked `[✓]`. Click a
  row to toggle. Click a bucket header to toggle the whole bucket.
- **Run This Bucket** — runs every test in the bucket whose row (or
  header) is currently selected.
- **Cancel** — stops the in-flight run cleanly.
- **Export…** — writes `dusq_validation_<timestamp>.json` containing
  every result + collected samples + downloaded blocks/journal in hex.
- **Reset** — clears all results back to PENDING (doesn't disconnect).

**Live evidence streaming** — the runner auto-selects the currently
running row and streams every meaningful step into the **Details** pane
in real time. You'll see lines like:

```
collecting 5 sensor sample(s)…
  sample 1/5: t=24°C  avg=42dB  peak=58dB  lux=120
  BAS notify: 73 % (arrival #1)
  sample 2/5: …
downloading sensor blocks…
  block 7: seq=42 rc=30 CRC=OK
journal download complete — 12 entry(ies)
```

So you don't have to stare at the tree wondering whether anything is
happening during a multi-minute test.

**Status colours**

| Status        | Colour  | Meaning                                                       |
| ------------- | ------- | ------------------------------------------------------------- |
| PASS          | green   | Test verified the invariant.                                  |
| FAIL          | pink bg | Test detected a regression.                                   |
| RUNNING       | blue    | Currently executing.                                          |
| SKIP          | grey    | A prerequisite didn't pass; test was skipped.                 |
| INCONCLUSIVE  | orange  | Test couldn't determine PASS/FAIL — timeout, no data,         |
|               |         | cancelled, OR cadence is steady but at a non-production rate  |
|               |         | (e.g. firmware in 2-second test mode).                        |
| PENDING       | grey    | Hasn't run yet in this session.                               |

**Cadence tests (C1, D2) — measured period and tolerant verdicts**

Both cadence tests now report the **actual measured inter-arrival times**
(`min / max / avg / jitter`) in their summary and details. The verdict
logic is:

- All gaps within **[55, 70] s** → **PASS** with the measured average.
- Gaps **outside** that window but **tightly clustered** (jitter < 10%
  of the average, or < 0.5 s) → **INCONCLUSIVE** (orange) with a
  message like *"steady ~2.0 s but ≠ production 60 s — is firmware in
  test mode?"*. So a debug build with a shortened sensor period
  doesn't trip a noisy FAIL — it just flags the rate as unusual.
- Inconsistent gaps → **FAIL**.

**Haptic tests (E1–E5) — user-confirmed Pass/Fail**

The BLE link gives no ground truth that the motor actually buzzed —
the firmware notifies you *what state it's IN*, but not whether the
motor coil is physically responding. So every haptic test:

1. Performs the BLE write (motor ON/OFF, intensity, countdown timer).
2. Pops up a modal **`Manual check — E_X`** dialog with a clear
   question and two big buttons: **`✓ Pass`** and **`✗ Fail`**.
3. Records the user's verdict as the test result.

E.g. **E1** asks *"Did the motor START buzzing?"*, **E5** asks *"After
~5 s, did the motor buzz briefly and then stop?"*, etc. Closing the
dialog without clicking is recorded as Fail.

**Circular-buffer (ring) testing — F5..F11**

The sensor-block storage is a 234-block ring with **page-level wrap**
(26 blocks lost per wrap). The suite verifies the wrap end-to-end:

- F5 — sync pointer advances after every download (so the ring never
  destroys un-downloaded data).
- F6 — within one boot, no duplicate sequence numbers (ring index math
  is correct).
- F7 — within one boot, sequence span ≤ 234 (capacity honoured).
- F8 — slot wrap (slot 233 → 0) carries `seq + 1`, so the sequence
  keeps increasing across the wrap.
- F9 — block sequence resets to 1 after a reboot (firmware design); any
  regression must align with a `BOOT` journal entry.
- F11 — every `FLASH_WRAP` audit entry has a valid block index.

A real wrap takes ~3.9 hours of runtime. Bucket H1 (opt-in) catches a
real wrap if you leave the tool open that long; otherwise F5–F11
verify the invariants the wrap depends on without forcing one.

**Result types in the JSON export**

```json
{
  "run_at": "2026-04-30T15:42:18",
  "results": {
    "F5": {
      "test_id": "F5", "bucket": "F", "name": "ring_pointer_advance",
      "status": "PASS",
      "summary": "sync pointer caught up (count = 0)",
      "details": ["pre = 7", "downloaded 7 blocks all CRC OK", "post = 0"],
      "duration_s": 0.6,
      "started_iso": "2026-04-30T15:43:01"
    },
    ...
  },
  "context": {
    "samples_count": 5, "blocks_count": 7, "journal_count": 12,
    "sent_epoch": 1735574400, "disconnect_count": 0, "error_state_seen": false
  }
}
```

Attach this JSON to firmware bug reports — it's a complete record of
every measurement the suite took.

[↑ Back to index](#index)

### 4.8 Log tab

Global activity log capturing **every BLE operation and user action**
performed during this session. Lives as the last tab in the notebook.

What gets logged:

| Kind         | Source       | Example                                              |
| ------------ | ------------ | ---------------------------------------------------- |
| `scan`       | BLEManager   | `start: timeout=8s` / `done: 12 device(s)`           |
| `connect`    | BLEManager   | `begin → C4:7B:66:49:B3:B5` / `ok MTU=185`            |
| `disconnect` | BLEManager   | `device disconnected` / `explicit disconnect`        |
| `read`       | BLEManager   | `0000152b…  2 B  07 00`                              |
| `write`      | BLEManager   | `00001524…  3 B  00 00 00`                           |
| `subscribe`  | BLEManager   | `00001535… CCCD on`                                  |
| `unsubscribe`| BLEManager   | `00001535… CCCD off`                                 |
| `error`      | BLEManager   | `read 00001524 failed: <error>`                      |
| `info`       | App / tabs   | `tool started` / `Log tab attached`                  |
| `ui`         | tabs         | (placeholder for future UI-action hooks)             |
| `test`       | Validation   | (placeholder for future per-test events)             |

Controls:

- **Clear** — wipes the in-memory log (capped at 5000 entries anyway).
- **Save to file…** — exports the entire log to `dusq_session_<timestamp>.log`.
- **Refresh** — re-renders from memory (use after toggling filters).
- **Filter checkboxes** — show only the categories you want. Untick
  `read` if the log is dominated by sensor reads, etc.

Each line format:

```
[HH:MM:SS.ms][source][      kind] message
```

Useful when:

- A scan returns no devices and you want a timestamp + result count
  for the bleak call.
- A test fails with an unexpected `read` / `write` error — find the
  exact byte sequence the firmware rejected.
- Reproducing a session offline — save the log and replay it manually.

[↑ Back to index](#index)

---

## 5. What gets tested

A complete run touches every external surface of the firmware. Here's the
breakdown by subsystem:

### BLE stack
- Advertising packet visibility (Connect tab scan).
- GAP connection and MTU negotiation (Connect tab status bar).
- Custom 128-bit base UUID resolution (every read/write/subscribe).
- GATT service & characteristic discovery (one-shot at connect).
- Disconnect detection (`on_disconnect` callback fires).

### Authentication
- 3-byte PIN write to `0x1524` (validation suite B1).
- State transition `CONNECTED → AUTHENTICATED` (verified via Status char read).
- Wrong-PIN rejection / pre-auth gating tests are **not currently in the
  suite** — they were removed because they require disconnect cycles
  that disturb downstream tests.

### Sensor pipeline
- Temperature sensor read path (TMP102 / on-die sensor).
- Mic / PDM dB calculation — average dB across 20 PDM buffers (~320 ms),
  with A-weighting HPF applied per-sample before RMS.
- Lux sensor (BH1750 or similar) read path.
- 60-second sensor-cycle timer (`EVT_SENSOR_TICK`).
- Notification fan-out for the merged sensor char.
- Battery-level service updates.

### Flash logging — sensor blocks
- Block-count characteristic accuracy (validation suite F1).
- 30-minute aggregation into one 152-byte block.
- CRC32 over the 28-byte header — host re-computes (validation suite F2).
- Pre-sync vs post-sync timestamp encoding (validation suite F4).
- Bulk transfer handshake: subscribe-then-write on the same UUID (`0x152D`).
- HVN-TX-COMPLETE pacing of the bulk download stream.
- Reading-count field consistency (validation suite F3).
- Skip-on-corrupt server-side handling (host sees gaps in `seq`).
- **Circular buffer wrap invariants** (validation suite F5–F11):
  sync-pointer advance, no-duplicates per boot, capacity ≤ 234,
  monotonic across slot wrap, reboot-aligned regressions, FLASH_WRAP
  audit trail.

### Flash logging — journal
- Journal-count characteristic accuracy (`0x1534`).
- Bulk transfer handshake: subscribe to `0x1535`, write `0x01` to `0x1536`.
- Boot history via `BOOT` events (`data` = boot_count).
- Time-sync confirmation via `TIME_SYNC` events (`data` = epoch sent).
- Storage rollover via `FLASH_WRAP` events (block index that wrapped).
- Per-entry CRC32 verification on the host.

### Time sync
- Epoch write to `0x152E`.
- Subsequent flash blocks carry real wall-clock year/month/day/...

### Haptic motor (user-confirmed Pass/Fail)
- Manual ON / OFF (E1 / E2).
- Duty-cycle / intensity setting (E3) with motor activated for the
  user to feel the level.
- Intensity clamp (E4) — write 200, motor should buzz at full strength.
- Countdown alarm (E5) — 5-second timer, motor should buzz briefly when
  it expires.

Each test pops up a Pass/Fail dialog after performing the BLE writes —
since the firmware doesn't know whether the motor coil is physically
responding, only the user can tell.

### State machine
- `SYS_INIT → SYS_SLOW_ADV` at boot.
- `SYS_SLOW_ADV → SYS_FAST_ADV` on hall / USB change (visible via status).
- `SYS_FAST_ADV → SYS_CONNECTED` on connect.
- `SYS_CONNECTED → SYS_AUTHENTICATED` on PIN match.
- `SYS_AUTHENTICATED → SYS_FLASH_TRANSFER` on bulk-download start.
- Disconnect path returns to `SYS_SLOW_ADV` cleanly.

### Things this tool does **not** test
- Watchdog timer / fault recovery (would need to deliberately hang firmware).
- Bootloader / DFU.
- USB power-path hardware.
- Long-term battery drift (single-session tool only).
- Pairing / bonding (firmware rejects pairing by design).

[↑ Back to index](#index)

---

## 6. BLE protocol reference

Quick lookup table of every UUID this tool talks to. The 128-bit base
(verified against nRF Connect for Mobile on a live device) is

```
0000XXXX-bcea-5f78-2315-deef12120000
```

where `XXXX` is the 16-bit short. The base is defined in
[src/config/ble_config.h](../src/config/ble_config.h); the SoftDevice
substitutes the short into the first 4 hex digits at advertising time.

| Service / Char                | Short  | Properties     | Payload                            |
| ----------------------------- | ------ | -------------- | ---------------------------------- |
| Auth Service                  | 0x1523 | —              | —                                  |
| └ Auth PIN                    | 0x1524 | W + N          | 3-byte PIN                         |
| Sensor Service                | 0x1525 | —              | —                                  |
| └ Sensor Data (merged)        | 0x1526 | R + N          | 6 B `<hBBH>` temp(0.1°C i16)/db/peak/lux — tool also reads 5 B `<bBBH>` legacy (int8 °C) |
| └ Status                      | 0x1529 | R + N          | 10-byte packed status              |
| Flash Service                 | 0x152A | —              | —                                  |
| └ Block count                 | 0x152B | R              | uint16 LE                          |
| └ Record stream               | 0x152D | W + N          | write `0x01` to start; 152 B blocks|
| └ Time sync                   | 0x152E | W              | uint32 LE Unix epoch UTC           |
| └ Journal count               | 0x1534 | R              | uint16 LE                          |
| └ Journal record              | 0x1535 | N              | 32-byte journal entries            |
| └ Journal start               | 0x1536 | W              | write `0x01` to start              |
| Haptic Service                | 0x152F | —              | —                                  |
| └ Control                     | 0x1530 | W              | `0x01`=ON / `0x00`=OFF             |
| └ Intensity                   | 0x1531 | R + W          | uint8 0–100                        |
| └ Countdown timer             | 0x1532 | W              | uint32 LE seconds                  |
| └ Status                      | 0x1533 | R + N          | 0=idle / 1=buzz / 2=countdown      |
| Battery Service               | 0x180F | (SIG std)      | —                                  |
| └ Battery level               | 0x2A19 | R + N          | uint8 0–100                        |

[↑ Back to index](#index)

---

## 7. Typical test session walkthrough

The recommended order from a freshly powered device:

1. **Connect tab** → Scan → device list shows **only** `DUSQ_CHARGER`
   (with the `Total seen` label confirming radio is healthy). Double-click
   the row to connect. Watch the top-right indicator flip green.
2. **Auth tab** → leave PIN as `00 00 00` → Authenticate. The status
   label flips **green: AUTHENTICATED** within ~1 s after the tool reads
   the device's Status char. (Battery tab auto-subscribes in the
   background once the PIN write returns.)
3. **Flash tab → Sensor Blocks** → Send Time Sync (so subsequent flash
   blocks carry real wall-clock time).
4. **Sensors tab** → Subscribe to Notifications.
5. Wait at least one 60-second cycle so a sensor reading lands.
6. **Flash tab → Sensor Blocks** → Get Block Count → Download Blocks.
   Inspect a few rows for CRC OK and plausible data.
7. **Flash tab → Journal** → Get Journal Count → Download Journal.
   Look for `BOOT` events (boot history) and a `TIME_SYNC` matching the
   epoch you sent in step 3.
8. **Haptic tab** → Set Intensity 50 → Motor ON → Motor OFF.
9. **Battery tab** → already streaming since step 2; check the rolling
   history list and chart for plausible deltas.
10. **Validation tab** → click **Run All**. Watch tests stream their
    progress live in the Details pane. The full sweep takes ~15–20 min;
    the Pass/Fail dialogs in Bucket E require you to be at the keyboard
    to confirm the motor buzzed.
11. **Connect tab** → Disconnect (or close window). Indicator goes red,
    Reconnect button activates so you can dial back without scanning.

If the link drops mid-session, click **`↻ Reconnect <NAME>`** in the
header — no need to repeat the scan/select dance.

[↑ Back to index](#index)

---

## 8. Troubleshooting

The **Log tab** (last tab) is your friend — every BLE op + scan adv +
error gets timestamped there. Filter to `error` to instantly surface
anything that broke.

| Symptom                                     | Likely cause / fix                                                         |
| ------------------------------------------- | -------------------------------------------------------------------------- |
| Scan finds nothing  (`Total seen: 0`)       | Bluetooth disabled; OS BT stack stuck; another central is already         |
|                                             | connected (only one BLE conn at a time). Restart Bluetooth service.       |
| `Total seen > 0` but no DUSQ_CHARGER       | Firmware not advertising the right name/UUID. Tick **Show all (debug)**  |
|                                             | to see every MAC. Cross-check with nRF Connect for Mobile.                |
| Scan finds device but Connect hangs         | OS-level pairing cache stale — Forget the device in OS Bluetooth          |
|                                             | settings, then re-scan.                                                    |
| **Connected to wrong MAC** (every op fails) | Look at the Log tab — `connect: ok addr=…` shows what you actually       |
| with "Characteristic not found"             | linked to. The DUSQ MAC is `C4:7B:66:…`. If the connect address is       |
|                                             | something else, you picked the wrong row. Disconnect, re-scan.            |
| Auth label stays grey                       | `_do_auth` reads the Status char to confirm. If the read raises, see     |
|                                             | Log tab for the error. Otherwise the firmware truly didn't transition    |
|                                             | to AUTHENTICATED — likely wrong PIN.                                      |
| Sensor values stay `--`                     | Sensor notifies only fire when authenticated. Verify Auth tab is green;  |
|                                             | then click Subscribe again.                                                |
| Flash download stalls                       | Notifications throttled by the OS — close other BLE-using apps.           |
| Tool feels frozen                           | If you're on a build before the asyncio-loop fix, the GUI/event-loop      |
|                                             | bridge is broken — pull latest.                                            |
| `bleak` install fails on Linux              | Need BlueZ headers: `sudo apt install bluez libbluetooth-dev`.             |

[↑ Back to index](#index)

---

## 9. Known limitations

- **Firmware doesn't notify on auth**. Worked around: the Auth tab now
  reads the Status char (`0x1529`) after the PIN write to confirm
  `state == AUTHENTICATED`. This is the only honest signal.
- **Firmware doesn't refresh the journal-count GATT value** until the
  host writes `0x01` to `JOURNAL_START`. So `Get Journal Count` returns
  a stale `0` until you trigger a download once. Validation suite G1
  flags this as a known-stale read.
- **Bucket H (long-running) tests are stubs in Pass 1**. They mark
  themselves INCONCLUSIVE; real long-soak watchers are deferred.
- **Block CRC only covers the 28-byte header**. Payload corruption is
  invisible to the host. See firmware roadmap below.
- **Single BLE connection at a time** — bleak limitation, fine for our use.
- **No headless / CLI mode** — everything is GUI-driven for now.

[↑ Back to index](#index)

---

## 10. Roadmap

### Tool side (host)

1. **Headless / CLI mode** — drive the suite from a script for CI, no
   Tk window required. Would need to factor out a `DusqClient` class.
2. **Persist window geometry + last-connected MAC** across runs, so
   relaunches don't reset everything to defaults.
3. **Bucket H implementation** — actual ≥ 4 h wrap watcher and
   connection-uptime stress runner.
4. **Generate UUID constants from `src/config/ble_config.h`** at build
   time so host and firmware can never drift again.
5. **CSV export** for collected sensor samples (in addition to the
   existing JSON export).

### Firmware side (suggested integrity additions)

1. **Refresh `journal_count` on every append** — call
   `ble_svc_update_journal_count()` from `flash_append_journal_event()`
   so the host's `Get Journal Count` read is always fresh.
2. **Extend block CRC to cover the payload** — today's CRC only protects
   the 28-byte header; sensor data slots are unprotected. Either widen
   the CRC range or add a `payload_crc32` in the reserved area.
3. **Persist `g_block_sequence` across reboots** — currently it resets
   to 0 on boot (unlike `g_journal_seq` which is restored). Asymmetry
   makes per-boot test logic harder.
4. **Auth-success notify** on the auth char — would let the host detect
   auth without a fallback Status read.
5. **Debug fast-fill command** — write a uint8 N to a debug-only char
   and have firmware synthesise N blocks in seconds, so the validation
   suite's H1 wrap watcher can be deterministic instead of a 4-hour soak.

[↑ Back to index](#index)
