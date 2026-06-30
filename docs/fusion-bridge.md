# The Fusion Electronics HTTP bridge

Steinmetz talks to a live Autodesk Fusion Electronics design over **plain HTTP
(JSON-RPC)** — no MCP client library, no plugin. Fusion publishes a local HTTP
endpoint; you `POST` JSON-RPC to it and call two tools:

- `fusion_mcp_electronics_read` — read the design's object model.
- `fusion_mcp_execute` — run Python inside Fusion (the write path).

`src/bridge.py` (`FusionBridge`) implements everything below; this doc is the
reference for *why* it does what it does. Every rule here was learned the hard
way — skip one and you get a confusing error.

## One-time setup (Windows side)

1. Fusion running, an **Electronics document open** (schematic or board).
2. **Preferences ▸ General ▸ API ▸ "Fusion MCP Server"** enabled. (This Autodesk
   toggle is the only thing here called "MCP" — it just publishes the HTTP
   endpoint on `127.0.0.1:27182`.)
3. From WSL2 you can't reach the Windows loopback, so forward Fusion's port to
   the **WSL gateway IP** (the Windows host as seen from WSL):

   ```powershell
   # GWIP = `ip route | grep default | awk '{print $3}'` from WSL, e.g. 172.17.64.1
   netsh interface portproxy add v4tov4 listenaddress=GWIP listenport=27182 ^
         connectaddress=127.0.0.1 connectport=27182
   ```

   ⚠️ **Bind the gateway IP, never `0.0.0.0`.** A `0.0.0.0:27182` listener hijacks
   the loopback Fusion's own server uses, so it "connects then closes
   unexpectedly." Health check (on Windows): `curl http://127.0.0.1:27182/mcp`
   should return `{"error":"Not Found"}` when healthy.

## The handshake (each rule bites if skipped)

1. **Hit the gateway IP, not `127.0.0.1`** — loopback is unreachable from WSL2.
2. **`initialize` once**, and capture **`MCP-Session-Id` from the *response
   header***. Resend it on every later request. Omit it → `Missing MCP-Session-Id
   header`.
3. **Send `notifications/initialized` before any `tools/call`.** Otherwise →
   `Session not initialized`.
4. Send `Accept: application/json, text/event-stream` on every request.
5. A tool's rows come back as a **JSON string** in `result.content[0].text` →
   parse that → `{ "items": [...], "pagination": {...} }`.

## Reading the design

`fusion_mcp_electronics_read(entity_type, object?)` where `object` is
`{ fields[], filters[{property,op,value}], pagination{limit,offset} }`.

With a **board** open, the full EAGLE object model is visible — far more than the
schematic `Part`/`Attribute`. The pieces that matter for placement/geometry:

| entity | key fields |
|--------|-----------|
| `electronics.Element` | `name`, `value`, `x`, `y`, `angle`, `mirror`, `package_object_id` — the board placement |
| `electronics.Smd` / `Pad` | `x`, `y` in **global board coords**, `angle`, `layer`, `contact_object_id`, `signal` |
| `electronics.Signal` | `name` (board net) |
| `electronics.ContactRef` | `element_object_id` + `contact_object_id` + `signal_object_id` — the connectivity graph |
| `electronics.Package` | bbox `x1,y1,x2,y2` (courtyard proxy) |
| `electronics.Wire` | `x1,y1,x2,y2,layer`; **layer 20** carries the board outline |

Join `ContactRef.contact_object_id == Smd.contact_object_id` to get, per
connected pad: element + signal + global position. Full schema for any class:
`resources/read resource://mcp.electronics_schema_<class>` (or
`FusionBridge.read_schema("element")`).

⚠️ **Reads cap at 100 rows by default** (`pagination.hasMore`); max `limit` is
1000. A naive single read silently truncates `Smd`/`ContactRef` → missing
connectivity. `FusionBridge.electronics_read` auto-paginates, so use it.

Coordinates are returned in the document's active unit, reported as
`coordinate_unit` in each response (e.g. `mm`); filter values for
`document_grid_unit` fields must use that same unit.

## The write path — driving the EAGLE command line

The Electronics **object** API is read-only. The writable channel is the
**EAGLE command interpreter**, reached by running Python in Fusion via
`fusion_mcp_execute` and wrapping each command in **`Electron.run "…"`**:

```python
import adsk.core
def run(_context):
    app = adsk.core.Application.get()
    app.executeTextCommand('Electron.run "MOVE R7 (44.52 19.42);"')
```

- A **bare** `executeTextCommand("script …")` / `"GRID"` / `"MOVE …"` hits
  Fusion's *core* channel and fails (`There is no command …`). `Electron.run`
  dispatches into the *electronics* interpreter.
- `fusion_mcp_execute` args: `{featureType:"script", object:{script:"<python>"}}`;
  the script **must define `def run(_context):`**. Its `print()` output returns
  in `{"message", "success"}` (note: the read tool returns `{"items"}` instead).
- `Electron.run` returns `''` — **no echo**. Verify out-of-band: re-read the
  affected entity, or `Electron.run "EXPORT PARTLIST C:\\tmp\\x.txt"` then read it.
- ⚠️ **Terminate every command with `;`.** A bare `MOVE Rn (x y)` leaves the MOVE
  tool **active**, and the next `fusion_mcp_execute` is rejected with `Cannot
  perform 'script' while a command dialog is open`. There is no bridge API to
  send Esc — the user must press Esc in Fusion. `run_eagle*` always appends `;`.
- **Paths are Fusion-host (Windows) paths**; a `.scr` stops at its first failing
  command; changes are **unsaved** until the user saves in Fusion.

Commands that work this way include `MOVE`, `ROTATE`, `VALUE`, `CHANGE PACKAGE`,
`ATTRIBUTE`, `EXPORT`, and `script <file>`.

---
*The handshake and write-path recipe were first reverse-engineered in the
sibling Hendley project (`docs/fusion-notes.md`); this is the cleaned-up,
generalized version that `bridge.py` implements.*
