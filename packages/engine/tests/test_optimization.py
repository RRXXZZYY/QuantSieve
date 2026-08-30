from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
import quantsieve_engine.optimization as optimization_module
from quantsieve_engine import (
    MIN_DEVELOPMENT_SELECTION_CLOSED_TRADES,
    BacktestConfig,
    BacktestMetrics,
    adaptive_screening_parameter_candidates,
    is_development_selection_eligible,
    is_forward_observation_eligible,
    optimize_strategy,
    parameter_candidates,
    run_backtest,
    screening_parameter_candidates,
    strategy_signals,
    validate_holdout_detailed,
)
from quantsieve_engine.optimization import _fold_boundaries, _participates, _score
from quantsieve_engine.risk import (
    ResearchEvidence,
    ResearchRiskLimits,
    build_kill_switch_snapshot,
    build_research_risk_request,
    build_research_rule_set,
    evaluate_research_risk,
)

RISK_EVALUATED_AT = datetime(2000, 1, 1, tzinfo=UTC)


def oscillating_data(periods: int = 240) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=periods, freq="D")
    trend = np.linspace(100, 145, periods)
    close = pd.Series(trend + np.sin(np.arange(periods) / 4) * 8, index=index)
    return pd.DataFrame(
        {
            "open": close * 0.998,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1_000_000 + np.arange(periods) * 100,
        },
        index=index,
    )


def ranking_metrics(
    *,
    total_return: float,
    annualized_return: float,
    duration_years: float,
    annualized_return_capped: bool = False,
    sharpe_ratio: float = 1.0,
    max_drawdown: float = -0.1,
) -> BacktestMetrics:
    return BacktestMetrics(
        bars=2,
        duration_years=duration_years,
        total_return=total_return,
        annualized_return=annualized_return,
        annualized_return_capped=annualized_return_capped,
        annualized_volatility=0.2,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_drawdown,
        win_rate=0.5,
        trades=2,
        closed_trades=1,
        trades_per_year=2 / duration_years,
        average_holding_bars=1,
        profit_factor=1.5,
        exposure_ratio=0.5,
        max_cash_streak=0,
        max_cash_streak_ratio=0,
    )


@pytest.mark.parametrize(
    "objective",
    ["total_return", "sharpe_ratio", "drawdown_control", "balanced"],
)
def test_same_window_score_is_not_driven_by_two_bar_annualization_cap(
    objective: str,
) -> None:
    duration_years = 15 / (365.2425 * 24 * 60)
    capped = ranking_metrics(
        total_return=1.0,
        annualized_return=1_000_000,
        annualized_return_capped=True,
        duration_years=duration_years,
    )
    same_realized_evidence = capped.model_copy(
        update={
            "annualized_return": 0.15,
            "annualized_return_capped": False,
        }
    )
    lower_realized_return = capped.model_copy(update={"total_return": 0.8})
    benchmark = capped.model_copy(
        update={
            "total_return": 0.25,
            "annualized_return": 1_000_000,
        }
    )
    exposure_matched = capped.model_copy(
        update={
            "total_return": 0.4,
            "annualized_return": 1_000_000,
        }
    )

    capped_score = _score(capped, objective, benchmark, exposure_matched)

    assert np.isfinite(capped_score)
    assert abs(capped_score) < 1_000
    assert capped_score == pytest.approx(
        _score(same_realized_evidence, objective, benchmark, exposure_matched)
    )
    assert capped_score > _score(
        lower_realized_return,
        objective,
        benchmark,
        exposure_matched,
    )


@pytest.mark.parametrize(
    ("duration_years", "strong_total", "strong_annual", "weak_total", "weak_annual"),
    [
        (1.0, 0.12, 0.12, 0.08, 0.08),
        (3.0, 0.404928, 0.12, 0.259712, 0.08),
    ],
)
@pytest.mark.parametrize(
    "objective",
    ["total_return", "sharpe_ratio", "drawdown_control", "balanced"],
)
def test_same_window_score_preserves_normal_one_and_multi_year_ordering(
    duration_years: float,
    strong_total: float,
    strong_annual: float,
    weak_total: float,
    weak_annual: float,
    objective: str,
) -> None:
    strong = ranking_metrics(
        total_return=strong_total,
        annualized_return=strong_annual,
        duration_years=duration_years,
    )
    weak = ranking_metrics(
        total_return=weak_total,
        annualized_return=weak_annual,
        duration_years=duration_years,
    )
    benchmark = ranking_metrics(
        total_return=0.03,
        annualized_return=0.03,
        duration_years=duration_years,
    )

    assert _score(strong, objective, benchmark, benchmark) > _score(
        weak,
        objective,
        benchmark,
        benchmark,
    )


