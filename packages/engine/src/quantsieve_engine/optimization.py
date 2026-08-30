from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from statistics import fmean, pstdev
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from .backtest import (
    BacktestConfig,
    BacktestMetrics,
    BacktestResult,
    evaluation_duration_years,
    run_backtest,
    run_exposure_matched_benchmark,
)
from .risk import research_risk_allows
from .strategies import (
    PASSIVE_STRATEGY_IDS,
    get_strategy,
    strategy_cycle_baseline_signals,
    strategy_signals,
)

OptimizationObjective = Literal[
    "total_return",
    "sharpe_ratio",
    "drawdown_control",
    "balanced",
]
FINAL_HOLDOUT_MIN_CLOSED_TRADES = 10
"""Independent closed decision cycles required for final-holdout validation."""

FORWARD_OBSERVATION_MIN_CLOSED_TRADES = 5
"""Lower bound for a frozen observation candidate; it is never validation."""

MIN_DEVELOPMENT_SELECTION_CLOSED_TRADES = 3
"""Smallest inner-holdout sample that may unlock one outer-holdout audit."""

HoldoutValidationCode = Literal[
    "passed",
    "negative_return",
    "benchmark_capture_failed",
    "exposure_matched_lag",
    "weak_market_lag",
    "sample_insufficient",
    "history_horizon_insufficient",
    "return_target_failed",
    "drawdown_limit_failed",
    "frequency_too_low",
    "exposure_too_low",
    "frequency_too_high",
    "cash_streak_too_long",
    "cost_stress_failed",
    "unclassified",
]
ValidationScope = Literal["final", "development_inner"]


class OptimizationCandidate(BaseModel):
    parameters: dict[str, float]
    score: float
    score_stability: float
    metrics: BacktestMetrics
    fold_scores: list[float] = Field(default_factory=list)
    fold_timing_excess_returns: list[float] = Field(default_factory=list)
    profitable_fold_ratio: float = 0.0
    timing_positive_fold_ratio: float = 0.0


class WalkForwardFold(BaseModel):
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    selected_parameters: dict[str, float]
    validation_metrics: BacktestMetrics
    exposure_matched_benchmark_metrics: BacktestMetrics
    timing_excess_return: float
    timing_value_added: bool


class CostStressTest(BaseModel):
    multiplier: float
    fee_rate: float
    slippage_rate: float
    metrics: BacktestMetrics
    positive_return: bool
    exposure_matched_total_return: float
    timing_excess_return: float
    beats_exposure_matched: bool
    passed: bool


class StrategyOptimizationResult(BaseModel):
    objective: OptimizationObjective
    selected_parameters: dict[str, float]
    train_ratio: float
    split_date: str
    candidates_evaluated: int
    candidates_eligible: int
    minimum_trades: int
    minimum_trades_per_year: int
    maximum_trades_per_year: int | None
    minimum_exposure: float
    minimum_annualized_return: float | None
    maximum_drawdown: float | None
    maximum_cash_streak_ratio: float
    maximum_cash_streak_bars: int | None
    minimum_profitable_fold_ratio: float
    minimum_timing_positive_fold_ratio: float
    train_metrics: BacktestMetrics
    validation_metrics: BacktestMetrics
    development_benchmark_metrics: BacktestMetrics
    development_exposure_matched_benchmark_metrics: BacktestMetrics
    validation_benchmark_metrics: BacktestMetrics
    validation_exposure_matched_benchmark_metrics: BacktestMetrics
    validation_excess_return: float
    validation_timing_excess_return: float
    validation_passed: bool
    validation_code: HoldoutValidationCode = "unclassified"
    validation_reason: str
    forward_observation_eligible: bool = False
    cost_stress_passed: bool
    cost_stress_tests: list[CostStressTest] = Field(default_factory=list)
    walk_forward_folds: list[WalkForwardFold] = Field(default_factory=list)
    walk_forward_method: Literal["expanding_window_reoptimization"] = (
        "expanding_window_reoptimization"
    )
    walk_forward_execution_state_carried: bool = True
    top_candidates: list[OptimizationCandidate] = Field(default_factory=list)
    full_result: BacktestResult


PARAMETER_GRIDS: dict[str, dict[str, list[float]]] = {
    "sma-cross": {
        "fast": [5, 10, 15, 20, 30, 40, 50],
        "slow": [40, 60, 80, 100, 120, 160, 200],
    },
    "ema-cross": {
        "fast": [4, 5, 8, 12, 16, 20, 26],
        "slow": [18, 20, 26, 35, 50, 65, 80],
    },
    "rsi": {
        "period": [5, 7, 10, 14, 21, 28],
        "oversold": [15, 20, 25, 30, 35, 40],
        "overbought": [60, 65, 70, 75, 80, 85],
    },
    "bollinger": {
        "period": [10, 15, 20, 30, 40, 50],
        "deviations": [1.25, 1.5, 1.75, 2, 2.5, 3],
    },
    "momentum": {
        "lookback": [3, 5, 10, 20, 40, 60, 90],
        "threshold": [0, 0.005, 0.01, 0.02, 0.03, 0.05],
    },
    "mean-reversion": {
        "period": [5, 10, 15, 20, 30, 50, 80],
        "entry_z": [0.75, 1, 1.25, 1.5, 2, 2.5],
        "exit_z": [0, 0.25, 0.5, 0.75],
        "regime": [50, 75, 100, 150, 200],
    },
    "breakout": {
        "entry_period": [20, 40, 55, 80, 120, 180],
        "exit_period": [5, 10, 20, 30, 40, 60],
    },
    "macd": {
        "fast": [5, 8, 12, 16, 20],
        "slow": [20, 26, 32, 40, 50, 65],
        "signal": [5, 7, 9, 12],
    },
    "volume-breakout": {
        "period": [5, 10, 15, 20, 30, 40, 60],
        "multiplier": [1.1, 1.2, 1.35, 1.5, 2, 2.5, 3],
    },
    "trend-filter": {
        "period": [30, 50, 75, 100, 125, 150, 200, 250, 300],
    },
    "core-trend-allocation": {
        "period": [50, 75, 100, 125, 150, 200, 250, 300],
        "defensive_exposure": [0.15, 0.25, 0.35, 0.5, 0.65, 0.8],
    },
    "volatility-target-trend": {
        "trend_period": [75, 100, 150, 200, 250],
        "volatility_period": [10, 15, 20, 30, 40],
        "minimum_allocation": [0.15, 0.25, 0.35, 0.5],
        "maximum_allocation": [0.75, 1.0],
        "adjustment_band": [0.05, 0.1, 0.15, 0.2, 0.25],
    },
    "macd-regime": {
        "fast": [5, 8, 12, 16],
        "slow": [20, 26, 35, 50],
        "signal": [5, 9, 12],
        "regime": [75, 100, 150, 200],
    },
    "atr-trend": {
        "fast": [10, 20, 30],
        "slow": [50, 60, 80, 120],
        "atr_period": [10, 14, 20],
        "atr_multiplier": [2, 3, 4],
    },
    "breakout-atr": {
        "entry_period": [20, 40, 55, 80, 120],
        "exit_period": [5, 10, 20, 30, 40],
        "atr_period": [10, 14, 20],
        "atr_multiplier": [2, 3, 4],
    },
    "rsi-regime-atr": {
        "rsi_period": [7, 14, 21],
        "oversold": [20, 30, 40],
        "exit_rsi": [50, 55, 60],
        "regime": [100, 150, 200],
        "atr_period": [14, 20],
        "atr_multiplier": [2, 3],
    },
    "buy-hold": {},
    "constant-allocation": {},
}


