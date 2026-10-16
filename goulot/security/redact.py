"""Redaction of secrets in log lines and error messages (docs/SECURITY.md, threats 12 and 19)."""

from __future__ import annotations

import logging
import re
import threading

REDACTED = "[expurgé]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"), REDACTED),  # Telegram bot token
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), REDACTED),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+|basic\s+)?[^\s,;\"']+"), r"\1" + REDACTED),
    (re.compile(r"(?i)(x-api-key\s*[:=]\s*)[^\s,;\"']+"), r"\1" + REDACTED),
    (re.compile(r"(?i)(ocp-apim-subscription-key\s*[:=]\s*)[^\s,;\"']+"), r"\1" + REDACTED),
    (re.compile(r"(?i)([\"']?(?:password|passwd|mot_de_passe|secret|token)[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'&]+"),
     r"\1" + REDACTED),
    (re.compile(r"(?i)([?&](?:api_key|apikey|key|token|access_token|registrationkey)=)[^&\s#\"']+"), r"\1" + REDACTED),
    (re.compile(r"(?i)(/api/area/csv/)[^/\s]+"), r"\1" + REDACTED),  # NASA FIRMS key in the path
)

_known_secrets: set[str] = set()
_lock = threading.Lock()


def register_secret(value: str) -> None:
    """Remember a secret value loaded at runtime so that it is redacted wherever it appears."""
    if len(value) >= 8:
        with _lock:
            _known_secrets.add(value)


def redact(text: str) -> str:
    with _lock:
        known = sorted(_known_secrets, key=len, reverse=True)
    for value in known:
        if value in text:
            text = text.replace(value, REDACTED)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formats the full record (message, arguments, traceback), then redacts the result."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
