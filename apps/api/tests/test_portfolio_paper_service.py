from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event as ThreadEvent
from threading import get_ident
from types import SimpleNamespace
from typing import Any, cast

import pytest
from quantsieve_api.portfolio_paper import (
    PortfolioPaperLease,
    PortfolioPaperTrackStore,
)
from quantsieve_api.portfolio_paper_contracts import (
    PortfolioPaperBasketContract,
    PortfolioPaperInstrumentContract,
)
from quantsieve_api.portfolio_paper_service import (
    PortfolioPaperExecutionQuoteProvider,
    PortfolioPaperOpeningService,
)
from quantsieve_providers import BinanceSpotTradingRules, ExecutionQuote

TRACK_ID = "b" * 32
BATCH_ID = "a" * 64
NOW = datetime(2026, 7, 25, 0, 3, tzinfo=UTC)
RULES_VERIFIED_AT = NOW - timedelta(minutes=13)
SYMBOLS = (
    ("BTCUSDT", "BTC"),
    ("ETHUSDT", "ETH"),
    ("BNBUSDT", "BNB"),
    ("SOLUSDT", "SOL"),
    ("XRPUSDT", "XRP"),
    ("ADAUSDT", "ADA"),
)


class FatalCommit(BaseException):
    pass


def execution_basket(size: int) -> PortfolioPaperBasketContract:
    instruments = []
    for symbol, base_asset in SYMBOLS[:size]:
        rules = BinanceSpotTradingRules(
            symbol=symbol,
            base_asset=base_asset,
            quote_asset="USDT",
            status="TRADING",
            spot_trading_allowed=True,
            order_types=("LIMIT", "MARKET"),
            lot_step_size=Decimal("0.00001"),
            lot_min_quantity=Decimal("0.00001"),
            lot_max_quantity=Decimal("1000000"),
            market_step_size=Decimal("0.00001"),
            market_min_quantity=Decimal("0.00001"),
            market_max_quantity=Decimal("1000000"),
            min_notional=Decimal("5"),
            min_notional_applies_to_market=True,
            max_notional=None,
            max_notional_applies_to_market=False,
            notional_average_price_minutes=5,
            verified_at=RULES_VERIFIED_AT,
        )
        instruments.append(
            PortfolioPaperInstrumentContract(
                symbol=symbol,
                base_asset=base_asset,
                quote_currency="USDT",
                rules=rules,
                created_at=RULES_VERIFIED_AT + timedelta(minutes=2),
            )
        )
    return PortfolioPaperBasketContract(
        instruments=tuple(instruments),
        created_at=RULES_VERIFIED_AT + timedelta(minutes=2),
    )


def execution_quote(symbol: str, index: int) -> ExecutionQuote:
    price = Decimal(100 + index)
    return ExecutionQuote(
        symbol=symbol,
        provider="binance",
        venue="Binance Spot",
        bid_price=price - Decimal("0.01"),
        ask_price=price,
        bid_quantity=Decimal("1000000"),
        ask_quantity=Decimal("1000000"),
        notional_reference_price=price,
        notional_reference_kind="exchange_reference",
        notional_reference_window_minutes=None,
        notional_reference_at=NOW - timedelta(milliseconds=1500),
        notional_reference_observed_at=NOW - timedelta(milliseconds=1400),
        exchange_reference_available=True,
        exchange_reference_at=NOW - timedelta(milliseconds=1500),
        exchange_reference_observed_at=NOW - timedelta(milliseconds=1400),
        request_started_at=NOW - timedelta(seconds=2),
        observed_at=NOW - timedelta(seconds=1),
        exchange_server_time=NOW - timedelta(milliseconds=500),
        clock_checked_at=NOW - timedelta(milliseconds=500),
        cache_used=False,
    )


def fake_batch(*, fill_count: int, revision: int = 0) -> Any:
    return SimpleNamespace(
        command=SimpleNamespace(
            track_id=TRACK_ID,
            state_revision=revision,
            idempotency_key=BATCH_ID,
        ),
        fills=tuple(SimpleNamespace(symbol=symbol) for symbol, _ in SYMBOLS[:fill_count]),
    )


