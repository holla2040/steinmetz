# Handoff — placement-algorithm rewrite for `src/place.py`

Status as of 2026-07-01, updated after the latest live-board run. Branch
**`placer-rotation`**.

This document hands off an in-progress rewrite of Steinmetz's Fusion placer. Read
it fully before touching `src/place.py`. The one-line summary has changed again:
the previous blocker was the legality model. A contact/copper fallback helped,
but it was too permissive: it let parts intrude into each other's
`ComponentExcludeTop` regions. The current fix uses package-local
`ComponentExcludeTop`/`ComponentExcludeBottom` geometry as the primary placement
box, with copper/contact geometry only as fallback. The corrected live reset-board
run landed at **254.0 mm / 13 crossings** with 0 checker overlaps and a passing
pad-position gate.

## Latest correction: ComponentExcludeTop legality

The user pointed out that the copper-bbox result violated `ComponentExcludeTop`.
Confirmed live:

- `ComponentExcludeTop` = layer 39, visible/used.
- `ComponentExcludeBottom` = layer 40.
- Layer 39 package-local wires define the intended placement boxes:
  - `R-0603`: about `3.02 x 1.45 mm`
  - `BGA256...`: about `16.05 x 16.05 mm`
  - `QFN50...`: about `8.27 x 8.27 mm`

Changes made after that correction:

- `src/board.py`
  - Added `Board.exclude_boxes` and `Board.placement_bbox()`.
  - `read_board()` now reads layer names plus package-local `Wire`, `Rectangle`,
    and `Circle` geometry on `ComponentExcludeTop`/`ComponentExcludeBottom`.
  - Placement boxes prefer component-exclude geometry; copper/contact boxes are
    fallback only.
- `src/place.py`
  - `_bbox()` now uses `Board.placement_bbox()`.
- Verified live:
  - `Signal-airwire: 2899.4 mm -> 254.0 mm`
  - `Airwire crossings: 432 -> 13`
  - `Verified 15/15 parts landed within 0.05 mm`
  - Pad-position check passed for all 15 parts.
  - Read-only `--refine-only` after apply: `254.0 -> 254.0 mm`, 0 movement, 0
    overlaps.
  - Screenshot exported to `/mnt/c/tmp/steinmetz_clearance_0p1.png` and copied to
    `docs/img/place-after.png`.

## Superseded update: copper/contact fallback

The user reset the board and selected `R1`-`R14` + `U2`. The next pass fixed the
package-artwork bbox mismatch by using copper/contact geometry. This produced a
tighter result but was later found to violate `ComponentExcludeTop`; keep this
section only as history. The current legal result is the
`ComponentExcludeTop`/`Bottom` result above.

- `src/board.py`
  - Added `ContactBox` and `Board.contact_boxes`.
  - `read_board()` now reads `electronics.Contact` and SMD `dx`/`dy`, learns
    package/contact pad sizes from connected SMD rows, fills missing contact
    sizes with a per-package median, and builds each element's local hard
    geometry in the element's current frame.
  - Added `Board.copper_bbox(pad=0.2)`. The old `pkg_bbox()` remains available,
    but package bboxes are no longer used for placement legality because they
    include text/value/artwork extents.
- `src/place.py`
  - `_bbox()` now uses `Board.copper_bbox()` instead of `Board.pkg_bbox()`.
- Docs/images
  - `docs/img/place-after.png` refreshed from the verified copper-bbox run.
  - `docs/PLACE.md` and `docs/tighten-placement.md` updated to describe
    hard-contact clearance rather than package courtyard clearance.

Verified live result after applying:

- `Signal-airwire: 2899.5 mm -> 206.9 mm`
- `Airwire crossings: 432 -> 9`
- `Verified 15/15 parts landed within 0.05 mm`
- Pad-position check passed for all 15 parts.
- Read-only `--refine-only` evaluation immediately after apply was idempotent:
  `206.9 -> 206.9 mm`, `9 -> 9`, 0 movement, 0 overlaps.
- Screenshot exported to `/mnt/c/tmp/steinmetz_place_after_copper.png` and copied
  to `docs/img/place-after.png`.

**Current recommendation:** the legality-model blocker is handled for this BGA
case. Next improvements should focus on validating the copper/body heuristic on
other packages, then reducing runtime or adding a deterministic side/edge packer
only if more boards expose layout-quality issues.

