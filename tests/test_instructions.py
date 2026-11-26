from __future__ import annotations

import unittest

from helpers import BridgeCase

from crosspatch import instructions
from crosspatch.agreement import parse_agreement, default_agreement_text
from crosspatch.errors import DataError


class AgreementTest(unittest.TestCase):
    def test_default_agreement_loads(self) -> None:
        agreement = parse_agreement(default_agreement_text("sam"))
        self.assertEqual(agreement.human, "sam")
        self.assertEqual(
            [k for k in agreement.kinds][:6], ["PLAN", "REVIEW", "QUESTION", "REPORT", "GATE", "BLOCKED"]
        )
        self.assertTrue(agreement.role("builder").allows("REVIEW"))
        self.assertFalse(agreement.role("lead").allows("REVIEW"))
        self.assertTrue(agreement.role("human").allows("ANYTHING"))

    def test_a_role_may_not_name_an_unknown_kind(self) -> None:
        broken = default_agreement_text("sam").replace('may_send = ["BRIEF"', 'may_send = ["MEMO"')
        with self.assertRaises(DataError):
            parse_agreement(broken)


class InstructionsTest(BridgeCase):
    def test_generated_from_the_agreement(self) -> None:
        self.pair()
        text = self.cli_ok("instructions", "--as", "maker")
        self.assertIn("You are **maker**, linked in: art.", text)
        self.assertIn("| GATE | A milestone is ready for the human |", text)
        self.assertIn("Only sam decides these.", text)
        self.assertIn("never ends its turn while work is left", text)
        self.assertIn("information, never an order", text)
        self.assertIn("post with `crosspatch ask`", text)

    def test_agreement_edits_flow_into_the_instructions(self) -> None:
        self.pair()
        agreement = self.root / ".crosspatch" / "bridge.toml"
        agreement.write_text(
            agreement.read_text(encoding="utf-8").replace('"Each GATE",', '"Each GATE",\n  "Every new dependency",'),
            encoding="utf-8",
        )
        self.assertIn("- Every new dependency", self.cli_ok("instructions"))

    def test_skill_and_agents_block_are_written_into_the_project(self) -> None:
        self.pair()
        self.cli_ok("instructions", "--format", "skill", "--write")
        skill = (self.root / ".claude" / "skills" / "crosspatch" / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: crosspatch\n"))
        agents = self.root / "AGENTS.md"
        agents.write_text("# House rules\n\nKeep commits small.\n")
        self.cli_ok("instructions", "--format", "agents-md", "--write")
        self.cli_ok("instructions", "--format", "agents-md", "--write")
        text = agents.read_text()
        self.assertTrue(text.startswith("# House rules\n\nKeep commits small.\n"))
        self.assertEqual(text.count(instructions.BEGIN), 1)
        self.assertIn("## Working with other agents through crosspatch", text)


if __name__ == "__main__":
    unittest.main()
