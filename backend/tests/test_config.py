from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from chaptercast.config import Settings
from tests.conftest import ACCESS_TOKEN, SENTINEL_KEY


def make(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_defaults_to_demo_without_a_key() -> None:
    assert make().resolved_provider == "demo"


def test_auto_selects_elevenlabs_when_a_key_is_present() -> None:
    assert make(elevenlabs_api_key=SENTINEL_KEY).resolved_provider == "elevenlabs"


def test_explicit_elevenlabs_without_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="ELEVENLABS_API_KEY is required"):
        make(provider="elevenlabs")


def test_blank_secrets_become_none() -> None:
    settings = make(elevenlabs_api_key="   ", access_token="")
    assert settings.elevenlabs_api_key is None
    assert settings.access_token is None


def test_secrets_never_appear_in_repr_or_dump() -> None:
    settings = make(elevenlabs_api_key=SENTINEL_KEY, access_token=ACCESS_TOKEN)
    for rendered in (repr(settings), str(settings), settings.model_dump_json()):
        assert SENTINEL_KEY not in rendered
        assert ACCESS_TOKEN not in rendered


def test_validation_errors_do_not_echo_the_secret() -> None:
    with pytest.raises(ValidationError) as info:
        make(access_token="short")
    assert "short" not in str(info.value).replace("at least 16 characters", "")


def test_key_is_read_from_the_standard_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", SENTINEL_KEY)
    monkeypatch.setenv("CHAPTERCAST_MAX_CHARS_PER_JOB", "123")
    settings = make()
    assert settings.elevenlabs_api_key is not None
    assert settings.elevenlabs_api_key.get_secret_value() == SENTINEL_KEY
    assert settings.max_chars_per_job == 123


@pytest.mark.parametrize(
    "url",
    [
        "http://api.elevenlabs.io",
        "https://evil.example.com",
        "https://api.elevenlabs.io.evil.com",
        "https://evilelevenlabs.io",
        "https://user:pw@api.elevenlabs.io",
        "https://api.elevenlabs.io/?x=1",
    ],
)
def test_provider_base_url_must_be_an_https_elevenlabs_host(url: str) -> None:
    with pytest.raises(ValidationError):
        make(elevenlabs_base_url=url)


@pytest.mark.parametrize(
    "url", ["https://api.elevenlabs.io", "https://api.eu.residency.elevenlabs.io/"]
)
def test_regional_elevenlabs_hosts_are_accepted(url: str) -> None:
    assert make(elevenlabs_base_url=url).elevenlabs_base_url.startswith("https://")


def test_wildcard_cors_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Wildcard"):
        make(cors_origins="*")


def test_cors_origins_are_parsed() -> None:
    settings = make(cors_origins="http://a.test, http://b.test ,")
    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]


def test_access_token_minimum_length() -> None:
    with pytest.raises(ValidationError, match="at least 16"):
        make(access_token="too-short")


@pytest.mark.parametrize("fmt", ["pcm_44100", "mp3_44100_128; rm", "../../x", "wav_1_1"])
def test_output_format_is_restricted_to_mp3(fmt: str) -> None:
    with pytest.raises(ValidationError):
        make(elevenlabs_output_format=fmt)


def test_bitrate_is_derived_from_the_format() -> None:
    assert make(elevenlabs_output_format="mp3_44100_192").mp3_bitrate_kbps == 192


def test_secret_values_are_exposed_for_redaction_only() -> None:
    settings = make(elevenlabs_api_key=SENTINEL_KEY, access_token=ACCESS_TOKEN)
    assert set(settings.secret_values()) == {SENTINEL_KEY, ACCESS_TOKEN}