## Latest update from this session

The board was reset by the user, then the rewritten placer was evaluated and run
live with:

```bash
STEINMETZ_FUSION_HOST=172.17.64.1 python src/place.py --only R1 R2 R3 R4 R5 R6 R7 R8 R9 R10 R11 R12 R13 R14 U2
```

Changes made:

- `src/place.py`
  - `_spread()` now recomputes bboxes after each accepted movable/movable push.
    This fixed stale-geometry overlap math that made dense clusters slosh.
  - A trial anchor-aware `_spread()` was tested and rejected: it made legality
    more principled but worsened the result (`361.3 mm / 25 crossings`) because
    it over-pushed parts away from the BGA.
  - Same-footprint swaps in `improve()` now evaluate swapping the slot rotations
    as well as the centers. This helped crossings.
- Added `scripts/evaluate_place.py`, a read-only live-board evaluator. It can
  evaluate current `src/place.py` or a saved placer via `--place-path`. It was
  used to compare old vs current without writing MOVEs.
- `docs/img/place-after.png` was refreshed once from a verified live run.

Verified live result from the revised default algorithm:

- `Signal-airwire: 2899.5 mm -> 350.7 mm`
- `Airwire crossings: 432 -> 19`
- `Verified 15/15 parts landed within 0.05 mm`
- Pad-position check passed for all 15 parts.
- Screenshot exported to `/mnt/c/tmp/steinmetz_place_after.png`.

Then the user showed the placement they actually expected. Screenshot:

- `/mnt/c/tmp/steinmetz_expected_place.png`

Metrics of that current expected live placement:

- `airwire = 262.4 mm`
- `crossings = 19`
- Visually: correct target. Passives are tightly edge-packed around `U1`; `U2`
  is tucked to the left.
- The current `_fits()` / bbox checker reports 27 overlaps against that expected
  layout, including many passive-vs-`U1` overlaps. This is now understood as a
  **clearance/courtyard-model mismatch**, not a layout-quality failure. The
  package bbox/courtyard proxy is too conservative for the intended BGA-adjacent
  placement.

**Superseded note:** this was the state before the continuation above. The
package-bbox legality model was the blocker; `_bbox()` now uses contact/copper
geometry, and the reset-board placement is both compact and verified live.

## What this project is

Steinmetz drives a **live** Autodesk Fusion Electronics board over an HTTP bridge
(see `CLAUDE.md`, `docs/fusion-bridge.md`). `src/place.py` places the
GROUP-selected parts (rest frozen), writes `ROTATE`/`MOVE` over the bridge, and
verifies by re-reading. Nothing runs without a live Fusion session; offline you
can only test the pure compute (see "Testing").

## The task

Upgrade the placer using the *ideas* from the KiCad project at
`../KiCadRoutingTools` (read `../KiCadRoutingTools/docs/placement-optimization.md`
— a strong survey + design). **Fusion only** — borrow ideas, do not import that
code or use its boards. Test on the currently-open `.brd`: a 16-part ECP5 BGA
board, `U1` (BGA) frozen as the anchor, **R1–R14 + U2** GROUP-selected.

## Key findings (these are load-bearing — verify before trusting)

1. **The problem is separable.** Every net is 2-pad; each passive (R/C) straddles
   two pads on the fixed IC; there are **zero device-to-device nets** (confirmed
   by `scratchpad/analyze.py`). So a part's airwire depends only on its own
   `(x,y,rotation)` — parts interact only by not overlapping. This is the whole
   basis of the "optimal-region" approach.
2. **Resolved: "the airwire floor is ~304 mm and illegal" was a bbox artifact.**
   The per-part optimum
   (each part at its own best spot, legal only vs the anchor) sums to ~280 mm for
   the movable nets; plus a **constant 24.4 mm of U1-internal airwire** (nets
   between two U1 pads — U1 is frozen) → ~304 mm total. But this floor requires
   parts to **overlap the BGA courtyard** (sit on the pads). See
   `scratchpad/verify_partaw.py`, `debug_optregion.py`.
   **Update:** after switching hard legality from package artwork bbox to
   contact/copper bbox, the placer reached `206.9 mm / 9 crossings` with 0
   checker overlaps and verified Fusion pad positions. Treat the old 304 mm
   floor as an artifact of the package bbox model.
