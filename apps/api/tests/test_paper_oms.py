from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError
from quantsieve_api.paper_oms import (
    CreatePaperAccountCommand,
    PaperOmsConflictError,
    PaperOmsExecutionUnavailableError,
    PaperOmsFillExecutionEvidence,
    PaperOmsIntegrityError,
    PaperOmsNotFoundError,
    PaperOmsOrderMarketEvidence,
    PaperOmsReservationStateEvidence,
    PaperOmsRevisionError,
    PaperOmsRiskRejectedError,
    PaperOmsRiskUnavailableError,
    PaperOmsStore,
    PaperOmsTransitionError,
    PaperOrderRecord,
    RecordPaperFillCommand,
    RecordPaperOrderEventCommand,
    ServerOwnedPaperKillSwitch,
    SubmitPaperOrderCommand,
)
from quantsieve_engine.risk import (
    OrderPriceEvidence,
    OrderRiskLimits,
    OrderRiskRuleSet,
    OrderRiskState,
    RiskDecision,
    build_order_risk_request,
    build_order_rule_set,
    canonical_payload_hash,
    evaluate_order_risk,
)
from quantsieve_providers import BinanceSpotTradingRules, ExecutionQuote

NOW = datetime(2026, 7, 29, 0, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def _test_order_rules() -> OrderRiskRuleSet:
    return build_order_rule_set(
        OrderRiskLimits(
            maximum_order_notional=Decimal("1000000000"),
            maximum_resulting_position=Decimal("1000000000"),
            maximum_active_orders=1000,
            minimum_cash_reserve=Decimal(0),
            maximum_price_age_seconds=1_000_000_000,
            maximum_kill_switch_age_seconds=1_000_000_000,
            market_order_price_buffer_ratio=Decimal(0),
            order_fee_buffer_ratio=Decimal(0),
        ),
        rule_set_id="paper-oms-tests",
    )


def _test_market_evidence(
    command: SubmitPaperOrderCommand,
    currency: str,
    *,
    reference_price: Decimal,
    observed_at: datetime,
    source: str = "paper-oms-tests",
) -> PaperOmsOrderMarketEvidence:
    rules = BinanceSpotTradingRules(
        symbol=command.symbol,
        base_asset="BTC",
        quote_asset=currency,
        status="TRADING",
        spot_trading_allowed=True,
        order_types=("LIMIT", "MARKET"),
        lot_step_size=Decimal("0.00000001"),
        lot_min_quantity=Decimal("0.00000001"),
        lot_max_quantity=Decimal("1000000000"),
        market_step_size=Decimal("0.00000001"),
        market_min_quantity=Decimal("0.00000001"),
        market_max_quantity=Decimal("1000000000"),
        min_notional=Decimal("0.00000001"),
        min_notional_applies_to_market=True,
        max_notional=None,
        max_notional_applies_to_market=False,
        notional_average_price_minutes=0,
        verified_at=observed_at,
    )
    quote = ExecutionQuote(
        symbol=command.symbol,
        provider="binance",
        venue="Binance Spot",
        bid_price=reference_price,
        ask_price=reference_price,
        bid_quantity=Decimal("1000000000"),
        ask_quantity=Decimal("1000000000"),
        notional_reference_price=reference_price,
        notional_reference_kind="last_price",
        notional_reference_window_minutes=0,
        notional_reference_at=observed_at,
        notional_reference_observed_at=observed_at,
        exchange_reference_available=False,
        exchange_reference_at=observed_at,
        exchange_reference_observed_at=observed_at,
        request_started_at=observed_at,
        observed_at=observed_at,
        exchange_server_time=observed_at,
        clock_checked_at=observed_at,
        cache_used=False,
    )
    snapshot_hash = canonical_payload_hash(
        {
            "contract": "quantsieve.paper-oms.binance-market-snapshot.v1",
            "rules": rules,
            "quote": quote,
        }
    )
    price_evidence = OrderPriceEvidence(
        symbol=command.symbol,
        quote_currency=currency,
        reference_price=reference_price,
        observed_at=observed_at,
        available_at=observed_at,
        source=source,
        snapshot_hash=snapshot_hash,
    )
    payload = {
        "schema_version": 1,
        "contract": "quantsieve.paper-oms.order-market-evidence.v1",
        "side": command.side,
        "trading_rules": rules,
        "quote": quote,
        "price_evidence": price_evidence,
        "rules_snapshot_hash": canonical_payload_hash(rules),
        "quote_snapshot_hash": canonical_payload_hash(quote),
    }
    return PaperOmsOrderMarketEvidence(
        **payload,
        evidence_hash=canonical_payload_hash(payload),
    )


def oms_store(
    path: str | Path,
    *,
    clock: Callable[[], datetime],
    allow_historical_client_timestamps: bool = True,
) -> PaperOmsStore:
    authority = ServerOwnedPaperKillSwitch(
        path,
        clock=clock,
        source="paper-oms-tests",
    )
    authority.clear()

    def market_evidence(
        *,
        command: SubmitPaperOrderCommand,
        currency: str,
        reference_price: Decimal,
    ) -> PaperOmsOrderMarketEvidence:
        observed_at = clock()
        rules = BinanceSpotTradingRules(
            symbol=command.symbol,
            base_asset="BTC",
            quote_asset=currency,
            status="TRADING",
            spot_trading_allowed=True,
            order_types=("LIMIT", "MARKET"),
            lot_step_size=Decimal("0.00000001"),
            lot_min_quantity=Decimal("0.00000001"),
            lot_max_quantity=Decimal("1000000000"),
            market_step_size=Decimal("0.00000001"),
            market_min_quantity=Decimal("0.00000001"),
            market_max_quantity=Decimal("1000000000"),
            min_notional=Decimal("0.00000001"),
            min_notional_applies_to_market=True,
            max_notional=None,
            max_notional_applies_to_market=False,
            notional_average_price_minutes=0,
            verified_at=observed_at,
        )
        quote = ExecutionQuote(
            symbol=command.symbol,
            provider="binance",
            venue="Binance Spot",
            bid_price=reference_price,
            ask_price=reference_price,
            bid_quantity=Decimal("1000000000"),
            ask_quantity=Decimal("1000000000"),
            notional_reference_price=reference_price,
            notional_reference_kind="last_price",
            notional_reference_window_minutes=0,
            notional_reference_at=observed_at,
            notional_reference_observed_at=observed_at,
            exchange_reference_available=False,
            exchange_reference_at=observed_at,
            exchange_reference_observed_at=observed_at,
            request_started_at=observed_at,
            observed_at=observed_at,
            exchange_server_time=observed_at,
            clock_checked_at=observed_at,
            cache_used=False,
        )
        snapshot_hash = canonical_payload_hash(
            {
                "contract": "quantsieve.paper-oms.binance-market-snapshot.v1",
                "rules": rules,
                "quote": quote,
            }
        )
        price_evidence = OrderPriceEvidence(
            symbol=command.symbol,
            quote_currency=currency,
            reference_price=reference_price,
            observed_at=observed_at,
            available_at=observed_at,
            source="paper-oms-tests",
            snapshot_hash=snapshot_hash,
        )
        payload = {
            "schema_version": 1,
            "contract": "quantsieve.paper-oms.order-market-evidence.v1",
            "side": command.side,
            "trading_rules": rules,
            "quote": quote,
            "price_evidence": price_evidence,
            "rules_snapshot_hash": canonical_payload_hash(rules),
            "quote_snapshot_hash": canonical_payload_hash(quote),
        }
        return PaperOmsOrderMarketEvidence(
            **payload,
            evidence_hash=canonical_payload_hash(payload),
        )

    def price(
        command: SubmitPaperOrderCommand,
        currency: str,
    ) -> PaperOmsOrderMarketEvidence:
        reference_price = {
            "buy-one": Decimal("12"),
            "buy-two": Decimal("20"),
            "sell-cross": Decimal("30"),
            "sell-covered": Decimal("5"),
            "low-cash-buy": Decimal("4"),
        }.get(command.order_id, Decimal("10"))
        return market_evidence(
            command=command,
            currency=currency,
            reference_price=reference_price,
        )

    def fill_evidence(
        command: RecordPaperFillCommand,
        order: PaperOrderRecord,
    ) -> PaperOmsFillExecutionEvidence:
        safe_order = order.risk_evaluation
        assert safe_order is not None
        approval_market = safe_order.market_evidence
        assert approval_market is not None
        submit = submit_command(
            order_id=command.order_id,
            side=order.side,
            quantity=str(command.quantity),
            minutes=0,
            account_id=command.account_id,
            source=command.execution_source,
        )
        market = market_evidence(
            command=submit,
            currency=approval_market.trading_rules.quote_asset,
            reference_price=command.fill_price,
        )
        payload = {
            "schema_version": 1,
            "contract": "quantsieve.paper-oms.simulated-fill-evidence.v1",
            "account_id": command.account_id,
            "order_id": command.order_id,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": command.quantity,
            "execution_source": command.execution_source,
            "external_fill_id": command.external_fill_id,
            "reference_price": command.fill_price,
            "fill_price": command.fill_price,
            "fee_rate": Decimal(0),
            "fee": command.fee,
            "observed_at": market.quote.observed_at,
            "available_at": market.quote.observed_at,
            "approval_evidence_hash": approval_market.evidence_hash,
            "trading_rules": market.trading_rules,
            "quote": market.quote,
            "rules_snapshot_hash": market.rules_snapshot_hash,
            "quote_snapshot_hash": market.quote_snapshot_hash,
        }
        return PaperOmsFillExecutionEvidence(
            **payload,
            evidence_hash=canonical_payload_hash(payload),
        )

    return PaperOmsStore(
        path,
        clock=clock,
        order_risk_rule_set=_test_order_rules(),
        kill_switch_reader=authority.read,
        price_evidence_reader=price,
        fill_evidence_reader=fill_evidence,
        allow_historical_client_timestamps=allow_historical_client_timestamps,
    )


def account_command(
    *,
    account_id: str = "account-one",
    key: str | None = None,
    cash: str = "1000",
    namespace: str = "test-suite",
) -> CreatePaperAccountCommand:
    return CreatePaperAccountCommand.model_validate(
        {
            "command_namespace": namespace,
            "idempotency_key": key or f"create-{account_id}",
            "account_id": account_id,
            "currency": "usdt",
            "initial_cash": cash,
            "occurred_at": NOW,
        }
    )


def submit_command(
    *,
    order_id: str,
    side: str,
    quantity: str,
    minutes: int,
    account_id: str = "account-one",
    source: str = "paper-sim:test-account",
    key: str | None = None,
) -> SubmitPaperOrderCommand:
    return SubmitPaperOrderCommand.model_validate(
        {
            "command_namespace": "test-suite",
            "idempotency_key": key or f"submit-{account_id}-{order_id}",
            "account_id": account_id,
            "order_id": order_id,
            "symbol": "btcusdt",
            "side": side,
            "execution_source": source,
            "quantity": quantity,
            "occurred_at": at(minutes),
        }
    )


def lifecycle_command(
    *,
    order_id: str,
    revision: int,
    event_type: str = "acknowledged",
    minutes: int,
    account_id: str = "account-one",
    key: str | None = None,
    reason: str | None = None,
) -> RecordPaperOrderEventCommand:
    payload: dict[str, object] = {
        "command_namespace": "test-suite",
        "idempotency_key": key or f"{event_type}-{account_id}-{order_id}-{revision}",
        "account_id": account_id,
        "order_id": order_id,
        "expected_order_revision": revision,
        "event_type": event_type,
        "occurred_at": at(minutes),
    }
    if reason is not None:
        payload["reason"] = reason
    return RecordPaperOrderEventCommand.model_validate(payload)


def fill_command(
    *,
    order_id: str,
    external_fill_id: str,
    order_revision: int,
    account_revision: int,
    quantity: str,
    fill_price: str,
    minutes: int,
    account_id: str = "account-one",
    source: str = "paper-sim:test-account",
    reference_price: str | None = None,
    fee: str = "0",
    key: str | None = None,
    namespace: str = "test-suite",
) -> RecordPaperFillCommand:
    return RecordPaperFillCommand.model_validate(
        {
            "command_namespace": namespace,
            "idempotency_key": key or f"record-{account_id}-{external_fill_id}",
            "account_id": account_id,
            "order_id": order_id,
            "expected_order_revision": order_revision,
            "expected_account_revision": account_revision,
            "execution_source": source,
            "external_fill_id": external_fill_id,
            "quantity": quantity,
            "reference_price": reference_price or fill_price,
            "fill_price": fill_price,
            "fee": fee,
            "occurred_at": at(minutes),
        }
    )


def create_and_ack_order(
    store: PaperOmsStore,
    *,
    order_id: str,
    side: str,
    quantity: str,
    submit_minute: int,
    ack_minute: int,
    account_id: str = "account-one",
    source: str = "paper-sim:test-account",
) -> None:
    store.submit_order(
        submit_command(
            order_id=order_id,
            side=side,
            quantity=quantity,
            minutes=submit_minute,
            account_id=account_id,
            source=source,
        )
    )
    store.record_order_event(
        lifecycle_command(
            order_id=order_id,
            revision=0,
            minutes=ack_minute,
            account_id=account_id,
        )
    )


def test_create_account_is_strict_idempotent_durable_and_wal_backed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "paper-oms.db"
    store = oms_store(database, clock=lambda: at(100))
    command = account_command()

    created = store.create_account(command)
    replayed = store.create_account(command)

    assert created.idempotent_replay is False
    assert replayed.idempotent_replay is True
    assert replayed.account == created.account
    assert created.account.ledger.cash == Decimal("1000")
    assert created.account.ledger.revision == 1
    with pytest.raises(ValidationError):
        created.account.currency = "USD"

    restarted = oms_store(database, clock=lambda: at(101))
    assert restarted.get_account(" account-one ") == created.account
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "paper_oms_meta",
        "paper_oms_accounts",
        "paper_oms_orders",
        "paper_oms_order_events",
        "paper_oms_commands",
    } <= tables

    conflicting = account_command(cash="1001")
    with pytest.raises(PaperOmsConflictError):
        restarted.create_account(conflicting)


