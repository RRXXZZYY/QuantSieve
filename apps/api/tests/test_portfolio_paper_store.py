from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from threading import Barrier

import pytest
import quantsieve_api.portfolio_paper as portfolio_paper_module
from quantsieve_api.portfolio_paper import (
    PortfolioPaperConfig,
    PortfolioPaperConflictError,
    PortfolioPaperCorruptionError,
    PortfolioPaperLease,
    PortfolioPaperSchemaError,
    PortfolioPaperTrackRecord,
    PortfolioPaperTrackStore,
)
from quantsieve_api.portfolio_paper_contracts import (
    PortfolioPaperBasketContract,
    PortfolioPaperDecisionCertificate,
    assess_portfolio_paper_eligibility,
    canonical_payload_hash,
    certify_binance_daily_bar,
    certify_portfolio_paper_decision,
)
from quantsieve_engine import PortfolioForwardTarget
from quantsieve_providers import (
    BinanceSpotTradingRules,
    ExecutionQuote,
    Instrument,
)

RULES_VERIFIED_AT = datetime(2026, 7, 24, 23, 50, tzinfo=UTC)
DEFAULT_DATABASE_NOW = datetime(2026, 7, 25, 0, 3, tzinfo=UTC)
SETTLEMENT_MATURITY = datetime(2026, 7, 26, 0, 2, tzinfo=UTC)
SETTLEMENT_DATABASE_NOW = datetime(2026, 7, 26, 0, 3, tzinfo=UTC)
SYMBOL_ASSETS = (
    ("BTCUSDT", "BTC"),
    ("ETHUSDT", "ETH"),
    ("BNBUSDT", "BNB"),
    ("SOLUSDT", "SOL"),
    ("XRPUSDT", "XRP"),
    ("ADAUSDT", "ADA"),
)


@pytest.fixture(autouse=True)
def fixed_database_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: DEFAULT_DATABASE_NOW,
    )


def eligible_basket(
    *,
    size: int = 2,
    verified_at: datetime = RULES_VERIFIED_AT,
    lot_max_quantity: str = "1000000",
    market_max_quantity: str = "1000000",
    lot_step_size: str = "0.00001",
    market_step_size: str = "0.00001",
    lot_min_quantity: str = "0.00001",
    market_min_quantity: str = "0.00001",
    min_notional: str = "5",
    min_notional_applies_to_market: bool = True,
    max_notional: str | None = None,
    max_notional_applies_to_market: bool = False,
) -> PortfolioPaperBasketContract:
    contracts = []
    for symbol, base_asset in SYMBOL_ASSETS[:size]:
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
            lot_step_size=lot_step_size,
            lot_min_quantity=lot_min_quantity,
            lot_max_quantity=lot_max_quantity,
            market_step_size=market_step_size,
            market_min_quantity=market_min_quantity,
            market_max_quantity=market_max_quantity,
            min_notional=min_notional,
            min_notional_applies_to_market=min_notional_applies_to_market,
            max_notional=max_notional,
            max_notional_applies_to_market=max_notional_applies_to_market,
            notional_average_price_minutes=5,
            verified_at=verified_at,
        )
        assessment = assess_portfolio_paper_eligibility(
            instrument,
            rules=rules,
            now=verified_at + timedelta(minutes=1),
        )
        assert assessment.contract is not None
        contracts.append(assessment.contract)
    return PortfolioPaperBasketContract(
        instruments=tuple(contracts),
        created_at=verified_at + timedelta(minutes=2),
    )


def config(
    experiment_id: str = "portfolio-experiment-1",
) -> PortfolioPaperConfig:
    return PortfolioPaperConfig(
        portfolio_experiment_id=experiment_id,
        symbols=("BTCUSDT", "ETHUSDT"),
        basket_identity=eligible_basket().identity,
        method="periodic_equal",
        calculation_version="portfolio-forward-target-v1",
        initial_cash=100_000,
        fee_rate=0.0003,
        slippage_rate=0.0002,
        volatility_lookback=60,
        rebalance_bars=21,
        maximum_asset_weight=0.6,
    )


def target(
    session: str = "2026-07-24",
) -> PortfolioForwardTarget:
    return PortfolioForwardTarget(
        method="periodic_equal",
        information_session=session,
        weights={"BTCUSDT": 0.5, "ETHUSDT": 0.5},
    )


def decision(
    configuration: PortfolioPaperConfig,
    *,
    basket: PortfolioPaperBasketContract | None = None,
) -> PortfolioPaperDecisionCertificate:
    basket = basket or eligible_basket()
    assert basket.identity == configuration.basket_identity
    open_time = datetime(2026, 7, 24, tzinfo=UTC)
    close_time = datetime(2026, 7, 24, 23, 59, 59, 999000, tzinfo=UTC)
    observed_at = close_time + timedelta(minutes=2, seconds=1)
    exchange_time = observed_at - timedelta(milliseconds=500)
    close_prices = {
        "BTCUSDT": 118_000,
        "ETHUSDT": 3_700,
        "BNBUSDT": 620,
        "SOLUSDT": 190,
        "XRPUSDT": 3.1,
        "ADAUSDT": 0.82,
    }
    rows = {}
    for index, contract in enumerate(basket.instruments):
        price = close_prices[contract.symbol]
        rows[contract.symbol] = {
            "symbol": contract.symbol,
            "interval": "1d",
            "open_time": open_time.isoformat(),
            "close_time": close_time.isoformat(),
            "observed_at": (
                observed_at + timedelta(milliseconds=index)
            ).isoformat(),
            "exchange_server_time": exchange_time.isoformat(),
            "clock_checked_at": exchange_time.isoformat(),
            "exchange_clock_verified": True,
            "finalized_at": (close_time + timedelta(minutes=2)).isoformat(),
            "open": price * 0.99,
            "high": price * 1.01,
            "low": price * 0.98,
            "close": price,
            "volume": 10_000,
            "exact_open": str(price * 0.99),
            "exact_high": str(price * 1.01),
            "exact_low": str(price * 0.98),
            "exact_close": str(price),
            "exact_volume": "10000",
            "finalized": True,
        }
    bars = tuple(
        certify_binance_daily_bar(rows[contract.symbol], contract)
        for contract in basket.instruments
    )
    weight = 1 / len(configuration.symbols)
    configured_target = PortfolioForwardTarget(
        method=configuration.method,
        information_session=open_time,
        weights={symbol: weight for symbol in configuration.symbols},
    )
    return certify_portfolio_paper_decision(
        target=configured_target,
        bars=bars,
        basket=basket,
        configuration_hash=canonical_payload_hash(
            configuration.model_dump(mode="json")
        ),
        volatility_lookback=configuration.volatility_lookback,
        maximum_asset_weight=configuration.maximum_asset_weight,
        decided_at=observed_at + timedelta(seconds=1),
    )


def create_track(
    store: PortfolioPaperTrackStore,
    *,
    experiment_id: str = "portfolio-experiment-1",
) -> PortfolioPaperTrackRecord:
    configuration = config(experiment_id)
    return store.create(
        configuration,
        decision(configuration),
    )


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def opening_quotes(
    *,
    size: int = 2,
    accepted_at: datetime = DEFAULT_DATABASE_NOW,
    btc_ask: str = "118001",
    eth_ask: str = "3701",
) -> tuple[ExecutionQuote, ...]:
    asks = {
        "BTCUSDT": Decimal(btc_ask),
        "ETHUSDT": Decimal(eth_ask),
        "BNBUSDT": Decimal("621"),
        "SOLUSDT": Decimal("191"),
        "XRPUSDT": Decimal("3.11"),
        "ADAUSDT": Decimal("0.83"),
    }
    return tuple(
        ExecutionQuote(
            symbol=symbol,
            provider="binance",
            venue="Binance Spot",
            bid_price=bid,
            ask_price=ask,
            bid_quantity="1000000",
            ask_quantity="1000000",
            request_started_at=accepted_at,
            observed_at=accepted_at,
            notional_reference_price=ask,
            notional_reference_kind="exchange_reference",
            notional_reference_window_minutes=None,
            notional_reference_at=accepted_at,
            notional_reference_observed_at=accepted_at,
            exchange_reference_available=True,
            exchange_reference_at=accepted_at,
            exchange_reference_observed_at=accepted_at,
            exchange_server_time=accepted_at,
            clock_checked_at=accepted_at,
            cache_used=False,
        )
        for symbol, ask in (
            (symbol, asks[symbol])
            for symbol, _base_asset in SYMBOL_ASSETS[:size]
        )
        for bid in (ask - Decimal("0.01"),)
    )


def close_bars(
    *,
    basket: PortfolioPaperBasketContract | None = None,
    btc_close: float = 120_000,
    eth_close: float = 3_800,
) -> tuple[object, ...]:
    execution_basket = basket or eligible_basket()
    open_time = datetime(2026, 7, 25, tzinfo=UTC)
    close_time = open_time + timedelta(days=1) - timedelta(milliseconds=1)
    finalized_at = close_time + timedelta(minutes=2)
    prices = {
        "BTCUSDT": btc_close,
        "ETHUSDT": eth_close,
        "BNBUSDT": 650,
        "SOLUSDT": 200,
        "XRPUSDT": 3.2,
        "ADAUSDT": 0.85,
    }
    bars = []
    for index, contract in enumerate(execution_basket.instruments):
        price = prices[contract.symbol]
        observed_at = finalized_at + timedelta(seconds=1, milliseconds=index)
        exchange_time = observed_at - timedelta(milliseconds=100)
        bars.append(
            certify_binance_daily_bar(
                {
                    "symbol": contract.symbol,
                    "interval": "1d",
                    "open_time": open_time.isoformat(),
                    "close_time": close_time.isoformat(),
                    "finalized_at": finalized_at.isoformat(),
                    "observed_at": observed_at.isoformat(),
                    "exchange_server_time": exchange_time.isoformat(),
                    "clock_checked_at": exchange_time.isoformat(),
                    "exchange_clock_verified": True,
                    "open": price * 0.99,
                    "high": price * 1.01,
                    "low": price * 0.98,
                    "close": price,
                    "volume": 10_000,
                    "exact_open": str(price * 0.99),
                    "exact_high": str(price * 1.01),
                    "exact_low": str(price * 0.98),
                    "exact_close": str(price),
                    "exact_volume": "10000",
                    "finalized": True,
                },
                contract,
            )
        )
    return tuple(bars)


def arm_settlement_lease(
    database: Path,
    track_id: str,
    *,
    owner: str = "settlement-worker",
    now: datetime = SETTLEMENT_DATABASE_NOW,
) -> PortfolioPaperLease:
    updated_at = now - timedelta(seconds=1)
    expires_at = now + timedelta(minutes=1)
    with sqlite3.connect(database) as connection:
        cursor = connection.execute(
            """
            UPDATE portfolio_paper_tracks
            SET refresh_generation = refresh_generation + 1,
                refresh_owner = ?,
                refresh_lease_until = ?,
                updated_at = ?
            WHERE id = ? AND state_revision = 0
            """,
            (
                owner,
                expires_at.isoformat(),
                updated_at.isoformat(),
                track_id,
            ),
        )
        assert cursor.rowcount == 1
        generation, revision = connection.execute(
            "SELECT refresh_generation, state_revision "
            "FROM portfolio_paper_tracks WHERE id = ?",
            (track_id,),
        ).fetchone()
    return PortfolioPaperLease(
        track_id=track_id,
        owner=owner,
        generation=generation,
        state_revision=revision,
        expires_at=expires_at,
    )


