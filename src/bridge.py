"""Steinmetz ↔ Autodesk Fusion Electronics HTTP bridge.

Talk to a live Fusion Electronics design over plain HTTP (JSON-RPC) — no MCP
client library, no plugin. Two capabilities:

* **read**  — :meth:`FusionBridge.electronics_read` walks the board/schematic
  object model (``electronics.Element``, ``.Smd``, ``.Signal``, ``.Package`` …).
* **write** — :meth:`FusionBridge.execute` runs Python inside Fusion;
  :meth:`run_eagle` / :meth:`run_scr` drive the EAGLE command interpreter via
  ``Electron.run`` (the object API is read-only, the command line is not).

Windows-side setup (once):

* Fusion running with an Electronics document open.
* Preferences ▸ General ▸ API ▸ "Fusion MCP Server" enabled.
* A Windows port-forward from the WSL gateway IP to Fusion's loopback:27182,
  bound to the **gateway IP, not 0.0.0.0** (see ``docs/fusion-bridge.md``).

Every non-obvious rule below was learned the hard way and is encoded here so
callers don't rediscover it: hit the gateway IP (loopback is unreachable from
WSL2), capture ``MCP-Session-Id`` from the *initialize response header* and
resend it, send ``notifications/initialized`` before any ``tools/call``,
default reads to a 1000-row page (the server silently caps at 100), and wrap
board edits in ``Electron.run`` with a trailing ``;`` (a bare ``MOVE`` leaves
its tool active and blocks the whole execute channel).
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import requests

DEFAULT_PORT = 27182
_ACCEPT = "application/json, text/event-stream"


def default_gateway() -> str:
    """Windows host IP as seen from WSL2 = the default-route gateway.

    Loopback (127.0.0.1) is NOT reachable from WSL2; Fusion listens on the
    Windows loopback, reached through the port-forward bound to this IP.
    """
    out = subprocess.run(["ip", "route"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("default"):
            return line.split()[2]
    raise RuntimeError("could not determine the default gateway (Windows host IP)")


class FusionError(RuntimeError):
    """A JSON-RPC error, or a Fusion-side script failure, from the bridge."""


class FusionBridge:
    """A live connection to Fusion's Electronics HTTP endpoint."""

    def __init__(self, host: str | None = None, port: int = DEFAULT_PORT,
                 timeout: float = 60.0):
        self.host = host or os.environ.get("STEINMETZ_FUSION_HOST") or default_gateway()
        self.port = int(os.environ.get("STEINMETZ_FUSION_PORT", port))
        self.url = f"http://{self.host}:{self.port}/mcp"
        self.timeout = timeout
        self._session = requests.Session()
        self._sid: str | None = None
        self._id = 0

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
                              "Fusion MCP Server enabled and the port-forward up?")
        # 2) say "initialized" BEFORE any tools/call, or every call is rejected.
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    # ----- low-level JSON-RPC ---------------------------------------------

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": _ACCEPT}
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

    def run_eagle(self, command: str, grid: str | None = None) -> dict:
        """Fire one EAGLE command through the electronics interpreter."""
        return self.run_eagle_batch([command], grid=grid)

    def run_eagle_batch(self, commands: list[str], grid: str | None = None) -> dict:
        """Fire several EAGLE commands in order via ``Electron.run``.

        Each is terminated with ``;`` so it self-completes; an un-terminated
        interactive command (``MOVE``) would leave its tool active and block the
        next execute. ``grid='MM'`` prepends ``GRID MM;`` (set the coordinate
        unit before firing ``MOVE Rn (x y)`` etc.).
        """
        cmds = ([f"GRID {grid}"] if grid else []) + list(commands)
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
        """Run a ``.scr`` file already on the Fusion (Windows) filesystem.

        ``windows_path`` is a Fusion-host path with single backslashes, e.g.
        ``r"C:\\tmp\\changes.scr"``. The ``.scr`` runs until its first failing
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
