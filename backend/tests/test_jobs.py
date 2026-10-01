from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chaptercast.chunking import split_text
from chaptercast.errors import (
    BudgetExceededError,
    CapacityError,
    EmptyTextError,
    TextTooLongError,
)
from chaptercast.guards import DailyBudget
from chaptercast.jobs import Job, JobManager, JobStatus
from chaptercast.providers.base import ProviderRejectedError
from chaptercast.storage import AudioStore
from tests.conftest import FakeProvider, make_settings, read_wav_values

# Distinct first letters per sentence let us verify output ordering from the audio itself.
STORY = " ".join(f"{letter}" + "x" * 38 + "." for letter in "ABCDEFGH")


class Env:
    def __init__(self, tmp_path: Path, provider: FakeProvider, **overrides: object) -> None:
        defaults: dict[str, object] = {"chunk_max_chars": 50}
        self.settings = make_settings(tmp_path, **{**defaults, **overrides})
        self.provider = provider
        self.store = AudioStore(self.settings.data_dir)
        self.budget = DailyBudget(self.settings.daily_char_budget)
        self.now = 0.0
        self.manager = JobManager(
            provider=provider,
            store=self.store,
            budget=self.budget,
            settings=self.settings,
            clock=lambda: self.now,
        )

    async def finish(self, job: Job) -> Job:
        assert job.task is not None
        await asyncio.wait_for(asyncio.shield(job.task), timeout=5)
        return job


async def test_job_succeeds_and_writes_audio(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider())
    job = await env.finish(await env.manager.submit(STORY, "voice1"))

    assert job.status is JobStatus.SUCCEEDED
    assert job.completed_chunks == job.total_chunks == len(split_text(STORY, 50))
    assert job.audio_path is not None and job.audio_path.exists()
    assert job.duration_seconds is not None and job.duration_seconds > 0
    assert job.error_code is None


async def test_chunks_are_stitched_in_order_even_when_they_finish_out_of_order(
    tmp_path: Path,
) -> None:
    chunks = split_text(STORY, 50)
    # Earlier chunks are slower, so completion order is the reverse of text order.
    delays = {c: 0.02 * (len(chunks) - i) for i, c in enumerate(chunks)}
    env = Env(tmp_path, FakeProvider(delay=lambda text: delays[text]), tts_concurrency=8)
    job = await env.finish(await env.manager.submit(STORY, "voice1"))

    assert job.audio_path is not None
    values = read_wav_values(job.audio_path.read_bytes())
    assert [v for i, v in enumerate(values) if i % 10 == 0] == [ord(c[0]) for c in chunks]


async def test_neighbouring_text_is_passed_for_prosody_continuity(tmp_path: Path) -> None:
    provider = FakeProvider()
    env = Env(tmp_path, provider)
    await env.finish(await env.manager.submit(STORY, "voice1"))
    chunks = split_text(STORY, 50)
    calls = {c["text"]: c for c in provider.calls}
    assert calls[chunks[0]]["previous"] is None
    assert calls[chunks[-1]]["next"] is None
    assert calls[chunks[1]]["previous"] == chunks[0][-300:]
    assert calls[chunks[1]]["next"] == chunks[2][:300]


async def test_outbound_concurrency_is_bounded(tmp_path: Path) -> None:
    provider = FakeProvider(delay=0.02)
    env = Env(tmp_path, provider, tts_concurrency=2)
    await env.finish(await env.manager.submit(STORY, "voice1"))
    assert provider.max_in_flight == 2


async def test_provider_failure_fails_the_job_with_a_public_message(tmp_path: Path) -> None:
    provider = FakeProvider(
        fail_when=lambda t: ProviderRejectedError("internal detail") if t.startswith("C") else None
    )
    env = Env(tmp_path, provider)
    job = await env.finish(await env.manager.submit(STORY, "voice1"))

    assert job.status is JobStatus.FAILED
    assert job.error_code == "provider_rejected"
    assert job.error_message is not None and "internal detail" not in job.error_message
    assert job.audio_path is None


