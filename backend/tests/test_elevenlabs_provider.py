from __future__ import annotations

import json
import random

import httpx
import pytest
import respx
from pydantic import SecretStr

from chaptercast.providers.base import (
    ProviderAuthError,
    ProviderQuotaError,
    ProviderRejectedError,
    ProviderUnavailableError,
)
from chaptercast.providers.elevenlabs import ElevenLabsProvider
from tests.conftest import SENTINEL_KEY

BASE = "https://api.elevenlabs.io"
TTS = f"{BASE}/v1/text-to-speech/voice1"
AUDIO = {"content-type": "audio/mpeg"}


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
    route = respx_mock.post(TTS).respond(200, content=b"MP3DATA", headers=AUDIO)
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
    route = respx_mock.post(TTS).respond(200, content=b"x", headers=AUDIO)
    await provider.synthesize("Hi.", "voice1")
    assert set(json.loads(route.calls.last.request.content)) == {"text", "model_id"}


async def test_server_errors_are_retried_then_succeed(
    provider: ElevenLabsProvider, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.post(TTS).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, content=b"ok", headers=AUDIO)]
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
            httpx.Response(200, content=b"ok", headers=AUDIO),
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
            httpx.Response(200, content=b"ok", headers=AUDIO),
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
    respx_mock.post(TTS).respond(200, content=b"x", headers=AUDIO)
    await provider.synthesize("Hi.", "voice1")
    assert {c.request.url.host for c in respx_mock.calls} == {"api.elevenlabs.io"}