def test_negative_return_stays_negative_and_annualized_target_remains_a_hard_gate() -> None:
    negative = ranking_metrics(
        total_return=-0.1,
        annualized_return=-0.19,
        duration_years=0.5,
        sharpe_ratio=-1,
        max_drawdown=-0.1,
    )
    flat = negative.model_copy(
        update={
            "total_return": 0.0,
            "annualized_return": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
        }
    )

    assert _score(negative, "balanced", flat, flat) < _score(
        flat,
        "balanced",
        flat,
        flat,
    )
    assert not _participates(
        negative,
        minimum_trades=0,
        maximum_trades_per_year=None,
        minimum_exposure=0,
        minimum_annualized_return=0,
        maximum_drawdown=None,
        maximum_cash_streak_ratio=1,
        maximum_cash_streak_bars=None,
    )
    assert _participates(
        flat.model_copy(update={"annualized_return": 0.11}),
        minimum_trades=0,
        maximum_trades_per_year=None,
        minimum_exposure=0,
        minimum_annualized_return=0.1,
        maximum_drawdown=None,
        maximum_cash_streak_ratio=1,
        maximum_cash_streak_bars=None,
    )


def test_participates_delegates_exact_metrics_to_shared_risk_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = ranking_metrics(
        total_return=0.12,
        annualized_return=0.15,
        duration_years=1,
    ).model_copy(
        update={
            "trades": 12,
            "trades_per_year": 6.5,
            "exposure_ratio": 0.55,
            "max_drawdown": -0.18,
            "max_cash_streak_ratio": 0.22,
            "max_cash_streak": 9,
        }
    )
    observed: dict[str, object] = {}

    def reject_from_kernel(**values: object) -> bool:
        observed.update(values)
        return False

    monkeypatch.setattr(
        optimization_module,
        "research_risk_allows",
        reject_from_kernel,
    )

    assert not _participates(
        metrics,
        minimum_trades=10,
        maximum_trades_per_year=20,
        minimum_exposure=0.4,
        minimum_annualized_return=0.1,
        maximum_drawdown=0.25,
        maximum_cash_streak_ratio=0.3,
        maximum_cash_streak_bars=15,
    )
    assert observed == {
        "trades": 12,
        "trades_per_year": 6.5,
        "exposure_ratio": 0.55,
        "annualized_return": 0.15,
        "max_drawdown": -0.18,
        "max_cash_streak_ratio": 0.22,
        "max_cash_streak_bars": 9,
        "minimum_trades": 10,
        "maximum_trades_per_year": 20,
        "minimum_exposure": 0.4,
        "minimum_annualized_return": 0.1,
        "maximum_drawdown": 0.25,
        "maximum_cash_streak_ratio": 0.3,
        "maximum_cash_streak_bars": 15,
    }


def test_participates_and_explainable_risk_decision_have_boundary_parity() -> None:
    limits = ResearchRiskLimits(
        minimum_trades=10,
        maximum_trades_per_year=5,
        minimum_exposure=0.5,
        minimum_annualized_return=0.1,
        maximum_drawdown=0.2,
        maximum_cash_streak_ratio=0.3,
        maximum_cash_streak_bars=10,
    )
    base = ranking_metrics(
        total_return=0.1,
        annualized_return=0.1,
        duration_years=1,
        max_drawdown=-0.2,
    ).model_copy(
        update={
            "trades": 10,
            "trades_per_year": 5,
            "exposure_ratio": 0.5,
            "max_cash_streak_ratio": 0.3,
            "max_cash_streak": 10,
        }
    )
    samples = (
        base,
        base.model_copy(update={"trades": 9}),
        base.model_copy(update={"trades_per_year": 5.01}),
        base.model_copy(update={"exposure_ratio": 0.49}),
        base.model_copy(update={"annualized_return": 0.09}),
        base.model_copy(update={"max_drawdown": -0.21}),
        base.model_copy(update={"max_cash_streak_ratio": 0.31}),
        base.model_copy(update={"max_cash_streak": 11}),
    )
    rule_set = build_research_rule_set(limits)
    clear_switch = build_kill_switch_snapshot(
        status="clear",
        revision=0,
        source="optimization-parity-test",
    )

    for index, metrics in enumerate(samples):
        participates = _participates(
            metrics,
            minimum_trades=limits.minimum_trades,
            maximum_trades_per_year=limits.maximum_trades_per_year,
            minimum_exposure=limits.minimum_exposure,
            minimum_annualized_return=limits.minimum_annualized_return,
            maximum_drawdown=limits.maximum_drawdown,
            maximum_cash_streak_ratio=limits.maximum_cash_streak_ratio,
            maximum_cash_streak_bars=limits.maximum_cash_streak_bars,
        )
        request = build_research_risk_request(
            evaluation_id=f"optimization-parity-{index}",
            evaluated_at=RISK_EVALUATED_AT,
            source_calculation_version="optimization-participation-v1",
            rule_set=rule_set,
            kill_switch=clear_switch,
            evidence=ResearchEvidence(
                trades=metrics.trades,
                trades_per_year=metrics.trades_per_year,
                exposure_ratio=metrics.exposure_ratio,
                annualized_return=metrics.annualized_return,
                max_drawdown=metrics.max_drawdown,
                max_cash_streak_ratio=metrics.max_cash_streak_ratio,
                max_cash_streak_bars=metrics.max_cash_streak,
            ),
        )
        decision = evaluate_research_risk(request)

        assert participates is (decision.decision == "allow")
        assert decision.request.evaluated_at == RISK_EVALUATED_AT
        assert decision.request.kill_switch.status == "clear"