def test_partial_fills_multiple_orders_and_fifo_sell_share_one_account(
    tmp_path: Path,
) -> None:
    store = oms_store(tmp_path / "multi-order.db", clock=lambda: at(100))
    store.create_account(account_command())
    create_and_ack_order(
        store,
        order_id="buy-one",
        side="buy",
        quantity="2",
        submit_minute=1,
        ack_minute=2,
    )
    first_partial = store.record_fill(
        fill_command(
            order_id="buy-one",
            external_fill_id="buy-one-first",
            order_revision=1,
            account_revision=1,
            quantity="1",
            fill_price="10",
            minutes=3,
        )
    )
    assert first_partial.order.state.status == "partially_filled"
    assert first_partial.order.state.filled_quantity == Decimal("1")

    create_and_ack_order(
        store,
        order_id="buy-two",
        side="buy",
        quantity="2",
        submit_minute=4,
        ack_minute=5,
    )
    store.record_fill(
        fill_command(
            order_id="buy-two",
            external_fill_id="buy-two-only",
            order_revision=1,
            account_revision=2,
            quantity="2",
            fill_price="20",
            minutes=6,
        )
    )
    store.record_fill(
        fill_command(
            order_id="buy-one",
            external_fill_id="buy-one-second",
            order_revision=2,
            account_revision=3,
            quantity="1",
            fill_price="12",
            minutes=7,
        )
    )

    create_and_ack_order(
        store,
        order_id="sell-cross",
        side="sell",
        quantity="2.5",
        submit_minute=8,
        ack_minute=9,
    )
    sold = store.record_fill(
        fill_command(
            order_id="sell-cross",
            external_fill_id="sell-cross-only",
            order_revision=1,
            account_revision=4,
            quantity="2.5",
            fill_price="30",
            minutes=10,
        )
    )

    assert sold.order.state.status == "filled"
    assert sold.account.ledger.revision == 5
    assert sold.account.ledger.cash == Decimal("1013")
    assert sold.account.ledger.realized_pnl == Decimal("35")
    position = sold.account.ledger.positions[0]
    assert position.quantity == Decimal("1.5")
    assert position.book_cost == Decimal("22")
    assert [lot.remaining_quantity for lot in position.lots] == [
        Decimal("0"),
        Decimal("0.5"),
        Decimal("1"),
    ]
    assert sold.event.ledger_fill is not None
    sell_consumptions = tuple(
        item
        for item in sold.account.ledger.lot_consumptions
        if item.fill_id == sold.event.ledger_fill.fill_id
    )
    assert [item.quantity for item in sell_consumptions] == [
        Decimal("1"),
        Decimal("1.5"),
    ]
    assert [item.cost_basis for item in sell_consumptions] == [
        Decimal("10"),
        Decimal("30"),
    ]
    assert [order.order_id for order in store.list_orders("account-one")] == [
        "buy-one",
        "buy-two",
        "sell-cross",
    ]


