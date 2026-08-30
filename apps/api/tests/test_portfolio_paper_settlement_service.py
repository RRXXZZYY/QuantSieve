from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from inspect import signature
from threading import Event as ThreadEvent
from threading import get_ident
from types import SimpleNamespace
from typing import Any, cast

import pytest
from quantsieve_api.portfolio_paper import PortfolioPaperLease
from quantsieve_api.portfolio_paper_contracts import (
    PortfolioPaperBasketContract,
    PortfolioPaperInstrumentContract,
)
from quantsieve_api.portfolio_paper_settlement_service import (
    PortfolioPaperSettlementHistoryProvider,
    PortfolioPaperSettlementService,
    PortfolioPaperSettlementStore,
)
from quantsieve_providers import (
    BinanceSettlementDailyBarEvidence,
    BinanceSpotTradingRules,
)

TRACK_ID = "b" * 32
SETTLEMENT_ID = "a" * 64
EXECUTION_OPEN = datetime(2026, 7, 25, tzinfo=UTC)
EXECUTION_SESSION = EXECUTION_OPEN.isoformat()
NOW = datetime(2026, 7, 27, 8, 30, tzinfo=UTC)
RULES_VERIFIED_AT = EXECUTION_OPEN - timedelta(minutes=10)
SYMBOLS = (
    ("BTCUSDT", "BTC"),
    ("ETHUSDT", "ETH"),
    ("BNBUSDT", "BNB"),
    ("SOLUSDT", "SOL"),
    ("XRPUSDT", "XRP"),
    ("ADAUSDT", "ADA"),
)


def settlement_basket(size: int) -> PortfolioPaperBasketContract:
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
                created_at=RULES_VERIFIED_AT + timedelta(minutes=1),
            )
        )
    return PortfolioPaperBasketContract(
        instruments=tuple(instruments),
        created_at=RULES_VERIFIED_AT + timedelta(minutes=1),
    )


def settlement_evidence(
    symbol: str,
    *,
    open_time: datetime = EXECUTION_OPEN,
    close: str = "105",
) -> BinanceSettlementDailyBarEvidence:
    close_time = open_time + timedelta(days=1) - timedelta(milliseconds=1)
    finalized_at = close_time + timedelta(minutes=2)
    return BinanceSettlementDailyBarEvidence(
        symbol=symbol,
        session=open_time.isoformat(),
        request_started_at=NOW - timedelta(seconds=1),
        observed_at=NOW,
        exchange_server_time=NOW - timedelta(milliseconds=100),
        clock_checked_at=NOW - timedelta(milliseconds=100),
        open_time=open_time,
        close_time=close_time,
        finalized_at=finalized_at,
        open=100.0,
        high=110.0,
        low=90.0,
        close=float(close),
        volume=1000.0,
        exact_open="100",
        exact_high="110",
        exact_low="90",
        exact_close=close,
        exact_volume="1000",
    )


