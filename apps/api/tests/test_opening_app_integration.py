from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event as ThreadEvent
from typing import Any

import pytest
import quantsieve_api.portfolio_paper as portfolio_paper_module
from fastapi.testclient import TestClient
from quantsieve_api.config import Settings
from quantsieve_api.main import create_app
from quantsieve_api.portfolio_paper import (
    PortfolioPaperConfig,
    PortfolioPaperLease,
    PortfolioPaperTrackRecord,
    PortfolioPaperTrackStore,
)
from quantsieve_api.portfolio_paper_contracts import (
    PortfolioPaperBasketContract,
    assess_portfolio_paper_eligibility,
    canonical_payload_hash,
    certify_binance_daily_bar,
    certify_portfolio_paper_decision,
)
from quantsieve_api.portfolio_paper_scheduler import (
    PortfolioPaperOpeningScheduler,
    PortfolioPaperOpeningSchedulerStatus,
)
from quantsieve_engine import PortfolioForwardTarget
from quantsieve_providers import (
    BinanceSpotTradingRules,
    ExecutionQuote,
    Instrument,
)

BAR_SESSION = datetime(2026, 7, 26, tzinfo=UTC)
EXECUTION_SESSION = BAR_SESSION + timedelta(days=1)
BAR_CLOSE = EXECUTION_SESSION - timedelta(milliseconds=1)
BAR_FINALIZED = BAR_CLOSE + timedelta(minutes=2)
BAR_OBSERVED = BAR_FINALIZED + timedelta(seconds=1)
RULES_VERIFIED_AT = BAR_OBSERVED + timedelta(seconds=1)
BASKET_CREATED_AT = RULES_VERIFIED_AT + timedelta(seconds=1)
DECIDED_AT = BASKET_CREATED_AT + timedelta(seconds=1)
DATABASE_NOW = EXECUTION_SESSION + timedelta(minutes=3)
SYMBOLS = (("BTCUSDT", "BTC"), ("ETHUSDT", "ETH"))


class DeterministicOpeningQuoteProvider:
    """Offline quote source whose barrier proves both requests overlap."""

    def __init__(self, *, failure_detail: str | None = None) -> None:
        self.failure_detail = failure_detail
        self.calls: list[str] = []
        self._all_started: asyncio.Event | None = None

    async def execution_quote(
        self,
        symbol: str,
        *,
        rules: BinanceSpotTradingRules,
    ) -> ExecutionQuote:
        assert rules.symbol == symbol
        if self._all_started is None:
            self._all_started = asyncio.Event()
        self.calls.append(symbol)
        if len(self.calls) == len(SYMBOLS):
            self._all_started.set()
        await asyncio.wait_for(self._all_started.wait(), timeout=1)
        if self.failure_detail is not None:
            raise RuntimeError(self.failure_detail)

        ask = {
            "BTCUSDT": Decimal("118001"),
            "ETHUSDT": Decimal("3701"),
        }[symbol]
        return ExecutionQuote(
            symbol=symbol,
            provider="binance",
            venue="Binance Spot",
            bid_price=ask - Decimal("1"),
            ask_price=ask,
            bid_quantity=Decimal("1000000"),
            ask_quantity=Decimal("1000000"),
            notional_reference_price=ask,
            notional_reference_kind="exchange_reference",
            notional_reference_window_minutes=None,
            notional_reference_at=DATABASE_NOW,
            notional_reference_observed_at=DATABASE_NOW,
            exchange_reference_available=True,
            exchange_reference_at=DATABASE_NOW,
            exchange_reference_observed_at=DATABASE_NOW,
            request_started_at=DATABASE_NOW,
            observed_at=DATABASE_NOW,
            exchange_server_time=DATABASE_NOW,
            clock_checked_at=DATABASE_NOW,
            cache_used=False,
        )


def _eligible_basket() -> PortfolioPaperBasketContract:
    contracts = []
    for symbol, base_asset in SYMBOLS:
        instrument = Instrument(
            symbol=symbol,
            name=f"{base_asset} / USDT",
            market="CRYPTO",
            exchange="Binance Spot",
            currency="USDT",
            provider="binance",
            asset_type="spot",
        )
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
        assessment = assess_portfolio_paper_eligibility(
            instrument,
            rules=rules,
            now=BASKET_CREATED_AT,
        )
        assert assessment.contract is not None
        contracts.append(assessment.contract)
    return PortfolioPaperBasketContract(
        instruments=tuple(contracts),
        created_at=BASKET_CREATED_AT,
    )


