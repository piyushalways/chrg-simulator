# dusq_sim — DUSQ Charger BLE Test Tool (package layout)

The tool was refactored from a single ~6,100-line `ble_test_tool.py` into this
package. Behaviour is unchanged except for the three feature items called out
below. `ble_test_tool.py` is now a thin launcher shim.

## Run

```
python ble_test_tool.py     # unchanged invocation (shim)
python -m dusq_sim          # equivalent
```

Dependencies are the same: `bleak`, `pylink-square`, `matplotlib` (all optional
ones degrade exactly as before — `HAS_MPL` / `HAS_PYLINK`).

## Layout

```
ble_test_tool.py              thin shim  ->  dusq_sim.app.main
dusq_sim/
  __init__.py                 import-light package marker
  __main__.py                 `python -m dusq_sim` entry
  constants.py                UUIDs, flash/journal geometry, EVENT/RESET/FAULT tables,
                              sensor error markers, DOWNLOAD_IDLE_TIMEOUT, ADV MSD layout
  decoders.py                 pure payload decoders: compute_auth_pin, decode_block,
                              decode_journal_entry, format_event_data, decode_status,
                              decode_dusq_msd, fmt_next_buzz, journal_timestamp_str
  util.py                     ts(), run_async(), hex_dump(), fmt_epoch_local(),
                              UI-state persistence (_load_state/_save_state), matplotlib shim
  ble.py                      BLEManager — bleak central (scan/connect/read/write/subscribe)
  rtt.py                      RTT engine: FlowEngine, RttReader, flow/anomaly rules (pylink)
  app.py                      App window: header, notebook, tab wiring, tab-visible
                              dispatch, latency probe, lifecycle;  main()
  tabs/
    devices.py                Devices (passive scan + MSD decode)          [AdvTab]
    auth.py                   Auth (PIN)                                   [AuthTab]
    device_info.py            Device Info (DIS)                            [DeviceInfoTab]
    sensors.py                Sensors (live + chart)                       [SensorTab]
    flash.py                  Flash (sensor blocks only)                   [FlashTab]
    journal.py                Journal (own tab; human timestamps)          [JournalTab]  *new tab*
    haptic.py                 Haptic (3-section, 5-state status)           [HapticTab]   *rebuilt*
    battery.py                Battery (live + history)                     [BatteryTab]
    automated_testing.py      Automated Testing catalogue                  [ValidationTab]
    log.py                    Log + the shared EventLog model              [LogTab]
    rtt_flow.py               RTT / Flow monitor tab                       [RttTab]
```

## How the split was done (zero-regression)

Tab classes and the core (`BLEManager`, RTT engine, decoders) were **moved by exact
line-slicing from the original file** — the logic is byte-for-byte identical, only
the imports changed. Shared names are pulled in with `from ..constants import *` /
`..decoders` / `..util` (each shared module declares `__all__` so underscore helpers
like `_decode_sensor_payload` still export), plus `from ..ble import BLEManager`.
Every module carries the original stdlib import header, so no name can go unresolved.

The App wiring is preserved verbatim: same tab constructor signatures, the
`auth -> battery/device-info` hook, the per-tab `_on_disconnected` disconnect
subscriptions, the `_on_tab_visible` dispatch, the latency probe, and the
state-persistence shutdown.

## The three feature changes

1. **Journal is its own top-level tab** (`tabs/journal.py`). It was the second page
   inside the Flash tab; it is now `JournalTab`, added to the notebook right after
   **Flash**. `FlashTab` keeps only the Sensor Blocks page.
2. **Human-readable journal timestamps.** `decode_journal_entry` now also reads the
   firmware `sync_status` byte (entry `reserved[0]`, offset 20). `journal_timestamp_str()`
   renders **post-sync** entries as a local date-time (`2026-07-17 21:00:00`) and
   **pre-sync** entries as `boot+Ns` (never a bogus 1970 date). The raw epoch/boot
   value is kept in a secondary **"Raw ts"** column and in the detail pane.
3. **Haptic tab rebuilt** into 3 sections — *Manual motor test* (0x1530/0x1531),
   *Reminder schedule* (0x1537, with quick presets and the 0x1532 `[N,0]` one-shot
   shortcut), and *Live status* (0x1533). The intensity slider float bug is fixed,
   and 0x1533 is decoded into a 5-state view
   `{0 idle, 1 buzzing, 2 single-shot-waiting, 3 recurring-waiting, 4 pending-sync}`.
   **Note:** on the wire the 0x1533 motor_state byte is only `0/1`; states `2..4` are
   *synthesized by the tool* from the schedule fields + flags byte for readability.

## Verification performed

- `python -m py_compile` on every module — **passes**.
- `import dusq_sim.app` — imports every module cleanly (validates all wiring).
- Full `App(loop)` constructed headlessly — all **11 tabs** build without error.
- `decode_journal_entry` + `journal_timestamp_str` and the Haptic 5-state decoder
  exercised with crafted payloads — correct.

Not runnable in this environment: the interactive `mainloop()` (needs a real
display + a device). Do the final on-device click-through as usual.