def test_exact_and_economic_fill_replay_do_not_advance_revisions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "fill-replay.db"
    store = oms_store(database, clock=lambda: at(100))
    store.create_account(account_command())
    create_and_ack_order(
        store,
        order_id="buy",
        side="buy",
        quantity="2",
        submit_minute=1,
        ack_minute=2,
    )
    original = fill_command(
        order_id="buy",
        external_fill_id="venue-fill-1",
        order_revision=1,
        account_revision=1,
        quantity="1",
        fill_price="10",
        minutes=3,
    )
    committed = store.record_fill(original)
    exact = store.record_fill(original)
    alias = fill_command(
        order_id="buy",
        external_fill_id="venue-fill-1",
        order_revision=999,
        account_revision=999,
        quantity="1",
        fill_price="10",
        minutes=3,
        key="fill-alias-delivery",
    )
    economic = store.record_fill(alias)

    assert exact.idempotent_replay is True
    assert economic.idempotent_replay is True
    assert exact.event.event_id == committed.event.event_id
    assert economic.event.event_id == committed.event.event_id
    assert economic.order.state.revision == 2
    assert economic.account.ledger.revision == 2

    with sqlite3.connect(database) as connection:
        fill_events = connection.execute(
            "SELECT COUNT(*) FROM paper_oms_order_events WHERE event_type = 'fill'"
        ).fetchone()[0]
        fill_commands = connection.execute(
            "SELECT COUNT(*) FROM paper_oms_commands WHERE command_kind = 'fill'"
        ).fetchone()[0]
    assert fill_events == 1
    assert fill_commands == 2

    changed_alias = fill_command(
        order_id="buy",
        external_fill_id="venue-fill-1",
        order_revision=999,
        account_revision=999,
        quantity="1",
        fill_price="11",
        minutes=3,
        key="fill-alias-delivery",
    )
    changed_quote_replay = store.record_fill(changed_alias)
    assert changed_quote_replay.idempotent_replay is True
    assert changed_quote_replay.event.event_id == economic.event.event_id

    changed_public_revision = fill_command(
        order_id="buy",
        external_fill_id="venue-fill-1",
        order_revision=998,
        account_revision=999,
        quantity="1",
        fill_price="10",
        minutes=3,
        key="fill-alias-delivery",
    )
    with pytest.raises(PaperOmsConflictError, match="idempotency"):
        store.record_fill(changed_public_revision)

    changed_economics = fill_command(
        order_id="buy",
        external_fill_id="venue-fill-1",
        order_revision=2,
        account_revision=2,
        quantity="1",
        fill_price="11",
        minutes=4,
        key="changed-economic-delivery",
    )
    with pytest.raises(PaperOmsConflictError, match="Namespaced fill"):
        store.record_fill(changed_economics)

    restarted = oms_store(database, clock=lambda: at(101))
    assert restarted.get_account("account-one").ledger.revision == 2
    assert restarted.get_order("account-one", "buy").state.revision == 2


def test_order_event_idempotency_precedes_stale_revision_checks(
    tmp_path: Path,
) -> None:
    database = tmp_path / "event-replay.db"
    store = oms_store(database, clock=lambda: at(100))
    store.create_account(account_command())
    store.submit_order(
        submit_command(
            order_id="cancel-me",
            side="buy",
            quantity="1",
            minutes=1,
        )
    )
    acknowledged = lifecycle_command(
        order_id="cancel-me",
        revision=0,
        minutes=2,
        key="ack-delivery",
    )
    committed = store.record_order_event(acknowledged)
    replayed = store.record_order_event(acknowledged)

    assert committed.idempotent_replay is False
    assert replayed.idempotent_replay is True
    assert replayed.event.event_id == committed.event.event_id
    assert replayed.order.state.revision == 1

    changed_same_key = lifecycle_command(
        order_id="cancel-me",
        revision=1,
        event_type="cancel_requested",
        minutes=3,
        key="ack-delivery",
    )
    with pytest.raises(PaperOmsConflictError, match="idempotency"):
        store.record_order_event(changed_same_key)

    stale_cancel = lifecycle_command(
        order_id="cancel-me",
        revision=0,
        event_type="cancel_requested",
        minutes=3,
        key="retryable-cancel",
    )
    with pytest.raises(PaperOmsRevisionError):
        store.record_order_event(stale_cancel)

    current_cancel = lifecycle_command(
        order_id="cancel-me",
        revision=1,
        event_type="cancel_requested",
        minutes=3,
        key="retryable-cancel",
    )
    cancelled = store.record_order_event(current_cancel)
    assert cancelled.order.state.status == "pending_cancel"
    assert cancelled.order.state.revision == 2
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_oms_order_events").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM paper_oms_commands WHERE command_kind = 'order_event'"
            ).fetchone()[0]
            == 2
        )


def test_namespaced_fill_identity_is_unique_across_orders_and_accounts(
    tmp_path: Path,
) -> None:
    store = oms_store(tmp_path / "global-fill.db", clock=lambda: at(100))
    store.create_account(account_command(account_id="account-one"))
    store.create_account(account_command(account_id="account-two"))
    create_and_ack_order(
        store,
        order_id="order-one",
        side="buy",
        quantity="1",
        submit_minute=1,
        ack_minute=2,
        account_id="account-one",
    )
    create_and_ack_order(
        store,
        order_id="order-two",
        side="buy",
        quantity="1",
        submit_minute=1,
        ack_minute=2,
        account_id="account-two",
    )
    store.record_fill(
        fill_command(
            order_id="order-one",
            external_fill_id="shared-external-id",
            order_revision=1,
            account_revision=1,
            quantity="1",
            fill_price="10",
            minutes=3,
            account_id="account-one",
        )
    )

    cross_account = fill_command(
        order_id="order-two",
        external_fill_id="shared-external-id",
        order_revision=1,
        account_revision=1,
        quantity="1",
        fill_price="10",
        minutes=3,
        account_id="account-two",
    )
    with pytest.raises(PaperOmsConflictError, match="Namespaced fill"):
        store.record_fill(cross_account)

    create_and_ack_order(
        store,
        order_id="other-source-order",
        side="buy",
        quantity="1",
        submit_minute=4,
        ack_minute=5,
        account_id="account-two",
        source="paper-sim:other-account",
    )
    other_source = store.record_fill(
        fill_command(
            order_id="other-source-order",
            external_fill_id="shared-external-id",
            order_revision=1,
            account_revision=1,
            quantity="1",
            fill_price="10",
            minutes=6,
            account_id="account-two",
            source="paper-sim:other-account",
        )
    )
    assert other_source.account.ledger.revision == 2


def test_insufficient_cash_and_position_roll_back_every_projection(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rollback.db"
    store = oms_store(database, clock=lambda: at(100))
    store.create_account(account_command(cash="10"))
    create_and_ack_order(
        store,
        order_id="low-cash-buy",
        side="buy",
        quantity="2",
        submit_minute=1,
        ack_minute=2,
    )
    unfunded = fill_command(
        order_id="low-cash-buy",
        external_fill_id="retryable-buy",
        order_revision=1,
        account_revision=1,
        quantity="2",
        fill_price="6",
        minutes=3,
        key="retryable-buy-command",
    )
    with pytest.raises(PaperOmsTransitionError, match="risk-price"):
        store.record_fill(unfunded)
    assert store.get_account("account-one").ledger.revision == 1
    assert store.get_order("account-one", "low-cash-buy").state.revision == 1

    funded = fill_command(
        order_id="low-cash-buy",
        external_fill_id="retryable-buy",
        order_revision=1,
        account_revision=1,
        quantity="2",
        fill_price="4",
        minutes=3,
        key="retryable-buy-command",
    )
    bought = store.record_fill(funded)
    assert bought.account.ledger.cash == Decimal("2")
    assert bought.order.state.status == "filled"

    excessive_sell = submit_command(
        order_id="sell-too-much",
        side="sell",
        quantity="3",
        minutes=4,
    )
    with pytest.raises(PaperOmsRiskRejectedError) as rejected:
        store.submit_order(excessive_sell)
    assert rejected.value.evaluation.outcome == "reject"
    assert rejected.value.evaluation.decision.findings[0].code == (
        "INSUFFICIENT_AVAILABLE_POSITION"
    )
    with pytest.raises(PaperOmsNotFoundError):
        store.get_order("account-one", "sell-too-much")
    assert store.get_account("account-one").ledger.revision == 2

    create_and_ack_order(
        store,
        order_id="sell-covered",
        side="sell",
        quantity="2",
        submit_minute=5,
        ack_minute=6,
    )
    covered = fill_command(
        order_id="sell-covered",
        external_fill_id="retryable-sell",
        order_revision=1,
        account_revision=2,
        quantity="2",
        fill_price="5",
        minutes=7,
        key="retryable-sell-command",
    )
    sold = store.record_fill(covered)
    assert sold.account.ledger.revision == 3
    assert sold.order.state.status == "filled"
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM paper_oms_order_events WHERE event_type = 'fill'"
            ).fetchone()[0]
            == 2
        )


