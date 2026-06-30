"""Read a live Fusion Electronics board into a structured, tool-friendly model.

This is the shared "what's on the board" layer that placement (and future tools)
build on. It pulls the raw ``electronics.*`` entities over the bridge and joins
them into elements, connected pads, nets, packages, and the board outline.

The one join that matters: a board net (``Signal``) connects to pads through
``ContactRef`` (``element_object_id`` + ``contact_object_id`` + ``signal_object_id``),
and the pad's placed global position lives on the ``Smd`` / ``Pad`` row keyed by
``contact_object_id``. Joining the two gives, per connected pad: which element,
which net, and where it physically sits.
"""
from __future__ import annotations

from dataclasses import dataclass

from bridge import FusionBridge

# EAGLE "Dimension" layer — board outline wires live here.
OUTLINE_LAYER = 20


@dataclass
class Element:
    """A placed component (footprint) on the board."""
    object_id: int
    name: str
    x: float
    y: float
    angle: float
    package_object_id: int
    value: str = ""


@dataclass
class Pad:
    """One connected pad: its net, its owning element, its global position."""
    element_id: int
    signal_id: int
    x: float
    y: float


@dataclass
class Board:
    elements: dict[int, Element]      # by object_id
    packages: dict[int, dict]         # object_id -> {name, x1, y1, x2, y2}
    signals: dict[int, str]           # object_id -> net name
    pads: list[Pad]                   # connected pads only
    outline: tuple[float, float, float, float]  # (min_x, min_y, max_x, max_y)
    unit: str = "mm"

    def by_name(self) -> dict[str, Element]:
        return {e.name: e for e in self.elements.values()}

    def net_pads(self) -> dict[int, list[Pad]]:
        """signal_id -> its connected pads."""
        out: dict[int, list[Pad]] = {}
        for p in self.pads:
            out.setdefault(p.signal_id, []).append(p)
        return out

    def pads_of(self, element_id: int) -> list[Pad]:
        return [p for p in self.pads if p.element_id == element_id]

    def pkg_bbox(self, element_id: int) -> tuple[float, float, float, float]:
        """Local package bounding box (courtyard proxy), in board units."""
        pk = self.packages.get(self.elements[element_id].package_object_id, {})
        return (pk.get("x1", -0.5), pk.get("y1", -0.5),
                pk.get("x2", 0.5), pk.get("y2", 0.5))


def read_board(bridge: FusionBridge) -> Board:
    """Pull the active Fusion Electronics board into a :class:`Board`."""
    els = bridge.electronics_read(
        "electronics.Element",
        fields=["object_id", "name", "value", "x", "y", "angle", "package_object_id"])
    pkgs = bridge.electronics_read(
        "electronics.Package", fields=["object_id", "name", "x1", "y1", "x2", "y2"])
    sigs = bridge.electronics_read("electronics.Signal", fields=["object_id", "name"])
    crefs = bridge.electronics_read(
        "electronics.ContactRef",
        fields=["element_object_id", "contact_object_id", "signal_object_id"])
    smds = bridge.electronics_read(
        "electronics.Smd", fields=["object_id", "contact_object_id", "x", "y"])
    pads_th = bridge.electronics_read(
        "electronics.Pad", fields=["object_id", "contact_object_id", "x", "y"])
    wires = bridge.electronics_read("electronics.Wire", fields=["x1", "y1", "x2", "y2", "layer"])

    elements = {e["object_id"]: Element(
        object_id=e["object_id"], name=e["name"], value=e.get("value") or "",
        x=e["x"], y=e["y"], angle=e["angle"],
        package_object_id=e["package_object_id"]) for e in els}
    packages = {p["object_id"]: p for p in pkgs}
    signals = {s["object_id"]: s["name"] for s in sigs}

    # contact_object_id -> placed global (x, y), from both SMD and TH pads
    contact_xy = {c["contact_object_id"]: (c["x"], c["y"]) for c in smds}
    contact_xy.update({c["contact_object_id"]: (c["x"], c["y"]) for c in pads_th})

    pads: list[Pad] = []
    for cr in crefs:
        xy = contact_xy.get(cr["contact_object_id"])
        if xy is None:
            continue
        pads.append(Pad(element_id=cr["element_object_id"],
                        signal_id=cr["signal_object_id"], x=xy[0], y=xy[1]))

    outline_wires = [w for w in wires if w.get("layer") == OUTLINE_LAYER]
    if outline_wires:
        xs = [w["x1"] for w in outline_wires] + [w["x2"] for w in outline_wires]
        ys = [w["y1"] for w in outline_wires] + [w["y2"] for w in outline_wires]
        outline = (min(xs), min(ys), max(xs), max(ys))
    else:  # fall back to the extent of placed pads, padded a little
        xs = [p.x for p in pads] or [0.0]
        ys = [p.y for p in pads] or [0.0]
        outline = (min(xs) - 5, min(ys) - 5, max(xs) + 5, max(ys) + 5)

    return Board(elements=elements, packages=packages, signals=signals,
                 pads=pads, outline=outline)
