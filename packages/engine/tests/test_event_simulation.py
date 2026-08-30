from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest
import quantsieve_engine.event_simulation as event_simulation_module
from pydantic import ValidationError
from quantsieve_engine.event_simulation import (
    AtomicFillRecord,
    CancelOrderIntent,
    EventSimulationRun,
    OrderEventRecord,
    OrderRiskRejectedRecord,
    OrderSubmittedRecord,
    SimulationBar,
    SimulationConfig,
    SimulationInputError,
    SimulationIntegrityError,
    SubmitOrderIntent,
    replay_simulation_records,
    simulate_event_driven,
    verify_event_simulation,
)
from quantsieve_engine.order_events import OrderAcknowledgedEvent, OrderFillEvent
from quantsieve_engine.paper_ledger import build_paper_fill_event
from quantsieve_engine.risk import (
    KillSwitchSnapshot,
    OrderPriceEvidence,
    OrderRiskLimits,
    OrderRiskRequest,
    OrderRiskRuleSet,
    build_kill_switch_snapshot,
    build_order_risk_request,
    build_order_rule_set,
    evaluate_order_risk,
)

START = datetime(2026, 7, 29, tzinfo=UTC)


def risk_rules(
    *,
    maximum_order_notional: str = "1000000000",
    maximum_resulting_position: str = "1000000000",
    maximum_active_orders: int = 100,
    minimum_cash_reserve: str = "0",
    maximum_price_age_seconds: int = 0,
    maximum_kill_switch_age_seconds: int = 1_000_000_000,
    market_order_price_buffer_ratio: str = "0",
    order_fee_buffer_ratio: str = "0",
    rule_set_version: str = "1.0.0",
) -> OrderRiskRuleSet:
    return build_order_rule_set(
        OrderRiskLimits.model_validate(
            {
                "maximum_order_notional": maximum_order_notional,
                "maximum_resulting_position": maximum_resulting_position,
                "maximum_active_orders": maximum_active_orders,
                "minimum_cash_reserve": minimum_cash_reserve,
                "maximum_price_age_seconds": maximum_price_age_seconds,
                "maximum_kill_switch_age_seconds": maximum_kill_switch_age_seconds,
                "market_order_price_buffer_ratio": market_order_price_buffer_ratio,
                "order_fee_buffer_ratio": order_fee_buffer_ratio,
            }
        ),
        rule_set_id="event-simulation-tests",
        rule_set_version=rule_set_version,
    )


def config(
    *,
    cash: str = "10000",
    latency_bars: int = 0,
    participation: str = "1",
    fee_rate: str = "0",
    slippage_bps: str = "0",
    rules: OrderRiskRuleSet | None = None,
    kill_switch: KillSwitchSnapshot | None = None,
) -> SimulationConfig:
    active_rules = rules or risk_rules(order_fee_buffer_ratio=fee_rate)
    active_switch = kill_switch or build_kill_switch_snapshot(source="event-simulation-tests")
    return SimulationConfig.model_validate(
        {
            "account_id": "paper-one",
            "symbol": "BTCUSDT",
            "quote_currency": "USDT",
            "initial_cash": cash,
            "opened_at": START,
            "order_risk_rule_set": active_rules,
            "kill_switch": active_switch,
            "kill_switch_observed_at": START,
            "kill_switch_available_at": START,
            "latency_bars": latency_bars,
            "volume_participation_rate": participation,
            "fee_rate": fee_rate,
            "slippage_bps": slippage_bps,
        }
    )


