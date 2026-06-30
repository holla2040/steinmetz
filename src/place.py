"""Constructive minimal-airwire placement for a live Fusion Electronics board.

Steinmetz's first tool. It reads the open board, anchors the big parts (the
ICs), pulls every other part toward the centroid of the pads it connects to,
and legalizes overlaps — a from-connectivity *initial* placement that minimizes
ratsnest (airwire) length.

With ``--rotate`` it then runs a 90°-step **orientation** refinement: each
movable part tries the four rotations and keeps the one that minimizes airwire
length (and, as a tiebreak, airwire crossings), with its centre held fixed.
Rotation is opt-in because, unlike translation, it depends on the EAGLE angle
convention — so on ``--apply`` the tool re-reads the board and checks the
**actual pad positions** of every moved/rotated part against its prediction,
the gate that proves the transform matched Fusion.

Dry-run by default: it prints the proposed moves/rotations and the before/after
airwire and crossing count. Pass ``--apply`` to write the ``ROTATE``/``MOVE``s
back over the bridge (each terminated with ``;``), then it re-reads to verify.
Changes are unsaved until you save in Fusion.

    python src/place.py                 # propose only (no writes)
    python src/place.py --apply         # propose, then move parts in Fusion
    python src/place.py --rotate        # also try 90° rotations per part
    python src/place.py --anchors 1     # free U2-style ICs too; pin only the biggest
    python src/place.py --lock "J*" --ignore-nets GND "VCC*"

Run with a board open in Fusion.
"""
from __future__ import annotations

import argparse
import fnmatch
import math
import re

from board import Board, read_board
from bridge import FusionBridge

# Nets excluded from the airwire objective by default (plane-routed power).
_POWER = re.compile(r"^(GND|AGND|DGND|VSS|VDD|VCC|VEE|VBAT|VREF|VTT|[+]?\d+V\d*|"
                    r"3V3|1V1|2V5|5V|1V2|1V8)\d*$", re.I)


def is_power(name: str, pin_count: int, extra: list[str]) -> bool:
    if _POWER.match(name) or any(fnmatch.fnmatch(name, p) for p in extra):
        return True
    return pin_count >= 8          # large fan-out => plane-like


def mst_len(points: list[tuple[float, float]]) -> float:
    """Euclidean minimum-spanning-tree length = the ratsnest length of one net."""
    n = len(points)
    if n < 2:
        return 0.0
    dist = [math.inf] * n
    dist[0] = 0.0
    used = [False] * n
    total = 0.0
    for _ in range(n):
        u = min((d, i) for i, d in enumerate(dist) if not used[i])[1]
        used[u] = True
        total += dist[u]
        ux, uy = points[u]
        for v in range(n):
            if not used[v]:
                d = math.hypot(points[v][0] - ux, points[v][1] - uy)
                if d < dist[v]:
                    dist[v] = d
    return total


def mst_edges(points: list[tuple[float, float]]) -> list[tuple[int, int]]:
    """The edges (index pairs) of the Euclidean MST — the drawn airwire segments.

    Same Prim's tree as :func:`mst_len`; this returns the segments so crossings
    can be tested. A planar Euclidean MST never self-crosses, so crossings only
    ever arise *between* nets.
    """
    n = len(points)
    if n < 2:
        return []
    dist = [math.inf] * n
    dist[0] = 0.0
    parent = [-1] * n
    used = [False] * n
    edges: list[tuple[int, int]] = []
    for _ in range(n):
        u = min((d, i) for i, d in enumerate(dist) if not used[i])[1]
        used[u] = True
        if parent[u] != -1:
            edges.append((parent[u], u))
        ux, uy = points[u]
        for v in range(n):
            if not used[v]:
                d = math.hypot(points[v][0] - ux, points[v][1] - uy)
                if d < dist[v]:
                    dist[v] = d
                    parent[v] = u
    return edges


