"""Request and response models: the public API contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class VoiceSettingsBody(BaseModel):
    """Optional voice tuning. Omitted fields use the voice's stored defaults."""

    model_config = ConfigDict(extra="forbid")

    stability: float | None = Field(
        default=None, ge=0, le=1, description="Lower is more expressive, higher more consistent."
    )
    similarity_boost: float | None = Field(
        default=None, ge=0, le=1, description="How closely to match the original voice."
    )
    style: float | None = Field(
        default=None, ge=0, le=1, description="Style exaggeration (models that support it)."
    )
    speed: float | None = Field(default=None, ge=0.7, le=1.2, description="Speaking rate.")


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Hard ceiling at the schema level; the configurable per-job limit is enforced
    # on the *normalised* text in the job manager.
    text: str = Field(min_length=1, max_length=200_000, description="The text to narrate.")
    voice_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$", description="Voice identifier.")
    model_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9_]{1,64}$",
        description="Model to use; one of GET /api/models. Defaults to the server default.",
    )
    voice_settings: VoiceSettingsBody | None = None


class EstimateRequest(CreateJobRequest):
    """Same shape as a job request: an estimate is a dry run of exactly that job."""

    text: str = Field(max_length=200_000, description="The text to narrate.")


class EstimateResponse(BaseModel):
    characters: int
    chunks: int
    cached_chunks: int = Field(description="Chunks already generated recently: free to reuse.")
    billable_characters: int
    cost_multiplier: float
    estimated_credits: int
    max_chars_per_job: int
    within_limit: bool
    daily_budget_remaining: int


class JobProgress(BaseModel):
    completed_chunks: int
    total_chunks: int


class JobError(BaseModel):
    code: str
    message: str


class CaptionLinks(BaseModel):
    srt: str
    vtt: str


class JobResponse(BaseModel):
    id: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    voice_id: str
    model_id: str
    char_count: int
    progress: JobProgress
    cached_chunks: int = Field(description="Chunks reused from the cache instead of re-generated.")
    billed_characters: int = Field(description="Characters actually sent to the provider.")
    created_at: datetime
    duration_seconds: float | None
    audio_url: str | None
    transcript_url: str | None
    captions: CaptionLinks | None
    error: JobError | None


class TranscriptWord(BaseModel):
    text: str
    start: float = Field(description="Seconds from the start of the audio.")
    end: float
    paragraph: int


class TranscriptResponse(BaseModel):
    duration_seconds: float | None
    words: list[TranscriptWord]


class VoiceResponse(BaseModel):
    voice_id: str
    name: str
    category: str | None
    description: str | None
    labels: dict[str, str]
    preview_url: str | None


class ModelResponse(BaseModel):
    model_id: str
    name: str
    description: str | None
    cost_multiplier: float
    supports_style: bool


class ModelsResponse(BaseModel):
    provider: Literal["elevenlabs", "demo"]
    default_model_id: str
    models: list[ModelResponse]


class VoicesResponse(BaseModel):
    provider: Literal["elevenlabs", "demo"]
    voices: list[VoiceResponse]


class ConfigResponse(BaseModel):
    provider: Literal["elevenlabs", "demo"]
    auth_required: bool
    max_chars_per_job: int
    chunk_max_chars: int
    cache_enabled: bool


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody
