from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chaptercast.cache import ClipCache
from chaptercast.chunking import split_text
from chaptercast.errors import (
    BudgetExceededError,
    CapacityError,
    EmptyTextError,
    TextTooLongError,
)
from chaptercast.guards import DailyBudget
from chaptercast.jobs import Job, JobManager, JobRequest, JobStatus
from chaptercast.models import ELEVENLABS_MODELS, ModelSpec
from chaptercast.providers.base import ProviderRejectedError, VoiceSettings
from chaptercast.storage import AudioStore
from tests.conftest import (
    FAKE_CLIP_SECONDS,
    FAKE_MODELS,
    FakeProvider,
    fake_model,
    make_settings,
    read_wav_values,
)

# Distinct first letters per sentence let us verify output ordering from the audio itself.
STORY = " ".join(f"{letter}" + "x" * 38 + "." for letter in "ABCDEFGH")


MODELS = {m.model_id: m for m in FAKE_MODELS}


def req(
    text: str = STORY, *, model_id: str = "model1", settings: VoiceSettings | None = None
) -> JobRequest:
    return JobRequest(text, "voice1", MODELS[model_id], settings or VoiceSettings())


class Env:
    def __init__(
        self, tmp_path: Path, provider: FakeProvider, *, cache: bool = False, **overrides: object
    ) -> None:
        defaults: dict[str, object] = {"chunk_max_chars": 50}
        self.settings = make_settings(tmp_path, **{**defaults, **overrides})
        self.provider = provider
        self.store = AudioStore(self.settings.data_dir)
        self.budget = DailyBudget(self.settings.daily_char_budget)
        self.cache = ClipCache(
            self.settings.data_dir / "cache",
            max_bytes=(10 * 1024 * 1024) if cache else 0,
            ttl_seconds=3600,
        )
        self.now = 0.0
        self.manager = JobManager(
            provider=provider,
            store=self.store,
            budget=self.budget,
            settings=self.settings,
            cache=self.cache,
            clock=lambda: self.now,
        )

    async def finish(self, job: Job) -> Job:
        assert job.task is not None
        await asyncio.wait_for(asyncio.shield(job.task), timeout=5)
        return job


async def test_job_succeeds_and_writes_audio(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider())
    job = await env.finish(await env.manager.submit(req(STORY)))

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
    job = await env.finish(await env.manager.submit(req(STORY)))

    assert job.audio_path is not None
    values = read_wav_values(job.audio_path.read_bytes())
    assert [v for i, v in enumerate(values) if i % 10 == 0] == [ord(c[0]) for c in chunks]


async def test_neighbouring_text_is_passed_for_prosody_continuity(tmp_path: Path) -> None:
    provider = FakeProvider()
    env = Env(tmp_path, provider)
    await env.finish(await env.manager.submit(req(STORY)))
    chunks = split_text(STORY, 50)
    calls = {c["text"]: c for c in provider.calls}
    assert calls[chunks[0]]["previous"] is None
    assert calls[chunks[-1]]["next"] is None
    assert calls[chunks[1]]["previous"] == chunks[0][-300:]
    assert calls[chunks[1]]["next"] == chunks[2][:300]


async def test_outbound_concurrency_is_bounded(tmp_path: Path) -> None:
    provider = FakeProvider(delay=0.02)
    env = Env(tmp_path, provider, tts_concurrency=2)
    await env.finish(await env.manager.submit(req(STORY)))
    assert provider.max_in_flight == 2


async def test_provider_failure_fails_the_job_with_a_public_message(tmp_path: Path) -> None:
    provider = FakeProvider(
        fail_when=lambda t: ProviderRejectedError("internal detail") if t.startswith("C") else None
    )
    env = Env(tmp_path, provider)
    job = await env.finish(await env.manager.submit(req(STORY)))

    assert job.status is JobStatus.FAILED
    assert job.error_code == "provider_rejected"
    assert job.error_message is not None and "internal detail" not in job.error_message
    assert job.audio_path is None


