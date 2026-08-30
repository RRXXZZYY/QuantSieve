from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
from typing import Any, Literal

import pytest
from pydantic import ValidationError
from quantsieve_api.portfolio_paper_scheduler import (
    PortfolioPaperOpeningScheduler,
    PortfolioPaperOpeningSchedulerStatus,
)
from quantsieve_api.portfolio_paper_service import PortfolioPaperOpeningRunResult

TRACK_ID = "b" * 32
BATCH_ID = "a" * 64
NOW = datetime(2026, 7, 26, 20, 0, tzinfo=UTC)


def opening_result(
    status: Literal["committed", "failed", "stale"],
    *,
    error_recorded: bool = False,
) -> PortfolioPaperOpeningRunResult:
    if status == "committed":
        return PortfolioPaperOpeningRunResult(
            status=status,
            track_id=TRACK_ID,
            batch_id=BATCH_ID,
            fill_count=2,
        )
    return PortfolioPaperOpeningRunResult(
        status=status,
        track_id=TRACK_ID,
        error_recorded=error_recorded,
    )


class StepSleeper:
    def __init__(self) -> None:
        self.entered: asyncio.Queue[tuple[float, asyncio.Event]] = asyncio.Queue()

    async def __call__(self, seconds: float) -> None:
        gate = asyncio.Event()
        await self.entered.put((seconds, gate))
        await gate.wait()

    async def next_gate(self, expected_seconds: float = 1.0) -> asyncio.Event:
        seconds, gate = await asyncio.wait_for(self.entered.get(), timeout=1)
        assert seconds == expected_seconds
        return gate


class NoCallService:
    async def run_once(self) -> PortfolioPaperOpeningRunResult | None:
        raise AssertionError("A disabled scheduler must not call its service.")


class CancellationSafeBlockingService:
    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.maximum_active = 0
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.allow_cleanup = asyncio.Event()
        self.cleanup_finished = asyncio.Event()

    async def run_once(self) -> PortfolioPaperOpeningRunResult | None:
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.allow_cleanup.wait()
            self.cleanup_finished.set()
            raise
        finally:
            self.active -= 1


class CountingService:
    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.maximum_active = 0

    async def run_once(self) -> PortfolioPaperOpeningRunResult | None:
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        return None


class CleanupErrorService(CancellationSafeBlockingService):
    async def run_once(self) -> PortfolioPaperOpeningRunResult | None:
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.allow_cleanup.wait()
            self.cleanup_finished.set()
            raise RuntimeError("quote_hash=must-not-escape") from None
        finally:
            self.active -= 1


class ScriptedService:
    def __init__(
        self,
        actions: list[PortfolioPaperOpeningRunResult | Exception | None],
    ) -> None:
        self.actions = deque(actions)
        self.calls = 0

    async def run_once(self) -> PortfolioPaperOpeningRunResult | None:
        self.calls += 1
        action = self.actions.popleft()
        if isinstance(action, Exception):
            raise action
        return action


class CancellingService:
    async def run_once(self) -> PortfolioPaperOpeningRunResult | None:
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_disabled_scheduler_is_safe_and_serializable() -> None:
    scheduler = PortfolioPaperOpeningScheduler(service=NoCallService())

    await scheduler.start()
    await asyncio.sleep(0)
    await scheduler.stop()

    status = scheduler.status
    assert status == PortfolioPaperOpeningSchedulerStatus(
        enabled=False,
        running=False,
        poll_seconds=5.0,
    )
    assert status.model_dump(mode="json") == {
        "enabled": False,
        "running": False,
        "poll_seconds": 5.0,
        "last_run_at": None,
        "last_outcome": None,
        "last_track_id": None,
        "last_error": None,
        "consecutive_failures": 0,
    }
    assert status.model_dump_json()