def parameter_candidates(strategy_id: str) -> list[dict[str, float]]:
    try:
        grid = PARAMETER_GRIDS[strategy_id]
    except KeyError as exc:
        raise KeyError(f"Unknown strategy: {strategy_id}") from exc
    if not grid:
        return [{}]
    keys = list(grid)
    candidates = [
        dict(zip(keys, values, strict=True))
        for values in itertools.product(*(grid[key] for key in keys))
    ]
    if strategy_id in {
        "sma-cross",
        "ema-cross",
        "macd",
        "macd-regime",
        "atr-trend",
    }:
        candidates = [
            parameters
            for parameters in candidates
            if parameters["fast"] < parameters["slow"]
        ]
    if strategy_id == "rsi":
        candidates = [
            parameters
            for parameters in candidates
            if parameters["oversold"] < parameters["overbought"]
        ]
    if strategy_id == "rsi-regime-atr":
        candidates = [
            parameters
            for parameters in candidates
            if parameters["oversold"] < parameters["exit_rsi"]
        ]
    if strategy_id == "mean-reversion":
        candidates = [
            parameters
            for parameters in candidates
            if parameters["entry_z"] > parameters["exit_z"]
        ]
    if strategy_id == "volatility-target-trend":
        candidates = [
            parameters
            for parameters in candidates
            if parameters["minimum_allocation"] < parameters["maximum_allocation"]
        ]
    if strategy_id in {"breakout", "breakout-atr"}:
        candidates = [
            parameters
            for parameters in candidates
            if parameters["exit_period"] < parameters["entry_period"]
        ]
    return candidates


def screening_parameter_candidates(
    strategy_id: str,
    *,
    maximum_candidates: int = 12,
) -> list[dict[str, float]]:
    """Return a bounded, default-inclusive, deterministic exploration slice.

    Strategy discovery uses this only on development data to decide which
    templates deserve a full robust optimization. It is deliberately not an
    optimizer: every candidate remains eligible for the later full grid,
    walk-forward checks, and untouched final holdout.
    """

    if not 1 <= maximum_candidates <= 64:
        raise ValueError("maximum_candidates must be between 1 and 64.")
    candidates = parameter_candidates(strategy_id)
    if len(candidates) <= maximum_candidates:
        return candidates

    definition, _ = get_strategy(strategy_id)
    default_parameters = {
        key: definition.parameters[key]
        for key in PARAMETER_GRIDS[strategy_id]
    }
    selected: list[dict[str, float]] = []
    if default_parameters in candidates:
        selected.append(default_parameters)

    keys = list(PARAMETER_GRIDS[strategy_id])
    levels = {
        key: list(dict.fromkeys(candidate[key] for candidate in candidates))
        for key in keys
    }
    level_positions = {
        key: {
            value: index / max(1, len(levels[key]) - 1)
            for index, value in enumerate(levels[key])
        }
        for key in keys
    }

    def coordinates(candidate: Mapping[str, float]) -> tuple[float, ...]:
        return tuple(level_positions[key][candidate[key]] for key in keys)

    # Preserve the public default, the geometric centre, and legal candidates
    # nearest both full-grid corners before greedily filling marginal levels.
    # This avoids a lexicographic product() prefix while retaining reproducible
    # endpoints for highly interactive parameter families.
    anchors = [
        tuple(0.5 for _ in keys),
        tuple(0.0 for _ in keys),
        tuple(1.0 for _ in keys),
        tuple(float(index % 2) for index, _ in enumerate(keys)),
        tuple(float((index + 1) % 2) for index, _ in enumerate(keys)),
    ]
    indexed_candidates = list(enumerate(candidates))
    for anchor in anchors:
        if len(selected) >= maximum_candidates:
            break
        _, nearest = min(
            indexed_candidates,
            key=lambda item: (
                sum(
                    (coordinate - target) ** 2
                    for coordinate, target in zip(
                        coordinates(item[1]),
                        anchor,
                        strict=True,
                    )
                ),
                item[0],
            ),
        )
        if nearest not in selected:
            selected.append(nearest)

    # Screening is only a bounded gate before full optimization, but the gate
    # itself should not omit an entire legal setting merely because product()
    # happened to order the grid that way. Greedily cover unseen values on
    # every parameter axis, then use normalized distance as a deterministic
    # tie-breaker for diverse combinations.
    covered = {
        key: {candidate[key] for candidate in selected}
        for key in keys
    }
    while len(selected) < maximum_candidates:
        remaining = [
            (index, candidate)
            for index, candidate in indexed_candidates
            if candidate not in selected
        ]
        if not remaining:
            break

        def rank(item: tuple[int, dict[str, float]]) -> tuple[int, float, int]:
            index, candidate = item
            unseen_values = sum(candidate[key] not in covered[key] for key in keys)
            minimum_distance = (
                min(
                    sum(
                        abs(value - prior_value)
                        for value, prior_value in zip(
                            coordinates(candidate),
                            coordinates(prior),
                            strict=True,
                        )
                    )
                    for prior in selected
                )
                if selected
                else len(keys)
            )
            return unseen_values, minimum_distance, -index

        _, candidate = max(remaining, key=rank)
        selected.append(candidate)
        for key in keys:
            covered[key].add(candidate[key])
    return selected


