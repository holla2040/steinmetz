# Placement — minimal-airwire component placement

`src/place.py` is Steinmetz's first tool. It reads a live Fusion Electronics
board and re-places the **selected** parts to minimize ratsnest (airwire)
length, holding every other part fixed, then writes the moves back over the
bridge and verifies them. The selection is read over the bridge (see
`src/selection.py`) — **select nothing and it does nothing**. Placement is
connectivity-driven — it positions the selected parts from where their nets land
on the fixed board rather than nudging an existing layout — and it always runs a
rotation-refinement pass (`--rotate` sets the step, default 90°).

## Iterative tightening loop

To tighten a GROUP-selected set of parts until it converges, run the ready-made
`/loop` prompt in [`tighten-placement.md`](tighten-placement.md): start a fresh
agent and enter **`/loop follow docs/tighten-placement.md`**. The invocation is
the same every firing — the prompt is self-contained and tracks progress in
`~/tmp/place_loop_state.json`, running placement passes until one improves airwire
by < 1 mm.

## Before / after

A set of selected parts pulled from a scattered state into a tight cluster that
minimizes airwire, while the rest of the board stays fixed:

| Before | After |
|---|---|
| ![before](img/place-before.png) | ![after](img/place-after.png) |

Each run prints the before/after signal-airwire length and crossing count; the
tightening loop refreshes these images from a converged run.

## Methodology

### 1. Read the board into a model — `src/board.py`

`read_board()` pulls the EAGLE object model over the bridge and joins it into
elements, connected pads, nets, packages, and the board outline. The key join:
a net (`Signal`) reaches a pad through `ContactRef`
(`element` + `contact` + `signal`), and the pad's placed global position is on
the `Smd`/`Pad` row keyed by `contact_object_id`. Joining them yields, per
connected pad: **which element, which net, and where it physically sits**. Reads
auto-paginate, so connectivity is never silently truncated.

### 2. The objective — ratsnest length

For each net, the airwire length is the **Euclidean minimum spanning tree** over
its pad positions (`mst_len` in `place.py`) — the same thing Fusion's ratsnest
draws. The cost is the sum over **signal nets only**: power/plane nets (`GND`,
`VCC*`, anything with ≥ 8 pins) are excluded, because they are poured/planed
rather than routed point-to-point — counting them would just drag every
decoupling cap toward the fixed parts. Classification is `is_power()` (name
pattern or high fan-out); extra nets can be excluded with `--ignore-nets`.

### 3. The fixed frame — why something must stay put

Airwire length is **relative**: if every part could move, the global optimum is
"all parts stacked on one point" (airwire → 0), which is meaningless. The placer
resolves this by moving **only the selected parts** and holding everything else
fixed — the unselected parts are the reference frame the selection is pulled
toward. So the selection *is* the input: group the parts you want placed and
leave the parts you want as anchors out of the group. `--only REF…` overrides the
live selection with an explicit ref list. Select nothing → nothing moves.

### 4. Constructive placement — two phases

