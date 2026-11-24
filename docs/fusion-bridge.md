# The Fusion Electronics HTTP bridge

Steinmetz talks to a live Autodesk Fusion Electronics design over **plain HTTP
(JSON-RPC)** — no MCP client library, no plugin. Fusion publishes a local HTTP
endpoint; you `POST` JSON-RPC to it and call two tools:

- `fusion_mcp_electronics_read` — read the design's object model.
- `fusion_mcp_execute` — run Python inside Fusion (the write path).

`src/bridge.py` (`FusionBridge`) implements everything below; this doc is the
reference for *why* it does what it does. Every rule here was learned the hard
way — skip one and you get a confusing error.

## One-time setup

Run Steinmetz natively on the machine running Fusion (Windows or macOS).

1. Fusion running, an **Electronics document open** (schematic or board).
2. **Preferences ▸ General ▸ API ▸ "Fusion MCP Server"** enabled. (This Autodesk
   toggle is the only thing here called "MCP" — it just publishes the HTTP
   endpoint on `127.0.0.1:27182`.)

That's all: `bridge.default_host` finds Fusion on loopback. If it can't, it
says so (Fusion not running, no Electronics document, or the preference off).
Health check: `curl http://127.0.0.1:27182/mcp` (`curl.exe` in Windows
PowerShell) should return `{"error":"Not Found"}`. If Fusion silently moved to another port (it does when
27182 is taken), set `STEINMETZ_FUSION_PORT`.

## Running under WSL2 (legacy)

Steinmetz was developed under WSL2; the last WSL-only version is the
`wsl-final` git tag. WSL still works as a fallback, with Fusion on the Windows
host. Fusion-side scratch files go to `C:\tmp`, which WSL reads at
`/mnt/c/tmp` (`bridge.local_path` does the mapping). The extra network step
depends on the WSL networking mode (`wslinfo --networking-mode`):

- **mirrored** — nothing to do. WSL shares the Windows loopback, so
  `127.0.0.1:27182` just works, and no portproxy rule should exist for the
  port (a leftover `0.0.0.0` rule steals the bind from Fusion — see the
  warning below).
- **NAT** — you can't reach the Windows loopback, so forward Fusion's port
  to the **WSL gateway IP** (the Windows host as seen from WSL):

  ```powershell
  # GWIP = `ip route | grep default | awk '{print $3}'` from WSL, e.g. 172.17.64.1
  netsh interface portproxy add v4tov4 listenaddress=GWIP listenport=27182 ^
        connectaddress=127.0.0.1 connectport=27182
  ```

  ⚠️ **Bind the gateway IP, never `0.0.0.0`.** A `0.0.0.0:27182` listener
  covers loopback too, so it steals the bind from Fusion's own server (which
  silently moves to a random port) and forwards to itself — connections hang
  or "close unexpectedly." This bit for real on 2026-08-25 after a switch to
  mirrored mode left a stale `0.0.0.0` rule behind. Health check (on
  Windows): `curl http://127.0.0.1:27182/mcp` should return
  `{"error":"Not Found"}` when healthy.

### When the connection misbehaves under WSL

Don't re-derive the diagnosis — run
`~/claude-code-fusion-mcp-check/check.sh`
(clone: `git@github.com:holla2040/claude-code-fusion-mcp-check.git`). It checks
the link layer by layer (WSL→host route, Windows listener on 27182, portproxy,
loop detection) and prints the exact fix for the broken layer. The failure modes
are counterintuitive — e.g. the add-in silently rebinds to a random port when
27182 is taken — and are already encoded there. Follow what it prints; don't
apply ad-hoc netsh/proxy workarounds from this repo.

## The handshake (each rule bites if skipped)

1. **Hit a host that reaches Fusion.** Natively and in mirrored-mode WSL:
   `127.0.0.1`. NAT-mode WSL: the gateway IP (loopback is unreachable).
   `bridge.default_host` probes loopback first; only on Linux (WSL) does it fall
   back to the gateway — elsewhere it raises a clear "Fusion isn't answering"
   error.
