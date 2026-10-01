"""Demo provider: no network, no API key, no cost.

It renders each word as a short sine blip (pitch derived from the word and the
selected voice) into a WAV file. The audio is obviously not speech; the point is
that the entire pipeline (chunking, concurrency, progress, stitching, serving,
the UI) can be exercised by anyone who clones the repo.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import struct
import wave

from chaptercast.audio import WAV
from chaptercast.providers.base import AudioClip, ProviderRejectedError, Voice, is_valid_voice_id

_SAMPLE_RATE = 22_050
_MAX_WORDS_PER_CLIP = 2_000
_PENTATONIC = (0, 2, 4, 7, 9)

_VOICES = (
    Voice("demo-aria", "Aria (demo)", "demo", "Low register tones"),
    Voice("demo-orion", "Orion (demo)", "demo", "Mid register tones"),
    Voice("demo-lyra", "Lyra (demo)", "demo", "High register tones"),
)
_BASE_HZ = {"demo-aria": 196.0, "demo-orion": 261.63, "demo-lyra": 392.0}


class DemoProvider:
    name = "demo"
    output_format = WAV

    def __init__(self, latency_seconds: float = 0.25) -> None:
        self._latency = latency_seconds

    async def list_voices(self) -> list[Voice]:
        return list(_VOICES)

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        *,
        previous_text: str | None = None,
        next_text: str | None = None,
    ) -> AudioClip:
        if not is_valid_voice_id(voice_id) or voice_id not in _BASE_HZ:
            raise ProviderRejectedError("unknown demo voice")
        if self._latency:
            await asyncio.sleep(self._latency)
        return AudioClip(await asyncio.to_thread(self._render, text, voice_id), WAV)

    async def aclose(self) -> None:
        return None

    @staticmethod
    def _render(text: str, voice_id: str) -> bytes:
        base = _BASE_HZ[voice_id]
        samples = bytearray()
        for word in text.split()[:_MAX_WORDS_PER_CLIP]:
            digest = hashlib.sha256(word.lower().encode()).digest()
            hz = base * 2 ** (_PENTATONIC[digest[0] % len(_PENTATONIC)] / 12)
            blip = min(0.35, 0.09 + 0.02 * len(word))
            count = int(_SAMPLE_RATE * blip)
            for n in range(count):
                envelope = math.sin(math.pi * n / count) ** 0.5  # fade in/out: no clicks
                value = int(9000 * envelope * math.sin(2 * math.pi * hz * n / _SAMPLE_RATE))
                samples += struct.pack("<h", value)
            samples += b"\x00\x00" * int(_SAMPLE_RATE * 0.04)  # gap between words
        if not samples:
            samples += b"\x00\x00" * int(_SAMPLE_RATE * 0.1)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(_SAMPLE_RATE)
            wav.writeframes(bytes(samples))
        return buffer.getvalue()
