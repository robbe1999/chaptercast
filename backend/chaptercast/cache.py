"""Content-addressed cache of synthesised chunks.

Re-generating a chapter after fixing one typo should not pay for the whole
chapter again. Every chunk is stored under a SHA-256 of *everything* that
influences its audio (provider, model, voice, settings, output format, the
text and the neighbouring context), so a hit is guaranteed to be the same
request and an edit invalidates exactly the chunks it touches plus their
immediate neighbours (whose context changed).

Privacy and safety:

* file names are hex digests, so neither paths nor directory listings reveal text;
* entries expire after ``ttl_seconds`` and the cache is bounded by ``max_bytes``
  with least-recently-used eviction;
* writes are atomic (temp file + rename) and a corrupt or partial entry is
  treated as a miss, never as an error.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chaptercast.audio import AudioFormat
from chaptercast.providers.base import Alignment, AudioClip, VoiceSettings

log = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ClipRequest:
    """Every input that can change a synthesised clip."""

    provider: str
    output_format: str
    model_id: str
    voice_id: str
    voice_settings: VoiceSettings
    text: str
    previous_text: str | None
    next_text: str | None

    def key(self) -> str:
        material = json.dumps(
            {
                "v": _SCHEMA_VERSION,
                "provider": self.provider,
                "format": self.output_format,
                "model": self.model_id,
                "voice": self.voice_id,
                "settings": self.voice_settings.as_payload(),
                "text": self.text,
                "prev": self.previous_text,
                "next": self.next_text,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ClipCache:
    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = root
        self._max_bytes = max_bytes
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()  # entries are read/written from worker threads
        self.enabled = max_bytes > 0
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)

    # ------------------------------------------------------------------ queries
    def contains(self, key: str) -> bool:
        if not self.enabled or not _KEY_RE.fullmatch(key):
            return False
        meta = self.root / f"{key}.json"
        try:
            return self._clock() - meta.stat().st_mtime <= self._ttl
        except OSError:
            return False

    def get(self, key: str) -> AudioClip | None:
        if not self.contains(key):
            return None
        audio_path, meta_path = self._paths(key)
        try:
            meta: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
            data = audio_path.read_bytes()
            if meta.get("v") != _SCHEMA_VERSION or len(data) != meta.get("size"):
                return None
            fmt = AudioFormat(**meta["format"])
            alignment = Alignment.from_json(meta["alignment"]) if meta.get("alignment") else None
        except (OSError, ValueError, KeyError, TypeError):
            return None
        # Touch for LRU, without extending the TTL: atime marks use, mtime marks creation.
        now = self._clock()
        with contextlib.suppress(OSError):
            os.utime(meta_path, (now, meta_path.stat().st_mtime))
        return AudioClip(data, fmt, alignment)

    # ----------------------------------------------------------------- commands
    def put(self, key: str, clip: AudioClip) -> None:
        if not self.enabled or not _KEY_RE.fullmatch(key):
            return
        if len(clip.data) > self._max_bytes:
            return
        audio_path, meta_path = self._paths(key)
        meta = {
            "v": _SCHEMA_VERSION,
            "size": len(clip.data),
            "format": {
                "extension": clip.fmt.extension,
                "content_type": clip.fmt.content_type,
                "bitrate_kbps": clip.fmt.bitrate_kbps,
            },
            "alignment": clip.alignment.to_json() if clip.alignment else None,
        }
        with self._lock:
            try:
                _atomic_write(audio_path, clip.data)
                # Metadata last: an entry only "exists" once both halves are on disk.
                _atomic_write(meta_path, json.dumps(meta).encode("utf-8"))
            except OSError as exc:
                log.warning("cache write failed: %s", type(exc).__name__)
                return
            self._evict_locked()

    def purge_expired(self) -> int:
        if not self.enabled:
            return 0
        with self._lock:
            now = self._clock()
            removed = 0
            for key, _, mtime, _ in self._entries():
                if now - mtime > self._ttl:
                    self._remove(key)
                    removed += 1
            return removed

    def clear(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            for key, *_ in self._entries():
                self._remove(key)

    def size_bytes(self) -> int:
        return sum(size for *_, size in self._entries()) if self.enabled else 0

    # ---------------------------------------------------------------- internals
    def _paths(self, key: str) -> tuple[Path, Path]:
        return self.root / f"{key}.audio", self.root / f"{key}.json"

    def _entries(self) -> list[tuple[str, float, float, int]]:
        """(key, last_used, created, total_size) for every complete entry."""
        entries = []
        for meta in self.root.glob("*.json"):
            key = meta.stem
            if not _KEY_RE.fullmatch(key):
                continue
            try:
                stat = meta.stat()
                size = stat.st_size + (self.root / f"{key}.audio").stat().st_size
            except OSError:
                continue
            entries.append((key, stat.st_atime, stat.st_mtime, size))
        return entries

    def _remove(self, key: str) -> None:
        for path in self._paths(key):
            path.unlink(missing_ok=True)

    def _evict_locked(self) -> None:
        entries = self._entries()
        total = sum(size for *_, size in entries)
        for key, *_, size in sorted(entries, key=lambda e: e[1]):
            if total <= self._max_bytes:
                break
            self._remove(key)
            total -= size


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)
