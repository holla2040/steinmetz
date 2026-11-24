# Feature request: Be able to create a floating ground symbol with no layout component

**Product:** Autodesk Fusion, Electronics workspace (library editor + schematic editor)
**Area:** Library / symbol / supply symbols
**Date:** 2026-09-02
**Submit to:** Fusion Ideas board (forums.autodesk.com → Fusion → Ideas), category Electronics

## Summary

Let me create a ground-style schematic symbol whose net is chosen per placed
instance, and which has **no footprint and no board element**. Today a symbol
can have one or the other, never both: a package-less symbol must use supply
pins whose net name is hard-wired to the pin name, and any symbol that can
attach to an arbitrary net must carry a footprint that lands copper on the
board.

## The use case

Designs with isolated or floating domains need a ground glyph that means "the
floating ground of *this* domain": a gate-driver floating ground riding on a
switch node, an isolated secondary return, a bootstrap node, a floating
measurement reference. There can be several of these in one design, and the
net each one belongs to is decided on the schematic, not in the library. The
symbol is a drawing convention only; it must never generate a part, a pad, a
BOM line, or a board element.

## What I tried, and what Fusion does today

Library `holla`, device set `GND-FLOATING`, one gate, symbol `GND-FLOATING`
with a single pin at the origin. Compared against the stock-style `GND` and
`AGND` supply symbols in the same library.

| Attempt | Pin direction | Footprint | Result |
|---|---|---|---|
| Copy of `AGND` | Sup | none | Adds fine, but the net is forced to the pin name. Cannot be renamed per instance. |
| Passive pin, no footprint | Pas | none | `ADD` refused: component needs a package. |
| Passive pin, empty footprint (no pads) | Pas | empty | `ADD` refused: pin is not connected to a pad. |
| Passive pin, test-pad footprint, pin connected | Pas | 0.5 mm SMD pad | Adds and connects to any net, but every instance becomes a board element with copper. |

So the rules as implemented are:

1. A device with no footprint may only be placed if **every** pin has direction
   `Sup`.
2. A `Sup` pin's net name is the pin name. It is global and not editable on the
   placed instance.
3. Any other pin direction requires a footprint, and every pin must be connected
   to a pad.

The only workaround is the `AGND` pattern: one supply device set per floating
domain (`GND_FL1`, `GND_FL2`, `GND_ISO`, …). That means editing the library for
every new domain, and the net is still baked in rather than chosen on the sheet.

## Requested behavior (any one of these solves it)

1. **Per-instance net name on supply symbols.** Let the net name of a placed
   supply symbol be overridden on the instance, for example through `NAME` on
   the instance or a dedicated property, with the symbol able to display it
   via a `>NET`-style placeholder. This mirrors how power ports work in other
   ECAD tools, where the glyph style and the net name are independent
   per-instance properties.
2. **Schematic-only components.** A device flag (or a footprint-less device
   with passive pins) that Fusion accepts on the schematic, attaches to
   whatever net it is wired to, and excludes from the board, BOM, and ERC
   "unconnected pin" checks.
3. **Footprint with zero pads and unconnected pins allowed.** Relax the
   "every pin must be connected to a pad" check when the footprint has no
   pads at all, and do not create a board element for such a device.

Option 1 is the most natural fit for how supply symbols already behave.

## Why it matters

Isolated gate drivers, multi-channel isolated supplies, and floating sense
references are common in power electronics. Every one of them needs a floating
ground glyph, and today each glyph either pollutes the board with a dummy
pad or forces a library edit per domain. A per-instance net on a supply symbol
removes both problems and matches long-standing behavior in competing tools.