def test_fill_transaction_rolls_back_after_order_projection_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "injected-rollback.db"
    store = oms_store(database, clock=lambda: at(100))
    store.create_account(account_command())
    create_and_ack_order(
        store,
        order_id="buy",
        side="buy",
        quantity="1",
        submit_minute=1,
        ack_minute=2,
    )
    command = fill_command(
        order_id="buy",
        external_fill_id="atomic-fill",
        order_revision=1,
        account_revision=1,
        quantity="1",
        fill_price="10",
        minutes=3,
    )

    def fail_account_update(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected account projection failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            PaperOmsStore,
            "_update_account_projection",
            staticmethod(fail_account_update),
        )
        with pytest.raises(RuntimeError, match="injected"):
            store.record_fill(command)

    assert store.get_account("account-one").ledger.revision == 1
    assert store.get_order("account-one", "buy").state.revision == 1
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM paper_oms_order_events WHERE event_type = 'fill'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM paper_oms_commands WHERE command_kind = 'fill'"
            ).fetchone()[0]
            == 0
        )

    committed = store.record_fill(command)
    assert committed.account.ledger.revision == 2
    assert committed.order.state.status == "filled"


def test_restart_and_reads_fail_closed_on_projection_corruption(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corrupt-restart.db"
    store = oms_store(database, clock=lambda: at(100))
    store.create_account(account_command())
    create_and_ack_order(
        store,
        order_id="buy",
        side="buy",
        quantity="1",
        submit_minute=1,
        ack_minute=2,
    )
    store.record_fill(
        fill_command(
            order_id="buy",
            external_fill_id="fill-one",
            order_revision=1,
            account_revision=1,
            quantity="1",
            fill_price="10",
            minutes=3,
        )
    )
    recovered = oms_store(database, clock=lambda: at(101))
    assert recovered.get_account("account-one").ledger.cash == Decimal("990")
    assert recovered.get_order("account-one", "buy").state.status == "filled"

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE paper_oms_accounts
            SET ledger_state_payload = '{}'
            WHERE account_id = 'account-one'
            """
        )
    with pytest.raises(PaperOmsIntegrityError):
        recovered.get_account("account-one")
    with pytest.raises(PaperOmsIntegrityError):
        oms_store(database, clock=lambda: at(102))


def test_concurrent_and_stale_revision_commands_fail_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "concurrent.db"
    setup = oms_store(database, clock=lambda: at(100))
    setup.create_account(account_command())
    create_and_ack_order(
        setup,
        order_id="buy",
        side="buy",
        quantity="2",
        submit_minute=1,
        ack_minute=2,
    )
    first_store = oms_store(database, clock=lambda: at(100))
    second_store = oms_store(database, clock=lambda: at(100))
    barrier = Barrier(2)
    commands = (
        fill_command(
            order_id="buy",
            external_fill_id="concurrent-one",
            order_revision=1,
            account_revision=1,
            quantity="1",
            fill_price="10",
            minutes=3,
        ),
        fill_command(
            order_id="buy",
            external_fill_id="concurrent-two",
            order_revision=1,
            account_revision=1,
            quantity="1",
            fill_price="10",
            minutes=3,
        ),
    )

    def apply(index: int) -> str:
        barrier.wait()
        try:
            (first_store, second_store)[index].record_fill(commands[index])
        except PaperOmsRevisionError:
            return "stale"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(apply, (0, 1)))

    assert outcomes == ["committed", "stale"]
    final_account = setup.get_account("account-one")
    final_order = setup.get_order("account-one", "buy")
    assert final_account.ledger.revision == 2
    assert final_order.state.revision == 2
    assert final_order.state.filled_quantity == Decimal("1")

    stale = fill_command(
        order_id="buy",
        external_fill_id="definitely-stale",
        order_revision=1,
        account_revision=1,
        quantity="1",
        fill_price="10",
        minutes=4,
    )
    with pytest.raises(PaperOmsRevisionError):
        setup.record_fill(stale)
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM paper_oms_order_events WHERE event_type = 'fill'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM paper_oms_commands WHERE command_kind = 'fill'"
            ).fetchone()[0]
            == 1
        )


def test_order_risk_allow_is_durable_readable_and_exactly_replayable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "risk-allow.db"
    command = submit_command(
        order_id="risk-allow",
        side="buy",
        quantity="2",
        minutes=1,
    )
    store = oms_store(database, clock=lambda: at(100))
    store.create_account(account_command())

    created = store.submit_order(command)
    evaluation = created.order.risk_evaluation
    assert evaluation is not None
    assert evaluation.outcome == "allow"
    assert evaluation.decision == evaluate_order_risk(evaluation.request)
    assert evaluation.request.request_hash == evaluation.decision.request.request_hash
    assert len(evaluation.request.request_hash) == 64
    assert len(evaluation.decision.decision_hash) == 64

    replayed = store.submit_order(command)
    assert replayed.idempotent_replay is True
    assert replayed.order.risk_evaluation == evaluation
    assert (
        store.get_order_risk_evaluation(
            command_namespace=command.command_namespace,
            idempotency_key=command.idempotency_key,
            account_id=command.account_id,
        )
        == evaluation
    )
    restarted = oms_store(database, clock=lambda: at(101))
    assert restarted.get_order("account-one", "risk-allow").risk_evaluation == evaluation

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT schema_version FROM paper_oms_meta WHERE singleton = 1"
        ).fetchone() == (3,)
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_oms_order_risk_evaluations"
        ).fetchone() == (1,)


def test_order_risk_rejection_is_durable_idempotent_and_payload_bound(
    tmp_path: Path,
) -> None:
    database = tmp_path / "risk-reject.db"
    store = oms_store(database, clock=lambda: at(100))
    store.create_account(account_command(cash="5"))
    command = submit_command(
        order_id="too-expensive",
        side="buy",
        quantity="1",
        minutes=1,
        key="stable-risk-rejection",
    )

    with pytest.raises(PaperOmsRiskRejectedError) as first:
        store.submit_order(command)
    with pytest.raises(PaperOmsRiskRejectedError) as second:
        store.submit_order(command)

    assert first.value.idempotent_replay is False
    assert second.value.idempotent_replay is True
    assert second.value.evaluation == first.value.evaluation
    assert {finding.code for finding in first.value.evaluation.decision.findings} >= {
        "INSUFFICIENT_AVAILABLE_CASH",
    }
    with pytest.raises(PaperOmsNotFoundError):
        store.get_order("account-one", "too-expensive")

    changed = submit_command(
        order_id="too-expensive",
        side="buy",
        quantity="2",
        minutes=1,
        key="stable-risk-rejection",
    )
    with pytest.raises(PaperOmsConflictError, match="different payload"):
        store.submit_order(changed)
    restarted = oms_store(database, clock=lambda: at(101))
    persisted = restarted.get_order_risk_evaluation(
        command_namespace=command.command_namespace,
        idempotency_key=command.idempotency_key,
        account_id=command.account_id,
    )
    assert persisted == first.value.evaluation


def test_stale_price_and_freshly_engaged_kill_switch_reject_without_order(
    tmp_path: Path,
) -> None:
    rules = build_order_rule_set(
        OrderRiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_resulting_position=Decimal("100"),
            maximum_active_orders=10,
            maximum_price_age_seconds=5,
            maximum_kill_switch_age_seconds=5,
        ),
        rule_set_id="paper-oms-freshness-tests",
    )

    def clock() -> datetime:
        return at(100)

    stale_database = tmp_path / "stale-price.db"
    stale_authority = ServerOwnedPaperKillSwitch(
        stale_database,
        clock=clock,
        source="paper-oms-freshness-tests",
    )
    stale_authority.clear()
    stale_store = PaperOmsStore(
        stale_database,
        clock=clock,
        order_risk_rule_set=rules,
        kill_switch_reader=stale_authority.read,
        price_evidence_reader=lambda command, currency: _test_market_evidence(
            command,
            currency,
            reference_price=Decimal("10"),
            observed_at=at(90),
            source="stale-price-test",
        ),
        allow_historical_client_timestamps=True,
    )
    stale_store.create_account(account_command())
    stale_command = submit_command(
        order_id="stale-price",
        side="buy",
        quantity="1",
        minutes=1,
    )
    with pytest.raises(PaperOmsRiskRejectedError) as stale:
        stale_store.submit_order(stale_command)
    assert "REFERENCE_PRICE_STALE" in {
        finding.code for finding in stale.value.evaluation.decision.findings
    }
    with pytest.raises(PaperOmsNotFoundError):
        stale_store.get_order("account-one", "stale-price")

    engaged_database = tmp_path / "engaged.db"
    engaged_authority = ServerOwnedPaperKillSwitch(
        engaged_database,
        clock=clock,
        source="paper-oms-engaged-tests",
    )
    engaged_authority.engage(reason_code="operator_halt")
    engaged_store = PaperOmsStore(
        engaged_database,
        clock=clock,
        order_risk_rule_set=rules,
        kill_switch_reader=engaged_authority.read,
        price_evidence_reader=lambda command, currency: _test_market_evidence(
            command,
            currency,
            reference_price=Decimal("10"),
            observed_at=clock(),
            source="engaged-test",
        ),
        allow_historical_client_timestamps=True,
    )
    engaged_store.create_account(account_command())
    engaged_command = submit_command(
        order_id="engaged",
        side="buy",
        quantity="1",
        minutes=1,
    )
    with pytest.raises(PaperOmsRiskRejectedError) as engaged:
        engaged_store.submit_order(engaged_command)
    assert "KILL_SWITCH_ENGAGED" in {
        finding.code for finding in engaged.value.evaluation.decision.findings
    }
    with pytest.raises(PaperOmsNotFoundError):
        engaged_store.get_order("account-one", "engaged")


def test_fill_rechecks_server_kill_switch_and_approved_price_envelope(
    tmp_path: Path,
) -> None:
    database = tmp_path / "fill-envelope.db"

    def clock() -> datetime:
        return at(100)

    store = oms_store(database, clock=clock)
    authority = ServerOwnedPaperKillSwitch(
        database,
        clock=clock,
        source="paper-oms-tests",
    )
    store.create_account(account_command())
    create_and_ack_order(
        store,
        order_id="fill-envelope",
        side="buy",
        quantity="1",
        submit_minute=1,
        ack_minute=2,
    )
    too_expensive = fill_command(
        order_id="fill-envelope",
        external_fill_id="too-expensive",
        order_revision=1,
        account_revision=1,
        quantity="1",
        fill_price="10.01",
        minutes=3,
    )
    with pytest.raises(PaperOmsTransitionError, match="risk-price"):
        store.record_fill(too_expensive)

    authority.engage(reason_code="operator_halt")
    blocked = fill_command(
        order_id="fill-envelope",
        external_fill_id="blocked",
        order_revision=1,
        account_revision=1,
        quantity="1",
        fill_price="10",
        minutes=3,
    )
    with pytest.raises(PaperOmsTransitionError, match="kill switch"):
        store.record_fill(blocked)
    assert store.get_order("account-one", "fill-envelope").state.revision == 1


def test_rehashed_account_state_tampering_fails_restart_replay(
    tmp_path: Path,
) -> None:
    database = tmp_path / "risk-state-tamper.db"
    store = oms_store(database, clock=lambda: at(100))
    store.create_account(account_command())
    command = submit_command(
        order_id="tamper-me",
        side="buy",
        quantity="1",
        minutes=1,
    )
    created = store.submit_order(command)
    evaluation = created.order.risk_evaluation
    assert evaluation is not None
    request = evaluation.request
    tampered_state = OrderRiskState.model_validate(
        {
            **request.state.model_dump(mode="python"),
            "account_state_hash": "f" * 64,
        }
    )
    tampered_request = build_order_risk_request(
        evaluation_id=request.evaluation_id,
        evaluated_at=request.evaluated_at,
        source_calculation_version=request.source_calculation_version,
        rule_set=request.rule_set,
        kill_switch=request.kill_switch,
        kill_switch_observed_at=request.kill_switch_observed_at,
        kill_switch_available_at=request.kill_switch_available_at,
        intent=request.intent,
        state=tampered_state,
        price_evidence=request.price_evidence,
    )
    tampered_decision: RiskDecision = evaluate_order_risk(tampered_request)

    def canonical(model: object) -> str:
        assert hasattr(model, "model_dump")
        return json.dumps(
            model.model_dump(mode="json"),  # type: ignore[union-attr]
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE paper_oms_order_risk_evaluations
            SET
                risk_request_payload = ?,
                risk_request_hash = ?,
                risk_decision_payload = ?,
                risk_decision_hash = ?
            WHERE evaluation_id = ?
            """,
            (
                canonical(tampered_request),
                tampered_request.request_hash,
                canonical(tampered_decision),
                tampered_decision.decision_hash,
                evaluation.evaluation_id,
            ),
        )
    with pytest.raises(PaperOmsIntegrityError, match="account state"):
        oms_store(database, clock=lambda: at(101))