def prepare_opened_track(
    database: Path,
) -> tuple[PortfolioPaperTrackStore, PortfolioPaperTrackRecord, object]:
    store = PortfolioPaperTrackStore(database)
    created = create_track(store)
    opening_lease = store.claim_due(
        owner="opening-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    )
    assert opening_lease is not None
    opening = store.commit_opening_if_current(
        track_id=created.id,
        owner=opening_lease.owner,
        generation=opening_lease.generation,
        expected_revision=0,
        basket=eligible_basket(),
        quotes=opening_quotes(),
    )
    return store, created, opening


def test_schema_and_activation_are_atomic_canonical_and_full_cash(
    tmp_path: Path,
) -> None:
    database = tmp_path / "portfolio-paper.db"
    store = PortfolioPaperTrackStore(database)
    record = create_track(store)

    assert record.tracking_mode == "execution_paper"
    assert record.status == "internal_only"
    assert record.state_revision == 0
    assert record.state.valuation_count == 0
    assert record.state.rebalance_count == 0
    assert record.state.cash == 100_000
    assert record.state.equity == 100_000
    assert record.state.total_return == 0
    assert record.state.max_drawdown == 0
    assert record.state.total_cost == 0
    assert record.state.shares == {"BTCUSDT": 0, "ETHUSDT": 0}
    assert record.state.pending_target == target()
    assert record.pending_decision is not None
    assert record.pending_decision.target == record.state.pending_target
    assert record.pending_decision.receipt.track_id == record.id
    assert (
        record.pending_decision.receipt.certificate
        == record.pending_decision.certificate
    )

    with sqlite3.connect(database) as connection:
        migration = connection.execute(
            "SELECT component, version FROM app_schema_migrations"
        ).fetchall()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        track_row = connection.execute(
            "SELECT config_payload, configuration_hash, "
            "state_payload, state_hash FROM portfolio_paper_tracks"
        ).fetchone()
        decision_row = connection.execute(
            "SELECT target_payload, target_hash, "
            "market_input_payload, market_input_hash "
            "FROM portfolio_paper_decisions"
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_advances"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_executions"
        ).fetchone()[0] == 0

    assert migration == [
        ("portfolio_paper", 1),
        ("portfolio_paper", 2),
        ("portfolio_paper", 3),
    ]
    assert {
        "portfolio_paper_tracks",
        "portfolio_paper_decisions",
        "portfolio_paper_advances",
        "portfolio_paper_executions",
        "portfolio_paper_execution_batches",
        "portfolio_paper_modeled_fills",
        "portfolio_paper_close_settlements",
    }.issubset(tables)
    assert track_row is not None
    assert decision_row is not None
    for payload, payload_hash in (
        (track_row[0], track_row[1]),
        (track_row[2], track_row[3]),
        (decision_row[0], decision_row[1]),
        (decision_row[2], decision_row[3]),
    ):
        assert canonical_json(json.loads(payload)) == payload
        assert digest(payload) == payload_hash
    with (
        sqlite3.connect(database) as connection,
        pytest.raises(sqlite3.IntegrityError, match="cannot store REAL"),
    ):
        connection.execute(
            "UPDATE portfolio_paper_tracks SET state_revision = 0.5 WHERE id = ?",
            (record.id,),
        )


def test_non_utc_aware_inputs_normalize_before_atomic_commit(
    tmp_path: Path,
) -> None:
    configuration = config()
    certificate = decision(configuration)
    payload = certificate.model_dump(mode="python")
    china_time = timezone(timedelta(hours=8))
    payload["decided_at"] = certificate.decided_at.astimezone(china_time)
    payload["execution_deadline"] = certificate.execution_deadline.astimezone(
        china_time
    )
    for bar in payload["bars"]:
        for key in (
            "open_time",
            "close_time",
            "finalized_at",
            "observed_at",
            "exchange_server_time",
            "clock_checked_at",
        ):
            bar[key] = bar[key].astimezone(china_time)
    payload["basket"]["created_at"] = payload["basket"]["created_at"].astimezone(
        china_time
    )
    for instrument in payload["basket"]["instruments"]:
        instrument["created_at"] = instrument["created_at"].astimezone(china_time)
        instrument["rules"]["verified_at"] = instrument["rules"][
            "verified_at"
        ].astimezone(china_time)
    normalized = PortfolioPaperDecisionCertificate.model_validate(payload)

    assert normalized.decided_at.utcoffset() == timedelta(0)
    assert all(bar.observed_at.utcoffset() == timedelta(0) for bar in normalized.bars)
    database = tmp_path / "canonical-time.db"
    record = PortfolioPaperTrackStore(database).create(configuration, normalized)

    assert record.pending_decision is not None
    assert record.pending_decision.persisted_at.utcoffset() == timedelta(0)
    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT observed_at, decided_at, persisted_at "
            "FROM portfolio_paper_decisions"
        ).fetchone()
    assert stored is not None
    assert all(value.endswith("+00:00") for value in stored)


def test_reversed_basket_order_survives_canonical_json_and_restart(
    tmp_path: Path,
) -> None:
    source_basket = eligible_basket()
    reversed_basket = PortfolioPaperBasketContract(
        instruments=tuple(reversed(source_basket.instruments)),
        created_at=source_basket.created_at,
    )
    configuration = config("reversed-order").model_copy(
        update={
            "symbols": ("ETHUSDT", "BTCUSDT"),
            "basket_identity": reversed_basket.identity,
        }
    )
    database = tmp_path / "reversed-order.db"
    created = PortfolioPaperTrackStore(database).create(
        configuration,
        decision(configuration, basket=reversed_basket),
    )
    reopened = PortfolioPaperTrackStore(database).get(created.id)

    assert reopened is not None
    assert reopened.config.symbols == ("ETHUSDT", "BTCUSDT")
    assert reopened.pending_decision is not None
    assert set(reopened.pending_decision.target.weights) == {
        "ETHUSDT",
        "BTCUSDT",
    }


def test_track_identity_accepts_refreshed_rules_but_binds_calculation_parameters(
    tmp_path: Path,
) -> None:
    store = PortfolioPaperTrackStore(tmp_path / "stable-identity.db")
    configuration = config()
    refreshed_basket = eligible_basket(
        verified_at=RULES_VERIFIED_AT + timedelta(minutes=10),
    )
    assert refreshed_basket.identity == configuration.basket_identity
    refreshed_decision = decision(configuration, basket=refreshed_basket)

    record = store.create(configuration, refreshed_decision)

    assert record.pending_decision is not None
    assert (
        record.pending_decision.certificate.basket.instruments[0].rules.verified_at
    ) == RULES_VERIFIED_AT + timedelta(minutes=10)
    mismatched = refreshed_decision.model_copy(
        update={
            "volatility_lookback": 2,
            "maximum_asset_weight": 0.9,
            "decision_id": "f" * 32,
        }
    )
    other_config = config("mismatched-parameters")
    mismatched = mismatched.model_copy(
        update={
            "configuration_hash": canonical_payload_hash(
                other_config.model_dump(mode="json")
            )
        }
    )
    with pytest.raises(ValueError, match="calculation parameters"):
        store.create(other_config, mismatched)


def test_activation_rejects_cash_below_all_or_nothing_exchange_minimums(
    tmp_path: Path,
) -> None:
    store = PortfolioPaperTrackStore(tmp_path / "insufficient-cash.db")
    tiny_config = config("tiny-cash").model_copy(update={"initial_cash": 1.0})
    tiny_decision = decision(tiny_config)

    with pytest.raises(ValueError, match="cannot fund"):
        store.create(tiny_config, tiny_decision)


def test_quote_validation_authenticates_committed_pending_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = config()
    basket = eligible_basket()
    certificate = decision(configuration, basket=basket)
    persisted_at = certificate.decided_at + timedelta(milliseconds=250)
    accepted_at = certificate.decided_at + timedelta(seconds=3)
    database_times = iter((persisted_at, *([accepted_at] * 8)))
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: next(database_times),
    )
    store = PortfolioPaperTrackStore(tmp_path / "authenticated-quotes.db")
    record = store.create(configuration, certificate)
    quotes = tuple(
        ExecutionQuote(
            symbol=symbol,
            provider="binance",
            venue="Binance Spot",
            bid_price=bid,
            ask_price=ask,
            bid_quantity="1000000",
            ask_quantity="1000000",
            request_started_at=persisted_at + timedelta(milliseconds=100),
            observed_at=accepted_at - timedelta(seconds=1),
            notional_reference_price=ask,
            notional_reference_kind="exchange_reference",
            notional_reference_window_minutes=None,
            notional_reference_at=accepted_at - timedelta(milliseconds=900),
            notional_reference_observed_at=accepted_at
            - timedelta(milliseconds=750),
            exchange_reference_available=True,
            exchange_reference_at=accepted_at - timedelta(milliseconds=900),
            exchange_reference_observed_at=accepted_at
            - timedelta(milliseconds=750),
            exchange_server_time=accepted_at - timedelta(milliseconds=500),
            clock_checked_at=accepted_at - timedelta(milliseconds=500),
            cache_used=False,
        )
        for symbol, bid, ask in (
            ("BTCUSDT", 118_000.0, 118_001.0),
            ("ETHUSDT", 3_700.0, 3_701.0),
        )
    )

    with pytest.raises(PortfolioPaperConflictError, match="existing committed"):
        store.validate_execution_quotes(
            track_id="0" * 32,
            decision_id=certificate.decision_id,
            basket=basket,
            quotes=quotes,
        )
    with pytest.raises(PortfolioPaperConflictError, match="current pending"):
        store.validate_execution_quotes(
            track_id=record.id,
            decision_id="f" * 32,
            basket=basket,
            quotes=quotes,
        )
    impossible_quotes = (
        quotes[0].model_copy(
            update={
                "bid_price": Decimal("1000000000000"),
                "ask_price": Decimal("1000000000000"),
            }
        ),
        quotes[1],
    )
    with pytest.raises(ValueError, match=r"cannot satisfy|trading-rule snapshot"):
        store.validate_execution_quotes(
            track_id=record.id,
            decision_id=certificate.decision_id,
            basket=basket,
            quotes=impossible_quotes,
        )
    thin_top_of_book = (
        quotes[0].model_copy(update={"ask_quantity": Decimal("0.01")}),
        quotes[1],
    )
    with pytest.raises(ValueError, match=r"cannot satisfy|trading-rule snapshot"):
        store.validate_execution_quotes(
            track_id=record.id,
            decision_id=certificate.decision_id,
            basket=basket,
            quotes=thin_top_of_book,
        )
    with pytest.raises(ValueError, match=r"cannot satisfy|trading-rule snapshot"):
        store.validate_execution_quotes(
            track_id=record.id,
            decision_id=certificate.decision_id,
            basket=eligible_basket(market_max_quantity="0.1"),
            quotes=quotes,
        )
    with pytest.raises(ValueError, match=r"cannot satisfy|trading-rule snapshot"):
        store.validate_execution_quotes(
            track_id=record.id,
            decision_id=certificate.decision_id,
            basket=eligible_basket(lot_max_quantity="0.1"),
            quotes=quotes,
        )
    with pytest.raises(ValueError, match=r"cannot satisfy|trading-rule snapshot"):
        store.validate_execution_quotes(
            track_id=record.id,
            decision_id=certificate.decision_id,
            basket=eligible_basket(
                max_notional="1000",
                max_notional_applies_to_market=True,
            ),
            quotes=quotes,
        )
    ignored_market_minimum = eligible_basket(
        min_notional="1000000000",
        min_notional_applies_to_market=False,
    )
    ignored_market_maximum = eligible_basket(
        max_notional="1000",
        max_notional_applies_to_market=False,
    )
    with pytest.raises(ValueError, match="trading-rule snapshot"):
        store.validate_execution_quotes(
            track_id=record.id,
            decision_id=certificate.decision_id,
            basket=ignored_market_minimum,
            quotes=quotes,
        )
    with pytest.raises(ValueError, match="trading-rule snapshot"):
        store.validate_execution_quotes(
            track_id=record.id,
            decision_id=certificate.decision_id,
            basket=ignored_market_maximum,
            quotes=quotes,
        )
    accepted = store.validate_execution_quotes(
        track_id=record.id,
        decision_id=certificate.decision_id,
        basket=basket,
        quotes=quotes,
    )

    assert tuple(accepted) == ("BTCUSDT", "ETHUSDT")
    assert portfolio_paper_module._common_decimal_step(
        Decimal("0.002"),
        Decimal("0.003"),
    ) == Decimal("0.006")