async def test_unexpected_errors_are_reported_generically(tmp_path: Path) -> None:
    provider = FakeProvider(fail_when=lambda _t: RuntimeError("secret stack detail"))
    env = Env(tmp_path, provider)
    job = await env.finish(await env.manager.submit(STORY, "voice1"))
    assert job.status is JobStatus.FAILED
    assert job.error_code == "internal_error"
    assert "secret" not in (job.error_message or "")


async def test_first_failure_stops_sibling_chunks_and_refunds_unspent_budget(
    tmp_path: Path,
) -> None:
    def fail_first(text: str) -> Exception | None:
        return ProviderRejectedError("no") if text.startswith("A") else None

    provider = FakeProvider(delay=lambda t: 0.0 if t.startswith("A") else 5.0, fail_when=fail_first)
    env = Env(tmp_path, provider, tts_concurrency=8, daily_char_budget=10_000)
    job = await env.finish(await env.manager.submit(STORY, "voice1"))

    assert job.status is JobStatus.FAILED
    assert job.completed_chunks == 0
    assert env.budget.remaining == 10_000  # nothing was spent, everything refunded


async def test_delete_cancels_a_running_job_and_refunds_everything(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider(delay=5.0), daily_char_budget=10_000)
    job = await env.manager.submit(STORY, "voice1")
    await asyncio.sleep(0.05)
    assert env.budget.remaining < 10_000

    assert await env.manager.delete(job.id) is True
    assert job.status is JobStatus.CANCELLED
    assert env.manager.get(job.id) is None
    assert env.budget.remaining == 10_000


async def test_delete_removes_the_audio_file(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider())
    job = await env.finish(await env.manager.submit(STORY, "voice1"))
    path = job.audio_path
    assert path is not None and path.exists()
    await env.manager.delete(job.id)
    assert not path.exists()
    assert await env.manager.delete(job.id) is False


async def test_empty_text_is_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider())
    with pytest.raises(EmptyTextError):
        await env.manager.submit(" \n\t \x00 ", "voice1")


async def test_text_over_the_limit_is_rejected_before_any_spend(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider(), max_chars_per_job=100, daily_char_budget=1000)
    with pytest.raises(TextTooLongError) as info:
        await env.manager.submit(STORY, "voice1")
    assert info.value.limit == 100
    assert env.budget.remaining == 1000
    assert env.provider.calls == []


async def test_daily_budget_is_enforced(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider(delay=5.0), daily_char_budget=len(STORY))
    first = await env.manager.submit(STORY, "voice1")
    with pytest.raises(BudgetExceededError):
        await env.manager.submit(STORY, "voice1")
    await env.manager.delete(first.id)


async def test_active_job_cap_is_enforced(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider(delay=5.0), max_active_jobs=1)
    first = await env.manager.submit(STORY, "voice1")
    with pytest.raises(CapacityError):
        await env.manager.submit(STORY, "voice1")
    await env.manager.delete(first.id)


async def test_finished_jobs_expire_after_the_ttl(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider(), job_ttl_seconds=60)
    job = await env.finish(await env.manager.submit(STORY, "voice1"))
    path = job.audio_path
    assert path is not None

    env.now += 30
    assert await env.manager.purge_expired() == 0
    assert path.exists()

    env.now += 40
    assert await env.manager.purge_expired() == 1
    assert not path.exists()
    assert env.manager.get(job.id) is None


async def test_oldest_finished_job_is_evicted_when_storage_is_full(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider(), max_stored_jobs=2)
    first = await env.finish(await env.manager.submit(STORY, "voice1"))
    env.now += 1
    second = await env.finish(await env.manager.submit(STORY, "voice1"))
    env.now += 1
    third = await env.finish(await env.manager.submit(STORY, "voice1"))

    assert env.manager.get(first.id) is None
    assert env.manager.get(second.id) is not None
    assert env.manager.get(third.id) is not None


async def test_shutdown_cancels_work_and_cleans_up(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider(delay=5.0))
    running = await env.manager.submit(STORY, "voice1")
    await asyncio.sleep(0.02)
    await env.manager.shutdown()
    assert running.status is JobStatus.CANCELLED
    assert env.manager.get(running.id) is None
