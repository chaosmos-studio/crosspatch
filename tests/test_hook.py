from __future__ import annotations

import io
import json
import threading
import time
import unittest

from helpers import BridgeCase

from crosspatch import fsutil, hook, wake
from crosspatch.project import find_project

SESSION = "44444444-5555-6666-7777-888888888888"


class WakeHookTest(BridgeCase):
    def setUp(self) -> None:
        super().setUp()
        self.cli_ok("join", "lead", "--role", "lead", "--session", f"claude:{SESSION}", "--wake", "claude-hook")
        self.cli_ok("join", "maker", "--role", "builder", "--platform", "codex")
        self.cli_ok("link", "art", "--lead", "lead", "--builder", "maker")
        self.payload = json.dumps({"session_id": SESSION, "cwd": str(self.root), "hook_event_name": "Stop"})

    def run_hook(self, seconds: float) -> tuple[int, str]:
        err = io.StringIO()
        code = hook.run_wake_hook(io.StringIO(self.payload), err, max_seconds=seconds, poll=0.05)
        return code, err.getvalue()

    def test_hook_exits_2_with_a_notice_when_a_message_arrives(self) -> None:
        result: dict[str, object] = {}
        waiter = threading.Thread(target=lambda: result.update(zip(("code", "err"), self.run_hook(10), strict=True)))
        waiter.start()
        time.sleep(0.3)
        self.assertTrue(fsutil.lock_is_held(wake.claude_hook_lock(find_project(self.root), "lead")))
        self.cli_ok("send", "art", "--as", "maker", "--kind", "QUESTION", "-m", "Which font?")
        waiter.join(10)
        self.assertEqual(result["code"], 2)
        self.assertIn("New QUESTION 0001 from maker", str(result["err"]))
        self.assertEqual(self.run_hook(0.3), (0, ""), "a message is announced once")

    def test_one_waiting_hook_per_member(self) -> None:
        project = find_project(self.root)
        with fsutil.locked(wake.claude_hook_lock(project, "lead")):
            started = time.monotonic()
            self.assertEqual(self.run_hook(5), (0, ""))
            self.assertLess(time.monotonic() - started, 1)

    def test_hook_ignores_sessions_that_are_not_members(self) -> None:
        err = io.StringIO()
        payload = json.dumps({"session_id": "99999999-0000-0000-0000-000000000000", "cwd": str(self.root)})
        self.assertEqual(hook.run_wake_hook(io.StringIO(payload), err, max_seconds=5), 0)

    def test_adapter_reports_whether_the_hook_is_armed(self) -> None:
        project = find_project(self.root)
        status = self.cli_ok("status", "art")
        self.assertIn("not ready, no crosspatch hook", status)
        hook.install(project)
        self.assertIn("hook installed, not armed now", self.cli_ok("status", "art"))
        with fsutil.locked(wake.claude_hook_lock(project, "lead")):
            self.assertIn("hook armed", self.cli_ok("status", "art"))


class StopHookTest(BridgeCase):
    def test_stop_hook_blocks_once_while_a_reply_is_owed(self) -> None:
        self.cli_ok("join", "maker", "--role", "builder", "--session", f"claude:{SESSION}", "--wake", "claude-hook")
        self.cli_ok("join", "lead", "--role", "lead", "--platform", "codex")
        self.cli_ok("link", "art", "--lead", "lead", "--builder", "maker")
        self.cli_ok("send", "art", "--as", "lead", "--kind", "BRIEF", "-m", "Paint the door.")
        out = io.StringIO()
        hook.run_stop_hook(io.StringIO(json.dumps({"session_id": SESSION, "cwd": str(self.root)})), out)
        decision = json.loads(out.getvalue())
        self.assertEqual(decision["decision"], "block")
        self.assertIn("art 0001 (BRIEF from lead)", decision["reason"])
        again = io.StringIO()
        hook.run_stop_hook(io.StringIO(json.dumps({"session_id": SESSION, "cwd": str(self.root), "stop_hook_active": True})), again)
        self.assertEqual(again.getvalue(), "")
        self.cli_ok("reply", "art", "1", "--as", "maker", "--kind", "PLAN", "-m", "Plan: one coat.")
        done = io.StringIO()
        hook.run_stop_hook(io.StringIO(json.dumps({"session_id": SESSION, "cwd": str(self.root)})), done)
        self.assertEqual(done.getvalue(), "")


class InstallTest(BridgeCase):
    def test_install_keeps_other_hooks_and_replaces_its_own(self) -> None:
        settings = self.root / ".claude" / "settings.local.json"
        settings.parent.mkdir()
        mine = {"type": "command", "command": "echo mine"}
        settings.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {"Stop": [{"hooks": [mine]}]}}))
        project = find_project(self.root)
        hook.install(project, momentum=True)
        hook.install(project, momentum=True)
        data = json.loads(settings.read_text())
        self.assertEqual(data["permissions"], {"allow": ["Read"]})
        stop_commands = [h["command"] for group in data["hooks"]["Stop"] for h in group["hooks"]]
        self.assertEqual(stop_commands.count("echo mine"), 1)
        self.assertEqual(sum("hook claude-stop" in c for c in stop_commands), 1)
        wake_hooks = [h for group in data["hooks"]["Stop"] for h in group["hooks"] if h["command"].endswith("hook claude")]
        self.assertEqual(len(wake_hooks), 1)
        self.assertTrue(wake_hooks[0]["asyncRewake"])
        self.assertEqual(len(data["hooks"]["SessionStart"]), 1)


if __name__ == "__main__":
    unittest.main()
