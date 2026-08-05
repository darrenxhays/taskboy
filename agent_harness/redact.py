"""secret redaction, applied at the durable/outbound sinks: task-row text fields, audit events, slack posts, memory files (§8.2, MEM-011).

two mechanisms: pattern rules for known token families, and an exact-match registry of live
secret values — every loaded or minted credential is registered so it can never survive any sink.
"""

import re

# (pattern, replacement) — replacement keeps any labeled prefix group
PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[redacted]"),  # github classic pat
    (re.compile(r"ghs_[A-Za-z0-9]{20,}"), "[redacted]"),  # github app installation token
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), "[redacted]"),  # github oauth token
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[redacted]"),  # github fine-grained pat
    (re.compile(r"xox[baprse]-[A-Za-z0-9-]{10,}"), "[redacted]"),  # slack bot/user tokens
    (re.compile(r"xapp-[A-Za-z0-9-]{10,}"), "[redacted]"),  # slack app-level token
    (re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA|A3T[A-Z0-9])[A-Z0-9]{16}\b"), "[redacted]"),  # aws access key ids
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"), "[redacted]"),  # anthropic api keys
    (re.compile(r"sntrys_[A-Za-z0-9_+/=.-]{10,}"), "[redacted]"),  # sentry org tokens
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"), "[redacted]"),  # jwts
    (re.compile(r"(?i)(authorization:\s*(?:bearer|basic)\s+)\S+"), r"\1[redacted]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[redacted private key]"),
]


class Redactor:
    def __init__(self):
        self._values: set[str] = set()

    def register(self, value: str | None) -> None:
        """exact-match redaction for a live secret value (loaded from the bundle or minted by the broker)."""
        if value and len(value) >= 8:  # refuse tiny values that would shred normal text
            self._values.add(value)

    def unregister(self, value: str | None) -> None:
        self._values.discard(value or "")

    def redact(self, text: str) -> str:
        if not text:
            return text
        for value in self._values:
            text = text.replace(value, "[redacted]")
        for pattern, replacement in PATTERNS:
            text = pattern.sub(replacement, text)
        return text


# process-wide instance: secrets.load_secrets() registers values, sinks call redactor.redact()
redactor = Redactor()
