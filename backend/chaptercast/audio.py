"""Audio formats, duration estimates and stitching of per-chunk clips."""

from __future__ import annotations

import asyncio
import io
import logging
import shutil
import tempfile
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioFormat:
    extension: str
    content_type: str
    bitrate_kbps: int | None = None  # set for constant-bitrate formats


WAV = AudioFormat("wav", "audio/wav")


def mp3_format(bitrate_kbps: int) -> AudioFormat:
    return AudioFormat("mp3", "audio/mpeg", bitrate_kbps)


def estimate_duration_seconds(data: bytes, fmt: AudioFormat) -> float | None:
    """Exact for WAV, bitrate-derived (CBR) for MP3."""
    if fmt.extension == "wav":
        try:
            with wave.open(io.BytesIO(data), "rb") as wav:
                return wav.getnframes() / float(wav.getframerate())
        except (wave.Error, EOFError):
            return None
    if fmt.bitrate_kbps:
        return len(data) * 8 / (fmt.bitrate_kbps * 1000)
    return None


def _stitch_wav(clips: Sequence[bytes]) -> bytes:
    out = io.BytesIO()
    params = None
    with wave.open(out, "wb") as writer:
        for clip in clips:
            with wave.open(io.BytesIO(clip), "rb") as reader:
                current = (reader.getnchannels(), reader.getsampwidth(), reader.getframerate())
                if params is None:
                    params = current
                    writer.setnchannels(current[0])
                    writer.setsampwidth(current[1])
                    writer.setframerate(current[2])
                elif current != params:
                    raise ValueError("WAV clips have mismatched parameters")
                writer.writeframes(reader.readframes(reader.getnframes()))
    return out.getvalue()


async def _ffmpeg_concat(
    ffmpeg: str, clips: Sequence[bytes], extension: str, timeout: float
) -> bytes:
    """Lossless concat via ffmpeg's concat demuxer. All file names are generated here."""
    with tempfile.TemporaryDirectory(prefix="chaptercast-") as tmp:
        root = Path(tmp)
        names = []
        for index, clip in enumerate(clips):
            name = f"clip_{index:04d}.{extension}"
            (root / name).write_bytes(clip)
            names.append(name)
        (root / "list.txt").write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
        out_name = f"out.{extension}"
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-i", "list.txt", "-c", "copy", out_name,
            cwd=root,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg exited with {proc.returncode}: {stderr[:300]!r}")
        return (root / out_name).read_bytes()


async def stitch(
    clips: Sequence[bytes],
    fmt: AudioFormat,
    *,
    ffmpeg_path: str | None = None,
    timeout: float = 60.0,
) -> bytes:
    """Join clips in order into a single file.

    WAV is stitched in pure Python. MP3 uses ffmpeg (``-c copy``, no re-encode)
    when available and falls back to byte concatenation, which is valid for
    constant-bitrate MP3 from a single encoder.
    """
    if not clips:
        raise ValueError("No clips to stitch")
    if len(clips) == 1:
        return clips[0]
    if fmt.extension == "wav":
        return _stitch_wav(clips)

    ffmpeg = ffmpeg_path if ffmpeg_path is not None else shutil.which("ffmpeg")
    if ffmpeg:
        try:
            return await _ffmpeg_concat(ffmpeg, clips, fmt.extension, timeout)
        except (OSError, RuntimeError, TimeoutError) as exc:
            log.warning("ffmpeg concat failed, falling back to byte concat: %s", exc)
    return b"".join(clips)
