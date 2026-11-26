from __future__ import annotations

import json
import os
import unittest

from helpers import BridgeCase

from crosspatch import registry
from crosspatch.project import find_project


class PairingTest(BridgeCase):
    def test_link_shows_who_is_bridged_to_whom(self) -> None:
        self.cli_ok("join", "lead", "--role", "lead", "--platform", "claude-code")
        self.cli_ok("join", "maker", "--role", "builder", "--platform", "codex")
        self.cli_ok("join", "checker", "--role", "reviewer", "--platform", "claude-code")
        self.cli_ok("link", "art", "--lead", "lead", "--builder", "maker", "--reviewer", "checker")
        thread = registry.load_thread(find_project(self.root), "art")
        self.assertEqual(list(thread.roles), ["lead", "builder", "reviewer", "human"])
        self.assertEqual(thread.roles["human"], ["sam"])
        threads = self.cli_ok("threads")
        self.assertIn("art (open): lead: lead; builder: maker; reviewer: checker; human: sam", threads)
        status = self.cli_ok("status", "art")
        self.assertIn("maker (builder, codex)", status)
        self.assertIn("wake poll: ok", status)

    def test_several_threads_with_their_own_members(self) -> None:
        self.cli_ok("join", "lead", "--role", "lead", "--platform", "claude-code")
        self.cli_ok("join", "maker", "--role", "builder", "--platform", "codex")
        self.cli_ok("join", "painter", "--role", "builder", "--platform", "codex")
        self.cli_ok("link", "art", "--lead", "lead", "--builder", "painter")
        self.cli_ok("link", "code", "--lead", "lead", "--builder", "maker")
        self.cli_ok("send", "code", "--as", "maker", "--kind", "REPORT", "-m", "code step")
        self.cli_ok("send", "art", "--as", "painter", "--kind", "REPORT", "-m", "art step")
        code, _, err = self.cli("send", "art", "--as", "maker", "--kind", "REPORT", "-m", "wrong thread")
        self.assertEqual(code, 64)
        self.assertIn("not in thread 'art'", err)
        watched = self.cli_ok("watch", "--as", "lead", "--timeout", "2")
        self.assertIn("code step", watched)
        self.assertIn("art step", watched)

        other = self.tmp / "other-project"
        other.mkdir()
        self.cli_ok("init", str(other), "--human", "sam")
        self.cli_ok("-C", str(other), "join", "boss", "--role", "lead", "--platform", "claude-code")
        self.cli_ok("-C", str(other), "join", "hands", "--role", "builder", "--platform", "codex")
        self.cli_ok("link", "-C", str(other), "site", "--lead", "boss", "--builder", "hands")
        everything = self.cli_ok("threads", "--all")
        for name in ("art (open)", "code (open)", "site (open)"):
            self.assertIn(name, everything)

    def test_unlink_a_member_then_close_the_thread(self) -> None:
        self.pair(builders=("maker", "painter"))
        self.cli_ok("unlink", "art", "painter")
        thread = registry.load_thread(find_project(self.root), "art")
        self.assertEqual(thread.roles["builder"], ["maker"])
        code, _, err = self.cli("send", "art", "--as", "painter", "--kind", "REPORT", "-m", "x")
        self.assertEqual(code, 64)
        self.cli_ok("unlink", "art")
        code, _, err = self.cli("send", "art", "--as", "maker", "--kind", "REPORT", "-m", "x")
        self.assertIn("closed", err)
        self.assertIn("No threads", self.cli_ok("threads"))
        self.assertIn("art (closed)", self.cli_ok("threads", "--closed"))
        self.cli_ok("link", "art", "--lead", "lead", "--builder", "painter")
        self.assertEqual(registry.load_thread(find_project(self.root), "art").status, "open")

    def test_a_member_holds_one_role_per_thread(self) -> None:
        self.pair()
        code, _, err = self.cli("link", "art", "--reviewer", "maker")
        self.assertEqual(code, 64)
        self.assertIn("already the builder", err)

    def test_leave_refuses_while_linked(self) -> None:
        self.pair()
        code, _, err = self.cli("leave", "maker")
        self.assertIn("linked in art", err)
        self.cli_ok("leave", "maker", "--force")
        self.assertNotIn("maker", registry.load_thread(find_project(self.root), "art").members)

    def test_a_session_joins_only_once(self) -> None:
        self.cli_ok("join", "one", "--role", "builder", "--session", "codex:0000aaaa-0000-0000-0000-000000000001")
        code, _, err = self.cli("join", "two", "--role", "builder", "--session", "codex:0000aaaa-0000-0000-0000-000000000001")
        self.assertIn("already joined as 'one'", err)

    def test_join_defaults_the_wake_from_the_session(self) -> None:
        self.cli_ok("join", "maker", "--role", "builder", "--session", "codex:0000aaaa-0000-0000-0000-000000000002")
        member = registry.load_members(find_project(self.root))["maker"]
        self.assertEqual((member.platform, member.wake.adapter, member.wake.target), ("codex", "codex-queue", "0000aaaa-0000-0000-0000-000000000002"))

    def test_whoami_and_as_default_from_the_session(self) -> None:
        session = "11111111-2222-3333-4444-555555555555"
        self.cli_ok("join", "lead", "--role", "lead", "--session", f"claude:{session}")
        os.environ["CLAUDE_CODE_SESSION_ID"] = session
        self.assertIn("lead: lead, claude-code", self.cli_ok("whoami"))

    def test_members_json_lists_threads(self) -> None:
        self.pair()
        data = json.loads(self.cli_ok("members", "--json"))
        self.assertEqual(data["maker"]["threads"], ["art"])


if __name__ == "__main__":
    unittest.main()
