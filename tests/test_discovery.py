from __future__ import annotations

import json
import os
import time
import unittest

from helpers import BridgeCase

from crosspatch import discovery, picker, registry
from crosspatch.agreement import load_agreement
from crosspatch.project import find_project

CODEX_ID = "0aaaaaaa-1111-2222-3333-444444444444"
CLAUDE_LIVE = "1bbbbbbb-1111-2222-3333-444444444444"
CLAUDE_STORED = "2ccccccc-1111-2222-3333-444444444444"


class DiscoveryCase(BridgeCase):
    def setUp(self) -> None:
        super().setUp()
        codex = self.tmp / "codex-home"
        day = codex / "sessions" / "2026" / "11" / "26"
        day.mkdir(parents=True)
        meta = {"type": "session_meta", "payload": {"id": CODEX_ID, "cwd": str(self.root), "originator": "codex_cli", "source": "cli"}}
        (day / f"rollout-2026-11-26T10-00-00-{CODEX_ID}.jsonl").write_text(json.dumps(meta) + "\n")
        guardian = {"type": "session_meta", "payload": {"cwd": str(self.root), "source": {"subagent": "guardian"}}}
        (day / "rollout-2026-11-26T10-00-01-0ddddddd-1111-2222-3333-444444444444.jsonl").write_text(json.dumps(guardian) + "\n")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (codex / "session_index.jsonl").write_text(
            json.dumps({"id": CODEX_ID, "thread_name": "Paint the street", "updated_at": stamp}) + "\n"
        )
        claude = self.tmp / "claude-home"
        (claude / "sessions").mkdir(parents=True)
        (claude / "sessions" / f"{os.getpid()}.json").write_text(
            json.dumps({"pid": os.getpid(), "sessionId": CLAUDE_LIVE, "cwd": str(self.root), "name": "lead-desk", "status": "idle"})
        )
        transcripts = claude / "projects" / "-project"
        transcripts.mkdir(parents=True)
        records = [
            {"type": "user", "cwd": str(self.root), "message": {"content": "Review the art"}},
            {"type": "ai-title", "aiTitle": "Art review"},
        ]
        (transcripts / f"{CLAUDE_STORED}.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (transcripts / "3eeeeeee-1111-2222-3333-444444444444.jsonl").write_text(json.dumps({"type": "user", "cwd": str(elsewhere)}) + "\n")


class DiscoveryTest(DiscoveryCase):
    def test_lists_sessions_of_this_project(self) -> None:
        out = self.cli_ok("sessions")
        self.assertIn(f"codex:{CODEX_ID}", out)
        self.assertIn("Paint the street", out)
        self.assertIn(f"claude:{CLAUDE_LIVE}", out)
        self.assertIn("claude-code, live", out)
        self.assertIn("Art review", out)
        self.assertNotIn("3eeeeeee", out)
        self.assertNotIn("0ddddddd", out)
        self.assertIn("3eeeeeee", self.cli_ok("sessions", "--all"))

    def test_join_by_number_from_the_listing(self) -> None:
        listed = json.loads(self.cli_ok("sessions", "--json"))
        number = next(i for i, s in enumerate(listed, 1) if s["ref"] == f"codex:{CODEX_ID}")
        self.cli_ok("join", "maker", "--role", "builder", "--session", str(number))
        member = registry.load_members(find_project(self.root))["maker"]
        self.assertEqual((member.platform, member.session, member.wake.adapter), ("codex", f"codex:{CODEX_ID}", "codex-queue"))
        self.assertIn("joined as maker", self.cli_ok("sessions"))

    def test_join_by_id_prefix_and_live_claude_gets_the_hook(self) -> None:
        self.cli_ok("join", "lead", "--role", "lead", "--session", "claude:1bbb")
        member = registry.load_members(find_project(self.root))["lead"]
        self.assertEqual((member.session, member.wake.adapter), (f"claude:{CLAUDE_LIVE}", "claude-hook"))
        self.cli_ok("join", "old", "--role", "reviewer", "--session", "claude:2ccc")
        stored = registry.load_members(find_project(self.root))["old"]
        self.assertEqual(stored.wake.adapter, "claude-resume")


class PickerTest(DiscoveryCase):
    def test_pick_sessions_into_a_thread(self) -> None:
        project = find_project(self.root)
        sessions = [s for s in discovery.discover(["claude-code", "codex"]) if s.id in (CODEX_ID, CLAUDE_LIVE)]
        choices = picker.build_choices(project, sessions)
        lead_number = next(i for i, c in enumerate(choices, 1) if c.session and c.session.id == CLAUDE_LIVE)
        builder_number = next(i for i, c in enumerate(choices, 1) if c.session and c.session.id == CODEX_ID)
        answers = iter(["art", str(lead_number), str(builder_number), "", "", "painter", "y"])
        said: list[str] = []
        thread = picker.run(project, load_agreement(project), sessions, lambda _: next(answers), said.append)
        self.assertEqual(thread.roles["lead"], ["claude-lead"])
        self.assertEqual(thread.roles["builder"], ["painter"])
        members = registry.load_members(project)
        self.assertEqual(members["painter"].wake.adapter, "codex-queue")
        self.assertEqual(members["claude-lead"].wake.adapter, "claude-hook")

    def test_a_bad_number_is_asked_again(self) -> None:
        project = find_project(self.root)
        self.cli_ok("join", "lead", "--role", "lead", "--platform", "claude-code")
        self.cli_ok("join", "maker", "--role", "builder", "--platform", "codex")
        answers = iter(["art", "9", "1", "0", "2", "", "y"])
        said: list[str] = []
        thread = picker.run(project, load_agreement(project), [], lambda _: next(answers), said.append)
        self.assertIn("'9' is not a number from the list", said)
        self.assertEqual((thread.roles["lead"], thread.roles["builder"]), (["lead"], ["maker"]))


class AgeTest(unittest.TestCase):
    def test_age_is_short(self) -> None:
        now = 1_000_000.0
        self.assertEqual(discovery.age(now - 30, now), "30s")
        self.assertEqual(discovery.age(now - 7200, now), "2h")
        self.assertEqual(discovery.age(now - 3 * 86400, now), "3d")


if __name__ == "__main__":
    unittest.main()
