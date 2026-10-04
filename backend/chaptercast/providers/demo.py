"""Demo provider: no network, no API key, no cost.

It renders each word as a short sine blip (pitch derived from the word and the
selected voice) into a WAV file. The audio is obviously not speech; the point is
that the entire pipeline (chunking, concurrency, caching, progress, stitching,
read-along timings, captions, serving, the UI) can be exercised by anyone who
clones the repo. Because the demo knows exactly when each blip plays, its
character alignment is exact, which makes it a good oracle for the transcript code.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import re
import struct
import wave

from chaptercast.audio import WAV
from chaptercast.providers.base import (
    Alignment,
    AudioClip,
    Model,
    Preview,
    ProviderRejectedError,
    Voice,
    VoiceSettings,
    is_valid_voice_id,
)

_SAMPLE_RATE = 22_050
_MAX_WORDS_PER_CLIP = 2_000
_PENTATONIC = (0, 2, 4, 7, 9)
_WORD_GAP_SECONDS = 0.04
_TOKEN_RE = re.compile(r"\S+|\s+")
_PREVIEW_TEXT = "Hello. This is a short preview of my voice."

_VOICES = (
    Voice("demo-aria", "Aria (demo)", "demo", "Low register tones", {"pitch": "low pitch"}, True),
    Voice("demo-orion", "Orion (demo)", "demo", "Mid register tones", {"pitch": "mid pitch"}, True),
    Voice("demo-lyra", "Lyra (demo)", "demo", "High register tones", {"pitch": "high pitch"}, True),
)
_BASE_HZ = {"demo-aria": 196.0, "demo-orion": 261.63, "demo-lyra": 392.0}

_MODELS = (
    Model("demo_standard", "Demo Standard", "Synthetic tones at full price.", 1.0, True),
    Model("demo_fast", "Demo Fast", "Synthetic tones at half price.", 0.5, False),
)


class DemoProvider:
    name = "demo"
    output_format = WAV

    def __init__(self, latency_seconds: float = 0.25) -> None:
        self._latency = latency_seconds

    async def list_voices(self) -> list[Voice]:
        return list(_VOICES)

    async def list_models(self) -> list[Model]:
        return list(_MODELS)

    async def voice_preview(self, voice_id: str) -> Preview | None:
        if voice_id not in _BASE_HZ:
            return None
        data, _ = await asyncio.to_thread(self._render, _PREVIEW_TEXT, voice_id, 1.0)
        return Preview(data, WAV.content_type)

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
        if not is_valid_voice_id(voice_id) or voice_id not in _BASE_HZ:
            raise ProviderRejectedError("unknown demo voice")
        if model_id is not None and model_id not in {m.model_id for m in _MODELS}:
            raise ProviderRejectedError("unknown demo model")
        if self._latency:
            await asyncio.sleep(self._latency)
        speed = voice_settings.speed if voice_settings and voice_settings.speed else 1.0
        data, alignment = await asyncio.to_thread(self._render, text, voice_id, speed)
        return AudioClip(data, WAV, alignment)

    async def aclose(self) -> None:
        return None

    @staticmethod
    def _render(text: str, voice_id: str, speed: float) -> tuple[bytes, Alignment]:
        base = _BASE_HZ[voice_id]
        samples = bytearray()
        chars: list[str] = []
        starts: list[float] = []
        ends: list[float] = []
        words = 0

        def now() -> float:
            return len(samples) / 2 / _SAMPLE_RATE

        for token in _TOKEN_RE.findall(text):
            start = now()
            if token.isspace():
                if words:
                    samples += b"\x00\x00" * int(_SAMPLE_RATE * _WORD_GAP_SECONDS / speed)
            elif words < _MAX_WORDS_PER_CLIP:  # beyond the cap: zero-length, still aligned
                words += 1
                digest = hashlib.sha256(token.lower().encode()).digest()
                hz = base * 2 ** (_PENTATONIC[digest[0] % len(_PENTATONIC)] / 12)
                count = int(_SAMPLE_RATE * min(0.35, 0.09 + 0.02 * len(token)) / speed)
                for n in range(count):
                    envelope = math.sin(math.pi * n / count) ** 0.5  # fade in/out: no clicks
                    value = int(9000 * envelope * math.sin(2 * math.pi * hz * n / _SAMPLE_RATE))
                    samples += struct.pack("<h", value)
            # Spread the token's time evenly over its characters.
            end = now()
            step = (end - start) / len(token)
            for i, ch in enumerate(token):
                chars.append(ch)
                starts.append(round(start + i * step, 4))
                ends.append(round(start + (i + 1) * step, 4))

        if not samples:
            samples += b"\x00\x00" * int(_SAMPLE_RATE * 0.1)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(_SAMPLE_RATE)
            wav.writeframes(bytes(samples))
        return buffer.getvalue(), Alignment(tuple(chars), tuple(starts), tuple(ends))