class FakeStore:
    def __init__(
        self,
        basket: PortfolioPaperBasketContract,
        *,
        no_due: bool = False,
        stale: bool = False,
        error_fence_current: bool = True,
    ) -> None:
        self.basket = basket
        self.no_due = no_due
        self.stale = stale
        self.error_fence_current = error_fence_current
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
        self.error_started: ThreadEvent | None = None
        self.error_release: ThreadEvent | None = None
        self.commit_error: BaseException | None = None
        self.replay_returned = False

    def claim_due_settlement(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
    ) -> PortfolioPaperLease | None:
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
            generation=11,
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
            state_revision=0,
            refresh_owner=self.lease.owner,
            refresh_generation=(
                self.lease.generation + 1
                if self.stale
                else self.lease.generation
            ),
            refresh_lease_until=self.lease.expires_at,
            opening_batch_id="c" * 64,
            opening_session=EXECUTION_SESSION,
            opening_committed_at=EXECUTION_OPEN + timedelta(minutes=3),
            settlement_id=None,
            settlement_session=None,
            settlement_committed_at=None,
            pending_decision=SimpleNamespace(
                certificate=SimpleNamespace(
                    basket=self.basket,
                    execution_session=EXECUTION_SESSION,
                )
            ),
        )

    def commit_close_settlement_if_current(self, **kwargs: Any) -> Any:
        self.thread_calls.append(("commit", get_ident()))
        self.commit_calls.append(kwargs)
        if self.commit_started is not None:
            self.commit_started.set()
        if self.commit_release is not None:
            assert self.commit_release.wait(timeout=2)
        if self.commit_error is not None:
            raise self.commit_error
        bars = tuple(kwargs["bars"])
        self.replay_returned = True
        return SimpleNamespace(
            command=SimpleNamespace(
                track_id=TRACK_ID,
                source_state_revision=0,
                target_state_revision=1,
                execution_session=EXECUTION_SESSION,
                symbols=tuple(bar.symbol for bar in bars),
                idempotency_key=SETTLEMENT_ID,
            ),
            bar_set=SimpleNamespace(
                execution_session=EXECUTION_SESSION,
                symbols=tuple(bar.symbol for bar in bars),
                bars=bars,
            ),
            account=SimpleNamespace(
                positions=tuple(object() for _bar in bars),
            ),
        )

    def set_error_if_current(self, **kwargs: Any) -> bool:
        self.thread_calls.append(("error", get_ident()))
        self.error_calls.append(kwargs)
        if self.error_started is not None:
            self.error_started.set()
        if self.error_release is not None:
            assert self.error_release.wait(timeout=2)
        return self.error_fence_current

    def release_claim(self, **kwargs: Any) -> bool:
        self.thread_calls.append(("release", get_ident()))
        self.release_calls.append(kwargs)
        return True


class BarrierHistoryProvider:
    def __init__(
        self,
        basket: PortfolioPaperBasketContract,
        *,
        evidence: dict[str, BinanceSettlementDailyBarEvidence] | None = None,
        fail_symbol: str | None = None,
        never_finish: bool = False,
    ) -> None:
        self.basket = basket
        self.evidence = evidence or {
            instrument.symbol: settlement_evidence(instrument.symbol)
            for instrument in basket.instruments
        }
        self.fail_symbol = fail_symbol
        self.never_finish = never_finish
        self.started: list[str] = []
        self.completed: list[str] = []
        self.cancelled: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self._barrier = asyncio.Event()
        self._never = asyncio.Event()

    async def settlement_daily_bar(
        self,
        symbol: str,
        *,
        session: date,
        deadline: float,
    ) -> BinanceSettlementDailyBarEvidence:
        self.started.append(symbol)
        self.calls.append(
            {
                "symbol": symbol,
                "session": session,
                "deadline": deadline,
            }
        )
        if len(self.started) == len(self.basket.instruments):
            self._barrier.set()
        try:
            await asyncio.wait_for(self._barrier.wait(), timeout=1)
            if symbol == self.fail_symbol:
                await asyncio.sleep(0)
                raise RuntimeError("private upstream response must not escape")
            if self.never_finish or self.fail_symbol is not None:
                await self._never.wait()
            self.completed.append(symbol)
            return self.evidence[symbol]
        except asyncio.CancelledError:
            self.cancelled.append(symbol)
            raise

    async def wait_until_all_started(self) -> None:
        await asyncio.wait_for(self._barrier.wait(), timeout=1)


def fixed_clock() -> datetime:
    return NOW


def service(
    store: FakeStore,
    provider: BarrierHistoryProvider,
    *,
    deadline: float = 20.0,
    lease_for: timedelta = timedelta(seconds=30),
    clock: Callable[[], datetime] = fixed_clock,
) -> PortfolioPaperSettlementService:
    return PortfolioPaperSettlementService(
        store=cast(PortfolioPaperSettlementStore, store),
        provider=cast(PortfolioPaperSettlementHistoryProvider, provider),
        owner_prefix="test-settlement",
        history_deadline_seconds=deadline,
        lease_for=lease_for,
        _clock=clock,
    )


