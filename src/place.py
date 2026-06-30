"""Constructive minimal-airwire placement for a live Fusion Electronics board.

Steinmetz's first tool. It reads the open board, anchors the big parts (the
ICs), pulls every other part toward the centroid of the pads it connects to,
and legalizes overlaps — a from-connectivity *initial* placement that minimizes
ratsnest (airwire) length. Translation-only (no rotation), so pad motion is
exact and there is no KiCad/EAGLE angle-convention to reconcile.

Dry-run by default: it prints the proposed moves and the before/after airwire.
Pass ``--apply`` to write the ``MOVE``s back over the bridge (each terminated
with ``;``), then it re-reads to verify. Changes are unsaved until you save in
Fusion.

    python src/place.py                 # propose only (no writes)
    python src/place.py --apply         # propose, then move parts in Fusion
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


class Placer:
    def __init__(self, board: Board, anchors: int, lock: list[str],
                 ignore_nets: list[str], clearance: float, margin: float):
        self.b = board
        self.clearance = clearance
        self.margin = margin
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

    # ----- airwire scoring -------------------------------------------------

    def _pad_global(self, pad, centers):
        ox, oy = self.orig[pad.element_id]
        cx, cy = centers[pad.element_id]
        return (pad.x + (cx - ox), pad.y + (cy - oy))

    def airwire(self, centers) -> float:
        total = 0.0
        for sid, pads in self.net_pads.items():
            if sid in self.ignore:
                continue
            total += mst_len([self._pad_global(p, centers) for p in pads])
        return total

    # ----- placement -------------------------------------------------------

    def _target(self, eid, centers):
        """Centroid of pads on OTHER elements sharing a non-ignored net."""
        xs, ys = [], []
        for pad in self.b.pads_of(eid):
            if pad.signal_id in self.ignore:
                continue
            for other in self.net_pads[pad.signal_id]:
                if other.element_id == eid:
                    continue
                gx, gy = self._pad_global(other, centers)
                xs.append(gx)
                ys.append(gy)
        if not xs:
            return None
        return sum(xs) / len(xs), sum(ys) / len(ys)

    def _bbox(self, eid, c):
        x1, y1, x2, y2 = self.b.pkg_bbox(eid)
        return (c[0] + x1, c[1] + y1, c[0] + x2, c[1] + y2)

    def _fits(self, eid, c, placed):
        bb = self._bbox(eid, c)
        bx0, by0, bx1, by1 = self.b.outline
        if not (bb[0] >= bx0 + self.margin and bb[1] >= by0 + self.margin
                and bb[2] <= bx1 - self.margin and bb[3] <= by1 - self.margin):
            return False
        g = self.clearance
        for oe, oc in placed.items():
            o = self._bbox(oe, oc)
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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the moves back to Fusion (default: propose only)")
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
                    args.clearance, args.margin)

    anchor_names = sorted(board.elements[e].name for e in placer.anchors)
    print(f"Board {board.outline[2]-board.outline[0]:.0f} x "
          f"{board.outline[3]-board.outline[1]:.0f} mm · {len(board.elements)} parts · "
          f"{len(board.net_pads())} nets ({len(placer.ignore)} power/plane ignored)")
    print(f"Anchored: {', '.join(anchor_names)}")

    before = placer.airwire(placer.orig)
    final = placer.solve()
    after = placer.airwire(final)
    pct = (100 * (before - after) / before) if before else 0.0
    print(f"\nSignal-airwire: {before:.1f} mm  ->  {after:.1f} mm  ({pct:+.0f}%)\n")

    moves = []
    for eid in placer.movable:
        ox, oy = placer.orig[eid]
        nx, ny = final[eid]
        if math.hypot(nx - ox, ny - oy) > 0.01:
            moves.append((board.elements[eid].name, round(nx, 4), round(ny, 4),
                          math.hypot(nx - ox, ny - oy)))
    moves.sort(key=lambda m: m[0])
    for name, nx, ny, d in moves:
        print(f"  {name:<5} -> ({nx:>8.2f}, {ny:>8.2f})   |d|={d:6.1f}")
    print(f"\n{len(moves)} part(s) would move.")

    if not args.apply:
        print("Dry run — nothing written. Re-run with --apply to move parts in Fusion.")
        return

    cmds = [f"MOVE {name} ({nx} {ny})" for name, nx, ny, _ in moves]
    print(f"\nApplying {len(cmds)} MOVE(s) over the bridge...")
    bridge.run_eagle_batch(cmds, grid="MM")

    after_board = read_board(bridge)
    by_name = after_board.by_name()
    ok = sum(1 for name, nx, ny, _ in moves
             if math.hypot(by_name[name].x - nx, by_name[name].y - ny) < 0.05)
    print(f"Verified {ok}/{len(moves)} parts landed within 0.05 mm.")
    print("Changes are unsaved — save in Fusion to keep them.")


if __name__ == "__main__":
    main()