def test_common_decimal_step_is_exact_beyond_decimal_context_precision() -> None:
    first = Decimal("12345678901234567890123456789")

    assert portfolio_paper_module._common_decimal_step(
        first,
        Decimal("1"),
    ) == first
    with pytest.raises(ValueError, match="finite positive"):
        portfolio_paper_module._common_decimal_step(Decimal("0"), Decimal("1"))


def test_opening_exact_depth_boundary_beyond_decimal_context_is_not_rejected(
    tmp_path: Path,
) -> None:
    basket = eligible_basket(
        lot_step_size="0.00000000000000000000000000001",
        market_step_size="0.00000000000000000000000000002",
        lot_min_quantity="0.00000000000000000000000000001",
        market_min_quantity="0.00000000000000000000000000001",
    )
    configuration = config("exact-depth").model_copy(
        update={"basket_identity": basket.identity}
    )
    store = PortfolioPaperTrackStore(tmp_path / "exact-depth.db")
    record = store.create(
        configuration,
        decision(configuration, basket=basket),
    )
    lease = store.claim_due(
        owner="exact-depth-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    )
    assert lease is not None
    exact_best_ask_quantity = Decimal(
        "0.42351346593463719322955618654"
    )
    base_quotes = opening_quotes()
    quotes = (
        base_quotes[0].model_copy(
            update={"ask_quantity": exact_best_ask_quantity}
        ),
        base_quotes[1],
    )

    assert record.pending_decision is not None
    accepted = store.validate_execution_quotes(
        track_id=record.id,
        decision_id=record.pending_decision.id,
        basket=basket,
        quotes=quotes,
    )
    assert accepted["BTCUSDT"].ask_quantity == exact_best_ask_quantity
    batch = store.commit_opening_if_current(
        track_id=record.id,
        owner=lease.owner,
        generation=lease.generation,
        expected_revision=lease.state_revision,
        basket=basket,
        quotes=quotes,
    )
    assert batch.fills[0].quantity == exact_best_ask_quantity


def test_opening_commit_is_atomic_auditable_idempotent_and_not_reclaimable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "opening-commit.db"
    store = PortfolioPaperTrackStore(database)
    record = create_track(store)
    lease = store.claim_due(
        owner="opening-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    )
    assert lease is not None

    batch = store.commit_opening_if_current(
        track_id=record.id,
        owner=lease.owner,
        generation=lease.generation,
        expected_revision=lease.state_revision,
        basket=eligible_basket(),
        quotes=opening_quotes(),
    )

    assert batch.command.track_id == record.id
    assert record.pending_decision is not None
    assert batch.command.decision_id == record.pending_decision.id
    assert batch.command.state_revision == 0
    assert batch.command.accepted_at == DEFAULT_DATABASE_NOW
    assert tuple(fill.symbol for fill in batch.fills) == record.config.symbols
    loaded = store.get(record.id)
    assert loaded is not None
    assert loaded.opening_batch_id == batch.command.idempotency_key
    assert loaded.opening_session == batch.command.execution_session
    assert loaded.opening_committed_at == DEFAULT_DATABASE_NOW
    assert loaded.state_revision == 0
    assert loaded.refresh_owner is None
    assert loaded.last_checked_at == DEFAULT_DATABASE_NOW
    assert store.claim_due(
        owner="must-not-open-twice",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    ) is None
    with pytest.raises(PortfolioPaperConflictError, match="committed opening"):
        store.delete(record.id)

    retry = store.commit_opening_if_current(
        track_id=record.id,
        owner="response-loss-retry",
        generation=999,
        expected_revision=0,
        basket=eligible_basket(),
        quotes=opening_quotes(btc_ask="118101", eth_ask="3711"),
    )
    assert retry == batch
    with pytest.raises(PortfolioPaperConflictError, match="different logical"):
        store.commit_opening_if_current(
            track_id=record.id,
            owner="different-command",
            generation=999,
            expected_revision=1,
            basket=eligible_basket(),
            quotes=opening_quotes(),
        )
    with pytest.raises(PortfolioPaperConflictError, match="different logical"):
        store.commit_opening_if_current(
            track_id=record.id,
            owner="different-command",
            generation=999,
            expected_revision=0,
            basket=eligible_basket(
                verified_at=RULES_VERIFIED_AT + timedelta(minutes=1)
            ),
            quotes=opening_quotes(),
        )
    with sqlite3.connect(database) as connection:
        batch_row = connection.execute(
            """
            SELECT id, expected_revision, fence_generation, quote_set_hash,
                   fill_set_hash, accepted_at, committed_at
            FROM portfolio_paper_execution_batches
            """
        ).fetchone()
        fill_count = connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_modeled_fills"
        ).fetchone()[0]
    assert batch_row == (
        batch.command.idempotency_key,
        0,
        lease.generation,
        batch.quote_set_hash,
        batch.fill_set_hash,
        DEFAULT_DATABASE_NOW.isoformat(),
        DEFAULT_DATABASE_NOW.isoformat(),
    )
    assert fill_count == len(batch.fills)
    restarted = PortfolioPaperTrackStore(database).get(record.id)
    assert restarted is not None
    assert restarted.opening_batch_id == batch.command.idempotency_key


@pytest.mark.parametrize(
    "tamper",
    [
        "noncanonical_batch",
        "batch_hash",
        "noncanonical_fill",
        "missing_fill",
        "audit_hash",
        "fence_generation",
        "accepted_at",
        "committed_at",
    ],
)
def test_opening_payload_hash_and_complete_fill_set_fail_closed_on_restart(
    tmp_path: Path,
    tamper: str,
) -> None:
    database = tmp_path / f"opening-corruption-{tamper}.db"
    store = PortfolioPaperTrackStore(database)
    record = create_track(store)
    lease = store.claim_due(
        owner="corruption-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    )
    assert lease is not None
    store.commit_opening_if_current(
        track_id=record.id,
        owner=lease.owner,
        generation=lease.generation,
        expected_revision=lease.state_revision,
        basket=eligible_basket(),
        quotes=opening_quotes(),
    )
    with sqlite3.connect(database) as connection:
        if tamper == "noncanonical_batch":
            connection.execute(
                "UPDATE portfolio_paper_execution_batches "
                "SET batch_payload = batch_payload || ' '"
            )
        elif tamper == "batch_hash":
            connection.execute(
                "UPDATE portfolio_paper_execution_batches "
                "SET batch_hash = ?",
                ("0" * 64,),
            )
        elif tamper == "noncanonical_fill":
            connection.execute(
                "UPDATE portfolio_paper_modeled_fills "
                "SET fill_payload = fill_payload || ' ' "
                "WHERE symbol = 'BTCUSDT'"
            )
        elif tamper == "missing_fill":
            connection.execute(
                "DELETE FROM portfolio_paper_modeled_fills "
                "WHERE symbol = 'BTCUSDT'"
            )
        elif tamper == "audit_hash":
            connection.execute(
                "UPDATE portfolio_paper_execution_batches "
                "SET quote_set_hash = ?",
                ("f" * 64,),
            )
        elif tamper == "fence_generation":
            connection.execute(
                "UPDATE portfolio_paper_execution_batches "
                "SET fence_generation = fence_generation + 1"
            )
        elif tamper == "accepted_at":
            connection.execute(
                "UPDATE portfolio_paper_execution_batches "
                "SET accepted_at = '2026-07-25T00:02:59+00:00'"
            )
        else:
            connection.execute(
                "UPDATE portfolio_paper_execution_batches "
                "SET committed_at = '2026-07-25T00:03:01+00:00'"
            )

    with pytest.raises(PortfolioPaperCorruptionError):
        store.get(record.id)
    with pytest.raises(PortfolioPaperCorruptionError):
        PortfolioPaperTrackStore(database)


@pytest.mark.parametrize(
    ("failure_stage", "failure_index"),
    [("after_batch", None), ("after_fill", 1)],
)
def test_opening_fault_injection_rolls_back_every_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    failure_index: int | None,
) -> None:
    database = tmp_path / f"rollback-{failure_stage}.db"
    store = PortfolioPaperTrackStore(database)
    record = create_track(store)
    lease = store.claim_due(
        owner="rollback-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    )
    assert lease is not None

    def fail_at_checkpoint(stage: str, fill_index: int | None) -> None:
        if stage == failure_stage and fill_index == failure_index:
            raise RuntimeError("injected opening crash")

    monkeypatch.setattr(
        portfolio_paper_module,
        "_opening_commit_checkpoint",
        fail_at_checkpoint,
    )
    with pytest.raises(RuntimeError, match="injected"):
        store.commit_opening_if_current(
            track_id=record.id,
            owner=lease.owner,
            generation=lease.generation,
            expected_revision=lease.state_revision,
            basket=eligible_basket(),
            quotes=opening_quotes(),
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_execution_batches"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_modeled_fills"
        ).fetchone()[0] == 0
    rolled_back = store.get(record.id)
    assert rolled_back is not None
    assert rolled_back.opening_batch_id is None
    assert rolled_back.refresh_owner == lease.owner
    assert rolled_back.refresh_generation == lease.generation


def test_two_stores_racing_opening_return_one_exact_batch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "opening-race.db"
    first = PortfolioPaperTrackStore(database)
    second = PortfolioPaperTrackStore(database)
    record = create_track(first)
    lease = first.claim_due(
        owner="race-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    )
    assert lease is not None
    barrier = Barrier(2)

    def commit(store: PortfolioPaperTrackStore) -> object:
        barrier.wait()
        return store.commit_opening_if_current(
            track_id=record.id,
            owner=lease.owner,
            generation=lease.generation,
            expected_revision=lease.state_revision,
            basket=eligible_basket(),
            quotes=opening_quotes(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        batches = list(pool.map(commit, (first, second)))

    assert batches[0] == batches[1]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_execution_batches"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_modeled_fills"
        ).fetchone()[0] == 2


def test_settlement_claim_uses_database_maturity_and_never_cross_claims_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "settlement-claim-maturity.db"
    store, opened, _opening = prepare_opened_track(database)
    assert store.claim_due(
        owner="must-not-reopen",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    ) is None
    unopened = create_track(store, experiment_id="unopened-settlement-candidate")
    clock = {"now": SETTLEMENT_MATURITY - timedelta(milliseconds=1)}
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: clock["now"],
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        store.claim_due_settlement(
            owner="naive-caller",
            now=datetime(2030, 1, 1),
            lease_for=timedelta(minutes=1),
        )
    assert store.claim_due_settlement(
        owner="forged-future-caller",
        now=datetime(2030, 1, 1, tzinfo=UTC),
        lease_for=timedelta(minutes=2),
    ) is None

    clock["now"] = SETTLEMENT_MATURITY
    lease = store.claim_due_settlement(
        owner="mature-settlement-worker",
        now=datetime(2020, 1, 1, tzinfo=UTC),
        lease_for=timedelta(minutes=2),
    )
    assert lease is not None
    assert lease.track_id == opened.id
    assert lease.state_revision == 0
    assert lease.expires_at == SETTLEMENT_MATURITY + timedelta(minutes=2)
    claimed = store.get(opened.id)
    assert claimed is not None
    assert claimed.refresh_owner == lease.owner
    untouched = store.get(unopened.id)
    assert untouched is not None
    assert untouched.refresh_owner is None

    clock["now"] = SETTLEMENT_MATURITY + timedelta(seconds=2)
    settlement = store.commit_close_settlement_if_current(
        track_id=opened.id,
        owner=lease.owner,
        generation=lease.generation,
        expected_revision=0,
        bars=close_bars(),
    )
    assert settlement.command.track_id == opened.id
    assert store.claim_due_settlement(
        owner="must-not-resettle",
        now=datetime(2030, 1, 1, tzinfo=UTC),
        lease_for=timedelta(minutes=1),
    ) is None


def test_settlement_claim_is_single_writer_restart_safe_and_fences_takeover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "settlement-claim-race.db"
    first, opened, _opening = prepare_opened_track(database)
    second = PortfolioPaperTrackStore(database)
    clock = {"now": SETTLEMENT_DATABASE_NOW}
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: clock["now"],
    )
    barrier = Barrier(2)

    def claim(store: PortfolioPaperTrackStore) -> PortfolioPaperLease | None:
        barrier.wait()
        return store.claim_due_settlement(
            owner="first-settlement-worker",
            now=datetime(2040, 1, 1, tzinfo=UTC),
            lease_for=timedelta(seconds=10),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, (first, second)))
    leases = [lease for lease in claims if lease is not None]
    assert len(leases) == 1
    first_lease = leases[0]
    assert first_lease.track_id == opened.id

    restarted = PortfolioPaperTrackStore(database)
    restored = restarted.get(opened.id)
    assert restored is not None
    assert restored.refresh_owner == first_lease.owner
    assert restored.refresh_generation == first_lease.generation
    clock["now"] = first_lease.expires_at - timedelta(milliseconds=1)
    assert restarted.claim_due_settlement(
        owner="too-early-takeover",
        now=datetime(2050, 1, 1, tzinfo=UTC),
        lease_for=timedelta(minutes=1),
    ) is None

    clock["now"] = first_lease.expires_at
    second_lease = restarted.claim_due_settlement(
        owner="replacement-settlement-worker",
        now=datetime(2020, 1, 1, tzinfo=UTC),
        lease_for=timedelta(minutes=1),
    )
    assert second_lease is not None
    assert second_lease.generation == first_lease.generation + 1
    assert restarted.release_claim(
        track_id=opened.id,
        owner=first_lease.owner,
        generation=first_lease.generation,
        now=clock["now"],
    ) is False
    assert restarted.set_error_if_current(
        track_id=opened.id,
        owner=first_lease.owner,
        generation=first_lease.generation,
        expected_revision=0,
        message="stale settlement worker",
        checked_at=clock["now"],
    ) is False
    with pytest.raises(PortfolioPaperConflictError, match="fenced lease"):
        restarted.commit_close_settlement_if_current(
            track_id=opened.id,
            owner=first_lease.owner,
            generation=first_lease.generation,
            expected_revision=0,
            bars=close_bars(),
        )
    settlement = restarted.commit_close_settlement_if_current(
        track_id=opened.id,
        owner=second_lease.owner,
        generation=second_lease.generation,
        expected_revision=0,
        bars=close_bars(),
    )
    assert settlement.command.track_id == opened.id


def test_settlement_claim_fails_closed_before_touching_a_corrupt_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "settlement-claim-corrupt.db"
    store, _opened, _opening = prepare_opened_track(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE portfolio_paper_execution_batches SET batch_hash = ?",
            ("0" * 64,),
        )
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: SETTLEMENT_DATABASE_NOW,
    )
    with pytest.raises(PortfolioPaperCorruptionError):
        store.claim_due_settlement(
            owner="must-not-claim-corruption",
            now=datetime(2050, 1, 1, tzinfo=UTC),
            lease_for=timedelta(minutes=1),
        )


def test_close_settlement_is_atomic_exact_idempotent_and_advances_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "close-settlement.db"
    store = PortfolioPaperTrackStore(database)
    created = create_track(store)
    opening_lease = store.claim_due(
        owner="opening-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    )
    assert opening_lease is not None
    opening = store.commit_opening_if_current(
        track_id=created.id,
        owner=opening_lease.owner,
        generation=opening_lease.generation,
        expected_revision=0,
        basket=eligible_basket(),
        quotes=opening_quotes(),
    )
    settlement_lease = arm_settlement_lease(database, created.id)
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: SETTLEMENT_DATABASE_NOW,
    )

    settlement = store.commit_close_settlement_if_current(
        track_id=created.id,
        owner=settlement_lease.owner,
        generation=settlement_lease.generation,
        expected_revision=0,
        bars=close_bars(),
    )

    assert settlement.command.opening_batch_id == opening.command.idempotency_key
    assert settlement.command.opening_batch_hash == opening.batch_hash
    assert settlement.command.source_state_revision == 0
    assert settlement.command.target_state_revision == 1
    assert settlement.account.cash == opening.ending_cash
    assert tuple(
        position.quantity for position in settlement.account.positions
    ) == tuple(fill.quantity for fill in opening.fills)
    assert settlement.account.close_trade_count == 0
    assert settlement.account.close_fee == 0
    assert settlement.account.close_slippage == 0
    loaded = store.get(created.id)
    assert loaded is not None
    assert loaded.pending_decision == created.pending_decision
    assert loaded.state_revision == 1
    assert loaded.state.pending_target is None
    assert loaded.state == settlement.forward_state
    assert loaded.settlement_id == settlement.command.idempotency_key
    assert loaded.settlement_session == settlement.command.execution_session
    assert loaded.settlement_committed_at == SETTLEMENT_DATABASE_NOW
    assert loaded.refresh_owner is None
    assert loaded.refresh_lease_until is None
    with pytest.raises(PortfolioPaperConflictError, match="close settlement"):
        store.delete(created.id)

    retry = store.commit_close_settlement_if_current(
        track_id=created.id,
        owner="response-loss-retry",
        generation=999,
        expected_revision=0,
        bars=close_bars(),
    )
    assert retry == settlement
    with pytest.raises(PortfolioPaperConflictError, match="different evidence"):
        store.commit_close_settlement_if_current(
            track_id=created.id,
            owner="different-bars",
            generation=999,
            expected_revision=0,
            bars=close_bars(btc_close=120_001),
        )
    with sqlite3.connect(database) as connection:
        settlement_row = connection.execute(
            """
            SELECT id, opening_batch_id, source_revision, target_revision,
                   fence_generation, accepted_at, settled_at, committed_at
            FROM portfolio_paper_close_settlements
            """
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_advances"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_executions"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert settlement_row == (
        settlement.command.idempotency_key,
        opening.command.idempotency_key,
        0,
        1,
        settlement_lease.generation,
        SETTLEMENT_DATABASE_NOW.isoformat(),
        SETTLEMENT_DATABASE_NOW.isoformat(),
        SETTLEMENT_DATABASE_NOW.isoformat(),
    )
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="cannot store REAL"):
            connection.execute(
                "UPDATE portfolio_paper_close_settlements "
                "SET source_revision = 0.5"
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                "UPDATE portfolio_paper_close_settlements "
                "SET target_revision = 2"
            )
        connection.rollback()
    restarted = PortfolioPaperTrackStore(database).get(created.id)
    assert restarted == loaded


def test_six_asset_close_settlement_persists_every_opening_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "six-asset-settlement.db"
    basket = eligible_basket(size=6)
    symbols = tuple(contract.symbol for contract in basket.instruments)
    configuration = PortfolioPaperConfig(
        portfolio_experiment_id="six-asset-settlement",
        symbols=symbols,
        basket_identity=basket.identity,
        method="periodic_equal",
        initial_cash=600_000,
        fee_rate=0.0003,
        slippage_rate=0.0002,
        volatility_lookback=60,
        rebalance_bars=21,
        maximum_asset_weight=0.6,
    )
    store = PortfolioPaperTrackStore(database)
    created = store.create(
        configuration,
        decision(configuration, basket=basket),
    )
    opening_lease = store.claim_due(
        owner="six-opening-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    )
    assert opening_lease is not None
    opening = store.commit_opening_if_current(
        track_id=created.id,
        owner=opening_lease.owner,
        generation=opening_lease.generation,
        expected_revision=0,
        basket=basket,
        quotes=opening_quotes(size=6),
    )
    lease = arm_settlement_lease(database, created.id)
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: SETTLEMENT_DATABASE_NOW,
    )
    settlement = store.commit_close_settlement_if_current(
        track_id=created.id,
        owner=lease.owner,
        generation=lease.generation,
        expected_revision=0,
        bars=close_bars(basket=basket),
    )
    assert len(opening.fills) == 6
    assert len(settlement.account.positions) == 6
    assert tuple(position.symbol for position in settlement.account.positions) == symbols
    assert tuple(position.quantity for position in settlement.account.positions) == tuple(
        fill.quantity for fill in opening.fills
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_modeled_fills"
        ).fetchone()[0] == 6
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_close_settlements"
        ).fetchone()[0] == 1


