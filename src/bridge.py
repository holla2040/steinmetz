"""Steinmetz ↔ Autodesk Fusion Electronics HTTP bridge.

Talk to a live Fusion Electronics design over plain HTTP (JSON-RPC) — no MCP
client library, no plugin. Two capabilities:

* **read**  — :meth:`FusionBridge.electronics_read` walks the board/schematic
  object model (``electronics.Element``, ``.Smd``, ``.Signal``, ``.Package`` …).
* **write** — :meth:`FusionBridge.execute` runs Python inside Fusion;
  :meth:`run_eagle` / :meth:`run_scr` drive the EAGLE command interpreter.
  On builds whose ``adsk.electron`` has ``EcadDrawing.executeCommands`` (Fusion
  Insider 2707.0.73, preview API) :meth:`run_eagle_batch` goes through
  :meth:`execute_commands`, which returns a status and messages per command;
  older builds fall back to ``Electron.run`` (no echo).

Setup (once), on the machine running Fusion (Windows or macOS):

* Fusion running with an Electronics document open.
* Preferences ▸ General ▸ API ▸ "Fusion MCP Server" enabled. Fusion then
  listens on ``127.0.0.1:27182``; run natively on the same machine and nothing
  else is needed.
* Legacy, WSL2 in NAT mode only: a Windows port-forward from the WSL gateway IP
  to Fusion's loopback:27182, bound to the **gateway IP, not 0.0.0.0** (see
  ``docs/fusion-bridge.md``). Mirrored-mode WSL shares the Windows loopback and
  needs no forward — :func:`default_host` probes and picks the right one.

Every non-obvious rule below was learned the hard way and is encoded here so
callers don't rediscover it: hit a host that actually reaches Fusion (loopback
natively and under mirrored WSL; the gateway port-forward under NAT-mode WSL,
where loopback is unreachable), capture ``MCP-Session-Id`` from the *initialize response header* and
resend it, send ``notifications/initialized`` before any ``tools/call``,
default reads to a 1000-row page (the server silently caps at 100), and wrap
board edits in ``Electron.run`` with a trailing ``;`` (a bare ``MOVE`` leaves
its tool active and blocks the whole execute channel).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from typing import Any

import requests

DEFAULT_PORT = 27182
_ACCEPT = "application/json, text/event-stream"

# EcadCommandStatuses values (adsk.electron, preview API).
CMD_SUCCEEDED, CMD_SUCCEEDED_WITH_WARNING, CMD_FAILED, CMD_NOT_EXECUTED = 0, 1, 2, 3

# Fusion-side script for execute_commands. Runs the batch against the active
# Electronics editor (Board, Schematic or Library — all are EcadDrawings) and
# prints the batch result as one JSON line between markers.
_COMMANDS_SCRIPT = '''import adsk.core, adsk.electron, json
def run(_context):
    app = adsk.core.Application.get()
    product = app.activeProduct
    drawing = adsk.electron.EcadDrawing.cast(product)
    if not drawing:
        raise RuntimeError("active product is %s, not a board/schematic/library editor"
                           % (product.objectType if product else None))
    ci = drawing.createCommandInput()
    ci.stopOnError = __STOP__
    ci.rollbackOnError = __ROLLBACK__
    for c in __COMMANDS__:
        ci.add(c)
    r = drawing.executeCommands(ci)
    out = {"success": r.success, "rolledBack": r.rolledBack,
           "drawing": product.objectType,
           "results": [{"command": x.command, "status": x.status,
                        "messages": [m.text for m in x.messages]}
                       for x in r.commandResults]}
    print("__CMD_START__" + json.dumps(out) + "__CMD_END__")
'''


def default_gateway() -> str:
    """Windows host IP as seen from WSL2 *in NAT mode* = the default-route
    gateway. In mirrored mode the gateway is just the LAN router — see
    :func:`default_host`, which picks correctly for both.
    """
    out = subprocess.run(["ip", "route"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("default"):
            return line.split()[2]
    raise RuntimeError("could not determine the default gateway (Windows host IP)")


def is_wsl() -> bool:
    """True when this Python runs inside WSL (Linux on a Windows host)."""
    if not sys.platform.startswith("linux"):
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def fusion_scratch_dir() -> str:
    """Fusion-side directory for Steinmetz's temporary files (ULPs, PNGs, results).

    This is a path on the machine running *Fusion*, written the way Fusion
    spells it. ``STEINMETZ_FUSION_TMP`` overrides it; otherwise ``/tmp`` on
    macOS and ``C:\\tmp`` on Windows (and under WSL, where Fusion runs on the
    Windows host). The path ends up single-quoted inside EAGLE commands, so it
    must not contain quote characters.
    """
    d = os.environ.get("STEINMETZ_FUSION_TMP") or (
        "/tmp" if sys.platform == "darwin" else "C:\\tmp")
    if "'" in d or '"' in d:
        raise ValueError(f"Fusion scratch dir must not contain quotes: {d!r}")
    return d


def fusion_scratch_path(name: str) -> str:
    """``name`` inside :func:`fusion_scratch_dir`, with that dir's separator."""
    d = fusion_scratch_dir()
    sep = "\\" if ("\\" in d or (len(d) >= 2 and d[1] == ":")) else "/"
    return d.rstrip("\\/") + sep + name


