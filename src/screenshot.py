"""Capture a PNG of the live Fusion board view, read back into WSL.

The companion to ``selection.py`` for *visual* verification: after a placement
run, snapshot the board and look at it. Two steps over the bridge:

1. ``WINDOW FIT`` — zoom the Electronics view to fit so the whole board fills
   the frame (always do this first, or the capture is whatever was on screen).
2. ``EXPORT IMAGE '<path>' <dpi>`` — the EAGLE command writes a PNG to a path on
   the Fusion (Windows) host.

Note: in the PCB Editor workspace ``app.activeViewport`` is ``None`` (that's the
3D-design path), so ``saveAsImageFile`` can't be used — the board-native
``EXPORT IMAGE`` command is the route.

Path mapping: this host's WSL ``~/tmp`` is a symlink to ``C:\\tmp`` (Win11), so
Fusion writes ``C:\\tmp\\x.png`` and we read it at ``~/tmp/x.png``.

    python src/screenshot.py                 # capture -> print the WSL path
    python src/screenshot.py C:\\tmp\\b.png   # ...to a chosen Fusion-side path
"""
from __future__ import annotations

import os
import sys

from bridge import FusionBridge

# Default Fusion-side (Windows) path; C:\tmp == WSL ~/tmp on this host.
DEFAULT_WIN_PATH = r"C:\tmp\steinmetz_view.png"


def wsl_path(win_path: str) -> str:
    """Map a Windows path (``C:\\tmp\\x.png``) to its WSL mount (``/mnt/c/tmp/x.png``)."""
    p = win_path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        return "/mnt/" + p[0].lower() + p[2:]
    return p


def capture(bridge: FusionBridge, win_path: str = DEFAULT_WIN_PATH,
            dpi: int = 150, fit: bool = True) -> str:
    """Snapshot the active board view to ``win_path`` (Windows); return that path.

    ``fit`` fires ``WINDOW FIT`` first (the usual case). Read the result from
    :func:`wsl_path` of the returned path. Raises on a capture failure.
    """
    # Clear any stale file first so a re-export can't trip an overwrite prompt
    # (the WSL mount is the same dir, so do it locally; harmless if absent).
    try:
        os.remove(wsl_path(win_path))
    except OSError:
        pass
    if fit:
        bridge.run_eagle("WINDOW FIT")
    # EAGLE wants forward slashes / single quotes (no double-quotes over the bridge).
    posix = win_path.replace("\\", "/")
    bridge.run_eagle(f"EXPORT IMAGE '{posix}' {dpi}")
    out = wsl_path(win_path)
    if not os.path.exists(out):
        raise RuntimeError(f"viewport capture produced no file at {out!r} "
                           f"(EXPORT IMAGE '{posix}' {dpi})")
    return win_path


def main() -> int:
    win = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_WIN_PATH
    bridge = FusionBridge().connect()
    path = capture(bridge, win)
    print(f"Captured {path}")
    print(f"Read it at: {wsl_path(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