@pytest.mark.asyncio
async def test_double_start_is_single_flight_and_stop_waits_for_cleanup() -> None:
    service = CancellationSafeBlockingService()
    scheduler = PortfolioPaperOpeningScheduler(
        service=service,
        enabled=True,
        poll_seconds=1,
    )

    await asyncio.gather(scheduler.start(), scheduler.start(), scheduler.start())
    await asyncio.wait_for(service.started.wait(), timeout=1)
    assert service.calls == 1
    assert service.maximum_active == 1
    assert scheduler.status.running is True

    stopping = asyncio.create_task(scheduler.stop())
    await asyncio.wait_for(service.cancelled.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not stopping.done()
    assert scheduler.status.running is True

    service.allow_cleanup.set()
    await asyncio.wait_for(stopping, timeout=1)
    assert service.cleanup_finished.is_set()
    assert service.active == 0
    assert scheduler.status.running is False


@pytest.mark.asyncio
async def test_start_stop_restart_runs_immediately_without_overlap() -> None:
    service = CountingService()
    sleeper = StepSleeper()
    scheduler = PortfolioPaperOpeningScheduler(
        service=service,
        enabled=True,
        poll_seconds=1,
        _sleep=sleeper,
    )

    await scheduler.start()
    await sleeper.next_gate()
    assert service.calls == 1
    await scheduler.stop()

    await scheduler.start()
    await sleeper.next_gate()
    assert service.calls == 2
    assert service.maximum_active == 1
    await scheduler.stop()


@pytest.mark.asyncio
async def test_failures_are_generic_and_loop_recovers_for_every_outcome() -> None:
    secret = "quote_hash=secret certificate=secret batch_id=secret"
    service = ScriptedService(
        [
            RuntimeError(secret),
            opening_result("failed", error_recorded=True),
            opening_result("stale", error_recorded=True),
            None,
            opening_result("committed"),
        ]
    )
    sleeper = StepSleeper()
    scheduler = PortfolioPaperOpeningScheduler(
        service=service,
        enabled=True,
        poll_seconds=1,
        _sleep=sleeper,
        _clock=lambda: NOW,
    )

    await scheduler.start()
    gate = await sleeper.next_gate(2.0)
    assert scheduler.status.last_outcome == "failed"
    assert scheduler.status.last_track_id is None
    assert scheduler.status.consecutive_failures == 1
    assert scheduler.status.last_error == (
        "Opening scheduler attempt failed (RuntimeError)."
    )
    assert secret not in scheduler.status.model_dump_json()

    gate.set()
    gate = await sleeper.next_gate(4.0)
    assert scheduler.status.last_outcome == "failed"
    assert scheduler.status.last_track_id == TRACK_ID
    assert scheduler.status.consecutive_failures == 2
    assert scheduler.status.last_error == "Opening attempt reported a generic failure."

    gate.set()
    gate = await sleeper.next_gate(8.0)
    assert scheduler.status.last_outcome == "stale"
    assert scheduler.status.consecutive_failures == 3
    assert scheduler.status.last_error == "Opening attempt lost its fenced claim."

    gate.set()
    gate = await sleeper.next_gate()
    assert scheduler.status.last_outcome == "none"
    assert scheduler.status.last_track_id is None
    assert scheduler.status.consecutive_failures == 0

    gate.set()
    await sleeper.next_gate()
    status_json = scheduler.status.model_dump_json()
    assert scheduler.status.last_outcome == "committed"
    assert scheduler.status.last_track_id == TRACK_ID
    assert scheduler.status.last_run_at == NOW
    assert scheduler.status.consecutive_failures == 0
    assert BATCH_ID not in status_json
    for forbidden in ("quote", "certificate", "hash", "batch_id"):
        assert forbidden not in status_json.lower()
    await scheduler.stop()


@pytest.mark.asyncio
async def test_stop_itself_can_be_cancelled_without_abandoning_service_cleanup() -> None:
    service = CancellationSafeBlockingService()
    scheduler = PortfolioPaperOpeningScheduler(
        service=service,
        enabled=True,
        poll_seconds=1,
    )
    await scheduler.start()
    await asyncio.wait_for(service.started.wait(), timeout=1)

    stopping = asyncio.create_task(scheduler.stop())
    await asyncio.wait_for(service.cancelled.wait(), timeout=1)
    stopping.cancel()
    await asyncio.sleep(0)
    assert not stopping.done()

    service.allow_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stopping, timeout=1)
    assert service.cleanup_finished.is_set()
    assert scheduler.status.running is False


@pytest.mark.asyncio
async def test_cleanup_exception_does_not_escape_stop_or_restart_the_loop() -> None:
    service = CleanupErrorService()
    scheduler = PortfolioPaperOpeningScheduler(
        service=service,
        enabled=True,
        poll_seconds=1,
    )
    await scheduler.start()
    await asyncio.wait_for(service.started.wait(), timeout=1)

    stopping = asyncio.create_task(scheduler.stop())
    await asyncio.wait_for(service.cancelled.wait(), timeout=1)
    service.allow_cleanup.set()
    await asyncio.wait_for(stopping, timeout=1)

    assert service.cleanup_finished.is_set()
    assert service.calls == 1
    assert scheduler.status.running is False
    assert scheduler.status.last_outcome == "failed"
    assert scheduler.status.consecutive_failures == 1
    assert scheduler.status.last_error == (
        "Opening scheduler attempt failed (RuntimeError)."
    )
    assert "must-not-escape" not in scheduler.status.model_dump_json()


@pytest.mark.asyncio
async def test_stop_consumes_an_already_failed_background_task() -> None:
    scheduler = PortfolioPaperOpeningScheduler(
        service=NoCallService(),
        enabled=True,
    )

    async def fail() -> None:
        raise RuntimeError("certificate=must-not-escape")

    failed_task = asyncio.create_task(fail())
    await asyncio.sleep(0)
    assert failed_task.done()
    scheduler._task = failed_task

    await scheduler.stop()
    assert scheduler.status.running is False
    assert scheduler.status.last_outcome == "failed"
    assert scheduler.status.last_error == (
        "Opening scheduler attempt failed (RuntimeError)."
    )
    assert "must-not-escape" not in scheduler.status.model_dump_json()


@pytest.mark.asyncio
async def test_cancelled_error_propagates_through_single_run() -> None:
    scheduler = PortfolioPaperOpeningScheduler(
        service=CancellingService(),
        enabled=True,
    )

    with pytest.raises(asyncio.CancelledError):
        await scheduler._run_single_flight()

    assert scheduler.status.last_run_at is None
    assert scheduler.status.consecutive_failures == 0


@pytest.mark.parametrize(
    "poll_seconds",
    [True, "5", float("nan"), float("inf"), 0, 0.999, 3600.001],
)
def test_poll_interval_rejects_unsafe_values(poll_seconds: Any) -> None:
    with pytest.raises(ValueError, match="poll"):
        PortfolioPaperOpeningScheduler(
            service=NoCallService(),
            poll_seconds=poll_seconds,
        )


def test_status_is_frozen_and_strict() -> None:
    status = PortfolioPaperOpeningSchedulerStatus(
        enabled=True,
        running=False,
        poll_seconds=1.0,
    )

    with pytest.raises(ValidationError):
        status.running = True
    with pytest.raises(ValidationError):
        PortfolioPaperOpeningSchedulerStatus(
            enabled=1,
            running=False,
            poll_seconds=1.0,
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        PortfolioPaperOpeningSchedulerStatus(
            enabled=True,
            running=False,
            poll_seconds=1.0,
            last_run_at=datetime(2026, 7, 26, 20, 0),
        )
