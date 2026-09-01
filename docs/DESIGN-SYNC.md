# Design sync — propagating gate-driver changes from Design A into B and C

**Status (2026-09-01): planning + spike done, nothing built yet.** Branch
`feature/design-sync`. This document is the hand-off for whoever builds the
tool: what the owner wants, why, what was verified against live Fusion, the
proposed architecture, and the traps. Read `CLAUDE.md` and
`docs/fusion-bridge.md` first — this builds on them and repeats nothing that
lives there.

---

## 1. The problem

The owner is building a **series-shunt MOSFET switch**: one gate-driver circuit
(two isolated gate-driver ICs `U_SERIES` / `U_SHUNT` — UCC5320SCDWVR — two
isolated supplies `PS_SERIES` / `PS_SHUNT`, gate resistors, bypass caps, TVS
diodes, connectors, test points) driving power MOSFETs. Three MOSFET package
families are candidates, and their pin layouts are incompatible, so a shared
board layout is impossible. Fusion *variants* can't help (variants share one
layout). Fusion *modules* can't help either: a module is embedded in the
schematic that contains it, and duplicating a design duplicates the module
with no link back. There is no cross-design module reuse in Fusion Electronics.

So there will be **three separate designs** — A, B, C — identical except for
the MOSFET package. The owner wants to edit the shared gate-driver circuit in
**one place (A)** and have an agent propagate those edits into B and C.

## 2. What the owner actually wants (verbatim intent, 2026-09-01)

> Get schematic A finalized enough to get to board layout, then duplicate that
> to create B and duplicate it for C and have those boards made. If there's an
> issue and I have to remake the boards, I'd like to make the schematic changes
> to A and have you go in and find what's new in that board layout compared to
> B and C, and duplicate those changes from A into B and C.

Key clarifications from the same conversation:

- **The open design (`series-shunt`) is the combo scratch design, not A.** It
  holds all twelve MOSFETs from all three families, wired in parallel, so the
  owner can evaluate them. It "has issues and that's what we're trying to
  determine."
- **Each real design gets four MOSFETs**, not twelve. One family per design.
- **All editing in B and C is done by the owner**, who understands the
  ramifications. The tool never has to defend against unknown edits in B/C —
  but it should still *detect and report* drift (see §6).
- **Traces that reach the MOSFET area are expected to be manual** in all three
  designs. The owner's mental model: "if we add something to schematic A we may
  end up having to route it manually in A, B, and C." The tool can do better
  than that inside the gate-driver area (see §7), but the MOSFET zone is
  hand-work by contract.

## 3. Facts about the combo design (read live, 2026-09-01)

Read over the bridge from the open `series-shunt` design. Verify against the
live design before relying on any of it — the combo is actively being edited.

- **Documents.** One Electronics design open, appearing as four Fusion
  documents all named `series-shunt`: `EcadDesignDocument` (.fprj),
  `SchematicDocument` (.fsch), `BoardDocument` (.fbrd), `FusionDocument`
  (.f3d). Hub project `solohm`, folder `series-shunt`. No other design named
  like it exists in the hub — **B and C have not been created**.
- **Schematic.** One sheet. No modules, no module instances. 70 parts
  including supply symbols.
- **MOSFETs — three families, four each:**

  | refs | value | package (per owner) |
  |------|-------|---------------------|
  | Q1–Q4 | PSMN1R4-100CSEJ | Nexperia CCPAK1212I, SOT8005A, 12-pin |
  | Q5–Q8 | PSMN1R0-100CSFJ | Nexperia CCPAK1212, SOT8000A, 13-pin |
  | Q9–Q12 | IPT017N12NM6 | Infineon PG-HSOF-8 |

  Working assumption: **A = Q1–Q4, B = Q5–Q8, C = Q9–Q12.** The original
  Autodesk-Assistant write-up put both Nexperia families in A; the owner's
  "four per design" correction supersedes it. **Confirm the mapping with the
  owner before forking.**