def test_close_settlement_retains_29_digit_opening_quantity_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "settlement-29-digit.db"
    basket = eligible_basket(
        lot_step_size="0.00000000000000000000000000001",
        market_step_size="0.00000000000000000000000000002",
        lot_min_quantity="0.00000000000000000000000000001",
        market_min_quantity="0.00000000000000000000000000001",
    )
    configuration = config("settlement-29-digit").model_copy(
        update={"basket_identity": basket.identity}
    )
    store = PortfolioPaperTrackStore(database)
    created = store.create(
        configuration,
        decision(configuration, basket=basket),
    )
    opening_lease = store.claim_due(
        owner="exact-opening-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    )
    assert opening_lease is not None
    exact_quantity = Decimal("0.42351346593463719322955618654")
    base_quotes = opening_quotes()
    quotes = (
        base_quotes[0].model_copy(update={"ask_quantity": exact_quantity}),
        base_quotes[1],
    )
    opening = store.commit_opening_if_current(
        track_id=created.id,
        owner=opening_lease.owner,
        generation=opening_lease.generation,
        expected_revision=0,
        basket=basket,
        quotes=quotes,
    )
    assert opening.fills[0].quantity == exact_quantity
    lease = arm_settlement_lease(database, created.id)
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: SETTLEMENT_DATABASE_NOW,
    )
    settlement = store.commit_close_settlement_if_current(
        track_id=created.id,
        owner=lease.owner,
        generation=lease.generation,
        expected_revision=0,
        bars=close_bars(basket=basket),
    )
    assert settlement.account.positions[0].quantity == exact_quantity
    assert (
        Fraction(settlement.account.positions[0].market_value)
        == Fraction(exact_quantity)
        * Fraction(settlement.account.positions[0].certified_close_price_decimal)
    )
    restarted = PortfolioPaperTrackStore(database).get(created.id)
    assert restarted is not None
    assert restarted.state == settlement.forward_state


