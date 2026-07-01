#!/usr/bin/env python3
"""Read-only placement evaluator for the live Fusion board."""
from __future__ import annotations

import argparse
import importlib.util
import inspect
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from board import read_board
from bridge import FusionBridge
from place import _read_main_grid_mm, _resolve_grid_step
from selection import read_selection


def load_placer(path: str | None):
    if path is None:
        from place import Placer
        return Placer
    spec = importlib.util.spec_from_file_location("candidate_place", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load placer from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Placer


def make_placer(Placer, board, args, only, bridge):
    sig = inspect.signature(Placer).parameters
    kwargs = {
        "only": only,
    }
    if "grid_mode" in sig:
        kwargs["grid_mode"] = args.grid_mode
    if "bridge" in sig:
        kwargs["bridge"] = bridge
    elif "grid_step" in sig:
        main_grid = _read_main_grid_mm(bridge)
        kwargs["grid_step"] = _resolve_grid_step(args.grid_mode, main_grid)
    elif args.grid_mode != "none":
        raise RuntimeError("grid snapping requires a placer that accepts grid settings")
    return Placer(board, args.ignore_nets, args.clearance, args.margin,
                  args.cross_weight, **kwargs)


def overlaps(placer, centers, rotations):
    bad = []
    items = sorted(placer.b.elements, key=lambda e: placer.b.elements[e].name)
    for i, ei in enumerate(items):
        bi = placer._bbox(ei, centers[ei], rotations)
        for ej in items[i + 1:]:
            bj = placer._bbox(ej, centers[ej], rotations)
            g = placer.clearance
            if not (bi[2] + g <= bj[0] or bj[2] + g <= bi[0]
                    or bi[3] + g <= bj[1] or bj[3] + g <= bi[1]):
                bad.append((placer.b.elements[ei].name, placer.b.elements[ej].name))
    return bad


def movement_stats(placer, centers):
    ds = [math.hypot(centers[e][0] - placer.orig[e][0],
                     centers[e][1] - placer.orig[e][1])
          for e in placer.movable]
    if not ds:
        return 0.0, 0.0
    return sum(ds) / len(ds), max(ds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="+", default=[])
    ap.add_argument("--ignore-nets", nargs="+", default=[])
    ap.add_argument("--clearance", type=float, default=0.1)
    ap.add_argument("--margin", type=float, default=1.0)
    ap.add_argument("--cross-weight", type=float, default=2.0)
    ap.add_argument("--rotate", type=float, default=90.0)
    ap.add_argument("--pos-step", type=float, default=0.5)
    ap.add_argument("--span", type=float, default=3.0)
    ap.add_argument("--nudge", type=float, default=1.5)
    ap.add_argument("--quench-passes", type=int, default=6)
    ap.add_argument("--max-displacement", type=float, default=None)
    ap.add_argument("--refine-only", action="store_true")
    ap.add_argument("--place-path", default=None)
    grid_group = ap.add_mutually_exclusive_group()
    ap.set_defaults(grid_mode="fine")
    grid_group.add_argument("--grid", dest="grid_mode", action="store_const",
                            const="grid")
    grid_group.add_argument("--nogrid", dest="grid_mode", action="store_const",
                            const="none")
    args = ap.parse_args()

    Placer = load_placer(args.place_path)
    bridge = FusionBridge().connect()
    only = args.only or read_selection(bridge)
    board = read_board(bridge)
    placer = make_placer(Placer, board, args, only, bridge)
    before_len = placer.airwire(placer.orig)
    before_cross = placer.crossings(placer.orig)
    if hasattr(placer, "place"):
        new_api = True
    else:
        new_api = False
    if args.refine_only and new_api:
        md = args.max_displacement if args.max_displacement is not None else 5.0
        final = dict(placer.orig)
        rotations = {e: 0.0 for e in placer.movable}
        final, rotations = placer.improve(final, rotations, nudge=args.nudge,
                                          pos_step=args.pos_step,
                                          angle_step=args.rotate,
                                          passes=args.quench_passes,
                                          max_disp=md)
    elif new_api:
        final, rotations = placer.place(pos_step=args.pos_step,
                                        angle_step=args.rotate,
                                        span=args.span,
                                        max_disp=args.max_displacement)
        final, rotations = placer.improve(final, rotations, nudge=args.nudge,
                                          pos_step=args.pos_step,
                                          angle_step=args.rotate,
                                          passes=args.quench_passes,
                                          max_disp=args.max_displacement)
    else:
        final = placer.solve()
        rotations = placer.refine_rotations(final, step=args.rotate)
    after_len = placer.airwire(final, rotations)
    after_cross = placer.crossings(final, rotations)
    bad = overlaps(placer, final, rotations)
    avg_move, max_move = movement_stats(placer, final)
    print(f"Selected: {', '.join(sorted(board.elements[e].name for e in placer.movable))}")
    main_grid = getattr(placer, "fusion_grid_step", None)
    grid_step = getattr(placer, "grid_step", None)
    if main_grid is not None:
        print(f"Fusion grid: {main_grid:g} mm")
    mode_desc = "grid/10" if args.grid_mode == "fine" else args.grid_mode
    grid_desc = "off" if grid_step is None else f"{grid_step:g} mm ({mode_desc})"
    print(f"Placement grid: {grid_desc}")
    print(f"Signal-airwire: {before_len:.1f} -> {after_len:.1f} mm")
    print(f"Crossings: {before_cross} -> {after_cross}")
    print(f"Movement: avg {avg_move:.2f} mm, max {max_move:.2f} mm")
    print(f"Overlaps: {len(bad)}")
    if bad:
        print("  " + ", ".join(f"{a}/{b}" for a, b in bad[:20]))
    for eid in sorted(placer.movable, key=lambda e: board.elements[e].name):
        ox, oy = placer.orig[eid]
        nx, ny = final[eid]
        rot = rotations.get(eid, 0.0) % 360
        print(f"{board.elements[eid].name:5} ({nx:8.2f}, {ny:8.2f}) "
              f"d={math.hypot(nx - ox, ny - oy):6.2f} R{rot:g}")


if __name__ == "__main__":
    main()