def adaptive_screening_parameter_candidates(
    strategy_id: str,
    *,
    evaluated_parameters: Sequence[Mapping[str, float]],
    promoted_parameters: Sequence[Mapping[str, float]],
    maximum_candidates: int = 24,
    global_exploration_fraction: float = 0.25,
) -> list[dict[str, float]]:
    """Extend a deterministic screening slice without exceeding a hard budget.

    Promoted candidates must already have been evaluated. Nearby legal grid
    points receive most refinement slots, while a fixed reserve is selected by
    maximin distance across the remaining grid. With no promoted candidate the
    whole second stage becomes global recovery instead of exploiting a failed
    region. The helper only handles parameters; callers remain responsible for
    deriving promotions from development data rather than a final holdout.
    """

    if not 1 <= maximum_candidates <= 64:
        raise ValueError("maximum_candidates must be between 1 and 64.")
    if not 0 <= global_exploration_fraction <= 1:
        raise ValueError("global_exploration_fraction must be between 0 and 1.")

    candidates = parameter_candidates(strategy_id)
    target_candidates = min(len(candidates), maximum_candidates)
    keys = list(PARAMETER_GRIDS[strategy_id])
    candidate_by_identity = {
        tuple(candidate[key] for key in keys): candidate
        for candidate in candidates
    }

    def canonical(
        parameters: Mapping[str, float],
        *,
        role: str,
    ) -> dict[str, float]:
        try:
            identity = tuple(parameters[key] for key in keys)
        except KeyError as exc:
            raise ValueError(
                f"{role} parameters must contain every strategy grid key."
            ) from exc
        try:
            return candidate_by_identity[identity]
        except KeyError as exc:
            raise ValueError(f"{role} parameters must be a legal grid candidate.") from exc

    selected: list[dict[str, float]] = []
    for parameters in evaluated_parameters:
        candidate = canonical(parameters, role="evaluated")
        if candidate not in selected:
            selected.append(candidate)
    if len(selected) > target_candidates:
        raise ValueError("evaluated_parameters already exceed maximum_candidates.")
    if not selected:
        selected.extend(
            screening_parameter_candidates(
                strategy_id,
                maximum_candidates=min(12, target_candidates),
            )
        )

    promoted: list[dict[str, float]] = []
    for parameters in promoted_parameters:
        candidate = canonical(parameters, role="promoted")
        if candidate not in selected:
            raise ValueError("promoted_parameters must already have been evaluated.")
        if candidate not in promoted:
            promoted.append(candidate)

    levels = {
        key: list(dict.fromkeys(candidate[key] for candidate in candidates))
        for key in keys
    }
    level_positions = {
        key: {
            value: index / max(1, len(levels[key]) - 1)
            for index, value in enumerate(levels[key])
        }
        for key in keys
    }
    coordinates = {
        tuple(candidate[key] for key in keys): tuple(
            level_positions[key][candidate[key]] for key in keys
        )
        for candidate in candidates
    }

    def identity(candidate: Mapping[str, float]) -> tuple[float, ...]:
        return tuple(candidate[key] for key in keys)

    def distance(
        left: Mapping[str, float],
        right: Mapping[str, float],
    ) -> float:
        return sum(
            abs(left_value - right_value)
            for left_value, right_value in zip(
                coordinates[identity(left)],
                coordinates[identity(right)],
                strict=True,
            )
        ) / max(1, len(keys))

    remaining_slots = target_candidates - len(selected)
    exploration_slots = (
        min(
            remaining_slots,
            (
                max(1, math.ceil(remaining_slots * global_exploration_fraction))
                if global_exploration_fraction > 0
                else 0
            ),
        )
        if promoted and remaining_slots
        else remaining_slots
    )
    local_slots = remaining_slots - exploration_slots

    # Round-robin over promoted regions prevents the first ranked seed from
    # consuming the entire local budget when several feasible basins exist.
    local_rankings = {
        identity(seed): sorted(
            (
                (index, candidate)
                for index, candidate in enumerate(candidates)
                if candidate not in selected
            ),
            key=lambda item: (distance(item[1], seed), item[0]),
        )
        for seed in promoted
    }
    local_added = 0
    while local_added < local_slots:
        progressed = False
        for seed in promoted:
            for _, candidate in local_rankings[identity(seed)]:
                if candidate in selected:
                    continue
                selected.append(candidate)
                local_added += 1
                progressed = True
                break
            if local_added >= local_slots:
                break
        if not progressed:
            break

    # Spend the reserve (or the entire recovery stage) on points farthest from
    # everything already examined. Marginal unseen levels are prioritized if a
    # caller supplied a smaller custom first stage.
    while len(selected) < target_candidates:
        covered = {
            key: {candidate[key] for candidate in selected}
            for key in keys
        }
        remaining = [
            (index, candidate)
            for index, candidate in enumerate(candidates)
            if candidate not in selected
        ]
        if not remaining:
            break

        ranked_remaining = [
            (
                sum(candidate[key] not in covered[key] for key in keys),
                min(distance(candidate, prior) for prior in selected),
                -index,
                candidate,
            )
            for index, candidate in remaining
        ]
        *_, candidate = max(
            ranked_remaining,
            key=lambda item: item[:3],
        )
        selected.append(candidate)
    return selected