async def test_unexpected_errors_are_reported_generically(tmp_path: Path) -> None:
    provider = FakeProvider(fail_when=lambda _t: RuntimeError("secret stack detail"))
    env = Env(tmp_path, provider)
    job = await env.finish(await env.manager.submit(req(STORY)))
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
    job = await env.finish(await env.manager.submit(req(STORY)))

    assert job.status is JobStatus.FAILED
    assert job.completed_chunks == 0
    assert env.budget.remaining == 10_000  # nothing was spent, everything refunded


async def test_delete_cancels_a_running_job_and_refunds_everything(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider(delay=5.0), daily_char_budget=10_000)
    job = await env.manager.submit(req(STORY))
    await asyncio.sleep(0.05)
    assert env.budget.remaining < 10_000

    assert await env.manager.delete(job.id) is True
    assert job.status is JobStatus.CANCELLED
    assert env.manager.get(job.id) is None
    assert env.budget.remaining == 10_000


async def test_delete_removes_the_audio_file(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider())
    job = await env.finish(await env.manager.submit(req(STORY)))
    path = job.audio_path
    assert path is not None and path.exists()
    await env.manager.delete(job.id)
    assert not path.exists()
    assert await env.manager.delete(job.id) is False


async def test_empty_text_is_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider())
    with pytest.raises(EmptyTextError):
        await env.manager.submit(req(" \n\t \x00 "))


async def test_text_over_the_limit_is_rejected_before_any_spend(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider(), max_chars_per_job=100, daily_char_budget=1000)
    with pytest.raises(TextTooLongError) as info:
        await env.manager.submit(req(STORY))
    assert info.value.limit == 100
    assert env.budget.remaining == 1000
    assert env.provider.calls == []


async def test_daily_budget_is_enforced(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider(delay=5.0), daily_char_budget=len(STORY))
    first = await env.manager.submit(req(STORY))
    with pytest.raises(BudgetExceededError):
        await env.manager.submit(req(STORY))
    await env.manager.delete(first.id)


async def test_active_job_cap_is_enforced(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider(delay=5.0), max_active_jobs=1)
    first = await env.manager.submit(req(STORY))
    with pytest.raises(CapacityError):
        await env.manager.submit(req(STORY))
    await env.manager.delete(first.id)


async def test_finished_jobs_expire_after_the_ttl(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider(), job_ttl_seconds=60)
    job = await env.finish(await env.manager.submit(req(STORY)))
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
    first = await env.finish(await env.manager.submit(req(STORY)))
    env.now += 1
    second = await env.finish(await env.manager.submit(req(STORY)))
    env.now += 1
    third = await env.finish(await env.manager.submit(req(STORY)))

    assert env.manager.get(first.id) is None
    assert env.manager.get(second.id) is not None
    assert env.manager.get(third.id) is not None


async def test_shutdown_cancels_work_and_cleans_up(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider(delay=5.0))
    running = await env.manager.submit(req(STORY))
    await asyncio.sleep(0.02)
    await env.manager.shutdown()
    assert running.status is JobStatus.CANCELLED
    assert env.manager.get(running.id) is None


# ----------------------------------------------------------------- smart re-generation
async def test_an_identical_job_is_served_entirely_from_the_cache(tmp_path: Path) -> None:
    provider = FakeProvider()
    env = Env(tmp_path, provider, cache=True, daily_char_budget=10_000)
    first = await env.finish(await env.manager.submit(req()))
    calls_after_first = len(provider.calls)
    remaining_after_first = env.budget.remaining

    second = await env.finish(await env.manager.submit(req()))
    assert second.status is JobStatus.SUCCEEDED
    assert len(provider.calls) == calls_after_first  # no new provider calls
    assert second.cached_chunks == second.total_chunks
    assert second.billed_chars == 0
    assert env.budget.remaining == remaining_after_first  # nothing charged
    assert first.audio_path is not None and second.audio_path is not None
    assert first.audio_path.read_bytes() == second.audio_path.read_bytes()