@pytest.mark.parametrize("attack", ["generation", "owner", "lease", "revision"])
def test_close_settlement_rejects_stale_fence_lease_and_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    database = tmp_path / f"settlement-stale-{attack}.db"
    store = PortfolioPaperTrackStore(database)
    created = create_track(store)
    opening_lease = store.claim_due(
        owner="opening-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    )
    assert opening_lease is not None
    store.commit_opening_if_current(
        track_id=created.id,
        owner=opening_lease.owner,
        generation=opening_lease.generation,
        expected_revision=0,
        basket=eligible_basket(),
        quotes=opening_quotes(),
    )
    lease = arm_settlement_lease(database, created.id)
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: SETTLEMENT_DATABASE_NOW,
    )
    owner = lease.owner if attack != "owner" else "stale-owner"
    generation = lease.generation if attack != "generation" else lease.generation - 1
    if attack == "lease":
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE portfolio_paper_tracks SET refresh_lease_until = ? "
                "WHERE id = ?",
                (
                    (SETTLEMENT_DATABASE_NOW - timedelta(milliseconds=1)).isoformat(),
                    created.id,
                ),
            )
    if attack == "revision":
        with pytest.raises(ValueError, match="revision zero"):
            store.commit_close_settlement_if_current(
                track_id=created.id,
                owner=owner,
                generation=generation,
                expected_revision=1,
                bars=close_bars(),
            )
        return
    with pytest.raises(PortfolioPaperConflictError, match="fenced lease"):
        store.commit_close_settlement_if_current(
            track_id=created.id,
            owner=owner,
            generation=generation,
            expected_revision=0,
            bars=close_bars(),
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_close_settlements"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("failure_stage", ["after_settlement", "after_state"])
def test_close_settlement_checkpoint_failures_roll_back_everything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    database = tmp_path / f"settlement-rollback-{failure_stage}.db"
    store = PortfolioPaperTrackStore(database)
    created = create_track(store)
    opening_lease = store.claim_due(
        owner="opening-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    )
    assert opening_lease is not None
    opening = store.commit_opening_if_current(
        track_id=created.id,
        owner=opening_lease.owner,
        generation=opening_lease.generation,
        expected_revision=0,
        basket=eligible_basket(),
        quotes=opening_quotes(),
    )
    lease = arm_settlement_lease(database, created.id)
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: SETTLEMENT_DATABASE_NOW,
    )

    def fail(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError("injected settlement crash")

    monkeypatch.setattr(
        portfolio_paper_module,
        "_settlement_commit_checkpoint",
        fail,
    )
    with pytest.raises(RuntimeError, match="injected settlement"):
        store.commit_close_settlement_if_current(
            track_id=created.id,
            owner=lease.owner,
            generation=lease.generation,
            expected_revision=0,
            bars=close_bars(),
        )
    rolled_back = store.get(created.id)
    assert rolled_back is not None
    assert rolled_back.state_revision == 0
    assert rolled_back.state.pending_target is not None
    assert rolled_back.opening_batch_id == opening.command.idempotency_key
    assert rolled_back.settlement_id is None
    assert rolled_back.refresh_owner == lease.owner
    assert rolled_back.refresh_generation == lease.generation
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_close_settlements"
        ).fetchone()[0] == 0


def test_two_stores_racing_close_settlement_return_one_logical_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "settlement-race.db"
    first = PortfolioPaperTrackStore(database)
    second = PortfolioPaperTrackStore(database)
    created = create_track(first)
    opening_lease = first.claim_due(
        owner="opening-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    )
    assert opening_lease is not None
    first.commit_opening_if_current(
        track_id=created.id,
        owner=opening_lease.owner,
        generation=opening_lease.generation,
        expected_revision=0,
        basket=eligible_basket(),
        quotes=opening_quotes(),
    )
    lease = arm_settlement_lease(database, created.id)
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: SETTLEMENT_DATABASE_NOW,
    )
    barrier = Barrier(2)

    def settle(store: PortfolioPaperTrackStore) -> object:
        barrier.wait()
        return store.commit_close_settlement_if_current(
            track_id=created.id,
            owner=lease.owner,
            generation=lease.generation,
            expected_revision=0,
            bars=close_bars(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(settle, (first, second)))
    assert results[0] == results[1]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_close_settlements"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "tamper",
    [
        "noncanonical_settlement",
        "command_hash",
        "bar_set_hash",
        "account_quantity",
        "forward_state_hash",
        "settlement_hash",
        "decision_identity",
        "opening_identity",
        "opening_batch_hash",
        "source_state",
        "settled_at",
        "current_state",
    ],
)
def test_close_settlement_payload_column_and_hash_tampering_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    database = tmp_path / f"settlement-corruption-{tamper}.db"
    store, created, _opening = prepare_opened_track(database)
    lease = arm_settlement_lease(database, created.id)
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: SETTLEMENT_DATABASE_NOW,
    )
    store.commit_close_settlement_if_current(
        track_id=created.id,
        owner=lease.owner,
        generation=lease.generation,
        expected_revision=0,
        bars=close_bars(),
    )
    with sqlite3.connect(database) as connection:
        if tamper == "noncanonical_settlement":
            connection.execute(
                "UPDATE portfolio_paper_close_settlements "
                "SET settlement_payload = settlement_payload || ' '"
            )
        elif tamper == "command_hash":
            connection.execute(
                "UPDATE portfolio_paper_close_settlements SET command_hash = ?",
                ("0" * 64,),
            )
        elif tamper == "bar_set_hash":
            connection.execute(
                "UPDATE portfolio_paper_close_settlements SET bar_set_hash = ?",
                ("0" * 64,),
            )
        elif tamper == "account_quantity":
            payload = json.loads(
                connection.execute(
                    "SELECT account_payload "
                    "FROM portfolio_paper_close_settlements"
                ).fetchone()[0]
            )
            payload["positions"][0]["quantity"] = "0.00001"
            connection.execute(
                "UPDATE portfolio_paper_close_settlements "
                "SET account_payload = ?",
                (canonical_json(payload),),
            )
        elif tamper == "forward_state_hash":
            connection.execute(
                "UPDATE portfolio_paper_close_settlements "
                "SET forward_state_hash = ?",
                ("0" * 64,),
            )
        elif tamper == "settlement_hash":
            connection.execute(
                "UPDATE portfolio_paper_close_settlements "
                "SET settlement_hash = ?",
                ("0" * 64,),
            )
        elif tamper == "decision_identity":
            connection.execute(
                "UPDATE portfolio_paper_close_settlements SET decision_id = ?",
                ("f" * 32,),
            )
        elif tamper == "opening_identity":
            connection.execute(
                "UPDATE portfolio_paper_close_settlements "
                "SET opening_batch_id = ?",
                ("f" * 64,),
            )
        elif tamper == "opening_batch_hash":
            connection.execute(
                "UPDATE portfolio_paper_execution_batches SET batch_hash = ?",
                ("f" * 64,),
            )
        elif tamper == "source_state":
            payload = json.loads(
                connection.execute(
                    "SELECT command_payload "
                    "FROM portfolio_paper_close_settlements"
                ).fetchone()[0]
            )
            payload["source_state_hash"] = "f" * 64
            connection.execute(
                "UPDATE portfolio_paper_close_settlements "
                "SET command_payload = ?",
                (canonical_json(payload),),
            )
        elif tamper == "settled_at":
            connection.execute(
                "UPDATE portfolio_paper_close_settlements SET settled_at = ?",
                (
                    (
                        SETTLEMENT_DATABASE_NOW + timedelta(milliseconds=1)
                    ).isoformat(),
                ),
            )
        else:
            connection.execute(
                "UPDATE portfolio_paper_tracks SET state_hash = ? WHERE id = ?",
                ("0" * 64, created.id),
            )
    with pytest.raises(PortfolioPaperCorruptionError):
        store.get(created.id)
    with pytest.raises(PortfolioPaperCorruptionError):
        store.list()
    with pytest.raises(PortfolioPaperCorruptionError):
        PortfolioPaperTrackStore(database)


def test_close_settlement_composite_foreign_key_rejects_cross_opening_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "settlement-composite-fk.db"
    store, first, _opening = prepare_opened_track(database)
    second = create_track(store, experiment_id="second-settlement-track")
    second_opening_lease = store.claim_due(
        owner="second-opening-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    )
    assert second_opening_lease is not None
    assert second_opening_lease.track_id == second.id
    store.commit_opening_if_current(
        track_id=second.id,
        owner=second_opening_lease.owner,
        generation=second_opening_lease.generation,
        expected_revision=0,
        basket=eligible_basket(),
        quotes=opening_quotes(),
    )
    lease = arm_settlement_lease(database, first.id)
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: SETTLEMENT_DATABASE_NOW,
    )
    store.commit_close_settlement_if_current(
        track_id=first.id,
        owner=lease.owner,
        generation=lease.generation,
        expected_revision=0,
        bars=close_bars(),
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for column, forged in (
            ("track_id", second.id),
            ("decision_id", "f" * 32),
            ("opening_batch_id", "f" * 64),
            ("execution_session", "2026-07-26T00:00:00+00:00"),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                connection.execute(
                    f"UPDATE portfolio_paper_close_settlements "
                    f"SET {column} = ? WHERE track_id = ?",
                    (forged, first.id),
                )
            connection.rollback()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_close_settlement_schema_rejects_extra_index(
    tmp_path: Path,
) -> None:
    database = tmp_path / "settlement-extra-index.db"
    PortfolioPaperTrackStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE INDEX unexpected_settlement_index "
            "ON portfolio_paper_close_settlements(committed_at)"
        )
    with pytest.raises(PortfolioPaperSchemaError, match="unexpected indexes"):
        PortfolioPaperTrackStore(database)


@pytest.mark.parametrize("object_kind", ["table", "view"])
def test_close_settlement_schema_rejects_extra_component_object(
    tmp_path: Path,
    object_kind: str,
) -> None:
    database = tmp_path / f"settlement-extra-{object_kind}.db"
    PortfolioPaperTrackStore(database)
    with sqlite3.connect(database) as connection:
        if object_kind == "table":
            connection.execute(
                "CREATE TABLE portfolio_paper_unexpected_shadow "
                "(value TEXT) STRICT"
            )
        else:
            connection.execute(
                "CREATE VIEW portfolio_paper_unexpected_shadow "
                "AS SELECT id FROM portfolio_paper_tracks"
            )

    with pytest.raises(
        PortfolioPaperSchemaError,
        match="unexpected component tables or views",
    ):
        PortfolioPaperTrackStore(database)


def test_opening_composite_keys_reject_cross_track_and_second_command(
    tmp_path: Path,
) -> None:
    database = tmp_path / "opening-composite-keys.db"
    store = PortfolioPaperTrackStore(database)
    first = create_track(store, experiment_id="opening-first")
    second = create_track(store, experiment_id="opening-second")
    lease = store.claim_due(
        owner="composite-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(minutes=1),
    )
    assert lease is not None
    assert lease.track_id == first.id
    batch = store.commit_opening_if_current(
        track_id=first.id,
        owner=lease.owner,
        generation=lease.generation,
        expected_revision=lease.state_revision,
        basket=eligible_basket(),
        quotes=opening_quotes(),
    )
    assert second.pending_decision is not None
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                """
                INSERT INTO portfolio_paper_execution_batches (
                    id, track_id, decision_id, execution_session,
                    idempotency_key, expected_revision, fence_generation,
                    command_schema_version, command_hash, command_payload,
                    batch_schema_version, batch_hash, batch_payload,
                    quote_set_hash, fill_set_hash, accepted_at, committed_at
                )
                SELECT ?, ?, decision_id, execution_session,
                       ?, expected_revision, fence_generation,
                       command_schema_version, command_hash, command_payload,
                       batch_schema_version, batch_hash, batch_payload,
                       quote_set_hash, fill_set_hash, accepted_at, committed_at
                FROM portfolio_paper_execution_batches WHERE track_id = ?
                """,
                ("e" * 64, second.id, "e" * 64, first.id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                """
                INSERT INTO portfolio_paper_execution_batches (
                    id, track_id, decision_id, execution_session,
                    idempotency_key, expected_revision, fence_generation,
                    command_schema_version, command_hash, command_payload,
                    batch_schema_version, batch_hash, batch_payload,
                    quote_set_hash, fill_set_hash, accepted_at, committed_at
                )
                SELECT ?, track_id, decision_id, execution_session,
                       ?, expected_revision, fence_generation,
                       command_schema_version, command_hash, command_payload,
                       batch_schema_version, batch_hash, batch_payload,
                       quote_set_hash, fill_set_hash, accepted_at, committed_at
                FROM portfolio_paper_execution_batches WHERE track_id = ?
                """,
                ("d" * 64, "d" * 64, first.id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                """
                INSERT INTO portfolio_paper_modeled_fills (
                    batch_id, track_id, decision_id, execution_session,
                    symbol, fill_schema_version, fill_hash, fill_payload
                )
                SELECT ?, ?, ?, execution_session, 'XRPUSDT',
                       fill_schema_version, fill_hash, fill_payload
                FROM portfolio_paper_modeled_fills
                WHERE batch_id = ? AND symbol = 'BTCUSDT'
                """,
                (
                    batch.command.idempotency_key,
                    second.id,
                    second.pending_decision.id,
                    batch.command.idempotency_key,
                ),
            )


def test_opening_rejects_stale_generation_expired_lease_and_model_copy_forgery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_clock = [DEFAULT_DATABASE_NOW]
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: database_clock[0],
    )
    store = PortfolioPaperTrackStore(tmp_path / "opening-fences.db")
    record = create_track(store)
    first = store.claim_due(
        owner="old-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(seconds=30),
    )
    assert first is not None
    assert store.release_claim(
        track_id=record.id,
        owner=first.owner,
        generation=first.generation,
        now=DEFAULT_DATABASE_NOW,
    )
    current = store.claim_due(
        owner="current-worker",
        now=DEFAULT_DATABASE_NOW,
        lease_for=timedelta(seconds=30),
    )
    assert current is not None
    with pytest.raises(PortfolioPaperConflictError, match="fenced lease"):
        store.commit_opening_if_current(
            track_id=record.id,
            owner=first.owner,
            generation=first.generation,
            expected_revision=first.state_revision,
            basket=eligible_basket(),
            quotes=opening_quotes(),
        )

    forged_quotes = (
        opening_quotes()[0].model_copy(update={"ask_price": Decimal("-1")}),
        opening_quotes()[1],
    )
    with pytest.raises(ValueError):
        store.commit_opening_if_current(
            track_id=record.id,
            owner=current.owner,
            generation=current.generation,
            expected_revision=current.state_revision,
            basket=eligible_basket(),
            quotes=forged_quotes,
        )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_execution_batches"
        ).fetchone()[0] == 0

    database_clock[0] = DEFAULT_DATABASE_NOW + timedelta(seconds=31)
    with pytest.raises(PortfolioPaperConflictError, match="fenced lease"):
        store.commit_opening_if_current(
            track_id=record.id,
            owner=current.owner,
            generation=current.generation,
            expected_revision=current.state_revision,
            basket=eligible_basket(),
            quotes=opening_quotes(accepted_at=database_clock[0]),
        )