@pytest.mark.parametrize("size", [2, 6])
@pytest.mark.asyncio
async def test_collects_two_or_six_histories_concurrently_in_basket_order(
    size: int,
) -> None:
    basket = settlement_basket(size)
    store = FakeStore(basket)
    provider = BarrierHistoryProvider(basket)
    worker = service(store, provider)
    main_thread = get_ident()

    result = await worker.run_once()

    assert result is not None
    assert result.status == "committed"
    assert result.settlement_id == SETTLEMENT_ID
    assert result.asset_count == size
    assert provider.started == [
        instrument.symbol for instrument in basket.instruments
    ]
    assert set(provider.completed) == set(provider.started)
    assert all(
        call["session"] == EXECUTION_OPEN.date()
        and isinstance(call["deadline"], float)
        for call in provider.calls
    )
    assert len({call["deadline"] for call in provider.calls}) == 1
    committed_bars = store.commit_calls[0]["bars"]
    assert tuple(bar.symbol for bar in committed_bars) == tuple(
        instrument.symbol for instrument in basket.instruments
    )
    assert all(thread_id != main_thread for _, thread_id in store.thread_calls)
    assert "bars" not in result.model_dump()
    assert "account" not in result.model_dump()
    assert "command" not in result.model_dump()


@pytest.mark.asyncio
async def test_typed_exact_evidence_is_certified_without_generic_history() -> None:
    basket = settlement_basket(2)
    evidence = {
        instrument.symbol: settlement_evidence(
            instrument.symbol,
            close=str(101 + index),
        )
        for index, instrument in enumerate(basket.instruments)
    }
    store = FakeStore(basket)
    provider = BarrierHistoryProvider(basket, evidence=evidence)

    result = await service(store, provider).run_once()

    assert result is not None and result.status == "committed"
    assert tuple(
        bar.exact_close for bar in store.commit_calls[0]["bars"]
    ) == ("101", "102")
    assert all(
        bar.session == EXECUTION_SESSION
        for bar in store.commit_calls[0]["bars"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", "ETHUSDT"),
        (
            "session",
            (EXECUTION_OPEN + timedelta(days=1)).isoformat(),
        ),
        ("cache_used", True),
        ("exchange_clock_verified", False),
        ("finalization_lag_seconds", 121),
        ("finalized", False),
        (
            "close_time",
            EXECUTION_OPEN + timedelta(days=2) - timedelta(milliseconds=1),
        ),
        (
            "finalized_at",
            EXECUTION_OPEN + timedelta(days=1, minutes=3)
            - timedelta(milliseconds=1),
        ),
        ("close", float("nan")),
        ("exact_close", ""),
    ],
)
@pytest.mark.asyncio
async def test_forged_typed_evidence_never_reaches_atomic_commit(
    field: str,
    value: object,
) -> None:
    basket = settlement_basket(2)
    evidence = {
        instrument.symbol: settlement_evidence(instrument.symbol)
        for instrument in basket.instruments
    }
    evidence["BTCUSDT"] = evidence["BTCUSDT"].model_copy(
        update={field: value}
    )
    store = FakeStore(basket)

    result = await service(
        store,
        BarrierHistoryProvider(basket, evidence=evidence),
    ).run_once()

    assert result is not None and result.status == "failed"
    assert result.error_recorded is True
    assert store.commit_calls == []
    assert len(store.error_calls) == 1
    assert store.error_calls[0]["message"].startswith(
        "Settlement finalized-bar collection failed ("
    )


@pytest.mark.asyncio
async def test_one_history_failure_cancels_siblings_without_leaking_details() -> None:
    basket = settlement_basket(6)
    store = FakeStore(basket)
    provider = BarrierHistoryProvider(basket, fail_symbol="BNBUSDT")

    result = await service(store, provider).run_once()

    assert result is not None and result.status == "failed"
    assert store.commit_calls == []
    assert "BNBUSDT" not in provider.cancelled
    assert set(provider.cancelled) == {
        instrument.symbol
        for instrument in basket.instruments
        if instrument.symbol != "BNBUSDT"
    }
    message = store.error_calls[0]["message"]
    assert message == (
        "Settlement finalized-bar collection failed (RuntimeError)."
    )
    assert "private upstream" not in message


