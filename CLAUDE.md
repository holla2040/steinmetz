# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Steinmetz drives a **live** Autodesk Fusion Electronics design over Fusion's
local HTTP (JSON-RPC) bridge — read the board, compute something, write changes
back, verify by re-reading. No plugin, no MCP client library. See `README.md`
for the project ethos and `docs/fusion-bridge.md` for the protocol reference.

## Setup, build, run

Runs natively on Windows (PowerShell) and macOS, Python 3.10+, on the same
machine as Fusion. The last WSL-only version is the `wsl-final` tag.

```
python -m venv .venv
.venv\Scripts\activate            # macOS: source .venv/bin/activate
pip install -e .                  # requests, numpy + the `steinmetz` command

steinmetz read                    # connect + summarize the open design
steinmetz run-command             # prove the write path (harmless WINDOW FIT)
steinmetz selection               # show the current GROUP-selected parts
steinmetz place                   # place the selected parts (writes MOVEs, then verifies)
steinmetz screenshot              # snapshot the board to the Fusion scratch dir
steinmetz validate                # numpy engine vs scalar spec (read-only)
```

`steinmetz` (`src/steinmetz_cli.py`) passes everything after the task to the
tool (`steinmetz place --only U1 --rotate 45`). The scripts still run directly
(`python src/place.py --only U1`, `python examples/read_design.py`). The
`Makefile` keeps the same tasks as Unix-shell aliases.

- **There is no automated test suite, linter, or CI.** "Verify" everywhere means
  re-reading the live design and checking parts landed (see `place.py`'s verify
  step). The `Makefile` only aliases the run commands — there's no `make test`
  or `pytest`; don't look for them.
- **Nothing runs without a live Fusion session.** Every example and tool requires
  Fusion with an Electronics document open and the "Fusion MCP Server"
  preference enabled; Fusion listens on `127.0.0.1:27182`.
- Host/port can be overridden with `STEINMETZ_FUSION_HOST` / `STEINMETZ_FUSION_PORT`;
  otherwise `bridge.default_host` probes loopback and, if nothing answers,
  raises a clear error (on Linux/WSL it falls back to the gateway instead).
- **Fusion-side scratch dir** (ULPs, selection/grid result files, screenshots):
  `bridge.fusion_scratch_dir()` — `C:\tmp` on Windows and WSL, `/tmp` on
  macOS, `STEINMETZ_FUSION_TMP` overrides. `bridge.local_path` maps it for
  this Python (identity natively, `/mnt/c/...` under WSL).

### Running under WSL2 (legacy)

WSL still works as a fallback with Fusion on Windows. NAT-mode WSL needs a
`netsh` port-forward to the WSL gateway IP; mirrored-mode WSL shares the
Windows loopback and needs no forward. Full setup (including the critical
`0.0.0.0` gotcha) is in `docs/fusion-bridge.md` → "Running under WSL2
(legacy)". Scratch files in `C:\tmp` are read at `/mnt/c/tmp`. If a tool can't
connect under WSL, suspect the environment, not the Python: run
`~/claude-code-fusion-mcp-check/check.sh`
([holla2040/claude-code-fusion-mcp-check](git@github.com:holla2040/claude-code-fusion-mcp-check.git))
— it diagnoses the link layer by layer and prints the exact fix. Report what
it says; don't apply ad-hoc network fixes from this repo.

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
  every run via `Board.grid`, or failing that by writing/running
  `steinmetz_grid_main.ulp` in the Fusion scratch dir, which reads
  `B.grid.distance` and `B.grid.unitdist`), writes
  `ROTATE`/`MOVE`s back, then re-reads to verify pad positions. Selection comes
  from `selection.py`;
  `--only REF...` overrides it. Nothing selected → nothing placed. Methodology
  and before/after in `docs/PLACE.md`. The template every future tool follows:
  read → compute → write back → re-read to verify.
- **`src/placevec.py` (`VecEngine`) — the placer's numpy engine.** The
  optimization loops (per-part ring search, spread, settle, legalize, quench)
  run here, batched over candidate blocks (batched Prim per net, vectorized
  separating-axis legality, bound-pruned crossing counts, exact dirty-part
  skipping). `Placer` keeps the scalar per-candidate scoring/legality methods
  as the readable spec; the engine must agree with them — change semantics in
  both places or not at all, and check with `make validate`
  (`scripts/validate_placevec.py`, read-only against the live board). ~seconds
  instead of minutes for a 23-part selection; even `--rotate 1` (360
  orientations) is tractable. Methodology, engineering log, and results in
  `docs/PLACE-VECTOR.md`.
- **`src/scr.py` — `.scr` generation/validation.** Renders EAGLE command-line
  scripts with the safety rules baked in (terminate with `;`, reject control
  chars and double-quotes).
