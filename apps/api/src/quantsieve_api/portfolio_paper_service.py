from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Literal, Protocol, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from quantsieve_providers import (
    BinanceSpotTradingRules,
    ExecutionQuote,
)

from .portfolio_paper import (
    PortfolioPaperLease,
    PortfolioPaperTrackRecord,
    PortfolioPaperTrackStore,
)
from .portfolio_paper_contracts import PortfolioPaperBasketContract
from .portfolio_paper_execution import PortfolioPaperExecutionBatch

_DEFAULT_QUOTE_DEADLINE_SECONDS = 4.0
_DEFAULT_LEASE_DURATION = timedelta(seconds=15)
_MINIMUM_LEASE_MARGIN_SECONDS = 5.0
_MAXIMUM_OWNER_PREFIX_LENGTH = 167


class PortfolioPaperExecutionQuoteProvider(Protocol):
    """Narrow provider boundary needed by the activation-opening worker."""

    async def execution_quote(
        self,
        symbol: str,
        *,
        rules: BinanceSpotTradingRules,
    ) -> ExecutionQuote: ...


class PortfolioPaperOpeningRunResult(BaseModel):
    """Redacted outcome of one claimed activation-opening attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    status: Literal["committed", "failed", "stale"]
    track_id: str = Field(
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
    )
    batch_id: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    fill_count: int = Field(default=0, ge=0, le=6)
    error_recorded: bool = False

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.status == "committed":
            if self.batch_id is None or not 2 <= self.fill_count <= 6:
                raise ValueError(
                    "A committed opening result requires its batch and fill count."
                )
            if self.error_recorded:
                raise ValueError(
                    "A committed opening result cannot report a persisted error."
                )
        elif self.batch_id is not None or self.fill_count != 0:
            raise ValueError(
                "A non-committed opening result cannot expose batch details."
            )
        return self


class PortfolioPaperOpeningService:
    """Claim, quote, and atomically commit one activation opening.

    All SQLite work is moved to worker threads. Quote collection happens outside
    store transactions and every symbol shares one service-level deadline.
    """

    def __init__(
        self,
        *,
        store: PortfolioPaperTrackStore,
        provider: PortfolioPaperExecutionQuoteProvider,
        owner_prefix: str = "portfolio-opening",
        quote_deadline_seconds: float = _DEFAULT_QUOTE_DEADLINE_SECONDS,
        lease_for: timedelta = _DEFAULT_LEASE_DURATION,
    ) -> None:
        normalized_prefix = _normalize_owner_prefix(owner_prefix)
        if not isfinite(quote_deadline_seconds) or quote_deadline_seconds <= 0:
            raise ValueError("Opening quote deadline must be finite and positive.")
        lease_seconds = lease_for.total_seconds()
        if (
            not isfinite(lease_seconds)
            or lease_seconds
            < quote_deadline_seconds + _MINIMUM_LEASE_MARGIN_SECONDS
            or lease_for > timedelta(hours=1)
        ):
            raise ValueError(
                "Opening lease must leave at least five seconds beyond the quote "
                "deadline and be at most one hour."
            )
        self._store = store
        self._provider = provider
        self._owner = f"{normalized_prefix}:{uuid4().hex}"
        self._quote_deadline_seconds = quote_deadline_seconds
        self._lease_for = lease_for

    @property
    def owner(self) -> str:
        """Unique fenced owner token for this service instance."""

        return self._owner

    @property
    def quote_deadline_seconds(self) -> float:
        return self._quote_deadline_seconds

    @property
    def lease_for(self) -> timedelta:
        return self._lease_for

    async def run_once(
        self,
        *,
        now: datetime | None = None,
    ) -> PortfolioPaperOpeningRunResult | None:
        """Process at most one due opening, or return ``None`` when none is due.

        SQLite worker calls cannot be interrupted once they start. Claim and record
        reads therefore finish under a shield so cancellation can release any lease
        they acquired before it propagates. Quote collection uses the same
        cancellation-safe release, while an atomic commit is allowed to finish before
        shutdown returns.
        """

        claim_now = _normalized_now(now)
        lease = await self._claim_cancellation_safe(claim_now)
        if lease is None:
            return None

        record = await self._get_cancellation_safe(lease)
        if not _record_matches_lease(record, lease):
            error_recorded = await self._set_error(
                lease,
                "Opening track changed after its fenced claim.",
            )
            return PortfolioPaperOpeningRunResult(
                status="stale",
                track_id=lease.track_id,
                error_recorded=error_recorded,
            )

        if record is None or record.pending_decision is None:
            raise AssertionError("Current record validation lost its pending decision.")
        basket = record.pending_decision.certificate.basket
        try:
            quotes = await self._collect_quotes(basket)
        except asyncio.CancelledError:
            await self._release_after_cancellation(lease)
            raise
        except Exception as error:
            error_recorded = await self._set_error(
                lease,
                _quote_failure_message(error, self._quote_deadline_seconds),
            )
            return PortfolioPaperOpeningRunResult(
                status="failed",
                track_id=lease.track_id,
                error_recorded=error_recorded,
            )

        try:
            batch = await self._commit_cancellation_safe(
                lease=lease,
                basket=basket,
                quotes=quotes,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            error_recorded = await self._set_error(
                lease,
                _commit_failure_message(error),
            )
            return PortfolioPaperOpeningRunResult(
                status="failed",
                track_id=lease.track_id,
                error_recorded=error_recorded,
            )

        if (
            batch.command.track_id != lease.track_id
            or batch.command.state_revision != lease.state_revision
        ):
            error_recorded = await self._set_error(
                lease,
                "Opening store returned a batch for a different fenced command.",
            )
            return PortfolioPaperOpeningRunResult(
                status="failed",
                track_id=lease.track_id,
                error_recorded=error_recorded,
            )
        return PortfolioPaperOpeningRunResult(
            status="committed",
            track_id=lease.track_id,
            batch_id=batch.command.idempotency_key,
            fill_count=len(batch.fills),
        )

    async def _claim_cancellation_safe(
        self,
        claim_now: datetime,
    ) -> PortfolioPaperLease | None:
        """Resolve a threaded claim before honoring cancellation.

        A SQLite worker cannot be cancelled once it starts. Waiting for its result
        lets shutdown release a lease that would otherwise appear after the
        application task had already exited.
        """

        claim_task = asyncio.create_task(
            asyncio.to_thread(
                self._store.claim_due,
                owner=self._owner,
                now=claim_now,
                lease_for=self._lease_for,
            )
        )
        cancellation_requested = False
        while True:
            try:
                lease = await asyncio.shield(claim_task)
            except asyncio.CancelledError:
                if claim_task.cancelled():
                    raise
                cancellation_requested = True
                continue
            except Exception:
                if cancellation_requested:
                    raise asyncio.CancelledError from None
                raise
            if cancellation_requested:
                if lease is not None:
                    await self._release_after_cancellation(lease)
                raise asyncio.CancelledError
            return lease

    async def _get_cancellation_safe(
        self,
        lease: PortfolioPaperLease,
    ) -> PortfolioPaperTrackRecord | None:
        """Finish the threaded record read and release its known lease on cancel."""

        get_task = asyncio.create_task(
            asyncio.to_thread(self._store.get, lease.track_id)
        )
        cancellation_requested = False
        while True:
            try:
                record = await asyncio.shield(get_task)
            except asyncio.CancelledError:
                if get_task.cancelled():
                    await self._release_after_cancellation(lease)
                    raise
                cancellation_requested = True
                continue
            except Exception:
                if cancellation_requested:
                    await self._release_after_cancellation(lease)
                    raise asyncio.CancelledError from None
                await self._release_after_cancellation(lease)
                raise
            if cancellation_requested:
                await self._release_after_cancellation(lease)
                raise asyncio.CancelledError
            return record

    async def _collect_quotes(
        self,
        basket: PortfolioPaperBasketContract,
    ) -> tuple[ExecutionQuote, ...]:
        instruments = basket.instruments
        if not 2 <= len(instruments) <= 6:
            raise ValueError(
                "Activation opening requires between two and six instruments."
            )
        collected: list[ExecutionQuote | None] = [None] * len(instruments)

        async def collect_one(index: int) -> None:
            instrument = instruments[index]
            collected[index] = await self._provider.execution_quote(
                instrument.symbol,
                rules=instrument.rules,
            )

        async with asyncio.timeout(self._quote_deadline_seconds):
            async with asyncio.TaskGroup() as group:
                for index in range(len(instruments)):
                    group.create_task(collect_one(index))

        if any(quote is None for quote in collected):
            raise RuntimeError("Opening quote collection returned an incomplete set.")
        return tuple(
            quote
            for quote in collected
            if quote is not None
        )

    async def _commit_cancellation_safe(
        self,
        *,
        lease: PortfolioPaperLease,
        basket: PortfolioPaperBasketContract,
        quotes: Sequence[ExecutionQuote],
    ) -> PortfolioPaperExecutionBatch:
        commit_task = asyncio.create_task(
            asyncio.to_thread(
                self._store.commit_opening_if_current,
                track_id=lease.track_id,
                owner=lease.owner,
                generation=lease.generation,
                expected_revision=lease.state_revision,
                basket=basket,
                quotes=quotes,
            )
        )
        cancellation_requested = False
        while True:
            try:
                batch = await asyncio.shield(commit_task)
            except asyncio.CancelledError:
                if commit_task.cancelled():
                    raise
                cancellation_requested = True
                continue
            except Exception:
                if cancellation_requested:
                    raise asyncio.CancelledError from None
                raise
            if cancellation_requested:
                raise asyncio.CancelledError
            return batch

    async def _release_after_cancellation(
        self,
        lease: PortfolioPaperLease,
    ) -> None:
        """Finish a known-lease release even if more cancellation requests arrive."""

        release_task = asyncio.create_task(
            asyncio.to_thread(
                self._store.release_claim,
                track_id=lease.track_id,
                owner=lease.owner,
                generation=lease.generation,
                now=datetime.now(UTC),
            )
        )
        while True:
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError:
                if release_task.cancelled():
                    return
                continue
            except Exception:
                return
            return

    async def _set_error(
        self,
        lease: PortfolioPaperLease,
        message: str,
    ) -> bool:
        try:
            return await asyncio.to_thread(
                self._store.set_error_if_current,
                track_id=lease.track_id,
                owner=lease.owner,
                generation=lease.generation,
                expected_revision=lease.state_revision,
                message=message,
                checked_at=datetime.now(UTC),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False


def _normalize_owner_prefix(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Opening owner prefix must be a string.")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > _MAXIMUM_OWNER_PREFIX_LENGTH
    ):
        raise ValueError(
            "Opening owner prefix must be canonical and at most 167 characters."
        )
    return normalized


def _normalized_now(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Opening service time must be timezone-aware.")
    return current.astimezone(UTC)


def _record_matches_lease(
    record: PortfolioPaperTrackRecord | None,
    lease: PortfolioPaperLease,
) -> bool:
    return (
        record is not None
        and record.id == lease.track_id
        and record.state_revision == lease.state_revision
        and record.refresh_owner == lease.owner
        and record.refresh_generation == lease.generation
        and record.refresh_lease_until == lease.expires_at
        and record.opening_batch_id is None
        and record.pending_decision is not None
    )


def _quote_failure_message(error: Exception, deadline_seconds: float) -> str:
    if isinstance(error, TimeoutError):
        return (
            "Opening quote collection exceeded its shared "
            f"{deadline_seconds:g}-second deadline."
        )
    leaf = _first_exception_leaf(error)
    return f"Opening quote collection failed ({type(leaf).__name__})."


def _commit_failure_message(error: Exception) -> str:
    leaf = _first_exception_leaf(error)
    return f"Opening atomic commit failed ({type(leaf).__name__})."


def _first_exception_leaf(error: BaseException) -> BaseException:
    current = error
    while isinstance(current, BaseExceptionGroup) and current.exceptions:
        current = current.exceptions[0]
    return current
