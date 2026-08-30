from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TypeAlias

from quantsieve_api.paper_oms import (
    CreatePaperAccountCommand,
    PaperEventMutationResult,
    PaperOmsFillExecutionEvidence,
    PaperOmsOrderMarketEvidence,
    PaperOmsStore,
    PaperOrderRecord,
    RecordPaperFillCommand,
    RecordPaperOrderEventCommand,
    ServerOwnedPaperKillSwitch,
    SubmitPaperOrderCommand,
)
from quantsieve_engine.event_simulation import (
    AccountOpenedRecord,
    AtomicFillRecord,
    CancelOrderIntent,
    EventSimulationRun,
    OrderCancellationRecord,
    OrderEventRecord,
    OrderSubmittedRecord,
    SimulationBar,
    SimulationConfig,
    SimulationRecord,
    SubmitOrderIntent,
    replay_simulation_records,
    simulate_event_driven,
)
from quantsieve_engine.order_events import OrderFillEvent, OrderState
from quantsieve_engine.paper_ledger import PaperLedgerState
from quantsieve_engine.risk import (
    OrderPriceEvidence,
    OrderRiskLimits,
    build_kill_switch_snapshot,
    build_order_rule_set,
    canonical_payload_hash,
)
from quantsieve_providers import BinanceSpotTradingRules, ExecutionQuote

START = datetime(2026, 7, 30, tzinfo=UTC)
EXECUTION_SOURCE = "event-simulation:contract-test"

OrderEconomics: TypeAlias = tuple[
    str,
    Decimal,
    Decimal,
    Decimal,
    Decimal | None,
    str | None,
    int,
    tuple[str, ...],
]
LedgerEconomics: TypeAlias = tuple[object, ...]


def _bar(index: int, *, volume: str = "4") -> SimulationBar:
    opened_at = START + timedelta(minutes=index + 1)
    return SimulationBar.model_validate(
        {
            "index": index,
            "symbol": "BTCUSDT",
            "opened_at": opened_at,
            "closed_at": opened_at + timedelta(minutes=1),
            "open": "100",
            "high": "105",
            "low": "95",
            "close": "102",
            "volume": volume,
        }
    )


def _simulation_run() -> EventSimulationRun:
    bars = tuple(_bar(index) for index in range(4))
    rules = build_order_rule_set(
        OrderRiskLimits.model_validate(
            {
                "maximum_order_notional": "1000000",
                "maximum_resulting_position": "1000000",
                "maximum_active_orders": 100,
                "minimum_cash_reserve": "0",
                "maximum_price_age_seconds": 0,
                "maximum_kill_switch_age_seconds": 1_000_000_000,
                "market_order_price_buffer_ratio": "0",
                "order_fee_buffer_ratio": "0.001",
            }
        ),
        rule_set_id="paper-oms-contract",
    )
    config = SimulationConfig.model_validate(
        {
            "account_id": "contract-account",
            "symbol": "BTCUSDT",
            "quote_currency": "USDT",
            "initial_cash": "1000",
            "opened_at": START,
            "order_risk_rule_set": rules,
            "kill_switch": build_kill_switch_snapshot(source="paper-oms-contract"),
            "kill_switch_observed_at": START,
            "kill_switch_available_at": START,
            "volume_participation_rate": "0.25",
            "fee_rate": "0.001",
            "slippage_bps": "10",
        }
    )
    order = SubmitOrderIntent.model_validate(
        {
            "sequence": 1,
            "idempotency_key": "simulation-submit",
            "order_id": "partial-then-cancel",
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": "3",
            "created_at": bars[0].closed_at,
        }
    )
    cancellation = CancelOrderIntent(
        sequence=2,
        idempotency_key="simulation-cancel",
        order_id=order.order_id,
        created_at=bars[2].closed_at,
    )
    return simulate_event_driven(config, bars, (order, cancellation))


def _order_economics(state: OrderState) -> OrderEconomics:
    return (
        state.status,
        state.requested_quantity,
        state.filled_quantity,
        state.filled_notional,
        state.average_fill_price,
        state.terminal_reason,
        state.revision,
        tuple(receipt.event_type for receipt in state.event_receipts),
    )