def bar(
    index: int,
    *,
    open_price: str = "100",
    high: str = "110",
    low: str = "90",
    close: str = "105",
    volume: str = "10",
) -> SimulationBar:
    opened_at = START + timedelta(minutes=index + 1)
    return SimulationBar.model_validate(
        {
            "index": index,
            "symbol": "BTCUSDT",
            "opened_at": opened_at,
            "closed_at": opened_at + timedelta(minutes=1),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def submit(
    *,
    sequence: int = 1,
    order_id: str = "order-one",
    created_at: datetime | None = None,
    side: str = "buy",
    order_type: str = "market",
    quantity: str = "1",
    limit_price: str | None = None,
    expires_after_bars: int | None = None,
) -> SubmitOrderIntent:
    values: dict[str, object] = {
        "sequence": sequence,
        "idempotency_key": f"submit-{order_id}",
        "order_id": order_id,
        "symbol": "BTCUSDT",
        "side": side,
        "order_type": order_type,
        "quantity": quantity,
        "created_at": created_at or bar(0).closed_at,
        "expires_after_bars": expires_after_bars,
    }
    if limit_price is not None:
        values["limit_price"] = limit_price
    return SubmitOrderIntent.model_validate(values)


def atomic_fills(run: EventSimulationRun) -> list[AtomicFillRecord]:
    return [record for record in run.records if isinstance(record, AtomicFillRecord)]


def order_events(run: EventSimulationRun) -> list[OrderEventRecord]:
    return [record for record in run.records if isinstance(record, OrderEventRecord)]


def risk_rejections(run: EventSimulationRun) -> list[OrderRiskRejectedRecord]:
    return [record for record in run.records if isinstance(record, OrderRiskRejectedRecord)]


def test_contracts_are_strict_content_addressed_and_revalidated() -> None:
    first = bar(0)
    equivalent = SimulationBar.model_validate(
        {
            "index": 0,
            "symbol": " btcusdt ",
            "opened_at": first.opened_at.astimezone(timezone(timedelta(hours=8))),
            "closed_at": first.closed_at,
            "open": Decimal("100.00"),
            "high": "110.0",
            "low": "90.000",
            "close": "105",
            "volume": "10.0",
        }
    )
    intent = submit()

    assert first == equivalent
    assert first.bar_hash == equivalent.bar_hash
    assert first.symbol == "BTCUSDT"
    assert intent.intent_hash == submit().intent_hash
    with pytest.raises(ValidationError, match="frozen"):
        first.open = Decimal("101")
    with pytest.raises(ValidationError, match="exact finite decimal"):
        SimulationBar.model_validate(
            {
                "index": 0,
                "symbol": "BTCUSDT",
                "opened_at": first.opened_at,
                "closed_at": first.closed_at,
                "open": 100.0,
                "high": "110",
                "low": "90",
                "close": "105",
                "volume": "10",
            }
        )

    configured = config()
    for required_policy_field in ("order_risk_rule_set", "kill_switch"):
        payload = configured.model_dump(
            mode="python",
            exclude={required_policy_field},
        )
        with pytest.raises(ValidationError, match=required_policy_field):
            SimulationConfig.model_validate(payload)
    with pytest.raises(ValidationError, match="timezone-aware"):
        SimulationBar.model_validate(
            {
                "index": 0,
                "symbol": "BTCUSDT",
                "opened_at": datetime(2026, 1, 1),
                "closed_at": datetime(2026, 1, 2),
                "open": "100",
                "high": "110",
                "low": "90",
                "close": "105",
                "volume": "10",
            }
        )
    forged = first.model_copy(update={"open": Decimal("101")})
    with pytest.raises(SimulationInputError, match="strict revalidation"):
        simulate_event_driven(config(), [forged], [])


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"high": "99"}, "high/low"),
        ({"low": "106"}, "high/low"),
        ({"volume": "-1"}, "volume"),
        ({"open_price": "0"}, "positive"),
    ],
)
def test_bar_economic_invariants_fail_closed(
    values: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        bar(0, **values)


def test_next_bar_execution_and_extra_latency_prevent_lookahead() -> None:
    bars = [
        bar(0, open_price="100"),
        bar(1, open_price="110", high="115", low="105", close="112"),
        bar(2, open_price="120", high="125", low="115", close="122"),
    ]

    buffered_rules = risk_rules(market_order_price_buffer_ratio="0.2")
    immediate = simulate_event_driven(
        config(rules=buffered_rules),
        bars,
        [submit(created_at=bars[0].closed_at)],
    )
    delayed = simulate_event_driven(
        config(latency_bars=1, rules=buffered_rules),
        bars,
        [submit(created_at=bars[0].closed_at)],
    )

    immediate_fill = atomic_fills(immediate)[0]
    delayed_fill = atomic_fills(delayed)[0]
    assert immediate_fill.bar_index == 1
    assert immediate_fill.ledger_event.reference_price == Decimal("110")
    assert delayed_fill.bar_index == 2
    assert delayed_fill.ledger_event.reference_price == Decimal("120")
    assert all(record.bar_index != 0 for record in atomic_fills(immediate))


def test_volume_cap_causes_deterministic_partial_fills_with_fees_and_slippage() -> None:
    bars = [bar(index, volume="4") for index in range(4)]
    run = simulate_event_driven(
        config(participation="0.5", fee_rate="0.001", slippage_bps="10"),
        bars,
        [submit(quantity="5")],
    )
    fills = atomic_fills(run)
    state = run.orders[0].state

    assert [record.order_event.fill_quantity for record in fills] == [
        Decimal("2"),
        Decimal("2"),
        Decimal("1"),
    ]
    assert all(record.order_event.fill_price == Decimal("100.1") for record in fills)
    assert all(
        record.order_event.fill_id == record.ledger_event.fill_id
        and record.order_event.payload_hash != record.ledger_event.event_hash
        for record in fills
    )
    assert state.status == "filled"
    assert state.average_fill_price == Decimal("100.1")
    assert run.ledger_state.fees_paid == Decimal("0.5005")
    assert run.ledger_state.cash == Decimal("9498.9995")
    assert run.ledger_state.positions[0].quantity == Decimal("5")
    assert len(run.ledger_state.transactions) == 1 + len(fills)


def test_limit_crossing_uses_gap_open_or_limit_and_respects_limit_price() -> None:
    bars = [
        bar(0),
        bar(1, open_price="105", high="108", low="101", close="103"),
        bar(2, open_price="104", high="106", low="99", close="100"),
    ]
    touched = simulate_event_driven(
        config(slippage_bps="50"),
        bars,
        [
            submit(
                order_type="limit",
                quantity="1",
                limit_price="100",
                created_at=bars[0].closed_at,
            )
        ],
    )

    assert len(atomic_fills(touched)) == 1
    fill = atomic_fills(touched)[0]
    assert fill.bar_index == 2
    assert fill.occurred_at == bars[2].closed_at
    assert fill.ledger_event.reference_price == Decimal("100")
    assert fill.order_event.fill_price == Decimal("100")

    gap_bars = [
        bar(0),
        bar(1, open_price="95", high="101", low="94", close="99"),
    ]
    gapped = simulate_event_driven(
        config(slippage_bps="50"),
        gap_bars,
        [
            submit(
                order_type="limit",
                limit_price="100",
                created_at=gap_bars[0].closed_at,
            )
        ],
    )
    gap_fill = atomic_fills(gapped)[0]
    assert gap_fill.ledger_event.reference_price == Decimal("95")
    assert gap_fill.order_event.fill_price == Decimal("95.475")
    assert gap_fill.order_event.fill_price <= Decimal("100")


def test_open_executions_precede_intrabar_touches_even_when_submitted_later() -> None:
    bars = [
        bar(0),
        bar(1, open_price="105", high="108", low="99", close="102"),
    ]
    earlier_limit = submit(
        sequence=1,
        order_id="earlier-limit",
        order_type="limit",
        limit_price="100",
        created_at=bars[0].closed_at,
    )
    later_market = submit(
        sequence=2,
        order_id="later-market",
        created_at=bars[0].closed_at,
    )
    run = simulate_event_driven(config(), bars, [earlier_limit, later_market])
    fills = atomic_fills(run)

    assert [fill.ledger_event.order_id for fill in fills] == [
        "later-market",
        "earlier-limit",
    ]
    assert [fill.occurred_at for fill in fills] == [
        bars[1].opened_at,
        bars[1].closed_at,
    ]
    assert tuple(transaction.occurred_at for transaction in run.ledger_state.transactions) == tuple(
        sorted(transaction.occurred_at for transaction in run.ledger_state.transactions)
    )


def test_same_phase_orders_share_volume_in_submission_sequence() -> None:
    bars = [bar(0), bar(1, volume="1")]
    first = submit(sequence=1, order_id="first-market")
    second = submit(sequence=2, order_id="second-market")
    run = simulate_event_driven(config(), bars, [first, second])

    assert [fill.ledger_event.order_id for fill in atomic_fills(run)] == ["first-market"]
    assert run.orders[0].state.status == "filled"
    assert run.orders[1].state.status == "working"


def test_sell_limit_reduces_long_position_without_ever_opening_a_short() -> None:
    bars = [
        bar(0),
        bar(1),
        bar(2, open_price="105", high="110", low="100", close="108"),
        bar(3, open_price="95", high="101", low="90", close="100"),
    ]
    intents = [
        submit(
            sequence=1,
            order_id="buy-first",
            quantity="2",
            created_at=bars[0].closed_at,
        ),
        submit(
            sequence=2,
            order_id="sell-after-buy",
            side="sell",
            order_type="limit",
            quantity="2",
            limit_price="104",
            created_at=bars[1].closed_at,
        ),
    ]
    run = simulate_event_driven(config(), bars, intents)

    sell_fill = next(record for record in atomic_fills(run) if record.ledger_event.side == "sell")
    assert sell_fill.bar_index == 2
    assert sell_fill.ledger_event.reference_price == Decimal("105")
    assert run.orders[1].state.status == "filled"
    assert run.ledger_state.positions[0].quantity == 0
    assert run.ledger_state.cash == Decimal("10010")
    assert run.ledger_state.realized_pnl == Decimal("10")


def test_insufficient_cash_and_position_are_order_rejections_without_journal_fills() -> None:
    bars = [bar(0), bar(1)]
    cash_run = simulate_event_driven(
        config(cash="50"),
        bars,
        [submit(order_id="too-expensive", quantity="1")],
    )
    position_run = simulate_event_driven(
        config(),
        bars,
        [submit(order_id="short-forbidden", side="sell", quantity="1")],
    )

    assert not cash_run.orders
    assert not position_run.orders
    assert {finding.code for finding in risk_rejections(cash_run)[0].risk_decision.findings} >= {
        "INSUFFICIENT_AVAILABLE_CASH"
    }
    assert {
        finding.code for finding in risk_rejections(position_run)[0].risk_decision.findings
    } >= {"INSUFFICIENT_AVAILABLE_POSITION"}
    assert not atomic_fills(cash_run)
    assert not atomic_fills(position_run)
    assert len(cash_run.ledger_state.transactions) == 1
    assert len(position_run.ledger_state.transactions) == 1


def test_engaged_kill_switch_rejects_before_an_executable_projection_exists() -> None:
    first_bar = bar(0)
    engaged = build_kill_switch_snapshot(
        status="engaged",
        reason_code="operator_halt",
        activated_at=START,
        source="event-simulation-tests",
    )
    run = simulate_event_driven(
        config(kill_switch=engaged),
        [first_bar],
        [submit(created_at=first_bar.closed_at)],
    )
    rejection = risk_rejections(run)[0]
    request = rejection.risk_decision.request

    assert not run.orders
    assert not atomic_fills(run)
    assert rejection.risk_decision.decision == "reject"
    assert {finding.code for finding in rejection.risk_decision.findings} >= {"KILL_SWITCH_ENGAGED"}
    assert isinstance(request, OrderRiskRequest)
    assert request.intent.quote_currency == "USDT"
    assert request.price_evidence.symbol == "BTCUSDT"
    assert request.price_evidence.quote_currency == "USDT"
    assert request.price_evidence.reference_price == first_bar.close
    assert request.price_evidence.observed_at == first_bar.closed_at
    assert request.price_evidence.available_at == first_bar.closed_at
    assert request.price_evidence.snapshot_hash == first_bar.bar_hash


def test_static_kill_switch_evidence_expires_instead_of_refreshing_per_order() -> None:
    first_bar = bar(0)
    run = simulate_event_driven(
        config(rules=risk_rules(maximum_kill_switch_age_seconds=0)),
        [first_bar],
        [submit(created_at=first_bar.closed_at)],
    )
    rejection = risk_rejections(run)[0]
    request = rejection.risk_decision.request

    assert not run.orders
    assert isinstance(request, OrderRiskRequest)
    assert request.kill_switch_observed_at == START
    assert request.kill_switch_available_at == START
    assert request.evaluated_at == first_bar.closed_at
    assert {finding.code for finding in rejection.risk_decision.findings} >= {
        "KILL_SWITCH_STATE_STALE"
    }


def test_kill_switch_is_rechecked_at_acknowledgement_and_fill_boundaries() -> None:
    rules = risk_rules(maximum_kill_switch_age_seconds=120)
    acknowledgement_bars = [bar(0), bar(1), bar(2)]
    acknowledgement_run = simulate_event_driven(
        config(latency_bars=1, rules=rules),
        acknowledgement_bars,
        [submit(created_at=acknowledgement_bars[0].closed_at)],
    )
    acknowledgement_projection = acknowledgement_run.orders[0]

    assert acknowledgement_projection.risk_decision.decision == "allow"
    assert acknowledgement_projection.state.status == "rejected"
    assert acknowledgement_projection.state.terminal_reason == (
        "execution kill-switch: snapshot is stale"
    )
    assert not atomic_fills(acknowledgement_run)
    assert acknowledgement_run.ledger_state.revision == 1
    assert acknowledgement_run.ledger_state.cash == Decimal("10000")

    submission = next(
        record for record in acknowledgement_run.records if isinstance(record, OrderSubmittedRecord)
    )
    stale_acknowledgement = event_simulation_module._build_record(
        OrderEventRecord,
        sequence=submission.sequence + 1,
        previous_hash=submission.record_hash,
        occurred_at=acknowledgement_bars[2].opened_at,
        bar_index=2,
        payload={
            "record_type": "order_event",
            "order_event": OrderAcknowledgedEvent.model_validate(
                {
                    "order_id": submission.intent.order_id,
                    "idempotency_key": "forged-stale-acknowledgement",
                }
            ),
        },
    )
    with pytest.raises(SimulationIntegrityError, match="kill-switch evidence"):
        replay_simulation_records(
            acknowledgement_run.config,
            (
                acknowledgement_run.records[0],
                submission,
                stale_acknowledgement,
            ),
        )

    fill_bars = [
        bar(0),
        bar(1, open_price="105", high="110", low="101", close="105"),
        bar(2, open_price="105", high="110", low="90", close="105"),
    ]
    fill_run = simulate_event_driven(
        config(rules=rules),
        fill_bars,
        [
            submit(
                order_type="limit",
                limit_price="100",
                created_at=fill_bars[0].closed_at,
            )
        ],
    )
    fill_projection = fill_run.orders[0]
    expiry = next(
        record for record in order_events(fill_run) if record.order_event.event_type == "expired"
    )

    assert fill_projection.state.status == "expired"
    assert fill_projection.state.terminal_reason == "execution kill-switch: snapshot is stale"
    assert expiry.occurred_at - START == timedelta(seconds=240)
    assert not atomic_fills(fill_run)
    assert fill_run.ledger_state.revision == 1
    assert fill_run.ledger_state.cash == Decimal("10000")

    acknowledgement_index = next(
        index
        for index, record in enumerate(fill_run.records)
        if isinstance(record, OrderEventRecord) and record.order_event.event_type == "acknowledged"
    )
    acknowledged_prefix = fill_run.records[: acknowledgement_index + 1]
    prefix_replay = replay_simulation_records(fill_run.config, acknowledged_prefix)
    stale_order_fill = OrderFillEvent.model_validate(
        {
            "order_id": fill_projection.intent.order_id,
            "idempotency_key": "forged-stale-fill-event",
            "fill_id": "forged-stale-fill",
            "fill_quantity": "1",
            "fill_price": "100",
        }
    )
    stale_ledger_fill = build_paper_fill_event(
        account_id=fill_run.config.account_id,
        expected_revision=prefix_replay.ledger_state.revision,
        idempotency_key=stale_order_fill.idempotency_key,
        fill_id=stale_order_fill.fill_id,
        order_id=stale_order_fill.order_id,
        symbol=fill_run.config.symbol,
        side="buy",
        quantity=stale_order_fill.fill_quantity,
        reference_price=Decimal("100"),
        fill_price=stale_order_fill.fill_price,
        fee=Decimal(0),
        occurred_at=fill_bars[2].closed_at,
    )
    stale_atomic_fill = event_simulation_module._build_record(
        AtomicFillRecord,
        sequence=len(acknowledged_prefix) + 1,
        previous_hash=acknowledged_prefix[-1].record_hash,
        occurred_at=fill_bars[2].closed_at,
        bar_index=2,
        payload={
            "record_type": "atomic_fill",
            "order_event": stale_order_fill,
            "ledger_event": stale_ledger_fill,
        },
    )
    with pytest.raises(SimulationIntegrityError, match="kill-switch evidence"):
        replay_simulation_records(
            fill_run.config,
            (*acknowledged_prefix, stale_atomic_fill),
        )


def test_order_notional_position_and_active_order_limits_reject_independently() -> None:
    first_bar = bar(0)
    notional_run = simulate_event_driven(
        config(rules=risk_rules(maximum_order_notional="100")),
        [first_bar],
        [submit(created_at=first_bar.closed_at)],
    )
    position_run = simulate_event_driven(
        config(rules=risk_rules(maximum_resulting_position="0.5")),
        [first_bar],
        [submit(created_at=first_bar.closed_at)],
    )
    active_run = simulate_event_driven(
        config(rules=risk_rules(maximum_active_orders=1)),
        [first_bar],
        [
            submit(
                order_id="active-first",
                quantity="0.1",
                created_at=first_bar.closed_at,
            ),
            submit(
                sequence=2,
                order_id="active-second",
                quantity="0.1",
                created_at=first_bar.closed_at,
            ),
        ],
    )

    assert {
        finding.code for finding in risk_rejections(notional_run)[0].risk_decision.findings
    } >= {"MAXIMUM_ORDER_NOTIONAL_EXCEEDED"}
    assert {
        finding.code for finding in risk_rejections(position_run)[0].risk_decision.findings
    } >= {"MAXIMUM_RESULTING_POSITION_EXCEEDED"}
    assert len(active_run.orders) == 1
    assert {finding.code for finding in risk_rejections(active_run)[0].risk_decision.findings} >= {
        "MAXIMUM_ACTIVE_ORDERS_EXCEEDED"
    }


def test_buy_and_sell_reservations_are_applied_at_each_close_time_decision() -> None:
    first_bar = bar(0)
    fee_buffer_rules = risk_rules(order_fee_buffer_ratio="0.01")
    buy_run = simulate_event_driven(
        config(cash="211", rules=fee_buffer_rules),
        [first_bar],
        [
            submit(order_id="buy-first", created_at=first_bar.closed_at),
            submit(
                sequence=2,
                order_id="buy-second",
                created_at=first_bar.closed_at,
            ),
        ],
    )
    buy_rejection = risk_rejections(buy_run)[0]
    buy_request = buy_rejection.risk_decision.request

    assert isinstance(buy_request, OrderRiskRequest)
    assert buy_request.state.reserved_buy_cash == Decimal("106.05")
    assert buy_request.state.reserved_buy_quantity == Decimal("1")
    assert {finding.code for finding in buy_rejection.risk_decision.findings} >= {
        "INSUFFICIENT_AVAILABLE_CASH"
    }

    bars = [bar(0), bar(1)]
    sell_run = simulate_event_driven(
        config(),
        bars,
        [
            submit(order_id="position-source", created_at=bars[0].closed_at),
            submit(
                sequence=2,
                order_id="sell-first",
                side="sell",
                created_at=bars[1].closed_at,
            ),
            submit(
                sequence=3,
                order_id="sell-second",
                side="sell",
                created_at=bars[1].closed_at,
            ),
        ],
    )
    sell_rejection = risk_rejections(sell_run)[0]
    sell_request = sell_rejection.risk_decision.request

    assert isinstance(sell_request, OrderRiskRequest)
    assert sell_request.state.position_quantity == Decimal("1")
    assert sell_request.state.reserved_sell_quantity == Decimal("1")
    assert {finding.code for finding in sell_rejection.risk_decision.findings} >= {
        "INSUFFICIENT_AVAILABLE_POSITION"
    }


def test_partial_fill_releases_only_the_filled_slice_of_risk_reservation() -> None:
    bars = [
        bar(0, close="100"),
        bar(1, open_price="100", close="100", volume="1"),
    ]
    buffered_rules = risk_rules(order_fee_buffer_ratio="0.01")
    run = simulate_event_driven(
        config(cash="405", rules=buffered_rules),
        bars,
        [
            submit(
                order_id="partial-reservation",
                quantity="4",
                created_at=bars[0].closed_at,
            ),
            submit(
                sequence=2,
                order_id="after-partial",
                quantity="0.1",
                created_at=bars[1].closed_at,
            ),
        ],
    )
    rejection = risk_rejections(run)[0]
    request = rejection.risk_decision.request

    assert run.orders[0].state.status == "partially_filled"
    assert run.orders[0].state.filled_quantity == Decimal("1")
    assert isinstance(request, OrderRiskRequest)
    assert request.state.cash_balance == Decimal("305")
    assert request.state.reserved_buy_quantity == Decimal("3")
    assert request.state.reserved_buy_cash == Decimal("303")
    assert {finding.code for finding in rejection.risk_decision.findings} >= {
        "INSUFFICIENT_AVAILABLE_CASH"
    }


def test_sell_limit_notional_uses_the_more_conservative_finalized_close() -> None:
    bars = [
        bar(
            0,
            open_price="50",
            high="55",
            low="45",
            close="50",
        ),
        bar(
            1,
            open_price="50",
            high="110",
            low="45",
            close="105",
        ),
    ]
    run = simulate_event_driven(
        config(rules=risk_rules(maximum_order_notional="100")),
        bars,
        [
            submit(order_id="position-source", created_at=bars[0].closed_at),
            submit(
                sequence=2,
                order_id="conservative-sell-limit",
                side="sell",
                order_type="limit",
                quantity="1",
                limit_price="50",
                created_at=bars[1].closed_at,
            ),
        ],
    )
    rejection = risk_rejections(run)[0]
    request = rejection.risk_decision.request

    assert rejection.intent.limit_price == Decimal("50")
    assert isinstance(request, OrderRiskRequest)
    assert request.price_evidence.reference_price == Decimal("105")
    assert {finding.code for finding in rejection.risk_decision.findings} >= {
        "MAXIMUM_ORDER_NOTIONAL_EXCEEDED"
    }


def test_market_gap_beyond_submit_buffer_is_terminally_rejected_before_ack() -> None:
    bars = [
        bar(0, close="100"),
        bar(
            1,
            open_price="111",
            high="120",
            low="100",
            close="112",
        ),
    ]
    run = simulate_event_driven(
        config(rules=risk_rules(market_order_price_buffer_ratio="0.05")),
        bars,
        [submit(created_at=bars[0].closed_at)],
    )
    projection = run.orders[0]
    events = order_events(run)
    request = projection.risk_decision.request

    assert projection.risk_decision.decision == "allow"
    assert isinstance(request, OrderRiskRequest)
    assert request.price_evidence.reference_price == Decimal("100")
    assert projection.state.status == "rejected"
    assert projection.state.terminal_reason == (
        "execution risk envelope: candidate price exceeds approved risk price"
    )
    assert [record.order_event.event_type for record in events] == ["rejected"]
    assert not atomic_fills(run)

    submission = next(record for record in run.records if isinstance(record, OrderSubmittedRecord))
    acknowledgement_event = OrderAcknowledgedEvent.model_validate(
        {
            "order_id": submission.intent.order_id,
            "idempotency_key": "forged-gap-ack",
        }
    )
    acknowledgement_record = event_simulation_module._build_record(
        OrderEventRecord,
        sequence=submission.sequence + 1,
        previous_hash=submission.record_hash,
        occurred_at=bars[1].opened_at,
        bar_index=1,
        payload={
            "record_type": "order_event",
            "order_event": acknowledgement_event,
        },
    )
    order_fill = OrderFillEvent.model_validate(
        {
            "order_id": submission.intent.order_id,
            "idempotency_key": "forged-gap-fill-event",
            "fill_id": "forged-gap-fill",
            "fill_quantity": "1",
            "fill_price": "111",
        }
    )
    ledger_fill = build_paper_fill_event(
        account_id=run.config.account_id,
        expected_revision=1,
        idempotency_key=order_fill.idempotency_key,
        fill_id=order_fill.fill_id,
        order_id=submission.intent.order_id,
        symbol=run.config.symbol,
        side=submission.intent.side,
        quantity=order_fill.fill_quantity,
        reference_price=Decimal("111"),
        fill_price=order_fill.fill_price,
        fee=Decimal(0),
        occurred_at=bars[1].opened_at,
    )
    unsafe_fill = event_simulation_module._build_record(
        AtomicFillRecord,
        sequence=acknowledgement_record.sequence + 1,
        previous_hash=acknowledgement_record.record_hash,
        occurred_at=bars[1].opened_at,
        bar_index=1,
        payload={
            "record_type": "atomic_fill",
            "order_event": order_fill,
            "ledger_event": ledger_fill,
        },
    )
    with pytest.raises(SimulationIntegrityError, match="execution-time risk envelope"):
        replay_simulation_records(
            run.config,
            (
                run.records[0],
                submission,
                acknowledgement_record,
                unsafe_fill,
            ),
        )


def test_working_sell_limit_accepts_beneficial_gap_but_replay_rejects_below_limit() -> None:
    bars = [
        bar(
            0,
            open_price="50",
            high="55",
            low="45",
            close="50",
        ),
        bar(
            1,
            open_price="50",
            high="105",
            low="45",
            close="100",
        ),
        bar(
            2,
            open_price="80",
            high="85",
            low="75",
            close="80",
        ),
        bar(
            3,
            open_price="120",
            high="125",
            low="110",
            close="122",
        ),
    ]
    run = simulate_event_driven(
        config(),
        bars,
        [
            submit(order_id="position-source", created_at=bars[0].closed_at),
            submit(
                sequence=2,
                order_id="gapped-sell-limit",
                side="sell",
                order_type="limit",
                limit_price="90",
                created_at=bars[1].closed_at,
            ),
        ],
    )
    sell = next(
        projection for projection in run.orders if projection.intent.order_id == "gapped-sell-limit"
    )
    request = sell.risk_decision.request

    assert sell.risk_decision.decision == "allow"
    assert isinstance(request, OrderRiskRequest)
    assert request.price_evidence.reference_price == Decimal("100")
    assert sell.state.status == "filled"
    sell_fill = next(
        fill
        for fill in atomic_fills(run)
        if fill.ledger_event.order_id == "gapped-sell-limit"
    )
    assert sell_fill.ledger_event.fill_price == Decimal("120")
    assert run.ledger_state.positions[0].quantity == Decimal(0)

    sell_ack_index = next(
        index
        for index, record in enumerate(run.records)
        if isinstance(record, OrderEventRecord)
        and record.order_event.order_id == "gapped-sell-limit"
        and record.order_event.event_type == "acknowledged"
    )
    replay_prefix = run.records[: sell_ack_index + 1]
    prefix_projection = replay_simulation_records(run.config, replay_prefix)
    forged_order_fill = OrderFillEvent.model_validate(
        {
            "order_id": "gapped-sell-limit",
            "idempotency_key": "forged-below-limit-fill-event",
            "fill_id": "forged-below-limit-fill",
            "fill_quantity": "1",
            "fill_price": "80",
        }
    )
    forged_ledger_fill = build_paper_fill_event(
        account_id=run.config.account_id,
        expected_revision=prefix_projection.ledger_state.revision,
        idempotency_key=forged_order_fill.idempotency_key,
        fill_id=forged_order_fill.fill_id,
        order_id=forged_order_fill.order_id,
        symbol=run.config.symbol,
        side="sell",
        quantity=forged_order_fill.fill_quantity,
        reference_price=Decimal("80"),
        fill_price=forged_order_fill.fill_price,
        fee=Decimal(0),
        occurred_at=bars[3].opened_at,
    )
    forged_atomic_fill = event_simulation_module._build_record(
        AtomicFillRecord,
        sequence=len(replay_prefix) + 1,
        previous_hash=replay_prefix[-1].record_hash,
        occurred_at=bars[3].opened_at,
        bar_index=3,
        payload={
            "record_type": "atomic_fill",
            "order_event": forged_order_fill,
            "ledger_event": forged_ledger_fill,
        },
    )
    with pytest.raises(SimulationIntegrityError, match="execution-time risk envelope"):
        replay_simulation_records(
            run.config,
            (*replay_prefix, forged_atomic_fill),
        )


def test_market_sell_gap_below_submit_buffer_is_rejected_before_ack() -> None:
    bars = [
        bar(0, close="100"),
        bar(
            1,
            open_price="100",
            high="105",
            low="95",
            close="100",
        ),
        bar(
            2,
            open_price="90",
            high="92",
            low="85",
            close="90",
        ),
    ]
    run = simulate_event_driven(
        config(rules=risk_rules(market_order_price_buffer_ratio="0.05")),
        bars,
        [
            submit(order_id="position-source", created_at=bars[0].closed_at),
            submit(
                sequence=2,
                order_id="gapped-market-sell",
                side="sell",
                created_at=bars[1].closed_at,
            ),
        ],
    )
    sell = next(
        projection
        for projection in run.orders
        if projection.intent.order_id == "gapped-market-sell"
    )
    request = sell.risk_decision.request

    assert isinstance(request, OrderRiskRequest)
    assert request.price_evidence.reference_price == Decimal("100")
    assert sell.state.status == "rejected"
    assert sell.state.terminal_reason == (
        "execution risk envelope: candidate price is below approved risk price"
    )
    assert all(
        fill.ledger_event.order_id != "gapped-market-sell"
        for fill in atomic_fills(run)
    )
    assert run.ledger_state.positions[0].quantity == Decimal("1")


def test_time_in_force_expires_unfilled_or_partially_filled_limit_orders() -> None:
    no_cross_bars = [
        bar(0),
        bar(1, open_price="105", low="101"),
        bar(2, open_price="105", low="101"),
    ]
    unfilled = simulate_event_driven(
        config(),
        no_cross_bars,
        [
            submit(
                order_type="limit",
                limit_price="100",
                expires_after_bars=2,
            )
        ],
    )
    assert unfilled.orders[0].state.status == "expired"
    assert unfilled.orders[0].state.filled_quantity == 0
    assert not atomic_fills(unfilled)

    partial_bars = [bar(0), bar(1, volume="2"), bar(2, volume="2")]
    partial = simulate_event_driven(
        config(participation="0.5"),
        partial_bars,
        [submit(quantity="3", expires_after_bars=1)],
    )
    assert partial.orders[0].state.status == "expired"
    assert partial.orders[0].state.filled_quantity == Decimal("1")
    assert partial.orders[0].state.terminal_reason == "time in force elapsed"


def test_close_time_cancel_is_atomic_and_prevents_future_execution() -> None:
    bars = [bar(0), bar(1), bar(2)]
    submitted = submit(quantity="2", created_at=bars[0].closed_at)
    cancellation = CancelOrderIntent(
        sequence=2,
        idempotency_key="cancel-one",
        order_id=submitted.order_id,
        created_at=bars[0].closed_at,
    )
    run = simulate_event_driven(config(), bars, [submitted, cancellation])

    assert run.orders[0].state.status == "cancelled"
    assert run.orders[0].state.filled_quantity == 0
    assert not atomic_fills(run)
    cancellation_record = next(
        record for record in run.records if record.record_type == "order_cancelled"
    )
    assert cancellation_record.bar_index == 0


def test_cancel_after_partial_fill_preserves_fill_and_stops_later_bars() -> None:
    bars = [bar(0), bar(1, volume="2"), bar(2, volume="10")]
    submitted = submit(quantity="3", created_at=bars[0].closed_at)
    cancellation = CancelOrderIntent(
        sequence=2,
        idempotency_key="cancel-after-partial",
        order_id=submitted.order_id,
        created_at=bars[1].closed_at,
    )
    run = simulate_event_driven(
        config(participation="0.5"),
        bars,
        [submitted, cancellation],
    )

    assert run.orders[0].state.status == "cancelled"
    assert run.orders[0].state.filled_quantity == Decimal("1")
    assert len(atomic_fills(run)) == 1
    assert run.ledger_state.positions[0].quantity == Decimal("1")
    submission = next(record for record in run.records if isinstance(record, OrderSubmittedRecord))
    replayed = replay_simulation_records(run.config, run.records)
    assert submission.risk_decision.decision == "allow"
    assert run.orders[0].risk_decision == submission.risk_decision
    assert replayed.orders[0].risk_decision.decision_hash == (
        submission.risk_decision.decision_hash
    )


def test_close_cancel_cannot_erase_same_bar_intrabar_limit_fill() -> None:
    bars = [
        bar(0),
        bar(1, open_price="105", high="108", low="99", close="102", volume="2"),
        bar(2),
    ]
    submitted = submit(
        order_type="limit",
        quantity="3",
        limit_price="100",
        created_at=bars[0].closed_at,
    )
    cancellation = CancelOrderIntent(
        sequence=2,
        idempotency_key="cancel-at-touch-close",
        order_id=submitted.order_id,
        created_at=bars[1].closed_at,
    )
    run = simulate_event_driven(
        config(participation="0.5"),
        bars,
        [submitted, cancellation],
    )
    fill = atomic_fills(run)[0]
    cancellation_index = next(
        index for index, record in enumerate(run.records) if record.record_type == "order_cancelled"
    )
    fill_index = run.records.index(fill)

    assert fill.occurred_at == bars[1].closed_at
    assert fill_index < cancellation_index
    assert run.orders[0].state.status == "cancelled"
    assert run.orders[0].state.filled_quantity == Decimal("1")
    assert run.ledger_state.positions[0].quantity == Decimal("1")


def test_submission_priority_and_reservations_prevent_cash_overcommitment() -> None:
    bars = [bar(0), bar(1, volume="10"), bar(2, volume="10")]
    first = submit(order_id="first", quantity="60")
    second = submit(sequence=2, order_id="second", quantity="60")
    run = simulate_event_driven(config(), bars, [first, second])

    assert run.orders[0].state.status == "partially_filled"
    assert run.orders[0].state.filled_quantity == Decimal("20")
    assert len(run.orders) == 1
    assert {finding.code for finding in risk_rejections(run)[0].risk_decision.findings} >= {
        "INSUFFICIENT_AVAILABLE_CASH"
    }
    assert all(record.ledger_event.order_id == "first" for record in atomic_fills(run))
    assert run.ledger_state.cash == Decimal("8000")


def test_internal_event_ids_remain_bounded_for_maximum_length_caller_ids() -> None:
    bars = [bar(0), bar(1)]
    long_order_id = "o" * 200
    long_key = "k" * 200
    intent = SubmitOrderIntent.model_validate(
        {
            "sequence": 1,
            "idempotency_key": long_key,
            "order_id": long_order_id,
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": "1",
            "created_at": bars[0].closed_at,
        }
    )
    run = simulate_event_driven(config(), bars, [intent])

    assert run.orders[0].state.status == "filled"
    fill = atomic_fills(run)[0]
    assert len(fill.order_event.idempotency_key) < 200
    assert len(fill.order_event.fill_id) < 200
    assert len(fill.ledger_event.idempotency_key) < 200


def test_exact_duplicate_intent_is_noop_but_conflicting_key_fails() -> None:
    bars = [bar(0), bar(1)]
    original = submit()
    duplicate_run = simulate_event_driven(config(), bars, [original, original])
    single_run = simulate_event_driven(config(), bars, [original])

    assert duplicate_run == single_run
    conflicting = SubmitOrderIntent.model_validate(
        {
            "sequence": 2,
            "idempotency_key": original.idempotency_key,
            "order_id": "another-order",
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": "1",
            "created_at": original.created_at,
        }
    )
    with pytest.raises(SimulationInputError, match="different payload"):
        simulate_event_driven(config(), bars, [original, conflicting])


def test_hash_chain_replay_is_deterministic_and_detects_tampering() -> None:
    bars = [bar(0), bar(1), bar(2)]
    run = simulate_event_driven(config(), bars, [submit(quantity="2")])
    replay = replay_simulation_records(run.config, run.records)

    assert replay.orders == run.orders
    assert replay.ledger_state == run.ledger_state
    assert replay.final_record_hash == run.final_record_hash
    assert verify_event_simulation(run) == replay

    fill_index = next(
        index for index, record in enumerate(run.records) if isinstance(record, AtomicFillRecord)
    )
    forged_fill = run.records[fill_index].model_copy(update={"record_hash": "f" * 64})
    forged_records = list(run.records)
    forged_records[fill_index] = forged_fill
    forged_run = run.model_copy(update={"records": tuple(forged_records)})
    with pytest.raises(SimulationIntegrityError, match="strict revalidation"):
        verify_event_simulation(forged_run)

    forged_projection = run.orders[0].model_copy(
        update={"state": run.orders[0].state.model_copy(update={"filled_quantity": Decimal(0)})}
    )
    with pytest.raises(SimulationIntegrityError, match="strict revalidation"):
        verify_event_simulation(run.model_copy(update={"orders": (forged_projection,)}))


def test_verify_reruns_canonical_inputs_and_rejects_a_resigned_fill() -> None:
    bars = [bar(0), bar(1)]
    intents = [submit()]
    run = simulate_event_driven(config(), bars, intents)
    original_fill = atomic_fills(run)[0]
    forged_order_fill = OrderFillEvent.model_validate(
        {
            "order_id": original_fill.order_event.order_id,
            "idempotency_key": original_fill.order_event.idempotency_key,
            "fill_id": original_fill.order_event.fill_id,
            "fill_quantity": original_fill.order_event.fill_quantity,
            "fill_price": "101",
        }
    )
    forged_ledger_fill = build_paper_fill_event(
        account_id=original_fill.ledger_event.account_id,
        expected_revision=original_fill.ledger_event.expected_revision,
        idempotency_key=forged_order_fill.idempotency_key,
        fill_id=forged_order_fill.fill_id,
        order_id=forged_order_fill.order_id,
        symbol=original_fill.ledger_event.symbol,
        side=original_fill.ledger_event.side,
        quantity=forged_order_fill.fill_quantity,
        reference_price=original_fill.ledger_event.reference_price,
        fill_price=forged_order_fill.fill_price,
        fee=Decimal(0),
        occurred_at=original_fill.occurred_at,
    )
    resigned_fill = event_simulation_module._build_record(
        AtomicFillRecord,
        sequence=original_fill.sequence,
        previous_hash=original_fill.previous_hash,
        occurred_at=original_fill.occurred_at,
        bar_index=original_fill.bar_index,
        payload={
            "record_type": "atomic_fill",
            "order_event": forged_order_fill,
            "ledger_event": forged_ledger_fill,
        },
    )
    forged_records = tuple(
        resigned_fill if record.sequence == original_fill.sequence else record
        for record in run.records
    )
    forged_replay = replay_simulation_records(run.config, forged_records)
    forged_replay_hash = event_simulation_module._hash(
        {
            "config": run.config,
            "bars_hash": run.bars_hash,
            "intents_hash": run.intents_hash,
            "final_record_hash": forged_replay.final_record_hash,
            "projection_hash": forged_replay.projection_hash,
        }
    )
    resigned_run = run.model_copy(
        update={
            "records": forged_records,
            "orders": forged_replay.orders,
            "ledger_state": forged_replay.ledger_state,
            "final_record_hash": forged_replay.final_record_hash,
            "replay_hash": forged_replay_hash,
        }
    )

    assert run.schema_version == 2
    assert run.bars == tuple(bars)
    assert run.intents == tuple(intents)
    assert forged_replay.ledger_state != run.ledger_state
    with pytest.raises(SimulationIntegrityError, match="canonical-input replay"):
        verify_event_simulation(resigned_run)


def test_rule_hash_change_invalidates_every_recorded_submit_decision() -> None:
    first_bar = bar(0)
    original_rules = risk_rules(rule_set_version="1.0.0")
    changed_rules = risk_rules(
        maximum_active_orders=99,
        rule_set_version="1.0.1",
    )
    run = simulate_event_driven(
        config(rules=original_rules),
        [first_bar],
        [submit(created_at=first_bar.closed_at)],
    )

    assert original_rules.rules_hash != changed_rules.rules_hash
    changed_config = run.config.model_copy(update={"order_risk_rule_set": changed_rules})
    with pytest.raises(SimulationIntegrityError, match="policy"):
        replay_simulation_records(changed_config, run.records)


def test_stale_or_content_tampered_price_evidence_cannot_cross_replay_boundary() -> None:
    first_bar = bar(0)
    run = simulate_event_driven(
        config(),
        [first_bar],
        [submit(created_at=first_bar.closed_at)],
    )
    submitted_index = next(
        index
        for index, record in enumerate(run.records)
        if isinstance(record, OrderSubmittedRecord)
    )
    submitted = run.records[submitted_index]
    assert isinstance(submitted, OrderSubmittedRecord)
    request = submitted.risk_decision.request
    assert isinstance(request, OrderRiskRequest)

    tampered_price = request.price_evidence.model_copy(update={"reference_price": Decimal("1")})
    tampered_request = request.model_copy(update={"price_evidence": tampered_price})
    tampered_decision = submitted.risk_decision.model_copy(update={"request": tampered_request})
    tampered_submission = submitted.model_copy(update={"risk_decision": tampered_decision})
    tampered_records = list(run.records)
    tampered_records[submitted_index] = tampered_submission
    with pytest.raises(SimulationIntegrityError, match="strict revalidation"):
        replay_simulation_records(run.config, tampered_records)

    stale_price = OrderPriceEvidence(
        symbol=request.price_evidence.symbol,
        quote_currency=request.price_evidence.quote_currency,
        reference_price=request.price_evidence.reference_price,
        observed_at=submitted.occurred_at - timedelta(seconds=1),
        available_at=submitted.occurred_at - timedelta(seconds=1),
        source=request.price_evidence.source,
        snapshot_hash=request.price_evidence.snapshot_hash,
    )
    stale_request = build_order_risk_request(
        evaluation_id=request.evaluation_id,
        evaluated_at=request.evaluated_at,
        source_calculation_version=request.source_calculation_version,
        rule_set=request.rule_set,
        kill_switch=request.kill_switch,
        kill_switch_observed_at=request.kill_switch_observed_at,
        kill_switch_available_at=request.kill_switch_available_at,
        intent=request.intent,
        state=request.state,
        price_evidence=stale_price,
    )
    stale_decision = evaluate_order_risk(stale_request)
    assert stale_decision.decision == "reject"
    assert {finding.code for finding in stale_decision.findings} >= {"REFERENCE_PRICE_STALE"}
    stale_record = event_simulation_module._build_record(
        OrderRiskRejectedRecord,
        sequence=submitted.sequence,
        previous_hash=submitted.previous_hash,
        occurred_at=submitted.occurred_at,
        bar_index=submitted.bar_index,
        payload={
            "record_type": "order_risk_rejected",
            "intent": submitted.intent,
            "risk_decision": stale_decision,
        },
    )
    with pytest.raises(SimulationIntegrityError, match="price timeline"):
        replay_simulation_records(
            run.config,
            (run.records[0], stale_record),
        )


def test_replay_recomputes_reservations_instead_of_trusting_forged_state() -> None:
    first_bar = bar(0)
    buffered_rules = risk_rules(order_fee_buffer_ratio="0.01")
    run = simulate_event_driven(
        config(cash="211", rules=buffered_rules),
        [first_bar],
        [
            submit(order_id="reservation-first", created_at=first_bar.closed_at),
            submit(
                sequence=2,
                order_id="reservation-second",
                created_at=first_bar.closed_at,
            ),
        ],
    )
    rejection_index = next(
        index
        for index, record in enumerate(run.records)
        if isinstance(record, OrderRiskRejectedRecord)
    )
    rejection = run.records[rejection_index]
    assert isinstance(rejection, OrderRiskRejectedRecord)
    request = rejection.risk_decision.request
    assert isinstance(request, OrderRiskRequest)
    assert request.state.reserved_buy_cash == Decimal("106.05")

    forged_state = request.state.model_copy(
        update={
            "account_state_hash": "f" * 64,
            "reserved_buy_cash": Decimal(0),
            "reserved_buy_quantity": Decimal(0),
        }
    )
    forged_request = build_order_risk_request(
        evaluation_id=request.evaluation_id,
        evaluated_at=request.evaluated_at,
        source_calculation_version=request.source_calculation_version,
        rule_set=request.rule_set,
        kill_switch=request.kill_switch,
        kill_switch_observed_at=request.kill_switch_observed_at,
        kill_switch_available_at=request.kill_switch_available_at,
        intent=request.intent,
        state=forged_state,
        price_evidence=request.price_evidence,
    )
    forged_allow = evaluate_order_risk(forged_request)
    assert forged_allow.decision == "allow"
    forged_submission = event_simulation_module._build_record(
        OrderSubmittedRecord,
        sequence=rejection.sequence,
        previous_hash=rejection.previous_hash,
        occurred_at=rejection.occurred_at,
        bar_index=rejection.bar_index,
        payload={
            "record_type": "order_submitted",
            "intent": rejection.intent,
            "eligible_bar_index": 1,
            "risk_decision": forged_allow,
        },
    )
    forged_prefix = (
        *run.records[:rejection_index],
        forged_submission,
    )
    with pytest.raises(SimulationIntegrityError, match="deterministic replay"):
        replay_simulation_records(run.config, forged_prefix)


def test_run_hashes_are_context_independent_and_change_with_execution_inputs() -> None:
    bars = [bar(0), bar(1, volume="3"), bar(2, volume="3")]
    intent = submit(quantity="3")
    with localcontext() as decimal_context:
        decimal_context.prec = 6
        low_precision = simulate_event_driven(
            config(fee_rate="0.00123456789", slippage_bps="12.3456789"),
            bars,
            [intent],
        )
    with localcontext() as decimal_context:
        decimal_context.prec = 80
        high_precision = simulate_event_driven(
            config(fee_rate="0.00123456789", slippage_bps="12.3456789"),
            bars,
            [intent],
        )

    assert low_precision == high_precision
    changed = simulate_event_driven(
        config(fee_rate="0.00123456789", slippage_bps="12.345679"),
        bars,
        [intent],
    )
    assert changed.replay_hash != high_precision.replay_hash


def test_input_order_gaps_overlap_unknown_cancel_and_terminal_cancel_fail_closed() -> None:
    first = bar(0)
    non_contiguous = bar(2)
    with pytest.raises(SimulationInputError, match="contiguous"):
        simulate_event_driven(config(), [first, non_contiguous], [])

    overlapping = SimulationBar.model_validate(
        {
            "index": 1,
            "symbol": "BTCUSDT",
            "opened_at": first.closed_at - timedelta(seconds=1),
            "closed_at": first.closed_at + timedelta(minutes=1),
            "open": "100",
            "high": "110",
            "low": "90",
            "close": "105",
            "volume": "10",
        }
    )
    with pytest.raises(SimulationInputError, match="overlap"):
        simulate_event_driven(config(), [first, overlapping], [])

    unknown_cancel = CancelOrderIntent(
        sequence=1,
        idempotency_key="cancel-unknown",
        order_id="unknown",
        created_at=first.closed_at,
    )
    with pytest.raises(SimulationInputError, match="unknown"):
        simulate_event_driven(config(), [first], [unknown_cancel])

    bars = [bar(0), bar(1)]
    filled = submit(quantity="1")
    too_late = CancelOrderIntent(
        sequence=2,
        idempotency_key="cancel-filled",
        order_id=filled.order_id,
        created_at=bars[1].closed_at,
    )
    with pytest.raises(SimulationInputError, match="terminal"):
        simulate_event_driven(config(), bars, [filled, too_late])


def test_intent_contract_rejects_ambiguous_order_parameters_and_bad_timing() -> None:
    with pytest.raises(ValidationError, match="requires"):
        submit(order_type="limit")
    with pytest.raises(ValidationError, match="cannot include"):
        submit(order_type="market", limit_price="100")
    with pytest.raises(ValidationError, match="positive"):
        submit(quantity="0")
    with pytest.raises(ValidationError, match="Slippage"):
        config(slippage_bps="10001")
    with pytest.raises(ValidationError, match="fee buffer"):
        config(
            fee_rate="0.002",
            rules=risk_rules(order_fee_buffer_ratio="0.001"),
        )

    bars = [bar(0), bar(1)]
    between_closes = submit(created_at=bars[0].closed_at + timedelta(seconds=1))
    with pytest.raises(SimulationInputError, match="finalized bar close"):
        simulate_event_driven(config(), bars, [between_closes])
