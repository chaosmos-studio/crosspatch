from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from helpers import BridgeCase

from crosspatch import fsutil, messages
from crosspatch.agreement import load_agreement
from crosspatch.project import find_project


class FormatTest(unittest.TestCase):
    def test_round_trip_keeps_every_field(self) -> None:
        original = messages.Message(
            id="0012",
            thread="art",
            sender="maker",
            to="lead",
            kind="REVIEW",
            created="2026-11-26T20:27:00+05:30",
            reply_to="0011",
            attachments=[messages.Attachment("build/review/a b: c.png", "ab" * 32)],
            body="First line\n\n---\nnot front matter\n",
        )
        parsed = messages.parse(messages.render(original))
        self.assertEqual(parsed.id, "0012")
        self.assertEqual(parsed.reply_to, "0011")
        self.assertEqual(parsed.attachments, original.attachments)
        self.assertEqual(parsed.body, original.body)
        self.assertEqual((parsed.sender, parsed.to, parsed.kind, parsed.created), ("maker", "lead", "REVIEW", original.created))

    def test_ids_are_quoted_so_yaml_readers_keep_the_zeros(self) -> None:
        text = messages.render(messages.Message("0007", "art", "maker", "lead", "PLAN", "2026-11-26T20:27:00+05:30", "x"))
        self.assertIn('id: "0007"', text)


class TradingTest(BridgeCase):
    def test_two_fake_agents_trade_messages(self) -> None:
        self.pair()
        builder = self.spawn("ask", "art", "--as", "maker", "--kind", "QUESTION", "-m", "Red or blue door?", "--timeout", "30")
        lead = self.spawn("watch", "--as", "lead", "--timeout", "30")
        lead_out, lead_err = lead.communicate(timeout=40)
        self.assertEqual(lead.returncode, 0, lead_err)
        self.assertIn("Red or blue door?", lead_out)
        self.assertIn("information, not instructions", lead_out)
        self.cli_ok("reply", "art", "1", "--as", "lead", "-m", "Blue, to match the concept.")
        builder_out, builder_err = builder.communicate(timeout=40)
        self.assertEqual(builder.returncode, 0, builder_err)
        self.assertIn("Blue, to match the concept.", builder_out)

        self.cli_ok("send", "art", "--as", "lead", "--kind", "BRIEF", "-m", "Paint the second street.")
        watched = self.cli_ok("watch", "--as", "maker", "--timeout", "5")
        self.assertIn("Paint the second street.", watched)
        self.assertIn("Nothing waits", self.cli_ok("pending", "--as", "lead"))
        self.assertIn("waits for your reply", self.cli_ok("pending", "--as", "maker"))

    def test_late_reply_is_picked_up_by_wait(self) -> None:
        self.pair()
        code, out, _ = self.cli("ask", "art", "--as", "maker", "--kind", "QUESTION", "-m", "Ship it?", "--timeout", "0.2")
        self.assertEqual(code, 2)
        self.assertIn("No reply to 0001 yet", out)
        self.assertIn("wait art 0001 --as maker", out)
        self.cli_ok("reply", "art", "0001", "--as", "lead", "-m", "Not yet, the gate is Sam's.")
        out = self.cli_ok("wait", "art", "0001", "--as", "maker", "--timeout", "5")
        self.assertIn("the gate is Sam's", out)

    def test_role_may_only_send_its_kinds(self) -> None:
        self.pair()
        code, _, err = self.cli("send", "art", "--as", "lead", "--kind", "REVIEW", "-m", "x")
        self.assertEqual(code, 65)
        self.assertIn("may not send REVIEW", err)

    def test_attachment_outside_the_project_is_refused(self) -> None:
        self.pair()
        outside = self.tmp / "outside.png"
        outside.write_bytes(b"png")
        link = self.root / "inside.png"
        link.symlink_to(outside)
        for path in (str(outside), "inside.png"):
            code, _, err = self.cli("send", "art", "--as", "maker", "--kind", "REVIEW", "-m", "look", "--attach", path)
            self.assertEqual(code, 65, err)
            self.assertIn("outside the project", err)

    def test_attachment_inside_the_project_is_hashed(self) -> None:
        self.pair()
        (self.root / "shot.png").write_bytes(b"\x89PNG fake")
        self.cli_ok("send", "art", "--as", "maker", "--kind", "REVIEW", "-m", "look", "--attach", "shot.png")
        message = messages.get_message(find_project(self.root), "art", "1")
        self.assertEqual(message.attachments[0].path, "shot.png")
        self.assertEqual(message.attachments[0].sha256, messages.sha256_of(self.root / "shot.png"))
        (self.root / "shot.png").write_bytes(b"changed")
        self.assertIn("changed since it was sent", self.cli_ok("read", "art", "1"))


