"""Logging setup with a redaction filter so secrets never reach log output."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

_REDACTED = "***"

# Generic token shapes: Telegram bot tokens, bearer tokens, Anthropic/ElevenLabs-style keys.
_PATTERNS = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bsk_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)(access_token=)[^&\s]+"),
]


class RedactingFilter(logging.Filter):
    """Replaces known secret values and secret-shaped strings in every log record."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, _REDACTED)
        for pattern in _PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + _REDACTED, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self.redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = self.redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Per-request / per-job INFO lines drown out real warnings; keep these libraries at WARNING.
    for noisy in ("httpx", "httpx2", "apscheduler", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
