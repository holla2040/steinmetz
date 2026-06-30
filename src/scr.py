"""Generate Fusion Electronics ``.scr`` scripts (EAGLE command-line scripts).

A ``.scr`` is just a newline-separated list of EAGLE commands the Electronics
command interpreter runs in order. Steinmetz fires them over the bridge either
from a file (:meth:`FusionBridge.run_scr`) or directly without a file
(:meth:`FusionBridge.run_eagle_batch`).

Two safety rules, learned the hard way and enforced here:

* **Each command is terminated with ``;``** so it self-completes. A bare
  interactive command such as ``MOVE Rn (x y)`` otherwise leaves its tool active
  and blocks the whole execute channel until someone presses Esc in Fusion.
* **Control characters / embedded newlines and ``"`` are rejected**, so one
  command cannot smuggle another and nothing breaks the ``Electron.run "…"``
  wrapper. (Single quotes are fine — EAGLE string args use them, e.g.
  ``CHANGE PACKAGE '-0402' R4``.)

The validation approach is adapted from Hendley's ``scr.py``; this version is
generalized to arbitrary commands rather than the part-swap shape.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

# Control chars (incl. newline/CR) are forbidden mid-command; tab is tolerated.
_FORBIDDEN = {chr(c) for c in range(0x00, 0x20)} - {"\t"}


def sanitize(command: str) -> str:
    """Validate a single EAGLE command (no termination added)."""
    c = command.strip()
    if not c:
        raise ValueError("empty command")
    bad = sorted({ch for ch in c if ch in _FORBIDDEN})
    if bad:
        raise ValueError(f"command {c!r} contains control character(s) {bad}")
    if '"' in c:
        raise ValueError(f"command {c!r} contains a double-quote, which breaks the "
                         "Electron.run wrapper (EAGLE string args use single quotes)")
    return c


def terminate(command: str) -> str:
    """Validate and ensure a command ends with ``;`` (self-completing)."""
    c = sanitize(command)
    return c if c.endswith(";") else c + ";"


def render_scr(commands: Iterable[str], header: str | None = None,
               grid: str | None = "MM") -> str:
    """Render commands into one ``.scr`` body.

    ``grid`` (default ``"MM"``) prepends ``GRID MM;`` so coordinate commands use
    millimetres — pass ``None`` to leave the document's current unit untouched.
    """
    lines = ["# Steinmetz-generated Fusion Electronics script (.scr)",
             "# Run in Electronics: File > Execute Script, or fire over the bridge."]
    if header:
        lines += [f"# {h}" for h in header.splitlines()]
    lines.append("")
    if grid:
        lines.append(f"GRID {grid};")
    lines += [terminate(c) for c in commands]
    return "\n".join(lines) + "\n"


def write_scr(commands: Iterable[str], path: str | Path, **kw) -> Path:
    """Render and write a ``.scr`` file; returns its :class:`~pathlib.Path`."""
    p = Path(path)
    p.write_text(render_scr(commands, **kw))
    return p
