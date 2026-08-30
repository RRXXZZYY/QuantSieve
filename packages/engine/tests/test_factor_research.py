from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from quantsieve_engine.factor_research import (
    FactorDiagnostics,
    FactorResearchPanel,
    FeatureObservation,
    LabelObservation,
    UniverseObservation,
    analyze_factor,
    build_factor_research_panel,
    build_feature_observation,
    build_label_observation,
    build_universe_observation,
)

PERIODS = (
    datetime(2026, 1, 31, 16, tzinfo=UTC),
    datetime(2026, 2, 28, 16, tzinfo=UTC),
    datetime(2026, 3, 31, 16, tzinfo=UTC),
)
INSTRUMENTS = tuple(f"security:xnas:test{index}" for index in range(7))
FEATURE_VALUES = (
    ("1", "2", "3", "4", "5", "6"),
    ("3", "1", "6", "2", "5", "4"),
    ("2", "5", "1", "6", "3", "4"),
)
LABEL_VALUES = (
    ("0.01", "0.03", "0.02", "0.05", "0.04", "0.08"),
    ("0.04", "-0.01", "0.08", "0.01", "0.03", "0.02"),
    ("0.01", "0.06", "-0.02", "0.04", "0.03", "0.02"),
)


def universe_point(
    instrument_uid: str,
    period_at: datetime,
    *,
    included: bool = True,
) -> UniverseObservation:
    return build_universe_observation(
        instrument_uid=instrument_uid,
        period_at=period_at,
        effective_at=period_at - timedelta(minutes=10),
        observed_at=period_at - timedelta(minutes=5),
        available_at=period_at - timedelta(minutes=1),
        included=included,
    )


def feature_point(
    instrument_uid: str,
    period_at: datetime,
    value: str,
) -> FeatureObservation:
    return build_feature_observation(
        instrument_uid=instrument_uid,
        period_at=period_at,
        effective_at=period_at - timedelta(minutes=10),
        observed_at=period_at - timedelta(minutes=5),
        available_at=period_at - timedelta(minutes=1),
        feature_name="quality.v1",
        value=Decimal(value),
    )


def label_point(
    instrument_uid: str,
    period_at: datetime,
    value: str,
) -> LabelObservation:
    effective_at = period_at + timedelta(days=20)
    return build_label_observation(
        instrument_uid=instrument_uid,
        period_at=period_at,
        effective_at=effective_at,
        observed_at=effective_at + timedelta(minutes=5),
        available_at=effective_at + timedelta(minutes=10),
        label_name="forward-return.20d",
        value=Decimal(value),
    )


def panel() -> FactorResearchPanel:
    universe = [
        universe_point(instrument_uid, period_at)
        for period_at in PERIODS
        for instrument_uid in INSTRUMENTS
    ]
    features = [
        feature_point(instrument_uid, period_at, FEATURE_VALUES[period_index][asset_index])
        for period_index, period_at in enumerate(PERIODS)
        for asset_index, instrument_uid in enumerate(INSTRUMENTS[:6])
    ]
    labels = [
        label_point(instrument_uid, period_at, LABEL_VALUES[period_index][asset_index])
        for period_index, period_at in enumerate(PERIODS)
        for asset_index, instrument_uid in enumerate(INSTRUMENTS[:6])
    ]
    return build_factor_research_panel(
        feature_name="quality.v1",
        label_name="forward-return.20d",
        universe=universe,
        features=features,
        labels=labels,
    )


def panel_with_feature_values(
    instruments: tuple[str, ...],
    feature_values: tuple[tuple[str, ...], ...],
) -> FactorResearchPanel:
    universe = [
        universe_point(instrument_uid, period_at)
        for period_at in PERIODS
        for instrument_uid in instruments
    ]
    features = [
        feature_point(
            instrument_uid,
            period_at,
            feature_values[period_index][asset_index],
        )
        for period_index, period_at in enumerate(PERIODS)
        for asset_index, instrument_uid in enumerate(instruments)
    ]
    labels = [
        label_point(
            instrument_uid,
            period_at,
            LABEL_VALUES[period_index][asset_index],
        )
        for period_index, period_at in enumerate(PERIODS)
        for asset_index, instrument_uid in enumerate(instruments)
    ]
    return build_factor_research_panel(
        feature_name="quality.v1",
        label_name="forward-return.20d",
        universe=universe,
        features=features,
        labels=labels,
    )


