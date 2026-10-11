"""Exercise the fork's localhost JSON-RPC wire contract without game media."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def connect(port: int, process: subprocess.Popen) -> socket.socket:
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("service exited before opening its port")
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=1)
            sock.settimeout(20)
            return sock
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"port {port} did not open")


class Rpc:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.file = sock.makefile("rwb", buffering=0)
        self.next_id = 0

    def call(self, method: str, **params):
        self.next_id += 1
        request = {"jsonrpc": "2.0", "id": self.next_id,
                   "method": method, "params": params}
        self.file.write(json.dumps(request).encode() + b"\n")
        response = json.loads(self.file.readline())
        if response["id"] != self.next_id:
            raise AssertionError("JSON-RPC id mismatch")
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["result"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vice", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--monitor-port", type=int, default=16502)
    parser.add_argument("--rpc-port", type=int, default=16503)
    parser.add_argument("--load-snapshot", type=Path)
    parser.add_argument("--run-until-address", type=lambda value: int(value, 0))
    parser.add_argument("--capture-boundary", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = r"C:\msys64\ucrt64\bin" + os.pathsep + env.get("PATH", "")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with (args.out / "vice.log").open("wb") as vice_log, \
            (args.out / "bridge.log").open("wb") as bridge_log:
        vice_command = [
            str(args.vice.resolve()), "-directory", str(args.data.resolve()),
            "-sounddev", "dummy", "-warp", "-binarymonitor",
            "-binarymonitoraddress", f"ip4://127.0.0.1:{args.monitor_port}",
        ]
        vice = subprocess.Popen(vice_command, env=env, stdout=vice_log, stderr=subprocess.STDOUT,
            creationflags=creationflags)
        bridge = None
        sock = None
        try:
            monitor_probe = connect(args.monitor_port, vice)
            monitor_probe.close()
            bridge = subprocess.Popen([
                sys.executable, "-u", str(args.bridge.resolve()),
                "--monitor-port", str(args.monitor_port),
                "--rpc-port", str(args.rpc_port),
            ], env=env, stdout=bridge_log, stderr=subprocess.STDOUT,
                creationflags=creationflags)
            sock = connect(args.rpc_port, bridge)
            rpc = Rpc(sock)
            capabilities = rpc.call("agent.capabilities")
            assert capabilities["transport"] == "JSON-RPC 2.0 TCP JSON-lines"
            assert rpc.call("session.status")["paused"]
            if args.load_snapshot:
                rpc.call("snapshot.load", path=str(args.load_snapshot.resolve()))
            initial = rpc.call("state.get")
            if args.run_until_address is not None:
                print(f"initial PC={initial['general'].get('pc'):04X} clock={initial['clock']}", flush=True)
                first_counter = rpc.call("memory.read", address=0x48, length=1)
                print(f"initial $48={base64.b64decode(first_counter['data_base64']).hex()}", flush=True)
            video = rpc.call("video.snapshot")
            if args.run_until_address is not None:
                print(f"video clock={video['clock']}", flush=True)
            pixels = base64.b64decode(video["data_base64"])
            assert (video["width"], video["height"], len(pixels)) == (320, 200, 64000)
            assert hashlib.sha256(pixels).hexdigest() == video["sha256"]
            memory = rpc.call("memory.read", address=0, length=256)
            if args.run_until_address is not None:
                print(f"memory clock={memory['clock']}", flush=True)
            assert len(base64.b64decode(memory["data_base64"])) == 256
            if not args.load_snapshot:
                rpc.call("input.keyboard", events=[{"key": "f7", "pressed": True}])
                rpc.call("input.keyboard", events=[{"key": "f7", "pressed": False}])
                rpc.call("input.joystick", port=2, mask=8)
                rpc.call("input.joystick", port=2, mask=0)
            if args.run_until_address is not None:
                try:
                    boundary = rpc.call("execution.run_until", address=args.run_until_address,
                                        timeout_ms=3000)
                except RuntimeError:
                    stalled = rpc.call("execution.pause")
                    counter = rpc.call("memory.read", address=0x48, length=1)
                    failed_video = rpc.call("video.snapshot")
                    (args.out / "stalled-indexed.bin").write_bytes(
                        base64.b64decode(failed_video["data_base64"]))
                    (args.out / "stalled-video.json").write_text(
                        json.dumps({key: value for key, value in failed_video.items()
                                    if key != "data_base64"}, indent=2) + "\n")
                    failed_ram = rpc.call("memory.read", address=0, length=65536)
                    (args.out / "stalled-ram.bin").write_bytes(
                        base64.b64decode(failed_ram["data_base64"]))
                    print(f"stalled PC={stalled['general'].get('pc'):04X} "
                          f"clock={stalled['clock']} "
                          f"$48={base64.b64decode(counter['data_base64']).hex()}", flush=True)
                    raise
                assert boundary["general"]["pc"] == args.run_until_address
                if args.capture_boundary:
                    boundary_video = rpc.call("video.snapshot")
                    boundary_ram = rpc.call("memory.read", address=0, length=65536)
                    boundary_vic = rpc.call("memory.read", address=0xD000, length=0x40)
                    (args.out / "boundary-indexed.bin").write_bytes(
                        base64.b64decode(boundary_video["data_base64"]))
                    (args.out / "boundary-ram.bin").write_bytes(
                        base64.b64decode(boundary_ram["data_base64"]))
                    (args.out / "boundary-vic.bin").write_bytes(
                        base64.b64decode(boundary_vic["data_base64"]))
                    (args.out / "boundary.json").write_text(json.dumps({
                        "state": boundary,
                        "video": {key: value for key, value in boundary_video.items()
                                  if key != "data_base64"},
                        "ram_sha256": boundary_ram["sha256"],
                        "vic_io_sha256": boundary_vic["sha256"],
                    }, indent=2) + "\n")
            snapshot = rpc.call("snapshot.save", path=str(args.out / "rpc-test.vsf"))
            if args.run_until_address is None:
                assert snapshot["clock"] == initial["clock"]
            first = rpc.call("execution.step", count=20_000)
            first_video = rpc.call("video.snapshot")
            first_memory = rpc.call("memory.read", address=0, length=65536)
            restored = rpc.call("snapshot.load", path=snapshot["path"])
            assert restored["clock"] == snapshot["clock"]
            second = rpc.call("execution.step", count=20_000)
            second_video = rpc.call("video.snapshot")
            second_memory = rpc.call("memory.read", address=0, length=65536)
            result = {
                "capability_methods": len(capabilities["methods"]),
                "initial_clock": initial["clock"],
                "boundary_clock": snapshot["clock"],
                "replay_clock": first["clock"],
                "clock_equal": first["clock"] == second["clock"],
                "registers_equal": first["general"] == second["general"],
                "pixel_sha256": first_video["sha256"],
                "pixels_equal": first_video["sha256"] == second_video["sha256"],
                "ram_equal": first_memory["sha256"] == second_memory["sha256"],
                "snapshot_sha256": snapshot["sha256"],
            }
            (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
            if not all(value for key, value in result.items() if key.endswith("_equal")):
                raise AssertionError("JSON-RPC replay diverged")
        finally:
            if sock:
                sock.close()
            if bridge:
                bridge.terminate()
                bridge.wait(timeout=5)
            vice.terminate()
            vice.wait(timeout=5)


if __name__ == "__main__":
    main()