def test_schema_v1_legacy_active_order_migrates_but_blocks_new_submit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-active.db"
    store = oms_store(database, clock=lambda: at(100))
    store.create_account(account_command())
    store.submit_order(
        submit_command(
            order_id="legacy-active",
            side="buy",
            quantity="1",
            minutes=1,
        )
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM paper_oms_order_risk_evaluations")
        connection.execute("UPDATE paper_oms_meta SET schema_version = 1 WHERE singleton = 1")

    migrated = oms_store(database, clock=lambda: at(101))
    legacy = migrated.get_order("account-one", "legacy-active")
    assert legacy.risk_evaluation is None
    with pytest.raises(PaperOmsRiskUnavailableError, match="active legacy"):
        migrated.submit_order(
            submit_command(
                order_id="blocked-by-legacy",
                side="buy",
                quantity="1",
                minutes=2,
            )
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT schema_version FROM paper_oms_meta WHERE singleton = 1"
        ).fetchone() == (3,)
        assert connection.execute("SELECT COUNT(*) FROM paper_oms_orders").fetchone() == (1,)


def test_concurrent_same_submit_creates_one_order_and_one_risk_receipt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "concurrent-submit.db"
    setup = oms_store(database, clock=lambda: at(100))
    setup.create_account(account_command())
    first = oms_store(database, clock=lambda: at(100))
    second = oms_store(database, clock=lambda: at(100))
    barrier = Barrier(2)
    command = submit_command(
        order_id="one-submit",
        side="buy",
        quantity="1",
        minutes=1,
    )

    def submit(index: int) -> bool:
        barrier.wait()
        result = (first, second)[index].submit_order(command)
        return result.idempotent_replay

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(submit, (0, 1)))
    assert outcomes == [False, True]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_oms_orders").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_oms_order_risk_evaluations"
        ).fetchone() == (1,)


def test_strict_server_timeline_rejects_backfilled_and_future_events(
    tmp_path: Path,
) -> None:
    store = oms_store(
        tmp_path / "strict-time.db",
        clock=lambda: at(100),
        allow_historical_client_timestamps=False,
    )
    store.create_account(account_command())
    store.submit_order(
        submit_command(
            order_id="strict-time",
            side="buy",
            quantity="1",
            minutes=1,
        )
    )

    with pytest.raises(PaperOmsConflictError, match="predates"):
        store.record_order_event(
            lifecycle_command(
                order_id="strict-time",
                revision=0,
                minutes=2,
            )
        )
    with pytest.raises(PaperOmsConflictError, match="clock tolerance"):
        store.record_order_event(
            lifecycle_command(
                order_id="strict-time",
                revision=0,
                minutes=101,
            )
        )

    accepted = store.record_order_event(
        lifecycle_command(
            order_id="strict-time",
            revision=0,
            minutes=100,
        )
    )
    assert accepted.event.occurred_at == at(100)
    assert accepted.event.received_at == at(100)
    assert accepted.event.committed_at == at(100)
    assert accepted.order.committed_at == at(100)
    assert accepted.order.updated_committed_at == at(100)
    with pytest.raises(PaperOmsConflictError, match="predates"):
        store.record_fill(
            fill_command(
                order_id="strict-time",
                external_fill_id="backfilled-fill",
                order_revision=1,
                account_revision=1,
                quantity="1",
                fill_price="10",
                minutes=99,
            )
        )
    with pytest.raises(PaperOmsConflictError, match="clock tolerance"):
        store.record_fill(
            fill_command(
                order_id="strict-time",
                external_fill_id="future-fill",
                order_revision=1,
                account_revision=1,
                quantity="1",
                fill_price="10",
                minutes=101,
            )
        )