async def test_an_edit_only_regenerates_the_changed_chunk_and_its_neighbours(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    env = Env(tmp_path, provider, cache=True)
    await env.finish(await env.manager.submit(req()))
    chunks = split_text(STORY, 50)
    assert len(chunks) == 8  # one sentence per chunk

    edited = STORY.replace("D" + "x" * 38 + ".", "D" + "y" * 38 + ".")
    provider.calls.clear()
    job = await env.finish(await env.manager.submit(req(edited)))

    regenerated = sorted(c["text"][0] for c in provider.calls)
    assert regenerated == ["C", "D", "E"]  # D changed; C and E because their context did
    assert job.cached_chunks == 5
    assert job.billed_chars == sum(len(c) for c in split_text(edited, 50) if c[0] in "CDE")


@pytest.mark.parametrize(
    "variant",
    [
        {"model_id": "cheap"},
        {"settings": VoiceSettings(stability=0.2)},
        {"settings": VoiceSettings(speed=1.1)},
    ],
)
async def test_a_different_model_or_setting_is_a_cache_miss(
    tmp_path: Path, variant: dict[str, object]
) -> None:
    provider = FakeProvider()
    env = Env(tmp_path, provider, cache=True)
    await env.finish(await env.manager.submit(req()))
    provider.calls.clear()
    job = await env.finish(await env.manager.submit(req(**variant)))  # type: ignore[arg-type]
    assert job.cached_chunks == 0
    assert len(provider.calls) == job.total_chunks


async def test_the_plan_reports_cache_hits_without_spending(tmp_path: Path) -> None:
    provider = FakeProvider()
    env = Env(tmp_path, provider, cache=True, daily_char_budget=10_000)
    cold = env.manager.plan(req())
    assert cold.cached_chunks == 0
    assert cold.billable_chars == cold.char_count

    await env.finish(await env.manager.submit(req()))
    calls, remaining = len(provider.calls), env.budget.remaining
    warm = env.manager.plan(req())
    assert warm.cached_chunks == len(warm.chunks)
    assert warm.billable_chars == 0
    assert (len(provider.calls), env.budget.remaining) == (calls, remaining)


async def test_cached_jobs_still_run_when_the_daily_budget_is_spent(tmp_path: Path) -> None:
    total = sum(len(c) for c in split_text(STORY, 50))
    env = Env(tmp_path, FakeProvider(), cache=True, daily_char_budget=total)
    await env.finish(await env.manager.submit(req()))
    assert env.budget.remaining == 0
    again = await env.finish(await env.manager.submit(req()))
    assert again.status is JobStatus.SUCCEEDED


async def test_a_predicted_hit_that_vanished_is_charged_or_fails_cleanly(tmp_path: Path) -> None:
    total = sum(len(c) for c in split_text(STORY, 50))
    env = Env(tmp_path, FakeProvider(), cache=True, daily_char_budget=total)
    await env.finish(await env.manager.submit(req()))

    job = await env.manager.submit(req())  # planned as fully cached: reserves nothing
    env.cache.clear()  # ...but the entries disappear before it runs
    await env.finish(job)
    assert job.status is JobStatus.FAILED
    assert job.error_code == "daily_budget_exceeded"
    assert env.budget.remaining == 0  # nothing double-charged or leaked


async def test_unspent_reservations_are_refunded_after_success(tmp_path: Path) -> None:
    provider = FakeProvider()
    env = Env(tmp_path, provider, cache=True, daily_char_budget=10_000)
    job = await env.finish(await env.manager.submit(req()))
    assert env.budget.remaining == 10_000 - job.billed_chars == 10_000 - job.char_count


async def test_model_and_settings_reach_the_provider(tmp_path: Path) -> None:
    provider = FakeProvider()
    env = Env(tmp_path, provider)
    settings = VoiceSettings(stability=0.4, speed=0.9)
    await env.finish(await env.manager.submit(req(model_id="cheap", settings=settings)))
    assert {c["model_id"] for c in provider.calls} == {"cheap"}
    assert {c["voice_settings"] for c in provider.calls} == {settings}


# ------------------------------------------------------------------- transcript
async def test_word_timings_span_the_stitched_audio_in_order(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider())
    job = await env.finish(await env.manager.submit(req()))
    assert job.words is not None
    assert [w.text for w in job.words] == STORY.split()
    starts = [w.start for w in job.words]
    assert starts == sorted(starts)
    # Word i lives in clip i (one sentence per chunk), so it is offset by i clip lengths.
    # Timings are rounded to the millisecond, hence the tolerance.
    for i, word in enumerate(job.words):
        assert i * FAKE_CLIP_SECONDS - 0.0005 <= word.start < (i + 1) * FAKE_CLIP_SECONDS
    assert job.duration_seconds == pytest.approx(8 * FAKE_CLIP_SECONDS)


async def test_paragraphs_survive_chunk_boundaries(tmp_path: Path) -> None:
    first = "First paragraph sentence one. First paragraph sentence two."
    second = "Second paragraph sentence one. Second paragraph sentence two."
    env = Env(tmp_path, FakeProvider())
    job = await env.finish(await env.manager.submit(req(f"{first}\n\n{second}")))
    assert job.words is not None
    assert job.total_chunks > 2  # the paragraph break falls between chunks
    paragraphs = [w.paragraph for w in job.words]
    assert paragraphs == [0] * len(first.split()) + [1] * len(second.split())


async def test_no_transcript_without_alignment(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider(with_alignment=False))
    job = await env.finish(await env.manager.submit(req()))
    assert job.status is JobStatus.SUCCEEDED
    assert job.words is None


# ------------------------------------------------------------- model capabilities
NO_STITCHING = fake_model("solo", supports_context_stitching=False)
NO_TIMESTAMPS = fake_model("untimed", supports_timestamps=False)
TAGGER = MODELS["tagger"]


def req_for(model: ModelSpec, text: str = STORY) -> JobRequest:
    return JobRequest(text, "voice1", model, VoiceSettings())


async def test_a_model_without_stitching_gets_no_context(tmp_path: Path) -> None:
    provider = FakeProvider()
    env = Env(tmp_path, provider)
    await env.finish(await env.manager.submit(req_for(NO_STITCHING)))
    assert provider.calls
    assert all(c["previous"] is None and c["next"] is None for c in provider.calls)


async def test_without_stitching_an_edit_only_regenerates_the_edited_chunk(
    tmp_path: Path,
) -> None:
    """No context in the request means no context in the cache key either."""
    provider = FakeProvider()
    env = Env(tmp_path, provider, cache=True)
    await env.finish(await env.manager.submit(req_for(NO_STITCHING)))
    edited = STORY.replace("D" + "x" * 38 + ".", "D" + "y" * 38 + ".")
    provider.calls.clear()
    job = await env.finish(await env.manager.submit(req_for(NO_STITCHING, edited)))
    assert [c["text"][0] for c in provider.calls] == ["D"]  # neighbours C and E stay cached
    assert job.cached_chunks == job.total_chunks - 1


async def test_a_model_without_timestamps_falls_back_to_plain_audio(tmp_path: Path) -> None:
    provider = FakeProvider()
    env = Env(tmp_path, provider)
    job = await env.finish(await env.manager.submit(req_for(NO_TIMESTAMPS)))
    assert job.status is JobStatus.SUCCEEDED
    assert {c["with_timestamps"] for c in provider.calls} == {False}
    assert job.words is None  # no read-along rather than a wrong one
    assert job.audio_path is not None and job.audio_path.exists()


async def test_models_with_timestamps_ask_for_them(tmp_path: Path) -> None:
    provider = FakeProvider()
    env = Env(tmp_path, provider)
    await env.finish(await env.manager.submit(req()))
    assert {c["with_timestamps"] for c in provider.calls} == {True}


async def test_chunks_respect_the_models_own_request_limit(tmp_path: Path) -> None:
    small = fake_model("small", max_chars_per_request=45)
    env = Env(tmp_path, FakeProvider(), chunk_max_chars=900)
    plan = env.manager.plan(req_for(small))
    assert plan.chunks and all(len(c.text) <= 45 for c in plan.chunks)


# ---------------------------------------------------------------- expression tags
TAGGED = "[warm] It was a quiet morning. [whispered] Nobody else was awake."


async def test_tags_are_removed_for_models_that_would_read_them(tmp_path: Path) -> None:
    provider = FakeProvider()
    env = Env(tmp_path, provider)
    plan = env.manager.plan(req(TAGGED))
    assert plan.tags_ignored
    assert [c.text for c in plan.chunks] == ["It was a quiet morning. Nobody else was awake."]
    await env.finish(await env.manager.submit(req(TAGGED)))
    assert all("[" not in c["text"] for c in provider.calls)


async def test_stripped_tags_do_not_change_the_cache_key(tmp_path: Path) -> None:
    env = Env(tmp_path, FakeProvider())
    plain = "It was a quiet morning. Nobody else was awake."
    assert [r.key() for r in env.manager.plan(req(TAGGED)).requests] == [
        r.key() for r in env.manager.plan(req(plain)).requests
    ]


async def test_tags_are_sent_billed_and_kept_out_of_the_transcript(tmp_path: Path) -> None:
    provider = FakeProvider()
    env = Env(tmp_path, provider, daily_char_budget=10_000)
    plan = env.manager.plan(req_for(TAGGER, TAGGED))
    assert not plan.tags_ignored
    # Tags are billed like text (seen on the live API); only the joining spaces are not sent.
    assert plan.billable_chars == sum(len(c.text) for c in plan.chunks) == len(TAGGED) - 1
    job = await env.finish(await env.manager.submit(req_for(TAGGER, TAGGED)))
    assert "".join(c["text"] for c in provider.calls).count("[") == 2
    assert job.billed_chars == plan.billable_chars
    assert job.words is not None
    assert [w.text for w in job.words] == [
        "It",
        "was",
        "a",
        "quiet",
        "morning.",
        "Nobody",
        "else",
        "was",
        "awake.",
    ]


# ------------------------------------------------------------ estimates and budget
async def test_the_estimate_reserves_exactly_what_the_plan_bills(tmp_path: Path) -> None:
    v4 = ELEVENLABS_MODELS["eleven_v4"]
    env = Env(tmp_path, FakeProvider(delay=5.0), daily_char_budget=10_000)
    plan = env.manager.plan(req_for(v4))
    assert plan.billable_chars == plan.char_count == sum(len(c) for c in split_text(STORY, 50))
    job = await env.manager.submit(req_for(v4))
    assert env.budget.remaining == 10_000 - plan.billable_chars
    await env.manager.delete(job.id)


async def test_a_failed_v4_job_refunds_its_unused_reservation(tmp_path: Path) -> None:
    v4 = ELEVENLABS_MODELS["eleven_v4"]

    def fail_late(text: str) -> Exception | None:
        return ProviderRejectedError("no") if text.startswith("H") else None

    provider = FakeProvider(fail_when=fail_late, delay=lambda t: 0.2 if t.startswith("H") else 0)
    env = Env(tmp_path, provider, tts_concurrency=8, daily_char_budget=10_000)
    job = await env.finish(await env.manager.submit(req_for(v4)))
    assert job.status is JobStatus.FAILED
    assert 0 < job.billed_chars < job.char_count  # some chunks were paid for, not all
    assert env.budget.remaining == 10_000 - job.billed_chars
