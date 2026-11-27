import base64
import hashlib
import json
import os
import selectors
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from crosspatch import codex_app

THREAD = "0000aaaa-0000-0000-0000-000000000001"
NOTICE = "[crosspatch] New BRIEF 1. Check pending."
FAKE = """
import json
import sys

state, report = sys.argv[1:3]
for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "initialized":
        continue
    reply = {"id": request["id"], "result": {}}
    match request["method"]:
        case "thread/read":
            reply["result"] = {"thread": {"status": {"type": state}}}
        case "thread/resume":
            reply = {"id": request["id"], "error": {"code": -32600, "message": "thread already has an active writer"}}
        case "thread/turns/list":
            reply["result"] = {"data": [{"id": "current-turn", "status": "inProgress"}]}
        case "turn/start" | "turn/steer":
            with open(report, "w") as f:
                json.dump(request, f)
            if request["method"] == "turn/start":
                reply["result"] = {"turn": {"id": "new-turn"}}
            else:
                reply["result"] = {"turnId": request["params"]["expectedTurnId"]}
    print(json.dumps({"method": "thread/status/changed", "params": {}}), flush=True)
    print(json.dumps(reply), flush=True)
"""


class CodexAppTest(unittest.TestCase):
    def fake(self, state, mode):
        with TemporaryDirectory() as tmp:
            report = Path(tmp) / "submission.json"
            with subprocess.Popen([sys.executable, "-c", FAKE, state, str(report)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process, selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                pending = bytearray()

                def send(data):
                    process.stdin.write(data + b"\n")
                    process.stdin.flush()

                def receive(timeout):
                    while b"\n" not in pending:
                        if not selector.select(timeout):
                            raise TimeoutError
                        packet = os.read(process.stdout.fileno(), 4096)
                        if not packet:
                            raise OSError("EOF")
                        pending.extend(packet)
                    end = pending.index(b"\n")
                    result = bytes(pending[:end])
                    del pending[:end+1]
                    return result

                try:
                    detail = codex_app.deliver_rpc(codex_app.RPC(send, receive), THREAD, NOTICE, mode)
                    return detail, json.loads(report.read_text())
                finally:
                    process.terminate()

    def test_auto_steers_with_the_expected_turn_and_notice(self):
        detail, request = self.fake("active", "auto")
        self.assertEqual(detail, "steered the active turn")
        self.assertEqual(request["method"], "turn/steer")
        self.assertEqual(request["params"]["expectedTurnId"], "current-turn")
        self.assertEqual(request["params"]["input"][0]["text"], NOTICE)

    def test_auto_starts_an_idle_thread(self):
        detail, request = self.fake("idle", "auto")
        self.assertEqual(detail, "started a turn")
        self.assertEqual(request["method"], "turn/start")
        self.assertEqual(request["params"]["threadId"], THREAD)

    def test_desktop_owned_thread_has_a_distinct_error(self):
        with self.assertRaisesRegex(codex_app.Unavailable, "owns the thread"):
            self.fake("notLoaded", "auto")

    def test_explicit_modes_do_not_switch_operation(self):
        with self.assertRaisesRegex(codex_app.Unavailable, "already has an active turn"):
            self.fake("active", "start")
        with self.assertRaisesRegex(codex_app.Unavailable, "status idle"):
            self.fake("idle", "steer")

    def test_unacknowledged_steer_is_distinct_from_a_rejection(self):
        def receive(_):
            raise OSError("disconnected")

        rpc = codex_app.RPC(lambda _: None, receive)
        with self.assertRaisesRegex(codex_app.Unconfirmed, "did not acknowledge"):
            rpc.call("turn/steer", {}, submits=True)

    def test_unix_socket_upgrade_and_fragmented_response(self):
        client, server = socket.socketpair()
        with client, server:
            transport = codex_app.WebSocket(client)
            with transport.reader, server.makefile("rb") as request:
                original = client.sendall

                class TestConnection:
                    def sendall(self, data):
                        original(data)
                        self.upgrade()

                    def upgrade(self):
                        self.assert_request()
                        server.sendall(b"\x01\x02{}\x80\x01\n")

                    def assert_request(self):
                        assert request.readline() == b"GET /rpc HTTP/1.1\r\n"
                        headers = {}
                        for line in iter(request.readline, b"\r\n"):
                            name, _, value = line.partition(b":")
                            headers[name.lower()] = value.strip()
                        key = headers[b"sec-websocket-key"] + codex_app.WS_GUID.encode()
                        accept = base64.b64encode(hashlib.sha1(key, usedforsecurity=False).digest())
                        server.sendall(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")

                    def settimeout(self, value):
                        client.settimeout(value)

                transport.connection = TestConnection()
                transport.handshake()
                self.assertEqual(transport.receive(1), b"{}\n")