def test_sell_fill_uses_directional_lower_envelope_and_rejects_extreme_price(
    tmp_path: Path,
) -> None:
    store = oms_store(tmp_path / "sell-envelope.db", clock=lambda: at(100))
    store.create_account(account_command())
    create_and_ack_order(
        store,
        order_id="seed-position",
        side="buy",
        quantity="2",
        submit_minute=1,
        ack_minute=2,
    )
    store.record_fill(
        fill_command(
            order_id="seed-position",
            external_fill_id="seed-position-fill",
            order_revision=1,
            account_revision=1,
            quantity="2",
            fill_price="10",
            minutes=3,
        )
    )
    create_and_ack_order(
        store,
        order_id="sell-envelope",
        side="sell",
        quantity="1",
        submit_minute=4,
        ack_minute=5,
    )
    with pytest.raises(PaperOmsTransitionError, match="below"):
        store.record_fill(
            fill_command(
                order_id="sell-envelope",
                external_fill_id="extreme-sell",
                order_revision=1,
                account_revision=2,
                quantity="1",
                fill_price="0.01",
                minutes=6,
            )
        )


def test_raw_store_rejects_fill_without_server_execution_evidence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "no-fill-evidence.db"
    authority = ServerOwnedPaperKillSwitch(
        database,
        clock=lambda: at(100),
        source="no-fill-evidence",
    )
    authority.clear()
    store = PaperOmsStore(
        database,
        clock=lambda: at(100),
        order_risk_rule_set=_test_order_rules(),
        kill_switch_reader=authority.read,
        price_evidence_reader=lambda command, currency: _test_market_evidence(
            command,
            currency,
            reference_price=Decimal("10"),
            observed_at=at(100),
        ),
        allow_historical_client_timestamps=True,
    )
    store.create_account(account_command())
    create_and_ack_order(
        store,
        order_id="server-evidence-only",
        side="buy",
        quantity="1",
        submit_minute=1,
        ack_minute=2,
    )
    with pytest.raises(PaperOmsExecutionUnavailableError, match="required"):
        store.record_fill(
            fill_command(
                order_id="server-evidence-only",
                external_fill_id="untrusted-client-fill",
                order_revision=1,
                account_revision=1,
                quantity="1",
                fill_price="10",
                minutes=3,
            )
        )


def test_reservation_prefix_replay_rejects_resigned_risk_and_state_tampering(
    tmp_path: Path,
) -> None:
    database = tmp_path / "reservation-prefix-tamper.db"
    store = oms_store(database, clock=lambda: at(100))
    store.create_account(account_command())
    store.submit_order(
        submit_command(
            order_id="first-active",
            side="buy",
            quantity="10",
            minutes=1,
        )
    )
    second_command = submit_command(
        order_id="second-active",
        side="buy",
        quantity="5",
        minutes=2,
    )
    second = store.submit_order(second_command)
    evaluation = second.order.risk_evaluation
    assert evaluation is not None
    reservation = evaluation.reservation_state
    assert reservation is not None
    assert reservation.active_order_count == 1

    tampered_state = evaluation.request.state.model_copy(
        update={
            "reserved_buy_cash": Decimal(0),
            "reserved_buy_quantity": Decimal(0),
            "reserved_sell_quantity": Decimal(0),
            "active_order_count": 0,
        }
    )
    tampered_request = build_order_risk_request(
        evaluation_id=evaluation.request.evaluation_id,
        evaluated_at=evaluation.request.evaluated_at,
        source_calculation_version=evaluation.request.source_calculation_version,
        rule_set=evaluation.request.rule_set,
        kill_switch=evaluation.request.kill_switch,
        kill_switch_observed_at=evaluation.request.kill_switch_observed_at,
        kill_switch_available_at=evaluation.request.kill_switch_available_at,
        intent=evaluation.request.intent,
        state=tampered_state,
        price_evidence=evaluation.request.price_evidence,
    )
    tampered_decision = evaluate_order_risk(tampered_request)
    reservation_payload = {
        "schema_version": 1,
        "account_id_hash": reservation.account_id_hash,
        "target_symbol": reservation.target_symbol,
        "event_horizon_id": reservation.event_horizon_id,
        "prior_evaluation_horizon_id": reservation.prior_evaluation_horizon_id,
        "active_orders": (),
        "reserved_buy_cash": Decimal(0),
        "reserved_buy_quantity": Decimal(0),
        "reserved_sell_quantity": Decimal(0),
        "active_order_count": 0,
    }
    tampered_reservation = PaperOmsReservationStateEvidence(
        **reservation_payload,
        evidence_hash=canonical_payload_hash(reservation_payload),
    )

    def canonical(model: object) -> str:
        assert hasattr(model, "model_dump")
        return json.dumps(
            model.model_dump(mode="json"),  # type: ignore[union-attr]
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE paper_oms_order_risk_evaluations
            SET risk_request_payload = ?, risk_request_hash = ?,
                risk_decision_payload = ?, risk_decision_hash = ?,
                reservation_state_payload = ?, reservation_state_hash = ?
            WHERE evaluation_id = ?
            """,
            (
                canonical(tampered_request),
                tampered_request.request_hash,
                canonical(tampered_decision),
                tampered_decision.decision_hash,
                canonical(tampered_reservation),
                tampered_reservation.evidence_hash,
                evaluation.evaluation_id,
            ),
        )
    with pytest.raises(PaperOmsIntegrityError, match="event-prefix"):
        oms_store(database, clock=lambda: at(101))


def test_sqlite_kill_switch_is_fail_closed_persistent_and_monotonic(
    tmp_path: Path,
) -> None:
    database = tmp_path / "persistent-kill.db"
    authority = ServerOwnedPaperKillSwitch(
        database,
        clock=lambda: at(100),
        source="persistent-kill-test",
    )
    initial = authority.read()
    assert initial.snapshot.status == "engaged"
    assert initial.snapshot.revision == 1
    cleared = authority.clear()
    assert cleared.snapshot.status == "clear"
    assert cleared.snapshot.revision == 2

    restarted = ServerOwnedPaperKillSwitch(
        database,
        clock=lambda: at(101),
        source="persistent-kill-test",
    )
    assert restarted.read().snapshot.status == "clear"
    assert restarted.read().snapshot.revision == 2
    engaged = restarted.engage(reason_code="OPERATOR_HALT")
    assert engaged.snapshot.revision == 3
    assert authority.read().snapshot.revision == 3
    assert authority.read().snapshot.status == "engaged"

    second_restart = ServerOwnedPaperKillSwitch(
        database,
        clock=lambda: at(102),
        source="persistent-kill-test",
    )
    assert second_restart.read().snapshot.status == "engaged"
    assert second_restart.read().snapshot.revision == 3
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE paper_oms_kill_switch SET state_hash = ? WHERE singleton = 1",
            ("0" * 64,),
        )
    with pytest.raises(PaperOmsIntegrityError, match="state evidence"):
        second_restart.read()
    with pytest.raises(PaperOmsIntegrityError, match="state evidence"):
        ServerOwnedPaperKillSwitch(
            database,
            clock=lambda: at(103),
            source="persistent-kill-test",
        )

    missing_database = tmp_path / "missing-kill.db"
    missing = ServerOwnedPaperKillSwitch(
        missing_database,
        clock=lambda: at(100),
        source="missing-kill-test",
    )
    with sqlite3.connect(missing_database) as connection:
        connection.execute("DELETE FROM paper_oms_kill_switch")
    with pytest.raises(PaperOmsIntegrityError, match="missing or ambiguous"):
        missing.read()
    with pytest.raises(PaperOmsIntegrityError, match="missing or ambiguous"):
        ServerOwnedPaperKillSwitch(
            missing_database,
            clock=lambda: at(101),
            source="missing-kill-test",
        )


def test_kill_switch_authority_is_consistent_across_os_processes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cross-process-kill.db"
    authority = ServerOwnedPaperKillSwitch(
        database,
        clock=lambda: at(100),
        source="parent-process",
    )
    cleared = authority.clear()
    assert cleared.snapshot.revision == 2
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from datetime import UTC, datetime\n"
                "from quantsieve_api.paper_oms import ServerOwnedPaperKillSwitch\n"
                "authority = ServerOwnedPaperKillSwitch(\n"
                "    sys.argv[1],\n"
                "    clock=lambda: datetime(2026, 7, 29, 1, 41, tzinfo=UTC),\n"
                "    source='parent-process',\n"
                ")\n"
                "result = authority.engage_if_revision(\n"
                "    reason_code='CROSS_PROCESS_HALT', expected_revision=2\n"
                ")\n"
                "print(result.snapshot.revision)\n"
            ),
            str(database),
        ],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip().endswith("3")
    observed = authority.read()
    assert observed.snapshot.status == "engaged"
    assert observed.snapshot.revision == 3
    assert observed.snapshot.reason_code == "CROSS_PROCESS_HALT"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def _create_evidence_generation_fixture(database: Path) -> None:
    store = oms_store(database, clock=lambda: at(100))
    store.create_account(account_command())
    create_and_ack_order(
        store,
        order_id="evidence-order",
        side="buy",
        quantity="2",
        submit_minute=1,
        ack_minute=2,
    )
    store.record_fill(
        fill_command(
            order_id="evidence-order",
            external_fill_id="evidence-fill",
            order_revision=1,
            account_revision=1,
            quantity="0.5",
            fill_price="10",
            minutes=3,
        )
    )


def test_v3_evidence_generation_is_complete_immutable_and_not_forgeable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "evidence-generation.db"
    _create_evidence_generation_fixture(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT evidence_generation FROM paper_oms_order_risk_evaluations"
        ).fetchone() == (1,)
        assert connection.execute(
            """
            SELECT fill_evidence_generation
            FROM paper_oms_order_events
            WHERE event_type = 'fill'
            """
        ).fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE paper_oms_order_risk_evaluations SET evidence_generation = 0"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE paper_oms_order_events
                SET fill_evidence_generation = 0
                WHERE event_type = 'fill'
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="generation is required"):
            connection.execute(
                """
                INSERT INTO paper_oms_order_risk_evaluations (
                    evaluation_id, origin_namespace, origin_idempotency_key,
                    command_payload, command_hash, account_id, order_id,
                    outcome, risk_request_payload, risk_request_hash,
                    risk_decision_payload, risk_decision_hash,
                    market_evidence_payload, market_evidence_hash,
                    reservation_state_payload, reservation_state_hash,
                    evidence_generation, recorded_at
                )
                SELECT evaluation_id + 100, origin_namespace,
                       origin_idempotency_key || '-forged',
                       command_payload, command_hash, account_id, order_id,
                       outcome, risk_request_payload, risk_request_hash,
                       risk_decision_payload, risk_decision_hash,
                       NULL, NULL, NULL, NULL, 0, recorded_at
                FROM paper_oms_order_risk_evaluations
                LIMIT 1
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="generation is required"):
            connection.execute(
                """
                INSERT INTO paper_oms_order_events (
                    event_id, account_id, order_id, order_revision,
                    account_revision, origin_namespace, origin_idempotency_key,
                    event_type, order_event_payload, order_event_hash,
                    ledger_event_payload, ledger_event_hash, execution_source,
                    external_fill_id, fill_economic_hash,
                    fill_execution_evidence_payload,
                    fill_execution_evidence_hash, fill_evidence_generation,
                    occurred_at, received_at, committed_at
                )
                SELECT event_id + 100, account_id, order_id, order_revision + 100,
                       account_revision + 100, origin_namespace,
                       origin_idempotency_key || '-forged',
                       event_type, order_event_payload, order_event_hash,
                       ledger_event_payload, ledger_event_hash, execution_source,
                       external_fill_id || '-forged', fill_economic_hash,
                       NULL, NULL, 0, occurred_at, received_at, committed_at
                FROM paper_oms_order_events
                WHERE event_type = 'fill'
                LIMIT 1
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """
                UPDATE paper_oms_order_risk_evaluations
                SET market_evidence_payload = NULL,
                    market_evidence_hash = NULL,
                    reservation_state_payload = NULL,
                    reservation_state_hash = NULL
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """
                UPDATE paper_oms_order_events
                SET fill_execution_evidence_payload = NULL,
                    fill_execution_evidence_hash = NULL
                WHERE event_type = 'fill'
                """
            )