def _ledger_economics(state: PaperLedgerState) -> LedgerEconomics:
    positions = tuple(
        (
            position.symbol,
            position.quantity,
            position.book_cost,
            tuple(
                (
                    lot.opened_revision,
                    lot.opened_at,
                    lot.unit_cost,
                    lot.original_quantity,
                    lot.remaining_quantity,
                    lot.original_cost,
                    lot.remaining_cost,
                )
                for lot in position.lots
            ),
        )
        for position in state.positions
    )
    transactions = tuple(
        (
            transaction.ledger_revision,
            transaction.source_kind,
            transaction.occurred_at,
            tuple(
                (posting.account, posting.currency, posting.amount)
                for posting in transaction.postings
            ),
        )
        for transaction in state.transactions
    )
    consumptions = tuple(
        (
            item.ledger_revision,
            item.sequence,
            item.symbol,
            item.quantity,
            item.cost_basis,
        )
        for item in state.lot_consumptions
    )
    return (
        state.account_id,
        state.currency,
        state.revision,
        state.initial_cash,
        state.cash,
        state.realized_pnl,
        state.fees_paid,
        positions,
        transactions,
        consumptions,
    )


def _create_oms(run: EventSimulationRun, database: Path) -> PaperOmsStore:
    def clock() -> datetime:
        return START + timedelta(days=1)

    authority = ServerOwnedPaperKillSwitch(
        database,
        clock=clock,
        source="event-simulation-contract",
    )
    authority.clear()

    def market_evidence(
        command: SubmitPaperOrderCommand,
        currency: str,
        *,
        price: Decimal,
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
            lot_max_quantity=Decimal("1000000"),
            market_step_size=Decimal("0.00000001"),
            market_min_quantity=Decimal("0.00000001"),
            market_max_quantity=Decimal("1000000"),
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
            bid_price=price,
            ask_price=price,
            bid_quantity=Decimal("1000000"),
            ask_quantity=Decimal("1000000"),
            notional_reference_price=price,
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
            reference_price=price,
            observed_at=observed_at,
            available_at=observed_at,
            source="event-simulation-contract",
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
        return PaperOmsOrderMarketEvidence.model_validate(
            {
                **payload,
                "evidence_hash": canonical_payload_hash(payload),
            }
        )

    def fill_evidence(
        command: RecordPaperFillCommand,
        order: PaperOrderRecord,
    ) -> PaperOmsFillExecutionEvidence:
        approval = order.risk_evaluation
        assert approval is not None and approval.market_evidence is not None
        submit = SubmitPaperOrderCommand(
            command_namespace="event-simulation-contract",
            idempotency_key=f"fill-evidence-{command.idempotency_key}",
            account_id=command.account_id,
            occurred_at=command.occurred_at,
            order_id=command.order_id,
            symbol=order.symbol,
            side=order.side,
            execution_source=command.execution_source,
            quantity=command.quantity,
        )
        market = market_evidence(
            submit,
            approval.market_evidence.trading_rules.quote_asset,
            price=command.reference_price,
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
            "reference_price": command.reference_price,
            "fill_price": command.fill_price,
            "fee_rate": run.config.fee_rate,
            "fee": command.fee,
            "observed_at": market.quote.observed_at,
            "available_at": market.quote.observed_at,
            "approval_evidence_hash": approval.market_evidence.evidence_hash,
            "trading_rules": market.trading_rules,
            "quote": market.quote,
            "rules_snapshot_hash": market.rules_snapshot_hash,
            "quote_snapshot_hash": market.quote_snapshot_hash,
        }
        return PaperOmsFillExecutionEvidence.model_validate(
            {
                **payload,
                "evidence_hash": canonical_payload_hash(payload),
            }
        )

    store = PaperOmsStore(
        database,
        clock=clock,
        order_risk_rule_set=run.config.order_risk_rule_set,
        kill_switch_reader=authority.read,
        price_evidence_reader=lambda command, currency: market_evidence(
            command,
            currency,
            price=Decimal("102"),
        ),
        fill_evidence_reader=fill_evidence,
        allow_historical_client_timestamps=True,
    )
    store.create_account(
        CreatePaperAccountCommand.model_validate(
            {
                "command_namespace": "event-simulation-contract",
                "idempotency_key": "create-account",
                "account_id": run.config.account_id,
                "currency": run.config.quote_currency,
                "initial_cash": run.config.initial_cash,
                "occurred_at": run.config.opened_at,
            }
        )
    )
    return store


def _assert_prefix_economics(
    run: EventSimulationRun,
    store: PaperOmsStore,
    record_index: int,
) -> None:
    replay = replay_simulation_records(
        run.config,
        run.records[: record_index + 1],
    )
    account = store.get_account(run.config.account_id)
    assert _ledger_economics(account.ledger) == _ledger_economics(replay.ledger_state)
    for projection in replay.orders:
        durable = store.get_order(
            run.config.account_id,
            projection.intent.order_id,
        )
        assert durable.symbol == projection.intent.symbol
        assert durable.side == projection.intent.side
        assert _order_economics(durable.state) == _order_economics(projection.state)


def _record_lifecycle_event(
    store: PaperOmsStore,
    run: EventSimulationRun,
    *,
    record_sequence: int,
    event_type: str,
    order_id: str,
    occurred_at: datetime,
    reason: str | None,
) -> PaperEventMutationResult:
    order = store.get_order(run.config.account_id, order_id)
    payload: dict[str, object] = {
        "command_namespace": "event-simulation-contract",
        "idempotency_key": f"record-{record_sequence}-{event_type}",
        "account_id": run.config.account_id,
        "order_id": order_id,
        "expected_order_revision": order.state.revision,
        "event_type": event_type,
        "occurred_at": occurred_at,
    }
    if reason is not None:
        payload["reason"] = reason
    return store.record_order_event(RecordPaperOrderEventCommand.model_validate(payload))


def _mirror_records(
    run: EventSimulationRun,
    store: PaperOmsStore,
) -> tuple[PaperEventMutationResult, ...]:
    mutations: list[PaperEventMutationResult] = []
    for record_index, record in enumerate(run.records):
        if isinstance(record, AccountOpenedRecord):
            _assert_prefix_economics(run, store, record_index)
            continue
        if isinstance(record, OrderSubmittedRecord):
            store.submit_order(
                SubmitPaperOrderCommand.model_validate(
                    {
                        "command_namespace": "event-simulation-contract",
                        "idempotency_key": record.intent.idempotency_key,
                        "account_id": run.config.account_id,
                        "order_id": record.intent.order_id,
                        "symbol": record.intent.symbol,
                        "side": record.intent.side,
                        "execution_source": EXECUTION_SOURCE,
                        "quantity": record.intent.quantity,
                        "occurred_at": record.intent.created_at,
                    }
                )
            )
            _assert_prefix_economics(run, store, record_index)
            continue
        if isinstance(record, OrderEventRecord):
            mutations.append(
                _record_lifecycle_event(
                    store,
                    run,
                    record_sequence=record.sequence,
                    event_type=record.order_event.event_type,
                    order_id=record.order_event.order_id,
                    occurred_at=record.occurred_at,
                    reason=getattr(record.order_event, "reason", None),
                )
            )
        elif isinstance(record, AtomicFillRecord):
            order = store.get_order(
                run.config.account_id,
                record.order_event.order_id,
            )
            account = store.get_account(run.config.account_id)
            mutations.append(
                store.record_fill(
                    RecordPaperFillCommand.model_validate(
                        {
                            "command_namespace": "event-simulation-contract",
                            "idempotency_key": f"record-{record.sequence}-fill",
                            "account_id": run.config.account_id,
                            "order_id": record.order_event.order_id,
                            "expected_order_revision": order.state.revision,
                            "expected_account_revision": account.ledger.revision,
                            "execution_source": EXECUTION_SOURCE,
                            "external_fill_id": record.order_event.fill_id,
                            "quantity": record.ledger_event.quantity,
                            "reference_price": record.ledger_event.reference_price,
                            "fill_price": record.ledger_event.fill_price,
                            "fee": record.ledger_event.fee,
                            "occurred_at": record.occurred_at,
                        }
                    )
                )
            )
        elif isinstance(record, OrderCancellationRecord):
            for event in (
                record.request_event,
                record.acknowledgement_event,
            ):
                mutations.append(
                    _record_lifecycle_event(
                        store,
                        run,
                        record_sequence=record.sequence,
                        event_type=event.event_type,
                        order_id=event.order_id,
                        occurred_at=record.occurred_at,
                        reason=None,
                    )
                )
        else:  # pragma: no cover - closed discriminated record union
            raise AssertionError(f"Unsupported simulation record at index {record_index}.")
        _assert_prefix_economics(run, store, record_index)
    return tuple(mutations)


def _simulation_event_types(records: Sequence[SimulationRecord]) -> tuple[str, ...]:
    event_types: list[str] = []
    for record in records:
        if isinstance(record, (OrderEventRecord, AtomicFillRecord)):
            event_types.append(record.order_event.event_type)
        elif isinstance(record, OrderCancellationRecord):
            event_types.extend(
                (
                    record.request_event.event_type,
                    record.acknowledgement_event.event_type,
                )
            )
    return tuple(event_types)


def test_durable_oms_matches_simulator_ack_fill_cancel_economics(
    tmp_path: Path,
) -> None:
    run = _simulation_run()
    database = tmp_path / "contract.db"
    store = _create_oms(run, database)
    mutations = _mirror_records(run, store)
    store = PaperOmsStore(database, clock=lambda: START + timedelta(days=2))
    durable_order = store.get_order(
        run.config.account_id,
        run.orders[0].intent.order_id,
    )
    durable_account = store.get_account(run.config.account_id)

    assert _simulation_event_types(run.records) == tuple(
        mutation.event.order_event.event_type for mutation in mutations
    )
    assert _order_economics(durable_order.state) == _order_economics(run.orders[0].state)
    assert _ledger_economics(durable_account.ledger) == _ledger_economics(run.ledger_state)
    assert durable_order.state.status == "cancelled"
    assert durable_order.state.filled_quantity == Decimal("2")
    assert durable_account.ledger.cash == Decimal("799.5998")
    assert durable_account.ledger.fees_paid == Decimal("0.2002")

    fill_mutations = [
        mutation for mutation in mutations if mutation.event.order_event.event_type == "fill"
    ]
    assert len(fill_mutations) == 2
    for mutation in fill_mutations:
        assert mutation.event.ledger_fill is not None
        order_fill = mutation.event.order_event
        assert isinstance(order_fill, OrderFillEvent)
        assert order_fill.fill_quantity == mutation.event.ledger_fill.quantity
        assert order_fill.fill_price == mutation.event.ledger_fill.fill_price
        assert mutation.event.account_revision == mutation.account.ledger.revision


def test_identity_envelopes_differ_but_replayable_economic_projection_is_equal(
    tmp_path: Path,
) -> None:
    """OMS namespaces command/fill ids, while the simulator uses receipt-local ids."""

    run = _simulation_run()
    store = _create_oms(run, tmp_path / "identity-envelope.db")
    mutations = _mirror_records(run, store)
    durable_order = store.get_order(
        run.config.account_id,
        run.orders[0].intent.order_id,
    )
    durable_ledger = store.get_account(run.config.account_id).ledger
    replay = replay_simulation_records(run.config, run.records)

    assert durable_order.state != replay.orders[0].state
    assert durable_ledger != replay.ledger_state
    assert _order_economics(durable_order.state) == _order_economics(replay.orders[0].state)
    assert _ledger_economics(durable_ledger) == _ledger_economics(replay.ledger_state)

    simulator_fill = next(record for record in run.records if isinstance(record, AtomicFillRecord))
    durable_fill = next(
        mutation.event for mutation in mutations if mutation.event.order_event.event_type == "fill"
    )
    assert durable_fill.ledger_fill is not None
    assert isinstance(durable_fill.order_event, OrderFillEvent)
    assert durable_fill.order_event.idempotency_key != (simulator_fill.order_event.idempotency_key)
    assert durable_fill.order_event.fill_id != simulator_fill.order_event.fill_id
    assert durable_fill.ledger_fill.event_hash != (simulator_fill.ledger_event.event_hash)
    assert (
        durable_fill.order_event.fill_quantity,
        durable_fill.order_event.fill_price,
        durable_fill.ledger_fill.fee,
    ) == (
        simulator_fill.order_event.fill_quantity,
        simulator_fill.order_event.fill_price,
        simulator_fill.ledger_event.fee,
    )