def test_execution_rows_cannot_cross_link_another_track_or_session(
    tmp_path: Path,
) -> None:
    database = tmp_path / "execution-foreign-key.db"
    store = PortfolioPaperTrackStore(database)
    first = create_track(store, experiment_id="first")
    second = create_track(store, experiment_id="second")
    advance_id = "b" * 32
    session = "2026-07-25T00:00:00+00:00"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO portfolio_paper_advances (
                id, track_id, session, idempotency_key,
                command_schema_version, command_hash,
                expected_revision, resulting_revision,
                previous_state_hash, resulting_state_hash,
                command_payload, advance_payload, committed_at
            ) VALUES (?, ?, ?, ?, 1, ?, 0, 1, ?, ?, '{}', '{}', ?)
            """,
            (
                advance_id,
                first.id,
                session,
                "advance-first-session",
                "1" * 64,
                "2" * 64,
                "3" * 64,
                datetime(2026, 7, 26, tzinfo=UTC).isoformat(),
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                """
                INSERT INTO portfolio_paper_executions (
                    advance_id, track_id, session, symbol,
                    execution_schema_version, execution_hash, execution_payload
                ) VALUES (?, ?, ?, 'BTCUSDT', 1, ?, '{}')
                """,
                (advance_id, second.id, session, "4" * 64),
            )


def test_failed_decision_insert_rolls_back_activation_track(tmp_path: Path) -> None:
    database = tmp_path / "portfolio-paper.db"
    store = PortfolioPaperTrackStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_portfolio_paper_decision
            BEFORE INSERT ON portfolio_paper_decisions
            BEGIN
                SELECT RAISE(ABORT, 'injected decision failure');
            END
            """
        )

    with pytest.raises(PortfolioPaperConflictError):
        create_track(store)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_tracks"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_decisions"
        ).fetchone()[0] == 0


def test_expired_activation_rolls_back_and_pre_deadline_activation_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "activation-deadline.db"
    store = PortfolioPaperTrackStore(database)
    expired_config = config("expired")
    expired_decision = decision(expired_config)
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: expired_decision.execution_deadline,
    )

    with pytest.raises(ValueError, match="expired"):
        store.create(expired_config, expired_decision)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_tracks"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_decisions"
        ).fetchone()[0] == 0

    valid_config = config("pre-deadline")
    valid_decision = decision(valid_config)
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: valid_decision.execution_deadline
        - timedelta(milliseconds=1),
    )
    record = store.create(valid_config, valid_decision)

    assert record.pending_decision is not None
    assert (
        record.pending_decision.persisted_at
        < record.pending_decision.certificate.execution_deadline
    )


def test_restart_round_trip_list_duplicate_and_cascade_delete(
    tmp_path: Path,
) -> None:
    database = tmp_path / "portfolio-paper.db"
    first_store = PortfolioPaperTrackStore(database)
    first = create_track(first_store)
    second = create_track(
        first_store,
        experiment_id="portfolio-experiment-2",
    )
    with pytest.raises(PortfolioPaperConflictError):
        create_track(first_store)

    restarted = PortfolioPaperTrackStore(database)
    loaded = restarted.get(first.id)
    assert loaded is not None
    assert loaded == first
    assert [item.id for item in restarted.list()] == [second.id, first.id]

    assert restarted.delete(first.id) is True
    assert restarted.delete(first.id) is False
    assert restarted.get(first.id) is None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_decisions WHERE track_id = ?",
            (first.id,),
        ).fetchone()[0] == 0


def test_two_independent_stores_compete_for_one_durable_lease(
    tmp_path: Path,
) -> None:
    database = tmp_path / "portfolio-paper.db"
    first_store = PortfolioPaperTrackStore(database)
    second_store = PortfolioPaperTrackStore(database)
    record = create_track(first_store)
    barrier = Barrier(2)
    now = datetime(2026, 7, 26, tzinfo=UTC)

    def claim(
        store: PortfolioPaperTrackStore,
        owner: str,
    ) -> PortfolioPaperLease | None:
        barrier.wait()
        return store.claim_due(
            owner=owner,
            now=now,
            lease_for=timedelta(minutes=5),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(claim, first_store, "worker-1"),
            executor.submit(claim, second_store, "worker-2"),
        ]
        leases = [future.result() for future in futures]

    claimed = [lease for lease in leases if lease is not None]
    assert len(claimed) == 1
    assert claimed[0].track_id == record.id
    assert claimed[0].generation == 1
    persisted = first_store.get(record.id)
    assert persisted is not None
    assert persisted.refresh_owner == claimed[0].owner


def test_restart_preserves_lease_and_new_generation_fences_old_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_clock = [DEFAULT_DATABASE_NOW]
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: database_clock[0],
    )
    database = tmp_path / "portfolio-paper.db"
    first_store = PortfolioPaperTrackStore(database)
    record = create_track(first_store)
    started = datetime(2026, 7, 26, tzinfo=UTC)
    first_lease = first_store.claim_due(
        owner="old-worker",
        now=started,
        lease_for=timedelta(seconds=10),
    )
    assert first_lease is not None

    restarted = PortfolioPaperTrackStore(database)
    persisted = restarted.get(record.id)
    assert persisted is not None
    assert persisted.refresh_owner == "old-worker"
    assert restarted.claim_due(
        owner="too-early",
        now=started + timedelta(seconds=5),
        lease_for=timedelta(seconds=10),
    ) is None

    database_clock[0] = DEFAULT_DATABASE_NOW + timedelta(seconds=11)
    second_lease = restarted.claim_due(
        owner="new-worker",
        now=started + timedelta(seconds=11),
        lease_for=timedelta(seconds=10),
    )
    assert second_lease is not None
    assert second_lease.track_id == record.id
    assert second_lease.generation == first_lease.generation + 1
    assert first_store.release_claim(
        track_id=record.id,
        owner=first_lease.owner,
        generation=first_lease.generation,
        now=started + timedelta(seconds=12),
    ) is False
    assert first_store.set_error_if_current(
        track_id=record.id,
        owner=first_lease.owner,
        generation=first_lease.generation,
        expected_revision=first_lease.state_revision,
        message="stale worker must not win",
        checked_at=started + timedelta(seconds=12),
    ) is False

    assert restarted.set_error_if_current(
        track_id=record.id,
        owner=second_lease.owner,
        generation=second_lease.generation,
        expected_revision=second_lease.state_revision,
        message="current fenced worker error",
        checked_at=started + timedelta(seconds=12),
    ) is True
    after_error = restarted.get(record.id)
    assert after_error is not None
    assert after_error.last_error == "current fenced worker error"
    assert after_error.refresh_owner is None
    assert after_error.refresh_lease_until is None


def test_claim_terminalizes_an_expired_lease_when_safe_window_is_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_clock = [DEFAULT_DATABASE_NOW]
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: database_clock[0],
    )
    store = PortfolioPaperTrackStore(tmp_path / "terminal-opening.db")
    record = create_track(store)
    assert record.pending_decision is not None
    execution_deadline = record.pending_decision.certificate.execution_deadline

    database_clock[0] = execution_deadline - timedelta(seconds=30)
    abandoned = store.claim_due(
        owner="abandoned-worker",
        now=database_clock[0],
        lease_for=timedelta(seconds=10),
    )
    assert abandoned is not None
    assert abandoned.expires_at == execution_deadline - timedelta(seconds=20)

    database_clock[0] = execution_deadline - timedelta(seconds=5)
    assert store.claim_due(
        owner="unsafe-retry",
        now=database_clock[0],
        lease_for=timedelta(seconds=15),
    ) is None

    terminal = store.get(record.id)
    assert terminal is not None
    assert terminal.last_error == (
        "Portfolio opening could not start within its certified execution window."
    )
    assert terminal.last_checked_at == database_clock[0]
    assert terminal.updated_at == database_clock[0]
    assert terminal.refresh_owner is None
    assert terminal.refresh_lease_until is None
    assert terminal.opening_batch_id is None
    assert terminal.refresh_generation == abandoned.generation
    stable_terminal_fields = (
        terminal.last_error,
        terminal.last_checked_at,
        terminal.updated_at,
        terminal.refresh_generation,
    )

    database_clock[0] = execution_deadline + timedelta(minutes=1)
    assert store.claim_due(
        owner="must-not-repeat",
        now=database_clock[0],
        lease_for=timedelta(seconds=15),
    ) is None
    reloaded = store.get(record.id)
    assert reloaded is not None
    assert (
        reloaded.last_error,
        reloaded.last_checked_at,
        reloaded.updated_at,
        reloaded.refresh_generation,
    ) == stable_terminal_fields


def test_claim_allows_lease_that_ends_exactly_at_execution_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_clock = [DEFAULT_DATABASE_NOW]
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: database_clock[0],
    )
    store = PortfolioPaperTrackStore(tmp_path / "deadline-boundary.db")
    record = create_track(store)
    assert record.pending_decision is not None
    execution_deadline = record.pending_decision.certificate.execution_deadline
    lease_for = timedelta(seconds=15)
    database_clock[0] = execution_deadline - lease_for

    lease = store.claim_due(
        owner="boundary-worker",
        now=database_clock[0],
        lease_for=lease_for,
    )

    assert lease is not None
    assert lease.track_id == record.id
    assert lease.expires_at == execution_deadline


def test_release_and_error_require_current_revision_and_fence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "portfolio-paper.db"
    store = PortfolioPaperTrackStore(database)
    record = create_track(store)
    now = datetime(2026, 7, 26, tzinfo=UTC)
    lease = store.claim_due(
        owner=" worker ",
        now=now,
        lease_for=timedelta(minutes=1),
    )
    assert lease is not None
    assert lease.owner == "worker"

    with pytest.raises(PortfolioPaperConflictError, match="active lease"):
        store.delete(record.id)
    assert store.set_error_if_current(
        track_id=record.id,
        owner=" worker ",
        generation=lease.generation,
        expected_revision=lease.state_revision + 1,
        message="wrong revision",
        checked_at=now,
    ) is False
    assert store.release_claim(
        track_id=record.id,
        owner=" worker ",
        generation=lease.generation,
        now=now,
    ) is True
    assert store.release_claim(
        track_id=record.id,
        owner=lease.owner,
        generation=lease.generation,
        now=now,
    ) is False
    loaded = store.get(record.id)
    assert loaded is not None
    assert loaded.last_error is None


