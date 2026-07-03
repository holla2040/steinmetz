"""Vectorized (numpy) engine behind ``place.Placer`` — same objective, batched.

The scalar placer in ``place.py`` evaluates one candidate ``(centre, rotation)``
at a time: a Python-loop MST per net, a Python separating-axis test per placed
part. That is ~200 s for 23 parts at 45°. The optimization problem itself is
tiny — the cost is per-candidate Python overhead — so this module keeps the
exact objective and pipeline (seed -> spread -> settle -> legalize -> quench)
and evaluates candidates in numpy batches instead:

- **Batched Prim** (:func:`mst_len_batch` / :func:`mst_edges_batch`): the MST of
  one net is over <= 7 pads (>= 8 pins is classified power and ignored), so a
  whole block of candidate centres is solved together — K-1 numpy passes over
  an (M, K) distance matrix instead of M Python Prims.
- **Vectorized SAT legality** (:meth:`VecEngine._fits_batch`): placement
  polygons are rectangles, so each pair has 4 separating axes whose projections
  are affine in the candidate centre — legality of a whole candidate block
  against every placed part is a handful of array comparisons.
- **Cost-pruned search**: within a ring block, candidates are scored first
  (cheap) and the expensive legality test runs only on those that could beat
  the best-so-far. The search explores at least the rings the scalar version
  did (stop rule applied per block), so per-part results are equal or better.
- **Incremental quench cost**: moving one part changes only its own nets'
  airwire, the crossings involving those nets' segments, and the orientation
  terms of parts sharing a net with it. Everything else cancels out of the
  accept test, so the quench compares exactly those terms, batched.

Angle handling matches ``Placer._rot`` up to trig round-off (~1e-16 mm, far
below the 1e-6 snap epsilon and the 0.1 mm pad-position gate).
"""
from __future__ import annotations

import math

import numpy as np

_SAME_EPS = 1e-6          # matches place._same endpoint tolerance


def _rot_mat(deg: float) -> np.ndarray:
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s], [s, c]])


def _rotate(off: np.ndarray, deg: float) -> np.ndarray:
    """Rotate (K, 2) offsets by ``deg`` counter-clockwise (EAGLE ROTATE Rn)."""
    return off @ _rot_mat(deg).T


def mst_len_batch(pts: np.ndarray) -> np.ndarray:
    """Euclidean-MST lengths for a batch of point sets: (M, K, 2) -> (M,).

    Vectorized Prim: K is tiny (a net has < 8 pads), M is the candidate count,
    so each of the K-1 tree-growing steps is one numpy pass over (M, K).
    """
    M, K = pts.shape[0], pts.shape[1]
    total = np.zeros(M)
    if K < 2 or M == 0:
        return total
    if K == 2:                     # the dominant case: a 2-pin net
        d = pts[:, 0] - pts[:, 1]
        return np.hypot(d[:, 0], d[:, 1])
    if K == 3:                     # MST of a triangle = perimeter - longest side
        d0 = pts[:, 0] - pts[:, 1]
        d1 = pts[:, 0] - pts[:, 2]
        d2 = pts[:, 1] - pts[:, 2]
        a = np.hypot(d0[:, 0], d0[:, 1])
        b = np.hypot(d1[:, 0], d1[:, 1])
        c = np.hypot(d2[:, 0], d2[:, 1])
        return a + b + c - np.maximum(a, np.maximum(b, c))
    diff = pts[:, :, None, :] - pts[:, None, :, :]
    D = np.sqrt((diff * diff).sum(-1))                      # (M, K, K)
    rows = np.arange(M)
    dist = D[:, 0, :].copy()
    dist[:, 0] = np.inf
    used = np.zeros((M, K), dtype=bool)
    used[:, 0] = True
    for _ in range(K - 1):
        j = np.argmin(dist, axis=1)
        total += dist[rows, j]
        used[rows, j] = True
        dist = np.minimum(dist, D[rows, j, :])
        dist[used] = np.inf
    return total