@pytest.mark.asyncio
async def test_shared_deadline_cancels_all_histories_and_records_generic_error() -> None:
    basket = settlement_basket(6)
    store = FakeStore(basket)
    provider = BarrierHistoryProvider(basket, never_finish=True)
    worker = service(
        store,
        provider,
        deadline=0.03,
        lease_for=timedelta(seconds=6),
    )

    result = await worker.run_once()

    assert result is not None and result.status == "failed"
    assert store.commit_calls == []
    assert set(provider.cancelled) == {
        instrument.symbol for instrument in basket.instruments
    }
    assert "0.03-second deadline" in store.error_calls[0]["message"]


@pytest.mark.asyncio
async def test_stale_lease_is_rejected_before_any_network_call() -> None:
    basket = settlement_basket(2)
    store = FakeStore(
        basket,
        stale=True,
        error_fence_current=False,
    )
    provider = BarrierHistoryProvider(basket)

    result = await service(store, provider).run_once()

    assert result is not None and result.status == "stale"
    assert result.error_recorded is False
    assert provider.started == []
    assert store.commit_calls == []
    assert store.error_calls[0]["generation"] == 11
    assert store.error_calls[0]["expected_revision"] == 0


@pytest.mark.asyncio
async def test_no_due_settlement_returns_none_without_network() -> None:
    basket = settlement_basket(2)
    store = FakeStore(basket, no_due=True)
    provider = BarrierHistoryProvider(basket)

    assert await service(store, provider).run_once() is None
    assert provider.started == []
    assert [name for name, _thread in store.thread_calls] == ["claim"]


@pytest.mark.asyncio
async def test_collection_cancellation_finishes_known_lease_release() -> None:
    basket = settlement_basket(6)
    store = FakeStore(basket)
    provider = BarrierHistoryProvider(basket, never_finish=True)
    worker = service(store, provider)
    task = asyncio.create_task(worker.run_once())

    await provider.wait_until_all_started()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert set(provider.cancelled) == {
        instrument.symbol for instrument in basket.instruments
    }
    assert len(store.release_calls) == 1
    assert store.release_calls[0]["track_id"] == TRACK_ID
    assert store.commit_calls == []


