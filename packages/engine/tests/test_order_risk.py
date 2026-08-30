from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from quantsieve_engine import (
    OrderPriceEvidence,
    OrderRiskIntent,
    OrderRiskLimits,
    OrderRiskRequest,
    OrderRiskRuleSet,
    OrderRiskState,
    RiskDecision,
    build_kill_switch_snapshot,
    build_order_risk_request,
    build_order_rule_set,
    evaluate_order_risk,
)
from quantsieve_engine import (
    canonical_payload_hash as public_canonical_payload_hash,
)
from quantsieve_engine.risk import (
    AllocationIntent,
    AllocationRiskLimits,
    AllocationRiskRequest,
    ResearchEvidence,
    ResearchRiskLimits,
    ResearchRiskRequest,
    RiskRuleSet,
    build_allocation_risk_request,
    build_allocation_rule_set,
    build_research_risk_request,
    build_research_rule_set,
    canonical_payload_hash,
    evaluate_allocation_risk,
    evaluate_research_risk,
)

NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
ACCOUNT_ID_HASH = "9" * 64
ACCOUNT_HASH = "a" * 64
SNAPSHOT_HASH = "b" * 64


def order_limits(**changes: object) -> OrderRiskLimits:
    values: dict[str, object] = {
        "long_only": True,
        "maximum_order_notional": Decimal("10000"),
        "maximum_resulting_position": Decimal("10"),
        "maximum_active_orders": 5,
        "minimum_cash_reserve": Decimal("100"),
        "maximum_price_age_seconds": 60,
        "maximum_kill_switch_age_seconds": 30,
        "market_order_price_buffer_ratio": Decimal("0.02"),
        "order_fee_buffer_ratio": Decimal("0.001"),
    }
    values.update(changes)
    return OrderRiskLimits.model_validate(values)


def order_intent(**changes: object) -> OrderRiskIntent:
    values: dict[str, object] = {
        "order_id": "order-1",
        "symbol": "BTCUSDT",
        "quote_currency": "USDT",
        "side": "buy",
        "order_type": "market",
        "quantity": Decimal("0.1"),
        "limit_price": None,
    }
    values.update(changes)
    return OrderRiskIntent.model_validate(values)


def order_state(**changes: object) -> OrderRiskState:
    values: dict[str, object] = {
        "account_id_hash": ACCOUNT_ID_HASH,
        "account_revision": 7,
        "account_state_hash": ACCOUNT_HASH,
        "symbol": "BTCUSDT",
        "quote_currency": "USDT",
        "cash_balance": Decimal("20000"),
        "position_quantity": Decimal("2"),
        "reserved_buy_cash": Decimal("0"),
        "reserved_buy_quantity": Decimal("0"),
        "reserved_sell_quantity": Decimal("0"),
        "active_order_count": 1,
    }
    values.update(changes)
    return OrderRiskState.model_validate(values)


def price_evidence(**changes: object) -> OrderPriceEvidence:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "quote_currency": "USDT",
        "reference_price": Decimal("50000"),
        "observed_at": NOW - timedelta(seconds=10),
        "available_at": NOW - timedelta(seconds=9),
        "source": "test-finalized-bars",
        "snapshot_hash": SNAPSHOT_HASH,
    }
    values.update(changes)
    return OrderPriceEvidence.model_validate(values)


