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
python src/place.py               # the first tool — dry-run placement (no writes)
python src/place.py --apply       # write MOVEs back to Fusion, then re-read to verify
```

- **There is no automated test suite, linter, or CI.** "Verify" everywhere means
  re-reading the live design and checking parts landed (see `place.py`'s verify
  step). Don't look for `pytest`/`make test` — they don't exist.
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
  packages, and the board outline. The shared "what's on the board" layer that
  tools consume. The one join that matters: `ContactRef` ties an element + a net
  (`Signal`) to a pad, and the pad's placed global position lives on the
  `Smd`/`Pad` row keyed by `contact_object_id`.
- **`src/place.py` (`Placer`) — a tool.** Minimal-airwire constructive placement.
  Reads the board, anchors the biggest ICs, pulls passives to the centroid of the
  pads they connect to, legalizes overlaps, writes `MOVE`s back. Methodology and
  before/after in `docs/PLACE.md`. The template every future tool follows:
  read → compute → write back → re-read to verify.
- **`src/scr.py` — `.scr` generation/validation.** Renders EAGLE command-line
  scripts with the safety rules baked in (terminate with `;`, reject control
  chars and double-quotes).

Future tools (swaps, inventory checks, exports) should be new modules under
`src/` that take a `FusionBridge` / `Board` and follow the same loop.

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
