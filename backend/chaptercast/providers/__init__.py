"""Speech providers."""

from __future__ import annotations

from chaptercast.config import Settings
from chaptercast.providers.base import TTSProvider
from chaptercast.providers.demo import DemoProvider
from chaptercast.providers.elevenlabs import ElevenLabsProvider


def build_provider(settings: Settings) -> TTSProvider:
    if settings.resolved_provider == "elevenlabs":
        assert settings.elevenlabs_api_key is not None  # guaranteed by Settings validation
        return ElevenLabsProvider(
            settings.elevenlabs_api_key,
            base_url=settings.elevenlabs_base_url,
            model_id=settings.elevenlabs_model_id,
            output_format=settings.elevenlabs_output_format,
            timeout=settings.tts_timeout_seconds,
            max_retries=settings.tts_max_retries,
        )
    return DemoProvider()