@pytest.mark.parametrize(
    "metric_name",
    (
        "trades",
        "trades_per_year",
        "exposure_ratio",
        "annualized_return",
        "max_drawdown",
        "max_cash_streak_ratio",
        "max_cash_streak",
    ),
)
def test_participates_preserves_fail_closed_nan_comparisons(
    metric_name: str,
) -> None:
    metrics = ranking_metrics(
        total_return=0.1,
        annualized_return=0.1,
        duration_years=1,
        max_drawdown=-0.2,
    ).model_copy(
        update={
            "trades": 10,
            "trades_per_year": 5,
            "exposure_ratio": 0.5,
            "max_cash_streak_ratio": 0.3,
            "max_cash_streak": 10,
            metric_name: float("nan"),
        }
    )

    assert not _participates(
        metrics,
        minimum_trades=10,
        maximum_trades_per_year=5,
        minimum_exposure=0.5,
        minimum_annualized_return=0.1,
        maximum_drawdown=0.2,
        maximum_cash_streak_ratio=0.3,
        maximum_cash_streak_bars=10,
    )


def test_macd_parameter_grid_rejects_invalid_period_order() -> None:
    candidates = parameter_candidates("macd")

    assert len(candidates) > 20
    assert all(item["fast"] < item["slow"] for item in candidates)


def test_discovery_screening_uses_a_bounded_stratified_slice_and_keeps_default() -> None:
    screened = screening_parameter_candidates("macd", maximum_candidates=12)

    assert len(screened) == 12
    assert len({tuple(sorted(item.items())) for item in screened}) == len(screened)
    assert {"fast": 12, "slow": 26, "signal": 9} in screened
    assert all(item["fast"] < item["slow"] for item in screened)
    assert screened[0] == {"fast": 12, "slow": 26, "signal": 9}


def test_discovery_screening_large_grid_keeps_corners_and_center() -> None:
    strategy_id = "volatility-target-trend"
    candidates = parameter_candidates(strategy_id)
    screened = screening_parameter_candidates(strategy_id, maximum_candidates=12)
    keys = list(candidates[0])
    levels = {
        key: sorted({candidate[key] for candidate in candidates})
        for key in keys
    }
    low_corner = {key: levels[key][0] for key in keys}
    high_corner = {key: levels[key][-1] for key in keys}

    def center_distance(candidate: dict[str, float]) -> float:
        distance = 0.0
        for key in keys:
            axis = levels[key]
            position = axis.index(candidate[key]) / max(1, len(axis) - 1)
            distance += (position - 0.5) ** 2
        return distance

    center = min(
        enumerate(candidates),
        key=lambda item: (center_distance(item[1]), item[0]),
    )[1]

    assert len(candidates) == 1_000
    assert low_corner in screened
    assert high_corner in screened
    assert center in screened
    assert len(screened) == 12


def test_discovery_screening_covers_each_legal_parameter_level_when_budget_allows() -> None:
    for strategy_id in {"macd", "breakout", "rsi-regime-atr"}:
        screened = screening_parameter_candidates(strategy_id, maximum_candidates=12)
        candidates = parameter_candidates(strategy_id)
        for key in screened[0]:
            assert {item[key] for item in screened} == {
                item[key] for item in candidates
            }


