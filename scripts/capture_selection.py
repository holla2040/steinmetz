"""OBSOLETE manual fallback — selection is now read over the bridge.

``src/selection.py`` :func:`read_selection` captures the selection 100% over the
bridge now (it drives a ULP whose ``ingroup()`` sees the active GROUP), so you no
longer need to run anything by hand in Fusion. Just GROUP-select the parts and
run ``python src/selection.py`` / ``python src/place.py``.

This ``ui.activeSelections`` approach does **not** work for an Electronics board
— not even from Fusion's own Scripts panel. The bridge already runs this Python
inside the live Fusion process (same pid, main thread, BoardLayout workspace),
and ``ui.activeSelections.count`` still reads ``0`` with parts selected: that
collection is the Fusion-*design* selection and is never wired to the Electronics
editor, whose selection lives in the embedded EAGLE engine (only ``ingroup()``
in a ULP can see it). Confirmed live by having this very code write its result to
the temp file — it wrote ``count=0`` while the ULP path saw 15 parts. Kept here
only as a record of the dead-end; use ``src/selection.py`` instead.

Writes ``C:\\tmp\\steinmetz_selection.txt``, one ref per line. The path is shared
with ``SELECTION_PATH`` in ``src/selection.py`` — keep them in sync. (``C:\\tmp``
is the WSL ``~/tmp`` symlink on this host, so the file is visible from WSL too.)
"""
import os
import traceback

import adsk.core
import adsk.electron

SELECTION_PATH = r"C:\tmp\steinmetz_selection.txt"   # must match src/selection.py


def run(_context):
    app = adsk.core.Application.get()
    ui = app.userInterface
    try:
        board = adsk.electron.Board.cast(app.activeProduct)
        if not board:
            ui.messageBox("Active product is not a PCB board — open the Board "
                          "editor and try again.")
            return

        sels = ui.activeSelections
        refs, skipped = [], 0
        for i in range(sels.count):
            element = adsk.electron.Element.cast(sels.item(i).entity)
            if element:
                refs.append(element.name)
            else:
                skipped += 1            # a wire/via/pad/etc., not a placed part

        # de-dup while preserving selection order
        seen = set()
        refs = [r for r in refs if not (r in seen or seen.add(r))]

        path = SELECTION_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("# steinmetz selection — %d part(s)\n" % len(refs))
            for r in refs:
                f.write(r + "\n")

        note = "" if not skipped else "  (%d non-component item(s) ignored)" % skipped
        app.log("steinmetz: captured %d selected part(s) -> %s" % (len(refs), path))
        ui.messageBox("Captured %d part(s):\n  %s\n\nWrote %s%s\n\nNow run in WSL:\n"
                      "  python src/place.py --selected"
                      % (len(refs), " ".join(refs) or "(none)", path, note))
    except Exception:
        if ui:
            ui.messageBox("Failed:\n%s" % traceback.format_exc())