2. **Send a loopback `Host` header** (`Host: 127.0.0.1:27182`). Fusion's MCP
   server validates the Host header (DNS-rebinding protection) and returns
   `403 {"error": "Invalid Host header"}` for anything but localhost — which is
   exactly what you send by default when hitting the WSL gateway IP. `bridge.py`
   sends it on every request.
3. **`initialize` once**, and capture **`MCP-Session-Id` from the *response
   header***. Resend it on every later request. Omit it → `Missing MCP-Session-Id
   header`.
4. **Send `notifications/initialized` before any `tools/call`.** Otherwise →
   `Session not initialized`.
5. Send `Accept: application/json, text/event-stream` on every request.
6. A tool's rows come back as a **JSON string** in `result.content[0].text` →
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
| `electronics.Package` | bbox `x1,y1,x2,y2`; useful for diagnostics, but often includes text/artwork and is not a placement keepout |
| `electronics.Layer` | layer names/numbers; `ComponentExcludeTop`/`ComponentExcludeBottom` are the placement keepout layers |
| `electronics.Contact` | package-local pad/contact coordinates |
| `electronics.Wire` / `Rectangle` / `Circle` | package or board geometry; **layer 20** carries the board outline, component-exclude layers carry placement keepouts |

Join `ContactRef.contact_object_id == Smd.contact_object_id` to get, per
connected pad: element + signal + global position. For placement legality, read
the `Layer` table to resolve `ComponentExcludeTop` / `ComponentExcludeBottom`,
then read package-local geometry (`Wire`, `Rectangle`, `Circle`) on those layers;
that is the footprint author's intended component placement keepout. Full schema
for any class: `resources/read resource://mcp.electronics_schema_<class>` (or
`FusionBridge.read_schema("element")`).

⚠️ **Reads cap at 100 rows by default** (`pagination.hasMore`); max `limit` is
1000. A naive single read silently truncates `Smd`/`ContactRef` → missing
connectivity. `FusionBridge.electronics_read` auto-paginates, so use it.

Coordinates are returned in the document's active unit, reported as
`coordinate_unit` in each response (e.g. `mm`); filter values for
`document_grid_unit` fields must use that same unit.

## The write path — driving the EAGLE command line

The Electronics **object** API was read-only in the production build tested
here (2026-06-30); Fusion Insider 2707.0.73 adds setters and a batch command
API — see "The command API (Insider builds)" below. Without those, the writable
channel is the **EAGLE command interpreter**, reached by running Python in Fusion via
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
- **Paths are Fusion-host paths** (`C:\tmp\…` on Windows, `/tmp/…` on macOS),
  written with forward slashes inside single quotes in EAGLE commands; a `.scr` stops at its first failing
  command; changes are **unsaved** until the user saves in Fusion.

Commands that work this way include `MOVE`, `ROTATE`, `VALUE`, `CHANGE PACKAGE`,
`ATTRIBUTE`, `EXPORT`, and `script <file>`.

### The command API (Insider builds)

Fusion Insider 2707.0.73 adds a preview API that replaces `Electron.run` for
batches: `Board` / `Schematic` / `Library` (all `adsk.electron.EcadDrawing`)
have `createCommandInput()` and `executeCommands(input)`.

```python
import adsk.core, adsk.electron
def run(_context):
    drawing = adsk.electron.EcadDrawing.cast(adsk.core.Application.get().activeProduct)
    ci = drawing.createCommandInput()
    ci.stopOnError = False          # True: later commands come back NotExecuted
    ci.rollbackOnError = False      # True: a failure discards the whole batch
    ci.add("GRID MM;")
    ci.add("MOVE R13 (11mm -7.8575mm);")
    r = drawing.executeCommands(ci)
    for x in r.commandResults:
        print(x.command, x.status, [m.text for m in x.messages])
```

- **Per-command results.** `status` is `EcadCommandStatuses`: 0 succeeded,
  1 succeeded with warning, 2 failed, 3 not executed. Messages carry the
  interpreter's text (`Unknown command: …`, the live-DRC refusal, net-merge
  notices). The batch result also has `success` and `rolledBack`.
