"""HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse

from chaptercast.deps import Container, client_ip, get_container, require_access
from chaptercast.errors import (
    ApiError,
    BudgetExceededError,
    CapacityError,
    EmptyTextError,
    TextTooLongError,
)
from chaptercast.jobs import Job, JobStatus
from chaptercast.providers.base import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from chaptercast.schemas import (
    ConfigResponse,
    CreateJobRequest,
    ErrorResponse,
    JobError,
    JobProgress,
    JobResponse,
    VoiceResponse,
    VoicesResponse,
)
from chaptercast.storage import JOB_ID_RE

router = APIRouter(prefix="/api")

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid access token"},
    429: {"model": ErrorResponse, "description": "Rate limit or daily budget exceeded"},
}


def _provider_http_error(exc: ProviderError) -> ApiError:
    if isinstance(exc, ProviderAuthError):
        return ApiError(502, exc.code, exc.public_message)
    if isinstance(exc, ProviderQuotaError | ProviderUnavailableError):
        return ApiError(503, exc.code, exc.public_message, headers={"Retry-After": "30"})
    if isinstance(exc, ProviderRateLimitError):
        return ApiError(503, exc.code, exc.public_message, headers={"Retry-After": "10"})
    return ApiError(502, exc.code, exc.public_message)


def _to_response(job: Job) -> JobResponse:
    return JobResponse(
        id=job.id,
        status=job.status.value,
        voice_id=job.voice_id,
        char_count=job.char_count,
        progress=JobProgress(completed_chunks=job.completed_chunks, total_chunks=job.total_chunks),
        created_at=job.created_at,
        duration_seconds=job.duration_seconds,
        audio_url=f"/api/jobs/{job.id}/audio" if job.status is JobStatus.SUCCEEDED else None,
        error=(
            JobError(code=job.error_code or "error", message=job.error_message or "")
            if job.status is JobStatus.FAILED
            else None
        ),
    )


def _find_job(container: Container, job_id: str) -> Job:
    # Malformed and unknown ids get the same answer: no signal for enumeration.
    job = container.jobs.get(job_id) if JOB_ID_RE.fullmatch(job_id) else None
    if job is None:
        raise ApiError(404, "not_found", "Job not found.")
    return job


@router.get("/config", response_model=ConfigResponse, summary="Public client configuration")
async def get_config(request: Request) -> ConfigResponse:
    settings = get_container(request).settings
    return ConfigResponse(
        provider=settings.resolved_provider,
        auth_required=settings.access_token is not None,
        max_chars_per_job=settings.max_chars_per_job,
        chunk_max_chars=settings.chunk_max_chars,
    )


@router.get(
    "/voices",
    response_model=VoicesResponse,
    dependencies=[Depends(require_access)],
    responses=_ERRORS,
    summary="List available voices",
)
async def list_voices(request: Request) -> VoicesResponse:
    container = get_container(request)
    try:
        voices = await container.provider.list_voices()
    except ProviderError as exc:
        raise _provider_http_error(exc) from None
    return VoicesResponse(
        provider=container.settings.resolved_provider,
        voices=[
            VoiceResponse(
                voice_id=v.voice_id, name=v.name, category=v.category, description=v.description
            )
            for v in voices
        ],
    )


@router.post(
    "/jobs",
    status_code=202,
    response_model=JobResponse,
    dependencies=[Depends(require_access)],
    responses=_ERRORS,
    summary="Start an audiobook job",
)
async def create_job(
    payload: CreateJobRequest, request: Request, response: Response
) -> JobResponse:
    container = get_container(request)
    settings = container.settings

    ip = client_ip(request, trust_proxy=settings.trust_proxy_headers)
    allowed, wait = container.job_limiter.allow(ip)
    if not allowed:
        raise ApiError(
            429, "rate_limited", "Too many jobs. Slow down.",
            headers={"Retry-After": str(int(wait) + 1)},
        )  # fmt: skip

    try:
        job = await container.jobs.submit(payload.text, payload.voice_id)
    except EmptyTextError:
        raise ApiError(422, "empty_text", "The text contains nothing to narrate.") from None
    except TextTooLongError as exc:
        raise ApiError(
            422, "text_too_long",
            f"Text is {exc.actual} characters; the limit is {exc.limit}.",
        ) from None  # fmt: skip
    except BudgetExceededError:
        raise ApiError(
            429, "daily_budget_exceeded", "The daily character budget is used up. Try tomorrow.",
            headers={"Retry-After": "3600"},
        ) from None  # fmt: skip
    except CapacityError:
        raise ApiError(
            503, "busy", "The server is busy. Try again in a moment.",
            headers={"Retry-After": "10"},
        ) from None  # fmt: skip

    response.headers["Location"] = f"/api/jobs/{job.id}"
    return _to_response(job)


@router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    dependencies=[Depends(require_access)],
    responses=_ERRORS,
    summary="Get job status and progress",
)
async def get_job(job_id: str, request: Request) -> JobResponse:
    return _to_response(_find_job(get_container(request), job_id))


@router.delete(
    "/jobs/{job_id}",
    status_code=204,
    dependencies=[Depends(require_access)],
    responses=_ERRORS,
    summary="Cancel a job and delete its audio",
)
async def delete_job(job_id: str, request: Request) -> Response:
    container = get_container(request)
    _find_job(container, job_id)
    await container.jobs.delete(job_id)
    return Response(status_code=204)


@router.get(
    "/jobs/{job_id}/audio",
    dependencies=[Depends(require_access)],
    responses={**_ERRORS, 200: {"content": {"audio/mpeg": {}, "audio/wav": {}}}},
    summary="Download the finished audio (supports Range requests)",
)
async def get_audio(job_id: str, request: Request) -> FileResponse:
    job = _find_job(get_container(request), job_id)
    if job.status is not JobStatus.SUCCEEDED or job.audio_path is None or job.audio_format is None:
        raise ApiError(409, "not_ready", "The audio is not ready.")
    return FileResponse(
        job.audio_path,
        media_type=job.audio_format.content_type,
        filename=f"chaptercast-{job.id[:8]}.{job.audio_format.extension}",
        content_disposition_type="inline",
    )
