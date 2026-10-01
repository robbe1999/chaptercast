"""Structured JSON logging with secret redaction.

Redaction runs on the *final serialized line*, so it also covers exception
tracebacks and anything a library logs. Known literal secrets are registered at
startup; pattern rules catch keys that were never registered.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

REDACTED = "[REDACTED]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # header-ish key/value pairs: xi-api-key: abc, 'xi-api-key': 'abc', \"xi-api-key\": \"abc\"
    (
        re.compile(r"""(?i)(xi-api-key[\\'"]*\s*[:=]\s*[\\'"]*)[^\s\\'",}]+"""),
        rf"\1{REDACTED}",
    ),
    (
        re.compile(r"""(?i)(authorization[\\'"]*\s*[:=]\s*[\\'"]*)(?:bearer\s+)?[^\s\\'",}]+"""),
        rf"\1{REDACTED}",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), f"Bearer {REDACTED}"),
    (re.compile(r"\bsk_[A-Za-z0-9]{16,}\b"), REDACTED),
)


class Redactor:
    def __init__(self) -> None:
        self._literals: set[str] = set()

    def register(self, *secrets: str) -> None:
        # Very short strings would shred ordinary log text; real keys are long.
        self._literals.update(s for s in secrets if len(s) >= 8)

    def clear(self) -> None:
        self._literals.clear()

    def redact(self, text: str) -> str:
        for literal in sorted(self._literals, key=len, reverse=True):
            text = text.replace(literal, REDACTED)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text


redactor = Redactor()

_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return redactor.redact(json.dumps(payload, ensure_ascii=False, default=str))


_HANDLER_FLAG = "_chaptercast_handler"


def configure_logging(level: str, secrets: list[str] | None = None) -> None:
    redactor.clear()
    redactor.register(*(secrets or []))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    setattr(handler, _HANDLER_FLAG, True)
    root = logging.getLogger()
    # Replace only our own previous handler; never clobber handlers owned by others.
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_FLAG, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    # We emit our own access log (no query strings, no client IPs).
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
