"""Job lifecycle: submit, run concurrently, report progress, cancel, expire.

Design notes
------------
* One asyncio task per job. A job slot semaphore bounds how many jobs run at
  once; a *shared* TTS semaphore bounds outbound provider calls across all
  jobs (providers enforce per-plan concurrency limits).
* Chunks run concurrently inside a ``TaskGroup``: the first failure cancels the
  siblings, so a doomed job stops spending credits immediately.
* Every chunk is looked up in a content-addressed cache first (see cache.py).
  Only chunks that miss are reserved against the daily budget and sent to the
  provider, so re-generating an edited chapter pays only for what changed.
* Characters are reserved against the daily budget at submit time and anything
  not actually billed (failures, cancellations, late cache hits) is refunded.
* When every clip carries character timings, they are merged into chapter-level
  word timings for the read-along transcript and caption exports.
* State is in memory. That is a conscious trade-off for a single-instance demo;
  docs/ARCHITECTURE.md describes the path to a queue plus object storage.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from chaptercast.audio import AudioFormat, estimate_duration_seconds, stitch
from chaptercast.cache import ClipCache, ClipRequest
from chaptercast.chunking import Chunk, split_chunks
from chaptercast.config import Settings
from chaptercast.errors import (
    BudgetExceededError,
    CapacityError,
    EmptyTextError,
    TextTooLongError,
)
from chaptercast.guards import DailyBudget
from chaptercast.providers.base import AudioClip, ProviderError, TTSProvider, VoiceSettings
from chaptercast.storage import AudioStore
from chaptercast.transcript import Segment, Word, build_words

log = logging.getLogger(__name__)

_CONTEXT_CHARS = 300  # how much neighbouring text is sent for prosody continuity


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


@dataclass(frozen=True)
class JobRequest:
    text: str
    voice_id: str
    model_id: str
    voice_settings: VoiceSettings = field(default_factory=VoiceSettings)


@dataclass(frozen=True)
class Plan:
    """How a request would be executed: its chunks, their cache keys and which are cached."""

    chunks: list[Chunk]
    requests: list[ClipRequest]
    cached: list[bool]

    @property
    def char_count(self) -> int:
        return sum(len(c.text) for c in self.chunks)

    @property
    def cached_chunks(self) -> int:
        return sum(self.cached)

    @property
    def billable_chars(self) -> int:
        return sum(len(c.text) for c, hit in zip(self.chunks, self.cached, strict=True) if not hit)


@dataclass
class Job:
    id: str
    voice_id: str
    model_id: str
    char_count: int
    total_chunks: int
    created_at: datetime
    status: JobStatus = JobStatus.QUEUED
    completed_chunks: int = 0
    cached_chunks: int = 0
    reserved_chars: int = 0
    billed_chars: int = 0
    error_code: str | None = None
    error_message: str | None = None
    audio_path: Path | None = None
    audio_format: AudioFormat | None = None
    duration_seconds: float | None = None
    words: list[Word] | None = field(default=None, repr=False)
    finished_at: float | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)


def _first_leaf(group: BaseExceptionGroup[Exception]) -> BaseException:
    exc: BaseException = group
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


class JobManager:
    def __init__(
        self,
        *,
        provider: TTSProvider,
        store: AudioStore,
        budget: DailyBudget,
        settings: Settings,
        cache: ClipCache | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._store = store
        self._budget = budget
        self._settings = settings
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._job_slots = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._tts_slots = asyncio.Semaphore(settings.tts_concurrency)

    # ------------------------------------------------------------------ queries
    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def active_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status not in TERMINAL)

    def plan(self, request: JobRequest) -> Plan:
        """Chunk the text and check the cache. Pure lookup: no spend, no provider call."""
        chunks = split_chunks(request.text, self._settings.chunk_max_chars)
        fmt = self._provider.output_format
        format_name = f"{fmt.extension}_{fmt.bitrate_kbps or 0}"
        requests = [
            ClipRequest(
                provider=self._provider.name,
                output_format=format_name,
                model_id=request.model_id,
                voice_id=request.voice_id,
                voice_settings=request.voice_settings,
                text=chunk.text,
                previous_text=chunks[i - 1].text[-_CONTEXT_CHARS:] if i > 0 else None,
                next_text=chunks[i + 1].text[:_CONTEXT_CHARS] if i + 1 < len(chunks) else None,
            )
            for i, chunk in enumerate(chunks)
        ]
        cache = self._cache
        cached = [cache is not None and cache.contains(r.key()) for r in requests]
        return Plan(chunks, requests, cached)

    # ----------------------------------------------------------------- commands
    async def submit(self, request: JobRequest) -> Job:
        plan = await asyncio.to_thread(self.plan, request)
        if not plan.chunks:
            raise EmptyTextError("No speakable text")
        if plan.char_count > self._settings.max_chars_per_job:
            raise TextTooLongError(self._settings.max_chars_per_job, plan.char_count)
        if self.active_count() >= self._settings.max_active_jobs:
            raise CapacityError
        self._evict_oldest_finished_if_full()
        if not self._budget.reserve(plan.billable_chars):
            raise BudgetExceededError(self._budget.remaining)

        job = Job(
            id=uuid.uuid4().hex,
            voice_id=request.voice_id,
            model_id=request.model_id,
            char_count=plan.char_count,
            total_chunks=len(plan.chunks),
            created_at=datetime.now(UTC),
            reserved_chars=plan.billable_chars,
        )
        self._jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, request, plan), name=f"job-{job.id}")
        log.info(
            "job submitted",
            extra={
                "job_id": job.id,
                "chunks": len(plan.chunks),
                "chars": plan.char_count,
                "cached_chunks": plan.cached_chunks,
                "model_id": request.model_id,
            },
        )
        return job

    async def delete(self, job_id: str) -> bool:
        """Cancel if running, remove audio, forget the job."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.task is not None and not job.task.done():
            job.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job.task
        self._store.delete(job.audio_path)
        self._jobs.pop(job_id, None)
        return True

    async def purge_expired(self) -> int:
        now = self._clock()
        expired = [
            j.id
            for j in self._jobs.values()
            if j.finished_at is not None and now - j.finished_at > self._settings.job_ttl_seconds
        ]
        for job_id in expired:
            await self.delete(job_id)
        return len(expired)

    async def shutdown(self) -> None:
        tasks = [j.task for j in self._jobs.values() if j.task and not j.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in self._jobs.values():
            self._store.delete(job.audio_path)
        self._jobs.clear()

    # ---------------------------------------------------------------- internals
    def _evict_oldest_finished_if_full(self) -> None:
        if len(self._jobs) < self._settings.max_stored_jobs:
            return
        finished = sorted(
            (j for j in self._jobs.values() if j.finished_at is not None),
            key=lambda j: j.finished_at or 0.0,
        )
        if not finished:
            raise CapacityError
        victim = finished[0]
        self._store.delete(victim.audio_path)
        self._jobs.pop(victim.id, None)

    def _finish(self, job: Job, status: JobStatus) -> None:
        job.status = status
        job.finished_at = self._clock()

    def _fail(self, job: Job, exc: BaseException) -> None:
        if isinstance(exc, BudgetExceededError):
            job.error_code = "daily_budget_exceeded"
            job.error_message = "The daily character budget ran out during this job."
            log.warning("job failed", extra={"job_id": job.id, "code": job.error_code})
        elif isinstance(exc, ProviderError):
            job.error_code, job.error_message = exc.code, exc.public_message
            log.warning(
                "job failed",
                extra={"job_id": job.id, "code": exc.code, "provider_status": exc.status_code},
            )
        else:
            job.error_code = "internal_error"
            job.error_message = "Audio generation failed unexpectedly."
            log.error("job crashed", extra={"job_id": job.id}, exc_info=exc)
        self._finish(job, JobStatus.FAILED)

    async def _synth(
        self, job: Job, request: JobRequest, plan: Plan, i: int, out: list[AudioClip | None]
    ) -> None:
        clip_request = plan.requests[i]
        key = clip_request.key()
        cache = self._cache
        clip = await asyncio.to_thread(cache.get, key) if cache is not None else None
        if clip is not None:
            job.cached_chunks += 1
        else:
            chars = len(clip_request.text)
            if plan.cached[i]:
                # Predicted hit that expired or was evicted since submit: pay for it now.
                if not self._budget.reserve(chars):
                    raise BudgetExceededError(self._budget.remaining)
                job.reserved_chars += chars
            async with self._tts_slots:
                clip = await self._provider.synthesize(
                    clip_request.text,
                    request.voice_id,
                    model_id=request.model_id,
                    voice_settings=request.voice_settings,
                    previous_text=clip_request.previous_text,
                    next_text=clip_request.next_text,
                )
            job.billed_chars += chars
            if cache is not None:
                await asyncio.to_thread(cache.put, key, clip)
        out[i] = clip
        job.completed_chunks += 1

    def _transcript(self, plan: Plan, clips: list[AudioClip]) -> list[Word] | None:
        segments = []
        for chunk, clip in zip(plan.chunks, clips, strict=True):
            duration = estimate_duration_seconds(clip.data, clip.fmt)
            if clip.alignment is None or duration is None:
                return None
            segments.append(Segment(clip.alignment, duration, chunk.starts_paragraph))
        return build_words(segments)

    async def _run(self, job: Job, request: JobRequest, plan: Plan) -> None:
        results: list[AudioClip | None] = [None] * len(plan.chunks)
        try:
            async with self._job_slots:
                job.status = JobStatus.RUNNING
                async with asyncio.TaskGroup() as group:
                    for i in range(len(plan.chunks)):
                        group.create_task(self._synth(job, request, plan, i, results))
                clips = [c for c in results if c is not None]
                fmt = self._provider.output_format
                data = await stitch([c.data for c in clips], fmt)
                job.audio_path = await asyncio.to_thread(
                    self._store.write, job.id, fmt.extension, data
                )
                job.audio_format = fmt
                job.duration_seconds = estimate_duration_seconds(data, fmt)
                job.words = self._transcript(plan, clips)
                self._finish(job, JobStatus.SUCCEEDED)
                log.info(
                    "job succeeded",
                    extra={
                        "job_id": job.id,
                        "cached_chunks": job.cached_chunks,
                        "billed_chars": job.billed_chars,
                    },
                )
        except asyncio.CancelledError:
            self._finish(job, JobStatus.CANCELLED)
            raise
        except BaseExceptionGroup as group_exc:
            self._fail(job, _first_leaf(group_exc))
        except Exception as exc:
            self._fail(job, exc)
        finally:
            self._budget.refund(job.reserved_chars - job.billed_chars)
