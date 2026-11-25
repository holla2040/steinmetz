"""Capture a PNG of the live Fusion board view.

The companion to ``selection.py`` for *visual* verification: after a placement
run, snapshot the board and look at it. Two steps over the bridge:

1. ``WINDOW FIT`` — zoom the Electronics view to fit so the whole board fills
   the frame (always do this first, or the capture is whatever was on screen).
2. ``EXPORT IMAGE '<path>' <dpi>`` — the EAGLE command writes a PNG to a path on
   the Fusion host.

Note: in the PCB Editor workspace ``app.activeViewport`` is ``None`` (that's the
3D-design path), so ``saveAsImageFile`` can't be used — the board-native
``EXPORT IMAGE`` command is the route.

The default path is in the Fusion-side scratch dir (``bridge.fusion_scratch_dir``:
``C:\\tmp`` on Windows, ``/tmp`` on macOS, ``STEINMETZ_FUSION_TMP`` overrides).
Natively Fusion and this script share the filesystem, so the PNG is read at the
same path. Under WSL, ``C:\\tmp\\x.png`` is read at ``/mnt/c/tmp/x.png``.

    python src/screenshot.py                 # capture -> print the path
    python src/screenshot.py C:\\tmp\\b.png   # ...to a chosen Fusion-side path
"""
from __future__ import annotations

import os
import sys

from bridge import FusionBridge, fusion_scratch_path, local_path

DEFAULT_NAME = "steinmetz_view.png"


def capture(bridge: FusionBridge, win_path: str | None = None,
            dpi: int = 150, fit: bool = True) -> str:
    """Snapshot the active board view to ``win_path`` (Fusion-side); return it.

    ``win_path`` defaults to ``steinmetz_view.png`` in the Fusion scratch dir.
    ``fit`` fires ``WINDOW FIT`` first (the usual case). Read the result at
    :func:`bridge.local_path` of the returned path. Raises on a capture failure.
    """
    win_path = win_path or fusion_scratch_path(DEFAULT_NAME)
    out = local_path(win_path)
    # Clear any stale file first so a re-export can't trip an overwrite prompt
    # and a failed export can't pass the existence check below.
    try:
        os.remove(out)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    bridge.ensure_board()  # otherwise EXPORT IMAGE snapshots the schematic
    if fit:
        bridge.run_eagle("WINDOW FIT")
    # EAGLE wants forward slashes / single quotes (no double-quotes over the bridge).
    posix = win_path.replace("\\", "/")
    bridge.run_eagle(f"EXPORT IMAGE '{posix}' {dpi}")
    if not os.path.exists(out):
        raise RuntimeError(f"viewport capture produced no file at {out!r} "
                           f"(EXPORT IMAGE '{posix}' {dpi})")
    return win_path


def main() -> int:
    win = sys.argv[1] if len(sys.argv) > 1 else None
    bridge = FusionBridge().connect()
    path = capture(bridge, win)
    print(f"Captured {path}")
    if local_path(path) != path:
        print(f"Read it at: {local_path(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