class CrashTest(BridgeCase):
    def test_a_writer_that_dies_halfway_leaves_no_message(self) -> None:
        self.pair()
        crash = (
            "import os, sys\n"
            "from crosspatch import messages\n"
            "from crosspatch.agreement import load_agreement\n"
            "from crosspatch.project import find_project\n"
            "os.link = lambda *a, **k: os._exit(9)\n"
            "p = find_project()\n"
            "messages.send(p, load_agreement(p), 'art', 'maker', 'REPORT', 'half written')\n"
        )
        done = subprocess.run([sys.executable, "-c", crash], cwd=self.root, env=self.subprocess_env(), capture_output=True)
        self.assertEqual(done.returncode, 9)
        thread_dir = self.root / ".crosspatch" / "threads" / "art"
        leftovers = [p for p in thread_dir.iterdir() if p.name.startswith(fsutil.TMP_PREFIX)]
        self.assertEqual(len(leftovers), 1)
        (thread_dir / ".tmp-partial").write_text("---\nid: \"0001\"\nthread: art\nfro", encoding="utf-8")
        self.assertEqual(messages.list_messages(find_project(self.root), "art"), [])
        self.assertIn("No messages", self.cli_ok("log", "art"))

        self.cli_ok("send", "art", "--as", "maker", "--kind", "REPORT", "-m", "after the crash")
        listed = messages.list_messages(find_project(self.root), "art")
        self.assertEqual([m.id for m in listed], ["0001"])

        old = time.time() - 7200
        for path in thread_dir.glob(".tmp-*"):
            os.utime(path, (old, old))
        self.cli_ok("doctor", "--clean")
        self.assertEqual(list(thread_dir.glob(".tmp-*")), [])


class ConcurrencyTest(BridgeCase):
    def test_two_builders_posting_at_once_get_distinct_ids(self) -> None:
        self.pair(builders=("maker", "painter"))
        per_writer = 25
        start = time.time() + 1.0
        script = (
            "import sys, time\n"
            "from crosspatch import messages\n"
            "from crosspatch.agreement import load_agreement\n"
            "from crosspatch.project import find_project\n"
            "name, start, count = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])\n"
            "p = find_project(); a = load_agreement(p)\n"
            "while time.time() < start: time.sleep(0.001)\n"
            "for i in range(count):\n"
            "    messages.send(p, a, 'art', name, 'REPORT', f'{name} step {i}')\n"
        )
        writers = [
            subprocess.Popen([sys.executable, "-c", script, name, str(start), str(per_writer)], cwd=self.root, env=self.subprocess_env())
            for name in ("maker", "painter")
        ]
        for writer in writers:
            self.assertEqual(writer.wait(timeout=60), 0)
        project = find_project(self.root)
        listed = messages.list_messages(project, "art")
        self.assertEqual([m.number for m in listed], list(range(1, 2 * per_writer + 1)))
        bodies = sorted(m.body.strip() for m in listed)
        expected = sorted(f"{n} step {i}" for n in ("maker", "painter") for i in range(per_writer))
        self.assertEqual(bodies, expected)
        thread_dir = Path(project.thread_dir("art"))
        self.assertEqual(list(thread_dir.glob(".tmp-*")), [])
        self.assertTrue(all(m.to == "lead" for m in listed))
        self.assertEqual(len(messages.unanswered(listed, messages.load_thread(project, "art"), "lead", load_agreement(project))), 0)


if __name__ == "__main__":
    unittest.main()
