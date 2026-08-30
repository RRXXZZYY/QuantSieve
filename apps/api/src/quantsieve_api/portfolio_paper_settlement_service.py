from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from time import monotonic
from typing import Literal, Protocol, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from quantsieve_providers import BinanceSettlementDailyBarEvidence

from .portfolio_paper import (
    PortfolioPaperLease,
    PortfolioPaperTrackRecord,
)
from .portfolio_paper_contracts import (
    CertifiedPortfolioBar,
    PortfolioPaperBasketContract,
    PortfolioPaperInstrumentContract,
    certify_binance_daily_bar,
)
from .portfolio_paper_settlement import (
    PortfolioPaperModeledCloseSettlement,
)

_DEFAULT_HISTORY_DEADLINE_SECONDS = 20.0
_DEFAULT_LEASE_DURATION = timedelta(seconds=30)
_MINIMUM_LEASE_MARGIN_SECONDS = 5.0
_MAXIMUM_OWNER_PREFIX_LENGTH = 167
_Clock = Callable[[], datetime]


class PortfolioPaperSettlementHistoryProvider(Protocol):
    """Uncached typed Binance evidence boundary for close valuation."""

    async def settlement_daily_bar(
        self,
        symbol: str,
        *,
        session: date,
        deadline: float,
    ) -> BinanceSettlementDailyBarEvidence: ...


class PortfolioPaperSettlementStore(Protocol):
    """Narrow persistence boundary used by the settlement service."""

    def claim_due_settlement(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
    ) -> PortfolioPaperLease | None: ...

    def get(self, track_id: str) -> PortfolioPaperTrackRecord | None: ...

    def release_claim(
        self,
        *,
        track_id: str,
        owner: str,
        generation: int,
        now: datetime,
    ) -> bool: ...

    def set_error_if_current(
        self,
        *,
        track_id: str,
        owner: str,
        generation: int,
        expected_revision: int,
        message: str,
        checked_at: datetime,
    ) -> bool: ...

    def commit_close_settlement_if_current(
        self,
        *,
        track_id: str,
        owner: str,
        generation: int,
        expected_revision: int,
        bars: Sequence[CertifiedPortfolioBar],
    ) -> PortfolioPaperModeledCloseSettlement: ...


