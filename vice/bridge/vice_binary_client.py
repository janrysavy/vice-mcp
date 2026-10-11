"""VICE 3.10 binary-monitor client used by the JSON-RPC bridge."""

from __future__ import annotations

import socket
import struct
from pathlib import Path


class ViceMonitor:
    def __init__(self, host: str = "127.0.0.1", port: int = 6502):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(20)
        self.request_id = 0
        self.events: list[dict] = []

    def close(self) -> None:
        self.sock.close()

    def _recv_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            part = self.sock.recv(size - len(chunks))
            if not part:
                raise ConnectionError("VICE monitor closed the connection")
            chunks.extend(part)
        return bytes(chunks)

    def request(self, command: int, body: bytes = b"") -> bytes:
        self.request_id += 1
        request_id = self.request_id
        self.sock.sendall(b"\x02\x02" + struct.pack("<IIB", len(body), request_id, command) + body)
        while True:
            header = self._recv_exact(12)
            if header[:2] != b"\x02\x02":
                raise ValueError(f"invalid VICE response header: {header[:2].hex()}")
            length, kind, error, response_id = struct.unpack_from("<IBBI", header, 2)
            if length > 2_000_000:
                raise ValueError(f"oversized VICE response: {length}")
            payload = self._recv_exact(length)
            if response_id == 0xFFFFFFFF:
                self.events.append({"kind": kind, "body": payload.hex()})
                continue
            if response_id != request_id:
                raise ValueError(f"unexpected VICE response id {response_id}, wanted {request_id}")
            if error:
                raise RuntimeError(f"VICE command {command:#x} failed: {error:#x}")
            return payload

    def step(self, count: int) -> None:
        if not 1 <= count <= 65535:
            raise ValueError("instruction count outside VICE protocol range")
        self.request(0x71, struct.pack("<BH", 0, count))
        self.wait_stopped()

    def wait_stopped(self) -> None:
        while True:
            header = self._recv_exact(12)
            if header[:2] != b"\x02\x02":
                raise ValueError("invalid VICE event header")
            length, kind, error, response_id = struct.unpack_from("<IBBI", header, 2)
            if length > 2_000_000 or response_id != 0xFFFFFFFF or error:
                raise ValueError("unexpected VICE response while awaiting stop")
            payload = self._recv_exact(length)
            self.events.append({"kind": kind, "body": payload.hex()})
            if kind == 0x62:
                return

    def registers(self) -> list[dict]:
        body = self.request(0x31, b"\x00")
        count = struct.unpack_from("<H", body)[0]
        registers = []
        offset = 2
        for _ in range(count):
            item_size = body[offset]
            register_id = body[offset + 1]
            value = struct.unpack_from("<H", body, offset + 2)[0]
            registers.append({"id": register_id, "value": value})
            offset += item_size + 1
        return registers

    def memory(self, start: int, length: int, bank: int = 0) -> bytes:
        if not (0 <= start <= 65535 and 1 <= length <= 65536 - start):
            raise ValueError("memory request outside 64 KB")
        body = struct.pack("<BHHBH", 0, start, start + length - 1, 0, bank)
        reply = self.request(0x01, body)
        reported = struct.unpack_from("<H", reply)[0]
        if reported != length or len(reply) != length + 2:
            raise ValueError("VICE returned an unexpected memory length")
        return reply[2:]

    def memory_write(self, start: int, data: bytes, bank: int = 0) -> None:
        if not data or start < 0 or start + len(data) > 65536:
            raise ValueError("memory write outside 64 KB")
        body = struct.pack("<BHHBH", 0, start, start + len(data) - 1, 0, bank) + data
        self.request(0x02, body)

    def display(self) -> tuple[dict, bytes]:
        reply = self.request(0x84, b"\x00\x00")
        info_length = struct.unpack_from("<I", reply)[0]
        if info_length != 13:
            raise ValueError(f"unexpected VICE display header size: {info_length}")
        width, height, x, y, inner_width, inner_height = struct.unpack_from("<6H", reply, 4)
        depth = reply[16]
        pixel_length = struct.unpack_from("<I", reply, 17)[0]
        pixels = reply[21:]
        if depth != 8 or pixel_length != width * height or len(pixels) != pixel_length:
            raise ValueError(f"unexpected VICE display format: depth={depth}, "
                             f"size={width}x{height}, advertised={pixel_length}, "
                             f"received={len(pixels)}, body={len(reply)}")
        return {"width": width, "height": height, "inner_x": x, "inner_y": y,
                "inner_width": inner_width, "inner_height": inner_height,
                "depth": depth}, pixels

    def palette(self) -> list[list[int]]:
        reply = self.request(0x91, b"\x00")
        count = struct.unpack_from("<H", reply)[0]
        colors = []
        offset = 2
        for _ in range(count):
            size = reply[offset]
            if size != 3:
                raise ValueError("unexpected VICE palette entry")
            colors.append(list(reply[offset + 1:offset + 4]))
            offset += size + 1
        return colors

    def snapshot(self, path: Path) -> None:
        encoded = str(path.resolve()).encode("utf-8")
        if len(encoded) > 255:
            raise ValueError("snapshot path exceeds VICE protocol limit")
        self.request(0x41, bytes((1, 1, len(encoded))) + encoded)

    def snapshot_load(self, path: Path) -> None:
        encoded = str(path.resolve()).encode("utf-8")
        if len(encoded) > 255:
            raise ValueError("snapshot path exceeds VICE protocol limit")
        self.request(0x42, bytes((len(encoded),)) + encoded)

    def feed(self, text: str) -> None:
        encoded = text.encode("ascii")
        if len(encoded) > 255:
            raise ValueError("keyboard feed too long")
        self.request(0x72, bytes((len(encoded),)) + encoded)

    def resume(self) -> None:
        self.request(0xAA)

    def key_matrix(self, row: int, column: int, pressed: bool) -> None:
        self.request(0xA3, bytes((row, column, int(pressed))))

    def joystick(self, port: int, value: int) -> None:
        self.request(0xA4, bytes((port, value)))

    def clock(self) -> int:
        reply = self.request(0xA5)
        if len(reply) != 8:
            raise ValueError("unexpected VICE clock response")
        return struct.unpack("<Q", reply)[0]
