# Vectorizing the placer — methodology, engineering log, results

`src/place.py` produced good placements but took **3 m 18 s of pure
optimization** to place 23 resistors at `--rotate 45`. This document records
how that became **2.5 s on the live board** — same objective, same pipeline,
same CLI, same verify gates — by moving the inner loops into a numpy engine
(`src/placevec.py`, `VecEngine`), and what was learned along the way. It is
the companion to [PLACE.md](PLACE.md), which describes *what* the placer
computes; this describes *how it computes it fast*.

## 1. The problem

The reference board: 95 × 43 mm, 25 parts, 68 nets (3 power/plane ignored),
23 movable resistors around a frozen QFP (U1) and a frozen 45°-rotated BGA
(U2). The original run:

```
[  0.4s] optimizing placement
[  5.7s] spreading overlaps
[ 15.7s] searching R23 at R270
   ...           (settle passes, ~2m45s)
[ 3m00.8s] quenching up to 6 pass(es)
[ 3m17.9s] scoring final placement
Signal-airwire: 2237.4 mm  ->  387.9 mm   Crossings: 474 -> 37
Elapsed:  3m27.2s
```

The optimization problem itself is tiny — 23 parts, nets of 2–7 pads, one
convex rectangle of placement geometry per part. What cost three minutes was
**per-candidate Python dispatch**: the scalar placer evaluated one
`(centre, rotation)` candidate at a time, and each evaluation ran a
Python-loop Prim MST per net plus a Python separating-axis test per placed
part. At 0.5 mm search granularity over rotations × rings × settle passes,
that is millions of interpreter round-trips over microscopic geometry.

So the thesis of the rework: **don't change the optimizer, change the unit of
evaluation** — from one candidate to a *block* of candidates, scored and
legality-tested as arrays.

## 2. Ground rules

The placer's behavior is documented, verified against the live board, and
trusted. The rework held these invariant:

- **The objective** — airwire (Euclidean-MST) length + `--cross-weight` ×
  proper crossings, plus the orientation tie-break and the optional halo/edge
  terms. Bit-for-bit the same formulas.
- **The pipeline** — seed → spread → settle → legalize → settle → legalize →
  quench, with the same acceptance rules (strict improvement, fixed
  most-constrained-first order) and the same hard-legality guarantee that
  makes Fusion's `MOVE` land every part.
- **The CLI, output, and gates** — every flag, the printed report, the
  `ROTATE`/`MOVE` write path, the 0.05 mm landing check, and the pad-position
  gate are untouched.
- **The scalar spec stays.** `Placer` keeps its single-candidate methods
  (`_fits`, `_part_cost_at`, `_global_cost`, `mst_len`, `mst_edges`,
  `segments_cross`, `polygons_overlap`) as the *readable specification* of
  the objective and legality. The engine is required to agree with them —
  `scripts/validate_placevec.py` checks the agreement on the live board.
  Change semantics in both places or not at all.

Where exact bit-parity was impossible, the deviation is one-sided: the
vectorized search explores a **superset** of the scalar candidate rings (stop
rules applied at block granularity), so per-part optima are equal or better;
ties inside the 1e-9/1e-12 acceptance epsilons may break differently. The
end-to-end results bear this out — airwire came out slightly *better* at
every rotation step.

## 3. Method: a fast, safe iteration loop

Optimizing against the live board would have meant minutes of bridge I/O per
experiment and a mutable target. Instead:

1. **Snapshot** — one bridge session pickled the `Board`, the live grid
   (0.5 mm), and the GROUP selection (R1–R23). All optimization runs offline
   against that snapshot; the placer's compute path is identical (`Placer`
   accepts `grid_step` directly, skipping the live grid query).
2. **Frozen baseline** — the pre-rework `place.py` was copied aside and run
   through the same harness for reference timings and quality.
3. **Spec validator** — every engine primitive is compared against the scalar
   methods on randomized states of the *real* board (~1,500 comparisons per
   run; §7). This gate ran after every change, and it caught two real bugs:
   an early `_bbox` mismatch (the engine derived the board-margin box from
   the SAT boxes instead of `placement_bbox` — they differ for parts using
   the padded contact fallback), and the AABB-axes case described in §4.2,
   which only surfaced once the validator ran against a board whose parts
   had already been *placed* at rotated angles.
