# Tighten placement — `/loop` task

Iteratively tighten the placement of the **GROUP-selected** parts on the live
Fusion board: minimize airwire (ratsnest) length with 90° rotations, keeping
parts legal (no ComponentExcludeTop/Bottom clearance overlaps, inside the board
edge). Runs
placement passes until one pass changes total signal-airwire length by less than
1 mm, then stops.

## How to run

1. In Fusion's Board editor, **GROUP-select** the parts you want placed
   (rubber-band Group tool — *not* a single click). `place.py` moves *exactly*
   what's in the group and freezes everything else, so group precisely the parts
   you want moved. (What to group is your call — e.g. leave an IC out of the
   group to keep it fixed, or include it to let it move too.)
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
  click). `python src/selection.py` prints them. Place **exactly** those parts —
  whatever the user grouped is the intended selection; do not second-guess or
  add/remove parts. If it prints nothing, STOP and tell the user to GROUP-select
  the parts to place.
- `python src/place.py --rotate 90` places ONLY the selected parts (freezes
  everything else) at their airwire-optimal position and rotation, writes
  ROTATE/MOVEs over the bridge, re-reads and verifies pad positions, and prints a
  line like: `Signal-airwire: <BEFORE> mm  ->  <AFTER> mm  (<pct>%)`. It is
  near-optimal in a **single pass** and idempotent, so the loop typically
  converges in 1–2 firings. Component-exclude clearance (`--clearance`, default
  0.1 mm) and board margin (`--margin`, default 1.0 mm) keep parts legal. Part
  origins snap to one tenth of Fusion's current main grid by default; `--grid`
  uses the full main grid and `--nogrid` disables snapping. `place.py` must query
  the main grid inside every run because that value is live Fusion state; the
  current implementation does this inside `Placer` by generating and running
  `C:\tmp\steinmetz_grid_main.ulp` (`B.grid.distance` / `B.grid.unitdist`). Tune
  only if you see overlaps/edge issues.
- `python src/screenshot.py C:\tmp\place_<n>.png` snapshots the board (readable
  at `/mnt/c/tmp/place_<n>.png`; `~/tmp/place_<n>.png` also works on this host).
  Use the Read tool to VIEW it and sanity-check the layout.

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