- **Same syntax as `.scr`**; a trailing `;` is accepted. Coordinates take unit
  suffixes (`mm`, `mil`). Relative forms (`(R x y)`, `(>x y)`, `(+x y)`) are
  treated as **absolute** — compute absolute targets.
- **Still true:** board `MOVE`s fail with 'Switch to "Ignore Violator"' while
  live DRC is on; commands that want more input (`BUS`, `POLYGON`, some `NET`
  forms) can open a dialog and latch the execute channel exactly like
  `Electron.run` does. The refusal comes back as status 2 with the message
  `Action has violated DRC parameters. Switch to "Ignore Violator" in the
  navigation toolbar and try again.`
  - **Ignore Violators resets on every Fusion restart** — it is off again after
    each launch, so expect the refusal on the first placement of a session.
  - **Turn it on from a script** (through `execute`):

    ```python
    import adsk.core
    def run(_context):
        ui = adsk.core.Application.get().userInterface
        ui.commandDefinitions.itemById("IgnoreViolatorsCommand").execute()
    ```

    Executing it once switched the mode on (a following `MOVE` succeeded).
    **Whether executing it again toggles it off is not verified**, so don't run
    it blind more than once per session — confirm with a test `MOVE` instead.
    Related ids: `ViolationModeCommand`, `PushViolatorsCommand`,
    `WalkaroundViolatorsCommand`.
  - `place.py` prints every refused command and, on this message, a hint to
    turn Ignore Violators on.
- **Setters.** Most objects are now writable directly: `Element.x/y/angle/
  locked`, `Part.value`, `Instance.angle`, `Net.name`, `Bus.name` (base name
  must not end in a digit), `Junction.x`, `Grid.distance`, `Layer.color`
  (ARGB hex string, e.g. `'0xccff00ff'`) / `visible`, `Text.value`,
  `Circle.radius` (not on circles locked inside a placed package), `PolyPour.
  isolate/width/rank`, and library `Package`/`Symbol`/`Pin`/`Contact` fields.
- **In the bridge:** `has_command_api()` probes once; `execute_commands()` is
  the raw batch; `run_eagle_batch()` uses it when present (`stop_on_error=False`,
  so semantics match `Electron.run`) and returns the results under
  `"commands"`; `check=True` raises on any failed command.
  `STEINMETZ_LEGACY_WRITE=1` forces `Electron.run`.
- **Preview API** ("may change in future releases"). Production builds without
  it keep working through the `Electron.run` fallback.

### Schematic auto-routing — `NETROUTE` (Insider builds)

Recent Insider builds add `NETROUTE`, a schematic **auto-router by pin
reference** — no more computing wire coordinates to connect pins. Run it from
the schematic command line or through `Electron.run` like any other command.

```
NETROUTE N <num_pins> <ref1> <pin_name1> <ref2> <pin_name2> ... ;   (by pin name)
NETROUTE P <num_pins> <ref1> <pin_num1>  <ref2> <pin_num2>  ... ;   (by pin number)
```

Examples:

```
NETROUTE N 5 U1 GND U2 GND U3 GND U4 GND U5 GND;   # five GND pins, U1–U5
NETROUTE P 3 X1 2 X2 2 X3 2;                        # pin 2 of X1–X3
```

- Connecting a pin to a pin that is already on a named net joins the new pin to
  that net — so "attach D1's cathode to PANEL+" is just
  `NETROUTE N 2 D1 C Q9 D;` when Q9's drain is on PANEL+.
- **Put `SET CONFIRM YES;` in the *same* `Electron.run` string as the
  NETROUTE** — `Electron.run "SET CONFIRM YES;NETROUTE N 2 D1 C Q9 D;"`. A
  separate `SET CONFIRM YES;` call does **not** suppress the *"Merge net
  segment 'N$nn' into net '<NAME>'?"* modal (each NETROUTE then queues one,
  and a human must click Yes on each). The same-string form came from
  Autodesk support and was A/B-verified on 2026-08-26: on the identical
  merge, in the same session, the bare NETROUTE prompted and the same-string
  form ran silent with the merge applied. Both the separate-call gap and the
  hidden-dialog block below are under investigation at Autodesk.