def optimize_strategy(
    data: pd.DataFrame,
    strategy_id: str,
    objective: OptimizationObjective,
    config: BacktestConfig | None = None,
    *,
    signal_history: pd.DataFrame | None = None,
    train_ratio: float = 0.8,
    minimum_trades_per_year: int = 6,
    maximum_trades_per_year: int | None = None,
    minimum_exposure: float = 0.2,
    minimum_annualized_return: float | None = None,
    maximum_drawdown: float | None = None,
    maximum_cash_streak_ratio: float = 0.4,
    maximum_cash_streak_bars: int | None = None,
    minimum_profitable_fold_ratio: float = 0.5,
    minimum_timing_positive_fold_ratio: float = 0.5,
    walk_forward_windows: int = 3,
    minimum_trades: int | None = None,
    top_n: int = 5,
    validation_scope: ValidationScope = "final",
) -> StrategyOptimizationResult:
    """Select on walk-forward windows and evaluate one untouched validation slice."""
    if strategy_id in PASSIVE_STRATEGY_IDS:
        raise ValueError("Passive allocation templates do not use parameter optimization.")
    if not 0.65 <= train_ratio <= 0.85:
        raise ValueError("train_ratio must be between 0.65 and 0.85.")
    if minimum_trades_per_year < 1:
        raise ValueError("minimum_trades_per_year must be at least 1.")
    if (
        maximum_trades_per_year is not None
        and maximum_trades_per_year < minimum_trades_per_year
    ):
        raise ValueError(
            "maximum_trades_per_year must be at least minimum_trades_per_year."
        )
    if not 0 <= minimum_exposure <= 1:
        raise ValueError("minimum_exposure must be between 0 and 1.")
    if minimum_annualized_return is not None and minimum_annualized_return < -1:
        raise ValueError("minimum_annualized_return must be at least -1.")
    if maximum_drawdown is not None and not 0 < maximum_drawdown <= 1:
        raise ValueError("maximum_drawdown must be between 0 and 1.")
    if not 0 < maximum_cash_streak_ratio <= 1:
        raise ValueError("maximum_cash_streak_ratio must be between 0 and 1.")
    if maximum_cash_streak_bars is not None and maximum_cash_streak_bars < 1:
        raise ValueError("maximum_cash_streak_bars must be at least 1.")
    if not 0 <= minimum_profitable_fold_ratio <= 1:
        raise ValueError("minimum_profitable_fold_ratio must be between 0 and 1.")
    if not 0 <= minimum_timing_positive_fold_ratio <= 1:
        raise ValueError(
            "minimum_timing_positive_fold_ratio must be between 0 and 1."
        )
    if not 2 <= walk_forward_windows <= 5:
        raise ValueError("walk_forward_windows must be between 2 and 5.")

    frame = data.sort_index()
    if len(frame) < 100:
        raise ValueError("Parameter optimization requires at least 100 bars.")
    split_index = int(len(frame) * train_ratio)
    if split_index < 70 or len(frame) - split_index < 20:
        raise ValueError("The selected interval is too short for walk-forward validation.")

    definition, _ = get_strategy(strategy_id)
    _, benchmark_strategy = get_strategy("buy-hold")
    config = config or BacktestConfig()
    benchmark_config = config.model_copy(update={"signal_delay_bars": 0})
    development = frame.iloc[:split_index]
    development_benchmark = run_backtest(
        development,
        benchmark_strategy(development, {}),
        benchmark_config,
        include_details=False,
    )
    required_trades = (
        minimum_trades
        if minimum_trades is not None
        else max(
            3,
            math.ceil(
                evaluation_duration_years(
                    development.index,
                    config.annual_periods,
                )
                * minimum_trades_per_year
            ),
        )
    )
    folds = _fold_boundaries(split_index, walk_forward_windows)
    fold_benchmarks = [
        run_backtest(
            frame.iloc[:validation_end],
            benchmark_strategy(frame.iloc[:validation_end], {}),
            benchmark_config,
            include_details=False,
            evaluation_start=validation_start,
        )
        for _, _, validation_start, validation_end in folds
    ]
    eligible: list[OptimizationCandidate] = []
    all_parameters = parameter_candidates(strategy_id)
    signal_cache: dict[tuple[tuple[str, float], ...], pd.Series] = {}

    def signal_for(parameters: dict[str, float]) -> pd.Series:
        key = tuple(sorted(parameters.items()))
        if key not in signal_cache:
            signal_cache[key] = strategy_signals(
                frame,
                strategy_id,
                parameters,
                signal_history=signal_history,
            )
        return signal_cache[key]

    def cycle_baseline_for(
        signals: pd.Series,
        parameters: dict[str, float],
    ) -> pd.Series | None:
        return strategy_cycle_baseline_signals(
            strategy_id,
            signals,
            parameters,
        )

    for parameters in all_parameters:
        merged = {**definition.parameters, **parameters}
        signals = signal_for(merged)
        development_signals = signals.iloc[:split_index]
        development_result = run_backtest(
            development,
            development_signals,
            config,
            include_details=False,
            cycle_baseline_signals=cycle_baseline_for(
                development_signals,
                merged,
            ),
        )
        if not _participates(
            development_result.metrics,
            minimum_trades=required_trades,
            maximum_trades_per_year=maximum_trades_per_year,
            minimum_exposure=minimum_exposure,
            minimum_annualized_return=minimum_annualized_return,
            maximum_drawdown=maximum_drawdown,
            maximum_cash_streak_ratio=maximum_cash_streak_ratio,
            maximum_cash_streak_bars=maximum_cash_streak_bars,
        ):
            continue

        fold_scores: list[float] = []
        fold_timing_excess_returns: list[float] = []
        profitable_folds = 0
        timing_positive_folds = 0
        fold_is_valid = True
        for fold_index, (_, _train_end, validation_start, validation_end) in enumerate(
            folds
        ):
            validation_context = frame.iloc[:validation_end]
            validation_signals = signals.iloc[:validation_end]
            validation_result = run_backtest(
                validation_context,
                validation_signals,
                config,
                include_details=False,
                evaluation_start=validation_start,
                cycle_baseline_signals=cycle_baseline_for(
                    validation_signals,
                    merged,
                ),
            )
            fold_benchmark = fold_benchmarks[fold_index]
            fold_exposure_matched_benchmark = run_exposure_matched_benchmark(
                validation_context,
                validation_result.metrics.exposure_ratio,
                config,
                include_details=False,
                evaluation_start=validation_start,
            )
            timing_excess_return = (
                validation_result.metrics.total_return
                - fold_exposure_matched_benchmark.metrics.total_return
            )
            # A position established before the validation boundary is carried
            # into this fold.  Requiring a new transaction in every short fold
            # would either reject a genuine low-turnover strategy or reward a
            # reset-to-cash simulation with a fictitious entry.  The full
            # development window still enforces the requested annual frequency.
            fold_minimum_trades = math.floor(
                evaluation_duration_years(
                    frame.index[validation_start:validation_end],
                    config.annual_periods,
                )
                * minimum_trades_per_year
                * 0.5
            )
            if validation_result.metrics.trades < fold_minimum_trades:
                fold_is_valid = False
                break
            if validation_result.metrics.exposure_ratio < minimum_exposure * 0.5:
                fold_is_valid = False
                break
            if (
                maximum_drawdown is not None
                and abs(validation_result.metrics.max_drawdown) > maximum_drawdown
            ):
                fold_is_valid = False
                break
            if (
                maximum_trades_per_year is not None
                and validation_result.metrics.trades_per_year
                > maximum_trades_per_year * 1.5
            ):
                fold_is_valid = False
                break
            if (
                maximum_cash_streak_bars is not None
                and validation_result.metrics.max_cash_streak
                > maximum_cash_streak_bars
            ):
                fold_is_valid = False
                break
            if validation_result.metrics.total_return > 0:
                profitable_folds += 1
            if timing_excess_return > 0:
                timing_positive_folds += 1
            fold_timing_excess_returns.append(timing_excess_return)
            fold_scores.append(
                _score(
                    validation_result.metrics,
                    objective,
                    fold_benchmark.metrics,
                    fold_exposure_matched_benchmark.metrics,
                )
            )
        if not fold_is_valid:
            continue
        profitable_fold_ratio = profitable_folds / len(fold_scores)
        if profitable_fold_ratio < minimum_profitable_fold_ratio:
            continue
        timing_positive_fold_ratio = timing_positive_folds / len(fold_scores)
        if timing_positive_fold_ratio < minimum_timing_positive_fold_ratio:
            continue

        stability = pstdev(fold_scores) if len(fold_scores) > 1 else 0.0
        robust_score = (
            fmean(fold_scores)
            - 0.35 * stability
            + 0.1 * profitable_fold_ratio
            + 0.15 * timing_positive_fold_ratio
        )
        eligible.append(
            OptimizationCandidate(
                parameters=merged,
                score=robust_score,
                score_stability=stability,
                metrics=development_result.metrics,
                fold_scores=fold_scores,
                fold_timing_excess_returns=fold_timing_excess_returns,
                profitable_fold_ratio=profitable_fold_ratio,
                timing_positive_fold_ratio=timing_positive_fold_ratio,
            )
        )

    if not eligible:
        constraints = [
            f"开发期至少 {required_trades} 笔交易",
            f"持仓率至少 {minimum_exposure:.0%}",
        ]
        if minimum_annualized_return is not None:
            constraints.append(f"年化收益至少 {minimum_annualized_return:.0%}")
        if maximum_drawdown is not None:
            constraints.append(f"最大回撤不超过 {maximum_drawdown:.0%}")
        constraints.append(
            "最长连续空仓不超过 "
            f"{maximum_cash_streak_bars or f'{maximum_cash_streak_ratio:.0%}'}"
        )
        constraints.append(
            f"至少 {minimum_profitable_fold_ratio:.0%} 的走步窗口为正收益"
        )
        constraints.append(
            "至少 "
                    f"{minimum_timing_positive_fold_ratio:.0%} 的走步窗口跑赢"
                    "相同起点投入比例的固定份额基准"
        )
        raise ValueError(
            "没有候选参数同时满足稳健性约束："
            + "、".join(constraints)
            + "。"
            "请延长区间，或适度放宽参与度约束。"
        )
    eligible.sort(
        key=lambda candidate: (
            candidate.score,
            candidate.metrics.total_return,
            candidate.metrics.trades,
        ),
        reverse=True,
    )
    selected = eligible[0]
    selected_signals = signal_for(selected.parameters)
    holdout_result = run_backtest(
        frame,
        selected_signals,
        config,
        include_details=False,
        evaluation_start=split_index,
        cycle_baseline_signals=cycle_baseline_for(
            selected_signals,
            selected.parameters,
        ),
    )
    holdout_benchmark = run_backtest(
        frame,
        benchmark_strategy(frame, {}),
        benchmark_config,
        include_details=False,
        evaluation_start=split_index,
    )
    holdout_exposure_matched_benchmark = run_exposure_matched_benchmark(
        frame,
        holdout_result.metrics.exposure_ratio,
        config,
        include_details=False,
        evaluation_start=split_index,
    )
    selected_cycle_baseline = cycle_baseline_for(
        selected_signals,
        selected.parameters,
    )
    full_result = run_backtest(
        frame,
        selected_signals,
        config,
        cycle_baseline_signals=selected_cycle_baseline,
    )
    development_exposure_matched_benchmark = run_exposure_matched_benchmark(
        development,
        selected.metrics.exposure_ratio,
        config,
        include_details=False,
    )
    (
        validation_passed,
        validation_code,
        validation_reason,
    ) = validate_holdout_detailed(
        holdout_result.metrics,
        holdout_benchmark.metrics,
        holdout_exposure_matched_benchmark.metrics,
        scope=validation_scope,
    )
    cost_stress_tests = run_cost_stress_tests(
        frame,
        selected_signals,
        config,
        evaluation_start=split_index,
        cycle_baseline_signals=selected_cycle_baseline,
    )
    cost_stress_passed = all(item.passed for item in cost_stress_tests)
    if (
        validation_passed
        and minimum_annualized_return is not None
        and holdout_result.metrics.annualized_return < minimum_annualized_return
    ):
        validation_passed = False
        validation_code = "return_target_failed"
        validation_reason = (
            "最终留出期年化收益低于设定目标，候选未通过收益门槛。"
        )
    if (
        validation_passed
        and maximum_drawdown is not None
        and abs(holdout_result.metrics.max_drawdown) > maximum_drawdown
    ):
        validation_passed = False
        validation_code = "drawdown_limit_failed"
        validation_reason = (
            "最终留出期最大回撤超过风险预算，候选未通过回撤门槛。"
        )
    if (
        validation_passed
        and holdout_result.metrics.trades_per_year
        < minimum_trades_per_year * 0.5
    ):
        validation_passed = False
        validation_code = "frequency_too_low"
        validation_reason = (
            "最终留出期交易频率低于最低要求的一半，候选未通过参与度验证。"
        )
    if (
        validation_passed
        and holdout_result.metrics.exposure_ratio < minimum_exposure * 0.5
    ):
        validation_passed = False
        validation_code = "exposure_too_low"
        validation_reason = "最终留出期持仓率低于最低要求的一半，候选长期空仓。"
    if (
        validation_passed
        and maximum_trades_per_year is not None
        and holdout_result.metrics.trades_per_year > maximum_trades_per_year * 1.5
    ):
        validation_passed = False
        validation_code = "frequency_too_high"
        validation_reason = (
            "最终留出期交易频率明显超过上限，成本与过度交易风险不可接受。"
        )
    if (
        validation_passed
        and maximum_cash_streak_bars is not None
        and holdout_result.metrics.max_cash_streak > maximum_cash_streak_bars
    ):
        validation_passed = False
        validation_code = "cash_streak_too_long"
        validation_reason = (
            "最终留出期连续空仓 K 线超过上限，候选未通过参与度验证。"
        )
    if validation_passed and not cost_stress_passed:
        validation_passed = False
        validation_code = "cost_stress_failed"
        validation_reason = (
            "最终留出期在提高交易成本后未同时保持正收益和相同起点投入比例的固定份额基准之上的择时优势，"
            "候选未通过成本压力测试。"
        )
    if (
        validation_passed
        and holdout_result.metrics.closed_trades < FINAL_HOLDOUT_MIN_CLOSED_TRADES
    ):
        validation_passed = False
        validation_code = "sample_insufficient"
        validation_reason = (
            "最终留出期少于 "
            f"{FINAL_HOLDOUT_MIN_CLOSED_TRADES} 个已闭合独立决策周期，"
            "样本不足以通过参与度验证。"
        )
    forward_observation_eligible = is_forward_observation_eligible(
        validation_passed=validation_passed,
        validation_code=validation_code,
        closed_trades=holdout_result.metrics.closed_trades,
        cost_stress_passed=cost_stress_passed,
    )
    selected_folds: list[WalkForwardFold] = []
    for train_start, train_end, validation_start, validation_end in folds:
        # This selection is deliberately repeated for every fold using only
        # information available before that fold's validation boundary.
        # The final parameter set above is still selected on the complete
        # development period before the untouched outer holdout is read.
        fold_training = frame.iloc[train_start:train_end]
        fold_training_benchmark = run_backtest(
            fold_training,
            benchmark_strategy(fold_training, {}),
            benchmark_config,
            include_details=False,
        )
        fold_candidates: list[tuple[float, dict[str, float]]] = []
        fold_required_trades = math.floor(
            evaluation_duration_years(
                fold_training.index,
                config.annual_periods,
            )
            * minimum_trades_per_year
            * 0.5
        )
        for parameters in all_parameters:
            fold_parameters = {**definition.parameters, **parameters}
            fold_signals = signal_for(fold_parameters)
            fold_training_signals = fold_signals.iloc[train_start:train_end]
            fold_training_result = run_backtest(
                fold_training,
                fold_training_signals,
                config,
                include_details=False,
                cycle_baseline_signals=cycle_baseline_for(
                    fold_training_signals,
                    fold_parameters,
                ),
            )
            if not _participates(
                fold_training_result.metrics,
                minimum_trades=fold_required_trades,
                maximum_trades_per_year=maximum_trades_per_year,
                # A fold's early training prefix may be mostly indicator
                # warm-up.  These quality gates apply to its unseen
                # validation segment and the final holdout, not to choosing
                # among otherwise runnable parameters on a tiny prefix.
                minimum_exposure=0,
                minimum_annualized_return=None,
                maximum_drawdown=None,
                maximum_cash_streak_ratio=1,
                maximum_cash_streak_bars=None,
            ):
                continue
            fold_training_exposure_benchmark = run_exposure_matched_benchmark(
                fold_training,
                fold_training_result.metrics.exposure_ratio,
                config,
                include_details=False,
            )
            fold_candidates.append(
                (
                    _score(
                        fold_training_result.metrics,
                        objective,
                        fold_training_benchmark.metrics,
                        fold_training_exposure_benchmark.metrics,
                    ),
                    fold_parameters,
                )
            )
        if not fold_candidates:
            raise ValueError(
                "No parameter set was eligible before a walk-forward validation window."
            )
        _, fold_parameters = max(fold_candidates, key=lambda item: item[0])
        fold_signals = signal_for(fold_parameters)
        fold_context = frame.iloc[:validation_end]
        fold_context_signals = fold_signals.iloc[:validation_end]
        fold_result = run_backtest(
            fold_context,
            fold_context_signals,
            config,
            include_details=False,
            evaluation_start=validation_start,
            cycle_baseline_signals=cycle_baseline_for(
                fold_context_signals,
                fold_parameters,
            ),
        )
        fold_exposure_matched_benchmark = run_exposure_matched_benchmark(
            fold_context,
            fold_result.metrics.exposure_ratio,
            config,
            include_details=False,
            evaluation_start=validation_start,
        )
        fold_timing_excess_return = (
            fold_result.metrics.total_return
            - fold_exposure_matched_benchmark.metrics.total_return
        )
        selected_folds.append(
            WalkForwardFold(
                train_start=_date_value(frame.index[train_start]),
                train_end=_date_value(frame.index[train_end - 1]),
                validation_start=_date_value(frame.index[validation_start]),
                validation_end=_date_value(frame.index[validation_end - 1]),
                selected_parameters=fold_parameters,
                validation_metrics=fold_result.metrics,
                exposure_matched_benchmark_metrics=(
                    fold_exposure_matched_benchmark.metrics
                ),
                timing_excess_return=fold_timing_excess_return,
                timing_value_added=fold_timing_excess_return > 0,
            )
        )
    return StrategyOptimizationResult(
        objective=objective,
        selected_parameters=selected.parameters,
        train_ratio=train_ratio,
        split_date=_date_value(frame.index[split_index]),
        candidates_evaluated=len(all_parameters),
        candidates_eligible=len(eligible),
        minimum_trades=required_trades,
        minimum_trades_per_year=minimum_trades_per_year,
        maximum_trades_per_year=maximum_trades_per_year,
        minimum_exposure=minimum_exposure,
        minimum_annualized_return=minimum_annualized_return,
        maximum_drawdown=maximum_drawdown,
        maximum_cash_streak_ratio=maximum_cash_streak_ratio,
        maximum_cash_streak_bars=maximum_cash_streak_bars,
        minimum_profitable_fold_ratio=minimum_profitable_fold_ratio,
        minimum_timing_positive_fold_ratio=minimum_timing_positive_fold_ratio,
        train_metrics=selected.metrics,
        validation_metrics=holdout_result.metrics,
        development_benchmark_metrics=development_benchmark.metrics,
        development_exposure_matched_benchmark_metrics=(
            development_exposure_matched_benchmark.metrics
        ),
        validation_benchmark_metrics=holdout_benchmark.metrics,
        validation_exposure_matched_benchmark_metrics=(
            holdout_exposure_matched_benchmark.metrics
        ),
        validation_excess_return=(
            holdout_result.metrics.total_return
            - holdout_benchmark.metrics.total_return
        ),
        validation_timing_excess_return=(
            holdout_result.metrics.total_return
            - holdout_exposure_matched_benchmark.metrics.total_return
        ),
        validation_passed=validation_passed,
        validation_code=validation_code,
        validation_reason=validation_reason,
        forward_observation_eligible=forward_observation_eligible,
        cost_stress_passed=cost_stress_passed,
        cost_stress_tests=cost_stress_tests,
        walk_forward_folds=selected_folds,
        top_candidates=eligible[:top_n],
        full_result=full_result,
    )


