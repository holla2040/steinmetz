#!/usr/bin/env python3
"""Validate the vectorized placement engine against the scalar spec.

``place.Placer``'s single-candidate methods (``_fits``, ``_part_cost_at``,
``_global_cost``, ``mst_len``, ``segments_cross``) are the readable
specification of the placement objective and legality; ``placevec.VecEngine``
is required to agree with them. This script asserts that agreement on
randomized states of the *live* board — roughly 1,500 comparisons across the
default config and a halo+edge config — including the two decomposition
identities the quench rests on (Δ own-terms ≡ Δ global-cost for moves and
swaps). Read-only: it never writes to the design.

    python scripts/validate_placevec.py              # uses the live selection
    python scripts/validate_placevec.py --only "R*"  # explicit refs (globs ok)

Run with a board open in Fusion and the bridge reachable. Exit code 0 iff
every check passes. See docs/PLACE-VECTOR.md for what each check pins down.
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

import placevec
from board import read_board
from bridge import FusionBridge
from place import Placer, mst_len, segments_cross
from selection import read_selection

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if not cond:
        FAIL += 1
        print(f"FAIL {name}: {detail}")


def check_primitives():
    """Batched MST and crossing counts vs the scalar geometry helpers."""
    for K in range(2, 10):
        pts = np.random.uniform(-10, 10, size=(50, K, 2))
        got = placevec.mst_len_batch(pts)
        want = np.array([mst_len([tuple(q) for q in row]) for row in pts])
        check(f"mst_len K={K}", np.allclose(got, want, atol=1e-9),
              f"max err {np.abs(got - want).max():g}")
        got2, _edges = placevec.mst_edges_batch(pts)
        check(f"mst_edges len K={K}", np.allclose(got2, want, atol=1e-9))

    for trial in range(20):
        E, F = random.randint(1, 6), random.randint(1, 60)
        A = np.random.uniform(0, 50, size=(3, E, 2, 2))
        B = np.random.uniform(0, 50, size=(F, 2, 2))
        if trial % 3 == 0 and F >= 2:
            B[1, 0] = A[0, 0, 0]          # exercise the shared-endpoint rule
        got = placevec.cross_count_ab(A, B)
        want = np.array([sum(segments_cross((tuple(s[0]), tuple(s[1])),
                                            (tuple(t[0]), tuple(t[1])))
                             for s in row for t in B) for row in A])
        check("cross_ab", (got == want).all(), f"{got} vs {want}")
        gs = placevec.cross_count_self(A)
        ws = np.array([sum(segments_cross((tuple(row[i][0]), tuple(row[i][1])),
                                          (tuple(row[j][0]), tuple(row[j][1])))
                           for i in range(E) for j in range(i + 1, E))
                       for row in A])
        check("cross_self", (gs == ws).all(), f"{gs} vs {ws}")


ANGLES8 = (0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0)


def random_state(placer):
    bx0, by0, bx1, by1 = placer.b.outline
    centers = dict(placer.orig)
    rotations = {}
    for e in placer.movable:
        centers[e] = (random.uniform(bx0 + 2, bx1 - 2),
                      random.uniform(by0 + 2, by1 - 2))
        rotations[e] = float(random.choice(ANGLES8))
    return centers, rotations


def run_config(label, board, only, grid_step, **kw):
    placer = Placer(board, [], 0.1, 1.0, 2.0, only=only,
                    grid_step=grid_step, **kw)
    if len(placer.movable) < 2:
        print(f"[{label}] needs at least 2 movable parts "
              f"(matched {len(placer.movable)}) — skipping")
        return
    eng = placer._engine()

    # pad transforms: engine world positions vs _pad_global
    centers, rotations = random_state(placer)
    ca, da = eng.to_state(centers, rotations)
    for eid in random.sample(list(placer.b.elements),
                             min(10, len(placer.b.elements))):
        for p in placer.b.pads_of(eid):
            want = placer._pad_global(p, centers, rotations)
            i = eng.idx[eid]
            off = np.array([p.x, p.y]) - eng.orig[i]
            got = eng._pads_world(np.array([i]), off[None], ca, da)[0]
            check(f"{label} pad_global", np.allclose(got, want, atol=1e-9),
                  f"{eid} {got} vs {want}")

    # legality: _fits_batch vs _fits, anchor-only and everyone placed-sets
    for trial in range(300):
        eid = random.choice(placer.movable)
        i = eng.idx[eid]
        a = float(random.choice(ANGLES8))
        centers, rotations = random_state(placer)
        ca, da = eng.to_state(centers, rotations)
        if trial % 2:
            placed_idx = [j for j in range(eng.N) if not eng.is_mov[j]]
            placed = {e: centers[e] for e in placer.anchors}
        else:
            placed_idx = list(range(eng.N))
            placed = {e: c for e, c in centers.items() if e != eid}
        base = np.array(centers[eid])
        C = np.vstack([eng._snap_arr(base[None]
                                     + np.random.uniform(-8, 8, (40, 2))),
                       base[None]])
        da_t = da.copy()
        da_t[i] = a
        fctx = eng._fits_ctx(i, placed_idx, ca, da_t)
        got = eng._fits_batch(i, a, C, fctx)
        want = np.array([placer._fits(eid, (float(c[0]), float(c[1])),
                                      placed, {**rotations, eid: a})
                         for c in C])
        check(f"{label} fits", (got == want).all(),
              f"{eid} R{a:g} mismatch at {C[got != want][:3]}")

    # row-batched legality/cost (the angle-batched search path)
    for trial in range(60):
        eid = random.choice(placer.movable)
        i = eng.idx[eid]
        centers, rotations = random_state(placer)
        ca, da = eng.to_state(centers, rotations)
        placed_idx = list(range(eng.N)) if trial % 2 else \
            [j for j in range(eng.N) if not eng.is_mov[j]]
        placed = {e: centers[e] for e in placer.b.elements
                  if (eng.idx[e] in placed_idx) and e != eid}
        angles = list(ANGLES8)
        sctx = eng._score_ctx(i, ca, da)
        fctx = eng._fits_ctx(i, placed_idx, ca, da)
        sx = eng._search_ctx(i, angles, fctx, sctx)
        base = np.array(centers[eid])
        C = eng._snap_arr(base[None] + np.random.uniform(-8, 8, (60, 2)))
        aid = np.random.randint(0, len(angles), size=60)
        got = eng._fits_rows(i, C, aid, fctx, sx)
        want = np.array([placer._fits(eid, (float(c[0]), float(c[1])), placed,
                                      {**rotations, eid: angles[k]})
                         for c, k in zip(C, aid)])
        check(f"{label} fits_rows", (got == want).all(), f"{eid}")
        gc = eng._air_rows(i, C, aid, sx, sctx)
        wc = np.array([placer._part_cost_at(eid, (float(c[0]), float(c[1])),
                                            angles[k], centers, rotations)
                       for c, k in zip(C, aid)])
        check(f"{label} air_rows", np.allclose(gc, wc, atol=1e-8),
              f"{eid} max err {np.abs(gc - wc).max():g}")

    # all-angle closed forms vs their per-angle scalars
    for trial in range(40):
        eid = random.choice(placer.movable)
        i = eng.idx[eid]
        centers, rotations = random_state(placer)
        ca, da = eng.to_state(centers, rotations)
        sctx = eng._score_ctx(i, ca, da)
        angles = [k * 7.5 for k in range(48)]
        got_o = eng._orient_pen_all(angles, sctx)
        want_o = np.array([eng._orient_pen(i, a, sctx) for a in angles])
        check(f"{label} orient_pen_all",
              np.allclose(got_o, want_o, atol=1e-9),
              f"{eid} max err {np.abs(got_o - want_o).max():g}")
        got_i = eng._implied_all(angles, sctx)
        want_i = np.array([eng._implied_centre(i, a, sctx)
                           if eng._implied_centre(i, a, sctx) is not None
                           else (np.nan, np.nan) for a in angles])
        check(f"{label} implied_all",
              np.allclose(got_i, want_i, atol=1e-9, equal_nan=True),
              f"{eid}")

    # row-batched neighbor orientation vs summed scalar penalties
    for trial in range(30):
        eid = random.choice(placer.movable)
        i = eng.idx[eid]
        centers, rotations = random_state(placer)
        ca, da = eng.to_state(centers, rotations)
        angles = list(ANGLES8)
        C = np.random.uniform(5, 40, size=(15, 2))
        aid = np.random.randint(0, len(angles), size=15)
        got = eng._neighbor_orient_rows(i, C, aid, angles, ca, da)
        want = []
        for c, k in zip(C, aid):
            c2 = {**centers, eid: (float(c[0]), float(c[1]))}
            r2 = {**rotations, eid: angles[k]}
            tot = 0.0
            for j in eng._nbrs(i):
                ej = eng.eids[j]
                tot += placer.orientation_weight * \
                    placer._part_orientation_penalty_at(ej, r2[ej], c2, r2)
            want.append(tot)
        check(f"{label} nbr_orient_rows", np.allclose(got, want, atol=1e-8),
              f"{eid} max err {np.abs(got - np.array(want)).max():g}")

    # part cost vs scalar
    for trial in range(200):
        eid = random.choice(placer.movable)
        i = eng.idx[eid]
        a = float(random.choice(ANGLES8))
        centers, rotations = random_state(placer)
        ca, da = eng.to_state(centers, rotations)
        sctx = eng._score_ctx(i, ca, da)
        C = np.random.uniform(5, 40, size=(20, 2))
        got = eng._part_air_batch(i, a, C, sctx) + eng._orient_pen(i, a, sctx)
        want = np.array([placer._part_cost_at(eid, (float(c[0]), float(c[1])),
                                              a, centers, rotations)
                         for c in C])
        check(f"{label} part_cost", np.allclose(got, want, atol=1e-8),
              f"{eid} R{a:g} max err {np.abs(got - want).max():g}")

    # THE identity the quench rests on: Δ own-terms == Δ global-cost
    bx0, by0, bx1, by1 = placer.b.outline
    for trial in range(60):
        eid = random.choice(placer.movable)
        i = eng.idx[eid]
        centers, rotations = random_state(placer)
        ca, da = eng.to_state(centers, rotations)
        eng._segs = None
        sctx = eng._score_ctx(i, ca, da)
        inc = {n for n, _ in eng.part_nets[i]}
        fixed = eng._fixed_segs(inc, ca, da)
        a2 = float(random.choice(ANGLES8))
        c2 = (random.uniform(bx0 + 5, bx1 - 5), random.uniform(by0 + 5, by1 - 5))
        t1 = eng._own_terms(i, da[i], ca[i][None], sctx, fixed, ca, da)[0]
        t2 = eng._own_terms(i, a2, np.array(c2)[None], sctx, fixed, ca, da)[0]
        g1 = placer._global_cost(centers, rotations)
        g2 = placer._global_cost({**centers, eid: c2},
                                 {**rotations, eid: a2})
        check(f"{label} own_terms delta", abs((t2 - t1) - (g2 - g1)) < 1e-6,
              f"{eid}: dterms {t2 - t1:.9f} vs dglobal {g2 - g1:.9f}")

    # and the swap analogue: Δ pair-terms == Δ global-cost
    for trial in range(40):
        ei, ej = random.sample(placer.movable, 2)
        i, j = eng.idx[ei], eng.idx[ej]
        centers, rotations = random_state(placer)
        ca, da = eng.to_state(centers, rotations)
        eng._segs = None
        t1 = eng._pair_terms(i, j, ca, da)
        g1 = placer._global_cost(centers, rotations)
        ca2, da2 = ca.copy(), da.copy()
        ca2[i], ca2[j] = ca[j].copy(), ca[i].copy()
        da2[i], da2[j] = da[j], da[i]
        t2 = eng._pair_terms(i, j, ca2, da2)
        g2 = placer._global_cost(
            {**centers, ei: centers[ej], ej: centers[ei]},
            {**rotations, ei: rotations[ej], ej: rotations[ei]})
        check(f"{label} pair_terms delta", abs((t2 - t1) - (g2 - g1)) < 1e-6,
              f"{ei}/{ej}: dterms {t2 - t1:.9f} vs dglobal {g2 - g1:.9f}")

    print(f"[{label}] done")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", nargs="+", default=[], metavar="REF",
                    help="movable refs (globs ok); default: the live selection")
    ap.add_argument("--seed", type=int, default=7,
                    help="RNG seed for the randomized states (default: 7)")
    args = ap.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)

    bridge = FusionBridge().connect()
    only = args.only or read_selection(bridge)
    if not only:
        print("Nothing selected and no --only given — GROUP-select the parts "
              "to treat as movable, or pass --only REF...")
        sys.exit(2)
    board = read_board(bridge)
    from place import _read_main_grid_mm
    grid_step = _read_main_grid_mm(bridge) / 10.0

    check_primitives()
    run_config("default", board, only, grid_step)
    run_config("halo+edge", board, only, grid_step,
               halo_weight=1.0, halo_gap=0.5, edge_weight=1.0, edge_margin=2.0)

    print("ALL PASS" if FAIL == 0 else f"{FAIL} FAILURES")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