def test_adaptive_screening_refines_promoted_regions_with_a_hard_total_budget() -> None:
    strategy_id = "volatility-target-trend"
    initial = screening_parameter_candidates(strategy_id, maximum_candidates=12)
    promoted = initial[:2]

    screened = adaptive_screening_parameter_candidates(
        strategy_id,
        evaluated_parameters=initial,
        promoted_parameters=promoted,
        maximum_candidates=24,
        global_exploration_fraction=0.25,
    )
    repeated = adaptive_screening_parameter_candidates(
        strategy_id,
        evaluated_parameters=initial,
        promoted_parameters=promoted,
        maximum_candidates=24,
        global_exploration_fraction=0.25,
    )

    assert screened == repeated
    assert screened[: len(initial)] == initial
    assert len(screened) == 24
    assert len({tuple(candidate.items()) for candidate in screened}) == 24
    refinement = screened[len(initial) :]
    assert any(
        sum(candidate[key] != seed[key] for key in candidate) == 1
        for candidate in refinement
        for seed in promoted
    )
    assert any(
        min(
            sum(candidate[key] != seed[key] for key in candidate)
            for seed in promoted
        )
        >= 3
        for candidate in refinement
    )


def test_adaptive_screening_uses_global_recovery_without_promoted_candidates() -> None:
    strategy_id = "mean-reversion"
    initial = screening_parameter_candidates(strategy_id, maximum_candidates=12)
    screened = adaptive_screening_parameter_candidates(
        strategy_id,
        evaluated_parameters=initial,
        promoted_parameters=[],
        maximum_candidates=24,
    )

    assert screened[: len(initial)] == initial
    assert len(screened) == 24
    assert len({tuple(candidate.items()) for candidate in screened}) == 24
    assert all(
        {candidate[key] for candidate in screened}
        == {candidate[key] for candidate in parameter_candidates(strategy_id)}
        for key in screened[0]
    )


def test_adaptive_screening_evaluates_a_small_grid_in_full() -> None:
    strategy_id = "trend-filter"
    candidates = parameter_candidates(strategy_id)
    initial = screening_parameter_candidates(strategy_id, maximum_candidates=12)

    screened = adaptive_screening_parameter_candidates(
        strategy_id,
        evaluated_parameters=initial,
        promoted_parameters=initial[:1],
        maximum_candidates=24,
    )

    assert len(candidates) == 9
    assert initial == candidates
    assert screened == candidates


@pytest.mark.parametrize("maximum_candidates", [0, 65])
def test_discovery_screening_rejects_unsafe_limits(maximum_candidates: int) -> None:
    with pytest.raises(ValueError, match="maximum_candidates"):
        screening_parameter_candidates(
            "macd",
            maximum_candidates=maximum_candidates,
        )


@pytest.mark.parametrize(
    ("maximum_candidates", "global_exploration_fraction"),
    [(0, 0.25), (65, 0.25), (24, -0.01), (24, 1.01)],
)
def test_adaptive_screening_rejects_unsafe_budgets(
    maximum_candidates: int,
    global_exploration_fraction: float,
) -> None:
    with pytest.raises(ValueError):
        adaptive_screening_parameter_candidates(
            "macd",
            evaluated_parameters=[],
            promoted_parameters=[],
            maximum_candidates=maximum_candidates,
            global_exploration_fraction=global_exploration_fraction,
        )


def test_breakout_grid_uses_slower_entry_and_faster_exit() -> None:
    candidates = parameter_candidates("breakout")

    assert candidates
    assert all(
        item["exit_period"] < item["entry_period"] for item in candidates
    )


def test_core_trend_grid_keeps_a_real_defensive_allocation() -> None:
    candidates = parameter_candidates("core-trend-allocation")

    assert len(candidates) == 48
    assert all(0 < item["defensive_exposure"] < 1 for item in candidates)