def _same(p, q, eps: float = 1e-6) -> bool:
    return abs(p[0] - q[0]) < eps and abs(p[1] - q[1]) < eps


def _orient(a, b, c) -> float:
    """>0 if c is left of a->b, <0 if right, 0 if collinear (2D cross product)."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(s1, s2) -> bool:
    """True iff two airwire segments *properly* intersect.

    Segments that merely share an endpoint (the common case at a net's star)
    or just touch/overlap collinearly do not count — only a genuine X-crossing.
    """
    (p1, p2), (p3, p4) = s1, s2
    if _same(p1, p3) or _same(p1, p4) or _same(p2, p3) or _same(p2, p4):
        return False
    d1, d2 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    d3, d4 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    # Strict straddle both ways. A zero (an endpoint touching the other line, a
    # T-junction or collinear overlap) is not a genuine X, so it does not count.
    return d1 * d2 < 0 and d3 * d4 < 0


class Placer:
    def __init__(self, board: Board, anchors: int, lock: list[str],
                 ignore_nets: list[str], clearance: float, margin: float,
                 cross_weight: float = 2.0):
        self.b = board
        self.clearance = clearance
        self.margin = margin
        self.cross_weight = cross_weight
        self.net_pads = board.net_pads()
        self.ignore = {sid for sid, pads in self.net_pads.items()
                       if is_power(board.signals.get(sid, ""), len(pads), ignore_nets)}
        # original centers; pads move rigidly relative to these
        self.orig = {eid: (e.x, e.y) for eid, e in board.elements.items()}

        # anchors = the N largest packages by area, plus any --lock matches
        def area(eid):
            x1, y1, x2, y2 = board.pkg_bbox(eid)
            return (x2 - x1) * (y2 - y1)
        by_area = sorted(board.elements, key=area, reverse=True)
        self.anchors = set(by_area[:anchors])
        for eid, e in board.elements.items():
            if any(fnmatch.fnmatch(e.name, p) for p in lock):
                self.anchors.add(eid)
        self.movable = [e for e in board.elements if e not in self.anchors]
        # Mirrored (bottom-side) parts rotate the opposite way; the angle field
        # may not capture mirror state, so leave their orientation alone.
        self.mirrored = {eid for eid, e in board.elements.items() if e.mirror}

    # ----- airwire scoring -------------------------------------------------

    @staticmethod
    def _rot(dx: float, dy: float, a: int) -> tuple[float, float]:
        """Apply what Fusion's ``ROTATE R<a>`` does to a pad offset (a in
        {0,90,180,270}). Exact integer remap — no sin/cos round-off.

        Counter-clockwise-positive, matching EAGLE/Fusion's ``ROTATE Rn`` —
        confirmed against a live board: ``ROTATE R90 'U2'`` moved all 15 pads of
        an IC to ``(-dy, dx)`` (the pad-position gate on ``--apply`` re-checks
        this for every rotated part).
        """
        a %= 360
        if a == 90:
            return (-dy, dx)
        if a == 180:
            return (-dx, -dy)
        if a == 270:
            return (dy, -dx)
        return (dx, dy)

    def _pad_global(self, pad, centers, rotations=None):
        """Global position of ``pad`` given each element's centre and rotation.

        The pad's offset from its element origin (taken in the board's current
        orientation) is rotated by the element's chosen 90°-step delta, then
        placed at the new centre. ``rotations`` is ``eid -> delta_deg`` (absent
        or 0 => translation only, the original behaviour).
        """
        eid = pad.element_id
        ox, oy = self.orig[eid]
        dx, dy = pad.x - ox, pad.y - oy
        a = rotations.get(eid, 0) if rotations else 0
        if a:
            dx, dy = self._rot(dx, dy, a)
        cx, cy = centers[eid]
        return (cx + dx, cy + dy)

    def airwire(self, centers, rotations=None) -> float:
        total = 0.0
        for sid, pads in self.net_pads.items():
            if sid in self.ignore:
                continue
            total += mst_len([self._pad_global(p, centers, rotations) for p in pads])
        return total

    def _airwire_segments(self, centers, rotations=None):
        """Every drawn airwire segment, as ((x1,y1),(x2,y2)) pairs."""
        segs = []
        for sid, pads in self.net_pads.items():
            if sid in self.ignore:
                continue
            pts = [self._pad_global(p, centers, rotations) for p in pads]
            for i, j in mst_edges(pts):
                segs.append((pts[i], pts[j]))
        return segs

    def crossings(self, centers, rotations=None) -> int:
        """Number of pairs of airwire segments that properly cross."""
        segs = self._airwire_segments(centers, rotations)
        n = 0
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                if segments_cross(segs[i], segs[j]):
                    n += 1
        return n

    def _cost(self, centers, rotations) -> float:
        """Length-primary objective: airwire length + a modest crossing penalty."""
        return (self.airwire(centers, rotations)
                + self.cross_weight * self.crossings(centers, rotations))

    # ----- placement -------------------------------------------------------

    def _target(self, eid, centers, rotations=None):
        """Centroid of pads on OTHER elements sharing a non-ignored net."""
        xs, ys = [], []
        for pad in self.b.pads_of(eid):
            if pad.signal_id in self.ignore:
                continue
            for other in self.net_pads[pad.signal_id]:
                if other.element_id == eid:
                    continue
                gx, gy = self._pad_global(other, centers, rotations)
                xs.append(gx)
                ys.append(gy)
        if not xs:
            return None
        return sum(xs) / len(xs), sum(ys) / len(ys)

    def _bbox(self, eid, c, rotations=None):
        x1, y1, x2, y2 = self.b.pkg_bbox(eid)
        a = rotations.get(eid, 0) if rotations else 0
        if a:
            corners = [self._rot(x1, y1, a), self._rot(x2, y1, a),
                       self._rot(x2, y2, a), self._rot(x1, y2, a)]
            xs = [p[0] for p in corners]
            ys = [p[1] for p in corners]
            x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        return (c[0] + x1, c[1] + y1, c[0] + x2, c[1] + y2)

    def _fits(self, eid, c, placed, rotations=None):
        bb = self._bbox(eid, c, rotations)
        bx0, by0, bx1, by1 = self.b.outline
        if not (bb[0] >= bx0 + self.margin and bb[1] >= by0 + self.margin
                and bb[2] <= bx1 - self.margin and bb[3] <= by1 - self.margin):
            return False
        g = self.clearance
        for oe, oc in placed.items():
            o = self._bbox(oe, oc, rotations)
            if not (bb[2] + g <= o[0] or o[2] + g <= bb[0]
                    or bb[3] + g <= o[1] or o[3] + g <= bb[1]):
                return False
        return True

    def solve(self, iters: int = 40):
        centers = dict(self.orig)
        # 1) barycentric relaxation toward the airwire-pulling targets
        for _ in range(iters):
            moved = 0.0
            for eid in self.movable:
                t = self._target(eid, centers)
                if t is None:
                    continue
                moved = max(moved, math.hypot(t[0] - centers[eid][0],
                                              t[1] - centers[eid][1]))
                centers[eid] = t
            if moved < 1e-3:
                break
        # 2) legalize: place near target, no overlap, on board
        placed = {e: self.orig[e] for e in self.anchors}
        order = sorted(self.movable,
                       key=lambda e: math.hypot(centers[e][0], centers[e][1]))
        final = dict(self.orig)
        for eid in order:
            tgt = centers[eid]
            spot = tgt if self._fits(eid, tgt, placed) else None
            if spot is None:
                for r in (i * 0.5 for i in range(1, 80)):
                    steps = max(8, int(r * 6))
                    for k in range(steps):
                        a = 2 * math.pi * k / steps
                        c = (tgt[0] + r * math.cos(a), tgt[1] + r * math.sin(a))
                        if self._fits(eid, c, placed):
                            spot = c
                            break
                    if spot:
                        break
            final[eid] = spot or tgt
            placed[eid] = final[eid]
        return final

    def _rotation_fits(self, eid, centers, rotations, a) -> bool:
        """Would rotating ``eid`` to delta ``a`` keep it on-board and clear?"""
        trial = dict(rotations)
        trial[eid] = a
        others = {e: c for e, c in centers.items() if e != eid}
        return self._fits(eid, centers[eid], others, trial)

    def refine_rotations(self, centers, passes: int = 8) -> dict[int, int]:
        """Pick a 90°-step rotation per movable part, centres held fixed.

        Greedy coordinate descent: sweep parts in a fixed order, each adopting
        the rotation that most lowers ``_cost`` (airwire length, with crossings
        as a tiebreak), keeping only strict improvements so a tie holds the
        current angle — which makes ``delta = 0`` the default and keeps a re-run
        on an already-placed board emitting no rotations. Mirrored parts and any
        rotation that would collide or leave the board are skipped. Returns
        ``eid -> delta_deg``.
        """
        rotations = {e: 0 for e in self.movable}
        rotatable = [e for e in self.movable if e not in self.mirrored]
        for _ in range(passes):
            changed = False
            for eid in rotatable:
                cur = rotations[eid]
                best_a, best_cost = cur, self._cost(centers, rotations)
                for a in (0, 90, 180, 270):
                    if a == cur or not self._rotation_fits(eid, centers, rotations, a):
                        continue
                    trial = dict(rotations)
                    trial[eid] = a
                    cost = self._cost(centers, trial)
                    if cost < best_cost - 1e-9:    # strict => ties keep current
                        best_a, best_cost = a, cost
                if best_a != cur:
                    rotations[eid] = best_a
                    changed = True
            if not changed:
                break
        return rotations


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the moves back to Fusion (default: propose only)")
    ap.add_argument("--rotate", action="store_true",
                    help="also try 90° rotations per part (min airwire, fewer crossings)")
    ap.add_argument("--cross-weight", type=float, default=2.0, metavar="MM",
                    help="airwire-crossing penalty in mm when --rotate (default: 2.0; "
                         "length stays primary, this only breaks near-ties)")
    ap.add_argument("--anchors", type=int, default=2,
                    help="pin the N largest parts in place (default: 2)")
    ap.add_argument("--lock", nargs="+", default=[], metavar="PAT",
                    help="also pin parts whose ref matches these patterns (e.g. 'J*')")
    ap.add_argument("--ignore-nets", nargs="+", default=[], metavar="PAT",
                    help="extra net-name patterns to exclude from airwire scoring")
    ap.add_argument("--clearance", type=float, default=0.3,
                    help="courtyard gap between parts in mm (default: 0.3)")
    ap.add_argument("--margin", type=float, default=1.0,
                    help="keep parts this far inside the board edge in mm (default: 1.0)")
    args = ap.parse_args()

    bridge = FusionBridge().connect()
    board = read_board(bridge)
    placer = Placer(board, args.anchors, args.lock, args.ignore_nets,
                    args.clearance, args.margin, args.cross_weight)

    anchor_names = sorted(board.elements[e].name for e in placer.anchors)
    print(f"Board {board.outline[2]-board.outline[0]:.0f} x "
          f"{board.outline[3]-board.outline[1]:.0f} mm · {len(board.elements)} parts · "
          f"{len(board.net_pads())} nets ({len(placer.ignore)} power/plane ignored)")
    print(f"Anchored: {', '.join(anchor_names)}")

    before_len = placer.airwire(placer.orig)
    before_cross = placer.crossings(placer.orig)
    final = placer.solve()
    rotations = (placer.refine_rotations(final) if args.rotate
                 else {e: 0 for e in placer.movable})
    after_len = placer.airwire(final, rotations)
    after_cross = placer.crossings(final, rotations)
    pct = (100 * (before_len - after_len) / before_len) if before_len else 0.0
    print(f"\nSignal-airwire: {before_len:.1f} mm  ->  {after_len:.1f} mm  ({pct:+.0f}%)")
    print(f"Airwire crossings: {before_cross}  ->  {after_cross}\n")

    # Each action: (name, nx, ny, |d|, delta_deg, eid). A part appears if it
    # would move or rotate.
    actions = []
    for eid in placer.movable:
        ox, oy = placer.orig[eid]
        nx, ny = final[eid]
        d = math.hypot(nx - ox, ny - oy)
        rot = rotations.get(eid, 0) % 360
        if d > 0.01 or rot:
            actions.append((board.elements[eid].name, round(nx, 4), round(ny, 4),
                            d, rot, eid))
    actions.sort(key=lambda m: m[0])
    for name, nx, ny, d, rot, _ in actions:
        tag = f"  R{rot}" if rot else ""
        print(f"  {name:<5} -> ({nx:>8.2f}, {ny:>8.2f})   |d|={d:6.1f}{tag}")
    n_move = sum(1 for a in actions if a[3] > 0.01)
    n_rot = sum(1 for a in actions if a[4])
    print(f"\n{n_move} part(s) would move, {n_rot} would rotate.")

    if not args.apply:
        print("Dry run — nothing written. Re-run with --apply to move parts in Fusion.")
        return

    # ROTATE (relative, preserves mirror) then MOVE — both pivot on the element
    # origin, so order is immaterial to the final pad positions. The part name
    # MUST be single-quoted for ROTATE: a bare `ROTATE R90 R4` is silently a
    # no-op (the parser doesn't bind the object), unlike MOVE which takes it bare.
    cmds = []
    for name, nx, ny, d, rot, _ in actions:
        if rot:
            cmds.append(f"ROTATE R{rot} '{name}'")
        if d > 0.01:
            cmds.append(f"MOVE {name} ({nx} {ny})")
    print(f"\nApplying {len(cmds)} command(s) over the bridge...")
    bridge.run_eagle_batch(cmds, grid="MM")

    after_board = read_board(bridge)
    by_name = after_board.by_name()
    ok = sum(1 for name, nx, ny, _, _, _ in actions
             if name in by_name
             and math.hypot(by_name[name].x - nx, by_name[name].y - ny) < 0.05)
    print(f"Verified {ok}/{len(actions)} parts landed within 0.05 mm.")

    # The hard gate: a part's predicted pad positions must match the board's
    # after the transform. This catches a wrong rotation sign or an unhandled
    # mirror — failures that MOVE-landing alone would miss.
    bad = []
    for name, nx, ny, d, rot, eid in actions:
        e2 = by_name.get(name)
        if e2 is None:
            bad.append(name)
            continue
        pred = sorted(placer._pad_global(p, final, rotations)
                      for p in placer.b.pads_of(eid))
        actual = sorted((p.x, p.y) for p in after_board.pads_of(e2.object_id))
        if not _pads_match(pred, actual):
            bad.append(name)
    if bad:
        print(f"WARNING: pad positions disagree with the board for: {', '.join(bad)}")
        print("  The rotate/move transform may not match Fusion (mirror or angle "
              "sign). Inspect these parts before saving.")
    else:
        print(f"Pad-position check passed for all {len(actions)} part(s).")
    print("Changes are unsaved — save in Fusion to keep them.")


def _pads_match(pred, actual, tol: float = 0.1) -> bool:
    """Two sorted pad-position lists agree within ``tol`` (rotation permutes the
    set, sorting both canonicalizes it)."""
    if len(pred) != len(actual):
        return False
    return all(abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol
               for p, q in zip(pred, actual))


if __name__ == "__main__":
    main()