def _create_due_track(store: PortfolioPaperTrackStore) -> PortfolioPaperTrackRecord:
    basket = _eligible_basket()
    configuration = PortfolioPaperConfig(
        portfolio_experiment_id="app-opening-integration",
        symbols=tuple(symbol for symbol, _ in SYMBOLS),
        basket_identity=basket.identity,
        method="periodic_equal",
        initial_cash=100_000,
        fee_rate=0.0003,
        slippage_rate=0.0002,
        volatility_lookback=60,
        rebalance_bars=21,
        maximum_asset_weight=0.6,
    )
    rows = {
        "BTCUSDT": (117_000.0, 119_000.0, 116_000.0, 118_000.0, 1_000.0),
        "ETHUSDT": (3_650.0, 3_750.0, 3_600.0, 3_700.0, 10_000.0),
    }
    bars = []
    for contract in basket.instruments:
        open_price, high, low, close, volume = rows[contract.symbol]
        bars.append(
            certify_binance_daily_bar(
                {
                    "symbol": contract.symbol,
                    "interval": "1d",
                    "open_time": BAR_SESSION.isoformat(),
                    "close_time": BAR_CLOSE.isoformat(),
                    "finalized_at": BAR_FINALIZED.isoformat(),
                    "observed_at": BAR_OBSERVED.isoformat(),
                    "exchange_server_time": BAR_OBSERVED.isoformat(),
                    "clock_checked_at": BAR_OBSERVED.isoformat(),
                    "exchange_clock_verified": True,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "exact_open": str(open_price),
                    "exact_high": str(high),
                    "exact_low": str(low),
                    "exact_close": str(close),
                    "exact_volume": str(volume),
                    "finalized": True,
                },
                contract,
            )
        )
    target = PortfolioForwardTarget(
        method="periodic_equal",
        information_session=BAR_SESSION,
        weights={"BTCUSDT": 0.5, "ETHUSDT": 0.5},
    )
    certificate = certify_portfolio_paper_decision(
        target=target,
        bars=tuple(bars),
        basket=basket,
        configuration_hash=canonical_payload_hash(
            configuration.model_dump(mode="json")
        ),
        volatility_lookback=configuration.volatility_lookback,
        maximum_asset_weight=configuration.maximum_asset_weight,
        decided_at=DECIDED_AT,
    )
    return store.create(configuration, certificate)


def _settings(database: Path, cache: Path) -> Settings:
    return Settings(
        build_version="opening-app-integration",
        database_path=str(database),
        cache_path=str(cache),
        monitor_scheduler_enabled=False,
        paper_scheduler_enabled=False,
        portfolio_opening_scheduler_enabled=True,
        portfolio_opening_poll_seconds=3600.0,
        portfolio_opening_quote_deadline_seconds=2.0,
        portfolio_opening_lease_seconds=10.0,
    )


def _wait_for_outcome(
    scheduler: PortfolioPaperOpeningScheduler,
    expected: str,
    *,
    timeout_seconds: float = 3,
) -> PortfolioPaperOpeningSchedulerStatus:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = scheduler.status
        result_is_complete = (
            status.last_outcome == expected
            and (
                expected not in {"failed", "stale"}
                or status.consecutive_failures > 0
            )
        )
        if result_is_complete:
            return status
        time.sleep(0.01)
    raise AssertionError(f"Opening scheduler did not report {expected!r}.")


