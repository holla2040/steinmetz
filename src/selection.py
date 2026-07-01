"""Read the user's live Fusion Electronics selection over the bridge.

The bridge's ``execute`` runs Python *inside* the live Fusion process — same
process, on the main thread, in the Board workspace (verified live: the pid
matches, ``threading.main_thread()`` is current, ``activeWorkspace`` is
``BoardLayoutEnvironement``). So this is **not** an out-of-process limitation.
The catch is narrower: the Electronics (BoardLayout) editor keeps its selection
in the embedded EAGLE engine, not in Fusion's ``ui.activeSelections``. That
collection is the Fusion-*design* selection and reads ``0`` for an Electronics
board even with parts highlighted — confirmed live while the EAGLE group was
populated (the ULP below saw 15 parts in the very same ``execute`` call, same
instant ``activeSelections.count`` was ``0``). ``adsk.electron.Element`` carries
no selection flag and ``Board.selectionSets`` throws, so the only thing that
sees the selection is EAGLE itself: in a User-Language Program (ULP),
``ingroup(obj)`` is true for every board object in the active **GROUP**.

So :func:`read_selection` is fully bridge-driven — one :meth:`execute` round trip:

1. Fusion-side Python writes a tiny ULP to ``C:\\tmp\\steinmetz_selection.ulp``.
2. It runs that ULP via ``Electron.run "RUN '…';"``; the ULP walks the board and
   writes every grouped element's ref to ``C:\\tmp\\steinmetz_selection.txt``
   (one per line).
3. The same script reads that file back and prints it; we parse the refs out of
   the execute ``message``.

No Scripts panel, no Text Commands paste — nothing has to be run by hand in
Fusion. The output file is left on disk so the WSL side can read it too
(``C:\\tmp`` == WSL ``~/tmp`` on this host).

**The gesture that matters:** the parts must be in the EAGLE *group*, i.e.
selected with the **Group** tool (the rubber-band / lasso select), not a single
left-click. In the Board editor that's the Group command (the dashed-rectangle
toolbar button) — drag a box around the parts, or Group + click to add. A plain
click-select is *not* visible to ``ingroup()`` out of process.

This is the shared "what's selected" utility every selection-aware tool builds
on (``place.py`` is the first). Run standalone to see what's captured:

    python src/selection.py
"""
from __future__ import annotations

import sys

from bridge import FusionBridge, FusionError

# C:\tmp == WSL ~/tmp on this host, so the output file is visible from WSL too.
SELECTION_PATH = r"C:\tmp\steinmetz_selection.txt"   # refs, one per line
_ULP_PATH = r"C:\tmp\steinmetz_selection.ulp"        # the generated ULP

_OUT_FWD = SELECTION_PATH.replace("\\", "/")         # EAGLE wants forward slashes
_ULP_FWD = _ULP_PATH.replace("\\", "/")

# The ULP. ``ingroup(E)`` is true for each element in the active GROUP; ``output``
# truncates the file even when nothing matches (so an empty file == empty group).
_ULP = '''output("__OUT__", "wt") {
  board(B) {
    B.elements(E) {
      if (ingroup(E))
        printf("%s\\n", E.name);
    }
  }
}
'''.replace("__OUT__", _OUT_FWD)

# Run the ULP via the EAGLE interpreter. Single quotes around the path — no
# double quotes (they'd break the ``Electron.run "<cmd>"`` wrapper).
_RUN_CMD = "Electron.run \"RUN '%s';\"" % _ULP_FWD


def _capture_script() -> str:
    """Fusion-side Python: write the ULP, run it, echo the result file back."""
    return "\n".join([
        "import adsk.core",
        "import os",
        "def run(_context):",
        "    app = adsk.core.Application.get()",
        f"    ulp_path = {_ULP_PATH!r}",
        f"    out_path = {SELECTION_PATH!r}",
        "    os.makedirs(os.path.dirname(ulp_path), exist_ok=True)",
        "    try:",                      # drop a stale result so a failed run
        "        os.remove(out_path)",   # can't masquerade as the current one
        "    except OSError:",
        "        pass",
        "    with open(ulp_path, 'w') as f:",
        f"        f.write({_ULP!r})",
        f"    app.executeTextCommand({_RUN_CMD!r})",
        "    print('__SEL_START__')",
        "    try:",
        "        with open(out_path) as f:",
        "            print(f.read())",
        "    except FileNotFoundError:",
        "        print('__SEL_MISSING__')",
        "    print('__SEL_END__')",
    ])


def read_selection(bridge: FusionBridge) -> list[str]:
    """Reference designators of the parts in Fusion's active GROUP.

    Drives the whole capture over the bridge (see module docstring) and returns
    the grouped refs in board order, or ``[]`` if the group is empty. Raises
    :class:`FusionError` if the capture produced no parseable output or the ULP
    didn't run (e.g. no Board editor open).
    """
    msg = bridge.execute(_capture_script()).get("message", "") or ""
    if "__SEL_START__" not in msg or "__SEL_END__" not in msg:
        raise FusionError(f"selection capture produced no parseable output: {msg!r}")
    body = msg.split("__SEL_START__", 1)[1].split("__SEL_END__", 1)[0]
    if "__SEL_MISSING__" in body:
        raise FusionError("the selection ULP did not run (no output file). Is a "
                          "Board editor open in Fusion?")
    refs = []
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            refs.append(line)
    return refs


def main() -> int:
    bridge = FusionBridge().connect()
    refs = read_selection(bridge)
    if not refs:
        print("Nothing in the group. In Fusion's Board editor, GROUP-select the "
              "parts (rubber-band with the Group tool), then re-run.")
        return 1
    print(f"{len(refs)} selected: {', '.join(refs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
