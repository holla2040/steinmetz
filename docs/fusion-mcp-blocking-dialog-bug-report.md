# Bug report: Fusion MCP server (Electronics) — execute channel blocked by dialogs the user cannot see

**Product:** Autodesk Fusion, built-in MCP server ("Fusion MCP Server" preference), Electronics workspace
**Build:** Current Insider build (has the new `NETROUTE` schematic command), Windows
**Client:** AI agent (Claude Code) on WSL2 driving the MCP server over streamable HTTP at `127.0.0.1:27182`, using `fusion_mcp_execute` (`featureType: "script"`, scripts calling `adsk.core` → `app.executeTextCommand('Electron.run "<EAGLE command>;"')`) and `fusion_mcp_electronics_read`
**Document:** An Electronics design ("series-shunt") with schematic and board tabs open; the session worked in both
**Date:** 2026-08-25, evening session (~3 hours)

## What we were doing

An end-to-end agent-driven ECAD session: (1) on the **schematic**, add TVS diodes and RC snubber parts and wire them (`ADD`, `NETROUTE`, `VALUE`); (2) on the **board**, place all 74 components from scratch (`GRID`, `ROTATE`, `MOVE`), resize the outline (`LAYER 20`, `DELETE`, `WIRE`), and iterate against the DRC error table read back through `electronics_read` (`electronics.Error`). The session succeeded — parts added, netlist verified, 74 components placed, placement DRC-clean — but it required roughly **eight manual interventions** where the human had to click into the Fusion canvas and press **Escape** to unblock the MCP channel, usually with **no dialog visible anywhere on screen**.

## The core problem

After most write batches, every subsequent `fusion_mcp_execute` call returns:

```
{"error": "Cannot perform 'script' while a command dialog is open. Close the active dialog and retry.", "success": false}
```

- The user looking at the Fusion window sees **no dialog**. Nothing is on screen, nothing is flashing in the taskbar.
- Pressing **Esc** in the canvas clears the state and the channel works again.
- While blocked, `fusion_mcp_electronics_read` keeps working, but `fusion_mcp_execute` with **`readOnly: true`** is also refused — contradicting the tool's own documentation that read-only scripts are allowed to run while a command dialog is open.
- The agent cannot clear the state programmatically; there is no ESC/cancel affordance in the MCP surface.

Our working theory: EAGLE-style commands that arm an interactive tool (`MOVE`, `WIRE`, `NETROUTE`, `VALUE`, `ADD`) leave that tool active even when the command is fully specified and `;`-terminated, and the MCP server's gate treats "tool armed" the same as "modal dialog open." From the UI side there is nothing to close, which is why the user can't see it — only Esc (which cancels the active tool) clears it.

## Concrete incidents

**1. Schematic — `ADD` batch refused before it ran.**
Attempting to execute a batch beginning `SET CONFIRM YES; GRID MM; ADD R-1206@holla R7 R0 (93.98 198.12); …` was refused with the dialog-open error. No dialog was visible. After clearing, the identical batch ran and all four `ADD`s placed correctly.

**2. Schematic — `SET CONFIRM YES` does not suppress the NETROUTE merge prompt.**
We ran `SET CONFIRM YES;` first, then:

```
NETROUTE N 4 D1 C D2 C R7 1 Q9 D;
```

The command succeeded (verified via `electronics.PinRef`), but the next execute was refused — and this time a dialog **was** visible:

> Merge net segment 'N$29' into net 'STRING+'?  [Yes] [No]

`SET CONFIRM YES` did not auto-answer it. Each NETROUTE that touched an existing named net queued its own merge prompt; the user had to click **Yes** on every one, one at a time.

**3. Schematic — invisible block after a fully successful batch.**
A single script ran 13 commands (5 × `NETROUTE`, 8 × `VALUE`); every `executeTextCommand` returned without exception. The next `fusion_mcp_execute` was refused. Nothing visible; Esc cleared it.

**4. Board — an error modal corrupted the commands queued behind it.**
A 140-command placement batch (`GRID MM;` then per part `ROTATE =R90 'Q9'; MOVE 'Q9' (22 84);` …, ending with `LAYER 20; DELETE (96 62); … WIRE 0 (0 0) (96 0) …`) timed out at the MCP client but kept executing server-side. Mid-batch Fusion raised an error modal:

> non-orthogonal orientation can only be used in a board or footprint

— although the **board document was active** and every `ROTATE` in the batch used `=R0/R90/R180/R270`. Worse, commands queued after that modal executed incorrectly: `MOVE 'Q10' (38 84)` landed at x = 35.3926; `MOVE 'J16' (28.3 67)` landed at (29.4674, 66.3426); `MOVE 'Q5' (54 84)` and `MOVE 'Q6' (70 84)` did not move the parts at all. A verification read (`electronics.Element`) and a repair batch were needed.

**5. Board — the block recurs after every write batch.**
Correction batches of 46, 84, 20, and 20 commands each reported per-command success, and each left the channel blocked afterward with nothing visible on screen. This happened regardless of whether the batch ended with `MOVE`, `VALUE`, `DELETE`, `WIRE`, or an inert `GRID MM;`. Each time the human pressed Esc so work could continue.

**6. `readOnly: true` refused while blocked.**
While in the blocked state, a pure read probe —

```json
{"featureType": "script", "readOnly": true,
 "object": {"script": "…print(app.activeDocument.objectType)…"}}
```

— returned the same "Cannot perform 'script' while a command dialog is open" error.

## Minimal reproduction

1. Open an Electronics design, board tab active, MCP server enabled.
2. Via `fusion_mcp_execute`, run a script that issues several commands, e.g.
   `Electron.run "GRID MM;"`, `Electron.run "ROTATE =R90 'X1';"`, `Electron.run "MOVE 'X1' (10 10);"`.
3. Call `fusion_mcp_execute` again (any script, even `readOnly: true`).
   **Result:** frequently refused with the dialog-open error while the Fusion UI shows no dialog.
4. Click into the canvas, press **Esc** → the channel works again.

NETROUTE variant: run `SET CONFIRM YES;` then any `NETROUTE` that lands on an existing named net → the "Merge net segment …?" prompt still appears and gates the channel until a human clicks Yes.

## Expected behavior

1. `SET CONFIRM YES` (or a NETROUTE flag) should suppress the net-merge confirmation so scripted schematic wiring can run unattended.
2. An armed interactive tool left behind by a `;`-terminated command should not gate the MCP execute channel as a "command dialog" — or the MCP surface should provide a programmatic ESC/cancel so the agent can clear it.
3. Any state that does gate the channel must be visible and focused in the UI; tonight's blocking state showed nothing, leaving the user unable to tell why automation stopped.
4. `readOnly: true` scripts should run while a dialog is open, as documented.
5. An error modal raised mid-sequence must not corrupt the coordinates or execution of subsequent `executeTextCommand` calls.

## Impact

Agent-driven electronics automation over the MCP server currently cannot run unattended: every write batch risks a silent, invisible block that only a human keypress can clear. In a session that placed 74 components and reached a DRC-clean placement, ~8 such interventions were needed, and one error modal silently corrupted a placement batch, requiring a read-back-and-repair pass. Workarounds we settled on — batching all writes into one script call, verifying every write out-of-band through `electronics_read`, and treating any execute refusal as "ask the human to press Esc" — work, but defeat the purpose of an autonomous MCP write path.