def test_optimizer_propagates_core_trend_satellite_cycle_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parameters = {"period": 3, "defensive_exposure": 0.35}
    monkeypatch.setattr(
        optimization_module,
        "parameter_candidates",
        lambda _strategy_id: [parameters],
    )
    periods = 160
    index = pd.date_range("2024-01-01", periods=periods, freq="D")
    close = pd.Series(
        [100.0, 100.0, 100.0]
        + [120.0 if offset % 2 == 0 else 80.0 for offset in range(periods - 3)],
        index=index,
    )
    data = pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1_000,
        },
        index=index,
    )

    result = optimize_strategy(
        data,
        "core-trend-allocation",
        "balanced",
        BacktestConfig(
            fee_rate=0,
            slippage_rate=0,
            annual_periods=365,
            signal_delay_bars=0,
        ),
        minimum_trades=0,
        minimum_trades_per_year=1,
        minimum_exposure=0,
        maximum_cash_streak_ratio=1,
        minimum_profitable_fold_ratio=0,
        minimum_timing_positive_fold_ratio=0,
    )

    assert result.full_result.metrics.closed_trades > 0
    assert result.validation_metrics.closed_trades > 0
    assert {
        cycle["cycle_kind"] for cycle in result.full_result.position_cycles
    } == {"satellite_over_core"}


@pytest.mark.parametrize("closed_trades", [5, 9])
def test_forward_observation_accepts_only_frozen_five_to_nine_trade_samples(
    closed_trades: int,
) -> None:
    assert is_forward_observation_eligible(
        validation_passed=False,
        validation_code="sample_insufficient",
        closed_trades=closed_trades,
        cost_stress_passed=True,
    )


@pytest.mark.parametrize("closed_trades", [0, 4, 10, 30])
def test_forward_observation_rejects_samples_outside_narrow_window(
    closed_trades: int,
) -> None:
    assert not is_forward_observation_eligible(
        validation_passed=False,
        validation_code="sample_insufficient",
        closed_trades=closed_trades,
        cost_stress_passed=True,
    )


def test_forward_observation_rejects_other_failures_and_cost_stress() -> None:
    assert not is_forward_observation_eligible(
        validation_passed=False,
        validation_code="negative_return",
        closed_trades=8,
        cost_stress_passed=True,
    )
    assert not is_forward_observation_eligible(
        validation_passed=False,
        validation_code="sample_insufficient",
        closed_trades=8,
        cost_stress_passed=False,
    )


def test_limited_development_sample_can_unlock_but_not_pass_outer_audit() -> None:
    assert is_development_selection_eligible(
        validation_passed=False,
        validation_code="sample_insufficient",
        closed_trades=MIN_DEVELOPMENT_SELECTION_CLOSED_TRADES,
        cost_stress_passed=True,
    )
    assert not is_development_selection_eligible(
        validation_passed=False,
        validation_code="sample_insufficient",
        closed_trades=MIN_DEVELOPMENT_SELECTION_CLOSED_TRADES - 1,
        cost_stress_passed=True,
    )
    assert not is_development_selection_eligible(
        validation_passed=False,
        validation_code="negative_return",
        closed_trades=20,
        cost_stress_passed=True,
    )


def test_holdout_rejects_timing_that_lags_same_exposure_passive_baseline() -> None:
    strategy = BacktestMetrics(
        bars=100,
        duration_years=1,
        total_return=0.08,
        annualized_return=0.08,
        annualized_volatility=0.1,
        sharpe_ratio=0.8,
        max_drawdown=-0.05,
        win_rate=0.6,
        trades=12,
        closed_trades=12,
        trades_per_year=12,
        average_holding_bars=4,
        profit_factor=1.5,
        exposure_ratio=0.3,
        max_cash_streak=15,
        max_cash_streak_ratio=0.15,
    )
    benchmark = strategy.model_copy(
        update={"total_return": 0.3, "annualized_return": 0.3}
    )
    exposure_matched = strategy.model_copy(
        update={"total_return": 0.1, "annualized_return": 0.1}
    )

    passed, code, reason = validate_holdout_detailed(
        strategy,
        benchmark,
        exposure_matched,
    )

    assert passed is False
    assert code == "exposure_matched_lag"
    assert "相同起点投入比例的固定份额买入持有" in reason


def test_holdout_labels_development_inner_validation() -> None:
    strategy = BacktestMetrics(
        bars=100,
        duration_years=1,
        total_return=-0.01,
        annualized_return=-0.01,
        annualized_volatility=0.1,
        sharpe_ratio=-0.1,
        max_drawdown=-0.05,
        win_rate=0.4,
        trades=4,
        closed_trades=4,
        trades_per_year=4,
        average_holding_bars=4,
        profit_factor=0.8,
        exposure_ratio=0.3,
        max_cash_streak=15,
        max_cash_streak_ratio=0.15,
    )

    passed, code, reason = validate_holdout_detailed(
        strategy,
        strategy,
        scope="development_inner",
    )

    assert passed is False
    assert code == "negative_return"
    assert reason == "开发期内层留出收益为负，参数未通过开发验证。"


