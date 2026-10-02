"""Guards that keep the public contract and the repository honest."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from chaptercast.main import create_app
from tests.conftest import SENTINEL_KEY, make_settings

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "docs" / "openapi.json"


def test_openapi_snapshot_is_current(tmp_path: Path) -> None:
    """The committed API contract must match the code.

    Regenerate with:  CHAPTERCAST_UPDATE_SNAPSHOT=1 pytest tests/test_contracts.py
    """
    current = create_app(make_settings(tmp_path)).openapi()
    rendered = json.dumps(current, indent=2, sort_keys=True) + "\n"
    if os.environ.get("CHAPTERCAST_UPDATE_SNAPSHOT") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(rendered, encoding="utf-8")
    assert SNAPSHOT.exists(), "docs/openapi.json is missing; regenerate it (see docstring)"
    assert SNAPSHOT.read_text(encoding="utf-8") == rendered, (
        "API contract changed; update the snapshot"
    )


def _load_scanner() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "scan_secrets", ROOT / "scripts" / "scan_secrets.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_contains_no_secrets() -> None:
    findings = _load_scanner().scan([ROOT])
    assert findings == [], f"possible secrets: {[(str(p), n, label) for p, n, label in findings]}"


def test_scanner_detects_key_shaped_values(tmp_path: Path) -> None:
    scanner = _load_scanner()
    leaky = tmp_path / "config.py"
    leaky.write_text(f'KEY = "{SENTINEL_KEY}"\nAWS = "AKIA' + "A" * 16 + '"\n')
    labels = {label for _, _, label in scanner.scan([tmp_path])}
    assert {"ElevenLabs-style key", "AWS access key id"} <= labels


def test_scanner_flags_hard_coded_credentials_but_not_env_lookups(tmp_path: Path) -> None:
    scanner = _load_scanner()
    # Assembled at runtime so this file does not itself look like a leaked credential.
    (tmp_path / "bad.py").write_text('api_key = "' + "abcdefghijklmnopqrstuvwxyz" + '012345"\n')
    (tmp_path / "good.py").write_text('api_key = os.environ["ELEVENLABS_API_KEY"]\n')
    flagged = {p.name for p, _, _ in scanner.scan([tmp_path])}
    assert flagged == {"bad.py"}


def test_scanner_honours_the_allow_marker_and_never_prints_the_secret(tmp_path: Path) -> None:
    scanner = _load_scanner()
    (tmp_path / "doc.md").write_text(f"example {SENTINEL_KEY}  <!-- scan:allow -->\n")
    assert scanner.scan([tmp_path]) == []
    (tmp_path / "leak.txt").write_text(f"{SENTINEL_KEY}\n")
    findings = scanner.scan([tmp_path])
    assert findings and SENTINEL_KEY not in repr(findings)


def test_env_example_has_no_real_values() -> None:
    lines = (ROOT / ".env.example").read_text().splitlines()
    assigned = [ln for ln in lines if ln and not ln.startswith("#") and "=" in ln]
    secret_lines = [
        ln
        for ln in assigned
        if ln.split("=")[0] in {"ELEVENLABS_API_KEY", "CHAPTERCAST_ACCESS_TOKEN"}
    ]
    assert secret_lines, "expected secret placeholders in .env.example"
    assert all(ln.split("=", 1)[1].strip() == "" for ln in secret_lines)


def test_gitignore_protects_env_files() -> None:
    patterns = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in patterns
    assert "!.env.example" in patterns
    assert "*.private.md" in patterns  # personal notes stay out of git and the image
    assert "*.private.md" in (ROOT / ".dockerignore").read_text().splitlines()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_scanner_checks_what_could_be_committed_not_ignored_files(tmp_path: Path) -> None:
    """A real key in the gitignored .env is expected; the same key anywhere else is a leak."""
    scanner = _load_scanner()
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-q", str(tmp_path)], check=True)  # noqa: S603
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / ".env").write_text(f"ELEVENLABS_API_KEY={SENTINEL_KEY}\n")
    (tmp_path / "notes.txt").write_text(f"{SENTINEL_KEY}\n")  # untracked, not ignored
    flagged = {p.name for p, _, _ in scanner.scan([tmp_path])}
    assert flagged == {"notes.txt"}
