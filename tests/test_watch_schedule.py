"""The watch keeps running when the Mac sleeps (docs/RETOURS_2026-10-27.md, lot 1). Simulated clock, no network."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from goulot.api.context import AppContext
from goulot.collect.base import iso
from goulot.collect.runner import CycleResult, material_watch
from goulot.pipeline import cycle
from goulot.security.clock import ManualClock
from goulot.server import TICK_MINUTES, schedule
from tests.test_collect import validated_chain


def now_of(clock: ManualClock) -> datetime:
    return datetime.fromtimestamp(clock.time(), UTC)


def record_cycle(ctx: AppContext, started: datetime, finished: bool = True, ok: bool = True) -> None:
    with ctx.db.transaction() as conn:
        conn.execute("INSERT INTO cycles (id, started_at, finished_at, ok) VALUES (?, ?, ?, ?)",
                     (str(uuid.uuid4()), iso(started), iso(started + timedelta(minutes=5)) if finished else None,
                      int(ok)))


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch, ctx: AppContext) -> list[datetime]:
    """Replaces the collection, prices and AI steps: each cycle only records itself, as the real one does."""
    calls: list[datetime] = []

    def fake_run_cycle(db: Any, http: Any, secret: Any, now: datetime, private: Any = None) -> CycleResult:
        calls.append(now)
        record_cycle(ctx, now)
        return CycleResult(cycle_id="test", ok=True)

    monkeypatch.setattr(cycle, "run_cycle", fake_run_cycle)
    monkeypatch.setattr(cycle.price_collect, "run", lambda *a, **k: None)
    monkeypatch.setattr(cycle, "process", lambda *a, **k: None)
    monkeypatch.setattr(cycle, "private_watch", lambda *a, **k: None)
    return calls


def test_jobs_tick_every_10_minutes_and_run_missed_ticks_on_waking(ctx: AppContext) -> None:
    scheduler = BackgroundScheduler(timezone="UTC")
    schedule(scheduler, ctx, watch=True)
    jobs = {j.id: j for j in scheduler.get_jobs()}
    assert set(jobs) == {"watch_cycle", "daily_backup"}
    for job in jobs.values():
        assert job.trigger.interval == timedelta(minutes=TICK_MINUTES) <= timedelta(minutes=10)
        assert job.misfire_grace_time is None and job.coalesce is True and job.max_instances == 1
    assert jobs["watch_cycle"].func is cycle.tick


def test_watch_off_keeps_only_the_backup(ctx: AppContext) -> None:
    scheduler = BackgroundScheduler(timezone="UTC")
    schedule(scheduler, ctx, watch=False)
    assert [j.id for j in scheduler.get_jobs()] == ["daily_backup"]


def test_cycle_runs_on_the_first_tick_after_a_7_hour_sleep(ctx: AppContext, clock: ManualClock,
                                                           started: list[datetime]) -> None:
    assert cycle.tick(ctx) is not None and len(started) == 1
    clock.advance(10 * 60)
    assert cycle.tick(ctx) is None  # 10 minutes later: not due
    clock.sleep_jump(7 * 3600)      # the Mac sleeps 7 hours; the next tick runs on waking
    assert cycle.tick(ctx) is not None and len(started) == 2


def test_cycle_due_after_2h45(ctx: AppContext, clock: ManualClock, started: list[datetime]) -> None:
    record_cycle(ctx, now_of(clock))
    clock.advance(2 * 3600 + 44 * 60)
    assert cycle.tick(ctx) is None
    clock.advance(60)
    assert cycle.tick(ctx) is not None and len(started) == 1


def test_crashed_cycle_retried_after_30_minutes_not_every_tick(ctx: AppContext, clock: ManualClock,
                                                               started: list[datetime]) -> None:
    record_cycle(ctx, now_of(clock) - timedelta(hours=5))
    record_cycle(ctx, now_of(clock), finished=False)
    clock.advance(20 * 60)
    assert cycle.tick(ctx) is None
    clock.advance(10 * 60)
    assert cycle.tick(ctx) is not None and len(started) == 1


def test_two_simultaneous_ticks_start_one_cycle(monkeypatch: pytest.MonkeyPatch, ctx: AppContext,
                                                started: list[datetime]) -> None:
    inside, release = threading.Event(), threading.Event()
    recording = cycle.run_cycle

    def slow_run_cycle(*args: Any, **kwargs: Any) -> CycleResult:
        inside.set()
        assert release.wait(5)
        return recording(*args, **kwargs)

    monkeypatch.setattr(cycle, "run_cycle", slow_run_cycle)
    first = threading.Thread(target=cycle.tick, args=(ctx,))
    first.start()
    assert inside.wait(5)
    assert cycle.tick(ctx) is None  # the second tick finds the cycle running
    release.set()
    first.join(5)
    assert cycle.tick(ctx) is None  # and a late tick finds it finished: not due
    assert len(started) == 1


def test_dashboard_watch_state_per_material(ctx: AppContext, clock: ManualClock) -> None:
    material_id = validated_chain(ctx)
    now = now_of(clock)
    with ctx.db.connect() as conn:
        material = {"id": material_id, "status": "validated"}
        assert material_watch(conn, material, now)["state"] == "pending"
        assert material_watch(conn, {**material, "status": "review"}, now) == {
            "state": "chain_to_validate", "last_cycle": None, "hours_ago": None}
    record_cycle(ctx, now - timedelta(hours=20))
    record_cycle(ctx, now - timedelta(hours=3), ok=False)
    with ctx.db.connect() as conn:
        late = material_watch(conn, material, now)
    assert late["state"] == "late" and late["hours_ago"] == 20
    record_cycle(ctx, now - timedelta(hours=2, minutes=30))
    with ctx.db.connect() as conn:
        active = material_watch(conn, material, now)
    assert active["state"] == "active" and active["hours_ago"] == 2