def _fold_boundaries(
    development_end: int,
    fold_count: int,
) -> list[tuple[int, int, int, int]]:
    initial_training = max(50, int(development_end * 0.5))
    remaining = development_end - initial_training
    validation_size = remaining // fold_count
    if validation_size < 8:
        raise ValueError("Not enough bars for the requested walk-forward windows.")
    folds: list[tuple[int, int, int, int]] = []
    for index in range(fold_count):
        validation_start = initial_training + validation_size * index
        validation_end = (
            development_end
            if index == fold_count - 1
            else validation_start + validation_size
        )
        folds.append((0, validation_start, validation_start, validation_end))
    return folds


def _participates(
    metrics: BacktestMetrics,
    *,
    minimum_trades: int,
    maximum_trades_per_year: int | None,
    minimum_exposure: float,
    minimum_annualized_return: float | None,
    maximum_drawdown: float | None,
    maximum_cash_streak_ratio: float,
    maximum_cash_streak_bars: int | None,
) -> bool:
    return research_risk_allows(
        trades=metrics.trades,
        trades_per_year=metrics.trades_per_year,
        exposure_ratio=metrics.exposure_ratio,
        annualized_return=metrics.annualized_return,
        max_drawdown=metrics.max_drawdown,
        max_cash_streak_ratio=metrics.max_cash_streak_ratio,
        max_cash_streak_bars=metrics.max_cash_streak,
        minimum_trades=minimum_trades,
        maximum_trades_per_year=maximum_trades_per_year,
        minimum_exposure=minimum_exposure,
        minimum_annualized_return=minimum_annualized_return,
        maximum_drawdown=maximum_drawdown,
        maximum_cash_streak_ratio=maximum_cash_streak_ratio,
        maximum_cash_streak_bars=maximum_cash_streak_bars,
    )


