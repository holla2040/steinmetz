# Placement — minimal-airwire component placement

`src/place.py` is Steinmetz's first tool. It reads a live Fusion Electronics
board, computes an **initial placement that minimizes ratsnest (airwire)
length**, and writes the moves back over the bridge. It is *constructive* —
it places parts from their connectivity rather than nudging an existing layout —
and it is **translation-only** (parts keep their rotation), so pad motion is
exact and there is no rotation-convention to reconcile.

## Before / after

A 16-part ECP5 BGA board with the passives scattered off-board, placed in one
pass (`--anchors 1`, anchoring only the BGA):

| Before — scattered | After — placed |
|---|---|
| ![before](img/place-before.png) | ![after](img/place-after.png) |

**Signal-airwire: 3084 → 377 mm (−88%)**, 15 parts moved and verified. The
fan of long airwires collapses into a tight ring of short ones hugging the BGA.

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
decoupling cap onto the BGA. Classification is `is_power()` (name pattern or
high fan-out); extra nets can be excluded with `--ignore-nets`.

### 3. Anchors — why something must stay fixed

Airwire length is **relative**: with no fixed part, the global optimum is "every
part stacked on one point" (airwire → 0), which is meaningless. So the placer
**pins the N largest-area packages** (the ICs) plus any `--lock` matches, and
moves everything else. The BGA is the natural anchor — biggest, highest-pin,
the center of the design. `--anchors 1` pins only it and moves all the rest
(including a second IC); `--anchors 2` pins the top two.

### 4. Constructive placement — two phases

**a. Barycentric relaxation (the pull).** A part's airwire-optimal spot is the
**centroid of the pads it connects to on other parts**. The placer computes that
target for every movable part and moves it there, iterating Gauss-Seidel (using
neighbours' updated positions) until it converges (`_target` / `solve`). This is
the classic force-directed / quadratic placement: it minimizes summed-squared
airwire and lets connected parts settle relative to the anchored ICs.

**b. Legalization (the spread).** Barycentric pulls parts together until their
courtyards overlap, so each part — most-central first — is placed at its target
if it fits, otherwise the placer **spirals outward to the nearest legal spot**
(`_fits` / `solve`). "Fits" means no courtyard overlap (package bounding box +
`--clearance`) and inside the board outline by `--margin`. Courtyards come from
the `Package` bounding box.

### 5. Translation-only

Parts keep their current rotation; each pad moves rigidly with its element's
centre (`_pad_global`). This sidesteps every KiCad/EAGLE angle-sign question, and
rotation mostly helps fine pad alignment, not gross airwire — so it is left for a
later refinement.

### 6. Write back and verify

For each moved part the placer emits `MOVE Rn (x y)`, fires them as **one
terminated batch** over the bridge (`run_eagle_batch(..., grid="MM")`), then
**re-reads every element and confirms it landed within 0.05 mm**. Changes are
unsaved until you save in Fusion — reopening reverts them.

## What it does *not* do

This minimizes airwire **length only**. It does not consider:

- airwire **crossings** or routing congestion,
- **rotation** optimization,
- a BGA's **decoupling caps wanting to sit at their power balls**,
- and none of the constraints that live *outside* the board file — thermal
  spreading, EMI zoning, connector/mechanical positions, datasheet-mandated
  hot-loop layouts.

So treat the result as a fast, sensible **constructive seed** — a starting
placement, not a finished layout. It is deterministic, and roughly idempotent:
re-running on an already-placed board changes little.

## Usage

```bash
python src/place.py                 # propose only — print moves + before/after, no writes
python src/place.py --apply         # propose, then MOVE the parts in Fusion (verified)
python src/place.py --apply --anchors 1     # pin only the biggest part; place everything else
python src/place.py --lock "J*" --ignore-nets "VCC*" "3V3"
```

| flag | default | meaning |
|------|---------|---------|
| `--apply` | off | write the moves (otherwise dry-run) |
| `--anchors N` | 2 | pin the N largest parts in place |
| `--lock PAT…` | – | also pin parts whose ref matches these patterns |
| `--ignore-nets PAT…` | – | extra net-name patterns to exclude from airwire scoring |
| `--clearance MM` | 0.3 | courtyard gap between parts |
| `--margin MM` | 1.0 | keep parts this far inside the board edge |

Run with a board open in Fusion and the bridge reachable (see
[fusion-bridge.md](fusion-bridge.md)).
