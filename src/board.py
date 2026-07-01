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
import math

from bridge import FusionBridge

# EAGLE "Dimension" layer — board outline wires live here.
OUTLINE_LAYER = 20
COMPONENT_EXCLUDE_TOP = "ComponentExcludeTop"
COMPONENT_EXCLUDE_BOTTOM = "ComponentExcludeBottom"


def _read_mirror_flags(bridge: FusionBridge) -> dict[int, bool]:
    """Best-effort ``object_id -> mirrored`` for elements.

    Rotation prediction assumes a top-side (un-mirrored) part — a mirrored part
    rotates the opposite way visually. The mirror/side column name isn't stable
    across Fusion versions and may be absent, so probe a few names and tolerate
    failure; an empty map just means "treat everything as top-side" (the
    pad-position check on ``--apply`` is the real guard).
    """
    boolish = {True, False, 0, 1, None}
    for field in ("mirror", "mirrored", "is_mirrored"):
        try:
            rows = bridge.electronics_read(
                "electronics.Element", fields=["object_id", field])
        except Exception:
            continue
        if not (rows and field in rows[0]):
            continue
        # Only trust a genuinely boolean column. A look-alike that holds a layer
        # number or an angle string would otherwise mark *every* part mirrored
        # and silently disable rotation (a no-op the pad check can't catch).
        if all(r.get(field) in boolish for r in rows):
            return {r["object_id"]: bool(r.get(field)) for r in rows}
    return {}


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
    mirror: bool = False          # placed on the bottom side (flips rotation sense)


@dataclass
class Pad:
    """One connected pad: its net, its owning element, its global position."""
    element_id: int
    signal_id: int
    x: float
    y: float


@dataclass
class ContactBox:
    """One package contact/body box in package-local coordinates."""
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class Board:
    elements: dict[int, Element]      # by object_id
    packages: dict[int, dict]         # object_id -> {name, x1, y1, x2, y2}
    signals: dict[int, str]           # object_id -> net name
    pads: list[Pad]                   # connected pads only
    contact_boxes: dict[int, list[ContactBox]]  # element_id -> local hard-geometry boxes
    exclude_boxes: dict[int, list[ContactBox]]  # element_id -> local component-exclude boxes
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
        """Local raw package bounding box, in board units.

        This often includes text/value/artwork extents. Placement legality should
        use :meth:`placement_bbox`, not this package-level diagnostic extent.
        """
        pk = self.packages.get(self.elements[element_id].package_object_id, {})
        return (pk.get("x1", -0.5), pk.get("y1", -0.5),
                pk.get("x2", 0.5), pk.get("y2", 0.5))

    def copper_bbox(self, element_id: int, pad: float = 0.2) -> tuple[float, float, float, float]:
        """Local hard-geometry box from package contacts plus a small body pad.

        Fusion's Package bbox often includes text, value/name origins, and other
        drawing/artwork, so it is too conservative as placement keepout. Contact
        copper is the stable hard geometry exposed by the bridge; the extra pad
        accounts for passive body/copper clearance without inheriting silkscreen
        extents.
        """
        boxes = self.contact_boxes.get(element_id) or []
        if not boxes:
            return self.pkg_bbox(element_id)
        return (min(b.x1 for b in boxes) - pad,
                min(b.y1 for b in boxes) - pad,
                max(b.x2 for b in boxes) + pad,
                max(b.y2 for b in boxes) + pad)

    def placement_bbox(self, element_id: int) -> tuple[float, float, float, float]:
        """Local hard placement box, preferring ComponentExcludeTop/Bottom.

        Component exclude geometry is the package author's intended placement
        keepout. Copper/contact boxes are a fallback for packages that do not
        define the exclude layer.
        """
        boxes = self.exclude_boxes.get(element_id) or []
        if boxes:
            return (min(b.x1 for b in boxes), min(b.y1 for b in boxes),
                    max(b.x2 for b in boxes), max(b.y2 for b in boxes))
        return self.copper_bbox(element_id)


def _rot(dx: float, dy: float, a: float) -> tuple[float, float]:
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


def _median(values: list[float], default: float) -> float:
    if not values:
        return default
    vals = sorted(values)
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def _rotated_box(points: list[tuple[float, float]], angle: float) -> ContactBox:
    corners = [_rot(x, y, angle) for x, y in points]
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return ContactBox(min(xs), min(ys), max(xs), max(ys))