4. **Profile, don't guess** — each optimization round started from a cProfile
   of the actual hot path. Every one of them pointed somewhere unexpected
   (§5).
5. **Live proof last** — the finished engine ran the real
   `place.py --rotate 45` against Fusion: write, re-read, 0.05 mm landing
   check, pad-position gate, screenshot.

## 4. The engine

`VecEngine` mirrors `Placer`'s state as arrays: `centers (N, 2)` and
`deltas (N,)` (rotation deltas), indexed by element order, converted to/from
the tool-facing dicts only at phase boundaries. Everything below lives in
`src/placevec.py`.

### 4.1 Batched Prim — `mst_len_batch` / `mst_edges_batch`

A scored net has < 8 pads (≥ 8 pins is classified power and ignored), so the
MST of one net is trivial — the win is solving one net's MST for **M
candidate centres at once**. `mst_len_batch` takes `(M, K, 2)` points and
runs Prim as K−1 numpy passes over an `(M, K)` frontier — `argmin`, gather,
`minimum` — instead of M Python Prims. `mst_edges_batch` additionally tracks
parents, because a candidate's crossing count needs the actual airwire
segments and the tree topology can change with the centre.

Two exact closed forms carry most of the load: `K = 2` is a hypot, and
`K = 3` is *perimeter minus longest side* (any two edges of a triangle share
a node, so the MST is the two shortest). On this board that covers the large
majority of net evaluations.

### 4.2 Vectorized separating-axis legality — `_fits_batch` / `_fits_rows`

Placement polygons are rectangles (the component-exclude union, or contact
boxes as fallback), so a moving/placed pair has **four candidate separating
axes**, and every projection is **affine in the candidate centre**:
`proj(c) = c·u + [pmin, pmax]`, with the placed part's interval a constant.
Legality of a whole candidate block against a placed part is therefore a
handful of `(M,)`-shaped comparisons; against *all* placed parts it is a few
`(M, J)` comparisons, with per-part constants (world corners, axes,
projection intervals) hoisted into a per-search context.

Details that matter:

- The common case (single exclude rect vs single exclude rect) runs the fully
  stacked fast path; multi-box parts fall back to a general per-pair SAT.
- A part contributes nothing but dead `(M, J)` columns if no candidate can
  reach it — a block-level bbox trim by `radius_i + radius_j + clearance`
  drops it before the row math.
- Zero-length rect edges normalize to a null axis whose projections can never
  separate — which is exactly the scalar behavior of skipping that axis.
- **The scalar's AABB early-out is part of the predicate, not a shortcut.**
  `_fits` skips the polygon SAT when the parts' axis-aligned bboxes are
  separated by ≥ clearance. That looks like a prefilter, but
  SAT-*with-clearance* over edge normals alone is incomplete: when both rects
  are rotated (say 45° vs 135°), all four edge normals are diagonal and the
  x/y axes — where the actual clearance may live — are never tested. The
  AABB check effectively contributes those two axes, so the engine includes
  it in every pair test (`_aabb_sep`). Found by the validator on the live
  board after the first placement run left parts at rotated base angles; the
  scattered-board snapshot (all movers axis-aligned) could never expose it.
  The engine had been strictly *over*-tight — no illegal output, but legal
  candidates pruned.
- Board-margin, grid-snap (`_on_grid`), and `max_disp` checks are the same
  vector ops; the margin box is the scalar `placement_bbox`-derived AABB, not
  the SAT-box union (they differ under the contact fallback).

`_fits_rows` is the same test with a **per-row angle id**: row *m* is
candidate `C[m]` at `angles[aid[m]]`, with per-angle geometry gathered by
`aid`. That is what lets a whole rotation sweep share one call.

### 4.3 The lockstep ring search — `search`

The scalar `_optimal_region` sweeps rotations; per rotation it walks rings
outward from the angle's implied-centre centroid, stopping `span` mm past the
first legal ring. The engine keeps those semantics *per angle* but walks all
angles **in lockstep through shared radius blocks**:

- **Unit-ring cache.** Every angle at every search walks the same radius
  grid; only the centre differs. Rings are generated once per `(radius,
  step)` around the origin and broadcast over the per-angle ideal centres —
  one numpy op per block instead of a Python call per (angle, ring). (The
  first implementation generated 1.5 M individual rings at `--rotate 1`.)
