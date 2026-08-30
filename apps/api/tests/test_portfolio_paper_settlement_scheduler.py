from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal

import pytest
from pydantic import ValidationError
from quantsieve_api.portfolio_paper_settlement_scheduler import (
    PortfolioPaperSettlementScheduler,
    PortfolioPaperSettlementSchedulerStatus,
)
from quantsieve_api.portfolio_paper_settlement_service import (
    PortfolioPaperSettlementRunResult,
)

TRACK_ID = "b" * 32
SETTLEMENT_ID = "a" * 64
NOW = datetime(2026, 7, 27, 3, 0, tzinfo=UTC)


def settlement_result(
    status: Literal["committed", "failed", "stale"],
    *,
    error_recorded: bool = False,
) -> PortfolioPaperSettlementRunResult:
    if status == "committed":
        return PortfolioPaperSettlementRunResult(
            status=status,
            track_id=TRACK_ID,
            settlement_id=SETTLEMENT_ID,
            asset_count=2,
        )
    return PortfolioPaperSettlementRunResult(
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

    async def next_gate(self, expected_seconds: float) -> asyncio.Event:
        seconds, gate = await asyncio.wait_for(self.entered.get(), timeout=1)
        assert seconds == expected_seconds
        return gate


class NoCallService:
    async def run_once(self) -> PortfolioPaperSettlementRunResult | None:
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

    async def run_once(self) -> PortfolioPaperSettlementRunResult | None:
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
        return None


class CleanupErrorService(CancellationSafeBlockingService):
    async def run_once(self) -> PortfolioPaperSettlementRunResult | None:
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
            raise RuntimeError(
                "bar_hash=private payload=private"
            ) from None
        finally:
            self.active -= 1
        return None


class CountingService:
    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.maximum_active = 0

    async def run_once(self) -> PortfolioPaperSettlementRunResult | None:
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        return None


class ScriptedService:
    def __init__(
        self,
        actions: list[
            PortfolioPaperSettlementRunResult | Exception | None
        ],
    ) -> None:
        self.actions = deque(actions)
        self.calls = 0

    async def run_once(self) -> PortfolioPaperSettlementRunResult | None:
        self.calls += 1
        action = self.actions.popleft()
        if isinstance(action, Exception):
            raise action
        return action


class CancellingService:
    async def run_once(self) -> PortfolioPaperSettlementRunResult | None:
        raise asyncio.CancelledError


class ExplodingSleeper:
    def __init__(self) -> None:
        self.called = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        assert seconds == 1.0
        self.called.set()
        raise RuntimeError(
            "close_bar=private certificate_hash=private payload=private"
        )


@pytest.mark.asyncio
async def test_disabled_scheduler_is_safe_and_serializable() -> None:
    scheduler = PortfolioPaperSettlementScheduler(service=NoCallService())

    await scheduler.start()
    await asyncio.sleep(0)
    await scheduler.stop()

    status = scheduler.status
    assert status == PortfolioPaperSettlementSchedulerStatus(
        enabled=False,
        running=False,
        poll_seconds=60.0,
    )
    assert status.model_dump(mode="json") == {
        "enabled": False,
        "running": False,
        "poll_seconds": 60.0,
        "last_run_at": None,
        "last_outcome": None,
        "last_track_id": None,
        "last_error": None,
        "consecutive_failures": 0,
    }
    assert status.model_dump_json()


@pytest.mark.asyncio
async def test_repeat_start_is_single_flight_and_stop_waits_for_cleanup() -> None:
    service = CancellationSafeBlockingService()
    scheduler = PortfolioPaperSettlementScheduler(
        service=service,
        enabled=True,
        poll_seconds=1,
    )

    await asyncio.gather(
        scheduler.start(),
        scheduler.start(),
        scheduler.start(),
    )
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
    scheduler = PortfolioPaperSettlementScheduler(
        service=service,
        enabled=True,
        poll_seconds=1,
        _sleep=sleeper,
    )

    await scheduler.start()
    await sleeper.next_gate(1.0)
    assert service.calls == 1
    await scheduler.stop()

    await scheduler.start()
    await sleeper.next_gate(1.0)
    assert service.calls == 2
    assert service.maximum_active == 1
    await scheduler.stop()


@pytest.mark.asyncio
async def test_results_drive_redacted_status_and_exponential_backoff() -> None:
    secret = "bar_hash=secret payload=secret settlement_id=secret"
    service = ScriptedService(
        [
            RuntimeError(secret),
            settlement_result("failed", error_recorded=True),
            settlement_result("stale", error_recorded=True),
            None,
            settlement_result("committed"),
        ]
    )
    sleeper = StepSleeper()
    scheduler = PortfolioPaperSettlementScheduler(
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
        "Settlement scheduler attempt failed."
    )
    assert secret not in scheduler.status.model_dump_json()

    gate.set()
    gate = await sleeper.next_gate(4.0)
    assert scheduler.status.last_outcome == "failed"
    assert scheduler.status.last_track_id == TRACK_ID
    assert scheduler.status.consecutive_failures == 2
    assert scheduler.status.last_error == (
        "Settlement valuation attempt reported a generic failure."
    )

    gate.set()
    gate = await sleeper.next_gate(8.0)
    assert scheduler.status.last_outcome == "stale"
    assert scheduler.status.last_track_id == TRACK_ID
    assert scheduler.status.consecutive_failures == 3
    assert scheduler.status.last_error == (
        "Settlement valuation attempt lost its fenced claim."
    )

    gate.set()
    gate = await sleeper.next_gate(1.0)
    assert scheduler.status.last_outcome == "none"
    assert scheduler.status.last_track_id is None
    assert scheduler.status.consecutive_failures == 0

    gate.set()
    await sleeper.next_gate(1.0)
    status_json = scheduler.status.model_dump_json()
    assert scheduler.status.last_outcome == "committed"
    assert scheduler.status.last_track_id == TRACK_ID
    assert scheduler.status.last_run_at == NOW
    assert scheduler.status.consecutive_failures == 0
    assert SETTLEMENT_ID not in status_json
    for forbidden in (
        "bar_hash",
        "certificate",
        "payload",
        "settlement_id",
    ):
        assert forbidden not in status_json.lower()
    await scheduler.stop()


@pytest.mark.asyncio
async def test_sleep_failure_is_generic_and_shutdown_remains_interruptible() -> None:
    sleeper = ExplodingSleeper()
    scheduler = PortfolioPaperSettlementScheduler(
        service=CountingService(),
        enabled=True,
        poll_seconds=1,
        _sleep=sleeper,
        _clock=lambda: NOW,
    )

    await scheduler.start()
    await asyncio.wait_for(sleeper.called.wait(), timeout=1)
    await asyncio.sleep(0)

    status_json = scheduler.status.model_dump_json()
    assert scheduler.status.last_outcome == "failed"
    assert scheduler.status.last_error == (
        "Settlement scheduler attempt failed."
    )
    assert scheduler.status.consecutive_failures == 1
    for forbidden in ("bar", "hash", "payload", "private"):
        assert forbidden not in status_json.lower()

    await asyncio.wait_for(scheduler.stop(), timeout=1)
    assert scheduler.status.running is False


@pytest.mark.asyncio
async def test_stop_caller_cancellation_does_not_abandon_service_cleanup() -> None:
    service = CancellationSafeBlockingService()
    scheduler = PortfolioPaperSettlementScheduler(
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
async def test_concurrent_stop_does_not_cancel_cleanup_twice() -> None:
    service = CancellationSafeBlockingService()
    scheduler = PortfolioPaperSettlementScheduler(
        service=service,
        enabled=True,
        poll_seconds=1,
    )
    await scheduler.start()
    await asyncio.wait_for(service.started.wait(), timeout=1)

    first = asyncio.create_task(scheduler.stop())
    second = asyncio.create_task(scheduler.stop())
    await asyncio.wait_for(service.cancelled.wait(), timeout=1)
    service.allow_cleanup.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

    assert service.cleanup_finished.is_set()
    assert service.active == 0
    assert scheduler.status.running is False


@pytest.mark.asyncio
async def test_cleanup_exception_is_consumed_and_never_leaks() -> None:
    service = CleanupErrorService()
    scheduler = PortfolioPaperSettlementScheduler(
        service=service,
        enabled=True,
        poll_seconds=1,
        _clock=lambda: NOW,
    )
    await scheduler.start()
    await asyncio.wait_for(service.started.wait(), timeout=1)

    stopping = asyncio.create_task(scheduler.stop())
    await asyncio.wait_for(service.cancelled.wait(), timeout=1)
    service.allow_cleanup.set()
    await asyncio.wait_for(stopping, timeout=1)

    assert service.cleanup_finished.is_set()
    assert scheduler.status.running is False
    assert scheduler.status.last_outcome == "failed"
    assert scheduler.status.last_error == (
        "Settlement scheduler attempt failed."
    )
    assert scheduler.status.consecutive_failures == 1
    status_json = scheduler.status.model_dump_json()
    for forbidden in ("bar", "hash", "payload", "private"):
        assert forbidden not in status_json.lower()


@pytest.mark.asyncio
async def test_direct_concurrent_runs_are_serialized() -> None:
    service = CountingService()
    scheduler = PortfolioPaperSettlementScheduler(
        service=service,
        enabled=True,
    )

    await asyncio.gather(
        scheduler._run_single_flight(),
        scheduler._run_single_flight(),
        scheduler._run_single_flight(),
    )

    assert service.calls == 3
    assert service.maximum_active == 1


@pytest.mark.asyncio
async def test_cancelled_error_propagates_through_single_run() -> None:
    scheduler = PortfolioPaperSettlementScheduler(
        service=CancellingService(),
        enabled=True,
    )

    with pytest.raises(asyncio.CancelledError):
        await scheduler._run_single_flight()

    assert scheduler.status.last_run_at is None
    assert scheduler.status.consecutive_failures == 0


def test_backoff_is_bounded_and_never_shortens_a_long_poll() -> None:
    default_scheduler = PortfolioPaperSettlementScheduler(
        service=NoCallService(),
    )
    long_scheduler = PortfolioPaperSettlementScheduler(
        service=NoCallService(),
        poll_seconds=3600,
    )

    for _ in range(20):
        default_scheduler._record_result(settlement_result("failed"))
        long_scheduler._record_result(settlement_result("failed"))

    assert default_scheduler._next_poll_delay() == 900.0
    assert long_scheduler._next_poll_delay() == 3600.0


@pytest.mark.parametrize(
    "poll_seconds",
    [True, "60", float("nan"), float("inf"), 0, 0.999, 3600.001],
)
def test_poll_interval_rejects_unsafe_values(poll_seconds: Any) -> None:
    with pytest.raises(ValueError, match="poll"):
        PortfolioPaperSettlementScheduler(
            service=NoCallService(),
            poll_seconds=poll_seconds,
        )


def test_constructor_rejects_invalid_controls_and_clock() -> None:
    with pytest.raises(ValueError, match="enabled"):
        PortfolioPaperSettlementScheduler(
            service=NoCallService(),
            enabled=1,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="sleep"):
        PortfolioPaperSettlementScheduler(
            service=NoCallService(),
            _sleep=None,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="clock"):
        PortfolioPaperSettlementScheduler(
            service=NoCallService(),
            _clock=None if False else 7,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        PortfolioPaperSettlementScheduler(
            service=NoCallService(),
            _clock=lambda: datetime(2026, 7, 27, 3, 0),
        )


def test_status_is_frozen_strict_and_rejects_extra_fields() -> None:
    status = PortfolioPaperSettlementSchedulerStatus(
        enabled=True,
        running=False,
        poll_seconds=60.0,
    )

    with pytest.raises(ValidationError):
        status.running = True
    with pytest.raises(ValidationError):
        PortfolioPaperSettlementSchedulerStatus(
            enabled=1,  # type: ignore[arg-type]
            running=False,
            poll_seconds=60.0,
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        PortfolioPaperSettlementSchedulerStatus(
            enabled=True,
            running=False,
            poll_seconds=60.0,
            last_run_at=datetime(2026, 7, 27, 3, 0),
        )
    with pytest.raises(ValidationError):
        PortfolioPaperSettlementSchedulerStatus.model_validate(
            {
                "enabled": True,
                "running": False,
                "poll_seconds": 60.0,
                "private_payload": "must not be accepted",
            }
        )


def test_aware_clock_is_normalized_to_utc() -> None:
    observed = datetime(
        2026,
        7,
        27,
        11,
        0,
        tzinfo=timezone(timedelta(hours=8)),
    )
    scheduler = PortfolioPaperSettlementScheduler(
        service=NoCallService(),
        _clock=lambda: observed,
    )

    scheduler._record_result(None)

    assert scheduler.status.last_run_at == NOW
    assert scheduler.status.last_run_at is not None
    assert scheduler.status.last_run_at.tzinfo is UTC
