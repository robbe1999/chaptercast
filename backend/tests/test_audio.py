from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from chaptercast.audio import WAV, estimate_duration_seconds, mp3_format, stitch
from tests.conftest import make_wav, read_wav_values


async def test_wav_clips_are_stitched_in_order() -> None:
    clips = [make_wav(1, 5), make_wav(2, 5), make_wav(3, 5)]
    values = read_wav_values(await stitch(clips, WAV))
    assert values == [1] * 5 + [2] * 5 + [3] * 5


async def test_wav_clips_with_mismatched_parameters_are_rejected() -> None:
    with pytest.raises(ValueError, match="mismatched"):
        await stitch([make_wav(rate=8000), make_wav(rate=16000)], WAV)


async def test_single_clip_is_returned_unchanged() -> None:
    clip = make_wav(7, 3)
    assert await stitch([clip], WAV) == clip


async def test_empty_input_is_an_error() -> None:
    with pytest.raises(ValueError, match="No clips"):
        await stitch([], WAV)


def test_wav_duration_is_exact() -> None:
    assert estimate_duration_seconds(make_wav(frames=8000, rate=8000), WAV) == pytest.approx(1.0)


def test_corrupt_wav_has_no_duration() -> None:
    assert estimate_duration_seconds(b"not a wav", WAV) is None


def test_mp3_duration_is_derived_from_the_bitrate() -> None:
    data = b"\x00" * 16_000  # 128 kbps = 16,000 bytes per second
    assert estimate_duration_seconds(data, mp3_format(128)) == pytest.approx(1.0)


def test_unknown_bitrate_has_no_duration() -> None:
    from chaptercast.audio import AudioFormat

    assert estimate_duration_seconds(b"x", AudioFormat("mp3", "audio/mpeg")) is None


async def test_mp3_falls_back_to_byte_concat_without_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert await stitch([b"aa", b"bb", b"cc"], mp3_format(128)) == b"aabbcc"


async def test_mp3_falls_back_when_ffmpeg_fails(tmp_path: Path) -> None:
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\nexit 3\n")
    fake.chmod(0o755)
    assert await stitch([b"aa", b"bb"], mp3_format(128), ffmpeg_path=str(fake)) == b"aabb"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_mp3_is_concatenated_losslessly_with_ffmpeg(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None

    def tone(name: str, hz: int) -> bytes:
        out = tmp_path / name
        command = [ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i"]
        command += [f"sine=frequency={hz}:duration=1", "-b:a", "128k", "-ar", "44100", str(out)]
        subprocess.run(command, check=True)  # noqa: S603
        return out.read_bytes()

    first, second = tone("a.mp3", 440), tone("b.mp3", 660)
    joined = await stitch([first, second], mp3_format(128))
    assert len(joined) > max(len(first), len(second))
    assert joined[:2] in {b"\xff\xfb", b"\xff\xf3", b"ID"}  # MP3 frame sync or ID3 tag