def score_optimization_metrics(
    metrics: BacktestMetrics,
    objective: OptimizationObjective,
    benchmark: BacktestMetrics | None = None,
    exposure_matched_benchmark: BacktestMetrics | None = None,
) -> float:
    """Rank candidates observed over the same evidence window.

    Candidate scores are compared only inside one development or walk-forward
    window.  Use the realized compounded return from that window rather than
    CAGR: annualizing a few intraday bars is useful as an explicitly requested
    hard constraint, but it is an unstable ranking signal and can saturate the
    published annualized-return cap.  Total-return differences also keep the
    market and exposure-matched comparisons on the same evidence horizon.
    """
    excess_return = (
        metrics.total_return - benchmark.total_return
        if benchmark is not None
        else 0.0
    )
    timing_excess_return = (
        metrics.total_return
        - exposure_matched_benchmark.total_return
        if exposure_matched_benchmark is not None
        else 0.0
    )
    if objective == "total_return":
        value = (
            metrics.total_return
            + 0.15 * excess_return
            + 0.35 * timing_excess_return
        )
    elif objective == "sharpe_ratio":
        value = (
            metrics.sharpe_ratio
            + 0.1 * excess_return
            + 0.25 * timing_excess_return
        )
    elif objective == "drawdown_control":
        value = (
            metrics.max_drawdown
            + 0.05 * metrics.total_return
            + 0.05 * excess_return
            + 0.25 * timing_excess_return
            + 0.01 * metrics.exposure_ratio
        )
    else:
        drawdown_floor = max(abs(metrics.max_drawdown), 0.02)
        value = (
            metrics.total_return / drawdown_floor
            + 0.1 * metrics.sharpe_ratio
            + 0.5 * excess_return
            + 1.0 * timing_excess_return
            + 0.05 * metrics.exposure_ratio
        )
    return float(value) if np.isfinite(value) else -1e12