def test_panel_and_diagnostics_are_deterministic_across_input_order() -> None:
    first = panel()
    second = build_factor_research_panel(
        feature_name=first.feature_name,
        label_name=first.label_name,
        universe=tuple(reversed(first.universe)),
        features=tuple(reversed(first.features)),
        labels=tuple(reversed(first.labels)),
    )
    first_result = analyze_factor(first, quantile_count=3)
    second_result = analyze_factor(second, quantile_count=3)

    assert first.panel_id == second.panel_id
    assert first_result.diagnostics_id == second_result.diagnostics_id
    assert first_result.period_count == 3
    assert first_result.observation_count == 18
    assert first_result.coverage_rate == Decimal("0.857142857143")
    assert len(first_result.quantile_mean_returns) == 3
    assert first_result.periods[0].long_short_turnover is None
    assert all(item.long_short_turnover is not None for item in first_result.periods[1:])
    assert Decimal("-1") <= first_result.quantile_monotonicity <= Decimal("1")


def test_point_in_time_timestamps_normalize_to_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    period_at = datetime(2026, 1, 31, 11, tzinfo=eastern)
    first = universe_point(INSTRUMENTS[0], period_at)
    second = universe_point(INSTRUMENTS[0], PERIODS[0])

    assert first.observation_id == second.observation_id
    assert first.period_at == PERIODS[0]