class FakeStore:
    def __init__(
        self,
        basket: PortfolioPaperBasketContract,
        *,
        no_due: bool = False,
        stale: bool = False,
        error_fence_current: bool = True,
        replay: bool = False,
    ) -> None:
        self.basket = basket
        self.no_due = no_due
        self.stale = stale
        self.error_fence_current = error_fence_current
        self.replay = replay
        self.replay_returned = False
        self.lease: PortfolioPaperLease | None = None
        self.thread_calls: list[tuple[str, int]] = []
        self.commit_calls: list[dict[str, Any]] = []
        self.error_calls: list[dict[str, Any]] = []
        self.release_calls: list[dict[str, Any]] = []
        self.claim_started: ThreadEvent | None = None
        self.claim_release: ThreadEvent | None = None
        self.get_started: ThreadEvent | None = None
        self.get_release: ThreadEvent | None = None
        self.commit_started: ThreadEvent | None = None
        self.commit_release: ThreadEvent | None = None
        self.commit_error: BaseException | None = None
        self.release_started: ThreadEvent | None = None
        self.release_release: ThreadEvent | None = None

    def claim_due(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        due_before: datetime | None = None,
    ) -> PortfolioPaperLease | None:
        del due_before
        self.thread_calls.append(("claim", get_ident()))
        if self.claim_started is not None:
            self.claim_started.set()
        if self.claim_release is not None:
            assert self.claim_release.wait(timeout=2)
        if self.no_due:
            return None
        self.lease = PortfolioPaperLease(
            track_id=TRACK_ID,
            owner=owner,
            generation=7,
            state_revision=0,
            expires_at=now + lease_for,
        )
        return self.lease

    def get(self, track_id: str) -> Any:
        self.thread_calls.append(("get", get_ident()))
        assert track_id == TRACK_ID
        assert self.lease is not None
        if self.get_started is not None:
            self.get_started.set()
        if self.get_release is not None:
            assert self.get_release.wait(timeout=2)
        return SimpleNamespace(
            id=TRACK_ID,
            state_revision=self.lease.state_revision,
            refresh_owner=self.lease.owner,
            refresh_generation=(
                self.lease.generation + 1 if self.stale else self.lease.generation
            ),
            refresh_lease_until=self.lease.expires_at,
            opening_batch_id=None,
            pending_decision=SimpleNamespace(
                certificate=SimpleNamespace(basket=self.basket)
            ),
        )

    def commit_opening_if_current(self, **kwargs: Any) -> Any:
        self.thread_calls.append(("commit", get_ident()))
        self.commit_calls.append(kwargs)
        if self.commit_started is not None:
            self.commit_started.set()
        if self.commit_release is not None:
            assert self.commit_release.wait(timeout=2)
        if self.commit_error is not None:
            raise self.commit_error
        if self.replay:
            self.replay_returned = True
        return fake_batch(fill_count=len(self.basket.instruments))

    def set_error_if_current(self, **kwargs: Any) -> bool:
        self.thread_calls.append(("error", get_ident()))
        self.error_calls.append(kwargs)
        return self.error_fence_current

    def release_claim(self, **kwargs: Any) -> bool:
        self.thread_calls.append(("release", get_ident()))
        self.release_calls.append(kwargs)
        if self.release_started is not None:
            self.release_started.set()
        if self.release_release is not None:
            assert self.release_release.wait(timeout=2)
        return True


class BarrierProvider:
    def __init__(
        self,
        basket: PortfolioPaperBasketContract,
        *,
        fail_symbol: str | None = None,
        never_finish: bool = False,
    ) -> None:
        self.basket = basket
        self.fail_symbol = fail_symbol
        self.never_finish = never_finish
        self.started: list[str] = []
        self.completed: list[str] = []
        self.cancelled: list[str] = []
        self._barrier = asyncio.Event()
        self._never = asyncio.Event()

    async def execution_quote(
        self,
        symbol: str,
        *,
        rules: BinanceSpotTradingRules,
    ) -> ExecutionQuote:
        assert rules.symbol == symbol
        self.started.append(symbol)
        if len(self.started) == len(self.basket.instruments):
            self._barrier.set()
        try:
            await asyncio.wait_for(self._barrier.wait(), timeout=1)
            if symbol == self.fail_symbol:
                await asyncio.sleep(0)
                raise RuntimeError("provider details must not escape")
            if self.never_finish or self.fail_symbol is not None:
                await self._never.wait()
            index = next(
                position
                for position, instrument in enumerate(self.basket.instruments)
                if instrument.symbol == symbol
            )
            await asyncio.sleep((len(self.basket.instruments) - index) / 1000)
            self.completed.append(symbol)
            return execution_quote(symbol, index)
        except asyncio.CancelledError:
            self.cancelled.append(symbol)
            raise

    async def wait_until_all_started(self) -> None:
        await asyncio.wait_for(self._barrier.wait(), timeout=1)


def service(
    store: FakeStore,
    provider: BarrierProvider,
    *,
    deadline: float = 4.0,
    lease_for: timedelta = timedelta(seconds=15),
) -> PortfolioPaperOpeningService:
    return PortfolioPaperOpeningService(
        store=cast(PortfolioPaperTrackStore, store),
        provider=cast(PortfolioPaperExecutionQuoteProvider, provider),
        owner_prefix="test-opening",
        quote_deadline_seconds=deadline,
        lease_for=lease_for,
    )