def order_request(
    *,
    limits: OrderRiskLimits | None = None,
    intent: OrderRiskIntent | None = None,
    state: OrderRiskState | None = None,
    price: OrderPriceEvidence | None = None,
    kill_switch_status: str = "clear",
    kill_switch_scope: str = "global",
    kill_switch_scope_id_hash: str | None = None,
    kill_switch_observed_at: datetime | None = None,
    kill_switch_available_at: datetime | None = None,
    rule_set_version: str = "1.0.0",
) -> OrderRiskRequest:
    kill_switch = build_kill_switch_snapshot(
        status=kill_switch_status,  # type: ignore[arg-type]
        scope=kill_switch_scope,  # type: ignore[arg-type]
        scope_id_hash=kill_switch_scope_id_hash,
        revision=11,
        reason_code=(
            "operator_stop"
            if kill_switch_status == "engaged"
            else "risk_store_unreachable"
            if kill_switch_status == "unavailable"
            else None
        ),
        activated_at=NOW if kill_switch_status == "engaged" else None,
        source="test-risk-control",
    )
    return build_order_risk_request(
        evaluation_id="order-risk-evaluation",
        evaluated_at=NOW,
        source_calculation_version="paper-oms-v1",
        rule_set=build_order_rule_set(
            limits or order_limits(),
            rule_set_version=rule_set_version,
        ),
        kill_switch=kill_switch,
        kill_switch_observed_at=kill_switch_observed_at or NOW - timedelta(seconds=2),
        kill_switch_available_at=kill_switch_available_at or NOW - timedelta(seconds=1),
        intent=intent or order_intent(),
        state=state or order_state(),
        price_evidence=price or price_evidence(),
    )


def finding_codes(decision: RiskDecision) -> tuple[str, ...]:
    return tuple(finding.code for finding in decision.findings)


def test_order_decision_is_exact_deterministic_and_content_addressed() -> None:
    request = order_request()
    decision = evaluate_order_risk(request)
    repeated = evaluate_order_risk(request)

    assert decision.decision == "allow"
    assert decision.effective_weights is None
    assert decision.findings == ()
    assert decision.decision_id == decision.decision_hash
    assert repeated == decision
    assert request.rule_set.evaluation_kind == "order_intent"
    assert set(request.rule_set.model_dump(mode="python")) == {
        "schema_version",
        "evaluation_kind",
        "rule_set_id",
        "rule_set_version",
        "compatibility_profile",
        "order_limits",
        "rules_hash",
    }
    assert request.request_hash == repeated.request.request_hash
    assert isinstance(request.intent.quantity, Decimal)
    assert isinstance(request.state.cash_balance, Decimal)
    assert isinstance(request.price_evidence.reference_price, Decimal)

    offset = timezone(timedelta(hours=8))
    normalized = build_order_risk_request(
        evaluation_id="order-risk-evaluation",
        evaluated_at=NOW.astimezone(offset),
        source_calculation_version="paper-oms-v1",
        rule_set=build_order_rule_set(
            order_limits(
                maximum_order_notional=Decimal("10000.000"),
                market_order_price_buffer_ratio=Decimal("0.0200"),
            )
        ),
        kill_switch=build_kill_switch_snapshot(
            revision=11,
            source="test-risk-control",
        ),
        kill_switch_observed_at=NOW - timedelta(seconds=2),
        kill_switch_available_at=NOW - timedelta(seconds=1),
        intent=order_intent(quantity=Decimal("0.1000")),
        state=order_state(cash_balance=Decimal("20000.00")),
        price_evidence=price_evidence(
            reference_price=Decimal("50000.0"),
            observed_at=(NOW - timedelta(seconds=10)).astimezone(offset),
            available_at=(NOW - timedelta(seconds=9)).astimezone(offset),
        ),
    )

    assert normalized.rule_set.rules_hash == request.rule_set.rules_hash
    assert normalized.request_hash == request.request_hash
    assert evaluate_order_risk(normalized).decision_hash == decision.decision_hash
    assert public_canonical_payload_hash(request.model_dump(mode="python")) == (
        canonical_payload_hash(request.model_dump(mode="python"))
    )