def mst_edges_batch(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Batched Prim keeping parents: (M, K, 2) -> lengths (M,), edges (M, K-1, 2).

    The edges are the drawn airwire segments — needed when a candidate's
    crossings are scored, since the tree topology can change with the centre.
    """
    M, K = pts.shape[0], pts.shape[1]
    lengths = np.zeros(M)
    if K < 2 or M == 0:
        return lengths, np.zeros((M, 0, 2), dtype=np.int64)
    diff = pts[:, :, None, :] - pts[:, None, :, :]
    D = np.sqrt((diff * diff).sum(-1))
    rows = np.arange(M)
    dist = D[:, 0, :].copy()
    dist[:, 0] = np.inf
    parent = np.zeros((M, K), dtype=np.int64)
    used = np.zeros((M, K), dtype=bool)
    used[:, 0] = True
    edges = np.empty((M, K - 1, 2), dtype=np.int64)
    for s in range(K - 1):
        j = np.argmin(dist, axis=1)
        lengths += dist[rows, j]
        edges[:, s, 0] = parent[rows, j]
        edges[:, s, 1] = j
        used[rows, j] = True
        nd = D[rows, j, :]
        upd = nd < dist
        dist = np.where(upd, nd, dist)
        parent = np.where(upd, j[:, None], parent)
        dist[used] = np.inf
    return lengths, edges


def _seg_orients(p1, p2, p3, p4):
    """The four orientation predicates of the proper-crossing test, batched."""
    d34 = p4 - p3
    d12 = p2 - p1
    d1 = d34[..., 0] * (p1 - p3)[..., 1] - d34[..., 1] * (p1 - p3)[..., 0]
    d2 = d34[..., 0] * (p2 - p3)[..., 1] - d34[..., 1] * (p2 - p3)[..., 0]
    d3 = d12[..., 0] * (p3 - p1)[..., 1] - d12[..., 1] * (p3 - p1)[..., 0]
    d4 = d12[..., 0] * (p4 - p1)[..., 1] - d12[..., 1] * (p4 - p1)[..., 0]
    return d1, d2, d3, d4


def _share_endpoint(p1, p2, p3, p4):
    def same(u, v):
        return (np.abs(u[..., 0] - v[..., 0]) < _SAME_EPS) \
             & (np.abs(u[..., 1] - v[..., 1]) < _SAME_EPS)
    return same(p1, p3) | same(p1, p4) | same(p2, p3) | same(p2, p4)


def cross_count_ab(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """Proper-crossing counts: batch segments A (M, E, 2, 2) vs fixed B (F, 2, 2).

    Matches ``place.segments_cross``: strict straddle both ways, and segments
    sharing an endpoint (a net's star point) never count. A crossing needs
    overlapping segment bboxes, so the orientation predicates only run on the
    sparse set of bbox-overlapping pairs.
    """
    M, E = A.shape[0], A.shape[1]
    F = B.shape[0]
    if M == 0 or E == 0 or F == 0:
        return np.zeros(M, dtype=np.int64)
    Amin, Amax = A.min(axis=2), A.max(axis=2)              # (M, E, 2)
    Bmin, Bmax = B.min(axis=1), B.max(axis=1)              # (F, 2)
    ov = ((Amin[:, :, None, 0] <= Bmax[None, None, :, 0])
          & (Amax[:, :, None, 0] >= Bmin[None, None, :, 0])
          & (Amin[:, :, None, 1] <= Bmax[None, None, :, 1])
          & (Amax[:, :, None, 1] >= Bmin[None, None, :, 1]))
    idx = np.flatnonzero(ov.reshape(-1))
    if not len(idx):
        return np.zeros(M, dtype=np.int64)
    m = idx // (E * F)
    e = (idx // F) % E
    f = idx % F
    p1, p2 = A[m, e, 0], A[m, e, 1]
    p3, p4 = B[f, 0], B[f, 1]
    d1, d2, d3, d4 = _seg_orients(p1, p2, p3, p4)
    hit = (d1 * d2 < 0) & (d3 * d4 < 0) & ~_share_endpoint(p1, p2, p3, p4)
    return np.bincount(m[hit], minlength=M).astype(np.int64)


def cross_count_self(A: np.ndarray) -> np.ndarray:
    """Proper crossings among each batch row's own segments: (M, E, 2, 2) -> (M,)."""
    M, E = A.shape[0], A.shape[1]
    if M == 0 or E < 2:
        return np.zeros(M, dtype=np.int64)
    p1 = A[:, :, None, 0, :]
    p2 = A[:, :, None, 1, :]
    p3 = A[:, None, :, 0, :]
    p4 = A[:, None, :, 1, :]
    d1, d2, d3, d4 = _seg_orients(p1, p2, p3, p4)
    hit = (d1 * d2 < 0) & (d3 * d4 < 0) & ~_share_endpoint(p1, p2, p3, p4)
    hit &= np.triu(np.ones((E, E), dtype=bool), k=1)[None]
    return hit.sum(axis=(1, 2))


class VecEngine:
    """Array mirror of a :class:`place.Placer` — state is ``centers`` (N, 2)
    and ``deltas`` (N,) rotation deltas, indexed by element order."""

    def __init__(self, placer):
        self.p = placer
        b = placer.b
        self.eids = list(b.elements)
        self.idx = {e: i for i, e in enumerate(self.eids)}
        self.N = len(self.eids)
        self.orig = np.array([placer.orig[e] for e in self.eids], dtype=float)
        self.base_angle = np.array([b.elements[e].angle for e in self.eids],
                                   dtype=float)
        self.mov = [self.idx[e] for e in placer.movable]
        self.is_mov = np.zeros(self.N, dtype=bool)
        self.is_mov[self.mov] = True
        self.mirrored = np.array([e in placer.mirrored for e in self.eids])
        # local placement rects per element, (P, 4) [x1, y1, x2, y2]
        self.boxes = [np.array([[bb.x1, bb.y1, bb.x2, bb.y2]
                                for bb in placer._placement_boxes(e)])
                      for e in self.eids]
        # the scalar _bbox rect (placement_bbox) is NOT always the union of the
        # SAT boxes — the contact fallback pads it by 0.2 — so keep it separate
        # for the board-margin check and the spread step
        self.pb = np.array([b.placement_bbox(e) for e in self.eids])
        self.pins = np.array([len(b.pads_of(e)) for e in self.eids])
        self._geom_cache: dict[tuple[int, float], dict] = {}
        self.outline = np.array(b.outline, dtype=float)
        self.grid = placer.grid_step
        self.clearance = placer.clearance
        self.margin = placer.margin
        self._build_nets()
        self._segs = None            # per-net segment cache, dropped on commit
        self._ring_cache: dict[tuple[float, float], np.ndarray] = {}

    # ----- dict <-> array state ----------------------------------------------

    def to_state(self, centers, rotations):
        """Placer-style dicts -> array state (missing entries fall back to the
        original position / a 0 delta, matching the scalar ``.get`` defaults)."""
        ca = self.orig.copy()
        for e, c in centers.items():
            ca[self.idx[e]] = c
        da = np.zeros(self.N)
        if rotations:
            for e, a in rotations.items():
                da[self.idx[e]] = a
        return ca, da

    def to_dicts(self, centers, deltas):
        """Array state -> the ``(centers, rotations)`` dicts tools consume."""
        cd = {e: (float(centers[i, 0]), float(centers[i, 1]))
              for i, e in enumerate(self.eids)}
        rd = {e: float(deltas[self.idx[e]]) for e in self.p.movable}
        return cd, rd

    # ----- nets ------------------------------------------------------------

    def _build_nets(self):
        """Non-ignored nets as arrays: element index + local pad offset per pad.

        Offsets are ``pad - element origin`` in the board's current orientation,
        exactly the frame ``Placer._pad_global`` rotates by the delta.
        """
        p = self.p
        self.net_part: list[np.ndarray] = []
        self.net_off: list[np.ndarray] = []
        self.part_nets: list[list[tuple[int, np.ndarray]]] = \
            [[] for _ in range(self.N)]
        for sid, pads in p.net_pads.items():
            if sid in p.ignore:
                continue
            n = len(self.net_part)
            part = np.array([self.idx[q.element_id] for q in pads])
            off = np.array([[q.x, q.y] for q in pads]) - self.orig[part]
            self.net_part.append(part)
            self.net_off.append(off)
            for i in np.unique(part):
                self.part_nets[int(i)].append((n, part == i))
        self.n_nets = len(self.net_part)
        # parts sharing a net — the set whose cost terms a move of i touches
        self._net_adj: list[set[int]] = [set() for _ in range(self.N)]
        for n in range(self.n_nets):
            ps = {int(j) for j in np.unique(self.net_part[n])}
            for j in ps:
                self._net_adj[j] |= ps - {j}

    def _net_bbox(self, n, centers, deltas):
        """World bbox of one net's pads — every airwire segment of the net
        lives inside it, so it bounds where the net's segments can cross."""
        pts = self._pads_world(self.net_part[n], self.net_off[n],
                               centers, deltas)
        return (pts[:, 0].min(), pts[:, 1].min(),
                pts[:, 0].max(), pts[:, 1].max())

    @staticmethod
    def _boxes_touch(a, b):
        return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])

    def _pads_world(self, part, off, centers, deltas):
        a = np.radians(deltas[part])
        c, s = np.cos(a), np.sin(a)
        x = c * off[:, 0] - s * off[:, 1]
        y = s * off[:, 0] + c * off[:, 1]
        return centers[part] + np.stack([x, y], axis=-1)

    def _score_ctx(self, i, centers, deltas):
        """Per incident net of ``i``: (net, own local offsets, others' world pads).

        The others are frozen during ``i``'s search, so their world positions
        are computed once here — the scalar version recomputed them per
        candidate, which was most of the seed/settle cost.
        """
        ctx = []
        for n, own in self.part_nets[i]:
            part, off = self.net_part[n], self.net_off[n]
            others = self._pads_world(part[~own], off[~own], centers, deltas)
            ctx.append((n, off[own], others))
        return ctx

    def _part_air_batch(self, i, delta, C, sctx):
        """Airwire of ``i``'s incident nets with ``i`` at each centre in C (M, 2)."""
        M = len(C)
        total = np.zeros(M)
        for _n, own_off, others in sctx:
            ro = _rotate(own_off, delta)
            F, k = len(others), len(ro)
            pts = np.empty((M, F + k, 2))
            pts[:, :F] = others
            pts[:, F:] = C[:, None, :] + ro[None]
            total += mst_len_batch(pts)
        return total

    def _own_segs_batch(self, i, delta, C, sctx):
        """Airwire lengths and segment coordinates of ``i``'s nets per candidate:
        returns (lengths (M,), segs (M, E, 2, 2)) with E summed over nets."""
        M = len(C)
        lengths = np.zeros(M)
        segs = []
        for _n, own_off, others in sctx:
            ro = _rotate(own_off, delta)
            F, k = len(others), len(ro)
            if F + k < 2:
                continue
            pts = np.empty((M, F + k, 2))
            pts[:, :F] = others
            pts[:, F:] = C[:, None, :] + ro[None]
            ln, ed = mst_edges_batch(pts)
            lengths += ln
            rows = np.arange(M)[:, None, None]
            segs.append(pts[rows, ed])                      # (M, K-1, 2, 2)
        if segs:
            return lengths, np.concatenate(segs, axis=1)
        return lengths, np.zeros((M, 0, 2, 2))

    def _own_segs_rows(self, i, C, aid, sx, sctx):
        """Row-batched :meth:`_own_segs_batch`: row m has ``i`` at ``C[m]``
        rotated by angle id ``aid[m]``. One tree build per net covers every
        (offset, angle) cell at once."""
        M = len(C)
        segs = []
        for (_n, _own, others), ro in zip(sctx, sx['ro']):
            F, k = len(others), ro.shape[1]
            if F + k < 2:
                continue
            pts = np.empty((M, F + k, 2))
            pts[:, :F] = others
            pts[:, F:] = C[:, None, :] + ro[aid]
            _, ed = mst_edges_batch(pts)
            rows = np.arange(M)[:, None, None]
            segs.append(pts[rows, ed])
        if segs:
            return np.concatenate(segs, axis=1)
        return np.zeros((M, 0, 2, 2))

    def _orient_pen(self, i, delta, sctx):
        """``orientation_weight`` × the scalar 2-net orientation penalty.

        Constant in the candidate centre (it only compares directions), so it
        is computed once per (part, angle) and added as a scalar.
        """
        w = self.p.orientation_weight
        if not w or len(sctx) != 2:
            return 0.0
        own_c, oth_c = [], []
        for _n, own_off, others in sctx:
            if len(own_off) == 0 or len(others) == 0:
                return 0.0
            own_c.append(own_off.mean(axis=0))
            oth_c.append(others.mean(axis=0))
        ov = _rotate((own_c[1] - own_c[0])[None], delta)[0]
        tv = oth_c[1] - oth_c[0]
        olen = math.hypot(*ov)
        tlen = math.hypot(*tv)
        if olen < 1e-9 or tlen < 1e-9:
            return 0.0
        cosd = max(-1.0, min(1.0, float(ov @ tv) / (olen * tlen)))
        return w * olen * (1.0 - cosd)

    def _implied_centre(self, i, delta, sctx):
        """Centroid of the origins ``i``'s connections want (see scalar
        ``_implied_centers``), or None when nothing pulls it."""
        pts = []
        for _n, own_off, others in sctx:
            if len(own_off) == 0 or len(others) == 0:
                continue
            ro = _rotate(own_off, delta)
            pts.append((others[None, :, :] - ro[:, None, :]).reshape(-1, 2))
        if not pts:
            return None
        return np.concatenate(pts).mean(axis=0)

    def _implied_all(self, angles, sctx):
        """:meth:`_implied_centre` for every angle at once: the centroid is
        ``U - R(a)·V`` — affine in the rotation — so one cos/sin table covers
        the whole sweep. Returns (A, 2), NaN rows when nothing pulls."""
        U = np.zeros(2)
        V = np.zeros(2)
        P = 0
        for _n, own_off, others in sctx:
            K, F = len(own_off), len(others)
            if K == 0 or F == 0:
                continue
            U += K * others.sum(axis=0)
            V += F * own_off.sum(axis=0)
            P += K * F
        if P == 0:
            return np.full((len(angles), 2), np.nan)
        rad = np.radians(angles)
        c, s = np.cos(rad), np.sin(rad)
        rv = np.stack([c * V[0] - s * V[1], s * V[0] + c * V[1]], axis=-1)
        return (U[None] - rv) / P

    def _orient_pen_all(self, angles, sctx):
        """:meth:`_orient_pen` for every angle at once — only the rotated own
        vector changes with the angle (its length does not). Returns (A,)."""
        w = self.p.orientation_weight
        A = len(angles)
        if not w or len(sctx) != 2:
            return np.zeros(A)
        own_c, oth_c = [], []
        for _n, own_off, others in sctx:
            if len(own_off) == 0 or len(others) == 0:
                return np.zeros(A)
            own_c.append(own_off.mean(axis=0))
            oth_c.append(others.mean(axis=0))
        ov = own_c[1] - own_c[0]
        tv = oth_c[1] - oth_c[0]
        olen = math.hypot(*ov)
        tlen = math.hypot(*tv)
        if olen < 1e-9 or tlen < 1e-9:
            return np.zeros(A)
        rad = np.radians(angles)
        c, s = np.cos(rad), np.sin(rad)
        dots = (c * ov[0] - s * ov[1]) * tv[0] + (s * ov[0] + c * ov[1]) * tv[1]
        cosd = np.clip(dots / (olen * tlen), -1.0, 1.0)
        return w * olen * (1.0 - cosd)

    # ----- geometry / legality ----------------------------------------------

    def _geom(self, i, delta):
        """Rotated placement geometry for element ``i`` at rotation-delta
        ``delta``: polys (P, 4, 2), the two unique rect axes (P, 2, 2) with the
        self-projection interval per axis, the local AABB, and a bounding
        radius for the cheap pair prefilter. Total angle = base + delta,
        matching ``Placer._bbox``/``_polys``."""
        key = (i, round(float(delta) % 360.0, 6))
        g = self._geom_cache.get(key)
        if g is not None:
            return g
        R = _rot_mat(self.base_angle[i] + delta)
        bx = self.boxes[i]
        corners = np.stack([bx[:, [0, 1]], bx[:, [2, 1]],
                            bx[:, [2, 3]], bx[:, [0, 3]]], axis=1)    # (P,4,2)
        polys = corners @ R.T
        e1 = polys[:, 1] - polys[:, 0]
        e2 = polys[:, 2] - polys[:, 1]
        axes = np.stack([np.stack([-e1[:, 1], e1[:, 0]], axis=-1),
                         np.stack([-e2[:, 1], e2[:, 0]], axis=-1)], axis=1)
        # A zero-length edge normalizes to (0,0): its projections are all 0 and
        # can never separate, which is exactly the scalar skip of that axis.
        norm = np.linalg.norm(axes, axis=-1, keepdims=True)
        axes = np.divide(axes, norm, out=np.zeros_like(axes), where=norm > 1e-12)
        proj = np.einsum('pcd,pad->pca', polys, axes)                 # (P,4,A)
        x1, y1, x2, y2 = self.pb[i]
        pbc = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]]) @ R.T
        g = {
            'polys': polys,
            'axes': axes,
            'pmin': proj.min(axis=1),                                 # (P,A)
            'pmax': proj.max(axis=1),
            'bbox': np.array([pbc[:, 0].min(), pbc[:, 1].min(),
                              pbc[:, 0].max(), pbc[:, 1].max()]),
            'radius': float(np.linalg.norm(polys, axis=-1).max()),
        }
        self._geom_cache[key] = g
        return g

    def _fits_ctx(self, i, placed, centers, deltas):
        """World geometry of the placed set, split into the vector fast path
        (single-box parts, the common case) and a per-pair fallback."""
        singles, multis = [], []
        for j in placed:
            if j == i:
                continue
            gj = self._geom(j, deltas[j])
            if len(gj['polys']) == 1:
                singles.append((j, gj))
            else:
                multis.append((j, gj))
        ctx = {'multis': [(centers[j], gj) for j, gj in multis],
               'singles': [(centers[j], gj) for j, gj in singles]}
        if singles:
            q = np.array([centers[j] for j, _ in singles])            # (J,2)
            polys = np.stack([gj['polys'][0] for _, gj in singles])   # (J,4,2)
            axes = np.stack([gj['axes'][0] for _, gj in singles])     # (J,2,2)
            pmin = np.stack([gj['pmin'][0] for _, gj in singles])     # (J,2)
            pmax = np.stack([gj['pmax'][0] for _, gj in singles])
            qa = np.einsum('jd,jad->ja', q, axes)
            ctx['q'] = q
            ctx['world'] = polys + q[:, None, :]                      # (J,4,2)
            ctx['axes'] = axes
            ctx['fmin'] = pmin + qa                                   # (J,2)
            ctx['fmax'] = pmax + qa
            ctx['radius'] = np.array([gj['radius'] for _, gj in singles])
            # world AABBs: the scalar _fits AABB early-out is NOT just a
            # prefilter — for two rotated rects it contributes the x/y axes
            # the edge-normal SAT lacks, so it is part of the predicate
            ctx['wbb'] = np.stack([gj['bbox'] for _, gj in singles]) \
                + q[:, [0, 1, 0, 1]]                                  # (J,4)
        else:
            ctx['q'] = None
        return ctx

    @staticmethod
    def _aabb_sep(x0, y0, x1, y1, wbb, cl):
        """The scalar AABB early-out as a separation predicate: rows (moving
        AABB sides, each (M,)) vs placed world AABBs (J, 4) -> (M, J)."""
        return ((x1[:, None] + cl <= wbb[None, :, 0])
                | (wbb[None, :, 2] + cl <= x0[:, None])
                | (y1[:, None] + cl <= wbb[None, :, 1])
                | (wbb[None, :, 3] + cl <= y0[:, None]))

    def _fits_batch(self, i, delta, C, ctx):
        """Vectorized ``Placer._fits``: board margin + grid + no SAT overlap
        (with clearance) against every part in the fits context. C is (M, 2)."""
        C = np.asarray(C, dtype=float)
        M = len(C)
        g = self._geom(i, delta)
        cl = self.clearance
        x0, y0, x1, y1 = self.outline
        bb = g['bbox']
        ok = ((C[:, 0] + bb[0] >= x0 + self.margin)
              & (C[:, 1] + bb[1] >= y0 + self.margin)
              & (C[:, 0] + bb[2] <= x1 - self.margin)
              & (C[:, 1] + bb[3] <= y1 - self.margin))
        if self.grid and self.is_mov[i]:
            sn = np.round(np.round(C / self.grid) * self.grid, 6)
            ok &= (np.abs(C - sn) < 1e-6).all(axis=1)
        if not ok.any():
            return ok

        if ctx['q'] is not None and len(g['polys']) == 1:
            # Fast path: this part and the placed parts are single rects. For
            # every axis the projections are affine in the centre, so overlap
            # against all J parts is a few (M, J)-shaped comparisons.
            q, axes = ctx['q'], ctx['axes']
            J = len(q)
            rr = g['radius'] + ctx['radius'] + cl                     # (J,)
            near = ((np.abs(C[:, 0, None] - q[None, :, 0]) <= rr)
                    & (np.abs(C[:, 1, None] - q[None, :, 1]) <= rr))  # (M,J)
            near[~ok] = False
            if near.any():
                # the scalar AABB axes (see _fits_ctx: part of the predicate)
                sep = self._aabb_sep(C[:, 0] + bb[0], C[:, 1] + bb[1],
                                     C[:, 0] + bb[2], C[:, 1] + bb[3],
                                     ctx['wbb'], cl)
                # my two axes: my interval slides with C, theirs is fixed
                for a in range(2):
                    u = g['axes'][0, a]
                    t = C @ u                                          # (M,)
                    fw = ctx['world'] @ u                              # (J,4)
                    fmin, fmax = fw.min(axis=1), fw.max(axis=1)
                    sep |= (t[:, None] + g['pmax'][0, a] + cl <= fmin[None]) \
                         | (fmax[None] + cl <= t[:, None] + g['pmin'][0, a])
                # their two axes each: my rect's projection onto their axis
                mc = np.einsum('cd,jad->jac', g['polys'][0], axes)     # (J,2,4)
                mmin, mmax = mc.min(axis=2), mc.max(axis=2)            # (J,2)
                t = C @ axes.reshape(-1, 2).T                          # (M,2J)
                t = t.reshape(M, J, 2)
                sep |= ((t + mmax[None] + cl <= ctx['fmin'][None])
                        | (ctx['fmax'][None] + cl <= t + mmin[None])).any(axis=2)
                ok &= ~(near & ~sep).any(axis=1)
        elif ctx['q'] is not None:
            # multi-box mover: run each placed single through the general pair
            for q, gj in ctx['singles']:
                self._sat_pair(g, q, gj, C, ok, cl)
                if not ok.any():
                    return ok

        for q, gj in ctx['multis']:
            self._sat_pair(g, q, gj, C, ok, cl)
            if not ok.any():
                break
        return ok

    def _sat_pair(self, g, q, gj, C, ok, cl):
        """General SAT of the moving part ``g`` at centres C against one placed
        part ``gj`` at ``q`` — the multi-box fallback. Clears ``ok`` in place."""
        rr = g['radius'] + gj['radius'] + cl
        sel = np.flatnonzero(ok & (np.abs(C[:, 0] - q[0]) <= rr)
                             & (np.abs(C[:, 1] - q[1]) <= rr))
        if not len(sel):
            return
        Cs = C[sel]
        # part-level scalar AABB early-out (an extra separating-axis pair)
        wbb = gj['bbox'] + np.array([q[0], q[1], q[0], q[1]])
        aabb_sep = self._aabb_sep(Cs[:, 0] + g['bbox'][0],
                                  Cs[:, 1] + g['bbox'][1],
                                  Cs[:, 0] + g['bbox'][2],
                                  Cs[:, 1] + g['bbox'][3],
                                  wbb[None], cl)[:, 0]
        bad = np.zeros(len(sel), dtype=bool)
        for p in range(len(g['polys'])):
            for b in range(len(gj['polys'])):
                ov = np.ones(len(Cs), dtype=bool)
                wb = gj['polys'][b] + q                               # (4,2)
                for a in range(2):                                    # my axes
                    u = g['axes'][p, a]
                    t = Cs @ u
                    fw = wb @ u
                    ov &= ~((t + g['pmax'][p, a] + cl <= fw.min())
                            | (fw.max() + cl <= t + g['pmin'][p, a]))
                for a in range(2):                                    # their axes
                    u = gj['axes'][b, a]
                    t = Cs @ u
                    mc = g['polys'][p] @ u
                    fmin = gj['pmin'][b, a] + q @ u
                    fmax = gj['pmax'][b, a] + q @ u
                    ov &= ~((t + mc.max() + cl <= fmin)
                            | (fmax + cl <= t + mc.min()))
                bad |= ov
        ok[sel[bad & ~aabb_sep]] = False

    # ----- shared helpers ----------------------------------------------------

    def _snap_arr(self, C):
        if not self.grid:
            return np.asarray(C, dtype=float)
        return np.round(np.round(np.asarray(C, dtype=float) / self.grid)
                        * self.grid, 6)

    @staticmethod
    def _unique_ordered(C, rids=None):
        """Drop duplicate rows keeping first occurrence and original order —
        the vector version of the scalar per-ring ``seen`` set."""
        _, first = np.unique(C, axis=0, return_index=True)
        keep = np.sort(first)
        if rids is None:
            return C[keep]
        return C[keep], rids[keep]

    def _ring(self, ox, oy, r, step):
        if r <= 0:
            return np.array([[ox, oy]])
        k = max(8, math.ceil(2 * math.pi * r / step))
        t = np.arange(k) * (2 * math.pi / k)
        return np.stack([ox + r * np.cos(t), oy + r * np.sin(t)], axis=-1)

    def _unit_rings(self, radii, step):
        """Origin-centred ring points for a block of radii, with each point's
        ring radius. Cached per (radius, step): every angle in every search
        walks the same radius grid, only the centre differs."""
        offs, rids = [], []
        for r in radii:
            key = (round(r, 9), step)
            ring = self._ring_cache.get(key)
            if ring is None:
                ring = self._ring(0.0, 0.0, r, step)
                self._ring_cache[key] = ring
            offs.append(ring)
            rids.append(np.full(len(ring), r))
        return np.concatenate(offs), np.concatenate(rids)

    def _diag(self):
        x0, y0, x1, y1 = self.outline
        return math.hypot(x1 - x0, y1 - y0)

    def _order(self, centers=None):
        """Movable indices, most-constrained first (scalar ``_placement_order``)."""
        return [self.idx[e] for e in self.p._placement_order()]

    def _name(self, i):
        return self.p.b.elements[self.eids[i]].name

    def _angles_for(self, i, angle_step):
        return [0.0] if self.mirrored[i] else self.p._angle_steps(angle_step)

    # ----- the per-part search (vectorized _optimal_region) ------------------

    def _search_ctx(self, i, angles, fctx, sctx):
        """Per-angle constants for the angle-batched search: rotated geometry,
        rotated own pad offsets, orientation penalty, ideal centre, and the
        SAT projection bounds of every placed single-box part on this part's
        (angle-dependent) axes. All independent of the candidate centre, so
        they are computed once and gathered per candidate row by angle id."""
        A = len(angles)
        sx = {
            'angles': angles,
            'axes': np.zeros((A, 2, 2)),
            'pmin': np.zeros((A, 2)), 'pmax': np.zeros((A, 2)),
            'bbox': np.zeros((A, 4)),
            'radius': np.zeros(A),
            'multi': [],                       # multi-box angle fallback flags
            'ro': [],                          # per net: (A, k, 2) rotated own
            'opv': np.zeros(A),
            'ideal': np.full((A, 2), np.nan),
        }
        single_me = len(self.boxes[i]) == 1
        for ai, a in enumerate(angles):
            g = self._geom(i, a)
            sx['bbox'][ai] = g['bbox']
            sx['radius'][ai] = g['radius']
            if single_me:
                sx['axes'][ai] = g['axes'][0]
                sx['pmin'][ai] = g['pmin'][0]
                sx['pmax'][ai] = g['pmax'][0]
        sx['opv'] = self._orient_pen_all(angles, sctx)
        sx['ideal'] = self._implied_all(angles, sctx)
        sx['single'] = single_me
        for _n, own_off, _others in sctx:
            ro = np.stack([_rotate(own_off, a) for a in angles])
            sx['ro'].append(ro)
        if single_me and fctx['q'] is not None:
            # fixed parts' projection interval on each of my per-angle axes
            fw = np.einsum('jcd,abd->abjc', fctx['world'], sx['axes'])
            sx['jfmin'] = fw.min(axis=3)                       # (A, 2, J)
            sx['jfmax'] = fw.max(axis=3)
            # my polys' projection interval on each fixed part's axes
            mypolys = np.stack([self._geom(i, a)['polys'][0] for a in angles])
            mc = np.einsum('acd,jbd->ajbc', mypolys, fctx['axes'])
            sx['mmin'] = mc.min(axis=3)                        # (A, J, 2)
            sx['mmax'] = mc.max(axis=3)
        return sx

    def _fits_rows(self, i, C, aid, fctx, sx):
        """Row-batched fits: candidate row m sits at C[m] with the angle
        ``sx['angles'][aid[m]]`` — one call legality-tests a whole lockstep
        block across every angle."""
        M = len(C)
        cl = self.clearance
        x0, y0, x1, y1 = self.outline
        bb = sx['bbox'][aid]                                   # (M, 4)
        ok = ((C[:, 0] + bb[:, 0] >= x0 + self.margin)
              & (C[:, 1] + bb[:, 1] >= y0 + self.margin)
              & (C[:, 0] + bb[:, 2] <= x1 - self.margin)
              & (C[:, 1] + bb[:, 3] <= y1 - self.margin))
        if self.grid and self.is_mov[i]:
            sn = np.round(np.round(C / self.grid) * self.grid, 6)
            ok &= (np.abs(C - sn) < 1e-6).all(axis=1)
        if not ok.any():
            return ok
        if not sx['single']:
            # multi-box mover: fall back to the per-angle batch path
            for ai in range(len(sx['angles'])):
                rows = np.flatnonzero(ok & (aid == ai))
                if len(rows):
                    ok[rows] = self._fits_batch(i, sx['angles'][ai],
                                                C[rows], fctx)
            return ok
        if fctx['q'] is not None:
            q = fctx['q']
            # block-level trim: parts no candidate row can reach contribute
            # nothing but (M, J) columns — drop them before the row math
            myr = float(sx['radius'].max())
            reach = myr + fctx['radius'] + cl
            cmin, cmax = C.min(axis=0), C.max(axis=0)
            jj = np.flatnonzero((q[:, 0] >= cmin[0] - reach)
                                & (q[:, 0] <= cmax[0] + reach)
                                & (q[:, 1] >= cmin[1] - reach)
                                & (q[:, 1] <= cmax[1] + reach))
        if fctx['q'] is not None and len(jj):
            q = fctx['q'][jj]
            rr = sx['radius'][aid, None] + fctx['radius'][None, jj] + cl
            near = ((np.abs(C[:, 0, None] - q[None, :, 0]) <= rr)
                    & (np.abs(C[:, 1, None] - q[None, :, 1]) <= rr))
            near[~ok] = False
            if near.any():
                # the scalar AABB axes (see _fits_ctx), per-row moving bbox
                sep = self._aabb_sep(C[:, 0] + bb[:, 0], C[:, 1] + bb[:, 1],
                                     C[:, 0] + bb[:, 2], C[:, 1] + bb[:, 3],
                                     fctx['wbb'][jj], cl)
                axesM = sx['axes'][aid]                        # (M, 2, 2)
                t = np.einsum('md,mad->ma', C, axesM)          # (M, 2)
                lo = t + sx['pmin'][aid]
                hi = t + sx['pmax'][aid]
                sep |= ((hi[:, :, None] + cl <= sx['jfmin'][aid][:, :, jj])
                        | (sx['jfmax'][aid][:, :, jj] + cl
                           <= lo[:, :, None])).any(axis=1)
                tj = (C @ fctx['axes'][jj].reshape(-1, 2).T).reshape(M, -1, 2)
                sep |= ((tj + sx['mmax'][aid][:, jj] + cl
                         <= fctx['fmin'][None, jj])
                        | (fctx['fmax'][None, jj] + cl
                           <= tj + sx['mmin'][aid][:, jj])).any(axis=2)
                ok &= ~(near & ~sep).any(axis=1)
        if fctx['multis']:                                      # rare fallback
            for ai in range(len(sx['angles'])):
                rows = np.flatnonzero(ok & (aid == ai))
                if not len(rows):
                    continue
                sub = ok[rows].copy()
                g = self._geom(i, sx['angles'][ai])
                for q, gj in fctx['multis']:
                    self._sat_pair(g, q, gj, C[rows], sub, cl)
                ok[rows] = sub
        return ok

    def _air_rows(self, i, C, aid, sx, sctx):
        """Row-batched part airwire + the per-angle orientation penalty."""
        M = len(C)
        total = sx['opv'][aid].copy()
        for (_n, _own, others), ro in zip(sctx, sx['ro']):
            F, k = len(others), ro.shape[1]
            pts = np.empty((M, F + k, 2))
            pts[:, :F] = others
            pts[:, F:] = C[:, None, :] + ro[aid]
            total += mst_len_batch(pts)
        return total

    def search(self, i, centers, deltas, placed, pos_step, angle_step, span,
               max_disp, progress=None):
        """Best legal ``(delta, centre, part-cost)`` for ``i`` against ``placed``.

        The scalar ``_optimal_region`` ring search, batched two ways: within a
        radius block (candidates scored by batched Prim, then only the ones
        that could beat the best-so-far legality-tested) and across rotations
        (every angle walks its rings in lockstep through shared numpy calls —
        what makes ``--rotate 1``'s 360 angles tractable). Each angle keeps its
        own scalar stop rule (``span`` mm past its first legal ring, applied at
        block granularity), so the explored rings are a superset of the scalar
        search and the optimum found is equal or better.
        """
        o0 = self.orig[i]
        board_disc = tuple(self.outline)
        sctx = self._score_ctx(i, centers, deltas)
        fctx = self._fits_ctx(i, placed, centers, deltas)
        if not sctx:                                   # nothing pulls it
            c0 = self._snap_arr(o0[None])[0]
            if self._fits_batch(i, 0.0, c0[None], fctx)[0]:
                return 0.0, (float(c0[0]), float(c0[1])), 0.0, board_disc
            a, c = self.nearest_legal(i, o0, centers, deltas, placed,
                                      angle_step, max_disp)
            return a, c, 0.0, board_disc
        angles = self._angles_for(i, angle_step)
        sx = self._search_ctx(i, angles, fctx, sctx)
        r_cap = self._diag() if max_disp is None else min(self._diag(), max_disp)
        live = [ai for ai in range(len(angles))
                if not np.isnan(sx['ideal'][ai, 0])]
        r_stop = np.full(len(angles), r_cap)
        r_first = np.full(len(angles), np.nan)
        best = (math.inf, None, None)
        disc = None                    # bbox of every candidate tested
        r = 0.0
        while live:
            if progress:
                progress.tick(f"searching {self._name(i)} r={r:g}")
            # one lockstep block: the next few rings of every live angle,
            # built as a single broadcast of shared unit rings over the
            # per-angle ideal centres. Blocks stay small while any angle is
            # still hunting its first legal ring (those rows are all
            # legality-tested); once every live angle prunes by cost, bigger
            # blocks just amortize the numpy overhead.
            hunting = any(np.isnan(r_first[ai]) for ai in live)
            target = 384 if hunting else 4096
            n_rings = max(1, int(math.ceil(
                target / max(1, len(live) * max(8, 2 * math.pi * r / pos_step)))))
            radii = [r + k * pos_step for k in range(n_rings)]
            offs, orid = self._unit_rings(radii, pos_step)
            live_arr = np.array(live)
            keep2d = orid[None, :] <= r_stop[live_arr][:, None] + pos_step + 1e-9
            r += n_rings * pos_step
            live = [ai for ai in live if r <= r_stop[ai] + pos_step + 1e-9]
            if not keep2d.any():
                continue
            Q = len(offs)
            flat = keep2d.reshape(-1)
            C = (sx['ideal'][live_arr][:, None, :]
                 + offs[None]).reshape(-1, 2)[flat]
            aid = np.repeat(live_arr, Q)[flat]
            rid = np.tile(orid, len(live_arr))[flat]
            C = self._snap_arr(C)
            if self.grid and self.grid >= pos_step / 2:
                # coarse grids collapse neighbouring ring points onto one cell;
                # drop the duplicates (the scalar per-ring seen set). On the
                # default fine grid ring spacing >> grid so nothing collides.
                key = np.column_stack([C, aid, rid])
                _, first = np.unique(key, axis=0, return_index=True)
                keep = np.sort(first)
                C, aid, rid = C[keep], aid[keep], rid[keep]
            if max_disp is not None:
                m = np.hypot(C[:, 0] - o0[0], C[:, 1] - o0[1]) <= max_disp + 1e-9
                C, aid, rid = C[m], aid[m], rid[m]
            if not len(C):
                continue
            cmin, cmax = C.min(axis=0), C.max(axis=0)
            if disc is None:
                disc = [cmin[0], cmin[1], cmax[0], cmax[1]]
            else:
                disc = [min(disc[0], cmin[0]), min(disc[1], cmin[1]),
                        max(disc[2], cmax[0]), max(disc[3], cmax[1])]
            if np.isnan(r_first[live_arr]).any():
                # hunting for first legal rings: everything must be
                # legality-tested anyway, so test first and score only the
                # legal rows (near a dense anchor most rows are illegal)
                legal = self._fits_rows(i, C, aid, fctx, sx)
                li = np.flatnonzero(legal)
                if not len(li):
                    continue
                costs = np.full(len(C), np.inf)
                costs[li] = self._air_rows(i, C[li], aid[li], sx, sctx)
            else:
                # every angle prunes by cost now: score first, and only
                # would-be improvements pay for the legality test
                costs = self._air_rows(i, C, aid, sx, sctx)
                cut = costs < best[0] - 1e-12
                if not cut.any():
                    continue
                legal = np.zeros(len(C), dtype=bool)
                legal[cut] = self._fits_rows(i, C[cut], aid[cut], fctx, sx)
                li = np.flatnonzero(legal)
                if not len(li):
                    continue
            for ai in np.unique(aid[li]):
                if np.isnan(r_first[ai]):
                    r_first[ai] = rid[li[aid[li] == ai][0]]
                    r_stop[ai] = min(r_cap, r_first[ai] + span)
            k = li[np.argmin(costs[li])]
            if costs[k] < best[0] - 1e-12:
                best = (float(costs[k]), angles[int(aid[k])],
                        (float(C[k, 0]), float(C[k, 1])))
        if best[1] is None:
            a, c = self.nearest_legal(i, o0, centers, deltas, placed,
                                      angle_step, max_disp)
            sc = self._part_air_batch(i, a, np.array([c]), sctx)[0] \
                + self._orient_pen(i, a, sctx)
            return a, c, float(sc), board_disc
        return best[1], best[2], best[0], tuple(disc) if disc else board_disc

    def nearest_legal(self, i, origin, centers, deltas, placed, angle_step,
                      max_disp=None):
        """Nearest legal (angle, centre) ignoring airwire — the safety net.
        Ring spacing is the scalar's fixed 0.5 mm; candidate order within a
        ring is position-major then angle, matching the scalar loop."""
        cand = self._angles_for(i, angle_step)
        fctx = self._fits_ctx(i, placed, centers, deltas)
        r_cap = self._diag() if max_disp is None else min(self._diag(), max_disp)
        r = 0.0
        while r <= r_cap:
            C = self._unique_ordered(self._snap_arr(
                self._ring(origin[0], origin[1], r, 0.5)))
            legal = np.stack([self._fits_batch(i, a, C, fctx) for a in cand],
                             axis=1)                                   # (M,A)
            if legal.any():
                flat = int(legal.reshape(-1).argmax())
                mi, ai = divmod(flat, len(cand))
                return cand[ai], (float(C[mi, 0]), float(C[mi, 1]))
            r += 0.5
        c = self._snap_arr(np.asarray(origin, dtype=float)[None])[0]
        return 0.0, (float(c[0]), float(c[1]))

    # ----- the placement phases ----------------------------------------------

    def place(self, pos_step, angle_step, span, max_disp, progress=None):
        """Seed -> spread -> settle -> legalize -> settle -> legalize, exactly
        the scalar :meth:`place.Placer.place` pipeline. Returns array state."""
        centers = self.orig.copy()
        deltas = np.zeros(self.N)
        anchors = [j for j in range(self.N) if not self.is_mov[j]]
        order = self._order()
        if progress:
            progress.log(f"seeding {len(order)} part(s)")
        for k, i in enumerate(order, 1):
            if progress:
                progress.tick(f"seeding {k}/{len(order)} {self._name(i)}")
            a, c, _, _ = self.search(i, centers, deltas, anchors, pos_step,
                                     angle_step, span, max_disp, progress)
            deltas[i] = a
            centers[i] = c
        if progress:
            progress.log("spreading overlaps")
        self.spread(centers, deltas)
        settle_span = max(span, 8.0)
        self.settle(centers, deltas, pos_step, angle_step, settle_span,
                    max_disp, progress=progress)
        self.legalize(centers, deltas, angle_step, progress=progress)
        self.settle(centers, deltas, pos_step, angle_step, settle_span,
                    max_disp, progress=progress)
        self.legalize(centers, deltas, angle_step, progress=progress)
        return centers, deltas

    def spread(self, centers, deltas, iters=600):
        """Scalar ``_spread`` with precomputed extents: minimal-displacement
        AABB pushes between movable pairs, then a board clamp. Gauss-Seidel —
        each push is applied before the next pair is tested — because the
        simultaneous variant makes dense clusters slosh. Mutates ``centers``."""
        mv = self.mov
        g = self.clearance
        x0, y0, x1, y1 = self.outline
        # bbox = centre + mid ± half, fixed per part while rotations are frozen
        mid = np.zeros((len(mv), 2))
        half = np.zeros((len(mv), 2))
        for k, i in enumerate(mv):
            bb = self._geom(i, deltas[i])['bbox']
            mid[k] = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)
            half[k] = ((bb[2] - bb[0]) / 2, (bb[3] - bb[1]) / 2)
        for _ in range(iters):
            moved = False
            for ii in range(len(mv)):
                for jj in range(ii + 1, len(mv)):
                    ci = centers[mv[ii]] + mid[ii]
                    cj = centers[mv[jj]] + mid[jj]
                    ox = (half[ii, 0] + half[jj, 0] + g) - abs(ci[0] - cj[0])
                    oy = (half[ii, 1] + half[jj, 1] + g) - abs(ci[1] - cj[1])
                    if ox > 1e-6 and oy > 1e-6:
                        moved = True
                        if ox <= oy:
                            s = ox / 2 + 1e-9
                            sign = 1.0 if ci[0] >= cj[0] else -1.0
                            centers[mv[ii], 0] += sign * s
                            centers[mv[jj], 0] -= sign * s
                        else:
                            s = oy / 2 + 1e-9
                            sign = 1.0 if ci[1] >= cj[1] else -1.0
                            centers[mv[ii], 1] += sign * s
                            centers[mv[jj], 1] -= sign * s
            for k, i in enumerate(mv):                       # keep on board
                lo = centers[i] + mid[k] - half[k]
                hi = centers[i] + mid[k] + half[k]
                dx = dy = 0.0
                if lo[0] < x0 + self.margin:
                    dx = (x0 + self.margin) - lo[0]
                elif hi[0] > x1 - self.margin:
                    dx = (x1 - self.margin) - hi[0]
                if lo[1] < y0 + self.margin:
                    dy = (y0 + self.margin) - lo[1]
                elif hi[1] > y1 - self.margin:
                    dy = (y1 - self.margin) - hi[1]
                if dx or dy:
                    centers[i] += (dx, dy)
            if not moved:
                break

    def settle(self, centers, deltas, pos_step, angle_step, span, max_disp,
               passes=25, progress=None):
        """Coordinate descent (scalar ``_settle``): re-search each part against
        all the others, keep strict part-cost improvements. Mutates state.

        A part's search depends only on the *other* parts' state, so a part is
        re-searched only when something it can see moved since its last search:
        a net-mate (changes its cost terms), or any part whose old/new
        footprint touches the disc of candidates the search tested (changes
        its legality). Skipping a clean part is exact — re-searching it would
        reproduce the same result and accept nothing.
        """
        order = self._order()
        everyone = list(range(self.N))
        dirty = set(order)
        disc: dict[int, tuple] = {}
        for p in range(passes):
            if progress:
                progress.tick(f"settling pass {p + 1}/{passes}")
            moved = False
            for i in order:
                if i not in dirty:
                    continue
                dirty.discard(i)
                sctx = self._score_ctx(i, centers, deltas)
                before = self._part_air_batch(i, deltas[i], centers[i][None],
                                              sctx)[0] \
                    + self._orient_pen(i, deltas[i], sctx)
                a, c, cost, dsc = self.search(i, centers, deltas, everyone,
                                              pos_step, angle_step, span,
                                              max_disp, progress)
                pad = self._geom(i, deltas[i])['radius'] + self.clearance
                disc[i] = (dsc[0] - pad, dsc[1] - pad,
                           dsc[2] + pad, dsc[3] + pad)
                if cost < before - 1e-9:
                    old = (float(centers[i, 0]), float(centers[i, 1]))
                    centers[i] = c
                    deltas[i] = a
                    moved = True
                    self._mark_dirty(i, [old, c], order, dirty, disc)
            if not moved:
                break

    def _mark_dirty(self, i, spots, order, dirty, disc):
        """Part ``i`` sat/lands at ``spots``: re-mark every part that could
        see the change — net-mates, and parts whose last-tested candidate disc
        one of the footprints touches. |corner| is rotation-invariant, so one
        radius covers every angle."""
        r = self._geom(i, 0.0)['radius']
        foots = [(cx - r, cy - r, cx + r, cy + r) for cx, cy in spots]
        for j in order:
            if j == i or j in dirty:
                continue
            if j in self._net_adj[i]:
                dirty.add(j)
                continue
            dj = disc.get(j)
            if dj is None or any(self._boxes_touch(dj, f) for f in foots):
                dirty.add(j)

    def legalize(self, centers, deltas, angle_step, passes=40, progress=None):
        """Force overlapping parts to their nearest legal spot (scalar
        ``_legalize``) — the guaranteed no-overlap output Fusion's MOVE needs."""
        order = self._order()
        everyone = list(range(self.N))
        for p in range(passes):
            if progress:
                progress.tick(f"legalizing pass {p + 1}/{passes}")
            moved = False
            for i in order:
                fctx = self._fits_ctx(i, everyone, centers, deltas)
                if self._fits_batch(i, deltas[i], centers[i][None], fctx)[0]:
                    continue
                a, c = self.nearest_legal(i, centers[i], centers, deltas,
                                          everyone, angle_step)
                centers[i] = c
                deltas[i] = a
                moved = True
            if not moved:
                break
        return centers, deltas

    # ----- quench (vectorized improve) ----------------------------------------

    def _segments(self, centers, deltas):
        """Per-net airwire segments at the committed state, cached until the
        next accepted move — only the moved part's nets ever go stale."""
        if self._segs is None:
            segs = []
            for n in range(self.n_nets):
                pts = self._pads_world(self.net_part[n], self.net_off[n],
                                       centers, deltas)
                if len(pts) < 2:
                    segs.append(np.zeros((0, 2, 2)))
                    continue
                _, ed = mst_edges_batch(pts[None])
                segs.append(pts[ed[0]])
            self._segs = segs
        return self._segs

    def _fixed_segs(self, exclude_nets, centers, deltas):
        segs = self._segments(centers, deltas)
        keep = [segs[n] for n in range(self.n_nets) if n not in exclude_nets]
        if keep:
            return np.concatenate(keep)
        return np.zeros((0, 2, 2))

    def _nbrs(self, i):
        """Movable 2-net parts sharing a net with ``i`` — the ones whose
        orientation penalty shifts when ``i`` moves (their "others" centroid
        includes ``i``'s pads)."""
        if not self.p.orientation_weight:
            return []
        out = []
        my_nets = {n for n, _ in self.part_nets[i]}
        for j in self.mov:
            if j == i or len(self.part_nets[j]) != 2:
                continue
            if any(n in my_nets for n, _ in self.part_nets[j]):
                out.append(j)
        return out

    def _neighbor_orient_batch(self, i, delta, C, centers, deltas):
        """Sum of the neighbors' orientation penalties with ``i`` at each C.

        A neighbor's "others" centroid is affine in ``i``'s centre (the fixed
        pads sum plus ``i``'s rotated pads), so each neighbor is a couple of
        vector ops over the whole candidate block.
        """
        w = self.p.orientation_weight
        M = len(C)
        total = np.zeros(M)
        for j in self._nbrs(i):
            cents = []
            own_c = []
            dead = False
            for n, own in self.part_nets[j]:
                part, off = self.net_part[n], self.net_off[n]
                if not own.any():
                    dead = True
                    break
                own_c.append(off[own].mean(axis=0))
                po, oo = part[~own], off[~own]
                if len(po) == 0:
                    dead = True
                    break
                mi = po == i
                fixed = self._pads_world(po[~mi], oo[~mi], centers, deltas)
                ro = _rotate(oo[mi], delta)
                ki = len(ro)
                base = (fixed.sum(axis=0) if len(fixed) else np.zeros(2)) \
                    + (ro.sum(axis=0) if ki else np.zeros(2))
                cents.append((base[None] + ki * C) / (len(po)))
            if dead:
                continue
            ov = _rotate((own_c[1] - own_c[0])[None], deltas[j])[0]
            olen = math.hypot(*ov)
            if olen < 1e-9:
                continue
            tv = cents[1] - cents[0]                                  # (M,2)
            tlen = np.hypot(tv[:, 0], tv[:, 1])
            cosd = np.clip((tv @ ov) / np.maximum(tlen * olen, 1e-300), -1, 1)
            total += np.where(tlen < 1e-9, 0.0, w * olen * (1.0 - cosd))
        return total

    def _halo_edge_batch(self, i, delta, C, centers, deltas):
        """The optional spreading terms of ``i`` at each C: halo pairs
        involving ``i`` plus ``i``'s board-edge shortfall. Zero when both
        weights are off (the default)."""
        if not self.p.halo_weight and not self.p.edge_weight:
            return np.zeros(len(C))
        bb = np.broadcast_to(self._geom(i, delta)['bbox'], (len(C), 4))
        return self._halo_edge_core(i, bb, C, centers, deltas)

    def _halo_edge_core(self, i, bb, C, centers, deltas):
        p = self.p
        M = len(C)
        total = np.zeros(M)
        a0x, a0y = C[:, 0] + bb[:, 0], C[:, 1] + bb[:, 1]
        a1x, a1y = C[:, 0] + bb[:, 2], C[:, 1] + bb[:, 3]
        if p.halo_weight:
            for j in self.mov:
                if j == i:
                    continue
                ob = self._geom(j, deltas[j])['bbox'] \
                    + np.array([centers[j, 0], centers[j, 1],
                                centers[j, 0], centers[j, 1]])
                req = self.clearance + p.halo_gap * math.sqrt(
                    min(self.pins[i], self.pins[j]))
                dx = np.maximum(a0x - ob[2], ob[0] - a1x)
                dy = np.maximum(a0y - ob[3], ob[1] - a1y)
                sep = np.where((dx > 0) & (dy > 0), np.hypot(dx, dy),
                               np.maximum(dx, dy))
                short = np.where(sep < req, req - np.maximum(sep, 0.0), 0.0)
                total += p.halo_weight * short * short
        if p.edge_weight:
            x0, y0, x1, y1 = self.outline
            for gp in (a0x - x0, a0y - y0, x1 - a1x, y1 - a1y):
                short = np.where(gp < p.edge_margin,
                                 p.edge_margin - np.maximum(gp, 0.0), 0.0)
                total += p.edge_weight * short * short
        return total

    def _neighbor_orient_rows(self, i, C, aid, angles, centers, deltas):
        """Row-batched :meth:`_neighbor_orient_batch`: row m has ``i`` at
        ``C[m]`` rotated by ``angles[aid[m]]``. Rotation is linear, so the sum
        of ``i``'s rotated pads is the rotated sum — one (A, 2) table gathered
        per row."""
        w = self.p.orientation_weight
        total = np.zeros(len(C))
        if not w:
            return total
        for j in self._nbrs(i):
            cents = []
            own_c = []
            dead = False
            for n, own in self.part_nets[j]:
                part, off = self.net_part[n], self.net_off[n]
                if not own.any():
                    dead = True
                    break
                own_c.append(off[own].mean(axis=0))
                po, oo = part[~own], off[~own]
                if len(po) == 0:
                    dead = True
                    break
                mi = po == i
                fixed = self._pads_world(po[~mi], oo[~mi], centers, deltas)
                fsum = fixed.sum(axis=0) if len(fixed) else np.zeros(2)
                ki = int(mi.sum())
                if ki:
                    osum = oo[mi].sum(axis=0)
                    rsums = np.stack([_rotate(osum[None], a)[0] for a in angles])
                    base = fsum[None] + rsums[aid]
                else:
                    base = np.broadcast_to(fsum, (len(C), 2))
                cents.append((base + ki * C) / len(po))
            if dead:
                continue
            ov = _rotate((own_c[1] - own_c[0])[None], deltas[j])[0]
            olen = math.hypot(*ov)
            if olen < 1e-9:
                continue
            tv = cents[1] - cents[0]
            tlen = np.hypot(tv[:, 0], tv[:, 1])
            cosd = np.clip((tv @ ov) / np.maximum(tlen * olen, 1e-300), -1, 1)
            total += np.where(tlen < 1e-9, 0.0, w * olen * (1.0 - cosd))
        return total

    def _own_terms(self, i, delta, C, sctx, fixed, centers, deltas):
        """Every global-cost term that varies with ``i``: own-net airwire,
        crossings touching own-net segments, own + neighbor orientation, and
        the optional halo/edge terms. Comparing these equals comparing the
        full ``_global_cost`` — everything else cancels."""
        airw, segs = self._own_segs_batch(i, delta, C, sctx)
        cost = airw + self.p.cross_weight * (
            cross_count_ab(segs, fixed) + cross_count_self(segs))
        cost += self._orient_pen(i, delta, sctx)
        cost += self._neighbor_orient_batch(i, delta, C, centers, deltas)
        cost += self._halo_edge_batch(i, delta, C, centers, deltas)
        return cost

    def _quench_part(self, i, centers, deltas, offs, angle_step, max_disp):
        """One nudge/rotate step of the quench for part ``i``. Returns True if
        a strictly better legal candidate was committed.

        Every (offset, angle) cell is scored in one row batch on the cheap
        terms (airwire + orientation + halo/edge) — a lower bound on the full
        cost, since the crossing term is non-negative. Cells whose bound
        already misses the incumbent can never be accepted; only the survivors
        get segments built and crossings counted, then legality-tested.
        """
        sctx = self._score_ctx(i, centers, deltas)
        inc = {n for n, _ in self.part_nets[i]}
        fixed = self._fixed_segs(inc, centers, deltas)
        cur = centers[i].copy()
        cura = float(deltas[i])
        C = self._unique_ordered(self._snap_arr(cur[None] + offs))
        if max_disp is not None:
            keep = np.hypot(C[:, 0] - self.orig[i, 0],
                            C[:, 1] - self.orig[i, 1]) <= max_disp + 1e-9
            C = C[keep]
        angles = self._angles_for(i, angle_step)
        A = len(angles)
        fctx = self._fits_ctx(i, range(self.N), centers, deltas)
        sx = self._search_ctx(i, angles, fctx, sctx)
        cost0 = float(self._own_terms(i, cura, cur[None], sctx, fixed,
                                      centers, deltas)[0])
        # flat cells, offset-major / angle-minor (the scalar sweep order)
        rows = np.repeat(C, A, axis=0)
        aid = np.tile(np.arange(A), len(C))
        cost = self._air_rows(i, rows, aid, sx, sctx)
        cost += self._neighbor_orient_rows(i, rows, aid, angles,
                                           centers, deltas)
        if self.p.halo_weight or self.p.edge_weight:
            cost += self._halo_edge_core(i, sx['bbox'][aid], rows,
                                         centers, deltas)
        surv = np.flatnonzero(cost < cost0 - 1e-9)
        if not len(surv):
            return False
        w = self.p.cross_weight
        if not w:
            legal = self._fits_rows(i, rows[surv], aid[surv], fctx, sx)
            good = surv[legal]
            if not len(good):
                return False
            k = int(good[np.argmin(cost[good])])
        else:
            # branch-and-bound over the crossing term: walk survivors in
            # ascending-bound chunks, and stop as soon as the next bound can't
            # beat the best legal full cost seen — the incumbent's own
            # crossings would otherwise let thousands of cells "survive"
            order = surv[np.argsort(cost[surv], kind="stable")]
            best_k = -1
            best_full = math.inf
            pos = 0
            while pos < len(order):
                thresh = min(cost0 - 1e-9, best_full)
                if cost[order[pos]] >= thresh:
                    break
                sel = order[pos:pos + 512]
                pos += 512
                sel = sel[cost[sel] < thresh]
                if not len(sel):
                    continue
                segs = self._own_segs_rows(i, rows[sel], aid[sel], sx, sctx)
                full = cost[sel] + w * (cross_count_ab(segs, fixed)
                                        + cross_count_self(segs))
                cand = np.flatnonzero(full < thresh)
                if not len(cand):
                    continue
                sel2 = sel[cand]
                legal = self._fits_rows(i, rows[sel2], aid[sel2], fctx, sx)
                good = sel2[legal]
                if not len(good):
                    continue
                fg = full[cand][legal]
                m = int(np.argmin(fg))
                if fg[m] < best_full:
                    best_full = float(fg[m])
                    best_k = int(good[m])
            if best_k < 0:
                return False
            k = best_k
        centers[i] = rows[k]
        deltas[i] = angles[int(aid[k])]
        self._segs = None
        return True

    def _pair_terms(self, i, j, centers, deltas):
        """Every global-cost term that varies when ``i`` and ``j`` swap:
        their union nets' airwire, crossings touching those nets, orientation
        of the pair and its neighbors, halo pairs involving either, edges."""
        p = self.p
        nets = {n for n, _ in self.part_nets[i]} | {n for n, _ in self.part_nets[j]}
        total = 0.0
        changed = []
        for n in nets:
            pts = self._pads_world(self.net_part[n], self.net_off[n],
                                   centers, deltas)
            if len(pts) < 2:
                continue
            ln, ed = mst_edges_batch(pts[None])
            total += float(ln[0])
            changed.append(pts[ed[0]])
        A = np.concatenate(changed)[None] if changed else np.zeros((1, 0, 2, 2))
        fixed = self._fixed_segs(nets, centers, deltas)
        total += p.cross_weight * float(cross_count_ab(A, fixed)[0]
                                        + cross_count_self(A)[0])
        if p.orientation_weight:
            ks = {k for k in (self._nbrs(i) + self._nbrs(j) + [i, j])
                  if len(self.part_nets[k]) == 2 and self.is_mov[k]}
            for k in ks:
                total += self._orient_pen(k, deltas[k],
                                          self._score_ctx(k, centers, deltas))
        if p.halo_weight or p.edge_weight:
            hi = float(self._halo_edge_batch(i, deltas[i], centers[i][None],
                                             centers, deltas)[0])
            hj = float(self._halo_edge_batch(j, deltas[j], centers[j][None],
                                             centers, deltas)[0])
            total += hi + hj - self._halo_pair(i, j, centers, deltas)
        return total

    def _halo_pair(self, i, j, centers, deltas):
        """The one halo pair counted twice by summing ``i``'s and ``j``'s terms."""
        p = self.p
        if not p.halo_weight:
            return 0.0
        bi = self._geom(i, deltas[i])['bbox'] + np.repeat(centers[i], 2)[[0, 2, 1, 3]]
        bj = self._geom(j, deltas[j])['bbox'] + np.repeat(centers[j], 2)[[0, 2, 1, 3]]
        req = self.clearance + p.halo_gap * math.sqrt(
            min(self.pins[i], self.pins[j]))
        dx = max(bi[0] - bj[2], bj[0] - bi[2])
        dy = max(bi[1] - bj[3], bj[1] - bi[3])
        sep = math.hypot(dx, dy) if (dx > 0 and dy > 0) else max(dx, dy)
        if sep >= req:
            return 0.0
        short = req - max(sep, 0.0)
        return p.halo_weight * short * short

    def _try_swap(self, i, j, centers, deltas):
        """Same-footprint swap of ``i`` and ``j`` (both angle assignments),
        accepted on a strict pair-terms improvement. Returns True on commit."""
        base = self._pair_terms(i, j, centers, deltas)
        ci, cj = centers[i].copy(), centers[j].copy()
        ai, aj = float(deltas[i]), float(deltas[j])
        everyone = range(self.N)
        best = (base, None)
        for nai, naj in ((ai, aj), (aj, ai)):
            centers[i], centers[j] = cj, ci
            deltas[i], deltas[j] = nai, naj
            fit_i = self._fits_batch(i, nai, centers[i][None],
                                     self._fits_ctx(i, everyone, centers,
                                                    deltas))[0]
            fit_j = fit_i and self._fits_batch(
                j, naj, centers[j][None],
                self._fits_ctx(j, everyone, centers, deltas))[0]
            if fit_i and fit_j:
                t = self._pair_terms(i, j, centers, deltas)
                if t < best[0] - 1e-9:
                    best = (t, (nai, naj))
            centers[i], centers[j] = ci, cj
            deltas[i], deltas[j] = ai, aj
        if best[1] is None:
            return False
        centers[i], centers[j] = cj, ci
        deltas[i], deltas[j] = best[1]
        self._segs = None
        return True

    def _mark_dirty_q(self, i, spots, net_boxes, order, dirty, qdisc, qhull,
                      centers, deltas):
        """Part ``i`` moved in the quench: re-mark net-mates, parts whose
        candidate box one of ``i``'s footprints touches (legality/halo), and
        parts whose own-segment hull one of ``i``'s old/new net bboxes touches
        (their crossing counts against ``i``'s nets' segments may change)."""
        r = self._geom(i, 0.0)['radius']
        cl = self.clearance
        foots = [(cx - r - cl, cy - r - cl, cx + r + cl, cy + r + cl)
                 for cx, cy in spots]
        boxes = list(net_boxes)
        for n, _own in self.part_nets[i]:
            boxes.append(self._net_bbox(n, centers, deltas))
        for j in order:
            if j == i or j in dirty:
                continue
            if j in self._net_adj[i]:
                dirty.add(j)
                continue
            qd, qh = qdisc.get(j), qhull.get(j)
            if qd is None or qh is None:
                dirty.add(j)
                continue
            if any(self._boxes_touch(qd, f) for f in foots) \
                    or any(self._boxes_touch(qh, b) for b in boxes):
                dirty.add(j)

    def improve(self, centers, deltas, nudge, pos_step, angle_step, passes,
                allow_swap, max_disp, progress=None):
        """Zero-temperature quench (scalar ``improve``): nudge/rotate sweeps
        plus same-footprint swaps, accepting only strict global-cost
        improvements — compared via :meth:`_own_terms`/:meth:`_pair_terms`,
        which is the same test with the unchanged terms cancelled.

        Parts are re-evaluated only when something their cost or legality can
        see moved since their last evaluation (same dirty rules as settle,
        plus a net-bbox rule for the crossing term) — skipping a clean part is
        exact, its sweep would accept nothing.
        """
        k = int(round(nudge / pos_step))
        offs = np.array([(ix * pos_step, iy * pos_step)
                         for ix in range(-k, k + 1)
                         for iy in range(-k, k + 1)])
        order = self._order()
        self._segs = None
        dirty = set(order)
        qdisc: dict[int, tuple] = {}
        qhull: dict[int, tuple] = {}
        halo_reach = 0.0
        if self.p.halo_weight and len(self.pins):
            halo_reach = self.p.halo_gap * math.sqrt(float(self.pins.max()))
        if progress:
            progress.log(f"quenching up to {passes} pass(es)")
        for p in range(passes):
            if progress:
                progress.tick(f"quenching pass {p + 1}/{passes}")
            changed = False
            for i in order:
                if i not in dirty:
                    continue
                dirty.discard(i)
                r = self._geom(i, 0.0)['radius']
                pad = r + self.clearance + halo_reach
                cx, cy = float(centers[i, 0]), float(centers[i, 1])
                box = (cx - nudge - pad, cy - nudge - pad,
                       cx + nudge + pad, cy + nudge + pad)
                qdisc[i] = box
                hull = list(box)
                nbs = []
                for n, _own in self.part_nets[i]:
                    nb = self._net_bbox(n, centers, deltas)
                    nbs.append(nb)
                    hull = [min(hull[0], nb[0]), min(hull[1], nb[1]),
                            max(hull[2], nb[2]), max(hull[3], nb[3])]
                qhull[i] = tuple(hull)
                if self._quench_part(i, centers, deltas, offs, angle_step,
                                     max_disp):
                    changed = True
                    dirty.add(i)          # its candidate box moved with it
                    self._mark_dirty_q(
                        i, [(cx, cy),
                            (float(centers[i, 0]), float(centers[i, 1]))],
                        nbs, order, dirty, qdisc, qhull, centers, deltas)
            if allow_swap:
                fp = {i: self.p._fp_name(self.eids[i]) for i in order}
                for x in range(len(order)):
                    for y in range(x + 1, len(order)):
                        i, j = order[x], order[y]
                        if fp[i] is None or fp[i] != fp[j]:
                            continue
                        oi = (float(centers[i, 0]), float(centers[i, 1]))
                        oj = (float(centers[j, 0]), float(centers[j, 1]))
                        nets = {n for n, _ in self.part_nets[i]} \
                            | {n for n, _ in self.part_nets[j]}
                        nbs = [self._net_bbox(n, centers, deltas)
                               for n in nets]
                        if self._try_swap(i, j, centers, deltas):
                            changed = True
                            dirty.add(i)
                            dirty.add(j)
                            ni = (float(centers[i, 0]), float(centers[i, 1]))
                            nj = (float(centers[j, 0]), float(centers[j, 1]))
                            self._mark_dirty_q(i, [oi, ni], nbs, order, dirty,
                                               qdisc, qhull, centers, deltas)
                            self._mark_dirty_q(j, [oj, nj], nbs, order, dirty,
                                               qdisc, qhull, centers, deltas)
            if not changed:
                break
        return centers, deltas
