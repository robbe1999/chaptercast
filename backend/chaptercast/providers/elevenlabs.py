"""ElevenLabs text-to-speech provider.

Reliability and safety properties:

* the API key lives only in the client's default headers and is never logged,
  returned or placed in an exception message;
* transient failures (timeouts, 5xx, 429) retry with exponential backoff and
  full jitter, honouring ``Retry-After``; permanent failures (auth, quota,
  4xx) fail fast;
* redirects are never followed, so the key cannot be bounced to another host;
* ``previous_text`` / ``next_text`` give the model context across chunk
  boundaries so stitched audio keeps its prosody;
* speech is requested from the ``/with-timestamps`` endpoint, so every clip
  comes back with per-character timings that power the read-along transcript
  and the caption exports at no extra cost;
* voice previews are fetched by a *separate* client that never carries the API
  key, only from an allowlisted host, and only for URLs the API itself returned.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import random
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from pydantic import SecretStr

from chaptercast.audio import AudioFormat, mp3_format
from chaptercast.providers.base import (
    Alignment,
    AudioClip,
    Model,
    Preview,
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderRejectedError,
    ProviderUnavailableError,
    Voice,
    VoiceSettings,
    is_valid_voice_id,
)

log = logging.getLogger(__name__)

_MAX_RETRY_AFTER = 30.0
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 8.0
_VOICES_PAGE_SIZE = 100
_VOICES_MAX_PAGES = 5
_VOICES_CACHE_SECONDS = 300.0
_MODELS_CACHE_SECONDS = 3600.0
_MAX_AUDIO_BYTES = 50 * 1024 * 1024
# base64 inflates by 4/3; the JSON envelope and alignment arrays add a little more.
_MAX_TIMESTAMPS_RESPONSE_BYTES = _MAX_AUDIO_BYTES * 3 // 2
_PREVIEW_HOSTS = frozenset({"storage.googleapis.com"})
_MAX_PREVIEW_BYTES = 2 * 1024 * 1024
_PREVIEW_CACHE_ENTRIES = 64
_MAX_LABELS = 8


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
        self._models_cache: tuple[float, list[Model]] | None = None
        self._preview_urls: dict[str, str] = {}
        self._previews: OrderedDict[str, Preview] = OrderedDict()
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"xi-api-key": api_key.get_secret_value(), "accept": "application/json"},
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            transport=transport,
        )
        # Previews live on a public CDN. This client has no default headers at all,
        # so the API key cannot leak to a third-party host by construction.
        self._public = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=5),
            transport=transport,
        )

    # ------------------------------------------------------------------ public
    async def list_voices(self) -> list[Voice]:
        now = self._clock()
        if self._voices_cache and now - self._voices_cache[0] < _VOICES_CACHE_SECONDS:
            return self._voices_cache[1]

        voices: list[Voice] = []
        preview_urls: dict[str, str] = {}
        token: str | None = None
        for _ in range(_VOICES_MAX_PAGES):
            params: dict[str, Any] = {"page_size": _VOICES_PAGE_SIZE}
            if token:
                params["next_page_token"] = token
            payload = (await self._request("GET", "/v2/voices", params=params)).json()
            for item in payload.get("voices", []):
                voice_id = str(item.get("voice_id", ""))
                if is_valid_voice_id(voice_id):
                    preview = item.get("preview_url")
                    if isinstance(preview, str) and _is_allowed_preview_url(preview):
                        preview_urls[voice_id] = preview
                    voices.append(
                        Voice(
                            voice_id=voice_id,
                            name=str(item.get("name") or voice_id),
                            category=item.get("category"),
                            description=item.get("description"),
                            labels=_clean_labels(item.get("labels")),
                            has_preview=voice_id in preview_urls,
                        )
                    )
            token = payload.get("next_page_token")
            if not payload.get("has_more") or not token:
                break
        self._voices_cache = (now, voices)
        self._preview_urls = preview_urls
        return voices

    async def list_models(self) -> list[Model]:
        now = self._clock()
        if self._models_cache and now - self._models_cache[0] < _MODELS_CACHE_SECONDS:
            return self._models_cache[1]
        payload = (await self._request("GET", "/v1/models")).json()
        models: list[Model] = []
        for item in payload if isinstance(payload, list) else []:
            model_id = str(item.get("model_id", ""))
            if not item.get("can_do_text_to_speech") or not _MODEL_ID_RE.fullmatch(model_id):
                continue
            rates = item.get("model_rates") or {}
            multiplier = rates.get("character_cost_multiplier", 1.0)
            if not isinstance(multiplier, int | float) or not 0 < multiplier <= 10:
                multiplier = 1.0
            models.append(
                Model(
                    model_id=model_id,
                    name=str(item.get("name") or model_id),
                    description=item.get("description"),
                    cost_multiplier=float(multiplier),
                    supports_style=bool(item.get("can_use_style")),
                )
            )
        self._models_cache = (now, models)
        return models

    async def voice_preview(self, voice_id: str) -> Preview | None:
        if voice_id in self._previews:
            self._previews.move_to_end(voice_id)
            return self._previews[voice_id]
        if voice_id not in self._preview_urls:
            await self.list_voices()  # (re)learn preview URLs; cheap when cached
        url = self._preview_urls.get(voice_id)
        if url is None:
            return None
        try:
            response = await self._public.get(url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderUnavailableError(type(exc).__name__) from None
        if response.status_code != 200:
            log.warning("voice preview fetch failed", extra={"status": response.status_code})
            raise ProviderUnavailableError(f"preview fetch returned {response.status_code}")
        if len(response.content) > _MAX_PREVIEW_BYTES:
            raise ProviderRejectedError("preview too large")
        # The CDN labels previews text/plain, so its header is useless. Identify the
        # audio from its magic bytes and set the type ourselves: the browser never
        # receives a content type chosen by a third party.
        content_type = _sniff_audio(response.content)
        if content_type is None:
            log.warning("voice preview is not recognisable audio")
            raise ProviderUnavailableError("preview is not audio")
        preview = Preview(response.content, content_type)
        self._previews[voice_id] = preview
        if len(self._previews) > _PREVIEW_CACHE_ENTRIES:
            self._previews.popitem(last=False)
        return preview

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        *,
        model_id: str | None = None,
        voice_settings: VoiceSettings | None = None,
        previous_text: str | None = None,
        next_text: str | None = None,
    ) -> AudioClip:
        if not is_valid_voice_id(voice_id):
            raise ProviderRejectedError("invalid voice id")
        body: dict[str, Any] = {"text": text, "model_id": model_id or self._model_id}
        if voice_settings is not None and not voice_settings.is_default():
            body["voice_settings"] = voice_settings.as_payload()
        if previous_text:
            body["previous_text"] = previous_text
        if next_text:
            body["next_text"] = next_text
        response = await self._request(
            "POST",
            f"/v1/text-to-speech/{quote(voice_id, safe='')}/with-timestamps",
            params={"output_format": self._output_format_name},
            json=body,
        )
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("application/json"):
            raise ProviderUnavailableError(f"unexpected content-type {content_type!r}")
        if len(response.content) > _MAX_TIMESTAMPS_RESPONSE_BYTES:
            raise ProviderRejectedError("audio response too large")
        try:
            payload = response.json()
            audio = base64.b64decode(payload["audio_base64"], validate=True)
        except (ValueError, KeyError, TypeError, binascii.Error):
            raise ProviderUnavailableError("malformed timestamps response") from None
        if not audio:
            raise ProviderUnavailableError("empty audio in response")
        alignment = _parse_alignment(payload.get("alignment"), text)
        return AudioClip(audio, self.output_format, alignment)

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._public.aclose()

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


_MODEL_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def _is_allowed_preview_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() in _PREVIEW_HOSTS
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
    )


def _sniff_audio(data: bytes) -> str | None:
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:4] == b"OggS":
        return "audio/ogg"
    return None


def _clean_labels(raw: object) -> dict[str, str]:
    """Voice labels (accent, gender, use case...) are display-only; keep them short and flat."""
    if not isinstance(raw, dict):
        return {}
    labels: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str) and value:
            labels[key[:32]] = value[:48]
        if len(labels) >= _MAX_LABELS:
            break
    return labels


def _parse_alignment(raw: object, text: str) -> Alignment | None:
    """Alignment is a bonus: a malformed one degrades to "no transcript", never a failed job."""
    if not isinstance(raw, dict):
        return None
    try:
        alignment = Alignment(
            tuple(str(c) for c in raw["characters"]),
            tuple(float(s) for s in raw["character_start_times_seconds"]),
            tuple(float(e) for e in raw["character_end_times_seconds"]),
        )
    except (KeyError, TypeError, ValueError):
        log.warning("provider returned a malformed alignment")
        return None
    if "".join(alignment.characters) != text:
        # Should not happen with `alignment` (vs `normalized_alignment`), but mapping
        # timings onto the wrong words would be worse than having none.
        log.warning("provider alignment does not match the request text")
        return None
    return alignment