@pytest.mark.asyncio
async def test_error_recording_finishes_before_cancellation_propagates() -> None:
    basket = settlement_basket(2)
    store = FakeStore(basket)
    store.error_started = ThreadEvent()
    store.error_release = ThreadEvent()
    provider = BarrierHistoryProvider(basket, fail_symbol="BTCUSDT")
    task = asyncio.create_task(service(store, provider).run_once())

    assert await asyncio.to_thread(store.error_started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    store.error_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(store.error_calls) == 1


@pytest.mark.asyncio
async def test_claim_cancellation_waits_for_late_lease_then_releases_it() -> None:
    basket = settlement_basket(2)
    store = FakeStore(basket)
    store.claim_started = ThreadEvent()
    store.claim_release = ThreadEvent()
    provider = BarrierHistoryProvider(basket)
    worker = service(store, provider)
    task = asyncio.create_task(worker.run_once())

    assert await asyncio.to_thread(store.claim_started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    store.claim_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(store.release_calls) == 1
    assert provider.started == []
    assert [name for name, _thread in store.thread_calls] == [
        "claim",
        "release",
    ]


@pytest.mark.asyncio
async def test_get_cancellation_waits_for_read_then_releases_lease() -> None:
    basket = settlement_basket(2)
    store = FakeStore(basket)
    store.get_started = ThreadEvent()
    store.get_release = ThreadEvent()
    provider = BarrierHistoryProvider(basket)
    worker = service(store, provider)
    task = asyncio.create_task(worker.run_once())

    assert await asyncio.to_thread(store.get_started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    store.get_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(store.release_calls) == 1
    assert provider.started == []


@pytest.mark.asyncio
async def test_commit_is_shielded_until_atomic_store_call_finishes() -> None:
    basket = settlement_basket(2)
    store = FakeStore(basket)
    store.commit_started = ThreadEvent()
    store.commit_release = ThreadEvent()
    provider = BarrierHistoryProvider(basket)
    task = asyncio.create_task(service(store, provider).run_once())

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
async def test_cancelled_failed_commit_releases_its_known_lease() -> None:
    basket = settlement_basket(2)
    store = FakeStore(basket)
    store.commit_started = ThreadEvent()
    store.commit_release = ThreadEvent()
    store.commit_error = RuntimeError("private commit failure")
    provider = BarrierHistoryProvider(basket)
    task = asyncio.create_task(service(store, provider).run_once())

    assert await asyncio.to_thread(store.commit_started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    store.commit_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(store.commit_calls) == 1
    assert len(store.release_calls) == 1
    assert store.error_calls == []


@pytest.mark.asyncio
async def test_idempotent_store_replay_is_reported_as_committed() -> None:
    basket = settlement_basket(2)
    store = FakeStore(basket)
    provider = BarrierHistoryProvider(basket)

    result = await service(store, provider).run_once()

    assert store.replay_returned is True
    assert result is not None
    assert result.status == "committed"
    assert result.settlement_id == SETTLEMENT_ID
    assert result.error_recorded is False


@pytest.mark.asyncio
async def test_run_uses_one_trusted_clock_observation_for_the_entire_window() -> None:
    basket = settlement_basket(2)
    store = FakeStore(basket)
    provider = BarrierHistoryProvider(basket)
    clock_calls = 0

    def one_shot_clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return NOW if clock_calls == 1 else NOW + timedelta(days=10_000)

    result = await service(
        store,
        provider,
        clock=one_shot_clock,
    ).run_once()

    assert result is not None and result.status == "committed"
    assert clock_calls == 1
    assert all(
        call["session"] == EXECUTION_OPEN.date()
        for call in provider.calls
    )


@pytest.mark.asyncio
async def test_invalid_private_clock_fails_before_claim_or_network() -> None:
    basket = settlement_basket(2)
    store = FakeStore(basket)
    provider = BarrierHistoryProvider(basket)
    worker = service(
        store,
        provider,
        clock=lambda: datetime(2026, 7, 27, 8, 30),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        await worker.run_once()

    assert store.thread_calls == []
    assert provider.started == []


def test_run_once_has_no_caller_controlled_time_parameter() -> None:
    basket = settlement_basket(2)
    worker = service(FakeStore(basket), BarrierHistoryProvider(basket))

    assert tuple(signature(worker.run_once).parameters) == ()


def test_defaults_owner_uniqueness_and_timing_validation() -> None:
    basket = settlement_basket(2)
    first = service(FakeStore(basket), BarrierHistoryProvider(basket))
    second = service(FakeStore(basket), BarrierHistoryProvider(basket))

    assert first.owner != second.owner
    assert first.owner.startswith("test-settlement:")
    assert first.history_deadline_seconds == 20.0
    assert first.lease_for == timedelta(seconds=30)
    with pytest.raises(ValueError, match="canonical"):
        PortfolioPaperSettlementService(
            store=cast(
                PortfolioPaperSettlementStore,
                FakeStore(basket),
            ),
            provider=cast(
                PortfolioPaperSettlementHistoryProvider,
                BarrierHistoryProvider(basket),
            ),
            owner_prefix=" leading-space",
        )
    with pytest.raises(ValueError, match="five seconds"):
        service(
            FakeStore(basket),
            BarrierHistoryProvider(basket),
            deadline=20,
            lease_for=timedelta(seconds=24),
        )
