#!/usr/bin/env python3
"""Dependency-free secret scanner.

Walks a directory tree and fails (exit 1) if it finds anything that looks like a
credential. It is a fast local guard that complements gitleaks in CI; it is not a
replacement for it.

Inside a git work tree it scans exactly what *could* be committed: tracked files
plus untracked files that are not gitignored. A real key in the (ignored) local
``.env`` is therefore fine, while the same key in any new or tracked file fails.

Usage:
    python scripts/scan_secrets.py [PATH ...]     # defaults to the repo root

A line can opt out with the marker ``scan:allow`` (use sparingly, e.g. in docs
that show a deliberately fake value).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

ALLOW_MARKER = "scan:allow"

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "htmlcov", "coverage",
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".zip", ".mp3", ".wav"}
SKIP_FILES = {"package-lock.json", "requirements.lock"}
MAX_BYTES = 2_000_000

PATTERNS: dict[str, re.Pattern[str]] = {
    "ElevenLabs-style key": re.compile(r"\bsk_[A-Za-z0-9]{24,}\b"),
    "OpenAI/Anthropic-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b"),
    "AWS access key id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "Private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    "Hard-coded credential": re.compile(
        r"""(?ix)
        \b(?:api[_-]?key|secret|token|passw(?:or)?d)\b
        ["']?\s*[:=]\s*
        ["'][A-Za-z0-9_\-/+=]{20,}["']
        """
    ),
}


def _git_candidates(root: Path) -> list[Path] | None:
    """Files git would let you commit under ``root``, or None outside a work tree."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603  (fixed argv, no shell)
            [git, "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    names = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return [root / name for name in names if name]


def iter_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    candidates = _git_candidates(root)
    for path in candidates if candidates is not None else root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES or path.name in SKIP_FILES:
            continue
        yield path


def scan_file(path: Path) -> list[tuple[int, str]]:
    try:
        if path.stat().st_size > MAX_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    findings: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(line):
                findings.append((lineno, label))
    return findings


def scan(paths: list[Path]) -> list[tuple[Path, int, str]]:
    results: list[tuple[Path, int, str]] = []
    for root in paths:
        for file in iter_files(root):
            for lineno, label in scan_file(file):
                results.append((file, lineno, label))
    return results


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv] or [Path(__file__).resolve().parent.parent]
    findings = scan(roots)
    for path, lineno, label in findings:
        # Print location and rule only. Never echo the matched text.
        print(f"{path}:{lineno}: possible secret ({label})")
    if findings:
        print(f"\n{len(findings)} potential secret(s) found.", file=sys.stderr)
        return 1
    print("No secrets found.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
