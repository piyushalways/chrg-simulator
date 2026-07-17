#!/usr/bin/env python3
"""DUSQ Charger BLE Test Tool — thin launcher.

The tool was split from this single file into the ``dusq_sim/`` package
(one module per tab + shared core).  This shim preserves the original
invocation so nothing downstream changes:

    python ble_test_tool.py     # unchanged
    python -m dusq_sim          # equivalent

See dusq_sim/README.md for the package layout.
"""
import os
import sys

# When launched by path (python ble_test_tool.py) this file's directory is
# already sys.path[0], so `dusq_sim` is importable; add it explicitly too so the
# shim also works if invoked from an unusual cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dusq_sim.app import main

if __name__ == "__main__":
    main()
