from __future__ import annotations

import os
from pathlib import Path

import pytest

from chaptercast.audio import WAV, mp3_format
from chaptercast.cache import ClipCache, ClipRequest
from chaptercast.providers.base import AudioClip, VoiceSettings
from tests.conftest import even_alignment


def request(**overrides: object) -> ClipRequest:
    fields: dict[str, object] = {
        "provider": "fake",
        "output_format": "wav_0",
        "model_id": "model1",
        "voice_id": "voice1",
        "voice_settings": VoiceSettings(),
        "text": "Hello there.",
        "previous_text": None,
        "next_text": "General Kenobi.",
    }
    return ClipRequest(**{**fields, **overrides})  # type: ignore[arg-type]


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_cache(tmp_path: Path, clock: Clock, max_bytes: int = 1_000_000) -> ClipCache:
    return ClipCache(tmp_path / "cache", max_bytes=max_bytes, ttl_seconds=100, clock=clock)


def put_at(cache: ClipCache, clock: Clock, key: str, clip: AudioClip) -> None:
    """Write an entry whose mtime matches the fake clock (the TTL is judged by mtime)."""
    cache.put(key, clip)
    for path in cache.root.glob(f"{key}.*"):
        os.utime(path, (clock.now, clock.now))


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "other"},
        {"output_format": "mp3_128"},
        {"model_id": "model2"},
        {"voice_id": "voice2"},
        {"voice_settings": VoiceSettings(stability=0.5)},
        {"text": "Hello there!"},
        {"previous_text": "Before."},
        {"next_text": None},
    ],
)
def test_every_input_changes_the_key(change: dict[str, object]) -> None:
    assert request().key() != request(**change).key()


def test_the_key_is_stable_and_hex() -> None:
    assert request().key() == request().key()
    assert len(request().key()) == 64
    assert int(request().key(), 16) >= 0


def test_round_trip_keeps_audio_format_and_alignment(tmp_path: Path, clock: Clock) -> None:
    cache = make_cache(tmp_path, clock)
    clip = AudioClip(b"MP3DATA", mp3_format(128), even_alignment("Hi.", 0.3))
    key = request().key()
    assert cache.get(key) is None
    put_at(cache, clock, key, clip)
    assert cache.contains(key)
    assert cache.get(key) == clip


def test_entries_without_alignment_round_trip(tmp_path: Path, clock: Clock) -> None:
    cache = make_cache(tmp_path, clock)
    put_at(cache, clock, request().key(), AudioClip(b"RIFF", WAV))
    assert cache.get(request().key()) == AudioClip(b"RIFF", WAV)


def test_file_names_reveal_nothing_about_the_text(tmp_path: Path, clock: Clock) -> None:
    cache = make_cache(tmp_path, clock)
    put_at(cache, clock, request().key(), AudioClip(b"x", WAV))
    names = " ".join(p.name for p in cache.root.iterdir())
    assert "Hello" not in names
    assert {p.suffix for p in cache.root.iterdir()} == {".audio", ".json"}
    assert oct(cache.root.stat().st_mode & 0o777) == "0o700"


def test_entries_expire(tmp_path: Path, clock: Clock) -> None:
    cache = make_cache(tmp_path, clock)
    key = request().key()
    put_at(cache, clock, key, AudioClip(b"x", WAV))
    clock.now += 101
    assert not cache.contains(key)
    assert cache.get(key) is None
    assert cache.purge_expired() == 1
    assert list(cache.root.iterdir()) == []


def test_reading_does_not_extend_the_ttl(tmp_path: Path, clock: Clock) -> None:
    cache = make_cache(tmp_path, clock)
    key = request().key()
    put_at(cache, clock, key, AudioClip(b"x", WAV))
    clock.now += 60
    assert cache.get(key) is not None
    clock.now += 60
    assert cache.get(key) is None


def test_least_recently_used_entries_are_evicted_first(tmp_path: Path, clock: Clock) -> None:
    cache = make_cache(tmp_path, clock, max_bytes=2_000)
    keys = [request(text=f"t{i}").key() for i in range(3)]
    put_at(cache, clock, keys[0], AudioClip(b"a" * 700, WAV))
    clock.now += 1
    put_at(cache, clock, keys[1], AudioClip(b"b" * 700, WAV))
    clock.now += 1
    assert cache.get(keys[0]) is not None  # keys[0] is now the most recently used
    clock.now += 1
    put_at(cache, clock, keys[2], AudioClip(b"c" * 700, WAV))  # pushes the total over 2 kB

    assert cache.contains(keys[0])
    assert not cache.contains(keys[1])
    assert cache.contains(keys[2])
    assert cache.size_bytes() <= 2_000


def test_corrupt_entries_are_misses_not_errors(tmp_path: Path, clock: Clock) -> None:
    cache = make_cache(tmp_path, clock)
    key = request().key()
    put_at(cache, clock, key, AudioClip(b"abcdef", WAV))
    (cache.root / f"{key}.audio").write_bytes(b"abc")  # truncated
    assert cache.get(key) is None
    (cache.root / f"{key}.json").write_text("{not json")
    assert cache.get(key) is None


def test_bad_keys_and_oversized_clips_are_ignored(tmp_path: Path, clock: Clock) -> None:
    cache = make_cache(tmp_path, clock, max_bytes=10)
    cache.put("../../etc/passwd", AudioClip(b"x", WAV))
    cache.put(request().key(), AudioClip(b"x" * 11, WAV))
    assert list(cache.root.iterdir()) == []
    assert cache.get("../../etc/passwd") is None


def test_a_zero_size_cache_is_disabled(tmp_path: Path, clock: Clock) -> None:
    cache = ClipCache(tmp_path / "off", max_bytes=0, ttl_seconds=100, clock=clock)
    cache.put(request().key(), AudioClip(b"x", WAV))
    assert not cache.enabled
    assert cache.get(request().key()) is None
    assert cache.purge_expired() == 0
    assert cache.size_bytes() == 0
    cache.clear()
    assert not (tmp_path / "off").exists()
