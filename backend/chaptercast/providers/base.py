"""Provider interface and error taxonomy.

The job pipeline depends only on ``TTSProvider``. That keeps the ElevenLabs
specifics in one file, makes the pipeline testable without the network, and is
what lets the app run in demo mode with no API key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from chaptercast.audio import AudioFormat

VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_valid_voice_id(voice_id: str) -> bool:
    """Voice ids are interpolated into a URL path, so they are strictly validated."""
    return VOICE_ID_RE.fullmatch(voice_id) is not None


@dataclass(frozen=True)
class Voice:
    voice_id: str
    name: str
    category: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class AudioClip:
    data: bytes
    fmt: AudioFormat


class ProviderError(Exception):
    """Base class. ``public_message`` is safe to show to end users; ``detail`` is for logs."""

    code = "provider_error"
    retryable = False
    public_message = "The speech provider returned an error."

    def __init__(self, detail: str = "", *, status_code: int | None = None) -> None:
        super().__init__(detail or self.code)
        self.detail = detail
        self.status_code = status_code


class ProviderAuthError(ProviderError):
    code = "provider_auth"
    public_message = "The server's speech provider credentials were rejected. Contact the operator."


class ProviderQuotaError(ProviderError):
    code = "provider_quota"
    public_message = "The speech provider quota is exhausted. Try again later."


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limited"
    retryable = True
    public_message = "The speech provider is rate limiting requests. Try again shortly."


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"
    retryable = True
    public_message = "The speech provider is temporarily unavailable. Try again shortly."


class ProviderRejectedError(ProviderError):
    code = "provider_rejected"
    public_message = "The speech provider rejected the request. Check the voice and the text."


class TTSProvider(Protocol):
    name: str
    output_format: AudioFormat

    async def list_voices(self) -> list[Voice]: ...

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        *,
        previous_text: str | None = None,
        next_text: str | None = None,
    ) -> AudioClip: ...

    async def aclose(self) -> None: ...
