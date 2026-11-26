"""The pre-send check that refuses a message which looks like it holds a secret.

A finding names the kind of secret and the line, never the text that matched.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

PATTERNS: tuple[tuple[str, str], ...] = (
    ("private key block", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    ("service account key", r'"private_key"\s*:'),
    ("AWS access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("AWS secret key", r"(?i)aws_secret_access_key\s*[=:]\s*\S{20,}"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    ("sk- API key", r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}"),
    ("Stripe live key", r"\b[rs]k_live_[A-Za-z0-9]{16,}"),
    ("Slack token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("Telegram bot token", r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    ("JSON web token", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("password in a URL", r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]{6,}@"),
    (
        "assigned secret",
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)\b"
        r"\s*[:=]\s*[\"']?(?=[^\s\"']*\d)[^\s\"']{12,}",
    ),
)


@dataclass(frozen=True)
class Finding:
    label: str
    line: int
    where: str = "message"

    def __str__(self) -> str:
        return f"{self.where}, line {self.line}: looks like a {self.label}"


def scan(
    text: str,
    where: str = "message",
    extra: Iterable[str] = (),
    allow: Iterable[str] = (),
) -> list[Finding]:
    checks = [*((label, re.compile(p)) for label, p in PATTERNS), *(("project pattern", re.compile(p)) for p in extra)]
    allowed = [re.compile(p) for p in allow]
    findings: list[Finding] = []
    for label, pattern in checks:
        for match in pattern.finditer(text):
            if any(a.search(match.group(0)) for a in allowed):
                continue
            line = text.count("\n", 0, match.start()) + 1
            findings.append(Finding(label, line, where))
    findings.sort(key=lambda f: (f.where, f.line, f.label))
    return findings