@pytest.mark.parametrize(
    ("database_name", "tamper_sql", "error_pattern"),
    (
        (
            "risk-pair-null.db",
            """
            UPDATE paper_oms_order_risk_evaluations
            SET market_evidence_payload = NULL,
                market_evidence_hash = NULL,
                reservation_state_payload = NULL,
                reservation_state_hash = NULL
            """,
            "integrity checks",
        ),
        (
            "fill-pair-null.db",
            """
            UPDATE paper_oms_order_events
            SET fill_execution_evidence_payload = NULL,
                fill_execution_evidence_hash = NULL
            WHERE event_type = 'fill'
            """,
            "integrity checks",
        ),
    ),
)
def test_pair_null_evidence_tamper_fails_closed_on_restart(
    tmp_path: Path,
    database_name: str,
    tamper_sql: str,
    error_pattern: str,
) -> None:
    database = tmp_path / database_name
    _create_evidence_generation_fixture(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(tamper_sql)
        connection.commit()
    with pytest.raises(PaperOmsIntegrityError, match=error_pattern):
        oms_store(database, clock=lambda: at(101))


def _create_legacy_database(
    path: Path,
    *,
    schema_version: int,
    include_v2_risk: bool,
) -> None:
    source_path = path.with_name(f"{path.stem}-v3-source.db")
    source = oms_store(source_path, clock=lambda: at(100))
    source.create_account(account_command())
    create_and_ack_order(
        source,
        order_id="migrated-order",
        side="buy",
        quantity="2",
        submit_minute=1,
        ack_minute=2,
    )
    source.record_fill(
        fill_command(
            order_id="migrated-order",
            external_fill_id="migrated-fill",
            order_revision=1,
            account_revision=1,
            quantity="0.5",
            fill_price="10",
            minutes=3,
        )
    )
    risk_ddl = (
        """
        CREATE TABLE paper_oms_order_risk_evaluations (
            evaluation_id INTEGER PRIMARY KEY,
            origin_namespace TEXT NOT NULL,
            origin_idempotency_key TEXT NOT NULL,
            command_payload TEXT NOT NULL,
            command_hash TEXT NOT NULL,
            account_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            risk_request_payload TEXT NOT NULL,
            risk_request_hash TEXT NOT NULL,
            risk_decision_payload TEXT NOT NULL,
            risk_decision_hash TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            UNIQUE(origin_namespace, origin_idempotency_key),
            FOREIGN KEY(account_id)
                REFERENCES paper_oms_accounts(account_id)
                ON DELETE RESTRICT
        ) STRICT;
        CREATE UNIQUE INDEX idx_paper_oms_allowed_order_risk
        ON paper_oms_order_risk_evaluations(account_id, order_id)
        WHERE outcome = 'allow';
        """
        if include_v2_risk
        else ""
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(
            f"""
            CREATE TABLE paper_oms_meta (
                singleton INTEGER PRIMARY KEY,
                schema_version INTEGER NOT NULL
            ) STRICT;
            INSERT INTO paper_oms_meta VALUES (1, {schema_version});
            CREATE TABLE paper_oms_accounts (
                account_id TEXT PRIMARY KEY,
                currency TEXT NOT NULL,
                ledger_revision INTEGER NOT NULL,
                opening_event_payload TEXT NOT NULL,
                opening_event_hash TEXT NOT NULL,
                ledger_state_payload TEXT NOT NULL,
                ledger_state_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            ) STRICT;
            CREATE TABLE paper_oms_orders (
                account_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                execution_source TEXT NOT NULL,
                requested_quantity TEXT NOT NULL,
                order_revision INTEGER NOT NULL,
                order_state_payload TEXT NOT NULL,
                order_state_hash TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(account_id, order_id),
                FOREIGN KEY(account_id)
                    REFERENCES paper_oms_accounts(account_id)
                    ON DELETE RESTRICT
            ) STRICT;
            CREATE TABLE paper_oms_order_events (
                event_id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                order_revision INTEGER NOT NULL,
                account_revision INTEGER,
                origin_namespace TEXT NOT NULL,
                origin_idempotency_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                order_event_payload TEXT NOT NULL,
                order_event_hash TEXT NOT NULL,
                ledger_event_payload TEXT,
                ledger_event_hash TEXT,
                execution_source TEXT,
                external_fill_id TEXT,
                fill_economic_hash TEXT,
                committed_at TEXT NOT NULL,
                UNIQUE(account_id, order_id, order_revision),
                UNIQUE(origin_namespace, origin_idempotency_key),
                FOREIGN KEY(account_id, order_id)
                    REFERENCES paper_oms_orders(account_id, order_id)
                    ON DELETE RESTRICT
            ) STRICT;
            CREATE UNIQUE INDEX idx_paper_oms_fill_identity
            ON paper_oms_order_events(execution_source, external_fill_id)
            WHERE external_fill_id IS NOT NULL;
            CREATE UNIQUE INDEX idx_paper_oms_account_revision
            ON paper_oms_order_events(account_id, account_revision)
            WHERE account_revision IS NOT NULL;
            CREATE INDEX idx_paper_oms_order_event_log
            ON paper_oms_order_events(account_id, order_id, order_revision);
            CREATE TABLE paper_oms_commands (
                command_namespace TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                command_kind TEXT NOT NULL,
                command_payload TEXT NOT NULL,
                command_hash TEXT NOT NULL,
                account_id TEXT NOT NULL,
                order_id TEXT,
                result_order_revision INTEGER,
                result_account_revision INTEGER NOT NULL,
                result_event_id INTEGER,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY(command_namespace, idempotency_key),
                FOREIGN KEY(account_id)
                    REFERENCES paper_oms_accounts(account_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY(account_id, order_id)
                    REFERENCES paper_oms_orders(account_id, order_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY(result_event_id)
                    REFERENCES paper_oms_order_events(event_id)
                    ON DELETE RESTRICT
            ) STRICT;
            CREATE INDEX idx_paper_oms_commands_event
            ON paper_oms_commands(result_event_id);
            {risk_ddl}
            """
        )
        connection.execute("ATTACH DATABASE ? AS source", (str(source_path),))
        connection.execute(
            """
            INSERT INTO paper_oms_accounts
            SELECT account_id, currency, ledger_revision,
                   opening_event_payload, opening_event_hash,
                   ledger_state_payload, ledger_state_hash,
                   created_at, updated_at
            FROM source.paper_oms_accounts
            """
        )
        connection.execute(
            """
            INSERT INTO paper_oms_orders
            SELECT account_id, order_id, symbol, side, execution_source,
                   requested_quantity, order_revision, order_state_payload,
                   order_state_hash, submitted_at, updated_at
            FROM source.paper_oms_orders
            """
        )
        connection.execute(
            """
            INSERT INTO paper_oms_order_events
            SELECT event_id, account_id, order_id, order_revision,
                   account_revision, origin_namespace, origin_idempotency_key,
                   event_type, order_event_payload, order_event_hash,
                   ledger_event_payload, ledger_event_hash, execution_source,
                   external_fill_id, fill_economic_hash, committed_at
            FROM source.paper_oms_order_events
            """
        )
        connection.execute(
            """
            INSERT INTO paper_oms_commands
            SELECT command_namespace, idempotency_key, command_kind,
                   command_payload, command_hash, account_id, order_id,
                   result_order_revision, result_account_revision,
                   result_event_id, recorded_at
            FROM source.paper_oms_commands
            """
        )
        if include_v2_risk:
            connection.execute(
                """
                INSERT INTO paper_oms_order_risk_evaluations
                SELECT evaluation_id, origin_namespace, origin_idempotency_key,
                       command_payload, command_hash, account_id, order_id,
                       outcome, risk_request_payload, risk_request_hash,
                       risk_decision_payload, risk_decision_hash, recorded_at
                FROM source.paper_oms_order_risk_evaluations
                """
            )
        connection.commit()
        connection.execute("DETACH DATABASE source")


@pytest.mark.parametrize(
    ("schema_version", "include_v2_risk"),
    ((1, False), (2, True)),
)
def test_true_legacy_schema_migrates_to_v3(
    tmp_path: Path,
    schema_version: int,
    include_v2_risk: bool,
) -> None:
    database = tmp_path / f"true-v{schema_version}.db"
    _create_legacy_database(
        database,
        schema_version=schema_version,
        include_v2_risk=include_v2_risk,
    )
    with sqlite3.connect(database) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }
        assert ("paper_oms_order_risk_evaluations" in names) is include_v2_risk
        assert "market_evidence_payload" not in {
            row[1]
            for row in connection.execute("PRAGMA table_info(paper_oms_order_risk_evaluations)")
        }

    authority = ServerOwnedPaperKillSwitch(
        database,
        clock=lambda: at(100),
        source=f"true-v{schema_version}-migration",
    )
    authority.clear()
    migrated_store = PaperOmsStore(
        database,
        clock=lambda: at(100),
        order_risk_rule_set=_test_order_rules(),
        kill_switch_reader=authority.read,
    )
    migrated_account = migrated_store.get_account("account-one")
    migrated_order = migrated_store.get_order("account-one", "migrated-order")
    assert migrated_account.ledger.revision == 2
    assert migrated_order.state.revision == 2
    assert migrated_order.state.filled_quantity == Decimal("0.5")
    assert (migrated_order.risk_evaluation is not None) is include_v2_risk
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT schema_version FROM paper_oms_meta WHERE singleton = 1"
        ).fetchone() == (3,)
        risk_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(paper_oms_order_risk_evaluations)")
        }
        assert {
            "market_evidence_payload",
            "market_evidence_hash",
            "reservation_state_payload",
            "reservation_state_hash",
        } <= risk_columns
        event = connection.execute(
            """
            SELECT fill_evidence_generation,
                   fill_execution_evidence_payload,
                   fill_execution_evidence_hash
            FROM paper_oms_order_events
            WHERE event_type = 'fill'
            """
        ).fetchone()
        assert event == (0, None, None)
        if include_v2_risk:
            assert connection.execute(
                """
                SELECT evidence_generation,
                       market_evidence_payload,
                       market_evidence_hash,
                       reservation_state_payload,
                       reservation_state_hash
                FROM paper_oms_order_risk_evaluations
                """
            ).fetchone() == (0, None, None, None, None)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT COUNT(*) FROM paper_oms_accounts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM paper_oms_orders").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM paper_oms_order_events").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM paper_oms_commands").fetchone() == (4,)
        not_null_columns = {
            (table, row[1])
            for table in (
                "paper_oms_orders",
                "paper_oms_order_events",
                "paper_oms_commands",
                "paper_oms_order_risk_evaluations",
            )
            for row in connection.execute(f"PRAGMA table_info({table})")
            if row[3] == 1
        }
        assert {
            ("paper_oms_orders", "committed_at"),
            ("paper_oms_orders", "updated_committed_at"),
            ("paper_oms_order_events", "fill_evidence_generation"),
            ("paper_oms_order_events", "occurred_at"),
            ("paper_oms_order_events", "received_at"),
            ("paper_oms_commands", "received_at"),
            ("paper_oms_order_risk_evaluations", "evidence_generation"),
        } <= not_null_columns
        schema_names = {
            (row[0], row[1])
            for row in connection.execute(
                """
                SELECT type, name
                FROM sqlite_master
                WHERE name LIKE 'idx_paper_oms_%'
                   OR name LIKE 'trg_paper_oms_%'
                """
            )
        }
        assert {
            ("index", "idx_paper_oms_fill_identity"),
            ("index", "idx_paper_oms_account_revision"),
            ("index", "idx_paper_oms_order_event_log"),
            ("index", "idx_paper_oms_commands_event"),
            ("index", "idx_paper_oms_allowed_order_risk"),
            ("trigger", "trg_paper_oms_v3_risk_evidence_insert"),
            ("trigger", "trg_paper_oms_risk_generation_immutable"),
            ("trigger", "trg_paper_oms_v3_fill_evidence_insert"),
            ("trigger", "trg_paper_oms_fill_generation_immutable"),
        } <= schema_names


def test_invalid_legacy_row_rolls_back_entire_schema_upgrade(
    tmp_path: Path,
) -> None:
    database = tmp_path / "invalid-v1.db"
    _create_legacy_database(
        database,
        schema_version=1,
        include_v2_risk=False,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE paper_oms_orders SET side = 'hold'")
    authority = ServerOwnedPaperKillSwitch(
        database,
        clock=lambda: at(100),
        source="invalid-v1-migration",
    )
    authority.clear()
    with pytest.raises(PaperOmsIntegrityError, match="complete v3 schema"):
        PaperOmsStore(
            database,
            clock=lambda: at(100),
            order_risk_rule_set=_test_order_rules(),
            kill_switch_reader=authority.read,
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT schema_version FROM paper_oms_meta WHERE singleton = 1"
        ).fetchone() == (1,)
        assert "committed_at" not in {
            row[1] for row in connection.execute("PRAGMA table_info(paper_oms_orders)")
        }
        assert connection.execute("SELECT side FROM paper_oms_orders").fetchone() == ("hold",)