- **Gate nets are auto-named.** Two gate nets exist: `N$3` (gates of Q1, Q3,
  Q7, Q8, Q11, Q12) and `N$19` (gates of Q2, Q4, Q5, Q6, Q9, Q10). Within each
  family two MOSFETs sit on each gate net — that's the series/shunt pairing.
  Auto-names renumber when nets are redrawn, which would break a name-keyed
  diff. **Recommend naming them (e.g. `GATE_SERIES` / `GATE_SHUNT`) in A before
  forking.** Which is series and which is shunt was not established.
- **Other nets that cross into the MOSFET area** (the "boundary nets"):
  `PANEL+`, `STRING+`, `STRING-`, plus `N$15` (Q7 source pins). The full net
  list: `3V3 15V0-SERIES 15V0-SHUNT 24V0 GND N$3 N$6 N$7 N$8 N$9 N$15 N$19 N$29
  N$32 PANEL+ SERIES_ENABLE SHUNT_ENABLE STRING+ STRING-`.
- **MOSFET pin interfaces differ.** CCPAK parts expose `G`, `D1..D6/D7`,
  `S1..S5/S7`; the Infineon part exposes `G`, `D`, `S`. A boundary net that
  attaches to "the drain" attaches to one pin in C and six or seven in A/B.
  This is why the per-design config carries a **pin map** (§6).

## 4. What was verified against live Fusion (the spike)

Run 2026-09-01 through the MCP tools directly (`fusion_mcp_electronics_read`,
`fusion_mcp_execute`, `fusion_mcp_read`), EAGLE engine reports itself as
**9.7.0**.

