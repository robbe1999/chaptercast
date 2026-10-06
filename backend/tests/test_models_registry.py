"""The model registry and its startup validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chaptercast.models import DEMO_MODELS, ELEVENLABS_MODELS
from tests.conftest import make_settings


def test_v4_entries_match_what_was_verified_on_the_live_api() -> None:
    """Values from GET /v1/models on 2026-10-06 (docs/MODELS.md)."""
    v4, turbo = ELEVENLABS_MODELS["eleven_v4"], ELEVENLABS_MODELS["eleven_v4_turbo"]
    assert (v4.cost_multiplier, turbo.cost_multiplier) == (1.0, 0.5)
    for spec in (v4, turbo):
        assert spec.max_chars_per_request == 10_000
        assert spec.supports_timestamps and spec.supports_context_stitching
        assert spec.supports_audio_tags and not spec.ssml_breaks
        assert not spec.supports_style and not spec.supports_speaker_boost
    assert (v4.latency_class, turbo.latency_class) == ("standard", "low")


def test_older_models_keep_their_behaviour() -> None:
    v2 = ELEVENLABS_MODELS["eleven_multilingual_v2"]
    assert v2.supports_style and v2.supports_speaker_boost and not v2.supports_audio_tags
    for model_id in ("eleven_flash_v2_5", "eleven_turbo_v2_5"):
        spec = ELEVENLABS_MODELS[model_id]
        assert spec.cost_multiplier == 0.5 and not spec.supports_audio_tags


def test_registries_are_consistent() -> None:
    for registry in (ELEVENLABS_MODELS, DEMO_MODELS):
        for model_id, spec in registry.items():
            assert spec.model_id == model_id
            assert 0 < spec.cost_multiplier <= 1
            # ChapterCast's own chunk size (at most 4,500) always fits in one request.
            assert spec.max_chars_per_request >= 4_500
            assert spec.label and spec.description
    assert not set(ELEVENLABS_MODELS) & set(DEMO_MODELS)
    assert any(s.supports_audio_tags for s in DEMO_MODELS.values())  # demo mode shows tags


def test_the_default_allowlist_is_the_five_registered_models(tmp_path: Path) -> None:
    assert make_settings(tmp_path).allowed_model_ids == [
        "eleven_multilingual_v2",
        "eleven_flash_v2_5",
        "eleven_turbo_v2_5",
        "eleven_v4",
        "eleven_v4_turbo",
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"allowed_models": "eleven_v4,eleven_v99"},
        {"allowed_models": "eleven_v3"},  # exists upstream, but is not registered
        {"elevenlabs_model_id": "eleven_unknown"},
    ],
)
def test_unregistered_models_fail_at_startup(tmp_path: Path, overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match=r"Register them in chaptercast/models\.py"):
        make_settings(tmp_path, **overrides)