# Kept as a private compatibility alias for focused engine tests and callers
# inside this module. Public cross-package callers use the named shared scorer.
_score = score_optimization_metrics


def validate_holdout(
    strategy: BacktestMetrics,
    benchmark: BacktestMetrics,
) -> tuple[bool, str]:
    passed, _code, reason = validate_holdout_detailed(strategy, benchmark)
    return passed, reason


def validate_holdout_detailed(
    strategy: BacktestMetrics,
    benchmark: BacktestMetrics,
    exposure_matched_benchmark: BacktestMetrics | None = None,
    *,
    scope: ValidationScope = "final",
) -> tuple[bool, HoldoutValidationCode, str]:
    scope_name = "开发期内层留出" if scope == "development_inner" else "最终留出期"
    validation_name = "开发验证" if scope == "development_inner" else "最终验证"
    if strategy.total_return <= 0:
        return (
            False,
            "negative_return",
            f"{scope_name}收益为负，参数未通过{validation_name}。",
        )
    if (
        exposure_matched_benchmark is not None
        and strategy.total_return <= exposure_matched_benchmark.total_return
    ):
        return (
            False,
            "exposure_matched_lag",
            f"{scope_name}未跑赢相同起点投入比例的固定份额买入持有，"
            "空仓降低回撤但择时没有增加价值。",
        )
    if benchmark.total_return > 0:
        benchmark_capture = strategy.total_return / benchmark.total_return
        drawdown_improved = abs(strategy.max_drawdown) <= abs(benchmark.max_drawdown) * 0.8
        if benchmark_capture < 0.5 and not drawdown_improved:
            return (
                False,
                "benchmark_capture_failed",
                f"{scope_name}仅捕获不足一半基准收益，且回撤改善不明显。",
            )
    elif strategy.total_return <= benchmark.total_return:
        return (
            False,
            "weak_market_lag",
            f"{scope_name}未能跑赢处于弱势阶段的买入持有基准。",
        )
    return (
        True,
        "passed",
        f"{scope_name}保持正收益，并通过收益/回撤相对基准检验。",
    )