**a. Barycentric relaxation (the pull).** A part's airwire-optimal spot is the
**centroid of the pads it connects to on other parts**. The placer computes that
target for every selected part and moves it there, iterating Gauss-Seidel (using
neighbours' updated positions) until it converges (`_target` / `solve`). This is
the classic force-directed / quadratic placement: it minimizes summed-squared
airwire and lets the selected parts settle relative to the fixed ones.

**b. Legalization (the spread).** Barycentric pulls parts together until their
courtyards overlap, so each part — most-central first — is placed at its target
if it fits, otherwise the placer **spirals outward to the nearest legal spot**
(`_fits` / `solve`). "Fits" means no courtyard overlap (package bounding box +
`--clearance`) and inside the board outline by `--margin`. Courtyards come from
the `Package` bounding box.

### 5. Rotation (`--rotate`)

Rotation is **always on**; `--rotate DEG` sets the step (default **90°** →
0/90/180/270; `--rotate 1` lets parts rotate freely in 1° steps). Run **after**
legalization with each part's centre held fixed, every selected part tries the
candidate orientations and keeps the one that most lowers the objective **airwire
length + `--cross-weight`·crossings** — a greedy coordinate descent
(`refine_rotations`) swept to convergence. Rotation reorients a part's own pads,
so it shortens the nets it touches and can *uncross* airwires whose crossing is
just a pad-ordering artifact.

Two deliberate choices keep it honest:

- **Length stays primary.** Crossings enter only as a small mm-weighted penalty
  (`--cross-weight`, default 2.0) that breaks near-ties — it never trades real
  length away to shave a crossing. The before/after **crossing count** is
  printed next to the airwire numbers so you can see the effect and tune it.
- **Ties hold the current angle**, so `Δ = 0` is the default and a second run on
  an already-placed board emits **no** rotations (idempotent).

Rotation depends on how Fusion applies `ROTATE Rn` to pad coordinates —
counter-clockwise-positive, `(dx,dy) → (-dy,dx)` for 90° (confirmed live: a
`ROTATE R90` moved all of a part's pads there). Right-angle steps use an exact
integer remap; other steps fall back to trig, with the pad-position tolerance
absorbing the round-off. Mirrored (bottom-side) parts are left un-rotated (their
angle field may not capture the flip), and after writing, the tool re-reads and
checks **actual pad positions vs. prediction** for every touched part (see §6) —
the gate that proves the transform matched Fusion.

> **Command quirk:** the part name must be **single-quoted** for `ROTATE`
> (`ROTATE R90 '<part>'`). A bare `ROTATE R90 <part>` is silently a no-op — the
> parser doesn't bind the object — even though `MOVE <part> (x y)` takes the name
> bare.

### 6. Write back and verify

For each touched part the placer emits a relative `ROTATE Rn '<part>'` (preserves
mirror state) followed by `MOVE <part> (x y)` — both pivot on the element origin,
so order does not matter — and fires them as **one terminated batch** over the
bridge (`run_eagle_batch(..., grid="MM")`). It then re-reads the board and
verifies twice: every element **landed within 0.05 mm**, and — the stronger gate
— each touched part's **predicted pad positions match the board's actual pads**
(`_pads_match`). A mismatch (wrong angle sign, an unhandled mirror) is flagged
loudly rather than silently shipped. Changes are unsaved until you save in Fusion
— reopening reverts them.

## What it does *not* do

This minimizes airwire **length** (with rotation as a secondary lever on
crossings). It does not consider:

- **routing congestion**, or crossings that need two parts to *swap places* —
  rotation keeps a part's centre fixed, so it cannot fix those; full crossing
  elimination needs repositioning (future work),
- decoupling caps that should sit at specific power pins per the datasheet,
- and none of the constraints that live *outside* the board file — thermal
  spreading, EMI zoning, connector/mechanical positions, datasheet-mandated
  hot-loop layouts.

So treat the result as a fast, sensible **constructive seed** — a starting
placement, not a finished layout. It is deterministic, and roughly idempotent:
re-running on an already-placed selection changes little.

## Usage

```bash
python src/place.py                 # place the current selection (90° rotation), write + verify
python src/place.py --rotate 1      # let parts rotate freely, in 1° steps
python src/place.py --only R4 R5 R8 # override the selection with an explicit ref list
python src/place.py --ignore-nets "VCC*" "3V3"
```

| flag | default | meaning |
|------|---------|---------|
| `--rotate DEG` | 90 | rotation step in degrees, always on (`1` = free rotation) |
| `--cross-weight MM` | 2.0 | airwire-crossing penalty (length stays primary) |
| `--only REF…` | – | place these refs instead of the live selection |
| `--ignore-nets PAT…` | – | extra net-name patterns to exclude from airwire scoring |
| `--clearance MM` | 0.3 | courtyard gap between parts |
| `--margin MM` | 1.0 | keep parts this far inside the board edge |

GROUP-select the parts to place first (see `src/selection.py`), and run with a
board open in Fusion and the bridge reachable (see
[fusion-bridge.md](fusion-bridge.md)).