def test_feature_and_universe_reject_lookahead_and_bad_time_order() -> None:
    period_at = PERIODS[0]
    with pytest.raises(ValidationError, match="lookahead"):
        build_feature_observation(
            instrument_uid=INSTRUMENTS[0],
            period_at=period_at,
            effective_at=period_at - timedelta(minutes=2),
            observed_at=period_at - timedelta(minutes=1),
            available_at=period_at + timedelta(microseconds=1),
            feature_name="quality.v1",
            value=Decimal("1"),
        )
    with pytest.raises(ValidationError, match="observed_at"):
        build_universe_observation(
            instrument_uid=INSTRUMENTS[0],
            period_at=period_at,
            effective_at=period_at - timedelta(minutes=2),
            observed_at=period_at,
            available_at=period_at - timedelta(minutes=1),
            included=True,
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        universe_point(INSTRUMENTS[0], period_at.replace(tzinfo=None))


def test_forward_label_must_be_realized_after_decision_and_observed_in_order() -> None:
    period_at = PERIODS[0]
    with pytest.raises(ValidationError, match="later than period_at"):
        build_label_observation(
            instrument_uid=INSTRUMENTS[0],
            period_at=period_at,
            effective_at=period_at,
            observed_at=period_at + timedelta(minutes=1),
            available_at=period_at + timedelta(minutes=2),
            label_name="forward-return.20d",
            value=Decimal("0.01"),
        )
    with pytest.raises(ValidationError, match="later than observed_at"):
        build_label_observation(
            instrument_uid=INSTRUMENTS[0],
            period_at=period_at,
            effective_at=period_at + timedelta(days=2),
            observed_at=period_at + timedelta(days=1),
            available_at=period_at + timedelta(days=3),
            label_name="forward-return.20d",
            value=Decimal("0.01"),
        )


def test_panel_rejects_duplicate_keys_and_missing_pair() -> None:
    original = panel()
    with pytest.raises(ValidationError, match="cannot be repeated"):
        build_factor_research_panel(
            feature_name=original.feature_name,
            label_name=original.label_name,
            universe=(*original.universe, original.universe[0]),
            features=original.features,
            labels=original.labels,
        )
    with pytest.raises(ValidationError, match="must be paired"):
        build_factor_research_panel(
            feature_name=original.feature_name,
            label_name=original.label_name,
            universe=original.universe,
            features=original.features,
            labels=original.labels[:-1],
        )


def test_panel_rejects_samples_outside_included_universe() -> None:
    original = panel()
    first_membership = original.universe[0]
    excluded = universe_point(
        first_membership.instrument_uid,
        first_membership.period_at,
        included=False,
    )
    with pytest.raises(ValidationError, match="included universe"):
        build_factor_research_panel(
            feature_name=original.feature_name,
            label_name=original.label_name,
            universe=(excluded, *original.universe[1:]),
            features=original.features,
            labels=original.labels,
        )


@pytest.mark.parametrize("bad_value", [Decimal("NaN"), Decimal("Infinity")])
def test_non_finite_values_fail_closed(bad_value: Decimal) -> None:
    with pytest.raises(ValidationError, match="finite"):
        build_feature_observation(
            instrument_uid=INSTRUMENTS[0],
            period_at=PERIODS[0],
            effective_at=PERIODS[0] - timedelta(minutes=3),
            observed_at=PERIODS[0] - timedelta(minutes=2),
            available_at=PERIODS[0] - timedelta(minutes=1),
            feature_name="quality.v1",
            value=bad_value,
        )


def test_strict_contract_rejects_floats_extra_fields_and_mutation() -> None:
    with pytest.raises(ValidationError, match="Decimal"):
        build_feature_observation(
            instrument_uid=INSTRUMENTS[0],
            period_at=PERIODS[0],
            effective_at=PERIODS[0] - timedelta(minutes=3),
            observed_at=PERIODS[0] - timedelta(minutes=2),
            available_at=PERIODS[0] - timedelta(minutes=1),
            feature_name="quality.v1",
            value=1.0,  # type: ignore[arg-type]
        )
    original = feature_point(INSTRUMENTS[0], PERIODS[0], "1")
    payload = original.model_dump(mode="python")
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        FeatureObservation.model_validate(payload)
    with pytest.raises(ValidationError, match="frozen"):
        original.value = Decimal("2")


def test_constant_factor_and_insufficient_quantile_samples_fail_closed() -> None:
    original = panel()
    constant_features = [
        feature_point(item.instrument_uid, item.period_at, "1") for item in original.features
    ]
    constant_panel = build_factor_research_panel(
        feature_name=original.feature_name,
        label_name=original.label_name,
        universe=original.universe,
        features=constant_features,
        labels=original.labels,
    )
    with pytest.raises(ValueError, match="Constant factor"):
        analyze_factor(constant_panel, quantile_count=3)
    with pytest.raises(ValueError, match="Insufficient samples"):
        analyze_factor(original, quantile_count=7)


def test_quantile_diagnostics_are_invariant_to_renaming_within_tie_groups() -> None:
    feature_values = (
        ("1", "1", "2", "2", "3", "3"),
        ("1", "1", "2", "2", "3", "3"),
        ("1", "1", "2", "2", "3", "3"),
    )
    renamed_instruments = (
        "security:xnas:zeta0",
        "security:xnas:alpha0",
        "security:xnas:zeta1",
        "security:xnas:alpha1",
        "security:xnas:zeta2",
        "security:xnas:alpha2",
    )

    original = analyze_factor(
        panel_with_feature_values(INSTRUMENTS[:6], feature_values),
        quantile_count=3,
    )
    renamed = analyze_factor(
        panel_with_feature_values(renamed_instruments, feature_values),
        quantile_count=3,
    )
    original_payload = original.model_dump(mode="python")
    renamed_payload = renamed.model_dump(mode="python")
    for payload in (original_payload, renamed_payload):
        payload.pop("diagnostics_id")
        payload.pop("panel_id")

    assert renamed_payload == original_payload


@pytest.mark.parametrize(
    "instruments",
    [
        INSTRUMENTS[:6],
        (
            "security:xnas:zeta0",
            "security:xnas:alpha0",
            "security:xnas:zeta1",
            "security:xnas:alpha1",
            "security:xnas:zeta2",
            "security:xnas:alpha2",
        ),
    ],
)
def test_quantile_diagnostics_reject_ties_crossing_a_boundary(
    instruments: tuple[str, ...],
) -> None:
    crossing_feature_values = (
        ("1", "2", "2", "3", "4", "5"),
        ("1", "2", "2", "3", "4", "5"),
        ("1", "2", "2", "3", "4", "5"),
    )
    research_panel = panel_with_feature_values(
        instruments,
        crossing_feature_values,
    )

    with pytest.raises(ValueError, match="cross a quantile boundary"):
        analyze_factor(research_panel, quantile_count=3)


def test_observation_panel_and_diagnostics_detect_tampering() -> None:
    original_panel = panel()
    original_feature = original_panel.features[0]
    feature_payload = original_feature.model_dump(mode="python")
    feature_payload["value"] = Decimal("999")
    with pytest.raises(ValidationError, match="observation_id"):
        FeatureObservation.model_validate(feature_payload)

    panel_payload = original_panel.model_dump(mode="python")
    panel_payload["feature_name"] = "value.v2"
    with pytest.raises(ValidationError, match=r"feature_name|panel_id"):
        FactorResearchPanel.model_validate(panel_payload)

    result = analyze_factor(original_panel, quantile_count=3)
    result_payload = result.model_dump(mode="python")
    result_payload["average_turnover"] = Decimal("0")
    with pytest.raises(ValidationError, match="diagnostics_id"):
        FactorDiagnostics.model_validate(result_payload)


def test_research_panel_and_results_are_frozen() -> None:
    original = panel()
    result = analyze_factor(original, quantile_count=3)

    with pytest.raises(ValidationError, match="frozen"):
        original.feature_name = "other"
    with pytest.raises(ValidationError, match="frozen"):
        result.coverage_rate = Decimal("1")
