"""Connect to a live Fusion Electronics design and summarize it.

Run from the repo root, with a board (or schematic) open in Fusion:

    python examples/read_design.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from bridge import FusionBridge  # noqa: E402

b = FusionBridge().connect()
print(f"connected: {b.url}  (session {b._sid[:8]}…)")

elements = b.electronics_read("electronics.Element",
                              fields=["name", "value", "x", "y", "angle"])
signals = b.electronics_read("electronics.Signal", fields=["name"])
print(f"{len(elements)} elements, {len(signals)} signals\n")

for e in elements[:12]:
    print(f"  {e['name']:<6} ({e['x']:>9.3f}, {e['y']:>9.3f})  angle={e['angle']}")
if len(elements) > 12:
    print(f"  … and {len(elements) - 12} more")
