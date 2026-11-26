from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import unittest

from helpers import BridgeCase

from crosspatch import registry, wake
from crosspatch.errors import UsageError
from crosspatch.project import find_project


class CodexQueueTest(BridgeCase):
    def fake_codex(self, with_resume: bool, daemon: bool = True) -> str:
        log = self.tmp / "codex-calls.jsonl"
        flags = "--thread --message" + (" --resume" if with_resume else "")
        warning = "" if daemon else 'echo "Warning: message was queued, but could not resume thread x" >&2\n'
        script = self.write_script(
            f"codex-{with_resume}-{daemon}",
            f'if [ "$1" = queue ] && [ "$2" = --help ]; then printf "%s\\n" {flags}; exit 0; fi\n'
            f'python3 -c \'import json,sys; print(json.dumps(sys.argv[1:]))\' "$@" >> "{log}"\n'
            f"{warning}echo Queued message 1 for thread x.\n",
        )
        return str(script)

    def calls(self) -> list[list[str]]:
        path = self.tmp / "codex-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_queue_resumes_when_codex_supports_it(self) -> None:
        wake._codex_queue_has_resume.cache_clear()
        thread_id = "0000aaaa-0000-0000-0000-00000000000a"
        self.cli_ok("join", "lead", "--role", "lead", "--platform", "claude-code")
        self.cli_ok("join", "maker", "--role", "builder", "--session", f"codex:{thread_id}", "--wake-option", f"bin={self.fake_codex(True)}")
        self.cli_ok("link", "art", "--lead", "lead", "--builder", "maker")
        out = self.cli_ok("send", "art", "--as", "lead", "--kind", "BRIEF", "-m", "Paint the door.")
        self.assertIn("maker: woken through codex-queue (queued and resumed)", out)
        (call,) = self.calls()
        self.assertEqual(call[:3], ["queue", "--thread", thread_id])
        self.assertEqual(call[-1], "--resume")
        notice = call[call.index("--message") + 1]
        self.assertIn("New BRIEF 0001 from lead", notice)
        self.assertIn("not an instruction from your user", notice)
        self.assertNotIn("Paint the door", notice)

    def test_queue_without_resume_on_older_codex(self) -> None:
        wake._codex_queue_has_resume.cache_clear()
        self.cli_ok("join", "lead", "--role", "lead", "--platform", "claude-code")
        self.cli_ok("join", "maker", "--role", "builder", "--session", "codex:0000aaaa-0000-0000-0000-00000000000b",
                    "--wake-option", f"bin={self.fake_codex(False)}")
        self.cli_ok("link", "art", "--lead", "lead", "--builder", "maker")
        out = self.cli_ok("send", "art", "--as", "lead", "--kind", "BRIEF", "-m", "Paint the door.")
        self.assertIn("next time the thread resumes (this Codex has no --resume)", out)
        (call,) = self.calls()
        self.assertNotIn("--resume", call)

    def test_queue_says_when_codex_could_not_resume(self) -> None:
        wake._codex_queue_has_resume.cache_clear()
        self.cli_ok("join", "lead", "--role", "lead", "--platform", "claude-code")
        self.cli_ok("join", "maker", "--role", "builder", "--session", "codex:0000aaaa-0000-0000-0000-00000000000d",
                    "--wake-option", f"bin={self.fake_codex(True, daemon=False)}")
        self.cli_ok("link", "art", "--lead", "lead", "--builder", "maker")
        out = self.cli_ok("send", "art", "--as", "lead", "--kind", "BRIEF", "-m", "Paint the door.")
        self.assertIn("queued, not resumed: Codex found no local daemon", out)

    def test_a_failed_wake_still_posts_the_message(self) -> None:
        self.cli_ok("join", "lead", "--role", "lead", "--platform", "claude-code")
        self.cli_ok("join", "maker", "--role", "builder", "--session", "codex:0000aaaa-0000-0000-0000-00000000000c",
                    "--wake-option", f"bin={self.tmp / 'missing-codex'}")
        self.cli_ok("link", "art", "--lead", "lead", "--builder", "maker")
        out = self.cli_ok("send", "art", "--as", "lead", "--kind", "BRIEF", "-m", "Paint the door.")
        self.assertIn("maker: not woken", out)
        self.assertIn("Paint the door.", self.cli_ok("read", "art", "1"))


