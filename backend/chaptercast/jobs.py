"""Job lifecycle: submit, run concurrently, report progress, cancel, expire.

Design notes
------------
* One asyncio task per job. A job slot semaphore bounds how many jobs run at
  once; a *shared* TTS semaphore bounds outbound provider calls across all
  jobs (providers enforce per-plan concurrency limits).
* Chunks run concurrently inside a ``TaskGroup``: the first failure cancels the
  siblings, so a doomed job stops spending credits immediately.
* Characters are reserved against the daily budget at submit time and refunded
  for chunks that did not complete when a job fails or is cancelled.
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
from chaptercast.chunking import split_text
from chaptercast.config import Settings
from chaptercast.errors import (
    BudgetExceededError,
    CapacityError,
    EmptyTextError,
    TextTooLongError,
)
from chaptercast.guards import DailyBudget
from chaptercast.providers.base import AudioClip, ProviderError, TTSProvider
from chaptercast.storage import AudioStore

log = logging.getLogger(__name__)

_CONTEXT_CHARS = 300  # how much neighbouring text is sent for prosody continuity


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


@dataclass
class Job:
    id: str
    voice_id: str
    char_count: int
    total_chunks: int
    created_at: datetime
    status: JobStatus = JobStatus.QUEUED
    completed_chunks: int = 0
    spent_chars: int = 0
    error_code: str | None = None
    error_message: str | None = None
    audio_path: Path | None = None
    audio_format: AudioFormat | None = None
    duration_seconds: float | None = None
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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
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

    # ----------------------------------------------------------------- commands
    async def submit(self, text: str, voice_id: str) -> Job:
        chunks = split_text(text, self._settings.chunk_max_chars)
        if not chunks:
            raise EmptyTextError("No speakable text")
        char_count = sum(len(c) for c in chunks)
        if char_count > self._settings.max_chars_per_job:
            raise TextTooLongError(self._settings.max_chars_per_job, char_count)
        if self.active_count() >= self._settings.max_active_jobs:
            raise CapacityError
        self._evict_oldest_finished_if_full()
        if not self._budget.reserve(char_count):
            raise BudgetExceededError(self._budget.remaining)

        job = Job(
            id=uuid.uuid4().hex,
            voice_id=voice_id,
            char_count=char_count,
            total_chunks=len(chunks),
            created_at=datetime.now(UTC),
        )
        self._jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, chunks), name=f"job-{job.id}")
        log.info(
            "job submitted", extra={"job_id": job.id, "chunks": len(chunks), "chars": char_count}
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
        if isinstance(exc, ProviderError):
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
        self, job: Job, chunks: list[str], i: int, out: list[AudioClip | None]
    ) -> None:
        previous = chunks[i - 1][-_CONTEXT_CHARS:] if i > 0 else None
        following = chunks[i + 1][:_CONTEXT_CHARS] if i + 1 < len(chunks) else None
        async with self._tts_slots:
            clip = await self._provider.synthesize(
                chunks[i], job.voice_id, previous_text=previous, next_text=following
            )
        out[i] = clip
        job.completed_chunks += 1
        job.spent_chars += len(chunks[i])

    async def _run(self, job: Job, chunks: list[str]) -> None:
        results: list[AudioClip | None] = [None] * len(chunks)
        try:
            async with self._job_slots:
                job.status = JobStatus.RUNNING
                async with asyncio.TaskGroup() as group:
                    for i in range(len(chunks)):
                        group.create_task(self._synth(job, chunks, i, results))
                clips = [c for c in results if c is not None]
                fmt = self._provider.output_format
                data = await stitch([c.data for c in clips], fmt)
                job.audio_path = await asyncio.to_thread(
                    self._store.write, job.id, fmt.extension, data
                )
                job.audio_format = fmt
                job.duration_seconds = estimate_duration_seconds(data, fmt)
                self._finish(job, JobStatus.SUCCEEDED)
                log.info("job succeeded", extra={"job_id": job.id})
        except asyncio.CancelledError:
            self._finish(job, JobStatus.CANCELLED)
            raise
        except BaseExceptionGroup as group_exc:
            self._fail(job, _first_leaf(group_exc))
        except Exception as exc:
            self._fail(job, exc)
        finally:
            if job.status is not JobStatus.SUCCEEDED:
                self._budget.refund(job.char_count - job.spent_chars)