def local_path(fusion_path: str) -> str:
    """Where a Fusion-side path is visible to *this* Python.

    Natively (Windows, macOS) Fusion and Steinmetz share a filesystem, so the
    path is unchanged. Under WSL, ``C:\\tmp\\x.png`` maps to ``/mnt/c/tmp/x.png``.
    """
    if not is_wsl():
        return fusion_path
    p = fusion_path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        return "/mnt/" + p[0].lower() + p[2:]
    return p


def default_host(port: int = DEFAULT_PORT) -> str:
    """Pick the host that actually reaches Fusion.

    Natively (Windows, macOS) and under **mirrored** WSL2 networking Fusion is
    on loopback. Only under WSL2 **NAT** networking is loopback unreachable, and
    Fusion is reached through a Windows port-forward on the default-route
    gateway. A quick connect probe on loopback decides; the gateway fallback is
    tried only on Linux (WSL).
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return "127.0.0.1"
    except OSError:
        if sys.platform.startswith("linux"):
            return default_gateway()
        raise FusionError(
            f"Fusion isn't answering on 127.0.0.1:{port}. Check that Fusion is "
            "running, an Electronics document is open, and Preferences > General "
            "> API > Fusion MCP Server is enabled (or set STEINMETZ_FUSION_HOST / "
            "STEINMETZ_FUSION_PORT).") from None


class FusionError(RuntimeError):
    """A JSON-RPC error, or a Fusion-side script failure, from the bridge."""


class FusionBridge:
    """A live connection to Fusion's Electronics HTTP endpoint."""

    def __init__(self, host: str | None = None, port: int = DEFAULT_PORT,
                 timeout: float = 60.0):
        self.port = int(os.environ.get("STEINMETZ_FUSION_PORT", port))
        self.host = host or os.environ.get("STEINMETZ_FUSION_HOST") or default_host(self.port)
        self.url = f"http://{self.host}:{self.port}/mcp"
        self.timeout = timeout
        self._session = requests.Session()
        self._sid: str | None = None
        self._id = 0
        self._command_api: bool | None = None

    # ----- handshake -------------------------------------------------------

    def connect(self) -> "FusionBridge":
        """Run the JSON-RPC handshake once; safe to call repeatedly."""
        if self._sid:
            return self
        # 1) initialize — the session id comes back in the RESPONSE HEADER.
        resp = self._post(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "steinmetz", "version": "0.1"}}},
            raw=True)
        self._sid = resp.headers.get("mcp-session-id")
        if not self._sid:
            raise FusionError("initialize returned no MCP-Session-Id header — is the "
                              "Fusion MCP Server enabled (and, under NAT-mode WSL, "
                              "the port-forward up)?")
        # 2) say "initialized" BEFORE any tools/call, or every call is rejected.
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    # ----- low-level JSON-RPC ---------------------------------------------

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _headers(self) -> dict[str, str]:
        # Fusion's MCP server validates the Host header (DNS-rebinding
        # protection) and 403s anything but localhost. Natively we connect to
        # loopback anyway; through the NAT-mode WSL port-forward we connect to
        # the gateway IP, so always send a loopback Host.
        h = {"Content-Type": "application/json", "Accept": _ACCEPT,
             "Host": f"127.0.0.1:{self.port}"}
        if self._sid:
            h["MCP-Session-Id"] = self._sid
        return h

    def _post(self, payload: dict, raw: bool = False):
        r = self._session.post(self.url, data=json.dumps(payload),
                               headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r if raw else self._parse(r.text)

    @staticmethod
    def _parse(text: str) -> dict:
        """Parse a response body: plain JSON, or SSE-framed (``data:`` lines)."""
        text = text.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            for line in reversed(text.splitlines()):
                line = line.strip()
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            raise

    def _rpc(self, method: str, params: dict) -> Any:
        self.connect()
        out = self._post({"jsonrpc": "2.0", "id": self._next_id(),
                          "method": method, "params": params})
        if isinstance(out, dict) and out.get("error"):
            raise FusionError(out["error"])
        return out.get("result", {})

    def _tool(self, name: str, arguments: dict) -> dict:
        """Call an MCP tool; unwrap the JSON string in ``result.content[0].text``."""
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        return json.loads(result["content"][0]["text"])

    # ----- read ------------------------------------------------------------

    def electronics_read(self, entity_type: str, fields: list[str] | None = None,
                         filters: list[dict] | None = None,
                         page: int = 1000) -> list[dict]:
        """Read all rows of an ``electronics.<Class>`` entity.

        Auto-paginates (the server caps a page at 100 by default / 1000 max),
        so the result is never silently truncated. ``filters`` is a list of
        ``{"property", "op", "value"}``; ``fields`` selects columns (omit = all).
        """
        obj: dict[str, Any] = {}
        if fields:
            obj["fields"] = fields
        if filters:
            obj["filters"] = filters
        items: list[dict] = []
        offset = 0
        while True:
            obj["pagination"] = {"limit": min(page, 1000), "offset": offset}
            payload = self._tool("fusion_mcp_electronics_read",
                                 {"entity_type": entity_type, "object": obj})
            batch = payload.get("items", [])
            items.extend(batch)
            if not batch or not (payload.get("pagination") or {}).get("hasMore"):
                break
            offset += len(batch)
        return items

    def list_entity_types(self) -> list[str]:
        """List the available ``electronics.*`` schema/entity resource URIs."""
        res = self._rpc("resources/list", {})
        return [r.get("uri") for r in res.get("resources", [])]

    def read_schema(self, klass: str) -> dict:
        """Read one entity's field/filter schema, e.g. ``read_schema('element')``."""
        res = self._rpc("resources/read",
                        {"uri": f"resource://mcp.electronics_schema_{klass}"})
        return json.loads(res["contents"][0]["text"])

    # ----- write / execute -------------------------------------------------

    def execute(self, python_source: str) -> dict:
        """Run a Python script inside Fusion. Must define ``def run(_context):``.

        Returns the ``{"message", "success"}`` envelope; raises
        :class:`FusionError` on a Fusion-side failure (e.g. a command dialog
        left open — press Esc in Fusion to recover).
        """
        payload = self._tool("fusion_mcp_execute",
                             {"featureType": "script",
                              "object": {"script": python_source}})
        if not payload.get("success", False):
            raise FusionError(payload.get("error") or payload.get("message") or payload)
        return payload

    def has_command_api(self) -> bool:
        """True when this Fusion build has ``EcadDrawing.executeCommands``.

        Probed once per bridge. Set ``STEINMETZ_LEGACY_WRITE=1`` to force the
        ``Electron.run`` path even when the API is there.
        """
        if os.environ.get("STEINMETZ_LEGACY_WRITE") == "1":
            return False
        if self._command_api is None:
            msg = self.execute(
                "import adsk.electron\n"
                "def run(_context):\n"
                "    print('__API__', hasattr(adsk.electron, 'EcadCommandInput'))\n"
            ).get("message", "") or ""
            self._command_api = "__API__ True" in msg
        return self._command_api

    def execute_commands(self, commands: list[str], stop_on_error: bool = True,
                         rollback_on_error: bool = False) -> dict:
        """Run EAGLE commands as one batch through ``EcadDrawing.executeCommands``.

        Targets the active Electronics editor. Returns ``{"success", "rolledBack",
        "drawing", "results": [{"command", "status", "messages"}]}`` where
        ``status`` is one of the ``CMD_*`` constants. Commands use EAGLE script
        syntax; a trailing ``;`` is accepted. Preview API — needs a build where
        :meth:`has_command_api` is true.

        Verified live (Insider 2707.0.73): NET / NAME / MOVE in a schematic and
        RIPUP / ROUTE on a board run non-interactively; ``rollback_on_error``
        undoes earlier successful commands in the batch. Still true here: board
        MOVEs fail with 'Switch to "Ignore Violator"' while live DRC is on, and
        commands that need more input (``BUS``, ``POLYGON``, some ``NET`` forms)
        can open a dialog that blocks the execute channel until Esc in Fusion.
        """
        script = (_COMMANDS_SCRIPT
                  .replace("__STOP__", repr(bool(stop_on_error)))
                  .replace("__ROLLBACK__", repr(bool(rollback_on_error)))
                  .replace("__COMMANDS__", repr([c.strip() for c in commands])))
        msg = self.execute(script).get("message", "") or ""
        if "__CMD_START__" not in msg or "__CMD_END__" not in msg:
            raise FusionError(f"executeCommands produced no parseable result: {msg!r}")
        return json.loads(msg.split("__CMD_START__", 1)[1].split("__CMD_END__", 1)[0])

    def run_eagle(self, command: str, grid: str | None = None,
                  check: bool = False) -> dict:
        """Fire one EAGLE command through the electronics interpreter."""
        return self.run_eagle_batch([command], grid=grid, check=check)

    def run_eagle_batch(self, commands: list[str], grid: str | None = None,
                        check: bool = False) -> dict:
        """Fire several EAGLE commands in order.

        With the command API (:meth:`has_command_api`) the batch goes through
        :meth:`execute_commands` with ``stop_on_error=False`` — every command
        runs, as with ``Electron.run`` — and the returned envelope gains a
        ``"commands"`` key with the per-command results. ``check=True`` raises
        :class:`FusionError` if any command failed. Without the API, each command
        goes through ``Electron.run``, which returns no echo (``check`` can't see
        failures there; verify by re-reading).

        Each is terminated with ``;`` so it self-completes; an un-terminated
        interactive command (``MOVE``) would leave its tool active and block the
        next execute. ``grid='MM'`` prepends ``GRID MM;`` (set the coordinate
        unit before firing ``MOVE Rn (x y)`` etc.).
        """
        cmds = ([f"GRID {grid}"] if grid else []) + list(commands)
        cmds = [c.strip() if c.strip().endswith(";") else c.strip() + ";" for c in cmds]
        if self.has_command_api():
            res = self.execute_commands(cmds, stop_on_error=False)
            failed = [r for r in res["results"]
                      if r["status"] not in (CMD_SUCCEEDED, CMD_SUCCEEDED_WITH_WARNING)]
            if check and failed:
                raise FusionError(f"EAGLE command(s) failed: {failed}")
            return {"success": True, "commands": res,
                    "message": f"ran {len(cmds)} command(s), {len(failed)} failed"}
        body = ["import adsk.core", "def run(_context):",
                "    app = adsk.core.Application.get()"]
        for c in cmds:
            c = c.strip()
            if '"' in c:
                raise FusionError(f"EAGLE command must not contain a double-quote: {c!r}")
            if not c.endswith(";"):
                c += ";"
            full = f'Electron.run "{c}"'          # repr() handles all escaping
            body.append(f"    app.executeTextCommand({full!r})")
        body.append("    print('ran %d command(s)')" % len(cmds))
        return self.execute("\n".join(body))

    def run_scr(self, windows_path: str, grid: str | None = None) -> dict:
        """Run a ``.scr`` file already on the Fusion host's filesystem.

        ``windows_path`` is a Fusion-host path, e.g. ``r"C:\\tmp\\changes.scr"``
        on Windows (single backslashes) or ``/tmp/changes.scr`` on macOS. The ``.scr`` runs until its first failing
        command; ``Electron.run`` returns no echo, so verify out-of-band.
        """
        full = f'Electron.run "script {windows_path}"'
        body = ["import adsk.core", "def run(_context):",
                "    app = adsk.core.Application.get()"]
        if grid:
            body.append(f"""    app.executeTextCommand({f'Electron.run "GRID {grid};"'!r})""")
        body.append(f"    app.executeTextCommand({full!r})")
        body.append("    print('ran scr')")
        return self.execute("\n".join(body))
