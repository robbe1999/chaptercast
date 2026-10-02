from __future__ import annotations

import base64
import json
import random
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from chaptercast.providers.base import (
    ProviderAuthError,
    ProviderQuotaError,
    ProviderRejectedError,
    ProviderUnavailableError,
    VoiceSettings,
)
from chaptercast.providers.elevenlabs import ElevenLabsProvider, _sniff_audio
from tests.conftest import SENTINEL_KEY

BASE = "https://api.elevenlabs.io"
TTS = f"{BASE}/v1/text-to-speech/voice1/with-timestamps"


def tts_payload(audio: bytes, text: str | None = None) -> dict[str, Any]:
    """The documented /with-timestamps response. Alignment only when ``text`` is given."""
    body: dict[str, Any] = {"audio_base64": base64.b64encode(audio).decode()}
    if text is not None:
        body["alignment"] = {
            "characters": list(text),
            "character_start_times_seconds": [i * 0.1 for i in range(len(text))],
            "character_end_times_seconds": [(i + 1) * 0.1 for i in range(len(text))],
        }
    return body


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def provider(sleeps: list[float]) -> ElevenLabsProvider:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return ElevenLabsProvider(
        SecretStr(SENTINEL_KEY), sleep=fake_sleep, rng=random.Random(0), max_retries=3
    )


async def test_synthesize_sends_the_documented_request(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(TTS).respond(200, json=tts_payload(b"MP3DATA"))
    clip = await provider.synthesize(
        "Hello.", "voice1", previous_text="Before.", next_text="After."
    )

    assert clip.data == b"MP3DATA"
    assert clip.fmt.extension == "mp3"
    request = route.calls.last.request
    assert request.headers["xi-api-key"] == SENTINEL_KEY
    assert request.url.params["output_format"] == "mp3_44100_128"
    assert json.loads(request.content) == {
        "text": "Hello.",
        "model_id": "eleven_multilingual_v2",
        "previous_text": "Before.",
        "next_text": "After.",
    }


async def test_context_fields_are_omitted_when_absent(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(TTS).respond(200, json=tts_payload(b"x"))
    await provider.synthesize("Hi.", "voice1")
    assert set(json.loads(route.calls.last.request.content)) == {"text", "model_id"}


async def test_server_errors_are_retried_then_succeed(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.post(TTS).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=tts_payload(b"ok"))]
    )
    assert (await provider.synthesize("Hi.", "voice1")).data == b"ok"
    assert route.call_count == 2
    assert len(sleeps) == 1


async def test_retry_after_is_honoured(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    respx_mock.post(TTS).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(200, json=tts_payload(b"ok")),
        ]
    )
    await provider.synthesize("Hi.", "voice1")
    assert sleeps == [2.0]


async def test_retry_after_is_capped(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    respx_mock.post(TTS).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "99999"}),
            httpx.Response(200, json=tts_payload(b"ok")),
        ]
    )
    await provider.synthesize("Hi.", "voice1")
    assert sleeps == [30.0]