3. **The "281.9 mm hand layout" was not necessarily a bad target.** The earlier
   conclusion that it was impossible/illegal is now suspect for the same reason:
   the current package bbox model likely overstates keepout around `U1` and the
   passives. Do not chase that exact number blindly, but do not dismiss compact
   BGA-edge placements as invalid solely because `_fits()` says they overlap.
4. **Results:**
   - Old placer (barycentric + spiral): **375 mm, 34 crossings, ~4 s** — always legal.
   - New placer from the handoff: **357 mm, 18 crossings, ~15 s** — legal, 0 overlaps.
   - New placer after this session's small fixes on the reset board:
     **350.7 mm, 19 crossings**, verified live, 0 overlaps by current checker.
   - User-expected compact layout: **262.4 mm, 19 crossings**, visually correct,
     but flagged by current checker as 27 overlaps. This is the important target.
   - Copper/contact legality model after continuation:
     **206.9 mm, 9 crossings**, verified live, 0 overlaps by current checker.
5. **The illegal-overlap bug (now fixed).** My first "313.9 mm" live result was a
   BUG: `_spread` never checked against anchors, so it pushed parts *into* the BGA
   courtyard, and `_settle`/`improve` won't move an illegal part to a legal-but-
   longer spot. Fusion **offsets a part whose `MOVE` target overlaps**, so those
   parts failed the pad-position gate (R2, then R4, then 4 parts — escalating as
   re-runs compounded the corruption). Fixed by adding `_legalize` (hard
   nearest-legal guarantee) — output now has 0 overlaps (`scratchpad/test_final.py`).

## Current code state (`src/place.py`)

Rewritten pipeline (all methods present; `grep -nE '^    def ' src/place.py`):

- **`place(pos_step, angle_step, span, max_disp)`** = **ideal → spread → settle →
  legalize → settle → legalize**:
  - `_optimal_region` — per part, ring search from `_implied_centers` centroid ×
    rotations, min `_part_airwire_at`, legal via `_fits`. (The core; it is
    CORRECT — per-part it hits the ~280 optimum, see `debug_optregion.py`.)
  - `_spread` — minimal-displacement AABB overlap removal. It now recomputes
    bboxes after each accepted movable/movable push, then clamps to board. A
    trial anchor-pushing version was worse, so anchor conflicts remain the
    responsibility of `_legalize`.
  - `_settle` — coordinate descent: re-place each part at its optimal region vs
    all others; monotone in airwire; **only accepts airwire improvements, so it
    will NOT fix an illegal position** (this is why legalize must follow).
  - `_legalize` — forces any overlapping part to nearest legal spot (guarantees 0
    overlaps). Added to fix finding #5.
- **`improve(...)`** — zero-temperature quench (nudge/rotate/same-footprint swap)
  on `_global_cost`. Same-footprint swaps now consider both keeping and swapping
  the rotations associated with the two slots.
- **Removed** from the old placer: `solve`, `_target`, `refine_rotations`,
  `_rotation_fits` (barycentric path — the user chose a clean rewrite).
- **Kept verbatim:** `_rot`, `_pad_global`, `airwire`, `crossings`, `_cost`,
  `_fits`, `_angle_steps`, and the whole `main()` write/verify tail
  (`run_eagle_batch(cmds, grid="MM")`, 0.05 mm landing + `_pads_match` pad gate).
- **Changed after continuation:** `_bbox()` now uses `Board.copper_bbox()`, whose
  hard geometry is derived from contacts/SMD pad sizes plus a small body pad,
  instead of the oversized package artwork bbox.
- **New optional cost terms**, default OFF: `_spread_penalty` (`--halo-weight`),
  `_edge_penalty` (`--edge-weight`). New CLI flags: `--refine-only`, `--pos-step`,
  `--span`, `--nudge`, `--quench-passes`, `--max-displacement`, `--halo-*`,
  `--edge-*`. The `Signal-airwire:` output line is byte-identical (the `/loop`
  parses it).

Docs already updated to match: `docs/PLACE.md` (methodology + floor caveat),
`docs/tighten-placement.md` (1–2 pass convergence), `docs/img/place-after.png`
(refreshed from the verified live run, not the old illegal 313.9 run).

