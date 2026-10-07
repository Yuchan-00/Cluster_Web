"""Secret masking shared by every service (docs/design/security.md 12.3).

Two layers: exact values a process has loaded (tokens, keys) and patterns for the formats we
know. Output is `[REDACTED:<kind>]`. Used on log records, audit details, Telegram text and
anything that leaves for an external model.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, List, Optional, Pattern, Tuple

# Our own token formats first: they are the most likely to leak into command output.
PATTERNS: List[Tuple[str, Pattern[str]]] = [
    ("agent-token", re.compile(r"cat_[A-Za-z0-9_-]{43}")),
    ("service-token", re.compile(r"cst_[A-Za-z0-9_-]{43}")),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("telegram-token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")),
    (
        "private-key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    ),
    ("bearer", re.compile(r"(?i)authorization:\s*bearer\s+\S+")),
    ("age-key", re.compile(r"AGE-SECRET-KEY-1[A-Z0-9]{50,}")),
    (
        "credential",
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key)"
            r"(\s*[=:]\s*)(?P<q>[\"']?)(?P<v>[^\s\"'&;]{4,})(?P=q)"
        ),
    ),
]

_MIN_EXACT = 8  # shorter values would mask ordinary words


class Redactor:
    def __init__(self, patterns: Iterable[Tuple[str, Pattern[str]]] = PATTERNS) -> None:
        self.patterns = list(patterns)
        self._exact: Dict[str, str] = {}

    def register(self, value: Optional[str], kind: str = "secret") -> None:
        """Mask this exact value wherever it appears (a loaded token, key or password)."""
        if value and len(value) >= _MIN_EXACT:
            self._exact[value] = kind

    def redact(self, text: str) -> str:
        if not text:
            return text
        for value, kind in sorted(self._exact.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(value, f"[REDACTED:{kind}]")
        for kind, pattern in self.patterns:
            if kind == "credential":
                text = pattern.sub(
                    lambda m, k=kind: f"{m.group(1)}{m.group(2)}[REDACTED:{k}]", text
                )
            else:
                text = pattern.sub(f"[REDACTED:{kind}]", text)
        return text

    def redact_obj(self, obj):  # type: ignore[no-untyped-def]
        """Recursively mask every string inside JSON-like data."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v) for v in obj]
        return obj


class RedactingFilter(logging.Filter):
    """logging filter that masks the formatted message and its arguments."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self.redactor.redact(str(record.getMessage()))
            record.args = ()
        except Exception:  # noqa: BLE001 - never let masking break logging
            record.msg = "[log record could not be redacted]"
            record.args = ()
        return True


default_redactor = Redactor()
redact = default_redactor.redact
