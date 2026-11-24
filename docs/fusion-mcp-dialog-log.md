# Fusion MCP: log of dialogs and blocked states that needed a human

A running record of every time work driven through Fusion's MCP server stopped
until a person clicked or pressed a key in Fusion. It covers all three projects
that use the bridge (steinmetz, hendley, ThomsonLint). The purpose is evidence
for the Fusion Insider group: how often it happens, on which build, after which
command, and what cleared it.

The original report, with six incidents from 2026-08-25, is
[`fusion-mcp-blocking-dialog-bug-report.md`](fusion-mcp-blocking-dialog-bug-report.md).
This file continues from there.

## What counts

Log it when any of these happens:

- `fusion_mcp_execute` is refused with "Cannot perform 'script' while a command
  dialog is open", whether or not a dialog is visible.
- A visible dialog or prompt appears that the script cannot answer (confirm,
  overwrite, merge, error modal).
- The bridge stops answering, or reads come back empty, until someone acts in
  Fusion or the MCP server is toggled.

Also log sessions where it did not happen, as one summary row, so the rate can
be worked out.

## How to add an entry

Add one row per incident, newest last. Record what was observed, not a guess at
the cause. If a field is unknown, write "unknown".

| Field | What to write |
|---|---|
| Date/time | Local time of the incident |
| Fusion build | `app.version` as reported over the bridge |
| Project / tool | Which repo and script |
| Write path | `executeCommands` (command API) or `Electron.run` (legacy), or "read only" |
| Editor | Schematic, board or library tab |
| Last command(s) | The exact command or batch sent just before the block |
| Symptom | The exact error text, or what stopped responding |
| Visible? | Whether anything was visible in Fusion, and its text if so |
| Cleared by | Esc, a button click, toggling the MCP server, restarting Fusion |
| Time lost | Roughly how long work was stopped |

## Incidents

| Date/time | Fusion build | Project / tool | Write path | Editor | Last command(s) | Symptom | Visible? | Cleared by | Time lost |
|---|---|---|---|---|---|---|---|---|---|
| 2026-11-24, first run after a Windows restart | 2707.1.13 | steinmetz `steinmetz validate --only R9 R13` had just finished (ALL PASS); the block was found by the next script call | `executeCommands` for the earlier steps; `validate` itself is read only | Board (`temp brd`) | In order, each preceded by a script that activates `temp brd`: `steinmetz read`, `run-command` (`WINDOW FIT;`), `screenshot` twice (`WINDOW FIT;`, `EXPORT IMAGE 'C:/tmp/steinmetz_view.png' 150;`), `validate --only R9 R13`. All five exited 0. | Every following `fusion_mcp_execute` refused: "Cannot perform 'script' while a command dialog is open. Close the active dialog and retry." `electronics_read` kept working (ThomsonLint's board export still succeeded). hendley's `EDIT .S1;` and ThomsonLint's schematic and image exports were refused. | Yes: the owner reported an "Unroute" dialog open in Fusion. The owner did nothing in Fusion to open it; it appeared on its own. Which bridge call opened it is not yet isolated. | The owner closed the dialog by hand | A few minutes; three tools' runs failed and had to be repeated |

## Sessions with no incident

| Date | Fusion build | Project | Write path | What ran | Blocks |
|---|---|---|---|---|---|
| 2026-11-24 | 2707.1.13 | steinmetz, native Windows | `executeCommands` | About seven execute batches on the board tab of a scratch design: two `place.py` runs (`MOVE`, `ROTATE`), one test `MOVE`, three `WINDOW FIT`, two `EXPORT IMAGE` (the second over an existing file). Three of the commands were refused by live DRC with a status and message, which is a normal result, not a block. | 0 |
| 2026-11-24 | 2707.1.13 | steinmetz, native Windows | `executeCommands`, plus two ULP `RUN`s through `Electron.run` | 29 execute calls with the board tab of a scratch design active: 9 guard scripts, 5 `WINDOW FIT`, 4 `EXPORT IMAGE` (three over an existing file), `RUN` of the selection ULP and of the grid ULP, the rest probes and reads. No `MOVE` or `ROTATE`. | 0 |
| 2026-11-24 | 2707.1.13 | ThomsonLint, native Windows | exporter's own (not counted per call) | `fusion_bridge.py ping`, then `fusion_export.py` `sch`, `brd` and `images` on a scratch design: schematic and board reads with tab switches, one schematic image and six board layer images (`EXPORT IMAGE`, layer `DISPLAY` changes). | 0 |
| 2026-11-24 | 2707.1.13 | hendley, native Windows | read only, plus the board-context switch | `hendley pcba --no-verify` on a scratch design: 33 parts read from the schematic, switch to the board, 33 placements read. A script call straight afterwards was accepted. | 0 |
| 2026-11-24, after the incident above | 2707.1.13 | steinmetz, ThomsonLint, hendley, native Windows | `executeCommands` and read only | Attempt to reproduce: the same steinmetz steps one at a time (`validate`, `read`, `run-command`, `screenshot`, `validate`) with a script probe after each, then ThomsonLint `sch`, `brd`, `images` and hendley `pcba --no-verify`. The probe reported `ui.activeCommand` as empty at first and `Electron::Group` after `WINDOW FIT;`, and the channel stayed open throughout. | 0 (the Unroute dialog did not come back) |
| 2026-11-24 15:22 | 2707.1.13 | steinmetz bridge, native Windows, schematic write test | `executeCommands` | Schematic tab of a scratch design, six batches with a script probe after each: `ADD R-0603@holla 'R90' R0 (243.84 190.50);`, a second `ADD` ('R91'), `VALUE` x2, `NETROUTE N 2 R90 1 R91 1;` (two unconnected pins), `NETROUTE N 2 R90 2 R1 2;` (pin to named net 5V0), `SET CONFIRM YES; NETROUTE N 2 R91 1 R2 1;` (a true merge of `N$1` into GND). Every readback matched. `ui.activeCommand` was empty after every batch. | 0 |
| 2026-11-24 15:22 | 2707.1.13 | same test, parts R92/R93 | `Electron.run` (legacy, forced) | The same six batches. Every readback matched, including the merge of `N$4` into GND with no prompt. | 0 |

## Notes for the Insider report

- **The August blocks did not recur on 2707.1.13 for the same commands.** In
  the 2026-08-25 session nearly every schematic or board write batch left the
  channel refused until Esc. On 2026-11-24, twelve schematic write batches
  (`ADD`, `VALUE`, `NETROUTE`, on both write paths) and the board `MOVE` /
  `ROTATE` batches left it open every time. This is one short session on one
  scratch design, not proof of a fix.
- **A merge reports failure although it worked.** Through `executeCommands`,
  `NETROUTE N 2 R91 1 R2 1;` (merging `N$1` into GND, after `SET CONFIRM YES;`
  in the same batch) returned status 2 with the message "Can't place junction
  on junction!", yet the readback showed both pins on GND. A caller that
  trusts the status would treat a successful merge as failed.

- The 2026-11-24 Unroute dialog appeared during the first run after a Windows
  restart and a fresh Fusion start, with no unroute command sent and nobody
  touching Fusion. Repeating the same steps minutes later did not bring it
  back, so the trigger is not known.

- The 2026-08-25 incidents were all on the `Electron.run` path. The 2026-11-24
  session used `executeCommands` and saw no block, but it was short and
  board-only. That is too little to say the problem is fixed on the command
  API; schematic commands (`ADD`, `NETROUTE`, `VALUE`) have not been re-run.