async def test_gives_up_after_max_retries_with_bounded_backoff(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.post(TTS).respond(503)
    with pytest.raises(ProviderUnavailableError):
        await provider.synthesize("Hi.", "voice1")
    assert route.call_count == 4  # 1 attempt + 3 retries
    assert len(sleeps) == 3
    assert all(0 <= s <= 8 for s in sleeps)


async def test_auth_errors_fail_fast_and_do_not_leak_the_key(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(TTS).respond(401, json={"detail": {"status": "invalid_api_key"}})
    with pytest.raises(ProviderAuthError) as info:
        await provider.synthesize("Hi.", "voice1")
    assert route.call_count == 1
    assert SENTINEL_KEY not in str(info.value)
    assert SENTINEL_KEY not in info.value.public_message


async def test_quota_exhaustion_is_distinguished_and_not_retried(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(TTS).respond(
        401, json={"detail": {"status": "quota_exceeded", "message": "out of credits"}}
    )
    with pytest.raises(ProviderQuotaError):
        await provider.synthesize("Hi.", "voice1")
    assert route.call_count == 1


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_client_errors_are_not_retried(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter, status: int
) -> None:
    route = respx_mock.post(TTS).respond(status, json={"detail": "bad"})
    with pytest.raises(ProviderRejectedError):
        await provider.synthesize("Hi.", "voice1")
    assert route.call_count == 1


async def test_timeouts_are_retried_then_reported_without_details(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(TTS).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(ProviderUnavailableError) as info:
        await provider.synthesize("Hi.", "voice1")
    assert route.call_count == 4
    assert str(info.value) == "ConnectTimeout"


@pytest.mark.parametrize("voice_id", ["../v1/user", "voice/../x", "a b", "", "x" * 65, "v?x=1"])
async def test_voice_ids_cannot_escape_the_url_path(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter, voice_id: str
) -> None:
    route = respx_mock.route().respond(200)
    with pytest.raises(ProviderRejectedError):
        await provider.synthesize("Hi.", voice_id)
    assert not route.called


async def test_non_audio_responses_are_rejected(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TTS).respond(200, content=b"<html>", headers={"content-type": "text/html"})
    with pytest.raises(ProviderUnavailableError):
        await provider.synthesize("Hi.", "voice1")


async def test_redirects_are_never_followed(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TTS).respond(302, headers={"location": "https://evil.example.com/steal"})
    evil = respx_mock.route(host="evil.example.com").respond(200)
    with pytest.raises(ProviderUnavailableError):
        await provider.synthesize("Hi.", "voice1")
    assert not evil.called


async def test_voices_are_paginated_filtered_and_cached(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(f"{BASE}/v2/voices").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "voices": [
                        {
                            "voice_id": "v1",
                            "name": "One",
                            "category": "premade",
                            "description": "d",
                        },
                        {"voice_id": "../evil", "name": "Bad"},
                    ],
                    "has_more": True,
                    "next_page_token": "tok",
                },
            ),
            httpx.Response(
                200, json={"voices": [{"voice_id": "v2", "name": "Two"}], "has_more": False}
            ),
        ]
    )
    voices = await provider.list_voices()
    assert [v.voice_id for v in voices] == ["v1", "v2"]
    assert route.calls[1].request.url.params["next_page_token"] == "tok"
    assert route.calls[0].request.url.params["page_size"] == "100"

    assert await provider.list_voices() == voices
    assert route.call_count == 2  # second call served from cache


async def test_the_key_is_only_ever_sent_to_the_configured_host(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TTS).respond(200, json=tts_payload(b"x"))
    await provider.synthesize("Hi.", "voice1")
    assert {c.request.url.host for c in respx_mock.calls} == {"api.elevenlabs.io"}


# ------------------------------------------------------------ timestamps + settings
async def test_alignment_is_returned_with_the_audio(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TTS).respond(200, json=tts_payload(b"MP3", "Hi there."))
    clip = await provider.synthesize("Hi there.", "voice1")
    assert clip.data == b"MP3"
    assert clip.alignment is not None
    assert "".join(clip.alignment.characters) == "Hi there."
    assert clip.alignment.ends[-1] == pytest.approx(0.9)


@pytest.mark.parametrize(
    "alignment",
    [
        {"characters": ["H"], "character_start_times_seconds": [0.0]},  # missing array
        {"characters": ["H", "i"], "character_start_times_seconds": [0, 1],
         "character_end_times_seconds": [1]},  # length mismatch
        {"characters": list("Bye"), "character_start_times_seconds": [0, 1, 2],
         "character_end_times_seconds": [1, 2, 3]},  # does not match the request text
        "not an object",
    ],
)  # fmt: skip
async def test_a_bad_alignment_degrades_to_none_instead_of_failing(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter, alignment: object
) -> None:
    respx_mock.post(TTS).respond(
        200, json={"audio_base64": base64.b64encode(b"MP3").decode(), "alignment": alignment}
    )
    clip = await provider.synthesize("Hi", "voice1")
    assert clip.data == b"MP3"
    assert clip.alignment is None


@pytest.mark.parametrize(
    "body", [{"audio_base64": "!!not base64!!"}, {"no_audio": True}, {"audio_base64": ""}]
)
async def test_malformed_timestamps_responses_are_provider_errors(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter, body: dict[str, Any]
) -> None:
    respx_mock.post(TTS).respond(200, json=body)
    with pytest.raises(ProviderUnavailableError):
        await provider.synthesize("Hi.", "voice1")


async def test_model_and_voice_settings_are_sent_when_given(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(TTS).respond(200, json=tts_payload(b"x"))
    await provider.synthesize(
        "Hi.",
        "voice1",
        model_id="eleven_flash_v2_5",
        voice_settings=VoiceSettings(stability=0.3, speed=1.1),
    )
    body = json.loads(route.calls.last.request.content)
    assert body["model_id"] == "eleven_flash_v2_5"
    assert body["voice_settings"] == {"stability": 0.3, "speed": 1.1}


async def test_default_voice_settings_are_not_sent(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(TTS).respond(200, json=tts_payload(b"x"))
    await provider.synthesize("Hi.", "voice1", voice_settings=VoiceSettings())
    assert "voice_settings" not in json.loads(route.calls.last.request.content)


# ---------------------------------------------------------------------- models
async def test_models_are_filtered_validated_and_cached(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(f"{BASE}/v1/models").respond(
        200,
        json=[
            {
                "model_id": "eleven_multilingual_v2",
                "name": "Multilingual v2",
                "can_do_text_to_speech": True,
                "can_use_style": True,
                "model_rates": {"character_cost_multiplier": 1.0},
            },
            {
                "model_id": "eleven_flash_v2_5",
                "name": "Flash v2.5",
                "can_do_text_to_speech": True,
                "model_rates": {"character_cost_multiplier": 0.5},
            },
            {"model_id": "eleven_sts", "can_do_text_to_speech": False},
            {"model_id": "../evil", "can_do_text_to_speech": True},
            {
                "model_id": "weird_rates",
                "can_do_text_to_speech": True,
                "model_rates": {"character_cost_multiplier": "lots"},
            },
        ],
    )
    models = await provider.list_models()
    assert [m.model_id for m in models] == [
        "eleven_multilingual_v2",
        "eleven_flash_v2_5",
        "weird_rates",
    ]
    assert models[0].supports_style and not models[1].supports_style
    assert models[1].cost_multiplier == 0.5
    assert models[2].cost_multiplier == 1.0  # nonsense rates fall back to full price

    assert await provider.list_models() == models
    assert route.call_count == 1


# --------------------------------------------------------------------- previews
PREVIEW_URL = "https://storage.googleapis.com/eleven-public-prod/voice1/preview.mp3"


def voices_with_previews(*items: dict[str, Any]) -> dict[str, Any]:
    return {"voices": list(items), "has_more": False}


async def test_previews_are_fetched_without_the_api_key_and_cached(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{BASE}/v2/voices").respond(
        200,
        json=voices_with_previews(
            {
                "voice_id": "voice1",
                "name": "One",
                "preview_url": PREVIEW_URL,
                "labels": {"accent": "british", "gender": "female", "nested": {"x": 1}},
            }
        ),
    )
    # Real-world quirk: the CDN serves previews as text/plain.
    cdn = respx_mock.get(PREVIEW_URL).respond(
        200, content=b"ID3PREVIEW", headers={"content-type": "text/plain"}
    )

    voices = await provider.list_voices()
    assert voices[0].has_preview
    assert dict(voices[0].labels) == {"accent": "british", "gender": "female"}

    preview = await provider.voice_preview("voice1")
    assert preview is not None and preview.data == b"ID3PREVIEW"
    assert preview.content_type == "audio/mpeg"
    assert "xi-api-key" not in cdn.calls.last.request.headers

    assert await provider.voice_preview("voice1") == preview
    assert cdn.call_count == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/preview.mp3",
        "http://storage.googleapis.com/insecure.mp3",
        "https://user:pw@storage.googleapis.com/x.mp3",
        "https://storage.googleapis.com:8443/x.mp3",
        "file:///etc/passwd",
    ],
)
async def test_previews_from_unexpected_urls_are_never_fetched(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter, url: str
) -> None:
    respx_mock.get(f"{BASE}/v2/voices").respond(
        200, json=voices_with_previews({"voice_id": "voice1", "name": "One", "preview_url": url})
    )
    anything_else = respx_mock.route(host__regex=r"^(?!api\.elevenlabs\.io$)").respond(200)
    voices = await provider.list_voices()
    assert not voices[0].has_preview
    assert await provider.voice_preview("voice1") is None
    assert not anything_else.called


async def test_preview_redirects_and_non_audio_bytes_are_rejected(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{BASE}/v2/voices").respond(
        200,
        json=voices_with_previews(
            {"voice_id": "voice1", "name": "One", "preview_url": PREVIEW_URL}
        ),
    )
    respx_mock.get(PREVIEW_URL).mock(
        side_effect=[
            httpx.Response(302, headers={"location": "https://evil.example.com/"}),
            httpx.Response(200, content=b"<html>", headers={"content-type": "audio/mpeg"}),
        ]
    )
    evil = respx_mock.route(host="evil.example.com").respond(200)
    with pytest.raises(ProviderUnavailableError):
        await provider.voice_preview("voice1")
    with pytest.raises(ProviderUnavailableError):
        await provider.voice_preview("voice1")
    assert not evil.called


async def test_preview_for_an_unknown_voice_is_none(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{BASE}/v2/voices").respond(200, json=voices_with_previews())
    assert await provider.voice_preview("nobody") is None


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"ID3\x04rest", "audio/mpeg"),
        (b"\xff\xfbframe", "audio/mpeg"),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav"),
        (b"OggS\x00", "audio/ogg"),
        (b"<script>", None),
        (b"", None),
    ],
)
def test_preview_audio_is_identified_by_its_bytes(data: bytes, expected: str | None) -> None:
    assert _sniff_audio(data) == expected