@pytest.mark.parametrize(
    "factory",
    (
        lambda: order_limits(maximum_order_notional=10000.0),
        lambda: order_limits(order_fee_buffer_ratio=0.001),
        lambda: order_intent(quantity=0.1),
        lambda: order_state(cash_balance=20000.0),
        lambda: price_evidence(reference_price=50000.0),
        lambda: order_limits(maximum_order_notional=Decimal("NaN")),
        lambda: order_intent(quantity=Decimal("Infinity")),
        lambda: order_state(position_quantity=Decimal("-Infinity")),
        lambda: price_evidence(reference_price=Decimal("NaN")),
    ),
)
def test_order_economic_contracts_reject_float_and_non_finite_values(
    factory: object,
) -> None:
    with pytest.raises(ValidationError, match="exact finite decimal"):
        factory()  # type: ignore[operator]


def test_order_rule_request_and_decision_fail_closed_on_tampering() -> None:
    rules = build_order_rule_set(order_limits(), rule_set_version="1.2.3")
    rules_payload = rules.model_dump(mode="python")
    rules_payload["order_limits"]["maximum_active_orders"] = 4
    with pytest.raises(ValidationError, match="rules_hash"):
        OrderRiskRuleSet.model_validate(rules_payload)

    request = order_request()
    request_payload = request.model_dump(mode="python")
    request_payload["state"]["cash_balance"] = Decimal("1")
    with pytest.raises(ValidationError, match="request_hash"):
        OrderRiskRequest.model_validate(request_payload)

    decision = evaluate_order_risk(request)
    decision_payload = decision.model_dump(mode="python")
    decision_payload["decision_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="decision_hash"):
        RiskDecision.model_validate(decision_payload)


def test_order_rule_version_and_evidence_hashes_change_decision_identity() -> None:
    first = evaluate_order_risk(order_request(rule_set_version="1.0.0"))
    second = evaluate_order_risk(order_request(rule_set_version="1.0.1"))
    changed_account = evaluate_order_risk(
        order_request(state=order_state(account_state_hash="c" * 64))
    )
    changed_price = evaluate_order_risk(order_request(price=price_evidence(snapshot_hash="d" * 64)))

    assert first.request.rule_set.rules_hash != second.request.rule_set.rules_hash
    assert first.request.request_hash != second.request.request_hash
    assert first.decision_hash != second.decision_hash
    assert changed_account.decision_hash != first.decision_hash
    assert changed_price.decision_hash != first.decision_hash


def test_market_buffer_reservations_and_limits_reject_without_clipping() -> None:
    decision = evaluate_order_risk(
        order_request(
            limits=order_limits(maximum_order_notional=Decimal("5000")),
            state=order_state(
                cash_balance=Decimal("20000"),
                reserved_buy_cash=Decimal("14950"),
                reserved_buy_quantity=Decimal("7.95"),
                active_order_count=5,
            ),
        )
    )

    assert decision.decision == "reject"
    assert decision.effective_weights is None
    assert finding_codes(decision) == (
        "MAXIMUM_ACTIVE_ORDERS_EXCEEDED",
        "MAXIMUM_RESULTING_POSITION_EXCEEDED",
        "MAXIMUM_ORDER_NOTIONAL_EXCEEDED",
        "INSUFFICIENT_AVAILABLE_CASH",
        "MINIMUM_CASH_RESERVE_BREACH",
    )
    assert decision.findings[2].actual == "5100"
    assert all(finding.action == "reject" for finding in decision.findings)


def test_limit_order_uses_limit_price_without_market_buffer() -> None:
    request = order_request(
        limits=order_limits(maximum_order_notional=Decimal("5000")),
        intent=order_intent(
            order_type="limit",
            limit_price=Decimal("50000"),
        ),
    )

    assert evaluate_order_risk(request).decision == "allow"


def test_limit_buy_cash_floor_includes_the_exact_fee_buffer() -> None:
    decision = evaluate_order_risk(
        order_request(
            limits=order_limits(
                maximum_order_notional=Decimal("5000"),
                minimum_cash_reserve=Decimal("100"),
                order_fee_buffer_ratio=Decimal("0.01"),
            ),
            intent=order_intent(
                order_type="limit",
                limit_price=Decimal("50000"),
            ),
            state=order_state(cash_balance=Decimal("5100")),
        )
    )

    assert decision.decision == "reject"
    assert finding_codes(decision) == ("MINIMUM_CASH_RESERVE_BREACH",)
    assert decision.findings[0].actual == "50"
    assert decision.findings[0].limit == "100"


def test_sell_limit_notional_uses_at_least_the_reference_price() -> None:
    decision = evaluate_order_risk(
        order_request(
            intent=order_intent(
                side="sell",
                order_type="limit",
                quantity=Decimal("0.3"),
                limit_price=Decimal("1"),
            )
        )
    )

    assert decision.decision == "reject"
    assert finding_codes(decision) == ("MAXIMUM_ORDER_NOTIONAL_EXCEEDED",)
    assert decision.findings[0].actual == "15000"


def test_sell_orders_respect_existing_position_reservations() -> None:
    decision = evaluate_order_risk(
        order_request(
            limits=order_limits(maximum_order_notional=Decimal("100000")),
            intent=order_intent(side="sell", quantity=Decimal("0.6")),
            state=order_state(
                position_quantity=Decimal("2"),
                reserved_sell_quantity=Decimal("1.5"),
            ),
        )
    )

    assert decision.decision == "reject"
    assert finding_codes(decision) == ("INSUFFICIENT_AVAILABLE_POSITION",)
    assert decision.findings[0].actual == "0.5"
    assert decision.findings[0].limit == "0.6"


@pytest.mark.parametrize(
    ("state", "price", "expected_code"),
    (
        (
            order_state(symbol="ETHUSDT"),
            price_evidence(),
            "ACCOUNT_POSITION_SYMBOL_MISMATCH",
        ),
        (
            order_state(quote_currency="USD"),
            price_evidence(),
            "ACCOUNT_QUOTE_CURRENCY_MISMATCH",
        ),
        (
            order_state(),
            price_evidence(symbol="ETHUSDT"),
            "PRICE_SYMBOL_MISMATCH",
        ),
        (
            order_state(),
            price_evidence(quote_currency="USD"),
            "PRICE_QUOTE_CURRENCY_MISMATCH",
        ),
    ),
)
def test_order_state_and_price_evidence_are_bound_to_the_instrument(
    state: OrderRiskState,
    price: OrderPriceEvidence,
    expected_code: str,
) -> None:
    decision = evaluate_order_risk(order_request(state=state, price=price))

    assert decision.decision == "reject"
    assert expected_code in finding_codes(decision)


@pytest.mark.parametrize(
    ("price", "expected_codes"),
    (
        (
            price_evidence(
                observed_at=NOW - timedelta(seconds=61),
                available_at=NOW - timedelta(seconds=60),
            ),
            ("REFERENCE_PRICE_STALE",),
        ),
        (
            price_evidence(
                observed_at=NOW + timedelta(seconds=1),
                available_at=NOW + timedelta(seconds=2),
            ),
            ("PRICE_OBSERVATION_IN_FUTURE", "PRICE_NOT_AVAILABLE"),
        ),
        (
            price_evidence(
                observed_at=NOW - timedelta(seconds=1),
                available_at=NOW + timedelta(seconds=1),
            ),
            ("PRICE_NOT_AVAILABLE",),
        ),
        (
            price_evidence(
                observed_at=NOW - timedelta(seconds=1),
                available_at=NOW - timedelta(seconds=2),
            ),
            ("PRICE_TIMELINE_INVALID",),
        ),
    ),
)
def test_price_evidence_fails_closed(
    price: OrderPriceEvidence,
    expected_codes: tuple[str, ...],
) -> None:
    decision = evaluate_order_risk(order_request(price=price))

    assert decision.decision == "reject"
    assert finding_codes(decision) == expected_codes


@pytest.mark.parametrize(
    ("status", "expected_code"),
    (
        ("engaged", "KILL_SWITCH_ENGAGED"),
        ("unavailable", "KILL_SWITCH_STATE_UNAVAILABLE"),
    ),
)
def test_order_kill_switch_fails_closed_first(
    status: str,
    expected_code: str,
) -> None:
    decision = evaluate_order_risk(order_request(kill_switch_status=status))

    assert decision.decision == "reject"
    assert decision.findings[0].code == expected_code
    assert decision.findings[0].severity == "critical"


def test_account_kill_switch_scope_must_match_the_account_identity() -> None:
    matching = evaluate_order_risk(
        order_request(
            kill_switch_scope="account",
            kill_switch_scope_id_hash=ACCOUNT_ID_HASH,
        )
    )
    mismatched = evaluate_order_risk(
        order_request(
            kill_switch_scope="account",
            kill_switch_scope_id_hash="8" * 64,
        )
    )
    unsupported = evaluate_order_risk(
        order_request(
            kill_switch_scope="venue",
            kill_switch_scope_id_hash="7" * 64,
        )
    )

    assert matching.decision == "allow"
    assert finding_codes(mismatched) == ("KILL_SWITCH_SCOPE_MISMATCH",)
    assert finding_codes(unsupported) == ("KILL_SWITCH_SCOPE_UNVERIFIABLE",)


@pytest.mark.parametrize(
    ("observed_at", "available_at", "expected_codes"),
    (
        (
            NOW - timedelta(seconds=31),
            NOW - timedelta(seconds=30),
            ("KILL_SWITCH_STATE_STALE",),
        ),
        (
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=2),
            ("KILL_SWITCH_OBSERVATION_IN_FUTURE", "KILL_SWITCH_NOT_AVAILABLE"),
        ),
        (
            NOW - timedelta(seconds=1),
            NOW + timedelta(seconds=1),
            ("KILL_SWITCH_NOT_AVAILABLE",),
        ),
        (
            NOW - timedelta(seconds=1),
            NOW - timedelta(seconds=2),
            ("KILL_SWITCH_TIMELINE_INVALID",),
        ),
    ),
)
def test_clear_kill_switch_evidence_must_be_current_and_available(
    observed_at: datetime,
    available_at: datetime,
    expected_codes: tuple[str, ...],
) -> None:
    decision = evaluate_order_risk(
        order_request(
            kill_switch_observed_at=observed_at,
            kill_switch_available_at=available_at,
        )
    )

    assert decision.decision == "reject"
    assert finding_codes(decision) == expected_codes


