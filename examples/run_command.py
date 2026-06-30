"""Prove the write path: fire a harmless EAGLE command through the bridge.

`WINDOW FIT` just re-fits the Electronics view — it changes nothing in the
design — so this is a safe end-to-end check of the execute / Electron.run path.

    python examples/run_command.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from bridge import FusionBridge  # noqa: E402

b = FusionBridge().connect()
print("execute envelope:", b.run_eagle("WINDOW FIT"))
print("execute channel clear:", b.execute("def run(_context):\n    print('ok')\n"))
