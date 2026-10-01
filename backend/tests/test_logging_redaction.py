from __future__ import annotations

import json
import logging

import pytest

from chaptercast.logging_config import JsonFormatter, Redactor, configure_logging, request_id_var
from tests.conftest import SENTINEL_KEY


def test_registered_literals_are_redacted_everywhere() -> None:
    redactor = Redactor()
    redactor.register("super-secret-value-123")
    assert "super-secret" not in redactor.redact(
        "a super-secret-value-123 b super-secret-value-123"
    )


def test_short_literals_are_ignored_to_avoid_shredding_logs() -> None:
    redactor = Redactor()
    redactor.register("abc")
    assert redactor.redact("abc def") == "abc def"


@pytest.mark.parametrize(
    "line",
    [
        "xi-api-key: HEADERVALUE12345",
        "{'xi-api-key': 'HEADERVALUE12345'}",
        '{\\"xi-api-key\\": \\"HEADERVALUE12345\\"}',
        "Authorization: Bearer HEADERVALUE12345",
        "authorization=HEADERVALUE12345",
        "token is Bearer HEADERVALUE12345 ok",
    ],
)
def test_header_shapes_are_redacted(line: str) -> None:
    assert "HEADERVALUE12345" not in Redactor().redact(line)


def test_unregistered_key_shaped_strings_are_redacted() -> None:
    assert SENTINEL_KEY not in Redactor().redact(f"leaked {SENTINEL_KEY} here")


def test_ordinary_text_is_untouched() -> None:
    text = "job abc123 finished in 41 ms"
    assert Redactor().redact(text) == text


def _emit(level: str = "INFO") -> tuple[logging.Logger, str]:
    configure_logging(level, [SENTINEL_KEY])
    return logging.getLogger("test.emit"), level


def test_formatter_emits_valid_json_with_request_id(capsys: pytest.CaptureFixture[str]) -> None:
    logger, _ = _emit()
    token = request_id_var.set("req-12345678")
    try:
        logger.info("hello", extra={"job_id": "j1"})
    finally:
        request_id_var.reset(token)
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["message"] == "hello"
    assert record["request_id"] == "req-12345678"
    assert record["job_id"] == "j1"
    assert record["level"] == "INFO"


def test_secrets_are_redacted_in_messages_extras_and_tracebacks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger, _ = _emit()
    logger.warning("key was %s", SENTINEL_KEY, extra={"header": SENTINEL_KEY})
    try:
        raise RuntimeError(f"boom {SENTINEL_KEY}")
    except RuntimeError:
        logger.exception("failed")
    out = capsys.readouterr().out
    assert SENTINEL_KEY not in out
    assert "[REDACTED]" in out


def test_configure_logging_is_idempotent() -> None:
    configure_logging("INFO")
    configure_logging("INFO")
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_chaptercast_handler", False)]
    assert len(ours) == 1
    assert isinstance(ours[0].formatter, JsonFormatter)