| question | result |
|----------|--------|
| Can the agent switch between a design's schematic and board documents? | **Yes.** `Document.activate()` on the `BoardDocument` made `electronics.Element` reads return board rows; activating the `SchematicDocument` brought `Instance`/`Wire`/`Net` reads back. `app.activeProduct.productType` reports `SchematicProductType` / `BoardProductType`. Recipe in `docs/fusion-bridge.md` §"Switching document tabs". |
| Can it switch between *different designs* (A ↔ B)? | **Untested — needs B to exist.** Same API (`app.documents` lists every open document; each design contributes its four). `fusion_mcp_read {queryType:"document", operation:"open"}` lists them with hub ids; `fusion_mcp_execute {featureType:"document", object:{operation:"open", fileId}}` can open one by id. Test this first when B exists. |
| Does `EXPORT SCRIPT` (dump the drawing as a recreatable script) work? | **No.** In both schematic and board editors Fusion shows *"The export option can only be used in a library"*. Over the bridge it is **invisible**: `Electron.run` returns `''`, no file appears, and the execute channel was *not* blocked afterward. Don't plan on it. Generate commands from the object model instead. |
| Does `EXPORT NETSCRIPT` work? | **Yes**, from the schematic: `Electron.run "EXPORT NETSCRIPT 'C:/tmp/x.scr';"` wrote a 284-line pin-level netlist (`Signal 'NET' 'REF' 'PIN' \` continuation lines, `Change Class n;` per net). Useful as a connectivity-verification artifact: export from A and from the target, diff. |
| Does the schematic object model expose enough to regenerate a circuit? | **Yes.** `Instance` has `x, y, angle, mirror, sheet, part_object_id, gate_object_id`; `Wire` has `x1,y1,x2,y2, layer (91 = Nets), width, segment_object_id`; `Net` has `name, net_class_number`; `Segment` links to its net; `PinRef` ties `net_object_id` + `segment_object_id` to `instance_object_id` + `pin_object_id`. Field names differ from the EAGLE API (`angle`, not `rotation`) — read `resource://mcp.electronics_schema_<Class>` when unsure. |

Probe artifacts are in `C:\tmp` (`~/tmp` from WSL) with prefix
`steinmetz_probe_`; safe to delete.

## 5. The workflow the tool supports

```
Phase 0  (now)      combo design → owner finalizes A (schematic + board)
                    prep: name gate nets, confirm Q mapping, define MOSFET zone
Phase 1  (fork)     sync snapshot A  → sync/baselines/<date>-A.json  (commit it)
                    owner duplicates A → B, C in Fusion; swaps Q package; hand-routes Q zone
                    sync snapshot B, C → baselines too (proves the copies match A outside the zone)
Phase 2  (respin)   owner edits A
                    sync diff   A-now vs baseline      → change set (what to propagate)
                    sync diff   baseline vs B-now      → drift report (should be empty outside Q zone)
                    sync apply  change set → B         (schematic deltas, then board deltas)
                    sync verify B                      (re-read, netscript diff, DRC, screenshot)
                    repeat for C
                    owner reviews, saves in Fusion, re-snapshots → new baseline
```

The tool is a **three-way change propagator**, not a full replicator. The
baseline is what lets "changed in A since the fork" be separated from "always
different in B because it's B."

## 6. Scope: what is synced, per design

A per-target config (`sync/<design>.json`, one for B and one for C):

```json
{
  "design": "B",
  "hub_file_id": "urn:adsk.wipprod:dm.lineage:...",
  "mosfet_refs": ["Q5", "Q6", "Q7", "Q8"],
  "mosfet_zone_mm": [[x1, y1], [x2, y2]],
  "boundary_pin_map": {
    "GATE_SERIES": {"Q5": ["G"], "Q7": ["G"]},
    "PANEL+":      {"Q5": ["D1","D2","D3","D4","D5","D6","D7"], "Q6": ["..."]}
  }
}
```

Scope rules, in order:

1. **Parts:** everything whose ref is *not* in `mosfet_refs` is in scope.
2. **Nets:** a net is *excluded* only if every pin on it belongs to a MOSFET
   ref. A net touching one gate-driver pin is in scope (these are the boundary
   nets). Key nets by name, with a **pin-membership fingerprint** (sorted
   `ref.pin` set, MOSFET pins removed) as the fallback for `N$` names.
3. **Schematic wires:** in scope if their segment belongs to an in-scope net
   and neither endpoint lands on a MOSFET pin. Wires ending on a MOSFET pin are
   the boundary stubs — regenerated per design via `NETROUTE` and the pin map,
   never copied geometrically.
4. **Board elements:** same ref rule as parts.
5. **Board copper (wires, vias, polygons):** in scope if on an in-scope signal
   *and* no endpoint inside `mosfet_zone_mm` *and* not ending on a MOSFET
   pad. Everything else in the zone is hand-work by contract.
6. **Board outline (layer 20), DRC settings, design rules:** out of scope;
   must stay identical across A/B/C by owner discipline. Report a diff, don't
   apply it.

## 7. Diff categories and the EAGLE commands that apply them

All writes go through `scr.py` (`render_scr` / `write_scr`, which enforce the
`;` terminator and reject double quotes) and `FusionBridge.run_scr`, with
`GRID MM;` first so coordinates are unambiguous. **Schematic first, board
second** — Fusion keeps the two consistent, so an `ADD` in B's schematic
creates the unplaced element in B's board and a schematic `DELETE` removes it.
Never add or delete parts in a board directly. Both documents of the target
design must be open (they are, by default).

### Schematic

| change in A | command in target |
|-------------|-------------------|
| part added | `ADD <device>@<lib> 'REF' R<rot> (x y);` — **quote the name** (`docs/fusion-bridge.md`: an unquoted `R99` parses as rotation 99°). Resolve device/library names from `DeviceSet`/`Device`/`Library` reads by object id. Same libraries exist in B/C because they're copies. |
| part removed | `DELETE (x y);` at the instance origin (name-based `DELETE REF` is unverified — check it; fall back to coordinates). Deleting also removes the board element. |
| value changed | `VALUE 'REF' '<val>';` |
| package/device variant changed | `CHANGE PACKAGE 'REF' '<pkg>';` (a.k.a. `PACKAGE`) — verify syntax on this build before relying on it. |
| attribute changed | `ATTRIBUTE 'REF' '<NAME>' '<value>';` |
| instance moved / rotated / mirrored | `MOVE 'REF' (x y);` `ROTATE =R<deg> 'REF';` `MIRROR 'REF';` |
| pin joined a net | `SET CONFIRM YES;NETROUTE N 2 REF PIN REF2 PIN2;` in the **same** `Electron.run` string (merge-prompt workaround; see bridge doc). For boundary nets, REF2/PIN2 come from the pin map. |
| pin left a net | delete the wire segment(s) reaching that pin: `DELETE (x y);` on the wire, coordinates from the baseline snapshot. Fragile — verify by re-reading `PinRef`. |
| wire geometry changed (cosmetic re-route) | rip the in-scope segments of that net (`DELETE` by coordinate, or `GROUP` polygon + `DELETE (> 0 0);`), then `NET '<name>' (x1 y1) (x2 y2);` per segment. Optional — connectivity, not geometry, is what matters electrically. Make it a flag. |
| net renamed | `NAME '<old>' '<new>';` — only when the fingerprint matches and the name differs. |

### Board

| change in A | command in target |
|-------------|-------------------|
| element moved / rotated / side changed | `MOVE 'REF' (x y);` `ROTATE =R<deg> 'REF';` `MIRROR 'REF';` — exactly what `place.py` already does. |
| new element (from a schematic add) | appears unplaced near the origin after the schematic step; then MOVE/ROTATE as above. |
| trace segment added | `LAYER <n>; WIRE '<SIG>' <width> (x1 y1) (x2 y2);` — coordinates carry over verbatim because the gate-driver pads are identical in all three boards. |
| trace segment removed / changed | `GROUP (x1 y1) (x2 y1) (x2 y2) (x1 y2); RIPUP (> 0 0);` or `DELETE (> 0 0);` scoped to the gate-driver area (the complement of the MOSFET zone), then redraw. Never `RIPUP <SIG>;` bare — that rips the hand-routed zone too. |
| via added / removed | `VIA '<SIG>' <diam> <shape> <layers> (x y);` / coordinate `DELETE`. |
| polygon changed | `POLYGON '<SIG>' <width> (pts…);` then `RATSNEST;` to pour. |
| text / silkscreen | `TEXT '<str>' R<rot> (x y);` on the right `LAYER`. Low priority. |

Expect to *discover* syntax quirks for `WIRE '<SIG>'`, `VIA`, `GROUP`+`(> x y)`
on this build the same way `ADD` and `NETROUTE` quirks were discovered:
one command at a time, re-read after each, and record every finding in
`docs/fusion-bridge.md`.

## 8. Verification (non-negotiable, per the repo's contract)

After every apply, before reporting success:

1. **Re-read the target** (`Part`, `Instance`, `PinRef`, `Element`, board
   `Wire`/`Via`) and compare against the *expected* post-apply state computed
   from the change set. Report every mismatch by ref/net.
2. **Netlist diff:** `EXPORT NETSCRIPT` from A and from the target; strip
   excluded refs and their exclusive nets; the remainder must be identical.
3. **DRC/ERC:** read `electronics.Error` before and after; new errors are
   findings.
4. **Screenshot** the board (`src/screenshot.py`) for the owner's eyes.
5. **List what was left for hand-routing:** every in-scope net that has a
   boundary pin in the target and whose zone-side copper the tool did not
   touch.

Success messages from `Electron.run` prove nothing — it returns `''` always.

## 9. Hazards specific to this tool

- **Hidden dialogs block the execute channel** after `ADD`, `NETROUTE`,
  `DELETE`, `MOVE`, `WIRE` — `Cannot perform 'script' while a command dialog is
  open`. Two mechanisms (minimized error dialog with a red X bottom-right of
  the canvas; or an armed-tool state with nothing visible). A human clicks the
  canvas and presses **Esc**. Batch writes into one `.scr`, expect to ask the
  owner to press Esc between batches, and design `apply` to be **resumable**
  (idempotent commands, progress checkpoint). Full write-up:
  `docs/fusion-mcp-blocking-dialog-bug-report.md`.
- **Errors are invisible over the bridge** (`EXPORT SCRIPT` proved it again).
  A `.scr` stops at its first failing command with no signal. Verify after
  every batch, and keep batches small enough that a failure is locatable.
- **Reads follow the active document.** Every read in `snapshot` must activate
  the right document first, and `apply` must activate the *target's* schematic
  vs board before each batch. With two designs open there are eight documents
  named similarly — select by `dataFile.id`, never by name.
- **Coordinates:** set `GRID MM;` before writing; reads report
  `coordinate_unit`. Snapshot in mm always.
- **Writes are unsaved** until the owner saves in Fusion. That is the rollback:
  close without saving. Never call `save` from the tool.
- **`N$` auto-names** drift. Fingerprint nets (§6).
- **Reads cap at 100 rows;** `FusionBridge.electronics_read` paginates, the raw
  MCP tool does not.
- **Both Nexperia families share the value prefix** `PSMN1R…`; don't scope by
  value pattern, scope by ref list from the config.

## 10. Proposed implementation

Fits the repo's three-layer pattern (transport → model → tool):

```
src/schematic.py      read_schematic(bridge) -> Schematic   (sibling of board.py:
                      parts, instances, nets w/ fingerprints, pinrefs, wires, attrs)
src/board.py          extend read_board with signals' wires/vias/polygons (currently
                      elements + pads only)
src/sync.py           the tool:  snapshot | diff | apply | verify
                        snapshot --design A  -> sync/baselines/<date>-A.json
                        diff     --from baseline --to live|snapshot  -> change set (JSON + human summary)
                        apply    --target B --changes <file> [--dry-run] [--schematic-only|--board-only]
                        verify   --target B --changes <file>
sync/<design>.json    per-target config (§6)
sync/baselines/       committed snapshots
docs/DESIGN-SYNC.md   this file; append an engineering log like docs/PLACE.md's
```

Build order, each step verifiable on its own:

1. **`snapshot`** — read-only. Can be built and tested **today** against the
   combo design; nothing to write. Includes the document-activation dance and
   the resolution of object ids to names (device, library, pin, package).
2. **`diff`** — pure Python over two snapshots. Test by snapshotting, moving a
   part by hand in Fusion, snapshotting again.
3. **`apply` schematic half, `--dry-run` first** — emits the `.scr` and the
   expected post-state without running. Test against a *duplicate of the
   combo* (owner makes it), never against A.
4. **`apply` board half** — same discipline. Start with element moves (known to
   work), then `WIRE`/`VIA`, then rip-up.
5. **`verify`** — §8. Then wire `apply` to call it automatically.
6. **Cross-design activation test** the moment B exists (§4, row 2). If
   `activate()` across designs misbehaves, fall back to "owner switches the
   active design by hand; tool asserts the active `dataFile.id` before every
   batch."

`make sync-snapshot` / `make sync-diff` aliases in the `Makefile` once they
exist, per repo convention (flags via the script directly).

## 11. Open questions for the owner

1. Confirm A/B/C ↔ Q1–Q4 / Q5–Q8 / Q9–Q12.
2. Name the gate nets before forking? (Recommended.)
3. Where is the MOSFET zone on the board, and is it the same rectangle in all
   three designs? (One zone or two — series and shunt groups may be apart.)
4. Are cosmetic schematic wire re-routes worth propagating, or is connectivity
   enough? (Affects the "wire geometry changed" row in §7.)
5. Are polygons/pours and silkscreen text in scope for the board half?

## 12. Session log — 2026-09-01

- Owner explained the three-board plan and the Autodesk-Assistant write-up;
  agent assessed feasibility (§1–§2), flagged that the sync is a diff, not a
  replay, and that the boundary nets need per-design pin maps.
- Live spike against `series-shunt` (§4): document enumeration, schematic ↔
  board activation with reads following, `EXPORT SCRIPT` failure (error text
  seen by the owner in the Fusion UI, invisible over the bridge),
  `EXPORT NETSCRIPT` success, schematic entity field survey, netlist analysis
  of which nets touch the MOSFETs.
- Owner corrected the design facts: the open design is the combo, each real
  design has four MOSFETs, all B/C editing is the owner's.
- Feasibility verdict given: schematic and placement sync high-confidence;
  gate-driver-area copper likely copies verbatim; MOSFET-zone copper manual by
  contract; hidden dialogs remain a human-in-the-loop step.
- Branch `feature/design-sync` created from `main` (`fb06584`). This document
  written. Nothing else changed.
