# VICE JSON-RPC bridge

This feature branch exposes the same localhost, newline-delimited JSON-RPC 2.0
wire format used by the Fuse fork. The Python bridge connects to VICE 3.10's
binary monitor, so x64sc owns every guest operation. Python 3.8+ and the
standard library are enough; no game image is distributed.

Start a headless or visible `x64sc` build with `-binarymonitor` and a loopback
monitor address, then start the bridge:

```text
x64sc -binarymonitor -binarymonitoraddress ip4://127.0.0.1:6502 ...
python bridge/vice_json_rpc.py --monitor-port 6502 --rpc-port 6503
```

The bridge starts with VICE paused. Connect to `127.0.0.1:6503` and send one
JSON object per line:

```json
{"jsonrpc":"2.0","id":1,"method":"agent.capabilities","params":{}}
```

`agent.capabilities` reports the implemented methods. The initial set covers
state, real CPU clock, memory, indexed final VIC-II raster, palette, full VSF
snapshot save/load, physical key matrix and joystick contacts, pause, continue,
instruction stepping, execution breakpoints and `execution.run_until`.
`video.snapshot` defaults to the 320×200 inner C64
display; `include_border:true` returns the full PAL display buffer.

The binary monitor pauses x64sc when a guest request arrives. `session.status`
does not interrupt a running guest; its clock is marked stale in that state.
Use `execution.pause` before input or inspection and `execution.continue` to
resume. `execution.run_until` takes a 16-bit CPU address and an optional
`timeout_ms`, creates a one-shot VICE execution checkpoint, waits until it
stops, and returns the paused register state. VICE resumes automatically when
the temporary checkpoint is created. Persistent execution checkpoints are
available through `breakpoints.create/list/delete`; VICE may resume on their
creation too. This is an instruction-address boundary, so callers must locate
the game's update loop and qualify the displayed raster phase. Paused
instruction steps retain the real guest CPU/device clocks. The wire endpoint
is a companion process, not an in-process VICE server.

`bridge/test_json_rpc.py` exercises the wire protocol,
64 KB memory read, complete 320×200 pixel capture, input commands, snapshot
roundtrip and a 20,000-instruction replay on a bare C64 boot. With
`--load-snapshot PATH --run-until-address 0x1BDD --capture-boundary` it loads a
user-supplied VSF after the initial pause and captures the exact breakpoint
state, RAM, VIC registers and indexed pixels before replay. Original game
images and ROMs are supplied by the local VICE installation, not this bridge.
