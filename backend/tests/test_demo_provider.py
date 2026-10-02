from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from chaptercast.audio import WAV, stitch
from chaptercast.providers import build_provider
from chaptercast.providers.base import ProviderRejectedError, VoiceSettings
from chaptercast.providers.demo import DemoProvider
from chaptercast.providers.elevenlabs import ElevenLabsProvider
from tests.conftest import SENTINEL_KEY, make_settings


def frames(data: bytes) -> int:
    with wave.open(io.BytesIO(data), "rb") as wav:
        return wav.getnframes()


async def test_demo_provider_lists_voices() -> None:
    voices = await DemoProvider(latency_seconds=0).list_voices()
    assert [v.voice_id for v in voices] == ["demo-aria", "demo-orion", "demo-lyra"]


async def test_demo_audio_is_valid_wav_and_scales_with_text_length() -> None:
    provider = DemoProvider(latency_seconds=0)
    short = await provider.synthesize("One two.", "demo-aria")
    long = await provider.synthesize("One two three four five six seven eight.", "demo-aria")
    assert short.fmt == WAV
    assert short.data[:4] == b"RIFF"
    assert frames(long.data) > frames(short.data) > 0


async def test_demo_audio_is_deterministic_and_voice_dependent() -> None:
    provider = DemoProvider(latency_seconds=0)
    a1 = await provider.synthesize("Same words here.", "demo-aria")
    a2 = await provider.synthesize("Same words here.", "demo-aria")
    b = await provider.synthesize("Same words here.", "demo-lyra")
    assert a1.data == a2.data
    assert a1.data != b.data


async def test_demo_clips_can_be_stitched() -> None:
    provider = DemoProvider(latency_seconds=0)
    clips = [
        await provider.synthesize(t, "demo-orion") for t in ("Hello there.", "General Kenobi.")
    ]
    joined = await stitch([c.data for c in clips], WAV)
    assert frames(joined) == sum(frames(c.data) for c in clips)


async def test_demo_handles_empty_text() -> None:
    clip = await DemoProvider(latency_seconds=0).synthesize("", "demo-aria")
    assert frames(clip.data) > 0


@pytest.mark.parametrize("voice_id", ["nope", "../x", "demo-aria/../x", ""])
async def test_demo_rejects_unknown_voices(voice_id: str) -> None:
    with pytest.raises(ProviderRejectedError):
        await DemoProvider(latency_seconds=0).synthesize("Hi.", voice_id)


async def test_build_provider_defaults_to_demo(tmp_path: Path) -> None:
    provider = build_provider(make_settings(tmp_path))
    assert isinstance(provider, DemoProvider)
    await provider.aclose()


async def test_build_provider_uses_elevenlabs_when_a_key_is_set(tmp_path: Path) -> None:
    provider = build_provider(make_settings(tmp_path, elevenlabs_api_key=SENTINEL_KEY))
    assert isinstance(provider, ElevenLabsProvider)
    assert provider.output_format.bitrate_kbps == 128
    await provider.aclose()


async def test_demo_can_be_forced_even_when_a_key_is_present(tmp_path: Path) -> None:
    provider = build_provider(
        make_settings(tmp_path, provider="demo", elevenlabs_api_key=SENTINEL_KEY)
    )
    assert isinstance(provider, DemoProvider)


def duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


async def test_demo_alignment_is_exact_and_matches_the_text() -> None:
    text = "One two,  three.\n\nFour"
    clip = await DemoProvider(latency_seconds=0).synthesize(text, "demo-aria")
    assert clip.alignment is not None
    assert "".join(clip.alignment.characters) == text
    assert list(clip.alignment.starts) == sorted(clip.alignment.starts)
    assert clip.alignment.ends[-1] == pytest.approx(duration(clip.data), abs=1e-3)


async def test_demo_speed_setting_changes_the_tempo() -> None:
    provider = DemoProvider(latency_seconds=0)
    normal = await provider.synthesize("Some words to say.", "demo-aria")
    fast = await provider.synthesize(
        "Some words to say.", "demo-aria", voice_settings=VoiceSettings(speed=1.2)
    )
    assert duration(fast.data) < duration(normal.data)


async def test_demo_models_and_previews() -> None:
    provider = DemoProvider(latency_seconds=0)
    models = await provider.list_models()
    assert {m.model_id: m.cost_multiplier for m in models} == {
        "demo_standard": 1.0,
        "demo_fast": 0.5,
    }
    preview = await provider.voice_preview("demo-lyra")
    assert preview is not None and preview.data[:4] == b"RIFF"
    assert preview.content_type == "audio/wav"
    assert await provider.voice_preview("nobody") is None
    assert all(v.has_preview for v in await provider.list_voices())


async def test_demo_rejects_unknown_models() -> None:
    with pytest.raises(ProviderRejectedError):
        await DemoProvider(latency_seconds=0).synthesize("Hi.", "demo-aria", model_id="nope")
