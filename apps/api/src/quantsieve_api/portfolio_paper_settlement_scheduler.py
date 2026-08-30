from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from math import isfinite
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .portfolio_paper_settlement_service import (
    PortfolioPaperSettlementRunResult,
)

_DEFAULT_POLL_SECONDS = 60.0
_MINIMUM_POLL_SECONDS = 1.0
_MAXIMUM_POLL_SECONDS = 3600.0
_MAXIMUM_BACKOFF_SECONDS = 900.0
_MAXIMUM_BACKOFF_DOUBLINGS = 8

_Sleep = Callable[[float], Awaitable[None]]
_Clock = Callable[[], datetime]
_SettlementOutcome = Literal["committed", "failed", "stale", "none"]


class PortfolioPaperSettlementRunner(Protocol):
    """Narrow asynchronous boundary used by the settlement scheduler."""

    async def run_once(self) -> PortfolioPaperSettlementRunResult | None: ...


class PortfolioPaperSettlementSchedulerStatus(BaseModel):
    """Frozen internal health snapshot without settlement evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool
    running: bool
    poll_seconds: float = Field(
        ge=_MINIMUM_POLL_SECONDS,
        le=_MAXIMUM_POLL_SECONDS,
    )
    last_run_at: datetime | None = None
    last_outcome: _SettlementOutcome | None = None
    last_track_id: str | None = Field(
        default=None,
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
    )
    last_error: str | None = Field(default=None, max_length=160)
    consecutive_failures: int = Field(default=0, ge=0)

    @field_validator("last_run_at")
    @classmethod
    def validate_last_run_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "Settlement scheduler status time must be timezone-aware."
            )
        return value.astimezone(UTC)


class PortfolioPaperSettlementScheduler:
    """Default-off, single-flight scheduler for modeled close valuations."""

    def __init__(
        self,
        *,
        service: PortfolioPaperSettlementRunner,
        enabled: bool = False,
        poll_seconds: float = _DEFAULT_POLL_SECONDS,
        _sleep: _Sleep = asyncio.sleep,
        _clock: _Clock | None = None,
    ) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("Settlement scheduler enabled must be a boolean.")
        if isinstance(poll_seconds, bool) or not isinstance(
            poll_seconds,
            (int, float),
        ):
            raise ValueError(
                "Settlement scheduler poll interval must be an int or float."
            )
        normalized_poll_seconds = float(poll_seconds)
        if (
            not isfinite(normalized_poll_seconds)
            or not _MINIMUM_POLL_SECONDS
            <= normalized_poll_seconds
            <= _MAXIMUM_POLL_SECONDS
        ):
            raise ValueError(
                "Settlement scheduler poll interval must be finite and between "
                f"{_MINIMUM_POLL_SECONDS:g} and "
                f"{_MAXIMUM_POLL_SECONDS:g} seconds."
            )
        if not callable(_sleep):
            raise ValueError("Settlement scheduler sleep must be callable.")

        clock = _clock or _system_utc_now
        if not callable(clock):
            raise ValueError("Settlement scheduler clock must be callable.")
        _normalized_utc_now(clock())

        self._service = service
        self._enabled = enabled
        self._poll_seconds = normalized_poll_seconds
        self._sleep = _sleep
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._cancel_requested_task: asyncio.Task[None] | None = None
        self._run_lock = asyncio.Lock()
        self._stop_requested = asyncio.Event()
        self._last_run_at: datetime | None = None
        self._last_outcome: _SettlementOutcome | None = None
        self._last_track_id: str | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0

    @property
    def status(self) -> PortfolioPaperSettlementSchedulerStatus:
        """Return an immutable internal snapshot."""

        task = self._task
        return PortfolioPaperSettlementSchedulerStatus(
            enabled=self._enabled,
            running=task is not None and not task.done(),
            poll_seconds=self._poll_seconds,
            last_run_at=self._last_run_at,
            last_outcome=self._last_outcome,
            last_track_id=self._last_track_id,
            last_error=self._last_error,
            consecutive_failures=self._consecutive_failures,
        )

    async def start(self) -> None:
        """Start one worker loop, or do nothing when disabled/already running."""

        if not self._enabled:
            return
        current = self._task
        if current is not None and not current.done():
            return
        if current is not None:
            self._consume_task_result(current)
        self._stop_requested.clear()
        task = asyncio.create_task(
            self._run(),
            name="quantsieve-portfolio-settlement",
        )
        self._task = task
        self._cancel_requested_task = None

    async def stop(self) -> None:
        """Request shutdown and wait through any service cancellation cleanup."""

        task = self._task
        if task is None:
            return

        self._stop_requested.set()
        if (
            not task.done()
            and self._cancel_requested_task is not task
        ):
            self._cancel_requested_task = task
            task.cancel()

        caller_cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    caller_cancelled = True
                if task.done():
                    break
            except Exception:
                break

        self._consume_task_result(task)
        if self._task is task:
            self._task = None
        if self._cancel_requested_task is task:
            self._cancel_requested_task = None
        if caller_cancelled:
            raise asyncio.CancelledError

    async def _run(self) -> None:
        while not self._stop_requested.is_set():
            await self._run_single_flight()
            if self._stop_requested.is_set():
                return

            try:
                await self._sleep(self._next_poll_delay())
            except asyncio.CancelledError:
                raise
            except Exception:
                self._record_unexpected_failure()
                if self._stop_requested.is_set():
                    return
                await self._fallback_sleep(self._next_poll_delay())

    async def _run_single_flight(self) -> None:
        async with self._run_lock:
            try:
                result = await self._service.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._record_unexpected_failure()
                return

            try:
                self._record_result(result)
            except Exception:
                self._record_unexpected_failure()

    async def _fallback_sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(
                self._stop_requested.wait(),
                timeout=seconds,
            )
        except TimeoutError:
            return

    def _record_result(
        self,
        result: PortfolioPaperSettlementRunResult | None,
    ) -> None:
        observed_at = _normalized_utc_now(self._clock())
        self._last_run_at = observed_at
        if result is None:
            self._last_outcome = "none"
            self._last_track_id = None
            self._last_error = None
            self._consecutive_failures = 0
            return

        self._last_outcome = result.status
        self._last_track_id = result.track_id
        if result.status == "failed":
            self._last_error = (
                "Settlement valuation attempt reported a generic failure."
            )
            self._consecutive_failures += 1
            return
        if result.status == "stale":
            self._last_error = (
                "Settlement valuation attempt lost its fenced claim."
            )
            self._consecutive_failures += 1
            return

        self._last_error = None
        self._consecutive_failures = 0

    def _record_unexpected_failure(self) -> None:
        try:
            observed_at = _normalized_utc_now(self._clock())
        except Exception:
            # A clock regression must not prevent cancellation-safe shutdown.
            observed_at = None
        if observed_at is not None:
            self._last_run_at = observed_at
        self._last_outcome = "failed"
        self._last_track_id = None
        self._last_error = "Settlement scheduler attempt failed."
        self._consecutive_failures += 1

    def _next_poll_delay(self) -> float:
        cap = max(self._poll_seconds, _MAXIMUM_BACKOFF_SECONDS)
        delay = self._poll_seconds
        for _ in range(
            min(self._consecutive_failures, _MAXIMUM_BACKOFF_DOUBLINGS)
        ):
            delay = min(delay * 2, cap)
        return delay

    def _consume_task_result(self, task: asyncio.Task[None]) -> None:
        if not task.done():
            return
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            self._record_unexpected_failure()


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


def _normalized_utc_now(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("Settlement scheduler clock must be timezone-aware.")
    return value.astimezone(UTC)
