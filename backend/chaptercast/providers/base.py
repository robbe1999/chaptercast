"""Provider interface and error taxonomy.

The job pipeline depends only on ``TTSProvider``. That keeps the ElevenLabs
specifics in one file, makes the pipeline testable without the network, and is
what lets the app run in demo mode with no API key.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

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
    labels: Mapping[str, str] = field(default_factory=dict)
    has_preview: bool = False


@dataclass(frozen=True)
class Model:
    model_id: str
    name: str
    description: str | None = None
    # Credits charged per character, relative to the standard models (Flash/Turbo are 0.5).
    cost_multiplier: float = 1.0
    supports_style: bool = False


@dataclass(frozen=True)
class VoiceSettings:
    """Per-request voice tuning. ``None`` means "use the voice's stored default"."""

    stability: float | None = None
    similarity_boost: float | None = None
    style: float | None = None
    speed: float | None = None

    def as_payload(self) -> dict[str, float]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    def is_default(self) -> bool:
        return not self.as_payload()


@dataclass(frozen=True)
class Alignment:
    """Per-character timings for one clip, as returned by a timestamps-capable provider.

    ``characters`` concatenate back to exactly the text that was synthesised, so
    timings can be mapped onto words without guessing.
    """

    characters: tuple[str, ...]
    starts: tuple[float, ...]
    ends: tuple[float, ...]

    def __post_init__(self) -> None:
        if not len(self.characters) == len(self.starts) == len(self.ends):
            raise ValueError("alignment arrays have different lengths")

    def to_json(self) -> dict[str, Any]:
        return {
            "characters": list(self.characters),
            "starts": list(self.starts),
            "ends": list(self.ends),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Alignment:
        return cls(
            tuple(str(c) for c in data["characters"]),
            tuple(float(s) for s in data["starts"]),
            tuple(float(e) for e in data["ends"]),
        )


@dataclass(frozen=True)
class AudioClip:
    data: bytes
    fmt: AudioFormat
    alignment: Alignment | None = None


@dataclass(frozen=True)
class Preview:
    data: bytes
    content_type: str


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

    async def list_models(self) -> list[Model]: ...

    async def voice_preview(self, voice_id: str) -> Preview | None: ...

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        *,
        model_id: str | None = None,
        voice_settings: VoiceSettings | None = None,
        previous_text: str | None = None,
        next_text: str | None = None,
    ) -> AudioClip: ...

    async def aclose(self) -> None: ...
