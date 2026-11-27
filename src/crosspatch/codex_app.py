"""The local Codex daemon's app-server transport and wake RPCs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from crosspatch import __version__

TIMEOUT = 5.0
MAX_MESSAGE = 8 * 1024 * 1024
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class Unavailable(Exception):
    """A request definitely did not submit a message."""


class Unconfirmed(Exception):
    """A submitted request might have reached Codex before the connection failed."""


class RPC:
    def __init__(self, send: Callable[[bytes], None], receive: Callable[[float], bytes]):
        self.send = send
        self.receive = receive
        self.sequence = 0

    def notify(self, method: str) -> None:
        self.send(json.dumps({"method": method}).encode())

    def call(self, method: str, params: dict, *, submits: bool = False) -> dict:
        self.sequence += 1
        request = {"id": self.sequence, "method": method, "params": params}
        deadline = time.monotonic() + TIMEOUT
        try:
            self.send(json.dumps(request).encode())
            while (remaining := deadline - time.monotonic()) > 0:
                response = json.loads(self.receive(remaining))
                if isinstance(response, dict) and response.get("id") == self.sequence:
                    return response
            raise TimeoutError
        except (OSError, ValueError) as error:
            if submits:
                raise Unconfirmed(f"{method} did not acknowledge the notice") from error
            raise Unavailable(f"{method} did not respond") from error


class WebSocket:
    def __init__(self, connection: socket.socket):
        self.connection = connection
        self.reader = connection.makefile("rb")

    def handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        expected = base64.b64encode(hashlib.sha1(f"{key}{WS_GUID}".encode(), usedforsecurity=False).digest())
        self.connection.sendall(
            ("GET /rpc HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
             f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode()
        )
        status = self.reader.readline(8192).split()
        headers = {}
        for _ in range(40):
            line = self.reader.readline(8192)
            if line in (b"\r\n", b""):
                break
            name, _, value = line.partition(b":")
            headers[name.lower()] = value.strip()
        if status[1:2] != [b"101"] or headers.get(b"sec-websocket-accept") != expected:
            raise Unavailable("the Codex daemon refused its WebSocket upgrade")

    def send(self, payload: bytes, opcode: int = 1) -> None:
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0xFE, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0xFF, length)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.connection.sendall(header + mask + masked)

    def receive(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        chunks = bytearray()
        while (remaining := deadline - time.monotonic()) > 0:
            self.connection.settimeout(remaining)
            first, length = self._read(2)
            opcode = first & 0x0F
            if first & 0x70 or length & 0x80:
                raise OSError("unexpected server WebSocket frame")
            if length == 126:
                length = int.from_bytes(self._read(2), "big")
            elif length == 127:
                length = int.from_bytes(self._read(8), "big")
            if len(chunks) + length > MAX_MESSAGE:
                raise OSError("app-server reply is too large")
            payload = self._read(length)
            if opcode == 8:
                raise OSError("app-server closed the WebSocket")
            if opcode == 9:
                self.send(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1, 2):
                raise OSError("unexpected app-server opcode")
            chunks.extend(payload)
            if first & 0x80:
                return bytes(chunks)
        raise TimeoutError

    def _read(self, length: int) -> bytes:
        data = self.reader.read(length)
        if len(data) != length:
            raise OSError("app-server closed mid-frame")
        return data


def _result(response: dict, method: str) -> dict[str, Any]:
    error = response.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("type") or "request rejected")
        if "already has an active writer" in message:
            raise Unavailable("another Codex app server owns the thread, such as the Desktop app")
        raise Unavailable(f"{method}: {message}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise Unavailable(f"{method} returned an unexpected response")
    return result


def deliver_rpc(rpc: RPC, thread_id: str, text: str, mode: str) -> str:
    _result(rpc.call("initialize", {"clientInfo": {"name": "crosspatch", "title": "Crosspatch", "version": __version__}}), "initialize")
    rpc.notify("initialized")
    thread = {"threadId": thread_id}
    read = rpc.call("thread/read", {**thread, "includeTurns": False})
    status = _status(read)
    if status == "notLoaded":
        if mode == "steer":
            raise Unavailable("the daemon has no loaded turn to steer")
        _result(rpc.call("thread/resume", {**thread, "excludeTurns": True}), "thread/resume")
        status = _status(rpc.call("thread/read", {**thread, "includeTurns": False}))
    payload = {**thread, "input": [{"type": "text", "text": text, "text_elements": []}], "clientUserMessageId": str(uuid4())}
    if status == "active":
        if mode == "start":
            raise Unavailable("the daemon thread already has an active turn")
        latest = _result(rpc.call("thread/turns/list", {**thread, "limit": 1, "itemsView": "notLoaded"}), "thread/turns/list").get("data")
        if not latest or latest[0].get("status") != "inProgress" or not latest[0].get("id"):
            raise Unavailable("the daemon did not report an active turn to steer")
        method = "turn/steer"
        payload["expectedTurnId"] = latest[0]["id"]
        detail = "steered the active turn"
    else:
        if mode == "steer" or status != "idle":
            raise Unavailable(f"the daemon thread has status {status}")
        method = "turn/start"
        detail = "started a turn"
    response = rpc.call(method, payload, submits=True)
    if isinstance(response.get("error"), dict):
        _result(response, method)
    result = response.get("result")
    if not isinstance(result, dict):
        raise Unconfirmed(f"{method} returned no acceptance or rejection")
    if method == "turn/steer":
        accepted = result.get("turnId") == payload["expectedTurnId"]
    else:
        started = result.get("turn")
        accepted = isinstance(started, dict) and bool(started.get("id"))
    if not accepted:
        raise Unconfirmed(f"{method} did not confirm the target turn")
    return detail


def _status(response: dict) -> str:
    error = response.get("error")
    if isinstance(error, dict) and error.get("type") == "notLoaded":
        return "notLoaded"
    return _result(response, "thread/read").get("thread", {}).get("status", {}).get("type", "unknown")


def deliver(path: Path, thread_id: str, text: str, mode: str) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(TIMEOUT)
        try:
            connection.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError) as error:
            raise Unavailable("no local Codex daemon is listening") from error
        except OSError as error:
            raise Unavailable(f"could not connect to the local Codex daemon: {error}") from error
        transport = WebSocket(connection)
        with transport.reader:
            try:
                transport.handshake()
                return deliver_rpc(RPC(transport.send, transport.receive), thread_id, text, mode)
            except OSError as error:
                raise Unavailable(f"local Codex daemon connection failed: {error}") from error