`scripts/evaluate_place.py` is new and intentionally read-only. Example:

```bash
STEINMETZ_FUSION_HOST=172.17.64.1 python scripts/evaluate_place.py --only R1 R2 R3 R4 R5 R6 R7 R8 R9 R10 R11 R12 R13 R14 U2
```

## Open problems / next steps

1. **Validate the new hard-geometry model on more packages.** The BGA/passive
   case is fixed by using package contacts + observed SMD sizes with a 0.2 mm
   body pad. Check this on non-BGA boards, odd pad shapes, exposed pads, and
   through-hole parts before treating it as a universal courtyard replacement.
2. **Consider explicit clearance profiles.** The current `--clearance` applies
   to contact/body boxes. If future boards need different semantics, add a
   profile for "copper/body hard keepout" vs "assembly courtyard" instead of
   reverting to raw package bboxes.
3. **If layout quality regresses, revisit the placer.** The expected screenshot
   suggests a side/edge packer would be useful: classify each passive by the
   angle of its connected U1 pads, place it on the corresponding BGA side, and
   pack/order along that side. This is likely more deterministic and reviewable
   than ring-search + spread for BGA escape parts.
4. **Runtime**: ~15-20 s is still slower than the old placer. Main costs: the `_legalize`+`_settle`
   iterations and `improve`'s full `_global_cost` per candidate. Options: make
   scoring incremental in `improve` (only the moved part's nets change; crossings
   incrementally), cap `_settle` passes, or drop `improve`.
5. **Legalization quality**: `_legalize` (nearest-legal) pushes parts far and
   costs airwire (raw hard-legalize alone gave 401 mm before `_settle` recovered
   to 357). An anchor-aware spreader was tried naively and made results worse;
   the better direction is a side-aware edge packer after clearance semantics are
   corrected.
6. **Non-idempotence / churn**: `place()` does not reproduce itself across board
   states — re-running shuffles parts a few mm (airwire converges ~stable, but
   positions drift). For the `/loop` this is cosmetically bad. Root cause: spread/
   settle are path-dependent. A deterministic legal fixed point would fix it.
7. **Generality**: the whole approach assumes separable 2-port-around-IC. The user
   said most boards won't be BGAs but that structure is the common case; coupled
   sets (buses, diff pairs) get a weaker seed (no global co-optimization). The
   old barycentric path handled coupling better as a seed — worth remembering.

## Testing

- **Offline (no Fusion), fast iteration**: the earlier handoff referenced
  `scratchpad/board.pkl` and `scratchpad/test_final.py`, but `scratchpad/` is not
  present in this checkout. If those files are restored, they may still be useful.
  For now, use the live read-only evaluator below.
- **Read-only live evaluation**:
  `STEINMETZ_FUSION_HOST=172.17.64.1 python scripts/evaluate_place.py --only R1 ... U2`.
  This reads the current Fusion board, computes a candidate, and prints airwire /
  crossings / checker overlaps without applying commands. It can compare a saved
  old placer via `--place-path /tmp/steinmetz_eval/place.py`.
- **Live**: `python src/place.py --rotate 90` with the parts GROUP-selected in
  Fusion. Requires the bridge (see `CLAUDE.md`). The pad-position gate
  (`_pads_match`, 0.1 mm) proves the requested transform matched Fusion.
  Checker-reported `0 overlaps` is still required, but it now means no overlap
  under the contact/copper hard-geometry model, not no raw package-artwork bbox
  overlap. Visual inspection plus pad-position verification are both required. Use
  `python src/screenshot.py <path>` to view.

## Reference material

- Approved plan: `~/.claude/plans/you-can-t-use-their-partitioned-pearl.md`.
- KiCad research: `../KiCadRoutingTools/docs/placement-optimization.md`; the
  matured optimizer to borrow from: `../KiCadRoutingTools/placement/quench.py`,
  `place_optimize.py`, `placement/fanout_clearance.py` (decap-around-IC — closest
  analog), `place_route_loop.py` (router-in-the-loop — they have a router; we
  don't, so airwire is our only proxy).
- Memory: `~/.claude/projects/-home-holla-steinmetz/memory/` (loop/screenshot/
  placer-use-case notes).

## Git state

The user approved committing this work after the `--clearance 0.1` live-board
run. Nothing has been pushed.