def test_order_v1_rejects_configuration_that_claims_shorting_support() -> None:
    with pytest.raises(ValidationError, match="Input should be True"):
        order_limits(long_only=False)


def test_order_decision_contract_cannot_expose_weights_or_clipping() -> None:
    allowed = evaluate_order_risk(order_request())
    payload = allowed.model_dump(mode="python")
    payload["effective_weights"] = [{"symbol": "BTCUSDT", "weight": 1.0}]
    payload["decision_hash"] = canonical_payload_hash(
        {key: value for key, value in payload.items() if key != "decision_hash"}
    )

    with pytest.raises(ValidationError, match="Order intent decisions"):
        RiskDecision.model_validate(payload)


def test_legacy_research_and_allocation_hashes_remain_byte_compatible() -> None:
    switch = build_kill_switch_snapshot(
        status="clear",
        revision=7,
        source="test-fixture",
    )
    research_rules = build_research_rule_set(ResearchRiskLimits(minimum_trades=5))
    research_request = build_research_risk_request(
        evaluation_id="research-evaluation",
        evaluated_at=datetime(2026, 7, 29, 8, 30, tzinfo=UTC),
        source_calculation_version="backtest-v1",
        rule_set=research_rules,
        kill_switch=switch,
        evidence=ResearchEvidence(
            trades=12,
            trades_per_year=4.0,
            exposure_ratio=0.6,
            annualized_return=0.15,
            max_drawdown=-0.12,
            max_cash_streak_ratio=0.2,
            max_cash_streak_bars=8,
        ),
    )
    research_decision = evaluate_research_risk(research_request)

    allocation_rules = build_allocation_rule_set(AllocationRiskLimits())
    allocation_request = build_allocation_risk_request(
        evaluation_id="allocation-evaluation",
        evaluated_at=datetime(2026, 7, 29, 8, 30, tzinfo=UTC),
        source_calculation_version="portfolio-forward-target-v1",
        rule_set=allocation_rules,
        kill_switch=switch,
        intent=AllocationIntent(
            symbols=("BTCUSDT", "ETHUSDT"),
            requested_weights={"BTCUSDT": 0.5, "ETHUSDT": 0.5},
        ),
    )
    allocation_decision = evaluate_allocation_risk(allocation_request)

    assert research_rules.rules_hash == (
        "236089d552b0827ede55fb2de4673a5ef30ddde882964e2c8509d8ffc4ecf9e4"
    )
    assert research_request.request_hash == (
        "0d0a0e8b926abc794c0767c3adad568e5f6f8440b4ea2afafbac78d61138262e"
    )
    assert research_decision.decision_hash == (
        "ff4aa4a616e7d24a840989cd40e237cc479be8930a25155788d06322d7fd6c42"
    )
    assert allocation_rules.rules_hash == (
        "c27d9f435eac66e8c0ee62da2e4697f20211423d3d046233644779edd6d19429"
    )
    assert allocation_request.request_hash == (
        "1379aacce9c9fbf0f9fb319c7cc1c0b007f70c3439cda3d7da85f422e82dc19e"
    )
    assert allocation_decision.decision_hash == (
        "4f298bfe21aaf808fb3ecd2fbd1c384f3935a6ee60cf399f793aef0b1c2d11c8"
    )

    research_payload = research_decision.model_dump(mode="python")
    allocation_payload = allocation_decision.model_dump(mode="python")
    expected_rule_keys = {
        "schema_version",
        "evaluation_kind",
        "rule_set_id",
        "rule_set_version",
        "compatibility_profile",
        "research_limits",
        "allocation_limits",
        "rules_hash",
    }
    expected_request_keys = {
        "schema_version",
        "evaluation_kind",
        "evaluation_id",
        "evaluated_at",
        "source_calculation_version",
        "rule_set",
        "kill_switch",
        "request_hash",
    }
    expected_decision_keys = {
        "schema_version",
        "request",
        "decision",
        "effective_weights",
        "findings",
        "decision_hash",
    }
    assert set(research_payload["request"]["rule_set"]) == {
        *expected_rule_keys,
    }
    assert set(allocation_payload["request"]["rule_set"]) == expected_rule_keys
    assert set(research_payload) == expected_decision_keys
    assert set(allocation_payload) == expected_decision_keys
    assert set(research_payload["request"]) == expected_request_keys | {"evidence"}
    assert set(allocation_payload["request"]) == expected_request_keys | {"intent"}
    assert RiskRuleSet.model_validate_json(research_rules.model_dump_json()) == research_rules
    assert RiskRuleSet.model_validate_json(allocation_rules.model_dump_json()) == allocation_rules
    assert (
        ResearchRiskRequest.model_validate_json(research_request.model_dump_json())
        == research_request
    )
    assert (
        AllocationRiskRequest.model_validate_json(allocation_request.model_dump_json())
        == allocation_request
    )
    assert (
        RiskDecision.model_validate_json(research_decision.model_dump_json()) == research_decision
    )
    assert (
        RiskDecision.model_validate_json(allocation_decision.model_dump_json())
        == allocation_decision
    )
    assert RiskDecision.model_validate(research_payload) == research_decision
    assert RiskDecision.model_validate(allocation_payload) == allocation_decision