- **Per-angle stop rules.** Each angle keeps its own `r_first`/`r_stop =
  r_first + span`; a prefix mask trims an angle's rows out of later blocks.
  Stop rules apply at block granularity, so the explored rings are a superset
  of the scalar walk — per-part optima are equal or better.
- **Hunting vs pruning.** Until an angle has found its first legal ring,
  every row must be legality-tested (that is what defines `r_first`) — in
  that phase legality runs *first* and only legal rows are scored, since near
  a dense anchor most rows are illegal. Once every live angle prunes by cost,
  the order flips: score everything cheaply, legality-test only rows that
  could beat the best-so-far.
- **Adaptive block size.** Blocks stay small (~384 rows) while hunting —
  those rows are all SAT-tested — and grow (~4096) once cost pruning is
  active, where bigger blocks just amortize numpy overhead. Getting this
  wrong is measurable: a fixed 1536-row target made 90° *slower* (6.4 s)
  than the naive version because the first block dragged ~24 radius-rings of
  four angles through full legality.
- **All-angle closed forms.** Two per-angle constants collapse across the
  sweep: the orientation penalty depends on the angle only through the
  rotated own-pad vector (whose *length* is invariant), and the
  implied-centre centroid is affine in the rotation matrix
  (`ideal(a) = U − R(a)·V`). Both are one cos/sin table for all 360 angles
  (`_orient_pen_all`, `_implied_all`) instead of 360 scalar calls.
- **Dedupe only when it pays.** The scalar per-ring `seen` set exists to skip
  duplicate snapped candidates. On the default fine grid (0.05 mm) ring
  spacing (0.5 mm) never collides — and the `np.unique(axis=0)` row-sort that
  implemented it was **32 % of the entire 1° runtime**. Deduplication now
  runs only when the snap grid is coarse enough to collapse neighbours
  (`grid ≥ pos_step / 2`).