- **`src/selection.py` (`read_selection`) — selection utility.** The shared
  "what's selected" layer, fully bridge-driven. `execute()` runs *in-process*
  (same pid/MainThread), but `ui.activeSelections` still reads `0` — it's the
  Fusion *design* selection collection, never wired to the Electronics editor
  (`Element` has no selected flag; `selectionSets` throws). The Electronics
  selection lives in the embedded EAGLE engine, which the bridge *can* drive: so
  `read_selection` (one `execute`) writes a tiny ULP to the
  Fusion scratch dir, runs it via `Electron.run "RUN '…'"`, and the ULP's `ingroup(E)`
  walks the board writing every grouped ref to
  `steinmetz_selection.txt` there, which the same call reads back. **Gesture:
  parts must be GROUP-selected (rubber-band Group tool), not click-selected.**
  `place.py` uses this as its default input; `--only` overrides with an explicit
  ref list. `scripts/capture_selection.py` is an obsolete manual fallback (the
  only way to capture a plain click-selection, run *inside* Fusion).
- **`src/screenshot.py` (`capture`) — visual verification.** Snapshots the board
  to a PNG for a look (the place → screenshot verify loop). `app.activeViewport`
  is `None` in the PCB Editor, so `saveAsImageFile` can't be used — it fires
  `WINDOW FIT` then the EAGLE `EXPORT IMAGE '<path>' <dpi>` command. Writes
  `steinmetz_view.png` to the Fusion scratch dir (same path locally; under WSL
  read it at `/mnt/c/tmp`).

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

**Update 2026-10-30 (Fusion Insider 2707.0.73, preview API):** that has started.
`EcadDrawing.executeCommands` runs a batch of EAGLE commands and returns a
status + messages per command, and most `adsk.electron` objects now have
setters (`Element.x/angle/locked`, `Part.value`, `Net.name`, `Grid.distance`, …).
`run_eagle_batch` now uses `execute_commands` when `has_command_api()` is true
and falls back to `Electron.run` otherwise (`STEINMETZ_LEGACY_WRITE=1` forces
the fallback); `place._read_main_grid_mm` reads `Board.grid` before falling
back to the grid ULP. Selection capture still needs the `ingroup()` ULP — the
API has `Schematic.selectObjects` but no way to read the current group. Details
in `docs/fusion-bridge.md` → "The command API (Insider builds)".

## Conventions and gotchas

- **Flat `src/` layout.** Modules import each other by bare name
  (`from bridge import FusionBridge`), so `src/` must be on the path. Tools run as
  `python src/place.py` (the script's own dir is on `sys.path`); examples insert
  `src/` themselves. `pyproject.toml` lists every `src/` module the `steinmetz`
  command reaches as a top-level `py-module` — add new ones there too.
- **The bridge rules are load-bearing — don't "simplify" them away.** Each was
  learned the hard way and is documented inline in `bridge.py`/`scr.py` and in
  `docs/fusion-bridge.md`: hit loopback natively, the gateway IP under NAT-mode
  WSL2 (which can't reach loopback); send a loopback `Host` header (Fusion's MCP server 403s "Invalid Host header"
  on anything but localhost);
  capture `MCP-Session-Id` from the *initialize response header* and resend it;
  send `notifications/initialized` before any `tools/call`; reads silently cap at
  100 rows so always paginate (`electronics_read` does); every EAGLE command must
  end with `;` (a bare `MOVE` leaves its tool active and blocks the whole execute
  channel until someone presses Esc in Fusion); no double-quotes in EAGLE
  commands (they break the `Electron.run "…"` wrapper — EAGLE uses single quotes).
- **Reads and commands follow the active document tab.** Schematic entities need
  the Schematic tab active, board entities the Board tab; the wrong tab returns
  empty items, not an error. Board tools call `FusionBridge.ensure_board()`
  (via `read_board`, `read_selection`, `screenshot.capture`), which probes
  `electronics.Board` and fires `EDIT .brd;` only when it reads empty — never
  a no-op `EDIT`, which arms Fusion's invisible dialog latch (same pattern as
  Hendley/ThomsonLint). Otherwise switch programmatically with `Document.activate()`
  via `execute`, and switch before firing editor-specific EAGLE commands. See
  "Switching document tabs" in `docs/fusion-bridge.md`.
- **Board MOVEs need Ignore Violators (Insider builds).** Live DRC refuses
  them (status 2, 'Switch to "Ignore Violator"'), and the mode resets on every
  Fusion restart. Turn it on from a script with the `IgnoreViolatorsCommand`
  command definition; toggle-off behavior is unverified. See "The command API
  (Insider builds)" in `docs/fusion-bridge.md`.
- **Schematic auto-routing: `NETROUTE` (Insider builds).** Connect pins by
  ref+pin name (`NETROUTE N 2 D1 C Q9 D;`) instead of drawing coordinate `NET`
  wires; run `SET CONFIRM YES;` first so prompts don't block the channel.
  Syntax and examples in `docs/fusion-bridge.md`.
- **Layout review reference: `~/ThomsonLint`.** A hardware-design-review
  knowledge base (hot loops, decoupling, snubbers, grounding) plus EAGLE ULP
  exporters (`tools/fusion-electronics-*.ulp`) that dump full schematic/board
  connectivity JSON and high-res images — run them via the bridge from the
  matching editor tab (copy to the Fusion scratch dir, output lands in `<ulp-dir>/../exports`).
- **Writes are unsaved.** Changes apply to the live design but are not persisted
  until the user saves in Fusion; reopening reverts them. `Electron.run` returns
  no echo, so verify out-of-band by re-reading.
- **Coordinates are in the document's active unit.** Set the grid (`grid="MM"`)
  before firing coordinate commands; the read response reports `coordinate_unit`.
