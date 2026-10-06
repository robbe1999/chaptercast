"""Typed, validated runtime configuration.

Everything comes from environment variables (or a local, gitignored ``.env``).
Secrets are held as ``SecretStr`` so they never appear in ``repr()``, logs or
error messages, and every value that ends up in a URL, header or file path is
validated here, once, at startup.
"""

from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from chaptercast.models import ELEVENLABS_MODELS

ProviderName = Literal["elevenlabs", "demo"]

_ALLOWED_PROVIDER_SUFFIX = "elevenlabs.io"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CHAPTERCAST_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        # A malformed secret must not be echoed back in a startup error or log line.
        hide_input_in_errors=True,
    )

    # --- Provider -------------------------------------------------------------
    provider: Literal["auto", "elevenlabs", "demo"] = "auto"
    # Standard variable name (no CHAPTERCAST_ prefix) so existing tooling just works.
    elevenlabs_api_key: SecretStr | None = Field(
        default=None, validation_alias="ELEVENLABS_API_KEY"
    )
    elevenlabs_base_url: str = "https://api.elevenlabs.io"
    elevenlabs_model_id: str = Field(default="eleven_multilingual_v2", pattern=r"^[a-z0-9_]{1,64}$")
    # Restricted to MP3 so duration can be derived from the CBR bitrate.
    elevenlabs_output_format: str = Field(default="mp3_44100_128", pattern=r"^mp3_\d{4,5}_\d{2,3}$")
    # Models users may pick (comma separated). The default model is always allowed.
    # Every id must be in the model registry (chaptercast/models.py); that is checked
    # at startup, so a typo fails at boot rather than on the first request.
    allowed_models: str = Field(
        default=(
            "eleven_multilingual_v2,eleven_flash_v2_5,eleven_turbo_v2_5,eleven_v4,eleven_v4_turbo"
        ),
        pattern=r"^[a-z0-9_]{1,64}(,[a-z0-9_]{1,64})*$",
    )
    tts_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    tts_max_retries: int = Field(default=4, ge=0, le=8)

    # --- Access control -------------------------------------------------------
    access_token: SecretStr | None = None
    cors_origins: str = ""
    trust_proxy_headers: bool = False

    # --- Limits and cost guards ----------------------------------------------
    max_chars_per_job: int = Field(default=5000, ge=1, le=100_000)
    daily_char_budget: int = Field(default=25_000, ge=1)
    chunk_max_chars: int = Field(default=900, ge=50, le=4500)
    tts_concurrency: int = Field(default=3, ge=1, le=10)
    max_concurrent_jobs: int = Field(default=2, ge=1, le=20)
    max_active_jobs: int = Field(default=5, ge=1, le=100)
    max_stored_jobs: int = Field(default=500, ge=1)
    job_ttl_seconds: int = Field(default=3600, ge=10)
    jobs_per_minute: int = Field(default=10, ge=1)
    max_request_bytes: int = Field(default=256 * 1024, ge=1024)
    # Re-use synthesised chunks across jobs so edits only pay for what changed. 0 disables.
    cache_max_mb: int = Field(default=100, ge=0, le=100_000)
    cache_ttl_seconds: int = Field(default=24 * 3600, ge=60)

    # --- Runtime --------------------------------------------------------------
    data_dir: Path = Field(default_factory=lambda: Path(tempfile.gettempdir()) / "chaptercast")
    static_dir: Path | None = None
    enable_docs: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Validators -----------------------------------------------------------
    @field_validator("elevenlabs_api_key", "access_token", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("access_token")
    @classmethod
    def _access_token_long_enough(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 16:
            raise ValueError("CHAPTERCAST_ACCESS_TOKEN must be at least 16 characters")
        return value

    @field_validator("elevenlabs_base_url")
    @classmethod
    def _base_url_is_elevenlabs_https(cls, value: str) -> str:
        # The API key is sent to this host, so a typo or injected env var must not be
        # able to redirect it somewhere else.
        parsed = urlparse(value.strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https":
            raise ValueError("ELEVENLABS base URL must use https")
        if host != _ALLOWED_PROVIDER_SUFFIX and not host.endswith("." + _ALLOWED_PROVIDER_SUFFIX):
            raise ValueError("ELEVENLABS base URL must be an elevenlabs.io host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("ELEVENLABS base URL must not contain credentials or a query")
        return f"https://{parsed.netloc}".rstrip("/")

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcard_cors(cls, value: str) -> str:
        if "*" in value:
            raise ValueError("Wildcard CORS origins are not allowed")
        return value.strip()

    @model_validator(mode="after")
    def _models_are_registered(self) -> Settings:
        unknown = [m for m in self.allowed_model_ids if m not in ELEVENLABS_MODELS]
        if unknown:
            raise ValueError(
                f"Unknown model ids in CHAPTERCAST_ALLOWED_MODELS or the default model: "
                f"{', '.join(unknown)}. Register them in chaptercast/models.py first."
            )
        return self

    @model_validator(mode="after")
    def _provider_has_what_it_needs(self) -> Settings:
        if self.provider == "elevenlabs" and self.elevenlabs_api_key is None:
            raise ValueError("ELEVENLABS_API_KEY is required when CHAPTERCAST_PROVIDER=elevenlabs")
        return self

    # --- Derived --------------------------------------------------------------
    @property
    def resolved_provider(self) -> ProviderName:
        if self.provider == "auto":
            return "elevenlabs" if self.elevenlabs_api_key is not None else "demo"
        return self.provider

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_model_ids(self) -> list[str]:
        ids = [m for m in self.allowed_models.split(",") if m]
        if self.elevenlabs_model_id not in ids:
            ids.insert(0, self.elevenlabs_model_id)
        return ids

    @property
    def mp3_bitrate_kbps(self) -> int:
        return int(self.elevenlabs_output_format.split("_")[2])

    def secret_values(self) -> list[str]:
        """Literal secrets, for the log redactor."""
        values = []
        for secret in (self.elevenlabs_api_key, self.access_token):
            if secret is not None:
                values.append(secret.get_secret_value())
        return values


@lru_cache
def get_settings() -> Settings:
    return Settings()
