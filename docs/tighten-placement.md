# Tighten placement — `/loop` task

Iteratively tighten the placement of the **GROUP-selected** parts on the live
Fusion board: minimize airwire (ratsnest) length with 90° rotations, keeping
parts legal (no courtyard-clearance overlaps, inside the board edge). Runs
placement passes until one pass changes total signal-airwire length by less than
1 mm, then stops.

## How to run

1. In Fusion's Board editor, **GROUP-select** the sub-circuit parts (rubber-band
   Group tool — *not* a single click). For "place a sub-circuit around an IC",
   group the IC's connected parts but **not** the IC, so the IC stays frozen.
2. Start a fresh agent and enter:

   ```
   /loop follow docs/tighten-placement.md
   ```

   No interval → dynamic mode: the agent self-paces and stops on convergence.

## The task (instructions to the agent)

This prompt is **identical every firing** — you start fresh each time, so read
this whole section, then read `~/tmp/place_loop_state.json` to recover progress.
That state file is your only memory across firings: **absent ⇒ this is the first
firing**; present ⇒ a run is already in progress (continue it).

**Context**
- Project `/home/holla/steinmetz` drives a LIVE Autodesk Fusion Electronics PCB
  over an HTTP bridge from WSL. Read `CLAUDE.md` for the full picture. It needs a
  live Fusion session with the board open; if the bridge can't connect, STOP and
  say so (that's the environment, not the code).
- Setup each run: `cd /home/holla/steinmetz && source .venv/bin/activate`
- The parts to place are GROUP-selected in Fusion (rubber-band Group tool, not a
  click). `python src/selection.py` prints them. If it prints nothing, STOP and
  tell the user to GROUP-select the sub-circuit parts (group the IC's connected
  parts but NOT the IC).
- `python src/place.py --rotate 90` places ONLY the selected parts (freezes
  everything else), tries 90° rotations, writes ROTATE/MOVEs over the bridge,
  re-reads and verifies pad positions, and prints a line like:
  `Signal-airwire: <BEFORE> mm  ->  <AFTER> mm  (<pct>%)`. Courtyard clearance
  (`--clearance`, default 0.3 mm) and board margin (`--margin`, default 1.0 mm)
  keep parts legal — tune only if you see overlaps/edge issues.
- `python src/screenshot.py C:\tmp\place_<n>.png` snapshots the board (readable
  at `~/tmp/place_<n>.png`). Use the Read tool to VIEW it and sanity-check the
  layout.

**First firing** (state file absent): before the first placement pass, capture a
baseline screenshot to `C:\tmp\place_before.png` and create
`~/tmp/place_loop_state.json` with the starting airwire and `iteration: 0`.

**Each iteration**
1. Run `python src/place.py --rotate 90`; parse BEFORE and AFTER airwire from its
   output.
2. Capture and VIEW a screenshot; confirm the result looks right (parts on-board,
   no overlaps, airwire visibly shorter).
3. Persist iteration count, starting airwire, and latest AFTER to
   `~/tmp/place_loop_state.json` (you lose memory between firings — read it back
   at the start of each firing).
4. **Convergence:** if this pass changed airwire by < 1.0 mm
   (`|BEFORE - AFTER| < 1.0`), you are DONE — STOP the loop (do not reschedule).
5. **Safety:** also STOP after 15 iterations, or immediately if `place.py` errors
   (e.g. a Fusion dialog is open — report it) rather than looping on a failure.

You MAY modify `src/place.py` if it lacks functionality you need (e.g. cleaner
machine-readable airwire output) — keep its read→compute→write→verify structure
and match the existing code style.

**Guardrails:** changes are unsaved on the live design — do NOT save; the user
saves in Fusion. Only touch the selected parts. Never leave an EAGLE command
un-terminated.

**When converged**
1. Capture the final board to `C:\tmp\place_after.png`.
2. Report: starting vs final airwire (mm and %), iteration count, that no
   clearance/edge violations remain, and the screenshot paths.
3. Update `docs/PLACE.md` to match the current selection-only placer (90°
   rotation always on, always writes + verifies, clearance heuristic,
   GROUP-select input), replacing `docs/img/place-before.png` and
   `docs/img/place-after.png` with your `place_before.png` / `place_after.png`.