class PortfolioPaperSettlementRunResult(BaseModel):
    """Redacted outcome of one claimed modeled close-valuation attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    status: Literal["committed", "failed", "stale"]
    track_id: str = Field(
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
    )
    settlement_id: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    asset_count: int = Field(default=0, ge=0, le=6)
    error_recorded: bool = False

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.status == "committed":
            if self.settlement_id is None or not 2 <= self.asset_count <= 6:
                raise ValueError(
                    "A committed settlement result requires its id and asset count."
                )
            if self.error_recorded:
                raise ValueError(
                    "A committed settlement result cannot report a persisted error."
                )
        elif self.settlement_id is not None or self.asset_count != 0:
            raise ValueError(
                "A non-committed settlement result cannot expose settlement details."
            )
        return self


class PortfolioPaperSettlementService:
    """Claim, certify, and atomically commit one modeled close valuation.

    Store work runs in worker threads. All Binance histories share one deadline,
    and the exact execution-session row is selected before certification. This
    service never places a close order or models a close fill.
    """

    def __init__(
        self,
        *,
        store: PortfolioPaperSettlementStore,
        provider: PortfolioPaperSettlementHistoryProvider,
        owner_prefix: str = "portfolio-settlement",
        history_deadline_seconds: float = _DEFAULT_HISTORY_DEADLINE_SECONDS,
        lease_for: timedelta = _DEFAULT_LEASE_DURATION,
        _clock: _Clock | None = None,
    ) -> None:
        normalized_prefix = _normalize_owner_prefix(owner_prefix)
        if (
            isinstance(history_deadline_seconds, bool)
            or not isinstance(history_deadline_seconds, (int, float))
            or not isfinite(float(history_deadline_seconds))
            or history_deadline_seconds <= 0
        ):
            raise ValueError(
                "Settlement history deadline must be finite and positive."
            )
        normalized_deadline = float(history_deadline_seconds)
        lease_seconds = lease_for.total_seconds()
        if (
            not isfinite(lease_seconds)
            or lease_seconds
            < normalized_deadline + _MINIMUM_LEASE_MARGIN_SECONDS
            or lease_for > timedelta(hours=1)
        ):
            raise ValueError(
                "Settlement lease must leave at least five seconds beyond the "
                "history deadline and be at most one hour."
            )
        self._store = store
        self._provider = provider
        self._owner = f"{normalized_prefix}:{uuid4().hex}"
        self._history_deadline_seconds = normalized_deadline
        self._lease_for = lease_for
        self._clock = _clock or _system_utc_now

    @property
    def owner(self) -> str:
        """Unique fenced owner token for this service instance."""

        return self._owner

    @property
    def history_deadline_seconds(self) -> float:
        return self._history_deadline_seconds

    @property
    def lease_for(self) -> timedelta:
        return self._lease_for

    async def run_once(self) -> PortfolioPaperSettlementRunResult | None:
        """Process at most one due opening settlement.

        Maturity remains a store/SQLite-clock decision. The trusted service clock
        supplies one observation for both the claim hint and UTC history-window end.
        """

        observed_now = _normalized_now(self._clock())
        lease = await self._claim_cancellation_safe(observed_now)
        if lease is None:
            return None

        record = await self._get_cancellation_safe(lease, observed_now)
        context = _settlement_context(record, lease, observed_now.date())
        if context is None:
            error_recorded = await self._set_error(
                lease,
                "Settlement track changed after its fenced claim.",
                checked_at=observed_now,
            )
            return PortfolioPaperSettlementRunResult(
                status="stale",
                track_id=lease.track_id,
                error_recorded=error_recorded,
            )
        basket, execution_session, execution_date = context

        try:
            bars = await self._collect_bars(
                basket=basket,
                execution_session=execution_session,
                execution_date=execution_date,
            )
        except asyncio.CancelledError:
            await self._release_after_cancellation(lease, observed_now)
            raise
        except Exception as error:
            error_recorded = await self._set_error(
                lease,
                _collection_failure_message(
                    error,
                    self._history_deadline_seconds,
                ),
                checked_at=observed_now,
            )
            return PortfolioPaperSettlementRunResult(
                status="failed",
                track_id=lease.track_id,
                error_recorded=error_recorded,
            )

        try:
            settlement = await self._commit_cancellation_safe(
                lease=lease,
                bars=bars,
                observed_now=observed_now,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            error_recorded = await self._set_error(
                lease,
                _commit_failure_message(error),
                checked_at=observed_now,
            )
            return PortfolioPaperSettlementRunResult(
                status="failed",
                track_id=lease.track_id,
                error_recorded=error_recorded,
            )

        if not _settlement_matches_claim(
            settlement,
            lease=lease,
            execution_session=execution_session,
            basket=basket,
            bars=bars,
        ):
            error_recorded = await self._set_error(
                lease,
                "Settlement store returned a valuation for a different command.",
                checked_at=observed_now,
            )
            return PortfolioPaperSettlementRunResult(
                status="failed",
                track_id=lease.track_id,
                error_recorded=error_recorded,
            )
        return PortfolioPaperSettlementRunResult(
            status="committed",
            track_id=lease.track_id,
            settlement_id=settlement.command.idempotency_key,
            asset_count=len(settlement.bar_set.bars),
        )

    async def _claim_cancellation_safe(
        self,
        claim_now: datetime,
    ) -> PortfolioPaperLease | None:
        claim_task = asyncio.create_task(
            asyncio.to_thread(
                self._store.claim_due_settlement,
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
            except BaseException:
                if cancellation_requested:
                    raise asyncio.CancelledError from None
                raise
            if cancellation_requested:
                if lease is not None:
                    await self._release_after_cancellation(lease, claim_now)
                raise asyncio.CancelledError
            return lease

    async def _get_cancellation_safe(
        self,
        lease: PortfolioPaperLease,
        observed_now: datetime,
    ) -> PortfolioPaperTrackRecord | None:
        get_task = asyncio.create_task(
            asyncio.to_thread(self._store.get, lease.track_id)
        )
        cancellation_requested = False
        while True:
            try:
                record = await asyncio.shield(get_task)
            except asyncio.CancelledError:
                if get_task.cancelled():
                    await self._release_after_cancellation(lease, observed_now)
                    raise
                cancellation_requested = True
                continue
            except BaseException:
                if cancellation_requested:
                    await self._release_after_cancellation(lease, observed_now)
                    raise asyncio.CancelledError from None
                await self._release_after_cancellation(lease, observed_now)
                raise
            if cancellation_requested:
                await self._release_after_cancellation(lease, observed_now)
                raise asyncio.CancelledError
            return record

    async def _collect_bars(
        self,
        *,
        basket: PortfolioPaperBasketContract,
        execution_session: str,
        execution_date: date,
    ) -> tuple[CertifiedPortfolioBar, ...]:
        instruments = basket.instruments
        if not 2 <= len(instruments) <= 6:
            raise ValueError(
                "Settlement requires between two and six durable instruments."
            )
        collected: list[CertifiedPortfolioBar | None] = [None] * len(instruments)
        shared_deadline = monotonic() + self._history_deadline_seconds

        async def collect_one(index: int) -> None:
            instrument = instruments[index]
            evidence = await self._provider.settlement_daily_bar(
                instrument.symbol,
                session=execution_date,
                deadline=shared_deadline,
            )
            collected[index] = _certify_target_bar(
                evidence,
                contract=instrument,
                execution_session=execution_session,
            )

        async with asyncio.timeout(self._history_deadline_seconds):
            async with asyncio.TaskGroup() as group:
                for index in range(len(instruments)):
                    group.create_task(collect_one(index))

        if any(bar is None for bar in collected):
            raise RuntimeError(
                "Settlement finalized-bar collection returned an incomplete set."
            )
        return tuple(bar for bar in collected if bar is not None)

    async def _commit_cancellation_safe(
        self,
        *,
        lease: PortfolioPaperLease,
        bars: Sequence[CertifiedPortfolioBar],
        observed_now: datetime,
    ) -> PortfolioPaperModeledCloseSettlement:
        commit_task = asyncio.create_task(
            asyncio.to_thread(
                self._store.commit_close_settlement_if_current,
                track_id=lease.track_id,
                owner=lease.owner,
                generation=lease.generation,
                expected_revision=lease.state_revision,
                bars=bars,
            )
        )
        cancellation_requested = False
        while True:
            try:
                settlement = await asyncio.shield(commit_task)
            except asyncio.CancelledError:
                if commit_task.cancelled():
                    raise
                cancellation_requested = True
                continue
            except BaseException:
                if cancellation_requested:
                    await self._release_after_cancellation(
                        lease,
                        observed_now,
                    )
                    raise asyncio.CancelledError from None
                raise
            if cancellation_requested:
                raise asyncio.CancelledError
            return settlement

    async def _release_after_cancellation(
        self,
        lease: PortfolioPaperLease,
        observed_now: datetime,
    ) -> None:
        release_task = asyncio.create_task(
            asyncio.to_thread(
                self._store.release_claim,
                track_id=lease.track_id,
                owner=lease.owner,
                generation=lease.generation,
                now=observed_now,
            )
        )
        while True:
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError:
                if release_task.cancelled():
                    return
                continue
            except BaseException:
                return
            return

    async def _set_error(
        self,
        lease: PortfolioPaperLease,
        message: str,
        *,
        checked_at: datetime,
    ) -> bool:
        error_task = asyncio.create_task(
            asyncio.to_thread(
                self._store.set_error_if_current,
                track_id=lease.track_id,
                owner=lease.owner,
                generation=lease.generation,
                expected_revision=lease.state_revision,
                message=message,
                checked_at=checked_at,
            )
        )
        cancellation_requested = False
        while True:
            try:
                recorded = await asyncio.shield(error_task)
            except asyncio.CancelledError:
                if error_task.cancelled():
                    raise
                cancellation_requested = True
                continue
            except Exception:
                if cancellation_requested:
                    raise asyncio.CancelledError from None
                return False
            if cancellation_requested:
                raise asyncio.CancelledError
            return recorded


def _normalize_owner_prefix(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Settlement owner prefix must be a string.")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > _MAXIMUM_OWNER_PREFIX_LENGTH
    ):
        raise ValueError(
            "Settlement owner prefix must be canonical and at most 167 characters."
        )
    return normalized


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


def _normalized_now(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("Settlement service time must be timezone-aware.")
    return value.astimezone(UTC)


def _settlement_context(
    record: PortfolioPaperTrackRecord | None,
    lease: PortfolioPaperLease,
    history_end: date,
) -> tuple[PortfolioPaperBasketContract, str, date] | None:
    try:
        if (
            record is None
            or record.id != lease.track_id
            or record.state_revision != lease.state_revision
            or record.state_revision != 0
            or record.refresh_owner != lease.owner
            or record.refresh_generation != lease.generation
            or record.refresh_lease_until != lease.expires_at
            or record.opening_batch_id is None
            or record.opening_session is None
            or record.opening_committed_at is None
            or record.settlement_id is not None
            or record.settlement_session is not None
            or record.settlement_committed_at is not None
            or record.pending_decision is None
        ):
            return None
        certificate = record.pending_decision.certificate
        execution_session, execution_date = _canonical_execution_session(
            certificate.execution_session
        )
        if (
            record.opening_session != execution_session
            or execution_date > history_end
        ):
            return None
        raw_basket = certificate.basket
        if not isinstance(raw_basket, PortfolioPaperBasketContract):
            return None
        basket = PortfolioPaperBasketContract.model_validate(
            raw_basket.model_dump(mode="python")
        )
        if not 2 <= len(basket.instruments) <= 6:
            return None
        return basket, execution_session, execution_date
    except (AttributeError, TypeError, ValueError):
        return None


def _canonical_execution_session(value: object) -> tuple[str, date]:
    if not isinstance(value, str) or not value:
        raise ValueError("Settlement execution session must be canonical text.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            "Settlement execution session must be a valid timestamp."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Settlement execution session must be timezone-aware.")
    normalized = parsed.astimezone(UTC)
    expected = datetime.combine(normalized.date(), time.min, tzinfo=UTC)
    if normalized != expected or value != normalized.isoformat():
        raise ValueError(
            "Settlement execution session must be canonical UTC midnight."
        )
    return value, normalized.date()


def _certify_target_bar(
    evidence: BinanceSettlementDailyBarEvidence,
    *,
    contract: PortfolioPaperInstrumentContract,
    execution_session: str,
) -> CertifiedPortfolioBar:
    if not isinstance(evidence, BinanceSettlementDailyBarEvidence):
        raise ValueError(
            "Binance settlement provider returned an invalid evidence type."
        )
    safe_evidence = BinanceSettlementDailyBarEvidence.model_validate(
        evidence.model_dump(mode="python")
    )
    if (
        safe_evidence.symbol != contract.symbol
        or safe_evidence.session != execution_session
    ):
        raise ValueError(
            "Binance settlement evidence identity does not match its contract."
        )
    bar = certify_binance_daily_bar(
        safe_evidence.model_dump(mode="python"),
        contract,
    )
    if bar.schema_version != 2 or bar.session != execution_session:
        raise ValueError(
            "Certified Binance bar does not match the execution session."
        )
    return bar


def _settlement_matches_claim(
    settlement: PortfolioPaperModeledCloseSettlement,
    *,
    lease: PortfolioPaperLease,
    execution_session: str,
    basket: PortfolioPaperBasketContract,
    bars: Sequence[CertifiedPortfolioBar],
) -> bool:
    try:
        symbols = tuple(instrument.symbol for instrument in basket.instruments)
        return (
            settlement.command.track_id == lease.track_id
            and settlement.command.source_state_revision == lease.state_revision
            and settlement.command.target_state_revision == 1
            and settlement.command.execution_session == execution_session
            and settlement.command.symbols == symbols
            and settlement.bar_set.execution_session == execution_session
            and settlement.bar_set.symbols == symbols
            and settlement.bar_set.bars == tuple(bars)
            and len(settlement.account.positions) == len(symbols)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _collection_failure_message(
    error: Exception,
    deadline_seconds: float,
) -> str:
    if isinstance(error, TimeoutError):
        return (
            "Settlement finalized-bar collection exceeded its shared "
            f"{deadline_seconds:g}-second deadline."
        )
    leaf = _first_exception_leaf(error)
    return (
        "Settlement finalized-bar collection failed "
        f"({type(leaf).__name__})."
    )


def _commit_failure_message(error: Exception) -> str:
    leaf = _first_exception_leaf(error)
    return f"Settlement atomic commit failed ({type(leaf).__name__})."


def _first_exception_leaf(error: BaseException) -> BaseException:
    current = error
    while isinstance(current, BaseExceptionGroup) and current.exceptions:
        current = current.exceptions[0]
    return current
