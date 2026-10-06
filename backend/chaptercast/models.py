"""What each speech model can do, as data.

Every capability decision in the pipeline (send context or not, ask for
timestamps or not, which voice settings to keep, whether expression tags are
sent or removed, what a character costs) reads one of these records instead of
checking model ids in scattered conditionals.

The ElevenLabs values were checked against the live API; docs/MODELS.md lists
what was measured and what was not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

LatencyClass = Literal["standard", "low"]


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    label: str
    description: str
    # Credits per character (`model_rates.character_cost_multiplier` in /v1/models).
    cost_multiplier: float
    max_chars_per_request: int
    supports_timestamps: bool  # /with-timestamps returns a usable alignment
    supports_context_stitching: bool  # previous_text / next_text
    supports_style: bool
    supports_speaker_boost: bool
    supports_audio_tags: bool  # expression tags such as [warm] or [whispered]
    ssml_breaks: bool
    latency_class: LatencyClass = "standard"


ELEVENLABS_MODELS: Mapping[str, ModelSpec] = {
    spec.model_id: spec
    for spec in (
        ModelSpec(
            model_id="eleven_multilingual_v2",
            label="Eleven Multilingual v2",
            description="Stable long-form narration in 29 languages.",
            cost_multiplier=1.0,
            max_chars_per_request=10_000,
            supports_timestamps=True,
            supports_context_stitching=True,
            supports_style=True,
            supports_speaker_boost=True,
            supports_audio_tags=False,
            ssml_breaks=True,
        ),
        ModelSpec(
            model_id="eleven_flash_v2_5",
            label="Eleven Flash v2.5",
            description="Fast and half price, 32 languages.",
            cost_multiplier=0.5,
            max_chars_per_request=40_000,
            supports_timestamps=True,
            supports_context_stitching=True,
            supports_style=False,
            supports_speaker_boost=False,
            supports_audio_tags=False,
            ssml_breaks=True,
            latency_class="low",
        ),
        ModelSpec(
            model_id="eleven_turbo_v2_5",
            label="Eleven Turbo v2.5",
            description="Quality and low latency at half price, 32 languages.",
            cost_multiplier=0.5,
            max_chars_per_request=40_000,
            supports_timestamps=True,
            supports_context_stitching=True,
            supports_style=False,
            supports_speaker_boost=False,
            supports_audio_tags=False,
            ssml_breaks=True,
            latency_class="low",
        ),
        ModelSpec(
            model_id="eleven_v4",
            label="Eleven v4",
            description="Expressive narration with audio tags, 85 languages.",
            cost_multiplier=1.0,
            max_chars_per_request=10_000,
            supports_timestamps=True,
            supports_context_stitching=True,
            supports_style=False,
            supports_speaker_boost=False,
            supports_audio_tags=True,
            ssml_breaks=False,
        ),
        ModelSpec(
            model_id="eleven_v4_turbo",
            label="Eleven v4 Turbo",
            description="v4 with audio tags at low latency and half price.",
            cost_multiplier=0.5,
            max_chars_per_request=10_000,
            supports_timestamps=True,
            supports_context_stitching=True,
            supports_style=False,
            supports_speaker_boost=False,
            supports_audio_tags=True,
            ssml_breaks=False,
            latency_class="low",
        ),
    )
}

# Demo mode mirrors the shapes of the real models so every UI path can be tried for free.
DEMO_MODELS: Mapping[str, ModelSpec] = {
    spec.model_id: spec
    for spec in (
        ModelSpec(
            model_id="demo_standard",
            label="Demo Standard",
            description="Synthetic tones at full price.",
            cost_multiplier=1.0,
            max_chars_per_request=10_000,
            supports_timestamps=True,
            supports_context_stitching=True,
            supports_style=True,
            supports_speaker_boost=True,
            supports_audio_tags=False,
            ssml_breaks=False,
        ),
        ModelSpec(
            model_id="demo_fast",
            label="Demo Fast",
            description="Synthetic tones at half price.",
            cost_multiplier=0.5,
            max_chars_per_request=10_000,
            supports_timestamps=True,
            supports_context_stitching=True,
            supports_style=False,
            supports_speaker_boost=False,
            supports_audio_tags=False,
            ssml_breaks=False,
            latency_class="low",
        ),
        ModelSpec(
            model_id="demo_expressive",
            label="Demo Expressive",
            description="Synthetic tones that understand audio tags.",
            cost_multiplier=1.0,
            max_chars_per_request=10_000,
            supports_timestamps=True,
            supports_context_stitching=True,
            supports_style=False,
            supports_speaker_boost=False,
            supports_audio_tags=True,
            ssml_breaks=False,
        ),
    )
}
