"""ElevenLabs text-to-speech provider.

Reliability and safety properties:

* the API key lives only in the client's default headers and is never logged,
  returned or placed in an exception message;
* transient failures (timeouts, 5xx, 429) retry with exponential backoff and
  full jitter, honouring ``Retry-After``; permanent failures (auth, quota,
  4xx) fail fast;
* redirects are never followed, so the key cannot be bounced to another host;
* ``previous_text`` / ``next_text`` give the model context across chunk
  boundaries so stitched audio keeps its prosody.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from chaptercast.audio import AudioFormat, mp3_format
from chaptercast.providers.base import (
    AudioClip,
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderRejectedError,
    ProviderUnavailableError,
    Voice,
    is_valid_voice_id,
)

log = logging.getLogger(__name__)

_MAX_RETRY_AFTER = 30.0
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 8.0
_VOICES_PAGE_SIZE = 100
_VOICES_MAX_PAGES = 5
_VOICES_CACHE_SECONDS = 300.0
_MAX_AUDIO_BYTES = 50 * 1024 * 1024


class ElevenLabsProvider:
    name = "elevenlabs"

    def __init__(
        self,
        api_key: SecretStr,
        *,
        base_url: str = "https://api.elevenlabs.io",
        model_id: str = "eleven_multilingual_v2",
        output_format: str = "mp3_44100_128",
        timeout: float = 60.0,
        max_retries: int = 4,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model_id = model_id
        self._output_format_name = output_format
        self.output_format: AudioFormat = mp3_format(int(output_format.split("_")[2]))
        self._max_retries = max_retries
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311  (jitter, not cryptography)
        self._clock = clock
        self._voices_cache: tuple[float, list[Voice]] | None = None
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"xi-api-key": api_key.get_secret_value(), "accept": "application/json"},
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            transport=transport,
        )

    # ------------------------------------------------------------------ public
    async def list_voices(self) -> list[Voice]:
        now = self._clock()
        if self._voices_cache and now - self._voices_cache[0] < _VOICES_CACHE_SECONDS:
            return self._voices_cache[1]

        voices: list[Voice] = []
        token: str | None = None
        for _ in range(_VOICES_MAX_PAGES):
            params: dict[str, Any] = {"page_size": _VOICES_PAGE_SIZE}
            if token:
                params["next_page_token"] = token
            payload = (await self._request("GET", "/v2/voices", params=params)).json()
            for item in payload.get("voices", []):
                voice_id = str(item.get("voice_id", ""))
                if is_valid_voice_id(voice_id):
                    voices.append(
                        Voice(
                            voice_id=voice_id,
                            name=str(item.get("name") or voice_id),
                            category=item.get("category"),
                            description=item.get("description"),
                        )
                    )
            token = payload.get("next_page_token")
            if not payload.get("has_more") or not token:
                break
        self._voices_cache = (now, voices)
        return voices

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        *,
        previous_text: str | None = None,
        next_text: str | None = None,
    ) -> AudioClip:
        if not is_valid_voice_id(voice_id):
            raise ProviderRejectedError("invalid voice id")
        body: dict[str, Any] = {"text": text, "model_id": self._model_id}
        if previous_text:
            body["previous_text"] = previous_text
        if next_text:
            body["next_text"] = next_text
        response = await self._request(
            "POST",
            f"/v1/text-to-speech/{quote(voice_id, safe='')}",
            params={"output_format": self._output_format_name},
            json=body,
            headers={"accept": "audio/mpeg"},
        )
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("audio/"):
            raise ProviderUnavailableError(f"unexpected content-type {content_type!r}")
        if len(response.content) > _MAX_AUDIO_BYTES:
            raise ProviderRejectedError("audio response too large")
        return AudioClip(response.content, self.output_format)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- internals
    def _backoff(self, attempt: int) -> float:
        return self._rng.uniform(0, min(_BACKOFF_CAP, _BACKOFF_BASE * 2**attempt))

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if raw is None:
            return None
        try:
            return max(0.0, min(float(raw), _MAX_RETRY_AFTER))
        except ValueError:
            return None

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        """Short, log-only description of an error body. Never shown to users."""
        try:
            detail = response.json().get("detail")
        except ValueError:
            return ""
        if isinstance(detail, dict):
            return f"{detail.get('status', '')} {detail.get('message', '')}".strip()[:200]
        return str(detail or "")[:200]

    def _classify(self, response: httpx.Response) -> ProviderError:
        status = response.status_code
        text = self._error_text(response)
        if "quota" in text.lower():
            return ProviderQuotaError(text, status_code=status)
        if status in (401, 403):
            return ProviderAuthError(text, status_code=status)
        if status == 429:
            return ProviderRateLimitError(text, status_code=status)
        if status >= 500:
            return ProviderUnavailableError(text, status_code=status)
        return ProviderRejectedError(text, status_code=status)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == self._max_retries:
                    raise ProviderUnavailableError(type(exc).__name__) from None
                delay = self._backoff(attempt)
                log.warning("provider transport error, retrying", extra={"attempt": attempt + 1})
                await self._sleep(delay)
                continue

            if response.status_code < 400:
                return response

            error = self._classify(response)
            if not error.retryable or attempt == self._max_retries:
                log.warning(
                    "provider request failed",
                    extra={"status": response.status_code, "code": error.code},
                )
                raise error
            server_delay = self._retry_after(response)
            await self._sleep(server_delay if server_delay is not None else self._backoff(attempt))
        raise ProviderUnavailableError("retries exhausted")  # pragma: no cover
