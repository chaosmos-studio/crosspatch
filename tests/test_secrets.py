from __future__ import annotations

import unittest

from helpers import BridgeCase

from crosspatch import secretcheck

# Fake secrets are assembled at run time so this file never holds one that a scanner would flag.
FAKES = {
    "private key block": "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    "AWS access key": "AKIA" + "Q" * 16,
    "GitHub token": "ghp" + "_" + "a1B2" * 9,
    "sk- API key": "sk" + "-ant-api03-" + "x7" * 15,
    "Slack token": "xox" + "b-" + "1234567890-abcdef",
    "Google API key": "AIza" + "S" * 35,
    "Telegram bot token": "123456789" + ":" + "A" * 35,
    "JSON web token": "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + "abcdefghijKLMNOP",
    "password in a URL": "https://deploy:" + "hunter2hunter2" + "@example.com/repo",
    "assigned secret": "api_key = " + "f00dbabe" * 3,
}


class SecretScanTest(unittest.TestCase):
    def test_each_kind_of_secret_is_found(self) -> None:
        for label, secret in FAKES.items():
            with self.subTest(label=label):
                findings = secretcheck.scan(f"Here it is:\n{secret}\n")
                self.assertIn(label, [f.label for f in findings])
                self.assertEqual(findings[0].line, 2)

    def test_ordinary_engineering_text_passes(self) -> None:
        text = (
            "Draw calls peak at 136, under the 150 budget.\n"
            "The password field now hides its text.\n"
            "Read the key from the KEY_PATH environment variable, never from the repo.\n"
            "Commit 3e5d854 is on sg/revamp, sha256 " + "ab" * 32 + ".\n"
            "See https://example.com/docs and token: <redacted>.\n"
        )
        self.assertEqual(secretcheck.scan(text), [])

    def test_a_finding_never_repeats_the_secret(self) -> None:
        secret = FAKES["GitHub token"]
        findings = secretcheck.scan(secret)
        self.assertNotIn(secret, " ".join(str(f) for f in findings))

    def test_allow_and_extra_patterns(self) -> None:
        public_test_key = "AKIA" + "EXAMPLE" + "0" * 9
        self.assertTrue(secretcheck.scan(public_test_key))
        self.assertEqual(secretcheck.scan(public_test_key, allow=[r"EXAMPLE"]), [])
        self.assertTrue(secretcheck.scan("internal-" + "9" * 12, extra=[r"internal-\d{12}"]))


class SendRefusesSecretsTest(BridgeCase):
    def test_send_refuses_a_message_with_a_secret(self) -> None:
        self.pair()
        secret = FAKES["AWS access key"]
        code, out, err = self.cli("send", "art", "--as", "maker", "--kind", "REPORT", "-m", f"Use {secret} for the upload.")
        self.assertEqual(code, 65)
        self.assertIn("looks like a AWS access key", err)
        self.assertNotIn(secret, err + out)
        self.assertIn("No messages", self.cli_ok("log", "art"))

    def test_send_refuses_a_text_attachment_with_a_secret(self) -> None:
        self.pair()
        (self.root / "notes.txt").write_text("token\n" + FAKES["Slack token"] + "\n", encoding="utf-8")
        code, _, err = self.cli("send", "art", "--as", "maker", "--kind", "REVIEW", "-m", "see notes", "--attach", "notes.txt")
        self.assertEqual(code, 65)
        self.assertIn("notes.txt, line 2", err)

    def test_project_allow_patterns_apply(self) -> None:
        self.pair()
        agreement = self.root / ".crosspatch" / "bridge.toml"
        agreement.write_text(
            agreement.read_text(encoding="utf-8").replace("allow_patterns = []", 'allow_patterns = ["EXAMPLE"]'),
            encoding="utf-8",
        )
        self.cli_ok("send", "art", "--as", "maker", "--kind", "REPORT", "-m", "AKIA" + "EXAMPLE" + "0" * 9)


if __name__ == "__main__":
    unittest.main()
