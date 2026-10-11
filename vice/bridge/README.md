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
snapshot save/load, physical key matrix and joystick contacts, pause, continue
and instruction stepping. `video.snapshot` defaults to the 320×200 inner C64
display; `include_border:true` returns the full PAL display buffer.

The binary monitor pauses x64sc when a guest request arrives. `session.status`
does not interrupt a running guest; its clock is marked stale in that state.
Use `execution.pause` before input or inspection and `execution.continue` to
resume. This bridge does not yet schedule requests for an exact gameplay update
or provide breakpoints and frame stepping. Its paused instruction steps retain
the real guest CPU/device clocks. The wire endpoint is a companion process,
not an in-process VICE server.

The project test `tests/check_vice_json_rpc.py` exercises the wire protocol,
64 KB memory read, complete 320×200 pixel capture, input commands, snapshot
roundtrip and a 20,000-instruction replay on a bare C64 boot. Original game
images and ROMs are supplied by the local VICE installation, not this bridge.
