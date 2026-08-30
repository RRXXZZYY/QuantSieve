from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from quantsieve_engine.risk import (
    AllocationIntent,
    AllocationRiskLimits,
    AllocationRiskRequest,
    AssetWeight,
    KillSwitchSnapshot,
    ResearchEvidence,
    ResearchRiskLimits,
    ResearchRiskRequest,
    RiskDecision,
    RiskRuleSet,
    build_allocation_risk_request,
    build_allocation_rule_set,
    build_kill_switch_snapshot,
    build_research_risk_request,
    build_research_rule_set,
    canonical_json,
    canonical_payload_hash,
    evaluate_allocation_risk,
    evaluate_research_risk,
)

NOW = datetime(2026, 7, 29, 8, 30, tzinfo=UTC)


def clear_switch() -> KillSwitchSnapshot:
    return build_kill_switch_snapshot(
        status="clear",
        revision=7,
        source="test-fixture",
    )


def research_evidence(**changes: object) -> ResearchEvidence:
    values: dict[str, object] = {
        "trades": 12,
        "trades_per_year": 4.0,
        "exposure_ratio": 0.6,
        "annualized_return": 0.15,
        "max_drawdown": -0.12,
        "max_cash_streak_ratio": 0.2,
        "max_cash_streak_bars": 8,
    }
    values.update(changes)
    return ResearchEvidence.model_validate(values)


def research_request(
    evidence: ResearchEvidence,
    limits: ResearchRiskLimits,
    *,
    kill_switch: KillSwitchSnapshot | None = None,
    evaluation_id: str = "research-evaluation",
) -> ResearchRiskRequest:
    return build_research_risk_request(
        evaluation_id=evaluation_id,
        evaluated_at=NOW,
        source_calculation_version="backtest-v1",
        rule_set=build_research_rule_set(limits),
        kill_switch=kill_switch or clear_switch(),
        evidence=evidence,
    )


def allocation_request(
    intent: AllocationIntent,
    limits: AllocationRiskLimits | None = None,
    *,
    kill_switch: KillSwitchSnapshot | None = None,
    evaluation_id: str = "allocation-evaluation",
) -> AllocationRiskRequest:
    return build_allocation_risk_request(
        evaluation_id=evaluation_id,
        evaluated_at=NOW,
        source_calculation_version="portfolio-forward-target-v1",
        rule_set=build_allocation_rule_set(limits or AllocationRiskLimits()),
        kill_switch=kill_switch or clear_switch(),
        intent=intent,
    )


def valid_allocation(**changes: object) -> AllocationIntent:
    values: dict[str, object] = {
        "symbols": ("BTCUSDT", "ETHUSDT"),
        "requested_weights": {"BTCUSDT": 0.5, "ETHUSDT": 0.5},
    }
    values.update(changes)
    return AllocationIntent.model_validate(values)


def test_canonical_hash_is_order_independent_normalized_and_strict() -> None:
    offset = timezone(timedelta(hours=8))
    left = {
        "z": [Decimal("1.2300"), -0.0],
        "a": datetime(2026, 7, 29, 16, 30, tzinfo=offset),
    }
    right = {
        "a": NOW,
        "z": [Decimal("1.23"), 0.0],
    }

    assert canonical_json(left) == canonical_json(right)
    assert canonical_payload_hash(left) == canonical_payload_hash(right)
    assert len(canonical_payload_hash(left)) == 64
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json({"bad": float("nan")})
    with pytest.raises(ValueError, match="timezone-aware"):
        canonical_json({"bad": datetime(2026, 7, 29)})
    with pytest.raises(TypeError, match="string keys"):
        canonical_json({1: "bad"})


def test_contracts_are_frozen_extra_forbidden_and_finite() -> None:
    weight = AssetWeight(symbol=" btcusdt ", weight=0.5)

    assert weight.symbol == "BTCUSDT"
    with pytest.raises(ValidationError):
        weight.weight = 0.6
    with pytest.raises(ValidationError, match="finite"):
        AssetWeight(symbol="BTCUSDT", weight=float("inf"))
    with pytest.raises(ValidationError, match="extra"):
        ResearchEvidence.model_validate(
            {
                **research_evidence().model_dump(mode="python"),
                "unknown": True,
            }
        )
    with pytest.raises(ValidationError, match="finite"):
        research_evidence(annualized_return=float("nan"))
    with pytest.raises(ValueError, match="timezone-aware"):
        build_research_risk_request(
            evaluation_id="naive",
            evaluated_at=datetime(2026, 7, 29),
            source_calculation_version="backtest-v1",
            rule_set=build_research_rule_set(ResearchRiskLimits()),
            kill_switch=clear_switch(),
            evidence=research_evidence(),
        )


