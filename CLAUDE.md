# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Steinmetz drives a **live** Autodesk Fusion Electronics design over Fusion's
local HTTP (JSON-RPC) bridge — read the board, compute something, write changes
back, verify by re-reading. No plugin, no MCP client library. See `README.md`
for the project ethos and `docs/fusion-bridge.md` for the protocol reference.

## Setup, build, run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .                  # installs the only dependency: requests

python examples/read_design.py    # connect + summarize the open design
python examples/run_command.py    # prove the write path (harmless WINDOW FIT)
python src/selection.py           # show the current GROUP-selected parts
python src/place.py               # place the selected parts (writes MOVEs, then verifies)
python src/screenshot.py          # snapshot the board to C:\tmp; read at /mnt/c/tmp
```

The top-level `Makefile` wraps these as thin aliases (`make setup`, `make read`,
`make place`, `make screenshot`, `make selection`; `make help` lists them). It
defaults to the `.venv` interpreter so targets run without activating first. The
tools are flag-driven, so to pass options call the script directly
(`.venv/bin/python src/place.py --only U1`) rather than through `make`.

- **There is no automated test suite, linter, or CI.** "Verify" everywhere means
  re-reading the live design and checking parts landed (see `place.py`'s verify
  step). The `Makefile` only aliases the run commands — there's no `make test`
  or `pytest`; don't look for them.
- **Nothing runs without a live Fusion session.** Every example and tool requires
  a Windows-side Fusion with an Electronics document open, the "Fusion MCP Server"
  preference enabled, and a `netsh` port-forward to the WSL gateway IP. Full
  setup (including the critical `0.0.0.0` gotcha) is in `docs/fusion-bridge.md`.
  If a tool can't connect, suspect this environment, not the Python.
- Host/port can be overridden with `STEINMETZ_FUSION_HOST` / `STEINMETZ_FUSION_PORT`;
  otherwise the host is auto-detected as the WSL default-route gateway.

## Architecture

Three layers, each built on the one below, plus a `.scr` helper:

- **`src/bridge.py` (`FusionBridge`) — transport.** The HTTP handshake,
  `electronics_read` (auto-paginating), `execute` (runs Python inside Fusion),
  and `run_eagle` / `run_eagle_batch` / `run_scr` for the write path. Everything
  else goes through this.
- **`src/board.py` (`read_board` → `Board`) — model.** Pulls the raw
  `electronics.*` entities and joins them into elements, connected pads, nets,
  packages, component-exclude placement geometry, and the board outline. The
  shared "what's on the board" layer that tools consume. The one join that
  matters: `ContactRef` ties an element + a net (`Signal`) to a pad, and the
  pad's placed global position lives on the `Smd`/`Pad` row keyed by
  `contact_object_id`. Placement legality should use package-local
  `ComponentExcludeTop` / `ComponentExcludeBottom` geometry, with contact/SMD
  geometry only as fallback.
- **`src/place.py` (`Placer`) — a tool.** Minimal-airwire placement of the
  **selected** parts (the rest frozen): chooses each part's position and
  rotation jointly from its incident nets, legalizes overlaps against
  component-exclude regions (`--clearance` default 0.1 mm), snaps part origins
  to one tenth of Fusion's current main grid by default (`--grid` for the full
  main grid, `--nogrid` to disable; `Placer` queries the live Fusion main grid
  every run by writing/running `C:\tmp\steinmetz_grid_main.ulp`, which reads
  `B.grid.distance` and `B.grid.unitdist`), writes
  `ROTATE`/`MOVE`s back, then re-reads to verify pad positions. Selection comes
  from `selection.py`;
  `--only REF...` overrides it. Nothing selected → nothing placed. Methodology
  and before/after in `docs/PLACE.md`. The template every future tool follows:
  read → compute → write back → re-read to verify.
- **`src/scr.py` — `.scr` generation/validation.** Renders EAGLE command-line
  scripts with the safety rules baked in (terminate with `;`, reject control
  chars and double-quotes).
- **`src/selection.py` (`read_selection`) — selection utility.** The shared
  "what's selected" layer, fully bridge-driven. `execute()` runs *in-process*
  (same pid/MainThread), but `ui.activeSelections` still reads `0` — it's the
  Fusion *design* selection collection, never wired to the Electronics editor
  (`Element` has no selected flag; `selectionSets` throws). The Electronics
  selection lives in the embedded EAGLE engine, which the bridge *can* drive: so
  `read_selection` (one `execute`) writes a tiny ULP to
  `C:\tmp`, runs it via `Electron.run "RUN '…'"`, and the ULP's `ingroup(E)`
  walks the board writing every grouped ref to
  `C:\tmp\steinmetz_selection.txt`, which the same call reads back. **Gesture:
  parts must be GROUP-selected (rubber-band Group tool), not click-selected.**
  `place.py` uses this as its default input; `--only` overrides with an explicit
  ref list. `scripts/capture_selection.py` is an obsolete manual fallback (the
  only way to capture a plain click-selection, run *inside* Fusion).
- **`src/screenshot.py` (`capture`) — visual verification.** Snapshots the board
  to a PNG for a look (the place → screenshot verify loop). `app.activeViewport`
  is `None` in the PCB Editor, so `saveAsImageFile` can't be used — it fires
  `WINDOW FIT` then the EAGLE `EXPORT IMAGE '<path>' <dpi>` command. Writes to
  `C:\tmp`; read the PNG from WSL at `/mnt/c/tmp` (`~/tmp` is a symlink there on
  this host).

Future tools (swaps, inventory checks, exports) should be new modules under
`src/` that take a `FusionBridge` / `Board` and follow the same loop. Anything
selection-aware reuses `selection.read_selection`.

### The write path is a deliberate workaround

As of 2026-06-30 the Fusion Electronics **object API is read-only**. Steinmetz
writes by driving the underlying **EAGLE command interpreter**: `execute` runs
Python in Fusion that calls `app.executeTextCommand('Electron.run "<CMD>;"')`.
Commands that work this way: `MOVE`, `ROTATE`, `VALUE`, `CHANGE PACKAGE`,
`ATTRIBUTE`, `EXPORT`, `script <file>`. If Autodesk ever makes the API
read/write, only `bridge.py`'s write layer changes — tools on top are unaffected.

## Conventions and gotchas

- **Flat `src/` layout.** Modules import each other by bare name
  (`from bridge import FusionBridge`), so `src/` must be on the path. Tools run as
  `python src/place.py` (the script's own dir is on `sys.path`); examples insert
  `src/` themselves. `pyproject.toml` only packages `bridge` and `scr` — `board`
  and `place` are run as scripts, not installed.
- **The bridge rules are load-bearing — don't "simplify" them away.** Each was
  learned the hard way and is documented inline in `bridge.py`/`scr.py` and in
  `docs/fusion-bridge.md`: hit the gateway IP (WSL2 can't reach loopback);
  capture `MCP-Session-Id` from the *initialize response header* and resend it;
  send `notifications/initialized` before any `tools/call`; reads silently cap at
  100 rows so always paginate (`electronics_read` does); every EAGLE command must
  end with `;` (a bare `MOVE` leaves its tool active and blocks the whole execute
  channel until someone presses Esc in Fusion); no double-quotes in EAGLE
  commands (they break the `Electron.run "…"` wrapper — EAGLE uses single quotes).
- **Writes are unsaved.** Changes apply to the live design but are not persisted
  until the user saves in Fusion; reopening reverts them. `Electron.run` returns
  no echo, so verify out-of-band by re-reading.
- **Coordinates are in the document's active unit.** Set the grid (`grid="MM"`)
  before firing coordinate commands; the read response reports `coordinate_unit`.
