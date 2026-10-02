"""Models, estimates, voice previews, the cache and read-along/caption endpoints."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chaptercast.providers.base import Model, ProviderAuthError
from tests.conftest import SENTINEL_KEY, FakeProvider, wait_for_job

ClientFactory = Callable[..., TestClient]
TEXT = "The lighthouse stood alone. Its beam swept the water. Ships steered by it."
JOB: dict[str, Any] = {"text": TEXT, "voice_id": "voice1"}


def finished(client: TestClient, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    created = client.post("/api/jobs", json=payload or JOB)
    assert created.status_code == 202, created.text
    return wait_for_job(client, created.json()["id"])


class ElevenLikeProvider(FakeProvider):
    """A fake that advertises real ElevenLabs model ids, to exercise the allowlist."""

    async def list_models(self) -> list[Model]:
        return [
            Model("eleven_v3", "Eleven v3", None, 1.0, False),
            Model("eleven_flash_v2_5", "Flash v2.5", None, 0.5, False),
            Model("eleven_multilingual_v2", "Multilingual v2", None, 1.0, True),
        ]


# ------------------------------------------------------------------------ models
def test_models_list_the_providers_models_with_costs(make_client: ClientFactory) -> None:
    body = make_client().get("/api/models").json()
    assert body["default_model_id"] == "model1"
    assert [(m["model_id"], m["cost_multiplier"], m["supports_style"]) for m in body["models"]] == [
        ("model1", 1.0, True),
        ("cheap", 0.5, False),
    ]


def test_elevenlabs_models_are_limited_to_the_allowlist(make_client: ClientFactory) -> None:
    client = make_client(
        ElevenLikeProvider(),
        elevenlabs_api_key=SENTINEL_KEY,
        allowed_models="eleven_flash_v2_5",
    )
    body = client.get("/api/models").json()
    # The default model is always allowed and listed first; eleven_v3 is not allowlisted.
    assert [m["model_id"] for m in body["models"]] == [
        "eleven_multilingual_v2",
        "eleven_flash_v2_5",
    ]
    assert body["default_model_id"] == "eleven_multilingual_v2"

    rejected = client.post("/api/jobs", json={**JOB, "model_id": "eleven_v3"})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "unknown_model"


def test_model_list_failures_map_to_gateway_errors(make_client: ClientFactory) -> None:
    class Broken(FakeProvider):
        async def list_models(self) -> list[Model]:
            raise ProviderAuthError("bad key")

    response = make_client(Broken()).get("/api/models")
    assert response.status_code == 502
    assert "bad key" not in response.text


def test_no_models_is_a_clear_503(make_client: ClientFactory) -> None:
    class Empty(FakeProvider):
        async def list_models(self) -> list[Model]:
            return []

    response = make_client(Empty()).post("/api/estimate", json=JOB)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_models"


# ------------------------------------------------------------- jobs with options
def test_jobs_carry_model_and_settings_to_the_provider(make_client: ClientFactory) -> None:
    provider = FakeProvider()
    client = make_client(provider)
    done = finished(
        client,
        {
            **JOB,
            "model_id": "cheap",
            "voice_settings": {"stability": 0.3, "style": 0.8, "speed": 1.1},
        },
    )
    assert done["model_id"] == "cheap"
    settings = {c["voice_settings"] for c in provider.calls}
    assert len(settings) == 1
    (only,) = settings
    assert (only.stability, only.speed) == (0.3, 1.1)
    assert only.style is None  # "cheap" has no style support, so it is dropped


@pytest.mark.parametrize(
    "voice_settings",
    [{"speed": 2.0}, {"stability": -0.1}, {"similarity_boost": 1.5}, {"unknown": 1}],
)
def test_out_of_range_voice_settings_are_rejected(
    make_client: ClientFactory, voice_settings: dict[str, Any]
) -> None:
    response = make_client().post("/api/jobs", json={**JOB, "voice_settings": voice_settings})
    assert response.status_code == 422


def test_job_responses_report_cache_use_and_billing(make_client: ClientFactory) -> None:
    client = make_client()
    first = finished(client)
    assert first["cached_chunks"] == 0
    assert first["billed_characters"] == first["char_count"]

    second = finished(client)
    assert second["cached_chunks"] == second["progress"]["total_chunks"]
    assert second["billed_characters"] == 0


def test_the_cache_can_be_disabled(make_client: ClientFactory) -> None:
    client = make_client(cache_max_mb=0)
    assert client.get("/api/config").json()["cache_enabled"] is False
    finished(client)
    assert finished(client)["cached_chunks"] == 0


# ---------------------------------------------------------------------- estimate
def test_estimate_is_a_dry_run_that_sees_the_cache(make_client: ClientFactory) -> None:
    provider = FakeProvider()
    client = make_client(provider, daily_char_budget=10_000)
    cold = client.post("/api/estimate", json=JOB).json()
    assert provider.calls == []
    assert cold["characters"] == len(TEXT)
    assert cold["cached_chunks"] == 0
    assert cold["billable_characters"] == len(TEXT)
    assert cold["estimated_credits"] == len(TEXT)
    assert cold["within_limit"] is True
    assert cold["daily_budget_remaining"] == 10_000

    finished(client)
    warm = client.post("/api/estimate", json=JOB).json()
    assert warm["cached_chunks"] == warm["chunks"]
    assert warm["billable_characters"] == 0
    assert warm["estimated_credits"] == 0
    assert warm["daily_budget_remaining"] == 10_000 - len(TEXT)


def test_estimate_applies_the_model_cost_multiplier(make_client: ClientFactory) -> None:
    body = make_client().post("/api/estimate", json={**JOB, "model_id": "cheap"}).json()
    assert body["cost_multiplier"] == 0.5
    assert body["estimated_credits"] == -(-len(TEXT) // 2)  # rounded up


def test_estimate_flags_text_over_the_limit_and_handles_empty_text(
    make_client: ClientFactory,
) -> None:
    client = make_client(max_chars_per_job=20)
    over = client.post("/api/estimate", json=JOB).json()
    assert over["within_limit"] is False
    empty = client.post("/api/estimate", json={**JOB, "text": "   "}).json()
    assert (empty["characters"], empty["chunks"], empty["estimated_credits"]) == (0, 0, 0)


def test_estimates_are_rate_limited(make_client: ClientFactory) -> None:
    client = make_client()
    statuses = [client.post("/api/estimate", json=JOB).status_code for _ in range(121)]
    assert statuses[:120] == [200] * 120
    assert statuses[120] == 429


# ---------------------------------------------------------------------- previews
def test_voice_previews_are_served_and_cacheable(make_client: ClientFactory) -> None:
    client = make_client()
    response = client.get("/api/voices/voice1/preview")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "private, max-age=86400"
    assert response.content[:4] == b"RIFF"


@pytest.mark.parametrize("voice_id", ["voice2", "nobody", "a.b", "x" * 65])
def test_missing_or_malformed_previews_are_404(make_client: ClientFactory, voice_id: str) -> None:
    assert make_client().get(f"/api/voices/{voice_id}/preview").status_code == 404


# ------------------------------------------------------- transcript and captions
def test_finished_jobs_link_their_transcript_and_captions(make_client: ClientFactory) -> None:
    client = make_client()
    done = finished(client)
    job_id = done["id"]
    assert done["transcript_url"] == f"/api/jobs/{job_id}/transcript"
    assert done["captions"] == {
        "srt": f"/api/jobs/{job_id}/captions.srt",
        "vtt": f"/api/jobs/{job_id}/captions.vtt",
    }

    transcript = client.get(done["transcript_url"]).json()
    assert [w["text"] for w in transcript["words"]] == TEXT.split()
    assert transcript["duration_seconds"] == done["duration_seconds"]
    assert all(w["end"] >= w["start"] for w in transcript["words"])

    srt = client.get(done["captions"]["srt"])
    assert srt.headers["content-type"].startswith("application/x-subrip")
    assert (
        srt.headers["content-disposition"] == f'attachment; filename="chaptercast-{job_id[:8]}.srt"'
    )
    assert srt.text.startswith("1\n00:00:00,000 --> ")
    assert "The lighthouse stood alone." in srt.text

    vtt = client.get(done["captions"]["vtt"])
    assert vtt.headers["content-type"].startswith("text/vtt")
    assert vtt.text.startswith("WEBVTT\n")


def test_transcript_endpoints_wait_for_the_job(make_client: ClientFactory) -> None:
    client = make_client(FakeProvider(delay=0.5))
    job_id = client.post("/api/jobs", json=JOB).json()["id"]
    for path in ("transcript", "captions.vtt"):
        response = client.get(f"/api/jobs/{job_id}/{path}")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "not_ready"
    client.delete(f"/api/jobs/{job_id}")


def test_audio_without_timings_has_no_transcript(make_client: ClientFactory) -> None:
    client = make_client(FakeProvider(with_alignment=False))
    done = finished(client)
    assert done["status"] == "succeeded"
    assert done["transcript_url"] is None
    assert done["captions"] is None
    response = client.get(f"/api/jobs/{done['id']}/transcript")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "no_transcript"


def test_unknown_caption_formats_are_404(make_client: ClientFactory) -> None:
    client = make_client()
    done = finished(client)
    assert client.get(f"/api/jobs/{done['id']}/captions.txt").status_code == 404
    assert client.get(f"/api/jobs/{'0' * 32}/captions.srt").status_code == 404