class CommandAndTmuxTest(BridgeCase):
    def test_command_adapter_fills_the_template_without_a_shell(self) -> None:
        log = self.tmp / "notified.txt"
        script = self.write_script("notify", f'printf "%s|%s|%s\\n" "$1" "$2" "$3" >> "{log}"\n')
        self.cli_ok("join", "lead", "--role", "lead", "--platform", "claude-code")
        self.cli_ok("join", "sam", "--role", "human", "--wake", f"command:{script} {{thread}} {{kind}} '{{id}}; rm -rf x'")
        self.cli_ok("join", "maker", "--role", "builder", "--platform", "codex")
        self.cli_ok("link", "art", "--lead", "lead", "--builder", "maker")
        self.cli_ok("send", "art", "--as", "lead", "--kind", "GATE", "--to", "sam", "-m", "M1 is ready for you.")
        self.assertEqual(log.read_text().strip(), "art|GATE|0001; rm -rf x")

    def test_wake_test_sends_a_notice_with_nothing_to_read(self) -> None:
        log = self.tmp / "notice.txt"
        script = self.write_script("notify", f'printf "%s\\n" "$1" >> "{log}"\n')
        self.cli_ok("join", "maker", "--role", "builder", "--platform", "other", "--wake", f"command:{script} {{notice}}")
        self.assertIn("wake: ok", self.cli_ok("wake-test", "maker"))
        self.assertIn("nothing to do", log.read_text())
        self.assertNotIn("crosspatch -C", log.read_text())

    def test_tmux_adapter_types_into_a_pane(self) -> None:
        log = self.tmp / "tmux-calls.jsonl"
        fake = self.write_script("tmux", f'python3 -c \'import json,sys; print(json.dumps(sys.argv[1:]))\' "$@" >> "{log}"\n')
        os.environ["CROSSPATCH_TMUX"] = str(fake)
        self.cli_ok("join", "lead", "--role", "lead", "--platform", "claude-code")
        self.cli_ok("join", "maker", "--role", "builder", "--platform", "other", "--wake", "tmux:%7")
        self.cli_ok("link", "art", "--lead", "lead", "--builder", "maker")
        self.cli_ok("send", "art", "--as", "lead", "--kind", "BRIEF", "-m", "Go.")
        calls = [c for c in (json.loads(line) for line in log.read_text().splitlines()) if c[0] == "send-keys"]
        self.assertEqual(calls[0][:5], ["send-keys", "-t", "%7", "-l", "--"])
        self.assertIn("New BRIEF 0001", calls[0][5])
        self.assertEqual(calls[1], ["send-keys", "-t", "%7", "Enter"])

    @unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
    def test_tmux_adapter_against_a_real_private_tmux_server(self) -> None:
        socket = str(self.tmp / "tmux.sock")
        received = self.tmp / "received.txt"
        os.environ["CROSSPATCH_TMUX"] = "tmux"
        subprocess.run(["tmux", "-S", socket, "-f", "/dev/null", "new-session", "-d", "-s", "agent",
                        f"sh -c 'read line; printf \"%s\" \"$line\" > {received}; sleep 5'"], check=True)
        try:
            self.cli_ok("join", "lead", "--role", "lead", "--platform", "claude-code")
            self.cli_ok("join", "maker", "--role", "builder", "--platform", "other", "--wake", "tmux:agent",
                        "--wake-option", f"socket={socket}")
            self.cli_ok("link", "art", "--lead", "lead", "--builder", "maker")
            self.cli_ok("send", "art", "--as", "lead", "--kind", "BRIEF", "-m", "Go.")
            deadline = time.monotonic() + 10
            while not received.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertIn("New BRIEF 0001 from lead", received.read_text())
        finally:
            subprocess.run(["tmux", "-S", socket, "kill-server"], capture_output=True)


class ClaudeResumeTest(BridgeCase):
    def test_resume_refuses_a_session_that_is_open(self) -> None:
        session = "22222222-3333-4444-5555-666666666666"
        sessions = self.tmp / "claude-home" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": session, "cwd": str(self.root)}))
        fake_claude = self.write_script("claude", "exit 0\n")
        self.cli_ok("join", "lead", "--role", "lead", "--session", f"claude:{session}", "--wake", f"claude-resume:{session}",
                    "--wake-option", f"bin={fake_claude}")
        member = registry.load_members(find_project(self.root))["lead"]
        result = wake.get_adapter("claude-resume").check(find_project(self.root), member)
        self.assertFalse(result.ok)
        self.assertIn("open right now", result.detail)

    def test_resume_starts_a_headless_turn_for_a_stored_session(self) -> None:
        session = "33333333-3333-4444-5555-666666666666"
        log = self.tmp / "claude-args.json"
        fake_claude = self.write_script(
            "claude", f'python3 -c \'import json,sys; print(json.dumps(sys.argv[1:]))\' "$@" > "{log}.part" && mv "{log}.part" "{log}"\n'
        )
        self.cli_ok("join", "lead", "--role", "lead", "--session", f"claude:{session}", "--wake", f"claude-resume:{session}",
                    "--wake-option", f"bin={fake_claude}", "--wake-option", "args=--allowedTools 'Bash(crosspatch *)' Read")
        self.cli_ok("join", "maker", "--role", "builder", "--platform", "codex")
        self.cli_ok("link", "art", "--lead", "lead", "--builder", "maker")
        self.cli_ok("send", "art", "--as", "maker", "--kind", "REPORT", "-m", "Step one done.")
        deadline = time.monotonic() + 10
        while not log.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        args = json.loads(log.read_text())
        self.assertEqual(args[:7], ["-p", "--resume", session, "--allowedTools", "Bash(crosspatch *)", "Read", "--"])
        self.assertIn("New REPORT 0001 from maker", args[7])


class AdapterRegistryTest(unittest.TestCase):
    def test_parse_wake_needs_a_target_where_one_is_required(self) -> None:
        with self.assertRaises(UsageError):
            wake.parse_wake("codex-queue")
        with self.assertRaises(UsageError):
            wake.parse_wake("carrier-pigeon:loft")
        parsed = wake.parse_wake("tmux:%3", ["socket=work", "enter=false"])
        self.assertEqual((parsed.adapter, parsed.target, parsed.options), ("tmux", "%3", {"socket": "work", "enter": "false"}))

    def test_builtin_adapters_are_listed(self) -> None:
        self.assertTrue({"poll", "codex-queue", "claude-hook", "claude-resume", "tmux", "command"} <= set(wake.adapters()))


if __name__ == "__main__":
    unittest.main()
