"""Minimal-airwire placement of the *selected* parts on a live Fusion board.

Steinmetz's first tool. It optimizes only the components you've selected in
Fusion and freezes everything else. **Select nothing and it does nothing.**

The placement problem for passives around an IC is *separable*: a 2-port R/C
straddles two pads on the (fixed) IC and shares no net with the other movable
parts, so its airwire depends only on its own position and rotation — the parts
interact only by not overlapping. So the placer works per-part:

1. **Optimal-region seed** (:meth:`Placer.place`): each selected part is dropped
   at the ``(rotation, position)`` that minimizes *its own* ratsnest — an outward
   ring search from where its connections want its origin, swept over rotations,
   legalized against the frozen anchors and the parts already placed. This lands
   each part in the legal crescent just outside the IC with its pads pointing back
   at the balls they connect to.
2. **Quench** (:meth:`Placer.improve`): a zero-temperature greedy pass —
   nudge / rotate / same-footprint swap, accepting only strict cost improvements —
   that resolves the one real coupling (two parts wanting the same spot).

The selection is read over the bridge (see ``src/selection.py``): GROUP-select
the parts in the Board editor (the rubber-band Group tool) and they're captured
automatically. ``--only`` bypasses that with an explicit ref list.

The objective is airwire length + a small crossing penalty; optional halo
(spreading) and edge-margin terms are available but **off by default**. Rotation
is chosen jointly with position in ``--rotate`` steps (default 90° => 0/90/180/270;
``--rotate 1`` = free 1°). Placed origins snap to one tenth of Fusion's current
main grid by default; use ``--grid`` for the full main grid or ``--nogrid`` to
disable snapping. The grid value is queried from Fusion on every run. Because
rotation depends on
the EAGLE angle convention, after writing the ``ROTATE``/``MOVE``s (each
terminated with ``;``) the tool re-reads the board and checks the **actual pad
positions** of every moved part against its prediction — the gate that proves the
transform matched Fusion. Changes are unsaved until you save in Fusion.

    python src/place.py                 # place the current selection (90° steps)
    python src/place.py --rotate 1      # let parts rotate freely, in 1° steps
    python src/place.py --only R4 R5 R8 # override the selection with these refs
    python src/place.py --refine-only   # quench the current layout, no re-seed
    python src/place.py --grid          # snap origins to Fusion's main grid
    python src/place.py --nogrid        # disable origin snapping
    python src/place.py --ignore-nets GND "VCC*"

Run with a board open in Fusion.
"""
from __future__ import annotations

import argparse
import fnmatch
import math
import re

from board import Board, read_board
from bridge import FusionBridge
from selection import read_selection

# Nets excluded from the airwire objective by default (plane-routed power).
_POWER = re.compile(r"^(GND|AGND|DGND|VSS|VDD|VCC|VEE|VBAT|VREF|VTT|[+]?\d+V\d*|"
                    r"3V3|1V1|2V5|5V|1V2|1V8)\d*$", re.I)


def is_power(name: str, pin_count: int, extra: list[str]) -> bool:
    if _POWER.match(name) or any(fnmatch.fnmatch(name, p) for p in extra):
        return True
    return pin_count >= 8          # large fan-out => plane-like


def _read_main_grid_mm(bridge: FusionBridge) -> float:
    """Read the visible EAGLE main grid distance in millimeters."""
    out_path = r"C:\tmp\steinmetz_grid_main.txt"
    ulp_path = r"C:\tmp\steinmetz_grid_main.ulp"
    out_fwd = out_path.replace("\\", "/")
    ulp_fwd = ulp_path.replace("\\", "/")
    ulp = f'''output("{out_fwd}", "wt") {{
  board(B) {{
    printf("%.12f\\n", B.grid.distance);
    printf("%d\\n", B.grid.unitdist);
  }}
}}
'''
    run_cmd = f"Electron.run \"RUN '{ulp_fwd}';\""
    script = "\n".join([
        "import adsk.core, os",
        "def run(_context):",
        "    app = adsk.core.Application.get()",
        f"    ulp_path = {ulp_path!r}",
        f"    out_path = {out_path!r}",
        "    try:",
        "        os.remove(out_path)",
        "    except OSError:",
        "        pass",
        "    with open(ulp_path, 'w') as f:",
        f"        f.write({ulp!r})",
        f"    app.executeTextCommand({run_cmd!r})",
        "    print('__GRID_START__')",
        "    try:",
        "        print(open(out_path).read())",
        "    except FileNotFoundError:",
        "        print('__GRID_MISSING__')",
        "    print('__GRID_END__')",
    ])
    msg = bridge.execute(script).get("message", "") or ""
    if "__GRID_START__" not in msg or "__GRID_END__" not in msg:
        raise RuntimeError(f"could not parse Fusion grid response: {msg!r}")
    body = msg.split("__GRID_START__", 1)[1].split("__GRID_END__", 1)[0].strip()
    if "__GRID_MISSING__" in body:
        raise RuntimeError("Fusion did not produce a main-grid response")
    try:
        lines = body.splitlines()
        grid = float(lines[0].strip())
        unitdist = int(lines[1].strip())
    except (IndexError, ValueError) as exc:
        raise RuntimeError(f"invalid Fusion main-grid response: {body!r}") from exc
    grid *= {0: 0.001, 1: 1.0, 2: 0.0254, 3: 25.4}.get(unitdist, 1.0)
    if grid <= 0:
        raise RuntimeError(f"Fusion returned a non-positive main grid: {grid:g}")
    return grid