@pytest.mark.parametrize("size", [2, 6])
@pytest.mark.asyncio
async def test_collects_two_or_six_quotes_concurrently_and_commits_in_basket_order(
    size: int,
) -> None:
    basket = execution_basket(size)
    store = FakeStore(basket)
    provider = BarrierProvider(basket)
    worker = service(store, provider)
    main_thread = get_ident()

    result = await worker.run_once(now=NOW)

    assert result is not None
    assert result.status == "committed"
    assert result.batch_id == BATCH_ID
    assert result.fill_count == size
    assert provider.started == [instrument.symbol for instrument in basket.instruments]
    assert set(provider.completed) == set(provider.started)
    assert len(store.commit_calls) == 1
    committed_quotes = store.commit_calls[0]["quotes"]
    assert tuple(quote.symbol for quote in committed_quotes) == tuple(
        instrument.symbol for instrument in basket.instruments
    )
    assert all(thread_id != main_thread for _, thread_id in store.thread_calls)
    assert "quotes" not in result.model_dump()
    assert "certificate" not in result.model_dump()


@pytest.mark.asyncio
async def test_default_is_one_shared_four_second_quote_deadline() -> None:
    basket = execution_basket(2)
    store = FakeStore(basket)
    provider = BarrierProvider(basket)
    worker = service(store, provider)

    assert worker.quote_deadline_seconds == 4.0
    assert worker.lease_for.total_seconds() - worker.quote_deadline_seconds >= 5


@pytest.mark.asyncio
async def test_shared_deadline_cancels_every_quote_and_records_fenced_error() -> None:
    basket = execution_basket(6)
    store = FakeStore(basket)
    provider = BarrierProvider(basket, never_finish=True)
    worker = service(
        store,
        provider,
        deadline=0.03,
        lease_for=timedelta(seconds=6),
    )

    result = await worker.run_once(now=NOW)

    assert result is not None
    assert result.status == "failed"
    assert result.error_recorded is True
    assert store.commit_calls == []
    assert set(provider.cancelled) == {
        instrument.symbol for instrument in basket.instruments
    }
    assert len(store.error_calls) == 1
    error = store.error_calls[0]
    assert error["track_id"] == TRACK_ID
    assert error["owner"] == worker.owner
    assert error["generation"] == 7
    assert error["expected_revision"] == 0
    assert "0.03-second deadline" in error["message"]


@pytest.mark.asyncio
async def test_one_quote_failure_cancels_siblings_and_never_commits() -> None:
    basket = execution_basket(6)
    store = FakeStore(basket)
    provider = BarrierProvider(basket, fail_symbol="BNBUSDT")
    worker = service(store, provider)

    result = await worker.run_once(now=NOW)

    assert result is not None
    assert result.status == "failed"
    assert result.error_recorded is True
    assert store.commit_calls == []
    assert "BNBUSDT" not in provider.cancelled
    assert set(provider.cancelled) == {
        instrument.symbol
        for instrument in basket.instruments
        if instrument.symbol != "BNBUSDT"
    }
    assert store.error_calls[0]["message"] == (
        "Opening quote collection failed (RuntimeError)."
    )
    assert "provider details" not in store.error_calls[0]["message"]


