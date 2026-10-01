"""Request and response models: the public API contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Hard ceiling at the schema level; the configurable per-job limit is enforced
    # on the *normalised* text in the job manager.
    text: str = Field(min_length=1, max_length=200_000, description="The text to narrate.")
    voice_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$", description="Voice identifier.")


class JobProgress(BaseModel):
    completed_chunks: int
    total_chunks: int


class JobError(BaseModel):
    code: str
    message: str


class JobResponse(BaseModel):
    id: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    voice_id: str
    char_count: int
    progress: JobProgress
    created_at: datetime
    duration_seconds: float | None
    audio_url: str | None
    error: JobError | None


class VoiceResponse(BaseModel):
    voice_id: str
    name: str
    category: str | None
    description: str | None


class VoicesResponse(BaseModel):
    provider: Literal["elevenlabs", "demo"]
    voices: list[VoiceResponse]


class ConfigResponse(BaseModel):
    provider: Literal["elevenlabs", "demo"]
    auth_required: bool
    max_chars_per_job: int
    chunk_max_chars: int


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody
