"""HTTP routes."""

from __future__ import annotations

import asyncio
import math
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse

from chaptercast.deps import Container, client_ip, get_container, require_access
from chaptercast.errors import (
    ApiError,
    BudgetExceededError,
    CapacityError,
    EmptyTextError,
    TextTooLongError,
)
from chaptercast.jobs import Job, JobRequest, JobStatus
from chaptercast.models import ModelSpec
from chaptercast.providers.base import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    VoiceSettings,
    is_valid_voice_id,
)
from chaptercast.schemas import (
    CaptionLinks,
    ConfigResponse,
    CreateJobRequest,
    ErrorResponse,
    EstimateRequest,
    EstimateResponse,
    JobError,
    JobProgress,
    JobResponse,
    ModelCapabilities,
    ModelResponse,
    ModelsResponse,
    TranscriptResponse,
    TranscriptWord,
    VoiceResponse,
    VoicesResponse,
)
from chaptercast.storage import JOB_ID_RE
from chaptercast.transcript import Word, build_cues, to_srt, to_webvtt

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
    done = job.status is JobStatus.SUCCEEDED
    has_transcript = done and bool(job.words)
    base = f"/api/jobs/{job.id}"
    return JobResponse(
        id=job.id,
        status=job.status.value,
        voice_id=job.voice_id,
        model_id=job.model_id,
        char_count=job.char_count,
        progress=JobProgress(completed_chunks=job.completed_chunks, total_chunks=job.total_chunks),
        cached_chunks=job.cached_chunks,
        billed_characters=job.billed_chars,
        created_at=job.created_at,
        duration_seconds=job.duration_seconds,
        audio_url=f"{base}/audio" if done else None,
        word_timings=has_transcript,
        transcript_url=f"{base}/transcript" if has_transcript else None,
        captions=(
            CaptionLinks(srt=f"{base}/captions.srt", vtt=f"{base}/captions.vtt")
            if has_transcript
            else None
        ),
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


def _limit(container: Container, request: Request) -> None:
    """Shared limiter for the cheap-but-not-free read endpoints (estimates, previews)."""
    ip = client_ip(request, trust_proxy=container.settings.trust_proxy_headers)
    allowed, wait = container.read_limiter.allow(ip)
    if not allowed:
        raise ApiError(
            429, "rate_limited", "Too many requests. Slow down.",
            headers={"Retry-After": str(int(wait) + 1)},
        )  # fmt: skip


async def _catalog(container: Container) -> tuple[list[ModelSpec], ModelSpec]:
    """The models users may choose from, and the default.

    For ElevenLabs this is the registered models the live API offers, intersected
    with the operator's allowlist: only what the operator is willing to pay for.
    """
    try:
        models = await container.provider.list_models()
    except ProviderError as exc:
        raise _provider_http_error(exc) from None
    if container.settings.resolved_provider == "elevenlabs":
        allowed = container.settings.allowed_model_ids
        models = sorted(
            (m for m in models if m.model_id in allowed),
            key=lambda m: allowed.index(m.model_id),
        )
    if not models:
        raise ApiError(503, "no_models", "No speech model is available right now.")
    default_id = container.settings.elevenlabs_model_id
    default = next((m for m in models if m.model_id == default_id), models[0])
    return models, default


async def _resolve(container: Container, payload: CreateJobRequest) -> tuple[JobRequest, ModelSpec]:
    models, default = await _catalog(container)
    model = default
    if payload.model_id is not None:
        found = next((m for m in models if m.model_id == payload.model_id), None)
        if found is None:
            raise ApiError(422, "unknown_model", "That model is not available on this server.")
        model = found
    body = payload.voice_settings
    settings = VoiceSettings(
        stability=body.stability if body else None,
        similarity_boost=body.similarity_boost if body else None,
        # Settings the model cannot use are dropped, so they never reach the provider
        # or change the cache key for nothing.
        style=body.style if body and model.supports_style else None,
        speed=body.speed if body else None,
        use_speaker_boost=(
            body.use_speaker_boost if body and model.supports_speaker_boost else None
        ),
    )
    return JobRequest(payload.text, payload.voice_id, model, settings), model


@router.get("/config", response_model=ConfigResponse, summary="Public client configuration")
async def get_config(request: Request) -> ConfigResponse:
    container = get_container(request)
    settings = container.settings
    return ConfigResponse(
        provider=settings.resolved_provider,
        auth_required=settings.access_token is not None,
        max_chars_per_job=settings.max_chars_per_job,
        chunk_max_chars=settings.chunk_max_chars,
        cache_enabled=container.cache.enabled,
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
                voice_id=v.voice_id,
                name=v.name,
                category=v.category,
                description=v.description,
                labels=dict(v.labels),
                preview_url=f"/api/voices/{v.voice_id}/preview" if v.has_preview else None,
            )
            for v in voices
        ],
    )


@router.get(
    "/voices/{voice_id}/preview",
    dependencies=[Depends(require_access)],
    responses={**_ERRORS, 200: {"content": {"audio/mpeg": {}, "audio/wav": {}}}},
    summary="A short, free sample of a voice",
)
async def voice_preview(voice_id: str, request: Request) -> Response:
    container = get_container(request)
    if not is_valid_voice_id(voice_id):
        raise ApiError(404, "not_found", "No preview for that voice.")
    _limit(container, request)
    try:
        preview = await container.provider.voice_preview(voice_id)
    except ProviderError as exc:
        raise _provider_http_error(exc) from None
    if preview is None:
        raise ApiError(404, "not_found", "No preview for that voice.")
    return Response(
        preview.data,
        media_type=preview.content_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get(
    "/models",
    response_model=ModelsResponse,
    dependencies=[Depends(require_access)],
    responses=_ERRORS,
    summary="List the speech models this server offers",
)
async def list_models(request: Request) -> ModelsResponse:
    container = get_container(request)
    models, default = await _catalog(container)
    return ModelsResponse(
        provider=container.settings.resolved_provider,
        default_model_id=default.model_id,
        models=[
            ModelResponse(
                model_id=m.model_id,
                label=m.label,
                description=m.description,
                cost_multiplier=m.cost_multiplier,
                max_chars_per_request=m.max_chars_per_request,
                latency_class=m.latency_class,
                capabilities=ModelCapabilities(
                    timestamps=m.supports_timestamps,
                    context_stitching=m.supports_context_stitching,
                    style=m.supports_style,
                    speaker_boost=m.supports_speaker_boost,
                    audio_tags=m.supports_audio_tags,
                    ssml_breaks=m.ssml_breaks,
                ),
            )
            for m in models
        ],
    )


@router.post(
    "/estimate",
    response_model=EstimateResponse,
    dependencies=[Depends(require_access)],
    responses=_ERRORS,
    summary="Dry-run a job: sections, cache hits and credits, without spending anything",
)
async def estimate(payload: EstimateRequest, request: Request) -> EstimateResponse:
    container = get_container(request)
    _limit(container, request)
    job_request, model = await _resolve(container, payload)
    plan = await asyncio.to_thread(container.jobs.plan, job_request)
    limit = container.settings.max_chars_per_job
    return EstimateResponse(
        characters=plan.char_count,
        chunks=len(plan.chunks),
        cached_chunks=plan.cached_chunks,
        billable_characters=plan.billable_chars,
        cost_multiplier=model.cost_multiplier,
        estimated_credits=math.ceil(plan.billable_chars * model.cost_multiplier),
        max_chars_per_job=limit,
        within_limit=plan.char_count <= limit,
        daily_budget_remaining=container.budget.remaining,
        tags_ignored=plan.tags_ignored,
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

    job_request, _ = await _resolve(container, payload)
    try:
        job = await container.jobs.submit(job_request)
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


def _words_or_error(job: Job) -> list[Word]:
    if job.status is not JobStatus.SUCCEEDED:
        raise ApiError(409, "not_ready", "The audio is not ready.")
    if not job.words:
        raise ApiError(
            409, "no_word_timings",
            "This audio has no word timings (the model did not return them), "
            "so there is no read-along or captions for it.",
        )  # fmt: skip
    return job.words


@router.get(
    "/jobs/{job_id}/transcript",
    response_model=TranscriptResponse,
    dependencies=[Depends(require_access)],
    responses=_ERRORS,
    summary="Word-level timings for the read-along view",
)
async def get_transcript(job_id: str, request: Request) -> TranscriptResponse:
    job = _find_job(get_container(request), job_id)
    words = _words_or_error(job)
    return TranscriptResponse(
        duration_seconds=job.duration_seconds,
        words=[
            TranscriptWord(text=w.text, start=w.start, end=w.end, paragraph=w.paragraph)
            for w in words
        ],
    )


@router.get(
    "/jobs/{job_id}/captions.{fmt}",
    dependencies=[Depends(require_access)],
    responses={**_ERRORS, 200: {"content": {"text/vtt": {}, "application/x-subrip": {}}}},
    summary="Download captions as SubRip (.srt) or WebVTT (.vtt)",
)
async def get_captions(job_id: str, fmt: str, request: Request) -> PlainTextResponse:
    if fmt not in {"srt", "vtt"}:
        raise ApiError(404, "not_found", "Unknown caption format.")
    job = _find_job(get_container(request), job_id)
    cues = build_cues(_words_or_error(job))
    body, media_type = (
        (to_srt(cues), "application/x-subrip") if fmt == "srt" else (to_webvtt(cues), "text/vtt")
    )
    return PlainTextResponse(
        body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="chaptercast-{job.id[:8]}.{fmt}"'},
    )