def test_noncanonical_persisted_owner_fails_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "noncanonical-owner.db"
    store = PortfolioPaperTrackStore(database)
    record = create_track(store)
    lease = store.claim_due(
        owner="worker",
        now=datetime(2026, 7, 26, tzinfo=UTC),
        lease_for=timedelta(minutes=5),
    )
    assert lease is not None
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE portfolio_paper_tracks SET refresh_owner = ' worker ' "
            "WHERE id = ?",
            (record.id,),
        )

    with pytest.raises(PortfolioPaperCorruptionError):
        store.get(record.id)
    with pytest.raises(PortfolioPaperCorruptionError):
        store.release_claim(
            track_id=record.id,
            owner=" worker ",
            generation=lease.generation,
            now=datetime(2026, 7, 26, tzinfo=UTC),
        )
    with pytest.raises(PortfolioPaperCorruptionError):
        PortfolioPaperTrackStore(database)


def test_release_claim_fails_closed_on_corrupt_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corrupt-release.db"
    store = PortfolioPaperTrackStore(database)
    record = create_track(store)
    now = datetime(2026, 7, 26, tzinfo=UTC)
    lease = store.claim_due(
        owner="corruption-guard",
        now=now,
        lease_for=timedelta(minutes=5),
    )
    assert lease is not None
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE portfolio_paper_tracks "
            "SET state_payload = state_payload || ' ' WHERE id = ?",
            (record.id,),
        )

    with pytest.raises(PortfolioPaperCorruptionError):
        store.release_claim(
            track_id=record.id,
            owner=lease.owner,
            generation=lease.generation,
            now=now,
        )
    with sqlite3.connect(database) as connection:
        persisted_lease = connection.execute(
            "SELECT refresh_owner, refresh_generation "
            "FROM portfolio_paper_tracks WHERE id = ?",
            (record.id,),
        ).fetchone()
    assert persisted_lease == (lease.owner, lease.generation)


def test_database_clock_rejects_forged_future_lease_time_across_connections(
    tmp_path: Path,
) -> None:
    database = tmp_path / "portfolio-paper.db"
    first_store = PortfolioPaperTrackStore(database)
    second_store = PortfolioPaperTrackStore(database)
    record = create_track(first_store)
    supplied_now = datetime(2026, 7, 26, tzinfo=UTC)
    forged_future = datetime(9999, 1, 1, tzinfo=UTC)
    lease = first_store.claim_due(
        owner="database-clock-owner",
        now=supplied_now,
        lease_for=timedelta(minutes=5),
    )
    assert lease is not None

    assert second_store.claim_due(
        owner="forged-future-thief",
        now=forged_future,
        lease_for=timedelta(minutes=5),
    ) is None
    assert first_store.set_error_if_current(
        track_id=record.id,
        owner=lease.owner,
        generation=lease.generation,
        expected_revision=lease.state_revision,
        message="database timestamp wins",
        checked_at=forged_future,
    ) is True

    loaded = second_store.get(record.id)
    assert loaded is not None
    assert loaded.last_checked_at is not None
    assert loaded.last_checked_at.year != forged_future.year
    assert loaded.updated_at.year != forged_future.year
    assert loaded.refresh_owner is None


def test_track_mutations_keep_persisted_time_monotonic_when_database_clock_rewinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_clock = [DEFAULT_DATABASE_NOW]
    monkeypatch.setattr(
        portfolio_paper_module,
        "_database_now",
        lambda _connection: database_clock[0],
    )
    store = PortfolioPaperTrackStore(tmp_path / "rewound-clock.db")
    record = create_track(store)
    lease_for = timedelta(minutes=5)

    database_clock[0] = DEFAULT_DATABASE_NOW - timedelta(minutes=1)
    first_lease = store.claim_due(
        owner="rewound-claim",
        now=datetime(2026, 7, 26, tzinfo=UTC),
        lease_for=lease_for,
    )
    assert first_lease is not None
    assert first_lease.expires_at == DEFAULT_DATABASE_NOW + lease_for
    claimed = store.get(record.id)
    assert claimed is not None
    assert claimed.updated_at == DEFAULT_DATABASE_NOW

    database_clock[0] = DEFAULT_DATABASE_NOW - timedelta(minutes=2)
    assert store.release_claim(
        track_id=record.id,
        owner=first_lease.owner,
        generation=first_lease.generation,
        now=datetime(2026, 7, 26, tzinfo=UTC),
    ) is True
    released = store.get(record.id)
    assert released is not None
    assert released.updated_at == DEFAULT_DATABASE_NOW
    assert released.refresh_owner is None

    database_clock[0] = DEFAULT_DATABASE_NOW - timedelta(minutes=3)
    second_lease = store.claim_due(
        owner="rewound-error",
        now=datetime(2026, 7, 26, tzinfo=UTC),
        lease_for=lease_for,
    )
    assert second_lease is not None
    assert store.set_error_if_current(
        track_id=record.id,
        owner=second_lease.owner,
        generation=second_lease.generation,
        expected_revision=second_lease.state_revision,
        message="clock rewound",
        checked_at=datetime(2026, 7, 26, tzinfo=UTC),
    ) is True
    checked = store.get(record.id)
    assert checked is not None
    assert checked.updated_at == DEFAULT_DATABASE_NOW
    assert checked.last_checked_at == DEFAULT_DATABASE_NOW
    assert checked.last_error == "clock rewound"
    assert checked.refresh_owner is None


@pytest.mark.parametrize(
    "tamper",
    ["noncanonical_state", "invalid_state_with_matching_hash", "decision_hash"],
)
def test_persisted_payload_tampering_fails_closed(
    tmp_path: Path,
    tamper: str,
) -> None:
    database = tmp_path / f"{tamper}.db"
    store = PortfolioPaperTrackStore(database)
    record = create_track(store)
    with sqlite3.connect(database) as connection:
        if tamper == "noncanonical_state":
            connection.execute(
                "UPDATE portfolio_paper_tracks "
                "SET state_payload = state_payload || ' ' WHERE id = ?",
                (record.id,),
            )
        elif tamper == "invalid_state_with_matching_hash":
            state_row = connection.execute(
                "SELECT state_payload FROM portfolio_paper_tracks WHERE id = ?",
                (record.id,),
            ).fetchone()
            payload = json.loads(state_row[0])
            payload["cash"] = -1
            changed = canonical_json(payload)
            connection.execute(
                "UPDATE portfolio_paper_tracks "
                "SET state_payload = ?, state_hash = ? WHERE id = ?",
                (changed, digest(changed), record.id),
            )
        else:
            connection.execute(
                "UPDATE portfolio_paper_decisions "
                "SET target_hash = ? WHERE track_id = ?",
                ("0" * 64, record.id),
            )

    with pytest.raises(PortfolioPaperCorruptionError):
        store.get(record.id)
    with pytest.raises(PortfolioPaperCorruptionError):
        store.list()
    with pytest.raises(PortfolioPaperCorruptionError):
        store.delete(record.id)
    with pytest.raises(PortfolioPaperCorruptionError):
        store.claim_due(
            owner="must-not-claim-corrupt-state",
            now=datetime(2026, 7, 26, tzinfo=UTC),
            lease_for=timedelta(minutes=1),
        )
    with sqlite3.connect(database) as connection:
        lease = connection.execute(
            "SELECT refresh_owner, refresh_lease_until "
            "FROM portfolio_paper_tracks WHERE id = ?",
            (record.id,),
        ).fetchone()
    assert lease == (None, None)


@pytest.mark.parametrize("tamper", ["updated_before_created", "check_after_update"])
def test_track_timestamp_inconsistency_fails_closed(
    tmp_path: Path,
    tamper: str,
) -> None:
    database = tmp_path / f"{tamper}.db"
    store = PortfolioPaperTrackStore(database)
    record = create_track(store)
    with sqlite3.connect(database) as connection:
        if tamper == "updated_before_created":
            connection.execute(
                "UPDATE portfolio_paper_tracks "
                "SET updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
                (record.id,),
            )
        else:
            connection.execute(
                "UPDATE portfolio_paper_tracks "
                "SET last_checked_at = '9999-01-01T00:00:00+00:00' WHERE id = ?",
                (record.id,),
            )

    with pytest.raises(PortfolioPaperCorruptionError):
        store.get(record.id)
    with pytest.raises(PortfolioPaperCorruptionError):
        store.claim_due(
            owner="timestamp-check",
            now=datetime(2026, 7, 26, tzinfo=UTC),
            lease_for=timedelta(minutes=1),
        )