def read_board(bridge: FusionBridge) -> Board:
    """Pull the active Fusion Electronics board into a :class:`Board`."""
    els = bridge.electronics_read(
        "electronics.Element",
        fields=["object_id", "name", "value", "x", "y", "angle", "package_object_id"])
    mirror_by_id = _read_mirror_flags(bridge)
    pkgs = bridge.electronics_read(
        "electronics.Package", fields=["object_id", "name", "x1", "y1", "x2", "y2"])
    contacts = bridge.electronics_read(
        "electronics.Contact", fields=["object_id", "name", "x", "y", "package_object_id"])
    layers = bridge.electronics_read(
        "electronics.Layer", fields=["name", "number"])
    sigs = bridge.electronics_read("electronics.Signal", fields=["object_id", "name"])
    crefs = bridge.electronics_read(
        "electronics.ContactRef",
        fields=["element_object_id", "contact_object_id", "signal_object_id"])
    smds = bridge.electronics_read(
        "electronics.Smd", fields=["object_id", "name", "contact_object_id", "x", "y", "dx", "dy"])
    pads_th = bridge.electronics_read(
        "electronics.Pad", fields=["object_id", "contact_object_id", "x", "y"])
    wires = bridge.electronics_read(
        "electronics.Wire",
        fields=["x1", "y1", "x2", "y2", "width", "layer", "package_object_id", "board_object_id"])
    rects = bridge.electronics_read(
        "electronics.Rectangle",
        fields=["x1", "y1", "x2", "y2", "angle", "layer", "package_object_id", "board_object_id"])
    circles = bridge.electronics_read(
        "electronics.Circle",
        fields=["x", "y", "radius", "width", "layer", "package_object_id", "board_object_id"])

    elements = {e["object_id"]: Element(
        object_id=e["object_id"], name=e["name"], value=e.get("value") or "",
        x=e["x"], y=e["y"], angle=e["angle"],
        package_object_id=e["package_object_id"],
        mirror=mirror_by_id.get(e["object_id"], False)) for e in els}
    packages = {p["object_id"]: p for p in pkgs}
    signals = {s["object_id"]: s["name"] for s in sigs}
    layer_num = {r["name"]: r["number"] for r in layers}
    exclude_top = layer_num.get(COMPONENT_EXCLUDE_TOP, 39)
    exclude_bottom = layer_num.get(COMPONENT_EXCLUDE_BOTTOM, 40)
    crefs_by_contact = {c["contact_object_id"]: c for c in crefs}

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

    # Size hints are only directly joinable for connected SMDs, but those are
    # enough to learn the pad dimensions for each package/contact name. Missing
    # contacts fall back to the package's median observed SMD size.
    smd_size_by_pkg_name: dict[tuple[int, str], tuple[float, float]] = {}
    sizes_by_pkg: dict[int, list[tuple[float, float]]] = {}
    for s in smds:
        cr = crefs_by_contact.get(s["contact_object_id"])
        if cr is None:
            continue
        elem = elements.get(cr["element_object_id"])
        if elem is None:
            continue
        size = (float(s.get("dx") or 0.0), float(s.get("dy") or 0.0))
        if size[0] <= 0 or size[1] <= 0:
            continue
        key = (elem.package_object_id, str(s.get("name") or ""))
        smd_size_by_pkg_name[key] = size
        sizes_by_pkg.setdefault(elem.package_object_id, []).append(size)

    contacts_by_pkg: dict[int, list[dict]] = {}
    for c in contacts:
        contacts_by_pkg.setdefault(c["package_object_id"], []).append(c)

    contact_boxes: dict[int, list[ContactBox]] = {}
    for eid, elem in elements.items():
        pkg_contacts = contacts_by_pkg.get(elem.package_object_id, [])
        pkg_sizes = sizes_by_pkg.get(elem.package_object_id, [])
        default_dx = _median([s[0] for s in pkg_sizes], 0.5)
        default_dy = _median([s[1] for s in pkg_sizes], 0.5)
        boxes: list[ContactBox] = []
        for c in pkg_contacts:
            dx, dy = smd_size_by_pkg_name.get(
                (elem.package_object_id, str(c.get("name") or "")),
                (default_dx, default_dy))
            hx, hy = dx / 2, dy / 2
            boxes.append(ContactBox(c["x"] - hx, c["y"] - hy,
                                    c["x"] + hx, c["y"] + hy))
        if boxes:
            contact_boxes[eid] = boxes

    exclude_by_pkg_layer: dict[tuple[int, int], list[ContactBox]] = {}
    exclude_layers = {exclude_top, exclude_bottom}
    for w in wires:
        pkg_id = w.get("package_object_id") or 0
        if not pkg_id or w.get("layer") not in exclude_layers:
            continue
        hw = float(w.get("width") or 0.0) / 2
        x1, y1, x2, y2 = w["x1"], w["y1"], w["x2"], w["y2"]
        box = ContactBox(min(x1, x2) - hw, min(y1, y2) - hw,
                         max(x1, x2) + hw, max(y1, y2) + hw)
        exclude_by_pkg_layer.setdefault((pkg_id, w["layer"]), []).append(box)
    for r in rects:
        pkg_id = r.get("package_object_id") or 0
        if not pkg_id or r.get("layer") not in exclude_layers:
            continue
        x1, y1, x2, y2 = r["x1"], r["y1"], r["x2"], r["y2"]
        box = _rotated_box([(x1, y1), (x2, y1), (x2, y2), (x1, y2)],
                           float(r.get("angle") or 0.0))
        exclude_by_pkg_layer.setdefault((pkg_id, r["layer"]), []).append(box)
    for c in circles:
        pkg_id = c.get("package_object_id") or 0
        if not pkg_id or c.get("layer") not in exclude_layers:
            continue
        rad = float(c.get("radius") or 0.0) + float(c.get("width") or 0.0) / 2
        box = ContactBox(c["x"] - rad, c["y"] - rad, c["x"] + rad, c["y"] + rad)
        exclude_by_pkg_layer.setdefault((pkg_id, c["layer"]), []).append(box)

    exclude_boxes: dict[int, list[ContactBox]] = {}
    for eid, elem in elements.items():
        layer = exclude_bottom if elem.mirror else exclude_top
        boxes = exclude_by_pkg_layer.get((elem.package_object_id, layer), [])
        if not boxes and layer == exclude_bottom:
            boxes = exclude_by_pkg_layer.get((elem.package_object_id, exclude_top), [])
        if not boxes:
            continue
        exclude_boxes[eid] = boxes

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
                 pads=pads, contact_boxes=contact_boxes,
                 exclude_boxes=exclude_boxes, outline=outline)