`nearest_legal` (the safety net and the legalizer's workhorse) is the same
ring walk, legality-only, first hit in the scalar's position-major /
angle-minor order.

### 4.4 The quench — exact cost decomposition + branch-and-bound

The scalar quench computed the **full global cost** (all nets' MSTs + all
segment-pair crossings) for every nudge/rotate candidate of every part —
`O(nets·K² + segments²)` per candidate, hundreds of candidates per part.

The engine compares candidates on **only the terms the move can change**
(`_own_terms`): the part's own nets' airwire, crossings that touch its own
nets' segments, its own orientation penalty, the orientation penalties of
*net-mates* (their "others" centroid contains this part's pads — an affine
function of the candidate centre, so neighbors batch too:
`_neighbor_orient_rows`), and its halo/edge terms when enabled. Every other
term is identical on both sides of the acceptance test and cancels. This is
not an approximation — the validator asserts the identity
`Δ_own_terms ≡ Δ_global_cost` on random states (§7).

On top of the decomposition:

- **All (offset × angle) cells in one row batch**, offset-major / angle-minor
  — the scalar sweep order, so tie-breaks match.
- **Lower-bound pruning.** The crossing term is non-negative, so
  `airwire + orientation (+ halo/edge)` is a valid lower bound on the full
  cost. Cells whose bound already misses the incumbent can never be accepted
  and are dropped before any segment is built.
- **Branch-and-bound over the crossing term.** The bound alone is weak
  whenever the incumbent itself carries crossings (its `w·c₀` inflates the
  threshold and thousands of near-flat cells "survive" — the profile showed
  the degenerate case: passes that accept nothing evaluated every survivor).
  Survivors are therefore processed in ascending-bound chunks; as soon as the
  next chunk's minimum bound can't beat the best *legal full* cost found, the
  rest are provably irrelevant.
- **Sparse crossing predicates.** A proper crossing requires overlapping
  segment bboxes, so `cross_count_ab` runs the orientation predicates only on
  the bbox-overlapping pairs (`flatnonzero` + gather + `bincount`), not the
  dense `(M, E, F)` tensor.

Same-footprint swaps use the pair analogue (`_pair_terms`: union nets,
crossings touching them, both parts' + neighbors' orientation, halo pairs
counted once) — also validated as an identity against `_global_cost`.

### 4.5 Exact dirty-tracking — settle and quench

Coordinate descent converges by long tails: after the first pass or two, most
parts re-search and accept nothing. But a part's search depends **only on the
other parts' state** — so re-searching a part whose visible surroundings
haven't changed must reproduce the same result and accept nothing. Skipping
it is *exact*, not heuristic.

"Visible" is made conservative and cheap:

- **Net-mates** — any part sharing a net (its cost terms move).
- **Legality disc** — each search records the bbox of every candidate it
  tested, padded by radii + clearance; a mover whose old or new footprint
  touches a part's disc re-dirties it.
- **Crossing hull (quench only)** — a part's decision also reads the
  *segments* of non-incident nets. Every segment of a net lives inside the
  net's pad bbox, so a mover re-dirties any part whose candidate-area ∪
  own-net hull touches one of the mover's nets' bboxes (old or new state).

On accept, the mover re-dirties the affected set (and, in the quench, itself
— its candidate box moves with it; in settle it does not — its own position
doesn't enter its own search). Passes still count and terminate exactly as
before; a fully clean pass is the same as the scalar's "no move" pass.

### 4.6 Spread — precomputed extents

The scalar `_spread` recomputed each part's rotated bbox from scratch inside
an O(pairs × iterations) Gauss-Seidel loop. Rotations are frozen during
spread, so bbox = centre + constant extents; the loop survives verbatim
(including its deliberate recompute-after-each-push ordering) with the
geometry hoisted — from ~10 s to negligible.

## 5. Engineering log — what was tried, what the profiles said

Abbreviated; offline snapshot timings are place + quench for 23 parts.

| step | 90° | 45° | 1° | note |
|---|---|---|---|---|
| scalar baseline | 97.6 s | 216.1 s | ~2.7 h (extrapolated) | quality 403.3 / 387.9 mm |
| **v1: batch per (angle, ring-block)** — batched Prim, stacked SAT, cost-first pruning, decomposed quench | 3.5 s | 5.3 s | 207.6 s | 41× at 45°; quality already equal-or-better |
| **v2: angle-lockstep search** — all rotations share each radius block via per-row angle ids | ~same | ~same | 185.1 s | helped less than expected → profile |
| profile #1 | | | | `np.unique` dedupe **55 s (32 %)**, 1.5 M `_ring` calls 26 s, general Prim on K≤3 nets 42 s |
| **v3: unit-ring broadcast, K≤3 closed forms, dedupe only for coarse grids** | 8.1 s ⚠ | 4.7 s | 73.0 s | 90° *regressed*: fixed 1536-row blocks legality-tested ~24 rings while hunting |
| **v4: adaptive block target** (small while hunting, large while pruning) | 2.3 s | 2.9 s | 65.1 s | fixed the regression |
| profile #2 | | | | `_fits_rows` 24 s, `_air_rows` on illegal rows 10.6 s, `_search_ctx` 5.6 s (360 scalar calls/part) |
| **v5: reach-trim of placed set, legality-first hunting, all-angle closed forms** | 2.3 s | 2.7 s | 55.9 s | |
| profile #3 (quench) | | | | 26,381 `cross_count_ab` calls, 12 s — one per (part, surviving angle); and no-accept passes evaluated *every* survivor |
| **v6: row-batched segments, bbox-sparse crossings, branch-and-bound, dirty-tracking (settle + quench)** | 2.2 s | 2.6 s | 38.5 s | |
| **v7: scalar AABB axes joined to every pair SAT** (the §4.2 predicate fix) | **2.2 s** | **2.6 s** | **40.6 s** | final; quality unchanged |

Dead ends considered and rejected: coarse-to-fine angle refinement and a
shared-across-angles `r_first` (both change semantics for a mode nobody was
blocked on); float32 predicates (sign-safety near collinearity); a quadratic
analytic seed (the existing seed's quality was already the reference).

## 6. Results

### Offline (snapshot, pure optimization: place + quench)

| config | scalar | vectorized | speedup | airwire (mm) | crossings | overlaps |
|---|---|---|---|---|---|---|
| `--rotate 90` | 97.6 s | **2.2 s** | **44×** | 403.3 → **401.6** | 39 → 40 | 0 → 0 |
| `--rotate 45` | 216.1 s | **2.6 s** | **83×** | 387.9 → **385.8** | 37 → 38 | 0 → 0 |
| `--rotate 1` | ~2.7 h* | **40.6 s** | **~240×** | — → 385.9 | — → 38 | 0 | 

\* extrapolated (×45 the 8-angle cost); the scalar 1° mode was never usable.
Airwire is the primary objective; the ±1 crossing is inside the
`--cross-weight 2` near-tie band (total cost 461.4 vs 461.9 at 45°).

### Live, end to end (`place.py --rotate 45`, same board, same selection)

| | scalar (original session) | vectorized |
|---|---|---|
| optimization phase | 3 m 17.5 s | **2.5 s** |
| total elapsed (incl. bridge I/O, write, verify) | 3 m 27.2 s | **13.6 s** |
| signal airwire | 2237.4 → 387.9 mm | 2237.4 → **385.8 mm** |
| crossings | 474 → 37 | 474 → 38 |
| landing check | 23/23 within 0.05 mm | 23/23 within 0.05 mm |
| pad-position gate | pass | pass |

The remaining 11 s of the live run is bridge I/O (reads, the 45-command
`ROTATE`/`MOVE` batch, the verify re-read) — the optimizer is no longer the
long pole. The placements themselves are near-identical to the scalar run
(e.g. R1 → (42.45, 36.25) R45 in both); a handful of parts land in
equivalent-cost mirror spots.

Behavioral spot-checks: re-running on the already-placed selection accepts
nothing (idempotent, as documented); `--refine-only` on the off-board
scattered state is a no-op for both implementations (every candidate fails
the board-margin check — parts must be on the board to be tidied).

## 7. Validation

`scripts/validate_placevec.py` (run it against the live board; it is
read-only) treats the scalar methods as ground truth and asserts agreement on
randomized states of the real design — roughly 1,500 comparisons per run,
repeated for the default config and a halo+edge config:

- `mst_len_batch` / `mst_edges_batch` vs `mst_len` for K = 2…9;
- `cross_count_ab` / `cross_count_self` vs `segments_cross`, including
  shared-endpoint (star-point) exclusion;
- engine pad transforms vs `_pad_global` (the trig path differs from the
  scalar exact right-angle remaps by ~1e-16 mm, far under the 1e-6 snap
  epsilon and the 0.1 mm pad gate);
- `_fits_batch` and `_fits_rows` vs `_fits` — random parts, angles, states,
  candidate sets, against both anchor-only and everyone placed-sets;
- `_part_air_batch` + orientation vs `_part_cost_at`;
- the all-angle closed forms vs their per-angle scalars;
- `_neighbor_orient_rows` vs summed scalar `_part_orientation_penalty_at`;
- **the decomposition identities**: `Δ _own_terms ≡ Δ _global_cost` for
  single-part moves and `Δ _pair_terms ≡ Δ _global_cost` for swaps — the
  exactness claims the quench rests on.

All pass. Beyond the validator, every benchmark run re-checks the hard
invariants (zero overlaps via the scalar `_fits`, before/after airwire and
crossings), and the live run is gated by the placer's own landing and
pad-position checks.

## 8. Costs and limits

- **numpy is now a dependency** (the only one besides `requests`).
- **Two implementations of one objective.** The scalar methods are the spec
  and the engine must agree; the validator makes that a checkable statement,
  but any semantic change must be made in both places.
- **`--rotate 1` is usable, not instant** (~40 s). The remaining cost is
  honest work — 360 independent per-angle stop rules over a dense anchor
  field. The cheap next step, if it ever matters, is `--rotate 5` (~8 s,
  near-identical quality); the expensive one is relaxing per-angle `r_first`
  semantics.
- **Scaling.** Everything batches over candidates × angles and trims placed
  parts by reach, so cost grows roughly linearly with movable-part count ×
  search area; dirty-tracking keeps settle/quench tails proportional to
  actual movement, which should matter more, not less, on bigger boards.

## 9. Reproducing the methodology

The offline harness lived in session scratch space by design (it is
throwaway); reproducing it is three small scripts against `src/`:

1. *Snapshot*: pickle `read_board(bridge)`, `_read_main_grid_mm(bridge)`, and
   `read_selection(bridge)`.
2. *Bench*: construct `Placer(board, [], 0.1, 1.0, 2.0, only=selection,
   grid_step=grid/10)` from the pickle, time `place()` + `improve()`, report
   airwire/crossings/overlaps (compare implementations via
   `importlib`-loading a frozen copy, as `scripts/evaluate_place.py` does
   with `--place-path`).
3. *Profile*: `cProfile` around `place()` / `improve()`.

The spec validator is committed as `scripts/validate_placevec.py` and runs
against the live board directly.