def is_forward_observation_eligible(
    *,
    validation_passed: bool,
    validation_code: HoldoutValidationCode,
    closed_trades: int,
    cost_stress_passed: bool,
) -> bool:
    """Allow a frozen forward observation only for one narrow failure mode."""
    return (
        not validation_passed
        and validation_code == "sample_insufficient"
        and FORWARD_OBSERVATION_MIN_CLOSED_TRADES
        <= closed_trades
        < FINAL_HOLDOUT_MIN_CLOSED_TRADES
        and cost_stress_passed
    )


def is_development_selection_eligible(
    *,
    validation_passed: bool,
    validation_code: HoldoutValidationCode,
    closed_trades: int,
    cost_stress_passed: bool,
) -> bool:
    """Allow limited inner evidence to unlock, but never pass, outer validation."""

    return validation_passed or (
        validation_code == "sample_insufficient"
        and closed_trades >= MIN_DEVELOPMENT_SELECTION_CLOSED_TRADES
        and cost_stress_passed
    )


def run_cost_stress_tests(
    data: pd.DataFrame,
    signals: pd.Series,
    config: BacktestConfig,
    multipliers: tuple[float, ...] = (2.0, 3.0),
    *,
    evaluation_start: int = 0,
    cycle_baseline_signals: pd.Series | None = None,
) -> list[CostStressTest]:
    tests: list[CostStressTest] = []
    for multiplier in multipliers:
        if multiplier < 1:
            raise ValueError("Cost stress multipliers must be at least 1.")
        stressed_config = config.model_copy(
            update={
                "fee_rate": min(config.fee_rate * multiplier, 0.1),
                "slippage_rate": min(config.slippage_rate * multiplier, 0.1),
            }
        )
        result = run_backtest(
            data,
            signals,
            stressed_config,
            include_details=False,
            evaluation_start=evaluation_start,
            cycle_baseline_signals=cycle_baseline_signals,
        )
        exposure_matched = run_exposure_matched_benchmark(
            data,
            result.metrics.exposure_ratio,
            stressed_config,
            include_details=False,
            evaluation_start=evaluation_start,
        )
        timing_excess_return = (
            result.metrics.total_return - exposure_matched.metrics.total_return
        )
        positive_return = result.metrics.total_return > 0
        beats_exposure_matched = timing_excess_return > 0
        tests.append(
            CostStressTest(
                multiplier=multiplier,
                fee_rate=stressed_config.fee_rate,
                slippage_rate=stressed_config.slippage_rate,
                metrics=result.metrics,
                positive_return=positive_return,
                exposure_matched_total_return=exposure_matched.metrics.total_return,
                timing_excess_return=timing_excess_return,
                beats_exposure_matched=beats_exposure_matched,
                passed=positive_return and beats_exposure_matched,
            )
        )
    return tests


def _date_value(value: object) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