def _resolve_grid_step(mode: str, main_grid: float) -> float | None:
    if mode == "none":
        return None
    if mode == "grid":
        return main_grid
    if mode == "fine":
        return main_grid / 10.0
    raise ValueError(f"unknown grid mode {mode!r}")


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
    def __init__(self, board: Board, ignore_nets: list[str], clearance: float,
                 margin: float, cross_weight: float = 2.0,
                 only: list[str] | None = None,
                 halo_weight: float = 0.0, halo_gap: float = 0.5,
                 edge_weight: float = 0.0, edge_margin: float = 2.0,
                 grid_step: float | None = None,
                 grid_mode: str = "fine",
                 bridge: FusionBridge | None = None):
        self.b = board
        self.clearance = clearance
        self.margin = margin
        self.cross_weight = cross_weight
        self.halo_weight = halo_weight
        self.halo_gap = halo_gap
        self.edge_weight = edge_weight
        self.edge_margin = edge_margin
        self.fusion_grid_step = None
        if grid_step is None and grid_mode != "none":
            if bridge is None:
                raise RuntimeError("grid snapping requires a live Fusion bridge")
            self.fusion_grid_step = _read_main_grid_mm(bridge)
            grid_step = _resolve_grid_step(grid_mode, self.fusion_grid_step)
        self.grid_mode = grid_mode
        self.grid_step = grid_step if grid_step and grid_step > 0 else None
        self.net_pads = board.net_pads()
        self.ignore = {sid for sid, pads in self.net_pads.items()
                       if is_power(board.signals.get(sid, ""), len(pads), ignore_nets)}
        # original centers; pads move rigidly relative to these
        self.orig = {eid: (e.x, e.y) for eid, e in board.elements.items()}

        # place ONLY the selected parts (globs ok); freeze everything else. The
        # frozen set is "all but these", so the selected parts flow against a
        # fixed board.
        self.only = list(only or [])
        picked = {eid for eid, e in board.elements.items()
                  if any(fnmatch.fnmatch(e.name, p) for p in self.only)}
        self.anchors = set(board.elements) - picked
        self.movable = [e for e in board.elements if e not in self.anchors]
        # Mirrored (bottom-side) parts rotate the opposite way; the angle field
        # may not capture mirror state, so leave their orientation alone.
        self.mirrored = {eid for eid, e in board.elements.items() if e.mirror}
        # per-part net cache: eid -> [(sid, own_pad_offsets, other_pads)]
        self._enets_cache: dict[int, list] = {}

    # ----- airwire scoring -------------------------------------------------

    @staticmethod
    def _rot(dx: float, dy: float, a: float) -> tuple[float, float]:
        """Apply what Fusion's ``ROTATE R<a>`` does to a pad offset.

        Counter-clockwise-positive, matching EAGLE/Fusion's ``ROTATE Rn`` —
        confirmed against a live board: a ``ROTATE R90`` moved every pad of a
        rotated part to ``(-dy, dx)`` (the pad-position gate re-checks this for
        every rotated part). The four right angles use an exact integer remap (no
        sin/cos round-off); any other step (e.g. 45°) falls back to trig, with
        the 0.1 mm pad-position tolerance absorbing the round-off.
        """
        a %= 360
        if a == 0:
            return (dx, dy)
        if a == 90:
            return (-dy, dx)
        if a == 180:
            return (-dx, -dy)
        if a == 270:
            return (dy, -dx)
        rad = math.radians(a)
        c, s = math.cos(rad), math.sin(rad)
        return (dx * c - dy * s, dx * s + dy * c)

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

    # ----- per-part (incremental) scoring ----------------------------------

    def _eid_nets(self, eid):
        """Cached ``[(sid, own_pad_offsets, other_pads)]`` for eid's non-ignored
        nets — the data needed to score eid's own airwire at any (centre, angle)
        without touching the other elements' geometry.
        """
        cached = self._enets_cache.get(eid)
        if cached is not None:
            return cached
        ox, oy = self.orig[eid]
        by_sid: dict[int, list] = {}
        for pad in self.b.pads_of(eid):
            sid = pad.signal_id
            if sid in self.ignore:
                continue
            by_sid.setdefault(sid, []).append((pad.x - ox, pad.y - oy))
        entries = [(sid, own, [p for p in self.net_pads[sid] if p.element_id != eid])
                   for sid, own in by_sid.items()]
        self._enets_cache[eid] = entries
        return entries

    def _incident_nets(self, eid) -> list[int]:
        """Distinct non-ignored signal_ids touching ``eid``."""
        return [sid for sid, _, _ in self._eid_nets(eid)]

    def _part_airwire_at(self, eid, c, a, centers, rotations) -> float:
        """Airwire of ONLY eid's incident nets, with eid hypothetically at centre
        ``c`` and rotation-delta ``a`` (all other pads read from centers/rotations).

        This is the separable objective: moving one part changes only its own
        nets, so greedy descent on this equals coordinate descent on the global
        :meth:`airwire`, evaluated over the handful of nets that actually change.
        """
        total = 0.0
        cx, cy = c
        for sid, own, others in self._eid_nets(eid):
            pts = []
            for ox, oy in own:
                rx, ry = self._rot(ox, oy, a) if a else (ox, oy)
                pts.append((cx + rx, cy + ry))
            for p in others:
                pts.append(self._pad_global(p, centers, rotations))
            total += mst_len(pts)
        return total

    def _part_airwire(self, eid, centers, rotations) -> float:
        """eid's own airwire at its current centre/rotation."""
        a = rotations.get(eid, 0.0) if rotations else 0.0
        return self._part_airwire_at(eid, centers[eid], a, centers, rotations)

    def _pins(self, eid) -> int:
        return len(self.b.pads_of(eid))

    # ----- geometry / legality ---------------------------------------------

    def _bbox(self, eid, c, rotations=None):
        x1, y1, x2, y2 = self.b.placement_bbox(eid)
        a = rotations.get(eid, 0) if rotations else 0
        if a:
            corners = [self._rot(x1, y1, a), self._rot(x2, y1, a),
                       self._rot(x2, y2, a), self._rot(x1, y2, a)]
            xs = [p[0] for p in corners]
            ys = [p[1] for p in corners]
            x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        return (c[0] + x1, c[1] + y1, c[0] + x2, c[1] + y2)

    def _snap(self, c):
        """Snap an element origin to the requested placement grid, if any."""
        if not self.grid_step:
            return c
        g = self.grid_step
        return (round(round(c[0] / g) * g, 6),
                round(round(c[1] / g) * g, 6))

    def _on_grid(self, c) -> bool:
        if not self.grid_step:
            return True
        sx, sy = self._snap(c)
        return abs(c[0] - sx) < 1e-6 and abs(c[1] - sy) < 1e-6

    def _fits(self, eid, c, placed, rotations=None):
        if eid in self.movable and not self._on_grid(c):
            return False
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

    @staticmethod
    def _rect_gap(a, b) -> float:
        """Smallest axis-aligned gap between two rects (negative if overlapping)."""
        dx = max(a[0] - b[2], b[0] - a[2])
        dy = max(a[1] - b[3], b[1] - a[3])
        if dx > 0 and dy > 0:
            return math.hypot(dx, dy)
        return max(dx, dy)

    # ----- optional spreading / edge cost ----------------------------------

    def _spread_penalty(self, centers, rotations) -> float:
        """Soft whitespace: quadratic penalty when two movable hard boxes are
        closer than ``halo_gap·sqrt(min pin count)`` — spreads parts for fanout
        room. The required gap sits *above* the hard clearance, so this only ever
        pushes beyond the DRC minimum. 0 when ``halo_weight`` is 0.
        """
        if not self.halo_weight:
            return 0.0
        pen = 0.0
        mv = self.movable
        for i in range(len(mv)):
            bi = self._bbox(mv[i], centers[mv[i]], rotations)
            pi = self._pins(mv[i])
            for j in range(i + 1, len(mv)):
                bj = self._bbox(mv[j], centers[mv[j]], rotations)
                req = self.clearance + self.halo_gap * math.sqrt(min(pi, self._pins(mv[j])))
                sep = self._rect_gap(bi, bj)
                if sep < req:
                    short = req - max(sep, 0.0)
                    pen += self.halo_weight * short * short
        return pen

    def _edge_penalty(self, centers, rotations) -> float:
        """Soft board-edge margin: quadratic penalty inside ``edge_margin`` of the
        outline. 0 when ``edge_weight`` is 0."""
        if not self.edge_weight:
            return 0.0
        pen = 0.0
        bx0, by0, bx1, by1 = self.b.outline
        for eid in self.movable:
            bb = self._bbox(eid, centers[eid], rotations)
            for g in (bb[0] - bx0, bb[1] - by0, bx1 - bb[2], by1 - bb[3]):
                if g < self.edge_margin:
                    short = self.edge_margin - max(g, 0.0)
                    pen += self.edge_weight * short * short
        return pen

    def _global_cost(self, centers, rotations) -> float:
        """Full placement objective: airwire + crossing penalty + optional
        halo/edge spreading. Equals :meth:`_cost` when halo/edge weights are 0."""
        return (self.airwire(centers, rotations)
                + self.cross_weight * self.crossings(centers, rotations)
                + self._spread_penalty(centers, rotations)
                + self._edge_penalty(centers, rotations))

    # ----- placement -------------------------------------------------------

    @staticmethod
    def _angle_steps(step: float) -> list[float]:
        """Candidate rotations 0..360 in increments of ``step`` degrees.

        ``90`` -> ``[0, 90, 180, 270]``; ``45`` -> eight orientations; etc. A
        step that doesn't divide 360 is rounded to the nearest whole count.
        """
        if step <= 0:
            raise ValueError("rotation step must be positive")
        n = max(1, round(360.0 / step))
        return [round(i * (360.0 / n), 6) for i in range(n)]

    def _fp_name(self, eid):
        """Footprint (package) name — the key for same-footprint swaps."""
        pkg = self.b.packages.get(self.b.elements[eid].package_object_id)
        return pkg.get("name") if pkg else None

    def _anchor_dist(self, pt) -> float:
        if not self.anchors:
            return 0.0
        return min(math.hypot(pt[0] - self.orig[a][0], pt[1] - self.orig[a][1])
                   for a in self.anchors)

    def _implied_centers(self, eid, a, centers, rotations):
        """For rotation-delta ``a``, the element-origin each connection *wants*:
        ``other_pad_global - rot(own_pad_offset, a)``. Their centroid is the
        ideal (unconstrained) centre and the origin of the ring search."""
        pts = []
        for sid, own, others in self._eid_nets(eid):
            for ox, oy in own:
                rx, ry = self._rot(ox, oy, a) if a else (ox, oy)
                for p in others:
                    gx, gy = self._pad_global(p, centers, rotations)
                    pts.append((gx - rx, gy - ry))
        return pts

    def _placement_order(self) -> list[int]:
        """Most-constrained first: highest pin count (least positional slack),
        tiebreak by ideal-centre distance to the nearest anchor, then name."""
        def key(eid):
            implied = self._implied_centers(eid, 0.0, self.orig, {})
            if implied:
                ox = sum(p[0] for p in implied) / len(implied)
                oy = sum(p[1] for p in implied) / len(implied)
                d = self._anchor_dist((ox, oy))
            else:
                d = math.inf
            return (-self._pins(eid), d, self.b.elements[eid].name)
        return sorted(self.movable, key=key)

    def _optimal_region(self, eid, centers, rotations, placed,
                        pos_step, angle_step, span, max_disp):
        """The (rotation-delta, centre) minimizing eid's own airwire, legal
        against ``placed``. Sweeps rotations; per rotation runs an outward ring
        from the implied-centre centroid, keeping the min-cost legal point and
        stopping ``span`` mm past the first legal ring (cost grows with distance
        from the ideal centre)."""
        o0 = self.orig[eid]
        cand_angles = [0.0] if eid in self.mirrored else self._angle_steps(angle_step)
        if not self._eid_nets(eid):                       # nothing to pull it
            c0 = self._snap(o0)
            if self._fits(eid, c0, placed, {**rotations, eid: 0.0}):
                return 0.0, c0
            return self._nearest_legal(eid, o0, placed, rotations, angle_step, max_disp)
        bx0, by0, bx1, by1 = self.b.outline
        diag = math.hypot(bx1 - bx0, by1 - by0)
        r_cap = min(diag, max_disp) if max_disp is not None else diag
        best = None                                       # (cost, angle, centre)
        for a in cand_angles:
            implied = self._implied_centers(eid, a, centers, rotations)
            if not implied:
                continue
            ox = sum(p[0] for p in implied) / len(implied)
            oy = sum(p[1] for p in implied) / len(implied)
            rot_trial = dict(rotations)
            rot_trial[eid] = a
            r_first = None
            r = 0.0
            while True:
                if r == 0.0:
                    ring = [(ox, oy)]
                else:
                    k = max(8, math.ceil(2 * math.pi * r / pos_step))
                    ring = [(ox + r * math.cos(2 * math.pi * t / k),
                             oy + r * math.sin(2 * math.pi * t / k))
                            for t in range(k)]
                seen = set()
                for c in ring:
                    c = self._snap(c)
                    if c in seen:
                        continue
                    seen.add(c)
                    if max_disp is not None and \
                            math.hypot(c[0] - o0[0], c[1] - o0[1]) > max_disp + 1e-9:
                        continue
                    if not self._fits(eid, c, placed, rot_trial):
                        continue
                    cost = self._part_airwire_at(eid, c, a, centers, rotations)
                    if best is None or cost < best[0] - 1e-12:
                        best = (cost, a, c)
                    if r_first is None:
                        r_first = r
                if (r_first is not None and r > r_first + span) or r > r_cap:
                    break
                r += pos_step
        if best is None:
            return self._nearest_legal(eid, o0, placed, rotations, angle_step, max_disp)
        return best[1], best[2]

    def _nearest_legal(self, eid, origin, placed, rotations,
                       angle_step, max_disp=None):
        """Nearest legal (angle, centre) to ``origin`` ignoring airwire — the
        grow-region safety net when the bounded search finds nothing (a=0 first)."""
        cand_angles = [0.0] if eid in self.mirrored else self._angle_steps(angle_step)
        bx0, by0, bx1, by1 = self.b.outline
        diag = math.hypot(bx1 - bx0, by1 - by0)
        r_cap = min(diag, max_disp) if max_disp is not None else diag
        r = 0.0
        while r <= r_cap:
            if r == 0.0:
                ring = [origin]
            else:
                k = max(8, math.ceil(2 * math.pi * r / 0.5))
                ring = [(origin[0] + r * math.cos(2 * math.pi * t / k),
                         origin[1] + r * math.sin(2 * math.pi * t / k))
                        for t in range(k)]
            seen = set()
            for c in ring:
                c = self._snap(c)
                if c in seen:
                    continue
                seen.add(c)
                for a in cand_angles:
                    if self._fits(eid, c, placed, {**rotations, eid: a}):
                        return a, c
            r += 0.5
        return 0.0, self._snap(origin)

    def place(self, pos_step: float = 0.5, angle_step: float = 90.0,
              span: float = 3.0, max_disp=None):
        """Optimal-region placement, in three steps:

        1. **ideal** — each part at the ``(rotation, centre)`` minimizing its OWN
           airwire, legal against the frozen anchors only (overlaps *between*
           movable parts allowed — this is the per-part lower bound);
        2. **spread** — remove those overlaps by minimal-displacement pushing, so
           parts barely leave their ideal spots;
        3. **settle** — coordinate descent: re-place each part at its optimal
           region against ALL the others, iterated, recovering the airwire the
           spread cost and resolving the contention a single greedy pass can't.

        Returns ``(centers, rotations)``.
        """
        anchors = {e: self.orig[e] for e in self.anchors}
        centers = dict(self.orig)
        rotations = {e: 0.0 for e in self.movable}
        for eid in self._placement_order():
            a, c = self._optimal_region(eid, centers, rotations, anchors,
                                        pos_step, angle_step, span, max_disp)
            rotations[eid] = a
            centers[eid] = c
        centers = self._spread(centers, rotations)
        centers, rotations = self._settle(centers, rotations, pos_step,
                                          angle_step, max(span, 8.0), max_disp)
        # HARD legality: _spread can push a part into an anchor hard box, and
        # _settle won't move an illegal part to a legal-but-longer spot — so force
        # any overlapping part to its nearest legal spot, then re-settle. Fusion
        # offsets a part whose MOVE target overlaps, so a legal output is what
        # makes every part land.
        centers, rotations = self._legalize(centers, rotations, angle_step)
        centers, rotations = self._settle(centers, rotations, pos_step,
                                          angle_step, max(span, 8.0), max_disp)
        return self._legalize(centers, rotations, angle_step)

    def _legalize(self, centers, rotations, angle_step, passes: int = 40):
        """Force every movable part that overlaps another part (or the board
        edge) to its nearest legal spot, keeping rotation — a guaranteed
        no-overlap output. Required for correctness: Fusion's ``MOVE`` offsets a
        part that would land overlapping, so an illegal target fails the
        pad-position gate."""
        centers = dict(centers)
        rotations = dict(rotations)
        for _ in range(passes):
            moved = False
            for eid in self._placement_order():
                others = {e: c for e, c in centers.items() if e != eid}
                if self._fits(eid, centers[eid], others, rotations):
                    continue
                a, c = self._nearest_legal(eid, centers[eid], others, rotations,
                                           angle_step)
                centers[eid] = c
                rotations[eid] = a
                moved = True
            if not moved:
                break
        return centers, rotations

    def _spread(self, centers, rotations, iters: int = 600):
        """Remove hard-geometry overlaps by minimal-displacement AABB pushes.

        Movable/movable pairs split the push. Recompute bboxes after each
        accepted push; stale geometry makes dense clusters slosh. Anchor
        conflicts are handled by the hard legalizer after this soft spread,
        since pushing every near-anchor interaction here costs too much wire.
        """
        centers = dict(centers)
        mv = self.movable
        bx0, by0, bx1, by1 = self.b.outline
        g = self.clearance
        for _ in range(iters):
            moved = False
            for i in range(len(mv)):
                for j in range(i + 1, len(mv)):
                    bi = self._bbox(mv[i], centers[mv[i]], rotations)
                    bj = self._bbox(mv[j], centers[mv[j]], rotations)
                    cxi = (bi[0] + bi[2]) / 2
                    cyi = (bi[1] + bi[3]) / 2
                    cxj = (bj[0] + bj[2]) / 2
                    cyj = (bj[1] + bj[3]) / 2
                    ox = ((bi[2] - bi[0]) / 2 + (bj[2] - bj[0]) / 2 + g) - abs(cxi - cxj)
                    oy = ((bi[3] - bi[1]) / 2 + (bj[3] - bj[1]) / 2 + g) - abs(cyi - cyj)
                    if ox > 1e-6 and oy > 1e-6:                 # overlapping
                        moved = True
                        if ox <= oy:
                            s = ox / 2 + 1e-9
                            sign = 1.0 if cxi >= cxj else -1.0
                            centers[mv[i]] = (centers[mv[i]][0] + sign * s, centers[mv[i]][1])
                            centers[mv[j]] = (centers[mv[j]][0] - sign * s, centers[mv[j]][1])
                        else:
                            s = oy / 2 + 1e-9
                            sign = 1.0 if cyi >= cyj else -1.0
                            centers[mv[i]] = (centers[mv[i]][0], centers[mv[i]][1] + sign * s)
                            centers[mv[j]] = (centers[mv[j]][0], centers[mv[j]][1] - sign * s)
            for eid in mv:                                       # keep on board
                bb = self._bbox(eid, centers[eid], rotations)
                dx = dy = 0.0
                if bb[0] < bx0 + self.margin:
                    dx = (bx0 + self.margin) - bb[0]
                elif bb[2] > bx1 - self.margin:
                    dx = (bx1 - self.margin) - bb[2]
                if bb[1] < by0 + self.margin:
                    dy = (by0 + self.margin) - bb[1]
                elif bb[3] > by1 - self.margin:
                    dy = (by1 - self.margin) - bb[3]
                if dx or dy:
                    centers[eid] = (centers[eid][0] + dx, centers[eid][1] + dy)
            if not moved:
                break
        return centers

    def _settle(self, centers, rotations, pos_step, angle_step, span, max_disp,
                passes: int = 25):
        """Coordinate descent: repeatedly re-place each movable part at its
        optimal region against all the OTHER parts, keeping only strict airwire
        improvements. Recovers the airwire the spread step cost and lets parts
        settle into the packed arrangement. Monotone (airwire) and terminating."""
        centers = dict(centers)
        rotations = dict(rotations)
        order = self._placement_order()
        for _ in range(passes):
            moved = False
            for eid in order:
                others = {e: c for e, c in centers.items() if e != eid}
                before = self._part_airwire_at(eid, centers[eid], rotations[eid],
                                               centers, rotations)
                a, c = self._optimal_region(eid, centers, rotations, others,
                                            pos_step, angle_step, span, max_disp)
                if self._part_airwire_at(eid, c, a, centers, rotations) < before - 1e-9:
                    centers[eid] = c
                    rotations[eid] = a
                    moved = True
            if not moved:
                break
        return centers, rotations

    def improve(self, centers, rotations, *, nudge: float = 1.5,
                pos_step: float = 0.5, angle_step: float = 90.0,
                passes: int = 6, allow_swap: bool = True, max_disp=None):
        """Zero-temperature quench: sweep parts, accept only strict global-cost
        improvements. Move set: nudge within a ``±nudge`` box, rotate, and
        same-footprint swap. Resolves the residual coupling the per-part seed
        can't see (two parts contending for one spot). Deterministic and
        terminating (strict improvement, fixed order). Returns ``(centers,
        rotations)``."""
        centers = dict(centers)
        rotations = dict(rotations)
        angles = self._angle_steps(angle_step)
        order = self._placement_order()
        k = int(round(nudge / pos_step))
        offsets = [(ix * pos_step, iy * pos_step)
                   for ix in range(-k, k + 1) for iy in range(-k, k + 1)]
        for _ in range(passes):
            changed = False
            # nudge + rotate, one part at a time
            for eid in order:
                others = {e: c for e, c in centers.items() if e != eid}
                cx, cy = centers[eid]
                cur_a = rotations[eid]
                cand_angles = [0.0] if eid in self.mirrored else angles
                best = (self._global_cost(centers, rotations), cx, cy, cur_a)
                seen = set()
                for dx, dy in offsets:
                    nc = self._snap((cx + dx, cy + dy))
                    if nc in seen:
                        continue
                    seen.add(nc)
                    if max_disp is not None and math.hypot(
                            nc[0] - self.orig[eid][0], nc[1] - self.orig[eid][1]) > max_disp + 1e-9:
                        continue
                    for a in cand_angles:
                        if abs(dx) < 1e-12 and abs(dy) < 1e-12 and a == cur_a:
                            continue
                        if not self._fits(eid, nc, others, {**rotations, eid: a}):
                            continue
                        centers[eid] = nc
                        rotations[eid] = a
                        cost = self._global_cost(centers, rotations)
                        if cost < best[0] - 1e-9:
                            best = (cost, nc[0], nc[1], a)
                centers[eid] = (best[1], best[2])
                rotations[eid] = best[3]
                if (best[1], best[2], best[3]) != (cx, cy, cur_a):
                    changed = True
            # same-footprint swaps
            if allow_swap:
                for i in range(len(order)):
                    ei = order[i]
                    for j in range(i + 1, len(order)):
                        ej = order[j]
                        if self._fp_name(ei) is None or self._fp_name(ei) != self._fp_name(ej):
                            continue
                        base = self._global_cost(centers, rotations)
                        ci, cj = centers[ei], centers[ej]
                        ai, aj = rotations[ei], rotations[ej]
                        best = (base, ci, cj, ai, aj)
                        for nai, naj in ((ai, aj), (aj, ai)):
                            centers[ei], centers[ej] = cj, ci
                            rotations[ei], rotations[ej] = nai, naj
                            oi = {e: c for e, c in centers.items() if e != ei}
                            oj = {e: c for e, c in centers.items() if e != ej}
                            if (self._fits(ei, centers[ei], oi, rotations)
                                    and self._fits(ej, centers[ej], oj, rotations)):
                                cost = self._global_cost(centers, rotations)
                                if cost < best[0] - 1e-9:
                                    best = (cost, centers[ei], centers[ej],
                                            rotations[ei], rotations[ej])
                        if best[0] < base - 1e-9:
                            centers[ei], centers[ej] = best[1], best[2]
                            rotations[ei], rotations[ej] = best[3], best[4]
                            changed = True
                        else:
                            centers[ei], centers[ej] = ci, cj
                            rotations[ei], rotations[ej] = ai, aj
            if not changed:
                break
        return centers, rotations


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rotate", type=float, default=90.0, metavar="DEG",
                    help="rotation granularity in degrees (default: 90). Rotation "
                         "is always on; --rotate 1 lets parts rotate freely in 1° "
                         "steps, --rotate 45 in 45° steps, etc.")
    ap.add_argument("--cross-weight", type=float, default=2.0, metavar="MM",
                    help="airwire-crossing penalty in mm (default: 2.0; length "
                         "stays primary, this only breaks near-ties)")
    ap.add_argument("--only", nargs="+", default=[], metavar="REF",
                    help="override the Fusion selection with this explicit ref "
                         "list (globs ok); handy for scripted/repeat runs")
    ap.add_argument("--ignore-nets", nargs="+", default=[], metavar="PAT",
                    help="extra net-name patterns to exclude from airwire scoring")
    ap.add_argument("--clearance", type=float, default=0.1,
                    help="ComponentExcludeTop/Bottom gap between parts in mm "
                         "(default: 0.1)")
    ap.add_argument("--margin", type=float, default=1.0,
                    help="keep parts this far inside the board edge in mm (default: 1.0)")
    ap.add_argument("--refine-only", action="store_true",
                    help="quench the CURRENT layout only (no re-seed); implies a "
                         "bounded --max-displacement (default 5 mm if unset)")
    ap.add_argument("--pos-step", type=float, default=0.5, metavar="MM",
                    help="position granularity of the per-part search and nudge "
                         "(default: 0.5)")
    ap.add_argument("--span", type=float, default=3.0, metavar="MM",
                    help="how far past the first legal ring the search explores "
                         "(default: 3.0)")
    ap.add_argument("--nudge", type=float, default=1.5, metavar="MM",
                    help="quench nudge half-window in mm (default: 1.5)")
    ap.add_argument("--quench-passes", type=int, default=6, metavar="N",
                    help="max quench sweeps (default: 6; stops early on no change)")
    ap.add_argument("--max-displacement", type=float, default=None, metavar="MM",
                    help="cap each part within this distance of its original "
                         "position (default: unbounded)")
    grid_group = ap.add_mutually_exclusive_group()
    ap.set_defaults(grid_mode="fine")
    grid_group.add_argument("--grid", dest="grid_mode", action="store_const",
                            const="grid",
                            help="snap placed origins to Fusion's main grid")
    grid_group.add_argument("--nogrid", dest="grid_mode", action="store_const",
                            const="none",
                            help="do not snap placed origins to a Fusion grid "
                                 "(default snaps to main grid / 10)")
    ap.add_argument("--halo-weight", type=float, default=0.0, metavar="W",
                    help="soft ComponentExcludeTop/Bottom spreading penalty for fanout room "
                         "(default: 0 = off)")
    ap.add_argument("--halo-gap", type=float, default=0.5, metavar="MM",
                    help="base spreading gap, scaled by sqrt(pin count) (default: 0.5)")
    ap.add_argument("--edge-weight", type=float, default=0.0, metavar="W",
                    help="soft board-edge margin penalty (default: 0 = off)")
    ap.add_argument("--edge-margin", type=float, default=2.0, metavar="MM",
                    help="edge soft-margin distance in mm (default: 2.0)")
    args = ap.parse_args()
    if args.rotate <= 0:
        ap.error("--rotate step must be a positive number of degrees")

    bridge = FusionBridge().connect()
    # The selection IS the input. --only is just a manual override of it; with
    # neither, there's nothing to place.
    if args.only:
        only = list(args.only)
        print(f"Placing ({len(only)}): {', '.join(only)}")
    else:
        only = read_selection(bridge)
        if not only:
            print("Nothing selected — no placement. In Fusion's Board editor, "
                  "GROUP-select the parts to place (rubber-band Group tool), "
                  "then re-run.")
            return
        print(f"Selected ({len(only)}): {', '.join(only)}")

    board = read_board(bridge)
    placer = Placer(board, args.ignore_nets, args.clearance, args.margin,
                    args.cross_weight, only=only,
                    halo_weight=args.halo_weight, halo_gap=args.halo_gap,
                    edge_weight=args.edge_weight, edge_margin=args.edge_margin,
                    grid_mode=args.grid_mode, bridge=bridge)
    if placer.fusion_grid_step is None:
        print("Placement grid: off")
    else:
        mode_desc = "grid/10" if args.grid_mode == "fine" else args.grid_mode
        grid_desc = f"{placer.grid_step:g} mm ({mode_desc})"
        print(f"Fusion grid: {placer.fusion_grid_step:g} mm")
        print(f"Placement grid: {grid_desc}")

    if not placer.movable:
        print(f"None of those refs matched a part on the board: {', '.join(only)}")
        return

    print(f"Board {board.outline[2]-board.outline[0]:.0f} x "
          f"{board.outline[3]-board.outline[1]:.0f} mm · {len(board.elements)} parts · "
          f"{len(board.net_pads())} nets ({len(placer.ignore)} power/plane ignored)")
    placing = sorted(board.elements[e].name for e in placer.movable)
    print(f"Placing: {', '.join(placing)} ({len(placer.anchors)} parts frozen)")

    before_len = placer.airwire(placer.orig)
    before_cross = placer.crossings(placer.orig)
    if args.refine_only:
        # Quench the layout as it sits, bounded so it stays "your placement, tidied".
        md = args.max_displacement if args.max_displacement is not None else 5.0
        final = dict(placer.orig)
        rotations = {e: 0.0 for e in placer.movable}
        final, rotations = placer.improve(final, rotations, nudge=args.nudge,
                                          pos_step=args.pos_step, angle_step=args.rotate,
                                          passes=args.quench_passes, max_disp=md)
    else:
        final, rotations = placer.place(pos_step=args.pos_step, angle_step=args.rotate,
                                        span=args.span, max_disp=args.max_displacement)
        final, rotations = placer.improve(final, rotations, nudge=args.nudge,
                                          pos_step=args.pos_step, angle_step=args.rotate,
                                          passes=args.quench_passes,
                                          max_disp=args.max_displacement)
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
        tag = f"  R{rot:g}" if rot else ""
        print(f"  {name:<5} -> ({nx:>8.2f}, {ny:>8.2f})   |d|={d:6.1f}{tag}")
    n_move = sum(1 for a in actions if a[3] > 0.01)
    n_rot = sum(1 for a in actions if a[4])
    print(f"\n{n_move} part(s) move, {n_rot} rotate.")

    # ROTATE (relative, preserves mirror) then MOVE — both pivot on the element
    # origin, so order is immaterial to the final pad positions. The part name
    # MUST be single-quoted for ROTATE: a bare `ROTATE R90 R4` is silently a
    # no-op (the parser doesn't bind the object), unlike MOVE which takes it bare.
    cmds = []
    for name, nx, ny, d, rot, _ in actions:
        if rot:
            cmds.append(f"ROTATE R{rot:g} '{name}'")
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
