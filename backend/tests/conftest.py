from __future__ import annotations

import asyncio
import io
import os
import time
import wave
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chaptercast.audio import WAV
from chaptercast.config import Settings
from chaptercast.main import create_app
from chaptercast.providers.base import (
    Alignment,
    AudioClip,
    Model,
    Preview,
    TTSProvider,
    Voice,
    VoiceSettings,
)

# Built at runtime so the repository never contains a key-shaped literal.
SENTINEL_KEY = "sk_" + "a1b2c3d4e5f6" * 4
ACCESS_TOKEN = "unit-test-access-token-0123456789"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never pick up a developer's real key or config."""
    keep = {"CHAPTERCAST_UPDATE_SNAPSHOT"}  # test tooling flag, not app configuration
    for name in list(os.environ):
        if name in keep:
            continue
        if name.startswith("CHAPTERCAST_") or name == "ELEVENLABS_API_KEY":
            monkeypatch.delenv(name, raising=False)


def make_wav(value: int = 0, frames: int = 10, rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(value.to_bytes(2, "little", signed=True) * frames)
    return buffer.getvalue()


def read_wav_values(data: bytes) -> list[int]:
    with wave.open(io.BytesIO(data), "rb") as wav:
        raw = wav.readframes(wav.getnframes())
    return [int.from_bytes(raw[i : i + 2], "little", signed=True) for i in range(0, len(raw), 2)]


FAKE_CLIP_FRAMES = 10
FAKE_CLIP_RATE = 8000
FAKE_CLIP_SECONDS = FAKE_CLIP_FRAMES / FAKE_CLIP_RATE


def even_alignment(text: str, duration: float) -> Alignment:
    step = duration / max(1, len(text))
    return Alignment(
        tuple(text),
        tuple(i * step for i in range(len(text))),
        tuple((i + 1) * step for i in range(len(text))),
    )


class FakeProvider:
    """Deterministic provider. Each clip's samples encode the first character of its text."""

    name = "fake"
    output_format = WAV

    def __init__(
        self,
        *,
        delay: float | Callable[[str], float] = 0.0,
        fail_when: Callable[[str], Exception | None] | None = None,
        with_alignment: bool = True,
    ) -> None:
        self.delay = delay
        self.fail_when = fail_when
        self.with_alignment = with_alignment
        self.calls: list[dict[str, Any]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def list_voices(self) -> list[Voice]:
        return [
            Voice("voice1", "Voice One", "premade", "A test voice", {"accent": "british"}, True),
            Voice("voice2", "Voice Two", "premade", None),
        ]

    async def list_models(self) -> list[Model]:
        return [
            Model("model1", "Model One", "Full price", 1.0, True),
            Model("cheap", "Cheap Model", "Half price", 0.5, False),
        ]

    async def voice_preview(self, voice_id: str) -> Preview | None:
        return Preview(make_wav(7), "audio/wav") if voice_id == "voice1" else None

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
        self.calls.append(
            {
                "text": text,
                "voice_id": voice_id,
                "model_id": model_id,
                "voice_settings": voice_settings,
                "previous": previous_text,
                "next": next_text,
            }
        )
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            delay = self.delay(text) if callable(self.delay) else self.delay
            if delay:
                await asyncio.sleep(delay)
            if self.fail_when is not None and (error := self.fail_when(text)) is not None:
                raise error
            data = make_wav(ord(text[0]) % 32000, FAKE_CLIP_FRAMES, FAKE_CLIP_RATE)
            alignment = even_alignment(text, FAKE_CLIP_SECONDS) if self.with_alignment else None
            return AudioClip(data, WAV, alignment)
        finally:
            self.in_flight -= 1

    async def aclose(self) -> None:
        return None


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path / "data", **overrides)


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def factory(fake_provider: TTSProvider | None = None, /, **overrides: Any) -> TestClient:
        app = create_app(make_settings(tmp_path, **overrides), fake_provider or FakeProvider())
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def wait_for_job(
    client: TestClient,
    job_id: str,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body: dict[str, Any] = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        if body["status"] in {"succeeded", "failed", "cancelled"}:
            return body
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")