def test_app_lifespan_claims_quotes_and_atomically_commits_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: DATABASE_NOW,
    )
    database = tmp_path / "opening-app.db"
    provider = DeterministicOpeningQuoteProvider()
    app = create_app(
        _settings(database, tmp_path / "opening-cache.db"),
        portfolio_opening_provider=provider,
    )
    store = app.state.portfolio_paper_track_store
    track = _create_due_track(store)
    scheduler = app.state.portfolio_opening_scheduler

    assert app.state.portfolio_opening_provider is provider
    assert scheduler.status.running is False
    with TestClient(app):
        status = _wait_for_outcome(scheduler, "committed")

        assert status.running is True
        assert status.consecutive_failures == 0
        assert set(provider.calls) == {"BTCUSDT", "ETHUSDT"}
        assert len(provider.calls) == 2
        persisted = store.get(track.id)
        assert persisted is not None
        assert persisted.opening_batch_id is not None
        assert persisted.opening_session == EXECUTION_SESSION.isoformat()
        assert persisted.last_error is None
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM portfolio_paper_execution_batches "
                "WHERE track_id = ?",
                (track.id,),
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT COUNT(*) FROM portfolio_paper_modeled_fills "
                "WHERE track_id = ?",
                (track.id,),
            ).fetchone() == (2,)

    assert scheduler.status.running is False


def test_app_records_generic_quote_failure_without_status_leakage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: DATABASE_NOW,
    )
    secret = "upstream-secret-host and credential-token"
    provider = DeterministicOpeningQuoteProvider(failure_detail=secret)
    app = create_app(
        _settings(tmp_path / "opening-failure.db", tmp_path / "failure-cache.db"),
        portfolio_opening_provider=provider,
    )
    store = app.state.portfolio_paper_track_store
    track = _create_due_track(store)
    scheduler = app.state.portfolio_opening_scheduler

    with TestClient(app) as api:
        status = _wait_for_outcome(scheduler, "failed")
        public_status = api.get("/api/v1/portfolio-paper/opening-status")

        assert status.running is True
        assert status.consecutive_failures == 1
        assert public_status.status_code == 200
        assert public_status.json() == {
            "availability": "internal_only",
            "enabled": True,
            "running": True,
        }
        assert track.id not in public_status.text
        assert secret not in public_status.text
        assert scheduler.status.last_error == (
            "Opening attempt reported a generic failure."
        )
        persisted = store.get(track.id)
        assert persisted is not None
        assert persisted.opening_batch_id is None
        assert persisted.last_error == (
            "Opening quote collection failed (RuntimeError)."
        )
        assert secret not in persisted.last_error
        with sqlite3.connect(tmp_path / "opening-failure.db") as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM portfolio_paper_execution_batches "
                "WHERE track_id = ?",
                (track.id,),
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT COUNT(*) FROM portfolio_paper_modeled_fills "
                "WHERE track_id = ?",
                (track.id,),
            ).fetchone() == (0,)

    assert scheduler.status.running is False


@pytest.mark.asyncio
async def test_app_shutdown_waits_for_blocked_claim_and_releases_late_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: DATABASE_NOW,
    )
    provider = DeterministicOpeningQuoteProvider()
    app = create_app(
        _settings(tmp_path / "blocked-claim.db", tmp_path / "blocked-cache.db"),
        portfolio_opening_provider=provider,
    )
    store = app.state.portfolio_paper_track_store
    track = _create_due_track(store)
    claim_started = ThreadEvent()
    allow_claim = ThreadEvent()
    original_claim = store.claim_due

    def blocked_claim(**kwargs: Any) -> PortfolioPaperLease | None:
        claim_started.set()
        if not allow_claim.wait(timeout=5):
            raise TimeoutError("Blocked claim test gate timed out.")
        return original_claim(**kwargs)

    monkeypatch.setattr(store, "claim_due", blocked_claim)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    shutdown: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(claim_started.wait, 2)

        async def close_lifespan() -> None:
            await lifespan.__aexit__(None, None, None)

        shutdown = asyncio.create_task(close_lifespan())
        await asyncio.sleep(0.02)
        assert not shutdown.done()

        allow_claim.set()
        await asyncio.wait_for(shutdown, timeout=2)
    finally:
        allow_claim.set()
        if shutdown is None:
            await lifespan.__aexit__(None, None, None)
        elif not shutdown.done():
            await shutdown

    persisted = store.get(track.id)
    assert persisted is not None
    assert persisted.refresh_owner is None
    assert persisted.refresh_lease_until is None
    assert persisted.opening_batch_id is None
    assert persisted.last_error is None
    assert provider.calls == []
    assert app.state.portfolio_opening_scheduler.status.running is False
