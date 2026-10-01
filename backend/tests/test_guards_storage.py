from __future__ import annotations

import stat
from datetime import date, timedelta
from pathlib import Path

import pytest

from chaptercast.guards import DailyBudget, SlidingWindowLimiter
from chaptercast.storage import AudioStore

JOB_ID = "0123456789abcdef0123456789abcdef"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestLimiter:
    def test_allows_up_to_the_limit_then_blocks(self) -> None:
        clock = Clock()
        limiter = SlidingWindowLimiter(2, 60, clock=clock)
        assert limiter.allow("a") == (True, 0.0)
        assert limiter.allow("a") == (True, 0.0)
        allowed, wait = limiter.allow("a")
        assert not allowed
        assert 0 < wait <= 60

    def test_window_slides(self) -> None:
        clock = Clock()
        limiter = SlidingWindowLimiter(1, 60, clock=clock)
        limiter.allow("a")
        clock.now += 59
        assert not limiter.allow("a")[0]
        clock.now += 2
        assert limiter.allow("a")[0]

    def test_keys_are_independent(self) -> None:
        limiter = SlidingWindowLimiter(1, 60, clock=Clock())
        assert limiter.allow("a")[0]
        assert limiter.allow("b")[0]

    def test_retry_after_does_not_consume(self) -> None:
        limiter = SlidingWindowLimiter(1, 60, clock=Clock())
        for _ in range(5):
            assert limiter.retry_after("a") == 0.0
        assert limiter.allow("a")[0]

    def test_hit_consumes_without_checking(self) -> None:
        limiter = SlidingWindowLimiter(2, 60, clock=Clock())
        limiter.hit("a")
        limiter.hit("a")
        assert limiter.retry_after("a") > 0

    def test_memory_is_bounded(self) -> None:
        limiter = SlidingWindowLimiter(1, 60, clock=Clock(), max_keys=50)
        for i in range(500):
            limiter.hit(f"k{i}")
        assert len(limiter._events) <= 50


class TestBudget:
    def test_reserve_and_refund(self) -> None:
        budget = DailyBudget(100)
        assert budget.reserve(60)
        assert not budget.reserve(60)
        assert budget.remaining == 40
        budget.refund(60)
        assert budget.remaining == 100

    def test_refund_never_goes_negative(self) -> None:
        budget = DailyBudget(10)
        budget.refund(999)
        assert budget.remaining == 10

    def test_resets_at_the_day_boundary(self) -> None:
        day = [date(2026, 10, 1)]
        budget = DailyBudget(10, today=lambda: day[0])
        assert budget.reserve(10)
        assert budget.remaining == 0
        day[0] += timedelta(days=1)
        assert budget.remaining == 10


class TestStore:
    def test_write_is_atomic_and_private(self, tmp_path: Path) -> None:
        store = AudioStore(tmp_path / "audio")
        path = store.write(JOB_ID, "wav", b"data")
        assert path.read_bytes() == b"data"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        assert not list(store.root.glob("*.tmp"))

    @pytest.mark.parametrize("job_id", ["../etc/passwd", "short", "G" * 32, JOB_ID + "0", ""])
    def test_rejects_ids_that_are_not_server_generated(self, tmp_path: Path, job_id: str) -> None:
        with pytest.raises(ValueError, match="invalid"):
            AudioStore(tmp_path).write(job_id, "wav", b"x")

    def test_rejects_unknown_extensions(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="invalid"):
            AudioStore(tmp_path).write(JOB_ID, "sh", b"x")

    def test_delete_removes_the_file_and_tolerates_none(self, tmp_path: Path) -> None:
        store = AudioStore(tmp_path)
        path = store.write(JOB_ID, "mp3", b"x")
        store.delete(path)
        store.delete(None)
        assert not path.exists()

    def test_delete_refuses_paths_outside_the_root(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("keep me")
        store = AudioStore(tmp_path / "audio")
        store.delete(outside)
        assert outside.exists()

    def test_purge_orphans_only_touches_our_files(self, tmp_path: Path) -> None:
        store = AudioStore(tmp_path)
        store.write(JOB_ID, "wav", b"x")
        (tmp_path / f".{JOB_ID}.tmp").write_bytes(b"x")
        keep = tmp_path / "notes.txt"
        keep.write_text("keep")
        assert store.purge_orphans() == 2
        assert keep.exists()

    def test_is_writable(self, tmp_path: Path) -> None:
        assert AudioStore(tmp_path).is_writable() is True
