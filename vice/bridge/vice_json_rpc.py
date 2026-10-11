"""Local JSON-RPC 2.0 bridge for the VICE x64sc binary monitor.

The wire format matches the Fuse fork: one JSON object per TCP line, bound to
127.0.0.1. VICE itself remains authoritative for CPU, devices and pixels.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import socket
import socketserver
from pathlib import Path
from typing import Any

from vice_binary_client import ViceMonitor


MAX_REQUEST = 1_048_576
MAX_MEMORY = 65_536
METHODS = (
    "agent.capabilities", "emulator.info", "session.status", "state.get",
    "state.get_registers", "memory.read", "video.snapshot", "video.palette",
    "snapshot.save", "snapshot.load", "execution.pause", "execution.continue",
    "execution.step", "execution.run_until", "breakpoints.create",
    "breakpoints.list", "breakpoints.delete", "input.keyboard", "input.joystick",
)
REGISTERS = {0: "a", 1: "x", 2: "y", 3: "pc", 4: "sp", 5: "p",
             55: "raster_line", 56: "raster_cycle"}
KEYS = {"f3": (0, 5), "f5": (0, 6), "f7": (0, 3),
        "space": (7, 4), "return": (0, 1)}


class InvalidParams(ValueError):
    pass


def fields(params: dict[str, Any], allowed: set[str]) -> None:
    unknown = params.keys() - allowed
    if unknown:
        raise InvalidParams(f"unknown parameter(s): {', '.join(sorted(unknown))}")


def integer(value: Any, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise InvalidParams(f"{name} must be an integer in {low}..{high}")
    return value


class Service:
    def __init__(self, monitor_port: int, rpc_port: int):
        self.monitor = ViceMonitor(port=monitor_port)
        self.rpc_port = rpc_port
        self.paused = True
        self.revision = 0
        self.last_clock = self.monitor.clock()
        self.breakpoints: dict[int, int] = {}

    def _pause(self) -> int:
        clock = self.monitor.clock()
        self.paused = True
        self.last_clock = clock
        return clock

    def _require_paused(self) -> None:
        if not self.paused:
            raise InvalidParams("call execution.pause before guest inspection or input")

    def state(self) -> dict[str, Any]:
        self._require_paused()
        clock = self._pause()
        raw = self.monitor.registers()
        general = {REGISTERS[row["id"]]: row["value"] for row in raw
                   if row["id"] in REGISTERS}
        return {"emulator": "VICE x64sc", "cpu": "6510", "clock": clock,
                "paused": True, "state_revision": self.revision,
                "general": general, "registers_raw": raw}

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method in ("agent.capabilities", "emulator.info"):
            fields(params, set())
            return {"emulator": "VICE x64sc", "version": "3.10.0",
                    "cpu": "6510", "model": "C64 PAL", "session_id": "vice",
                    "transport": "JSON-RPC 2.0 TCP JSON-lines", "bind": "127.0.0.1",
                    "rpc_port": self.rpc_port, "backend": "VICE binary monitor",
                    "cpu_cycle_frequency": 985248, "time_base": "native CPU clocks",
                    "methods": list(METHODS),
                    "limits": {"address_bits": 16, "max_line_bytes": MAX_REQUEST,
                               "max_memory_bytes": MAX_MEMORY},
                    "scheduling": "requests enter VICE's paused binary monitor; "
                                  "execution.step uses native instruction count"}
        if method == "session.status":
            fields(params, {"session_id"})
            if params.get("session_id", "vice") != "vice":
                raise InvalidParams("unknown session_id")
            if self.paused:
                self._pause()
            return {"session_id": "vice", "state": "paused" if self.paused else "running",
                    "paused": self.paused, "clock": self.last_clock,
                    "clock_stale": not self.paused, "state_revision": self.revision}
        if method in ("state.get", "state.get_registers"):
            fields(params, set())
            return self.state()
        if method == "execution.pause":
            fields(params, set())
            self._pause()
            return self.state()
        if method == "execution.continue":
            fields(params, set())
            self._require_paused()
            clock = self._pause()
            self.monitor.resume()
            self.paused = False
            self.revision += 1
            return {"state": "running", "paused": False, "clock_at_resume": clock,
                    "state_revision": self.revision}
        if method == "execution.step":
            fields(params, {"count", "mode"})
            self._require_paused()
            if params.get("mode", "into") != "into":
                raise InvalidParams("only step into is supported")
            count = integer(params.get("count", 1), 1, 65_535, "count")
            self.monitor.step(count)
            self.revision += 1
            return self.state()
        if method == "execution.run_until":
            fields(params, {"address", "timeout_ms"})
            self._require_paused()
            address = integer(params.get("address"), 0, 65_535, "address")
            timeout = integer(params.get("timeout_ms", 20_000), 1, 30_000, "timeout_ms")
            # VICE resumes automatically when a temporary checkpoint is added.
            checkpoint_id = self.monitor.checkpoint(address, temporary=True)
            self.paused = False
            previous_timeout = self.monitor.sock.gettimeout()
            self.monitor.sock.settimeout(timeout / 1000)
            try:
                self.monitor.wait_stopped()
            except socket.timeout as exc:
                self.monitor.sock.settimeout(previous_timeout)
                recent_events = self.monitor.events[-6:]
                clock = self._pause()
                try:
                    info = self.monitor.checkpoint_info(checkpoint_id)
                except RuntimeError:
                    info = None
                pc = self.state()["general"].get("pc")
                if info:
                    self.monitor.checkpoint_delete(checkpoint_id)
                raise TimeoutError(
                    f"checkpoint {checkpoint_id} not reached in {timeout} ms; "
                    f"pc={pc:04x}, clock={clock}, checkpoint={info}, "
                    f"events={recent_events}") from exc
            finally:
                self.monitor.sock.settimeout(previous_timeout)
            self.paused = True
            self.revision += 1
            result = self.state()
            if result["general"].get("pc") != address:
                raise RuntimeError("VICE stopped at a different checkpoint")
            result["checkpoint_id"] = checkpoint_id
            return result
        if method == "breakpoints.create":
            fields(params, {"address", "kind", "once"})
            self._require_paused()
            address = integer(params.get("address"), 0, 65_535, "address")
            if params.get("kind", "execution") != "execution":
                raise InvalidParams("only execution breakpoints are supported")
            once = params.get("once", False)
            if type(once) is not bool:
                raise InvalidParams("once must be boolean")
            if once:
                raise InvalidParams("use execution.run_until for a one-shot breakpoint")
            checkpoint_id = self.monitor.checkpoint(address, temporary=False)
            self.breakpoints[checkpoint_id] = address
            self.revision += 1
            return {"breakpoint_id": checkpoint_id, "address": address,
                    "once": once, "state_revision": self.revision}
        if method == "breakpoints.list":
            fields(params, set())
            self._require_paused()
            return {"breakpoints": [{"breakpoint_id": key, "address": value}
                                    for key, value in sorted(self.breakpoints.items())]}
        if method == "breakpoints.delete":
            fields(params, {"breakpoint_id"})
            self._require_paused()
            checkpoint_id = integer(params.get("breakpoint_id"), 1, 2**32 - 1,
                                    "breakpoint_id")
            if checkpoint_id not in self.breakpoints:
                raise InvalidParams("unknown persistent breakpoint")
            self.monitor.checkpoint_delete(checkpoint_id)
            del self.breakpoints[checkpoint_id]
            self.revision += 1
            return {"deleted": checkpoint_id, "state_revision": self.revision}
        if method == "memory.read":
            fields(params, {"address", "length", "bank"})
            self._require_paused()
            address = integer(params.get("address"), 0, 65_535, "address")
            length = integer(params.get("length", 1), 1, 65_536 - address, "length")
            bank = integer(params.get("bank", 0), 0, 65_535, "bank")
            data = bytearray()
            while len(data) < length:
                portion = min(32_768, length - len(data))
                data.extend(self.monitor.memory(address + len(data), portion, bank))
            return {"address": address, "length": length, "bank": bank,
                    "clock": self._pause(), "format": "raw",
                    "data_base64": base64.b64encode(data).decode("ascii"),
                    "sha256": hashlib.sha256(data).hexdigest()}
        if method == "video.palette":
            fields(params, set())
            self._require_paused()
            return {"clock": self._pause(), "palette_rgb": self.monitor.palette()}
        if method == "video.snapshot":
            fields(params, {"include_border"})
            self._require_paused()
            include_border = params.get("include_border", False)
            if type(include_border) is not bool:
                raise InvalidParams("include_border must be boolean")
            layout, full = self.monitor.display()
            if include_border:
                width, height, pixels = layout["width"], layout["height"], full
            else:
                x, y = layout["inner_x"], layout["inner_y"]
                width, height = layout["inner_width"], layout["inner_height"]
                pitch = layout["width"]
                pixels = b"".join(full[(y + row) * pitch + x:
                                       (y + row) * pitch + x + width]
                                  for row in range(height))
            return {"clock": self._pause(), "width": width, "height": height,
                    "format": "indexed8", "adapter": "VIC-II final raster",
                    "layout": layout, "palette_rgb": self.monitor.palette(),
                    "data_base64": base64.b64encode(pixels).decode("ascii"),
                    "sha256": hashlib.sha256(pixels).hexdigest()}
        if method in ("snapshot.save", "snapshot.load"):
            fields(params, {"path"})
            self._require_paused()
            raw_path = params.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                raise InvalidParams("path must be a nonempty string")
            path = Path(raw_path).resolve()
            if method == "snapshot.save":
                path.parent.mkdir(parents=True, exist_ok=True)
                before = self._pause()
                self.monitor.snapshot(path)
                after = self._pause()
                if before != after:
                    raise RuntimeError("snapshot save advanced the guest clock")
                return {"path": str(path), "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "clock": after}
            if not path.is_file():
                raise InvalidParams("snapshot path does not exist")
            self.monitor.snapshot_load(path)
            self.revision += 1
            return self.state()
        if method == "input.keyboard":
            fields(params, {"events"})
            self._require_paused()
            events = params.get("events")
            if not isinstance(events, list) or not 1 <= len(events) <= 32:
                raise InvalidParams("events must contain 1..32 key changes")
            changes = []
            for event in events:
                if not isinstance(event, dict):
                    raise InvalidParams("each event must be an object")
                fields(event, {"key", "pressed"})
                key, pressed = event.get("key"), event.get("pressed")
                if not isinstance(key, str) or key.lower() not in KEYS or type(pressed) is not bool:
                    raise InvalidParams("unsupported key or invalid pressed value")
                changes.append((*KEYS[key.lower()], pressed))
            for row, column, pressed in changes:
                self.monitor.key_matrix(row, column, pressed)
            self.revision += 1
            return {"accepted": len(changes), "clock": self._pause(),
                    "state_revision": self.revision}
        if method == "input.joystick":
            fields(params, {"port", "mask"})
            self._require_paused()
            port = integer(params.get("port", 2), 1, 2, "port")
            mask = integer(params.get("mask"), 0, 31, "mask")
            self.monitor.joystick(port, mask)
            self.revision += 1
            return {"port": port, "mask": mask, "clock": self._pause(),
                    "state_revision": self.revision}
        raise LookupError(method)


class Handler(socketserver.StreamRequestHandler):
    service: Service

    def handle(self) -> None:
        while True:
            line = self.rfile.readline(MAX_REQUEST + 1)
            if not line:
                return
            if len(line) > MAX_REQUEST:
                return
            request_id: Any = None
            try:
                request = json.loads(line)
                if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                    raise InvalidParams("expected JSON-RPC 2.0 object")
                request_id = request.get("id")
                method = request.get("method")
                params = request.get("params", {})
                if not isinstance(method, str) or not isinstance(params, dict):
                    raise InvalidParams("method and params must be string and object")
                result = self.service.dispatch(method, params)
                reply = {"jsonrpc": "2.0", "id": request_id, "result": result}
            except LookupError as exc:
                reply = {"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": -32601, "message": f"method not found: {exc}"}}
            except (InvalidParams, ValueError, KeyError) as exc:
                reply = {"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": -32602, "message": str(exc)}}
            except Exception as exc:
                reply = {"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": -32000, "message": str(exc)}}
            self.wfile.write(json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--monitor-port", type=int, default=6502)
    parser.add_argument("--rpc-port", type=int, default=6503)
    args = parser.parse_args()
    service = Service(args.monitor_port, args.rpc_port)
    Handler.service = service
    class LocalServer(socketserver.TCPServer):
        allow_reuse_address = True
    with LocalServer(("127.0.0.1", args.rpc_port), Handler) as server:
        print(f"VICE JSON-RPC listening on 127.0.0.1:{args.rpc_port}", flush=True)
        try:
            server.serve_forever()
        finally:
            service.monitor.close()


if __name__ == "__main__":
    main()
