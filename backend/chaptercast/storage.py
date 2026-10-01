"""On-disk storage for finished audio.

File names are derived only from a server-generated 128-bit hex job id and a
fixed extension allowlist, so no user input ever reaches a path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_EXTENSIONS = {"mp3", "wav"}
_ORPHAN_RE = re.compile(r"^\.?[0-9a-f]{32}\.(?:mp3|wav|tmp)$")


class AudioStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def write(self, job_id: str, extension: str, data: bytes) -> Path:
        if not JOB_ID_RE.fullmatch(job_id) or extension not in _EXTENSIONS:
            raise ValueError("invalid job id or extension")
        final = self.root / f"{job_id}.{extension}"
        tmp = self.root / f".{job_id}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, final)  # atomic: readers never see a partial file
        return final

    def delete(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            if path.resolve().parent == self.root.resolve():
                path.unlink(missing_ok=True)
        except OSError:
            pass

    def purge_orphans(self) -> int:
        """Remove files left behind by a previous process (job state is in memory)."""
        removed = 0
        for entry in self.root.iterdir():
            if entry.is_file() and _ORPHAN_RE.fullmatch(entry.name):
                entry.unlink(missing_ok=True)
                removed += 1
        return removed

    def is_writable(self) -> bool:
        return os.access(self.root, os.W_OK)
