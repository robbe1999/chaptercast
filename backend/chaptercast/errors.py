"""Domain and API error types."""

from __future__ import annotations

from collections.abc import Mapping


class ApiError(Exception):
    """An error that maps directly to an HTTP response."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = dict(headers or {})


class EmptyTextError(ValueError):
    """The submitted text has no speakable content."""


class TextTooLongError(ValueError):
    def __init__(self, limit: int, actual: int) -> None:
        super().__init__(f"Text has {actual} characters; the limit is {limit}")
        self.limit = limit
        self.actual = actual


class BudgetExceededError(Exception):
    """The daily character budget would be exceeded."""

    def __init__(self, remaining: int) -> None:
        super().__init__("Daily character budget exceeded")
        self.remaining = remaining


class CapacityError(Exception):
    """Too many jobs are already queued or running."""
