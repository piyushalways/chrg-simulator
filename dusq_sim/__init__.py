"""DUSQ Charger BLE Test Tool — packaged.

Run the GUI with either:
    python -m dusq_sim
    python ble_test_tool.py   (thin shim in the parent directory)

Kept intentionally import-light: importing this package does NOT pull in Tk /
bleak / matplotlib, so `from dusq_sim import decoders` stays cheap for tests.
The GUI entry point is dusq_sim.app.main.
"""