def test_extra_unvalidated_ledger_rows_fail_closed_for_reads_and_mutations(
    tmp_path: Path,
) -> None:
    database = tmp_path / "extra-ledger-row.db"
    store = PortfolioPaperTrackStore(database)
    record = create_track(store)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO portfolio_paper_decisions (
                id, track_id, information_session,
                target_schema_version, target_hash, target_payload,
                market_input_hash, market_input_payload,
                observed_at, decided_at, persisted_at
            ) VALUES (?, ?, ?, 1, ?, '{}', ?, '{}', ?, ?, ?)
            """,
            (
                "c" * 32,
                record.id,
                "2026-07-23T00:00:00+00:00",
                "1" * 64,
                "2" * 64,
                "2026-07-24T00:02:00+00:00",
                "2026-07-24T00:02:01+00:00",
                "2026-07-24T00:03:00+00:00",
            ),
        )

    with pytest.raises(PortfolioPaperCorruptionError, match="exactly one"):
        store.get(record.id)
    with pytest.raises(PortfolioPaperCorruptionError, match="exactly one"):
        store.delete(record.id)
    with pytest.raises(PortfolioPaperCorruptionError, match="exactly one"):
        PortfolioPaperTrackStore(database)


def test_real_version_1_database_migrates_in_place_once(
    tmp_path: Path,
) -> None:
    database = tmp_path / "version-1.db"
    original_store = PortfolioPaperTrackStore(database)
    record = create_track(original_store)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DROP TABLE portfolio_paper_close_settlements")
        connection.execute("DROP TABLE portfolio_paper_modeled_fills")
        connection.execute("DROP TABLE portfolio_paper_execution_batches")
        connection.execute(
            "DROP INDEX idx_portfolio_paper_decision_execution_identity"
        )
        connection.execute(
            "DELETE FROM app_schema_migrations "
            "WHERE component = 'portfolio_paper' AND version >= 2"
        )

    migrated = PortfolioPaperTrackStore(database)
    loaded = migrated.get(record.id)
    assert loaded is not None
    assert loaded.opening_batch_id is None
    with sqlite3.connect(database) as connection:
        versions = connection.execute(
            "SELECT version FROM app_schema_migrations "
            "WHERE component = 'portfolio_paper' ORDER BY version"
        ).fetchall()
        decisions = connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_decisions"
        ).fetchone()[0]
    assert versions == [(1,), (2,), (3,)]
    assert decisions == 1

    PortfolioPaperTrackStore(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM app_schema_migrations "
            "WHERE component = 'portfolio_paper'"
        ).fetchone()[0] == 3


def test_failed_version_2_migration_rolls_back_and_can_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "migration-resume.db"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        PortfolioPaperTrackStore._create_migration_ledger(connection)
        PortfolioPaperTrackStore._apply_migration_1(connection)
        connection.execute(
            "INSERT INTO app_schema_migrations "
            "(component, version, applied_at) VALUES (?, 1, ?)",
            ("portfolio_paper", DEFAULT_DATABASE_NOW.isoformat()),
        )
    real_migration = PortfolioPaperTrackStore._apply_migration_2

    def fail_after_ddl(connection: sqlite3.Connection) -> None:
        real_migration(connection)
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(
        PortfolioPaperTrackStore,
        "_apply_migration_2",
        staticmethod(fail_after_ddl),
    )
    with pytest.raises(RuntimeError, match="injected"):
        PortfolioPaperTrackStore(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version FROM app_schema_migrations "
            "WHERE component = 'portfolio_paper' ORDER BY version"
        ).fetchall() == [(1,)]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' "
            "AND name = 'portfolio_paper_execution_batches'"
        ).fetchone() is None

    monkeypatch.setattr(
        PortfolioPaperTrackStore,
        "_apply_migration_2",
        staticmethod(real_migration),
    )
    PortfolioPaperTrackStore(database)


def test_real_version_2_database_with_opening_migrates_to_version_3(
    tmp_path: Path,
) -> None:
    database = tmp_path / "version-2-with-opening.db"
    _store, created, opening = prepare_opened_track(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DROP TABLE portfolio_paper_close_settlements")
        connection.execute(
            "DELETE FROM app_schema_migrations "
            "WHERE component = 'portfolio_paper' AND version = 3"
        )

    migrated = PortfolioPaperTrackStore(database)
    loaded = migrated.get(created.id)
    assert loaded is not None
    assert loaded.opening_batch_id == opening.command.idempotency_key
    assert loaded.settlement_id is None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version FROM app_schema_migrations "
            "WHERE component = 'portfolio_paper' ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,)]
        assert connection.execute(
            "SELECT COUNT(*) FROM portfolio_paper_close_settlements"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_version_3_migration_rolls_back_and_concurrent_resume_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "version-3-resume.db"
    _store, created, opening = prepare_opened_track(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DROP TABLE portfolio_paper_close_settlements")
        connection.execute(
            "DELETE FROM app_schema_migrations "
            "WHERE component = 'portfolio_paper' AND version = 3"
        )
    real_migration = PortfolioPaperTrackStore._apply_migration_3

    def fail_after_ddl(connection: sqlite3.Connection) -> None:
        real_migration(connection)
        raise RuntimeError("injected version 3 migration failure")

    monkeypatch.setattr(
        PortfolioPaperTrackStore,
        "_apply_migration_3",
        staticmethod(fail_after_ddl),
    )
    with pytest.raises(RuntimeError, match="version 3"):
        PortfolioPaperTrackStore(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version FROM app_schema_migrations "
            "WHERE component = 'portfolio_paper' ORDER BY version"
        ).fetchall() == [(1,), (2,)]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'portfolio_paper_close_settlements'"
        ).fetchone() is None

    monkeypatch.setattr(
        PortfolioPaperTrackStore,
        "_apply_migration_3",
        staticmethod(real_migration),
    )
    barrier = Barrier(2)

    def initialize(_: int) -> PortfolioPaperTrackRecord | None:
        barrier.wait()
        return PortfolioPaperTrackStore(database).get(created.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        loaded = list(pool.map(initialize, (1, 2)))
    assert loaded[0] == loaded[1]
    assert loaded[0] is not None
    assert loaded[0].opening_batch_id == opening.command.idempotency_key
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM app_schema_migrations "
            "WHERE component = 'portfolio_paper' AND version = 3"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("attack", ["negative", "missing_v1", "bad_time"])
def test_migration_ledger_version_set_and_timestamps_fail_closed(
    tmp_path: Path,
    attack: str,
) -> None:
    database = tmp_path / f"migration-{attack}.db"
    PortfolioPaperTrackStore(database)
    with sqlite3.connect(database) as connection:
        if attack == "negative":
            connection.execute(
                "INSERT INTO app_schema_migrations "
                "(component, version, applied_at) VALUES (?, ?, ?)",
                ("portfolio_paper", -1, DEFAULT_DATABASE_NOW.isoformat()),
            )
        elif attack == "missing_v1":
            connection.execute(
                "DELETE FROM app_schema_migrations "
                "WHERE component = 'portfolio_paper' AND version = 1"
            )
        else:
            connection.execute(
                "UPDATE app_schema_migrations SET applied_at = 'not-canonical' "
                "WHERE component = 'portfolio_paper' AND version = 2"
            )

    with pytest.raises(PortfolioPaperSchemaError):
        PortfolioPaperTrackStore(database)


def test_unknown_component_schema_version_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "future-schema.db"
    PortfolioPaperTrackStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO app_schema_migrations "
            "(component, version, applied_at) VALUES (?, ?, ?)",
            (
                "portfolio_paper",
                4,
                datetime(2026, 7, 26, tzinfo=UTC).isoformat(),
            ),
        )

    with pytest.raises(PortfolioPaperSchemaError, match="newer"):
        PortfolioPaperTrackStore(database)


def test_version_2_schema_rejects_extra_trigger_and_index(
    tmp_path: Path,
) -> None:
    for attack in ("trigger", "index"):
        database = tmp_path / f"v2-{attack}.db"
        PortfolioPaperTrackStore(database)
        with sqlite3.connect(database) as connection:
            if attack == "trigger":
                connection.execute(
                    """
                    CREATE TRIGGER mutate_opening_batch
                    AFTER INSERT ON portfolio_paper_execution_batches
                    BEGIN
                        UPDATE portfolio_paper_execution_batches
                        SET command_hash = command_hash
                        WHERE id = NEW.id;
                    END
                    """
                )
            else:
                connection.execute(
                    "CREATE INDEX unexpected_opening_index "
                    "ON portfolio_paper_execution_batches(committed_at)"
                )
        with pytest.raises(PortfolioPaperSchemaError):
            PortfolioPaperTrackStore(database)


def test_mutated_migration_ledger_schema_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "mutated-migration-ledger.db"
    PortfolioPaperTrackStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE app_schema_migrations")
        connection.execute(
            """
            CREATE TABLE app_schema_migrations (
                component TEXT,
                version INTEGER,
                applied_at TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO app_schema_migrations "
            "(component, version, applied_at) VALUES (?, ?, ?)",
            ("portfolio_paper", 1, "forged"),
        )
        connection.execute(
            """
            CREATE TRIGGER erase_future_portfolio_paper_migrations
            AFTER INSERT ON app_schema_migrations
            WHEN NEW.component = 'portfolio_paper' AND NEW.version > 1
            BEGIN
                DELETE FROM app_schema_migrations
                WHERE component = NEW.component AND version = NEW.version;
            END
            """
        )

    with pytest.raises(PortfolioPaperSchemaError):
        PortfolioPaperTrackStore(database)


def test_restart_rejects_orphaned_component_rows(tmp_path: Path) -> None:
    database = tmp_path / "orphaned-row.db"
    PortfolioPaperTrackStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO portfolio_paper_decisions (
                id, track_id, information_session,
                target_schema_version, target_hash, target_payload,
                market_input_hash, market_input_payload,
                observed_at, decided_at, persisted_at
            ) VALUES (?, ?, ?, 1, ?, '{}', ?, '{}', ?, ?, ?)
            """,
            (
                "a" * 32,
                "b" * 32,
                "2026-07-24T00:00:00+00:00",
                "1" * 64,
                "2" * 64,
                "2026-07-25T00:02:00+00:00",
                "2026-07-25T00:02:01+00:00",
                "2026-07-25T00:03:00+00:00",
            ),
        )

    with pytest.raises(PortfolioPaperCorruptionError, match="orphaned"):
        PortfolioPaperTrackStore(database)


def test_schema_verifier_rejects_partial_unique_indexes() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    PortfolioPaperTrackStore._apply_migration_1(connection)
    connection.execute(
        "CREATE UNIQUE INDEX invalid_partial_unique "
        "ON portfolio_paper_tracks(status) WHERE status = 'internal_only'"
    )

    with pytest.raises(PortfolioPaperSchemaError, match="partial unique"):
        PortfolioPaperTrackStore._verify_schema(connection)
    connection.close()


@pytest.mark.parametrize(
    "broken_table",
    [
        "portfolio_paper_tracks",
        "portfolio_paper_decisions",
        "portfolio_paper_advances",
        "portfolio_paper_executions",
    ],
)
def test_schema_verifier_rejects_columns_without_required_constraints(
    broken_table: str,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=OFF")
    PortfolioPaperTrackStore._apply_migration_1(connection)
    connection.execute(f"ALTER TABLE {broken_table} RENAME TO broken_original")
    replacements = {
        "portfolio_paper_tracks": """
            CREATE TABLE portfolio_paper_tracks (
                id TEXT, portfolio_experiment_id TEXT, tracking_mode TEXT,
                status TEXT, config_schema_version INTEGER,
                configuration_hash TEXT, config_payload TEXT,
                state_schema_version INTEGER, state_revision INTEGER,
                state_session TEXT, state_hash TEXT, state_payload TEXT,
                refresh_generation INTEGER, refresh_owner TEXT,
                refresh_lease_until TEXT, created_at TEXT, updated_at TEXT,
                last_checked_at TEXT, last_error TEXT
            )
        """,
        "portfolio_paper_decisions": """
            CREATE TABLE portfolio_paper_decisions (
                id TEXT, track_id TEXT, information_session TEXT,
                target_schema_version INTEGER, target_hash TEXT,
                target_payload TEXT, market_input_hash TEXT,
                market_input_payload TEXT, observed_at TEXT, decided_at TEXT
            )
        """,
        "portfolio_paper_advances": """
            CREATE TABLE portfolio_paper_advances (
                id TEXT, track_id TEXT, session TEXT, idempotency_key TEXT,
                command_schema_version INTEGER, command_hash TEXT,
                expected_revision INTEGER, resulting_revision INTEGER,
                previous_state_hash TEXT, resulting_state_hash TEXT,
                command_payload TEXT, advance_payload TEXT, committed_at TEXT
            )
        """,
        "portfolio_paper_executions": """
            CREATE TABLE portfolio_paper_executions (
                advance_id TEXT, track_id TEXT, session TEXT, symbol TEXT,
                execution_schema_version INTEGER, execution_hash TEXT,
                execution_payload TEXT
            )
        """,
    }
    connection.execute(replacements[broken_table])
    connection.execute("DROP TABLE broken_original")

    with pytest.raises(PortfolioPaperSchemaError):
        PortfolioPaperTrackStore._verify_schema(connection)
    connection.close()


def test_schema_verifier_rejects_tracks_without_primary_and_unique_keys() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=OFF")
    PortfolioPaperTrackStore._apply_migration_1(connection)
    connection.execute("ALTER TABLE portfolio_paper_tracks RENAME TO broken_tracks")
    connection.execute(
        """
        CREATE TABLE portfolio_paper_tracks (
            id TEXT NOT NULL,
            portfolio_experiment_id TEXT NOT NULL,
            tracking_mode TEXT NOT NULL CHECK(tracking_mode = 'execution_paper'),
            status TEXT NOT NULL CHECK(status = 'internal_only'),
            config_schema_version INTEGER NOT NULL,
            configuration_hash TEXT NOT NULL,
            config_payload TEXT NOT NULL,
            state_schema_version INTEGER NOT NULL,
            state_revision INTEGER NOT NULL CHECK(state_revision >= 0),
            state_session TEXT,
            state_hash TEXT NOT NULL,
            state_payload TEXT NOT NULL,
            refresh_generation INTEGER NOT NULL CHECK(refresh_generation >= 0),
            refresh_owner TEXT,
            refresh_lease_until TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_checked_at TEXT,
            last_error TEXT,
            CHECK(
                (refresh_owner IS NULL AND refresh_lease_until IS NULL)
                OR
                (refresh_owner IS NOT NULL AND refresh_lease_until IS NOT NULL)
            )
        )
        """
    )
    connection.execute("DROP TABLE broken_tracks")

    with pytest.raises(PortfolioPaperSchemaError):
        PortfolioPaperTrackStore._verify_schema(connection)
    connection.close()
