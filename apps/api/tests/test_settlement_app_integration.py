from __future__ import annotations

import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import quantsieve_api.portfolio_paper as portfolio_paper_module
from fastapi import FastAPI
from fastapi.testclient import TestClient
from quantsieve_api.config import Settings
from quantsieve_api.main import create_app
from quantsieve_api.portfolio_paper import (
    PortfolioPaperConfig,
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
from quantsieve_api.portfolio_paper_settlement_scheduler import (
    PortfolioPaperSettlementScheduler,
    PortfolioPaperSettlementSchedulerStatus,
)
from quantsieve_engine import PortfolioForwardTarget
from quantsieve_providers import (
    BinanceSettlementDailyBarEvidence,
    BinanceSpotTradingRules,
    ExecutionQuote,
    Instrument,
)

INFORMATION_SESSION = datetime(2026, 7, 23, tzinfo=UTC)
EXECUTION_SESSION = INFORMATION_SESSION + timedelta(days=1)
INFORMATION_CLOSE = EXECUTION_SESSION - timedelta(milliseconds=1)
INFORMATION_FINALIZED = INFORMATION_CLOSE + timedelta(minutes=2)
INFORMATION_OBSERVED = INFORMATION_FINALIZED + timedelta(seconds=1)
RULES_VERIFIED_AT = INFORMATION_OBSERVED + timedelta(seconds=1)
BASKET_CREATED_AT = RULES_VERIFIED_AT + timedelta(seconds=1)
DECIDED_AT = BASKET_CREATED_AT + timedelta(seconds=1)
OPENING_NOW = EXECUTION_SESSION + timedelta(minutes=3)
SETTLEMENT_NOW = EXECUTION_SESSION + timedelta(days=1, minutes=3)
SYMBOLS = (("BTCUSDT", "BTC"), ("ETHUSDT", "ETH"))


class ExactSettlementHistoryProvider:
    """Offline typed finalization evidence for a real app lifecycle."""

    def __init__(self, *, failure_detail: str | None = None) -> None:
        self.failure_detail = failure_detail
        self.calls: list[dict[str, object]] = []

    async def settlement_daily_bar(
        self,
        symbol: str,
        *,
        session: date,
        deadline: float,
    ) -> BinanceSettlementDailyBarEvidence:
        self.calls.append(
            {
                "symbol": symbol,
                "session": session,
                "deadline": deadline,
            }
        )
        if self.failure_detail is not None:
            raise RuntimeError(self.failure_detail)
        close = {
            "BTCUSDT": "120000",
            "ETHUSDT": "4000",
        }[symbol]
        close_time = EXECUTION_SESSION + timedelta(days=1) - timedelta(
            milliseconds=1
        )
        return BinanceSettlementDailyBarEvidence(
            symbol=symbol,
            session=EXECUTION_SESSION.isoformat(),
            request_started_at=SETTLEMENT_NOW - timedelta(seconds=1),
            observed_at=SETTLEMENT_NOW,
            exchange_server_time=(
                SETTLEMENT_NOW - timedelta(milliseconds=100)
            ),
            clock_checked_at=SETTLEMENT_NOW - timedelta(milliseconds=100),
            open_time=EXECUTION_SESSION,
            close_time=close_time,
            finalized_at=close_time + timedelta(minutes=2),
            open=100.0,
            high=float(Decimal(close) + Decimal("100")),
            low=90.0,
            close=float(close),
            volume=1000.0,
            exact_open="100",
            exact_high=str(Decimal(close) + Decimal("100")),
            exact_low="90",
            exact_close=close,
            exact_volume="1000",
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


def _create_track(
    store: PortfolioPaperTrackStore,
    basket: PortfolioPaperBasketContract,
) -> PortfolioPaperTrackRecord:
    configuration = PortfolioPaperConfig(
        portfolio_experiment_id="app-settlement-integration",
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
    prices = {
        "BTCUSDT": (117_000.0, 119_000.0, 116_000.0, 118_000.0, 1_000.0),
        "ETHUSDT": (3_650.0, 3_750.0, 3_600.0, 3_700.0, 10_000.0),
    }
    bars = []
    for contract in basket.instruments:
        open_price, high, low, close, volume = prices[contract.symbol]
        bars.append(
            certify_binance_daily_bar(
                {
                    "symbol": contract.symbol,
                    "interval": "1d",
                    "open_time": INFORMATION_SESSION.isoformat(),
                    "close_time": INFORMATION_CLOSE.isoformat(),
                    "finalized_at": INFORMATION_FINALIZED.isoformat(),
                    "observed_at": INFORMATION_OBSERVED.isoformat(),
                    "exchange_server_time": INFORMATION_OBSERVED.isoformat(),
                    "clock_checked_at": INFORMATION_OBSERVED.isoformat(),
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
        information_session=INFORMATION_SESSION.isoformat(),
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


def _opening_quotes(
    basket: PortfolioPaperBasketContract,
) -> tuple[ExecutionQuote, ...]:
    quotes = []
    for contract in basket.instruments:
        ask = {
            "BTCUSDT": Decimal("118001"),
            "ETHUSDT": Decimal("3701"),
        }[contract.symbol]
        quotes.append(
            ExecutionQuote(
                symbol=contract.symbol,
                provider="binance",
                venue="Binance Spot",
                bid_price=ask - Decimal("1"),
                ask_price=ask,
                bid_quantity=Decimal("1000000"),
                ask_quantity=Decimal("1000000"),
                notional_reference_price=ask,
                notional_reference_kind="exchange_reference",
                notional_reference_window_minutes=None,
                notional_reference_at=OPENING_NOW,
                notional_reference_observed_at=OPENING_NOW,
                exchange_reference_available=True,
                exchange_reference_at=OPENING_NOW,
                exchange_reference_observed_at=OPENING_NOW,
                request_started_at=OPENING_NOW,
                observed_at=OPENING_NOW,
                exchange_server_time=OPENING_NOW,
                clock_checked_at=OPENING_NOW,
                cache_used=False,
            )
        )
    return tuple(quotes)


def _open_track(
    store: PortfolioPaperTrackStore,
) -> PortfolioPaperTrackRecord:
    basket = _eligible_basket()
    track = _create_track(store, basket)
    lease = store.claim_due(
        owner="settlement-app-opening",
        now=OPENING_NOW,
        lease_for=timedelta(seconds=10),
    )
    assert lease is not None
    store.commit_opening_if_current(
        track_id=track.id,
        owner=lease.owner,
        generation=lease.generation,
        expected_revision=lease.state_revision,
        basket=basket,
        quotes=_opening_quotes(basket),
    )
    opened = store.get(track.id)
    assert opened is not None
    assert opened.opening_batch_id is not None
    assert opened.state_revision == 0
    return opened


def _settings(database: Path, cache: Path, *, enabled: bool) -> Settings:
    return Settings(
        build_version="settlement-app-integration",
        environment="test",
        database_path=str(database),
        cache_path=str(cache),
        monitor_scheduler_enabled=False,
        paper_scheduler_enabled=False,
        portfolio_opening_scheduler_enabled=False,
        portfolio_settlement_scheduler_enabled=enabled,
        portfolio_settlement_poll_seconds=3600.0,
        portfolio_settlement_history_deadline_seconds=2.0,
        portfolio_settlement_lease_seconds=10.0,
    )


def _wait_for_outcome(
    scheduler: PortfolioPaperSettlementScheduler,
    expected: str,
    *,
    timeout_seconds: float = 3,
) -> PortfolioPaperSettlementSchedulerStatus:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = scheduler.status
        if (
            status.last_outcome == expected
            and (
                expected not in {"failed", "stale"}
                or status.consecutive_failures > 0
            )
        ):
            return status
        time.sleep(0.01)
    raise AssertionError(f"Settlement scheduler did not report {expected!r}.")


def _app_with_opened_track(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: ExactSettlementHistoryProvider,
    database_name: str,
) -> tuple[FastAPI, PortfolioPaperTrackRecord, Path]:
    database_clock = {"now": OPENING_NOW}
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: database_clock["now"],
    )
    database = tmp_path / database_name
    app = create_app(
        _settings(database, tmp_path / f"{database_name}.cache", enabled=True),
        portfolio_settlement_provider=provider,
    )
    opened = _open_track(app.state.portfolio_paper_track_store)
    database_clock["now"] = SETTLEMENT_NOW
    return app, opened, database


def test_app_lifespan_commits_one_exact_close_valuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ExactSettlementHistoryProvider()
    app, opened, database = _app_with_opened_track(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        provider=provider,
        database_name="settlement-success.db",
    )
    store = app.state.portfolio_paper_track_store
    scheduler = app.state.portfolio_settlement_scheduler

    assert app.state.portfolio_settlement_provider is provider
    assert scheduler.status.running is False
    with TestClient(app):
        status = _wait_for_outcome(scheduler, "committed")

        assert status.running is True
        assert status.consecutive_failures == 0
        assert status.last_track_id == opened.id
        assert {
            str(call["symbol"]) for call in provider.calls
        } == {"BTCUSDT", "ETHUSDT"}
        assert all(
            call["session"] == EXECUTION_SESSION.date()
            and isinstance(call["deadline"], float)
            for call in provider.calls
        )
        assert len({call["deadline"] for call in provider.calls}) == 1
        persisted = store.get(opened.id)
        assert persisted is not None
        assert persisted.state_revision == 1
        assert persisted.state.valuation_count == 1
        assert persisted.state.pending_target is None
        assert persisted.settlement_id is not None
        assert persisted.settlement_session == EXECUTION_SESSION.isoformat()
        assert persisted.last_error is None
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM portfolio_paper_close_settlements "
                "WHERE track_id = ?",
                (opened.id,),
            ).fetchone() == (1,)

    assert scheduler.status.running is False


def test_app_persists_and_reports_only_generic_history_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "private-host credential-token bar_hash=private"
    provider = ExactSettlementHistoryProvider(failure_detail=secret)
    app, opened, database = _app_with_opened_track(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        provider=provider,
        database_name="settlement-failure.db",
    )
    store = app.state.portfolio_paper_track_store
    scheduler = app.state.portfolio_settlement_scheduler

    with TestClient(app):
        status = _wait_for_outcome(scheduler, "failed")

        assert status.running is True
        assert status.consecutive_failures == 1
        assert status.last_track_id == opened.id
        assert status.last_error == (
            "Settlement valuation attempt reported a generic failure."
        )
        assert secret not in status.model_dump_json()
        persisted = store.get(opened.id)
        assert persisted is not None
        assert persisted.state_revision == 0
        assert persisted.settlement_id is None
        assert persisted.last_error is not None
        assert persisted.last_error.startswith(
            "Settlement finalized-bar collection failed"
        )
        assert secret not in persisted.last_error
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM portfolio_paper_close_settlements "
                "WHERE track_id = ?",
                (opened.id,),
            ).fetchone() == (0,)

    assert scheduler.status.running is False


def test_disabled_settlement_lifecycle_never_requests_history(
    tmp_path: Path,
) -> None:
    provider = ExactSettlementHistoryProvider(
        failure_detail="must never be reached"
    )
    app = create_app(
        _settings(
            tmp_path / "settlement-disabled.db",
            tmp_path / "settlement-disabled.cache",
            enabled=False,
        ),
        portfolio_settlement_provider=provider,
    )
    scheduler = app.state.portfolio_settlement_scheduler

    with TestClient(app) as api:
        assert api.get("/health").status_code == 200
        assert scheduler.status.enabled is False
        assert scheduler.status.running is False

    assert provider.calls == []
    assert scheduler.status.running is False