@pytest.mark.asyncio
async def test_commit_waits_for_thread_completion_before_rethrowing_cancellation() -> None:
    basket = execution_basket(2)
    store = FakeStore(basket)
    store.commit_started = ThreadEvent()
    store.commit_release = ThreadEvent()
    provider = BarrierProvider(basket)
    worker = service(store, provider)
    task = asyncio.create_task(worker.run_once(now=NOW))

    assert await asyncio.to_thread(store.commit_started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()

    store.commit_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(store.commit_calls) == 1
    assert store.error_calls == []


@pytest.mark.asyncio
async def test_commit_fatal_base_exception_is_not_masked_by_pending_cancellation() -> None:
    basket = execution_basket(2)
    store = FakeStore(basket)
    store.commit_started = ThreadEvent()
    store.commit_release = ThreadEvent()
    store.commit_error = FatalCommit()
    provider = BarrierProvider(basket)
    worker = service(store, provider)
    task = asyncio.create_task(worker.run_once(now=NOW))

    assert await asyncio.to_thread(store.commit_started.wait, 1)
    task.cancel()
    store.commit_release.set()
    with pytest.raises(FatalCommit):
        await task


@pytest.mark.asyncio
async def test_quote_cancellation_safely_finishes_known_lease_release() -> None:
    basket = execution_basket(6)
    store = FakeStore(basket)
    store.release_started = ThreadEvent()
    store.release_release = ThreadEvent()
    provider = BarrierProvider(basket, never_finish=True)
    worker = service(store, provider)
    main_thread = get_ident()
    task = asyncio.create_task(worker.run_once(now=NOW))

    await provider.wait_until_all_started()
    task.cancel()
    assert await asyncio.to_thread(store.release_started.wait, 1)
    await asyncio.sleep(0.02)
    assert not task.done()

    store.release_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(store.release_calls) == 1
    release = store.release_calls[0]
    assert release["track_id"] == TRACK_ID
    assert release["owner"] == worker.owner
    assert release["generation"] == 7
    assert release["now"].tzinfo is UTC
    release_thread = next(
        thread_id for name, thread_id in store.thread_calls if name == "release"
    )
    assert release_thread != main_thread
    assert store.commit_calls == []


@pytest.mark.asyncio
async def test_claim_cancellation_waits_for_thread_and_releases_late_lease() -> None:
    basket = execution_basket(2)
    store = FakeStore(basket)
    store.claim_started = ThreadEvent()
    store.claim_release = ThreadEvent()
    provider = BarrierProvider(basket)
    worker = service(store, provider)
    task = asyncio.create_task(worker.run_once(now=NOW))

    assert await asyncio.to_thread(store.claim_started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()

    store.claim_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.lease is not None
    assert len(store.release_calls) == 1
    assert store.release_calls[0]["track_id"] == TRACK_ID
    assert provider.started == []
    assert [name for name, _ in store.thread_calls] == ["claim", "release"]


@pytest.mark.asyncio
async def test_cancelled_threaded_record_read_waits_and_releases_known_lease() -> None:
    basket = execution_basket(2)
    store = FakeStore(basket)
    store.get_started = ThreadEvent()
    store.get_release = ThreadEvent()
    provider = BarrierProvider(basket)
    worker = service(store, provider)
    task = asyncio.create_task(worker.run_once(now=NOW))

    assert await asyncio.to_thread(store.get_started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()

    store.get_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.lease is not None
    assert store.lease.expires_at - NOW == timedelta(seconds=15)
    assert len(store.release_calls) == 1
    assert store.release_calls[0]["track_id"] == TRACK_ID
    assert provider.started == []


@pytest.mark.asyncio
async def test_no_due_track_returns_none_without_touching_provider() -> None:
    basket = execution_basket(2)
    store = FakeStore(basket, no_due=True)
    provider = BarrierProvider(basket)
    worker = service(store, provider)

    assert await worker.run_once(now=NOW) is None
    assert provider.started == []
    assert store.commit_calls == []
    assert [name for name, _ in store.thread_calls] == ["claim"]


@pytest.mark.asyncio
async def test_stale_record_is_rejected_before_network_and_error_write_is_fenced() -> None:
    basket = execution_basket(2)
    store = FakeStore(basket, stale=True, error_fence_current=False)
    provider = BarrierProvider(basket)
    worker = service(store, provider)

    result = await worker.run_once(now=NOW)

    assert result is not None
    assert result.status == "stale"
    assert result.error_recorded is False
    assert provider.started == []
    assert store.commit_calls == []
    assert store.error_calls[0]["owner"] == worker.owner
    assert store.error_calls[0]["generation"] == 7
    assert store.error_calls[0]["expected_revision"] == 0


@pytest.mark.asyncio
async def test_store_returning_an_existing_idempotent_batch_is_success() -> None:
    basket = execution_basket(2)
    store = FakeStore(basket, replay=True)
    provider = BarrierProvider(basket)
    worker = service(store, provider)

    first = await worker.run_once(now=NOW)

    assert store.replay_returned is True
    assert first is not None
    assert first.status == "committed"
    assert first.batch_id == BATCH_ID
    assert first.error_recorded is False


def test_owner_is_unique_and_constructor_rejects_unsafe_timing() -> None:
    basket = execution_basket(2)
    first = service(FakeStore(basket), BarrierProvider(basket))
    second = service(FakeStore(basket), BarrierProvider(basket))

    assert first.owner != second.owner
    assert first.owner.startswith("test-opening:")
    with pytest.raises(ValueError, match="canonical"):
        PortfolioPaperOpeningService(
            store=cast(PortfolioPaperTrackStore, FakeStore(basket)),
            provider=cast(
                PortfolioPaperExecutionQuoteProvider,
                BarrierProvider(basket),
            ),
            owner_prefix=" leading-space",
        )
    with pytest.raises(ValueError, match="five seconds"):
        service(
            FakeStore(basket),
            BarrierProvider(basket),
            deadline=4,
            lease_for=timedelta(seconds=8),
        )