- **The merge modal only fires on a true merge.** Routing a *virgin* pin to a
  pin on a named net attaches it directly — no anonymous segment exists, so
  no prompt, with or without `SET CONFIRM`. The prompt appears only when an
  existing segment (e.g. two previously-wired virgin pins on an auto-named
  `N$nn`) must merge into another net. Don't "verify" confirm behavior with
  2-pin virgin→named routes — they can't prompt, and the silence proves
  nothing.
- ⚠️ Even with the same-string workaround, NETROUTE (like MOVE/WIRE/ADD/
  DELETE) leaves the execute channel gated afterward — `Cannot perform
  'script' while a command dialog is open` — until someone clears it in
  Fusion. Two distinct mechanisms (both verified 2026-08-26):
  1. **A real error dialog rendered minimized** — look for a tiny **red X in
     the bottom-right corner of the canvas**; clicking it opens the actual
     error window, and clicking through it releases the gate.
  2. **An armed-tool state after an error-free command** — nothing visible at
     all; a click into the canvas plus **Esc** clears it.
  Batch writes into one script and verify every write out-of-band via
  `electronics_read` (`PinRef`/`Element`) — success messages alone prove
  nothing about what landed.
- ⚠️ **Quote element names in `ADD`.** An unquoted name token that looks like
  a rotation is parsed as one: `ADD R-1206@holla R99 R0 (220 40)` reads `R99`
  as *rotation 99°* → a "Non orthogonal orientations can only be used in a
  board or footprint!" error (minimized, see above), the name is discarded,
  and the part is auto-numbered (`R9`). Quoted works:
  `ADD R-1206@holla 'R99' R0 (220 40)`. Beware the silent coincidence: if the
  requested name happens to be the next free number, the part *appears*
  correctly named and the bug goes unnoticed.
- Pin *names* can be numeric strings (`1`, `2` on connectors/jumpers), so
  option `N` covers those too.

### Switching document tabs (schematic ↔ board)

Every `fusion_mcp_electronics_read` (and the reads behind `bridge.py`) queries
the **active document tab** only. An Electronics design keeps several tabs open
at once (`EcadDesignDocument`, `SchematicDocument`, `BoardDocument`, plus the 3D
`FusionDocument`), and reading schematic entities (Instance, Net, PinRef, Part)
with the board tab active — or board entities (Element, Signal, Smd) with the
schematic active — returns **empty items, not an error**. Activate the right
tab first with an `execute` script:

```python
import adsk.core
def run(_context):
    app = adsk.core.Application.get()
    for i in range(app.documents.count):
        d = app.documents.item(i)
        if 'SchematicDocument' in str(d.objectType):  # or 'BoardDocument'
            d.activate()
            break
```

`Electron.run` commands likewise land in whichever editor is active, so switch
tabs before firing schematic vs. board commands. Screenshots and ULP runs also
follow the active tab.

## Temporary ULP probes

Some Electronics state is only available through the embedded EAGLE runtime. The
same `Electron.run` path can run a temporary ULP that writes a result file on
the Fusion host, then the Fusion-side Python script reads that file back and
prints it into the bridge response.

The files live in the Fusion-side scratch dir, `bridge.fusion_scratch_dir()`:
`C:\tmp` on Windows (and under WSL), `/tmp` on macOS, overridable with
`STEINMETZ_FUSION_TMP` (no quote characters — the path is single-quoted in
EAGLE commands). Current users:

- `src/selection.py` writes a temporary selection ULP there, runs it,
  and reads `steinmetz_selection.txt`. The ULP uses `ingroup(E)`, so it
  sees GROUP-selected board elements, not Fusion's design-selection collection.
- `src/place.py` (when the API's `Board.grid` isn't available) writes
  `steinmetz_grid_main.ulp`, runs it, and reads
  `steinmetz_grid_main.txt`. The ULP prints `B.grid.distance` and
  `B.grid.unitdist`; the placer converts that live main-grid value to
  millimeters on every placement run.

---
*The handshake and write-path recipe were first reverse-engineered in the
sibling Hendley project (`docs/fusion-notes.md`); this is the cleaned-up,
generalized version that `bridge.py` implements.*