def test_rule_switch_request_and_decision_hashes_fail_closed_on_tampering() -> None:
    rules = build_research_rule_set(
        ResearchRiskLimits(minimum_trades=5),
        rule_set_version="1.2.3",
    )
    rules_payload = rules.model_dump(mode="python")
    rules_payload["research_limits"]["minimum_trades"] = 6
    with pytest.raises(ValidationError, match="rules_hash"):
        RiskRuleSet.model_validate(rules_payload)

    switch = clear_switch()
    switch_payload = switch.model_dump(mode="python")
    switch_payload["revision"] = 8
    with pytest.raises(ValidationError, match="state_hash"):
        KillSwitchSnapshot.model_validate(switch_payload)

    request = research_request(
        research_evidence(),
        ResearchRiskLimits(minimum_trades=5),
    )
    request_payload = request.model_dump(mode="python")
    request_payload["evidence"]["trades"] = 4
    with pytest.raises(ValidationError, match="request_hash"):
        ResearchRiskRequest.model_validate(request_payload)

    decision = evaluate_research_risk(request)
    decision_payload = decision.model_dump(mode="python")
    decision_payload["decision_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="decision_hash"):
        RiskDecision.model_validate(decision_payload)


def test_rule_version_is_independent_evidence_and_changes_decision_identity() -> None:
    limits = ResearchRiskLimits(minimum_trades=5)
    first_rules = build_research_rule_set(limits, rule_set_version="1.0.0")
    second_rules = build_research_rule_set(limits, rule_set_version="1.0.1")
    evidence = research_evidence()
    first = evaluate_research_risk(
        build_research_risk_request(
            evaluation_id="same-logical-evaluation",
            evaluated_at=NOW,
            source_calculation_version="backtest-v1",
            rule_set=first_rules,
            kill_switch=clear_switch(),
            evidence=evidence,
        )
    )
    second = evaluate_research_risk(
        build_research_risk_request(
            evaluation_id="same-logical-evaluation",
            evaluated_at=NOW,
            source_calculation_version="backtest-v1",
            rule_set=second_rules,
            kill_switch=clear_switch(),
            evidence=evidence,
        )
    )

    assert first_rules.rules_hash != second_rules.rules_hash
    assert first.request.request_hash != second.request.request_hash
    assert first.decision_hash != second.decision_hash


def test_research_evaluator_reports_every_legacy_gate_in_stable_order() -> None:
    limits = ResearchRiskLimits(
        minimum_trades=10,
        maximum_trades_per_year=5,
        minimum_exposure=0.5,
        minimum_annualized_return=0.1,
        maximum_drawdown=0.2,
        maximum_cash_streak_ratio=0.3,
        maximum_cash_streak_bars=10,
    )
    evidence = research_evidence(
        trades=2,
        trades_per_year=8,
        exposure_ratio=0.2,
        annualized_return=-0.1,
        max_drawdown=-0.4,
        max_cash_streak_ratio=0.5,
        max_cash_streak_bars=20,
    )

    decision = evaluate_research_risk(research_request(evidence, limits))

    assert decision.decision == "reject"
    assert decision.effective_weights is None
    assert tuple(finding.sequence for finding in decision.findings) == tuple(range(7))
    assert tuple(finding.code for finding in decision.findings) == (
        "MIN_TRADES_NOT_MET",
        "MAX_TRADES_PER_YEAR_EXCEEDED",
        "MIN_EXPOSURE_NOT_MET",
        "MIN_ANNUALIZED_RETURN_NOT_MET",
        "MAX_DRAWDOWN_EXCEEDED",
        "MAX_CASH_STREAK_RATIO_EXCEEDED",
        "MAX_CASH_STREAK_BARS_EXCEEDED",
    )
    assert all(finding.action == "reject" for finding in decision.findings)
    assert decision.findings[4].actual == "0.4"
    assert decision.findings[4].limit == "0.2"
    assert evaluate_research_risk(decision.request).decision_hash == decision.decision_hash


def test_research_evaluator_matches_the_legacy_boolean_boundaries() -> None:
    limits = ResearchRiskLimits(
        minimum_trades=10,
        maximum_trades_per_year=5,
        minimum_exposure=0.5,
        minimum_annualized_return=0.1,
        maximum_drawdown=0.2,
        maximum_cash_streak_ratio=0.3,
        maximum_cash_streak_bars=10,
    )
    samples = (
        research_evidence(
            trades=10,
            trades_per_year=5,
            exposure_ratio=0.5,
            annualized_return=0.1,
            max_drawdown=-0.2,
            max_cash_streak_ratio=0.3,
            max_cash_streak_bars=10,
        ),
        research_evidence(trades=9),
        research_evidence(trades_per_year=5.01),
        research_evidence(exposure_ratio=0.49),
        research_evidence(annualized_return=0.09),
        research_evidence(max_drawdown=-0.21),
        research_evidence(max_cash_streak_ratio=0.31),
        research_evidence(max_cash_streak_bars=11),
    )

    for evidence in samples:
        legacy_participates = (
            evidence.trades >= limits.minimum_trades
            and (
                limits.maximum_trades_per_year is None
                or evidence.trades_per_year <= limits.maximum_trades_per_year
            )
            and evidence.exposure_ratio >= limits.minimum_exposure
            and (
                limits.minimum_annualized_return is None
                or evidence.annualized_return >= limits.minimum_annualized_return
            )
            and (
                limits.maximum_drawdown is None
                or abs(evidence.max_drawdown) <= limits.maximum_drawdown
            )
            and evidence.max_cash_streak_ratio <= limits.maximum_cash_streak_ratio
            and (
                limits.maximum_cash_streak_bars is None
                or evidence.max_cash_streak_bars <= limits.maximum_cash_streak_bars
            )
        )
        decision = evaluate_research_risk(research_request(evidence, limits))

        assert (decision.decision == "allow") is legacy_participates


def test_allocation_validate_only_is_map_order_independent_and_preserves_target() -> None:
    first_intent = AllocationIntent(
        symbols=(" btcusdt ", "ethusdt"),
        requested_weights={"ETHUSDT": 0.5, "BTCUSDT": 0.5},
    )
    second_intent = AllocationIntent(
        symbols=("BTCUSDT", "ETHUSDT"),
        requested_weights={"BTCUSDT": 0.5, "ETHUSDT": 0.5},
    )
    first_request = allocation_request(first_intent)
    second_request = allocation_request(second_intent)

    first = evaluate_allocation_risk(first_request)
    second = evaluate_allocation_risk(second_request)

    assert first_intent == second_intent
    assert first_request.request_hash == second_request.request_hash
    assert first.decision == "allow"
    assert first.findings == ()
    assert first.effective_weights == first_intent.requested_weights
    assert first.decision_hash == second.decision_hash


def test_allocation_evaluator_rejects_structure_and_exposure_without_clipping() -> None:
    intent = AllocationIntent(
        symbols=("A", "A", "B"),
        requested_weights=(
            {"symbol": "A", "weight": -0.2},
            {"symbol": "A", "weight": 0.6},
            {"symbol": "C", "weight": 0.9},
        ),
    )
    limits = AllocationRiskLimits(
        minimum_assets=2,
        maximum_assets=4,
        maximum_asset_weight=0.5,
    )

    decision = evaluate_allocation_risk(allocation_request(intent, limits))

    assert decision.decision == "reject"
    assert decision.effective_weights is None
    assert tuple((finding.code, finding.subject) for finding in decision.findings) == (
        ("DUPLICATE_ALLOCATION_SYMBOL", "A"),
        ("DUPLICATE_WEIGHT_SYMBOL", "A"),
        ("WEIGHT_SYMBOL_MISSING", "B"),
        ("WEIGHT_SYMBOL_UNEXPECTED", "C"),
        ("LONG_ONLY_VIOLATION", "A"),
        ("WEIGHT_SUM_MISMATCH", None),
        ("GROSS_EXPOSURE_EXCEEDED", None),
        ("NET_EXPOSURE_EXCEEDED", None),
        ("ASSET_WEIGHT_EXCEEDED", "A"),
        ("ASSET_WEIGHT_EXCEEDED", "C"),
    )
    assert all(finding.action == "reject" for finding in decision.findings)


def test_allocation_weight_cap_reports_infeasible_policy_and_each_breach() -> None:
    decision = evaluate_allocation_risk(
        allocation_request(
            valid_allocation(),
            AllocationRiskLimits(maximum_asset_weight=0.4),
        )
    )

    assert decision.decision == "reject"
    assert tuple((finding.code, finding.subject) for finding in decision.findings) == (
        ("ASSET_WEIGHT_CAP_INFEASIBLE", None),
        ("ASSET_WEIGHT_EXCEEDED", "BTCUSDT"),
        ("ASSET_WEIGHT_EXCEEDED", "ETHUSDT"),
    )


def test_optional_allocation_rules_fail_closed_on_missing_invalid_and_breached_state() -> None:
    limits = AllocationRiskLimits(
        maximum_turnover_ratio=0.2,
        minimum_cash_reserve_ratio=0.1,
        maximum_drawdown=0.25,
    )
    missing = evaluate_allocation_risk(
        allocation_request(valid_allocation(), limits, evaluation_id="missing")
    )
    invalid = evaluate_allocation_risk(
        allocation_request(
            valid_allocation(
                proposed_turnover_ratio=-0.1,
                post_trade_cash_ratio=1.2,
                current_drawdown=0.1,
            ),
            limits,
            evaluation_id="invalid",
        )
    )
    breached = evaluate_allocation_risk(
        allocation_request(
            valid_allocation(
                proposed_turnover_ratio=0.3,
                post_trade_cash_ratio=0.05,
                current_drawdown=-0.3,
            ),
            limits,
            evaluation_id="breached",
        )
    )

    assert tuple(finding.code for finding in missing.findings) == (
        "TURNOVER_EVIDENCE_MISSING",
        "CASH_RESERVE_EVIDENCE_MISSING",
        "DRAWDOWN_EVIDENCE_MISSING",
    )
    assert tuple(finding.code for finding in invalid.findings) == (
        "TURNOVER_RATIO_INVALID",
        "POST_TRADE_CASH_RATIO_INVALID",
        "DRAWDOWN_STATE_INVALID",
    )
    assert tuple(finding.code for finding in breached.findings) == (
        "MAX_TURNOVER_EXCEEDED",
        "CASH_FLOOR_BREACH",
        "DRAWDOWN_HALT",
    )
    assert breached.findings[-1].severity == "critical"


@pytest.mark.parametrize(
    ("status", "expected_code"),
    (
        ("engaged", "KILL_SWITCH_ENGAGED"),
        ("unavailable", "KILL_SWITCH_STATE_UNAVAILABLE"),
    ),
)
def test_kill_switch_fails_closed_before_allocation_rules(
    status: str,
    expected_code: str,
) -> None:
    switch = build_kill_switch_snapshot(
        status=status,  # type: ignore[arg-type]
        revision=11,
        reason_code="operator_stop" if status == "engaged" else "store_unreachable",
        activated_at=NOW if status == "engaged" else None,
        source="risk-control",
    )

    decision = evaluate_allocation_risk(allocation_request(valid_allocation(), kill_switch=switch))

    assert decision.decision == "reject"
    assert decision.effective_weights is None
    assert decision.findings[0].code == expected_code
    assert decision.findings[0].severity == "critical"
    assert decision.request.kill_switch.revision == 11


def test_kill_switch_contract_is_scoped_hashed_and_does_not_block_research() -> None:
    scope_hash = canonical_payload_hash("portfolio-opaque-id")
    engaged = build_kill_switch_snapshot(
        status="engaged",
        scope="portfolio",
        scope_id_hash=scope_hash,
        revision=12,
        reason_code="drawdown_limit",
        activated_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        source="risk-control",
    )
    decision = evaluate_research_risk(
        research_request(
            research_evidence(),
            ResearchRiskLimits(),
            kill_switch=engaged,
        )
    )

    assert engaged.scope_id_hash == scope_hash
    assert decision.decision == "allow"
    assert decision.findings == ()
    with pytest.raises(ValidationError, match="scope id"):
        build_kill_switch_snapshot(
            status="engaged",
            scope="portfolio",
            revision=1,
            reason_code="operator_stop",
            activated_at=NOW,
        )
    with pytest.raises(ValidationError, match="cannot carry activation"):
        build_kill_switch_snapshot(
            status="clear",
            reason_code="stale_reason",
        )


def test_decision_contract_rejects_executable_rejects_and_unordered_findings() -> None:
    rejected = evaluate_allocation_risk(
        allocation_request(
            valid_allocation(),
            AllocationRiskLimits(maximum_asset_weight=0.4),
        )
    )
    executable_payload = rejected.model_dump(mode="python")
    executable_payload["effective_weights"] = [
        {"symbol": "BTCUSDT", "weight": 0.5},
        {"symbol": "ETHUSDT", "weight": 0.5},
    ]
    with pytest.raises(ValidationError, match="cannot expose executable"):
        RiskDecision.model_validate(executable_payload)

    unordered_payload = rejected.model_dump(mode="python")
    unordered_payload["findings"] = list(reversed(unordered_payload["findings"]))
    unordered_payload["decision_hash"] = canonical_payload_hash(
        {key: value for key, value in unordered_payload.items() if key != "decision_hash"}
    )
    with pytest.raises(ValidationError, match="contiguous and ordered"):
        RiskDecision.model_validate(unordered_payload)