def test_optimizer_selects_on_training_and_reports_holdout() -> None:
    result = optimize_strategy(
        oscillating_data(),
        "macd",
        "balanced",
        BacktestConfig(fee_rate=0, slippage_rate=0, annual_periods=365),
    )

    assert result.candidates_evaluated == len(parameter_candidates("macd"))
    assert result.selected_parameters["fast"] < result.selected_parameters["slow"]
    assert result.split_date.startswith("2024-")
    assert result.full_result.config.annual_periods == 365
    assert result.validation_metrics.trades >= 0
    assert 1 <= len(result.top_candidates) <= 5
    assert len(result.walk_forward_folds) == 3
    assert result.minimum_trades >= 3
    assert result.train_metrics.exposure_ratio >= result.minimum_exposure
    assert (
        result.train_metrics.max_cash_streak_ratio
        <= result.maximum_cash_streak_ratio
    )
    assert 0 <= result.minimum_profitable_fold_ratio <= 1
    assert 0 <= result.minimum_timing_positive_fold_ratio <= 1
    assert all(
        candidate.timing_positive_fold_ratio
        >= result.minimum_timing_positive_fold_ratio
        for candidate in result.top_candidates
    )
    assert all(
        candidate.timing_positive_fold_ratio
        == pytest.approx(
            sum(value > 0 for value in candidate.fold_timing_excess_returns)
            / len(candidate.fold_timing_excess_returns)
        )
        for candidate in result.top_candidates
    )
    assert all(
        fold.timing_excess_return
        == pytest.approx(
            fold.validation_metrics.total_return
            - fold.exposure_matched_benchmark_metrics.total_return
        )
        for fold in result.walk_forward_folds
    )
    assert isinstance(result.validation_passed, bool)
    assert result.validation_code != "unclassified"
    assert result.validation_reason
    # Buy-and-hold was already invested before the validation boundary, so it
    # contributes return but not a new holdout-period entry transaction.
    assert result.validation_benchmark_metrics.trades == 0
    assert (
        result.validation_timing_excess_return
        == pytest.approx(
            result.validation_metrics.total_return
            - result.validation_exposure_matched_benchmark_metrics.total_return
        )
    )
    assert result.cost_stress_passed is True
    assert [item.multiplier for item in result.cost_stress_tests] == [2, 3]


def test_walk_forward_fold_carries_execution_context_from_its_training_window() -> None:
    data = oscillating_data()
    config = BacktestConfig(fee_rate=0, slippage_rate=0, annual_periods=365)
    result = optimize_strategy(data, "macd", "balanced", config)
    _, _, validation_start, validation_end = _fold_boundaries(
        int(len(data) * result.train_ratio),
        len(result.walk_forward_folds),
    )[1]
    carried_fold = result.walk_forward_folds[1]
    signals = strategy_signals(data, "macd", carried_fold.selected_parameters)
    assert signals.iloc[validation_start - 1] == 1
    assert signals.iloc[validation_start] == 1
    expected = run_backtest(
        data.iloc[:validation_end],
        signals.iloc[:validation_end],
        config,
        include_details=False,
        evaluation_start=validation_start,
    )

    assert carried_fold.validation_metrics.total_return == pytest.approx(
        expected.metrics.total_return
    )
    assert carried_fold.validation_metrics.trades == expected.metrics.trades


def test_optimizer_validates_timing_positive_fold_ratio() -> None:
    with pytest.raises(
        ValueError,
        match="minimum_timing_positive_fold_ratio",
    ):
        optimize_strategy(
            oscillating_data(),
            "macd",
            "balanced",
            minimum_timing_positive_fold_ratio=1.1,
        )


def test_optimizer_rejects_passive_allocation_templates() -> None:
    with pytest.raises(ValueError, match="do not use parameter optimization"):
        optimize_strategy(
            oscillating_data(),
            "constant-allocation",
            "balanced",
        )


def test_optimizer_rejects_candidates_outside_return_and_drawdown_budget() -> None:
    with pytest.raises(ValueError, match="年化收益至少"):
        optimize_strategy(
            oscillating_data(),
            "trend-filter",
            "balanced",
            BacktestConfig(fee_rate=0, slippage_rate=0, annual_periods=365),
            minimum_trades=0,
            minimum_exposure=0,
            minimum_annualized_return=10,
            maximum_drawdown=0.01,
            maximum_cash_streak_ratio=1,
            minimum_profitable_fold_ratio=0,
        )
