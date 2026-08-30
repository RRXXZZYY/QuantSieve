from __future__ import annotations

import asyncio
from datetime import date, timedelta
from math import ceil, prod, sqrt
from time import perf_counter
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from quantsieve_engine import (
    FINAL_HOLDOUT_MIN_CLOSED_TRADES,
    FORWARD_OBSERVATION_MIN_CLOSED_TRADES,
    MIN_DEVELOPMENT_SELECTION_CLOSED_TRADES,
    PASSIVE_STRATEGY_IDS,
    STRATEGIES,
    BacktestConfig,
    HoldoutValidationCode,
    OptimizationObjective,
    PortfolioMethod,
    SandboxError,
    SandboxExecutor,
    adaptive_screening_parameter_candidates,
    evaluation_duration_years,
    get_strategy,
    is_development_selection_eligible,
    is_forward_observation_eligible,
    optimize_strategy,
    parameter_candidates,
    run_backtest,
    run_cost_stress_tests,
    run_exposure_matched_benchmark,
    run_portfolio_backtest,
    run_risk_budgeted_benchmark,
    score_optimization_metrics,
    screening_parameter_candidates,
    strategy_cycle_baseline_signals,
    strategy_signals,
    strategy_warmup_bars,
    validate_holdout_detailed,
)
from quantsieve_providers import (
    BarInterval,
    DataProvider,
    MarketFilter,
    search_popular_instruments,
)

from ..config import Settings
from ..experiments import ExperimentStore
from ..providers import ProviderRouter
from ..research_runs import ResearchRunStore
from ..run_manifests import attach_and_record_backtest_run
from ..schemas import (
    BacktestRequest,
    CompareBacktestRequest,
    CustomBacktestRequest,
    DiscoverBacktestRequest,
    OptimizeBacktestRequest,
    PortfolioBacktestRequest,
    StrategyRobustnessRequest,
)

router = APIRouter(tags=["market"])

_DISCOVERY_SCREENING_INITIAL_PARAMETER_LIMIT = 12
_DISCOVERY_SCREENING_PARAMETER_BUDGET = 24
_DISCOVERY_SCREENING_PROMOTED_REGIONS = 3
_DISCOVERY_SCREENING_GLOBAL_EXPLORATION_FRACTION = 0.25
_DISCOVERY_QUALITY_RANK = {
    "outperform": 0,
    "defensive": 1,
    "lagging": 2,
    "negative": 3,
}
_DISCOVERY_MINIMUM_HORIZON_DAYS: dict[BarInterval, tuple[int, int]] = {
    # A high-frequency strategy can produce ten trades in days. The research
    # window must also cover enough calendar time before we call it validated.
    # The second value is the minimum untouched holdout horizon.
    "15m": (90, 18),
    "1h": (180, 36),
    "4h": (365, 73),
    "1d": (365, 73),
    "1wk": (730, 146),
}
_TREND_FOLLOWING_STRATEGY_IDS = frozenset(
    {
        "sma-cross",
        "ema-cross",
        "momentum",
        "breakout",
        "breakout-atr",
        "macd",
        "volume-breakout",
        "trend-filter",
        "core-trend-allocation",
        "macd-regime",
        "atr-trend",
        "rsi-regime-atr",
    }
)
_MEAN_REVERSION_STRATEGY_IDS = frozenset({"rsi", "bollinger", "mean-reversion"})
_ALLOCATION_STRATEGY_IDS = frozenset(
    {"core-trend-allocation", "volatility-target-trend"}
)
_REGIME_FIT_RANK = {"aligned": 0, "neutral": 1, "counter_regime": 2}
_INTERVALS: tuple[BarInterval, ...] = ("15m", "1h", "4h", "1d", "1wk")
# This is deliberately kept beside the request router rather than inferred from
# a successful history call: a user needs to know a documented upstream window
# *before* spending time configuring an experiment. ``None`` means the adapter
# has no QuantSieve-imposed calendar cap (the upstream may still have history
# gaps, which remain visible in returned metadata).
_PROVIDER_HISTORY_CAPABILITIES: dict[str, dict[BarInterval, int | None]] = {
    "akshare": {"15m": 120, "1h": 120, "4h": 120, "1d": None, "1wk": None},
    "yfinance": {"15m": 60, "1h": 60, "4h": 60, "1d": None, "1wk": 7_300},
    "binance": {"15m": 180, "1h": 730, "4h": 1_825, "1d": 7_300, "1wk": 14_600},
    "futures": {"1d": None, "1wk": None},
    "macro": {"1d": None, "1wk": None},
}


def _is_constraint_rejection(error: ValueError) -> bool:
    """Keep an evidence rejection distinct from a provider/data failure."""

    return str(error).startswith("没有候选参数同时满足稳健性约束")


def _providers(request: Request) -> ProviderRouter:
    return cast(ProviderRouter, request.app.state.providers)


def _experiment_store(request: Request) -> ExperimentStore:
    return cast(ExperimentStore, request.app.state.experiment_store)


def _research_run_store(request: Request) -> ResearchRunStore:
    return cast(ResearchRunStore, request.app.state.research_run_store)


def _history_capabilities(adapter: DataProvider, symbol: str) -> dict[str, Any]:
    provider_capabilities = _PROVIDER_HISTORY_CAPABILITIES.get(adapter.name, {})
    return {
        "symbol": symbol,
        "provider": adapter.name,
        "intervals": {
            interval: {
                "supported": interval in provider_capabilities,
                "max_history_days": provider_capabilities.get(interval),
            }
            for interval in _INTERVALS
        },
    }


async def _verified_portfolio_currency(
    *,
    requested_currency: str,
    canonical_symbol: str,
    adapter: DataProvider,
) -> str:
    known = next(
        (
            instrument
            for instrument in search_popular_instruments(
                canonical_symbol,
                market="all",
                limit=30,
            )
            if instrument.symbol.strip().upper() == canonical_symbol
            and instrument.provider == adapter.name
        ),
        None,
    )
    if known is None:
        try:
            discovered = await adapter.search(canonical_symbol, limit=30)
        except Exception as exc:
            raise LookupError(
                f"无法从 {adapter.name} 验证 {canonical_symbol} 的结算币种，"
                "为避免无汇率换算的错误组合，本次请求已拒绝。"
            ) from exc
        known = next(
            (
                instrument
                for instrument in discovered
                if instrument.symbol.strip().upper() == canonical_symbol
            ),
            None,
        )
    if known is None:
        raise ValueError(
            f"无法从服务端目录验证 {canonical_symbol} 的结算币种，"
            "请先通过标的搜索选择可验证资产。"
        )
    normalized_requested = requested_currency.strip().upper()
    normalized_known = known.currency.strip().upper()
    if normalized_requested != normalized_known:
        raise ValueError(
            f"{canonical_symbol} 的服务端资料结算币种为 {known.currency}，"
            f"不能按 {normalized_requested} 参与组合研究。"
        )
    return normalized_known


def _annual_periods(adapter: DataProvider, interval: BarInterval) -> int:
    if interval == "1wk":
        return 52
    if adapter.name == "binance":
        return {
            "15m": 365 * 24 * 4,
            "1h": 365 * 24,
            "4h": 365 * 6,
            "1d": 365,
        }[interval]
    if adapter.name == "akshare":
        # Mainland continuous trading is 09:30–11:30 and 13:00–15:00:
        # 16 x 15m, 4 x 1h, or one four-trading-hour session composite.
        return {
            "15m": 252 * 16,
            "1h": 252 * 4,
            "4h": 252,
            "1d": 252,
        }[interval]
    return {
        "15m": 252 * 26,
        "1h": 252 * 7,
        "4h": 252 * 2,
        "1d": 252,
    }[interval]


def _minimum_trade_events_for_window(
    index: Any,
    annual_periods: int,
    minimum_trades_per_year: int,
    explicit_minimum: int | None,
) -> int:
    """Resolve a discovery gate on the same elapsed-time basis as the engine."""
    if explicit_minimum is not None:
        return explicit_minimum
    return max(
        3,
        ceil(
            evaluation_duration_years(index, annual_periods)
            * minimum_trades_per_year
        ),
    )


def _backtest_config(
    adapter: DataProvider,
    requested: BacktestConfig | None,
    interval: BarInterval,
) -> BacktestConfig:
    """Build the execution contract shared by active strategy endpoints.

    All built-in and sandbox signals are calculated after an OHLCV bar has
    completed.  Letting a caller set ``signal_delay_bars=0`` here would then
    value a decision made with that bar's close at the same bar's open.  Keep
    the public research endpoints on next-bar-open execution regardless of a
    caller-supplied config.  Passive allocation baselines deliberately opt
    into their separate, known-at-start execution path at their call sites.
    """
    config = requested or BacktestConfig()
    return config.model_copy(
        update={
            "annual_periods": _annual_periods(adapter, interval),
            "bar_interval": interval,
            "signal_delay_bars": 1,
        }
    )


def _maximum_strategy_warmup(strategy_id: str) -> int:
    return max(
        strategy_warmup_bars(strategy_id, parameters)
        for parameters in parameter_candidates(strategy_id)
    )


def _warmup_calendar_days(
    adapter: DataProvider,
    interval: BarInterval,
    bars: int,
) -> int:
    if bars <= 0:
        return 0
    annual_periods = _annual_periods(adapter, interval)
    calendar_factor = 1.05 if adapter.name == "binance" else 1.35
    days = ceil(bars / annual_periods * 365 * calendar_factor) + 7
    if adapter.name == "yfinance" and interval in {"15m", "1h", "4h"}:
        return min(days, 60)
    if adapter.name == "akshare" and interval in {"15m", "1h", "4h"}:
        return min(days, 120)
    return max(days, 2)


async def _history_for_backtest(
    adapter: DataProvider,
    symbol: str,
    start: date | None,
    end: date | None,
    interval: BarInterval,
    warmup_bars: int,
) -> tuple[Any, Any, Any, str | None]:
    history = await adapter.history_interval(symbol, start, end, interval=interval)
    data = history.to_frame()
    signal_history = data.iloc[0:0]
    warmup_error: str | None = None
    if start is None or warmup_bars <= 0 or data.empty:
        return history, data, signal_history, warmup_error

    pre_roll_end = start - timedelta(days=1)
    pre_roll_start = start - timedelta(
        days=_warmup_calendar_days(adapter, interval, warmup_bars)
    )
    try:
        pre_roll = await adapter.history_interval(
            symbol,
            pre_roll_start,
            pre_roll_end,
            interval=interval,
        )
        signal_history = pre_roll.to_frame()
        signal_history = signal_history.loc[
            signal_history.index < data.index[0]
        ].tail(warmup_bars)
        signal_history = signal_history.reindex(columns=data.columns)
    except (RuntimeError, ValueError, LookupError) as exc:
        warmup_error = str(exc)
    return history, data, signal_history, warmup_error


def _with_warmup_metadata(
    history: Any,
    required_bars: int,
    signal_history: Any,
    warmup_error: str | None,
) -> Any:
    updated = history.model_copy(deep=True)
    available = len(signal_history)
    updated.metadata.update(
        {
            "indicator_warmup_required_bars": required_bars,
            "indicator_warmup_available_bars": available,
            "indicator_warmup_complete": available >= required_bars,
            "indicator_warmup_start": (
                _date_value(signal_history.index[0]) if available else None
            ),
            "indicator_warmup_end": (
                _date_value(signal_history.index[-1]) if available else None
            ),
            "indicator_warmup_error": warmup_error,
        }
    )
    return updated


def _discovery_horizon(
    data: Any,
    split_index: int,
    interval: BarInterval,
) -> dict[str, Any]:
    """Describe whether a discovery run spans enough *calendar* history."""

    full_start = data.index[0]
    full_end = data.index[-1]
    holdout_start = data.index[split_index]
    full_days = max(0.0, (full_end - full_start).total_seconds() / 86_400)
    holdout_days = max(0.0, (full_end - holdout_start).total_seconds() / 86_400)
    required_full_days, required_holdout_days = _DISCOVERY_MINIMUM_HORIZON_DAYS[
        interval
    ]
    validation_eligible = (
        full_days >= required_full_days and holdout_days >= required_holdout_days
    )
    return {
        "status": "adequate" if validation_eligible else "short_horizon",
        "validation_eligible": validation_eligible,
        "full_sample_days": round(full_days, 1),
        "minimum_full_sample_days": required_full_days,
        "holdout_days": round(holdout_days, 1),
        "minimum_holdout_days": required_holdout_days,
        "reason": (
            "完整样本和最终留出期均覆盖了该周期的最低日历时间要求。"
            if validation_eligible
            else (
                "当前区间可用于探索和比较，但覆盖的日历时间不足；"
                "不会把任何结果标记为已验证策略，也不能进入前向观察。"
            )
        ),
    }


def _with_discovery_horizon(history: Any, horizon: dict[str, Any]) -> Any:
    updated = history.model_copy(deep=True)
    updated.metadata["discovery_horizon"] = horizon
    return updated


def _benchmark(
    data: Any,
    config: BacktestConfig,
    *,
    evaluation_start: int = 0,
) -> tuple[dict[str, Any], Any]:
    definition, strategy = get_strategy("buy-hold")
    benchmark_config = config.model_copy(update={"signal_delay_bars": 0})
    result = run_backtest(
        data,
        strategy(data, {}),
        benchmark_config,
        evaluation_start=evaluation_start,
    )
    return definition.model_dump(), result


def _comparison(strategy: Any, benchmark: Any) -> dict[str, Any]:
    strategy_metrics = strategy.metrics
    benchmark_metrics = benchmark.metrics
    excess_return = strategy_metrics.total_return - benchmark_metrics.total_return
    drawdown_improvement = (
        abs(benchmark_metrics.max_drawdown) - abs(strategy_metrics.max_drawdown)
    )
    return {
        "excess_return": excess_return,
        "excess_annualized_return": (
            strategy_metrics.annualized_return
            - benchmark_metrics.annualized_return
        ),
        "drawdown_improvement": drawdown_improvement,
        "beats_benchmark": excess_return > 0,
        "positive_return": strategy_metrics.total_return > 0,
    }


def _exposure_matched_benchmark(
    data: Any,
    strategy: Any,
    config: BacktestConfig,
    *,
    include_details: bool = True,
    evaluation_start: int = 0,
) -> Any:
    return run_exposure_matched_benchmark(
        data,
        strategy.metrics.exposure_ratio,
        config,
        include_details=include_details,
        evaluation_start=evaluation_start,
    )


def _timing_comparison(strategy: Any, exposure_matched_benchmark: Any) -> dict[str, Any]:
    excess_return = (
        strategy.metrics.total_return
        - exposure_matched_benchmark.metrics.total_return
    )
    return {
        "target_exposure": strategy.metrics.exposure_ratio,
        "excess_return": excess_return,
        "beats_exposure_matched": excess_return > 0,
    }


def _date_value(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _signal_state(data: Any, signals: Any, result: Any) -> dict[str, Any]:
    aligned = signals.reindex(data.index).fillna(0).clip(0, 1).astype(float)
    latest_signal = float(aligned.iloc[-1])
    latest_position = float(result.equity[-1]["position"])
    changes = aligned.ne(aligned.shift(1).fillna(0))
    change_positions = [index for index, changed in enumerate(changes) if changed]
    last_change_position = change_positions[-1] if change_positions else 0
    last_change_at = _date_value(data.index[last_change_position])
    previous_signal = (
        float(aligned.iloc[last_change_position - 1])
        if last_change_position > 0
        else 0.0
    )
    bars_in_state = len(aligned) - last_change_position
    if latest_signal > latest_position:
        status = "pending_entry"
        pending_action = "buy_next_open"
    elif latest_signal < latest_position:
        status = "pending_exit"
        pending_action = "sell_next_open"
    elif latest_position > 0:
        status = "holding"
        pending_action = "hold"
    else:
        status = "cash"
        pending_action = "wait"
    current_cash_streak = 0
    for row in reversed(result.equity):
        if float(row["position"]) > 0:
            break
        current_cash_streak += 1
    return {
        "status": status,
        "requested_signal": latest_signal,
        "executed_position": latest_position,
        "pending_action": pending_action,
        "latest_bar_at": _date_value(data.index[-1]),
        "last_signal_change_at": last_change_at,
        "last_signal_from": previous_signal,
        "last_signal_to": float(aligned.iloc[last_change_position]),
        "bars_in_signal_state": bars_in_state,
        "bars_in_executed_cash": current_cash_streak,
    }


def _fixed_share_signal_state(
    data: Any,
    result: Any,
    initial_allocation: float,
) -> dict[str, Any]:
    """Describe a buy-once allocation without inventing rebalance orders.

    A fixed-share baseline's capital exposure drifts with price. Comparing that
    drift with its initial allocation as though both were target weights would
    produce false pending buy/sell signals, even though this baseline has no
    rebalance rule.
    """
    latest_position = (
        float(result.equity[-1]["position"]) if result.equity else 0.0
    )
    invested = initial_allocation > 1e-12
    return {
        "status": "holding" if invested else "cash",
        "requested_signal": float(initial_allocation),
        "executed_position": latest_position,
        "pending_action": "hold" if invested else "wait",
        "latest_bar_at": _date_value(data.index[-1]),
        "last_signal_change_at": _date_value(data.index[0]),
        "last_signal_from": 0.0,
        "last_signal_to": float(initial_allocation),
        "bars_in_signal_state": len(data),
        "bars_in_executed_cash": 0 if invested else len(data),
    }


def _segment_return(equity_rows: list[dict[str, Any]]) -> float:
    return float(prod(1 + float(row["return"]) for row in equity_rows) - 1)


def _segment_drawdown(equity_rows: list[dict[str, Any]]) -> float:
    growth = 1.0
    peak = 1.0
    worst = 0.0
    for row in equity_rows:
        growth *= 1 + float(row["return"])
        peak = max(peak, growth)
        worst = min(worst, growth / peak - 1)
    return float(worst)


def _segment_diagnostics(strategy: Any, benchmark: Any) -> dict[str, Any]:
    count = min(len(strategy.equity), len(benchmark.equity))
    segment_count = min(4, count)
    if segment_count == 0:
        return {
            "segments": [],
            "profitable_segment_ratio": 0.0,
            "benchmark_beaten_segment_ratio": 0.0,
            "worst_segment_return": 0.0,
        }
    base_size, remainder = divmod(count, segment_count)
    rows: list[dict[str, Any]] = []
    cursor = 0
    for index in range(segment_count):
        size = base_size + (1 if index < remainder else 0)
        end = cursor + size
        strategy_rows = strategy.equity[cursor:end]
        benchmark_rows = benchmark.equity[cursor:end]
        strategy_return = _segment_return(strategy_rows)
        benchmark_return = _segment_return(benchmark_rows)
        rows.append(
            {
                "index": index + 1,
                "start": strategy_rows[0]["date"],
                "end": strategy_rows[-1]["date"],
                "bars": size,
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "excess_return": strategy_return - benchmark_return,
                "max_drawdown": _segment_drawdown(strategy_rows),
                "profitable": strategy_return > 0,
                "beats_benchmark": strategy_return > benchmark_return,
            }
        )
        cursor = end
    return {
        "segments": rows,
        "profitable_segment_ratio": (
            sum(bool(row["profitable"]) for row in rows) / len(rows)
        ),
        "benchmark_beaten_segment_ratio": (
            sum(bool(row["beats_benchmark"]) for row in rows) / len(rows)
        ),
        "worst_segment_return": min(float(row["strategy_return"]) for row in rows),
    }


def _trade_quality(result: Any) -> dict[str, Any]:
    returns = [
        float(row["return"])
        for row in result.position_cycles
        if bool(row.get("closed", False))
    ]
    count = len(returns)
    metrics = getattr(result, "metrics", None)
    reported_count = getattr(metrics, "closed_trades", count)
    if reported_count != count:
        raise ValueError(
            "Trade-quality details are incomplete: closed position-cycle "
            f"rows={count}, metrics.closed_trades={reported_count}."
        )
    winners = [value for value in returns if value > 0]
    losers = [value for value in returns if value < 0]
    win_rate = len(winners) / count if count else 0.0
    if count:
        z = 1.96
        denominator = 1 + z**2 / count
        center = (win_rate + z**2 / (2 * count)) / denominator
        margin = (
            z
            * sqrt((win_rate * (1 - win_rate) + z**2 / (4 * count)) / count)
            / denominator
        )
        confidence_low = max(0.0, center - margin)
        confidence_high = min(1.0, center + margin)
    else:
        confidence_low = 0.0
        confidence_high = 1.0
    average_winner = sum(winners) / len(winners) if winners else 0.0
    average_loser = sum(losers) / len(losers) if losers else 0.0
    payoff_ratio = (
        average_winner / abs(average_loser)
        if average_winner > 0 and average_loser < 0
        else None
    )
    sample_quality = (
        "mature"
        if count >= 100
        else "developing"
        if count >= 30
        else "insufficient"
    )
    return {
        "closed_trades": count,
        "sample_quality": sample_quality,
        "win_rate": win_rate,
        "win_rate_confidence_low": confidence_low,
        "win_rate_confidence_high": confidence_high,
        "average_winner": average_winner,
        "average_loser": average_loser,
        "payoff_ratio": payoff_ratio,
        "expectancy": sum(returns) / count if count else 0.0,
    }


def _diagnostics(data: Any, signals: Any, strategy: Any, benchmark: Any) -> dict[str, Any]:
    return {
        "signal_state": _signal_state(data, signals, strategy),
        "trade_quality": _trade_quality(strategy),
        **_segment_diagnostics(strategy, benchmark),
    }


def _template_score(
    strategy: Any,
    benchmark: Any,
    exposure_matched_benchmark: Any | None = None,
) -> tuple[float, str]:
    """Rank templates on realized evidence from their shared sample window.

    Annualized return remains a reported metric and an explicit discovery
    constraint.  It is deliberately excluded from this ranking score because a
    short intraday move can otherwise be extrapolated to the CAGR saturation
    limit even though every template was observed over the same few bars.
    """
    metrics = strategy.metrics
    baseline = benchmark.metrics
    excess_return = metrics.total_return - baseline.total_return
    timing_excess_return = (
        metrics.total_return
        - exposure_matched_benchmark.metrics.total_return
        if exposure_matched_benchmark is not None
        else excess_return
    )
    drawdown_improvement = abs(baseline.max_drawdown) - abs(metrics.max_drawdown)
    score = (
        metrics.total_return / max(abs(metrics.max_drawdown), 0.02)
        + 0.2 * metrics.sharpe_ratio
        + 0.5 * excess_return
        + 0.75 * timing_excess_return
    )
    if metrics.total_return <= 0:
        quality = "negative"
    elif timing_excess_return <= 0:
        quality = "lagging"
    elif metrics.total_return > baseline.total_return:
        quality = "outperform"
    elif drawdown_improvement >= 0.1:
        quality = "defensive"
    else:
        quality = "lagging"
    return score, quality


def _screening_objective_score(
    strategy: Any,
    benchmark: Any,
    exposure_matched_benchmark: Any,
    objective: OptimizationObjective,
) -> float:
    """Use the exact same objective semantics as full development optimization."""

    return score_optimization_metrics(
        strategy.metrics,
        objective,
        benchmark.metrics,
        exposure_matched_benchmark.metrics,
    )


def _screening_sort_key(item: dict[str, Any], objective: str) -> tuple[Any, ...]:
    constraints = bool(item["constraint_reasons"])
    quality = _DISCOVERY_QUALITY_RANK[str(item["quality"])]
    if objective == "balanced":
        return constraints, quality, -float(item["score"])
    return constraints, -float(item["screening_objective_score"]), quality, -float(item["score"])


def _discovery_shortlist_sort_key(
    item: dict[str, Any],
    objective: str,
) -> tuple[Any, ...]:
    """Prefer feasible templates designed for the requested K-line interval.

    An interval recommendation is a strategy-structure prior, not a claim of
    profitability.  It should nevertheless guide the short list once hard
    development constraints are satisfied; otherwise a one-off score from an
    unsuitable interval can consume one of the few expensive full optimizations.
    Unsupported intervals remain available as a clearly marked fallback when no
    recommended template satisfies the same base constraints.
    """

    eligible = not bool(item["screening_constraint_reasons"])
    interval_recommended = bool(item["interval_recommended"])
    tier = (
        0
        if eligible and interval_recommended
        else 1
        if eligible
        else 2
        if interval_recommended
        else 3
    )
    return (
        tier,
        *_screening_sort_key(
            {
                "constraint_reasons": item["screening_constraint_reasons"],
                "quality": item["quality"],
                "score": item["score"],
                "screening_objective_score": item["screening_objective_score"],
            },
            objective,
        ),
        _REGIME_FIT_RANK[str(item["market_regime_fit"])],
    )


def _discovery_strategy_family(strategy_id: str) -> str:
    """Return a broad hypothesis family, never a claim about profitability."""

    if strategy_id in _MEAN_REVERSION_STRATEGY_IDS:
        return "mean_reversion"
    if strategy_id in _ALLOCATION_STRATEGY_IDS:
        return "risk_managed_allocation"
    return "trend_or_breakout"


def _select_diverse_discovery_shortlist(
    ranked: list[dict[str, Any]],
    maximum_candidates: int,
) -> list[dict[str, Any]]:
    """Preserve separate feasible hypotheses before filling by development rank.

    The development ranking stays authoritative within a family.  This only
    prevents several near-identical trend variants from exhausting the small,
    expensive optimization budget before a feasible mean-reversion or
    risk-managed allocation hypothesis receives the same development-only
    audit.  Final holdout data never reaches this function.
    """

    if maximum_candidates <= 0:
        return []
    selected: list[dict[str, Any]] = []
    selected_families: set[str] = set()
    for item in ranked:
        if item["screening_constraint_reasons"]:
            continue
        family = _discovery_strategy_family(str(item["strategy"]["id"]))
        if family not in selected_families:
            selected.append(item)
            selected_families.add(family)
        if len(selected) >= maximum_candidates:
            return selected
    for item in ranked:
        if item in selected:
            continue
        selected.append(item)
        if len(selected) >= maximum_candidates:
            break
    return selected


def _feasible_discovery_strategy_families(
    ranked: list[dict[str, Any]],
) -> list[str]:
    """Expose which independent hypotheses passed development-only hard gates.

    This is deliberately derived before optimization and final-holdout handling.
    It lets the client distinguish "not investigated" from "investigated but no
    parameter set was feasible under the user's stated constraints".
    """

    families: list[str] = []
    for item in ranked:
        if item["screening_constraint_reasons"]:
            continue
        family = _discovery_strategy_family(str(item["strategy"]["id"]))
        if family not in families:
            families.append(family)
    return families


def _development_market_regime(data: Any) -> dict[str, Any]:
    """Classify only the development history; never inspect the final holdout."""

    close = data["close"].astype(float)
    returns = close.pct_change().dropna()
    sample_bars = len(close)
    if returns.empty:
        return {
            "classification": "insufficient",
            "direction": "flat",
            "net_return": 0.0,
            "path_efficiency": 0.0,
            "sample_bars": sample_bars,
            "reason": "开发样本不足以判断趋势或震荡状态。",
        }
    net_return = float(close.iloc[-1] / close.iloc[0] - 1)
    path_return = float(returns.abs().sum())
    path_efficiency = abs(net_return) / max(path_return, 1e-12)
    direction = "rising" if net_return > 0.02 else "falling" if net_return < -0.02 else "flat"
    if sample_bars < 40:
        classification = "insufficient"
    elif path_efficiency >= 0.35:
        classification = "trending"
    elif path_efficiency <= 0.18:
        classification = "range_bound"
    else:
        classification = "mixed"
    reason = {
        "trending": "开发样本的净位移相对路径波动较高，属于趋势型环境。",
        "range_bound": "开发样本反复往返、净位移较小，属于震荡型环境。",
        "mixed": "开发样本既有方向段也有反复段，不偏向单一策略范式。",
        "insufficient": "开发样本不足，市场状态不参与模板排序。",
    }[classification]
    return {
        "classification": classification,
        "direction": direction,
        "net_return": net_return,
        "path_efficiency": path_efficiency,
        "sample_bars": sample_bars,
        "reason": reason,
    }


def _strategy_regime_fit(strategy_id: str, market_regime: dict[str, Any]) -> tuple[str, str]:
    classification = str(market_regime["classification"])
    if classification in {"mixed", "insufficient"}:
        return "neutral", "开发样本没有形成足够明确的单一市场状态，不按范式偏好加分。"
    is_trend = strategy_id in _TREND_FOLLOWING_STRATEGY_IDS
    is_reversion = strategy_id in _MEAN_REVERSION_STRATEGY_IDS
    aligned = (classification == "trending" and is_trend) or (
        classification == "range_bound" and is_reversion
    )
    counter_regime = (classification == "trending" and is_reversion) or (
        classification == "range_bound" and is_trend
    )
    if aligned:
        return "aligned", "模板范式与开发样本市场状态相符，仅作短名单的轻微排序依据。"
    if counter_regime:
        return (
            "counter_regime",
            "模板范式与开发样本市场状态相反；仍可入选，但不获得状态适配优先级。",
        )
    return "neutral", "该模板不属于固定的趋势或均值回归范式，不按市场状态偏好排序。"


def _screening_constraint_reasons(
    metrics: Any,
    *,
    minimum_trades: int,
    body: DiscoverBacktestRequest,
) -> tuple[str, ...]:
    """Explain only development-sample hard gates used before full optimization."""

    reasons: list[str] = []
    if metrics.trades < minimum_trades:
        reasons.append("开发样本交易次数不足")
    if (
        body.maximum_trades_per_year is not None
        and metrics.trades_per_year > body.maximum_trades_per_year
    ):
        reasons.append("开发样本交易频率超过上限")
    if metrics.exposure_ratio < body.minimum_exposure:
        reasons.append("开发样本持仓率低于下限")
    if (
        body.minimum_annualized_return is not None
        and metrics.annualized_return < body.minimum_annualized_return
    ):
        reasons.append("开发样本年化收益低于目标")
    if (
        body.maximum_drawdown is not None
        and abs(metrics.max_drawdown) > body.maximum_drawdown
    ):
        reasons.append("开发样本最大回撤超过预算")
    if metrics.max_cash_streak_ratio > body.maximum_cash_streak_ratio:
        reasons.append("开发样本连续空仓比例超过上限")
    if (
        body.maximum_cash_streak_bars is not None
        and metrics.max_cash_streak > body.maximum_cash_streak_bars
    ):
        reasons.append("开发样本连续空仓 K 线超过上限")
    return tuple(reasons)


def _screening_constraint_summary(
    screened: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize development-only hard-gate impact without changing any gate.

    A parameter set can fail more than one constraint, so individual failure
    counts intentionally do not add up to the number of rejected sets.  This
    makes an empty shortlist explainable without pretending that relaxing one
    gate would necessarily produce a validated strategy.
    """

    total = len(screened)
    reason_counts: dict[str, int] = {}
    passing = 0
    for item in screened:
        reasons = tuple(str(reason) for reason in item["constraint_reasons"])
        if not reasons:
            passing += 1
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "candidates_evaluated": total,
        "candidates_passing_base_constraints": passing,
        "constraint_failures": [
            {
                "reason": reason,
                "failed_candidates": count,
                "failure_ratio": count / total if total else 0.0,
            }
            for reason, count in sorted(
                reason_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
    }


def _validation_score(optimization: Any) -> float:
    strategy = optimization.validation_metrics
    benchmark = optimization.validation_benchmark_metrics
    drawdown_improvement = abs(benchmark.max_drawdown) - abs(strategy.max_drawdown)
    return float(
        strategy.total_return
        - benchmark.total_return
        + 0.5 * drawdown_improvement
        + 0.05 * strategy.sharpe_ratio
    )


def _validate_discovery_holdout(
    strategy: Any,
    benchmark: Any,
    exposure_matched_benchmark: Any,
    body: DiscoverBacktestRequest,
    *,
    cost_stress_passed: bool = True,
) -> tuple[bool, HoldoutValidationCode, str]:
    passed, code, reason = validate_holdout_detailed(
        strategy,
        benchmark,
        exposure_matched_benchmark,
    )
    if not passed:
        return passed, code, reason
    if (
        body.minimum_annualized_return is not None
        and strategy.annualized_return < body.minimum_annualized_return
    ):
        return (
            False,
            "return_target_failed",
            "最终留出期年化收益低于目标，候选未通过收益门槛。",
        )
    if (
        body.maximum_drawdown is not None
        and abs(strategy.max_drawdown) > body.maximum_drawdown
    ):
        return (
            False,
            "drawdown_limit_failed",
            "最终留出期最大回撤超过风险预算，候选未通过回撤门槛。",
        )
    if strategy.trades_per_year < body.minimum_trades_per_year * 0.5:
        return (
            False,
            "frequency_too_low",
            "最终留出期交易频率低于最低要求的一半，候选未通过参与度验证。",
        )
    if strategy.exposure_ratio < body.minimum_exposure * 0.5:
        return (
            False,
            "exposure_too_low",
            "最终留出期持仓率低于最低要求的一半，候选长期空仓。",
        )
    if (
        body.maximum_trades_per_year is not None
        and strategy.trades_per_year > body.maximum_trades_per_year * 1.5
    ):
        return (
            False,
            "frequency_too_high",
            "最终留出期交易频率明显超过上限，成本与过度交易风险不可接受。",
        )
    if (
        body.maximum_cash_streak_bars is not None
        and strategy.max_cash_streak > body.maximum_cash_streak_bars
    ):
        return (
            False,
            "cash_streak_too_long",
            "最终留出期连续空仓 K 线超过上限，候选未通过参与度验证。",
        )
    if not cost_stress_passed:
        return (
            False,
            "cost_stress_failed",
            "最终留出期在提高交易成本后未同时保持正收益和相同起点投入比例的固定份额基准之上的择时优势，"
            "候选未通过成本压力测试。",
        )
    if strategy.closed_trades < FINAL_HOLDOUT_MIN_CLOSED_TRADES:
        return (
            False,
            "sample_insufficient",
            "最终留出期少于 "
            f"{FINAL_HOLDOUT_MIN_CLOSED_TRADES} 个已闭合独立决策周期，"
            "样本不足以通过参与度验证。",
        )
    return passed, code, reason


def _optimized_backtest_payload(
    *,
    body: DiscoverBacktestRequest,
    history: Any,
    data: Any,
    definition: Any,
    signal_history: Any,
    optimization: Any,
    benchmark_definition: dict[str, Any],
    benchmark_result: Any,
) -> dict[str, Any]:
    signals = strategy_signals(
        data,
        definition.id,
        optimization.selected_parameters,
        signal_history=signal_history,
    )
    selected_warmup = strategy_warmup_bars(
        definition.id,
        optimization.selected_parameters,
    )
    exposure_matched_benchmark = _exposure_matched_benchmark(
        data,
        optimization.full_result,
        optimization.full_result.config,
    )
    payload = {
        "symbol": body.symbol,
        "interval": body.interval,
        "data_metadata": history.metadata,
        "strategy": definition.model_copy(
            update={
                "parameters": optimization.selected_parameters,
                "warmup_bars": selected_warmup,
            }
        ).model_dump(),
        "ohlcv": history.rows,
        "result": optimization.full_result.model_dump(mode="json"),
        "benchmark": {
            "strategy": benchmark_definition,
            "result": benchmark_result.model_dump(mode="json"),
        },
        "exposure_matched_benchmark": {
            "target_exposure": optimization.full_result.metrics.exposure_ratio,
            "result": exposure_matched_benchmark.model_dump(mode="json"),
        },
        "comparison": _comparison(optimization.full_result, benchmark_result),
        "timing_comparison": _timing_comparison(
            optimization.full_result,
            exposure_matched_benchmark,
        ),
        "diagnostics": _diagnostics(
            data,
            signals,
            optimization.full_result,
            benchmark_result,
        ),
        "optimization": optimization.model_dump(mode="json", exclude={"full_result"}),
        "citations": [item.model_dump(mode="json") for item in history.citations],
    }
    return payload


def _passive_backtest_payload(
    *,
    body: DiscoverBacktestRequest,
    history: Any,
    data: Any,
    benchmark_definition: dict[str, Any],
    benchmark_result: Any,
) -> dict[str, Any]:
    _, benchmark_strategy = get_strategy("buy-hold")
    signals = benchmark_strategy(data, {})
    metadata = dict(history.metadata)
    metadata.update(
        {
            "indicator_warmup_required_bars": 0,
            "indicator_warmup_available_bars": 0,
            "indicator_warmup_complete": True,
            "indicator_warmup_start": None,
            "indicator_warmup_end": None,
            "indicator_warmup_error": None,
        }
    )
    payload = {
        "symbol": body.symbol,
        "interval": body.interval,
        "data_metadata": metadata,
        "strategy": benchmark_definition,
        "ohlcv": history.rows,
        "result": benchmark_result.model_dump(mode="json"),
        "benchmark": {
            "strategy": benchmark_definition,
            "result": benchmark_result.model_dump(mode="json"),
        },
        "exposure_matched_benchmark": {
            "target_exposure": 1.0,
            "result": benchmark_result.model_dump(mode="json"),
        },
        "comparison": _comparison(benchmark_result, benchmark_result),
        "timing_comparison": _timing_comparison(
            benchmark_result,
            benchmark_result,
        ),
        "diagnostics": _diagnostics(
            data,
            signals,
            benchmark_result,
            benchmark_result,
        ),
        "citations": [item.model_dump(mode="json") for item in history.citations],
    }
    return payload


def _risk_budgeted_backtest_payload(
    *,
    body: DiscoverBacktestRequest,
    history: Any,
    data: Any,
    calibration_data: Any,
    benchmark_definition: dict[str, Any],
    benchmark_result: Any,
    maximum_drawdown: float,
    config: BacktestConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # This is a passive allocation baseline, but it must still respect the
    # discovery boundary: calibrate the allocation before seeing the final
    # holdout, then audit that fixed allocation across the full sample.
    allocation_config = config.model_copy(update={"signal_delay_bars": 0})
    calibration = run_risk_budgeted_benchmark(
        calibration_data,
        maximum_drawdown,
        allocation_config,
        include_details=False,
    )
    definition, _ = get_strategy("constant-allocation")
    parameters = {"allocation": calibration.target_exposure}
    # Keep the full-sample audit on the exact same fixed-share mechanism used
    # during calibration.  Re-running the constant-allocation strategy here
    # would silently introduce free per-bar rebalancing and could breach the
    # drawdown budget that the calibration just established.
    full_result = run_exposure_matched_benchmark(
        data,
        calibration.target_exposure,
        allocation_config,
    )
    metadata = dict(history.metadata)
    metadata.update(
        {
            "indicator_warmup_required_bars": 0,
            "indicator_warmup_available_bars": 0,
            "indicator_warmup_complete": True,
            "indicator_warmup_start": None,
            "indicator_warmup_end": None,
            "indicator_warmup_error": None,
            "execution_model": "fixed_shares",
            "initial_allocation": calibration.target_exposure,
            "rebalance_policy": "none",
        }
    )
    backtest = {
        "symbol": body.symbol,
        "interval": body.interval,
        "data_metadata": metadata,
        "strategy": definition.model_copy(
            update={
                "name": "风险预算固定份额配置",
                "description": (
                    "仅在起点按历史回撤预算投入，之后固定份额与剩余现金，"
                    "不做择时或再平衡。"
                ),
                "parameters": parameters,
            }
        ).model_dump(),
        "ohlcv": history.rows,
        "result": full_result.model_dump(mode="json"),
        "benchmark": {
            "strategy": benchmark_definition,
            "result": benchmark_result.model_dump(mode="json"),
        },
        "exposure_matched_benchmark": {
            "target_exposure": calibration.target_exposure,
            "result": full_result.model_dump(mode="json"),
        },
        "comparison": _comparison(full_result, benchmark_result),
        "timing_comparison": _timing_comparison(
            full_result,
            full_result,
        ),
        "diagnostics": {
            "signal_state": _fixed_share_signal_state(
                data,
                full_result,
                calibration.target_exposure,
            ),
            "trade_quality": _trade_quality(full_result),
            **_segment_diagnostics(full_result, benchmark_result),
        },
        "citations": [item.model_dump(mode="json") for item in history.citations],
    }
    summary = {
        **calibration.model_dump(mode="json", exclude={"result"}),
        "calibration_start": _date_value(calibration_data.index[0]),
        "calibration_end": _date_value(calibration_data.index[-1]),
        "calibration_budget_satisfied": calibration.budget_satisfied,
        "full_sample_budget_satisfied": (
            abs(full_result.metrics.max_drawdown) <= maximum_drawdown + 1e-9
        ),
    }
    return backtest, summary


@router.get("/strategies")
async def list_strategies() -> list[dict[str, Any]]:
    return [definition.model_dump() for definition in STRATEGIES.values()]


@router.get("/backtests/discovery-requirements")
async def discovery_requirements() -> dict[str, Any]:
    """Publish the server-owned time horizon required for a validated discovery."""

    return {
        "intervals": {
            interval: {
                "full_sample_days": full_sample_days,
                "holdout_days": holdout_days,
            }
            for interval, (full_sample_days, holdout_days) in (
                _DISCOVERY_MINIMUM_HORIZON_DAYS.items()
            )
        }
    }


@router.post("/backtests/portfolio")
async def portfolio_backtest(
    request: Request,
    body: PortfolioBacktestRequest,
) -> dict[str, Any]:
    async def load_asset(asset: Any) -> tuple[Any, Any, Any, str]:
        adapter = _providers(request).resolve(asset.symbol, asset.provider)
        pre_roll_start = body.start - timedelta(
            days=max(
                _warmup_calendar_days(
                    adapter,
                    "1d",
                    body.volatility_lookback,
                ),
                body.volatility_lookback * 2 + 14,
            )
        )
        history = await adapter.history_interval(
            asset.symbol,
            pre_roll_start,
            body.end,
            interval="1d",
        )
        currency = await _verified_portfolio_currency(
            requested_currency=asset.currency,
            canonical_symbol=history.symbol.strip().upper(),
            adapter=adapter,
        )
        return asset, adapter, history, currency

    try:
        loaded = await asyncio.gather(
            *(load_asset(asset) for asset in body.assets)
        )
        data: dict[str, Any] = {}
        for _asset, _adapter, history, _currency in loaded:
            canonical_symbol = history.symbol.strip().upper()
            if canonical_symbol in data:
                raise ValueError(
                    f"组合中有多个输入解析为同一标的 {canonical_symbol}，请移除重复资产。"
                )
            data[canonical_symbol] = history.to_frame()
        annual_periods = (
            365
            if loaded
            and all(adapter.name == "binance" for _, adapter, _, _ in loaded)
            else 252
        )
        config = (body.config or BacktestConfig()).model_copy(
            update={
                "annual_periods": annual_periods,
                "bar_interval": "1d",
                "signal_delay_bars": 0,
            }
        )
        methods: tuple[PortfolioMethod, ...] = (
            "initial_equal_hold",
            "periodic_equal",
            "periodic_inverse_volatility",
        )
        results = {
            method: run_portfolio_backtest(
                data,
                method,
                config,
                evaluation_start=body.start,
                evaluation_end=body.end,
                volatility_lookback=body.volatility_lookback,
                rebalance_bars=body.rebalance_bars,
                maximum_asset_weight=body.maximum_asset_weight,
            )
            for method in methods
        }
        evaluation_dates = [
            row["date"] for row in results["periodic_equal"].equity
        ]
        segments: list[dict[str, Any]] = []
        for index in range(4):
            lower = len(evaluation_dates) * index // 4
            upper = len(evaluation_dates) * (index + 1) // 4
            if upper <= lower:
                continue
            segment_start = evaluation_dates[lower]
            segment_end = evaluation_dates[upper - 1]
            segment_results = {
                method: run_portfolio_backtest(
                    data,
                    method,
                    config,
                    evaluation_start=segment_start,
                    evaluation_end=segment_end,
                    volatility_lookback=body.volatility_lookback,
                    rebalance_bars=body.rebalance_bars,
                    maximum_asset_weight=body.maximum_asset_weight,
                    include_details=False,
                )
                for method in methods
            }
            segments.append(
                {
                    "index": index + 1,
                    "start": segment_start,
                    "end": segment_end,
                    "results": {
                        method: result.model_dump(mode="json")
                        for method, result in segment_results.items()
                    },
                }
            )
    except (LookupError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    inverse = results["periodic_inverse_volatility"]
    periodic_equal = results["periodic_equal"]
    minimum_segment_bars = max(10, body.rebalance_bars)
    evaluable_segments = sum(
        segment["results"]["periodic_inverse_volatility"]["metrics"]["bars"]
        >= minimum_segment_bars
        and segment["results"]["periodic_inverse_volatility"]["metrics"][
            "rebalances"
        ]
        >= 2
        and segment["results"]["periodic_inverse_volatility"]["metrics"][
            "annualized_volatility"
        ]
        > 1e-8
        and abs(
            sum(
                segment["results"]["periodic_inverse_volatility"][
                    "latest_weights"
                ].values()
            )
            - 1
        )
        <= 1e-6
        for segment in segments
    )
    drawdown_improved_segments = sum(
        segment["results"]["periodic_inverse_volatility"]["metrics"][
            "max_drawdown"
        ]
        > segment["results"]["periodic_equal"]["metrics"]["max_drawdown"]
        + 1e-6
        for segment in segments
    )
    sharpe_improved_segments = sum(
        segment["results"]["periodic_inverse_volatility"]["metrics"][
            "sharpe_ratio"
        ]
        > segment["results"]["periodic_equal"]["metrics"]["sharpe_ratio"]
        + 1e-3
        for segment in segments
    )
    full_sample_invested = (
        inverse.metrics.rebalances >= 2
        and inverse.metrics.annualized_volatility > 1e-8
        and abs(sum(inverse.latest_weights.values()) - 1) <= 1e-6
    )
    risk_evidence_passed = (
        bool(segments)
        and evaluable_segments == len(segments)
        and drawdown_improved_segments == len(segments)
        and sharpe_improved_segments >= ceil(len(segments) / 2)
        and inverse.metrics.sharpe_ratio
        > periodic_equal.metrics.sharpe_ratio + 1e-3
        and inverse.metrics.total_return > 0
        and full_sample_invested
    )
    citations: list[dict[str, Any]] = []
    citation_keys: set[tuple[str, str]] = set()
    assets: list[dict[str, Any]] = []
    for asset, adapter, history, currency in loaded:
        assets.append(
            {
                "symbol": history.symbol.strip().upper(),
                "requested_symbol": asset.symbol.strip().upper(),
                "provider": adapter.name,
                "currency": currency,
                "metadata": history.metadata,
            }
        )
        for citation in history.citations:
            dumped = citation.model_dump(mode="json")
            key = (str(dumped.get("url", "")), str(dumped.get("title", "")))
            if key not in citation_keys:
                citation_keys.add(key)
                citations.append(dumped)
    payload = {
        "calculation_version": str(request.app.version),
        "assets": assets,
        "start": body.start,
        "end": body.end,
        "common_bars": inverse.metrics.bars,
        "data_quality": {
            "actual_start": evaluation_dates[0] if evaluation_dates else None,
            "actual_end": evaluation_dates[-1] if evaluation_dates else None,
            "annual_periods": annual_periods,
            "source_bars": {
                history.symbol.strip().upper(): len(history.rows)
                for _, _, history, _ in loaded
            },
            "alignment": "common_daily_session_labels",
            "valuation_limit": (
                "仅保留所有资产都有日线的共同交易日；加密资产周末波动不会形成"
                "独立组合净值点。"
            ),
        },
        "assumptions": {
            "volatility_lookback": body.volatility_lookback,
            "rebalance_bars": body.rebalance_bars,
            "maximum_asset_weight": body.maximum_asset_weight,
            "fee_rate": config.fee_rate,
            "slippage_rate": config.slippage_rate,
            "cash_return": 0,
            "execution": "next_common_session_open_proxy",
        },
        "results": {
            method: result.model_dump(mode="json")
            for method, result in results.items()
        },
        "segments": segments,
        "research_decision": {
            "risk_evidence_passed": risk_evidence_passed,
            "drawdown_improved_segments": drawdown_improved_segments,
            "sharpe_improved_segments": sharpe_improved_segments,
            "evaluable_segments": evaluable_segments,
            "total_segments": len(segments),
            "evidence_checks": {
                "all_segments_evaluable": evaluable_segments == len(segments),
                "all_segments_drawdown_strictly_improved": (
                    drawdown_improved_segments == len(segments)
                ),
                "majority_segments_sharpe_improved": (
                    sharpe_improved_segments >= ceil(len(segments) / 2)
                    if segments
                    else False
                ),
                "full_sample_sharpe_improved": (
                    inverse.metrics.sharpe_ratio
                    > periodic_equal.metrics.sharpe_ratio + 1e-3
                ),
                "full_sample_positive_return": inverse.metrics.total_return > 0,
                "full_sample_invested": full_sample_invested,
                "minimum_segment_bars": minimum_segment_bars,
            },
            "title": (
                "逆波动配置具备跨窗口风险改善证据"
                if risk_evidence_passed
                else "逆波动配置尚未形成稳定风险优势"
            ),
            "reason": (
                "完整样本夏普高于定期等权，每个可评估窗口的最大回撤都严格更小，"
                "且至少半数窗口夏普更高；这仍是历史组合研究，不代表未来最优。"
                if risk_evidence_passed
                else (
                    "至少一个证据门槛未满足：窗口需有足够样本和真实再平衡，"
                    "回撤必须严格改善，至少半数窗口及完整样本夏普需更高，"
                    "完整样本还必须保持投入并取得正收益；当前不自动推荐。"
                )
            ),
        },
        "citations": citations,
    }
    run = _experiment_store(request).create_portfolio_run(body, payload)
    return {
        **payload,
        "run_id": run.run_id,
        "run_expires_at": run.expires_at.isoformat(),
    }


@router.get("/symbols/search")
async def search_symbols(
    request: Request,
    q: str = Query(default="", max_length=80),
    market: MarketFilter = "all",
    limit: int = Query(default=10, ge=1, le=30),
) -> list[dict[str, Any]]:
    instruments = await _providers(request).search(q, market=market, limit=limit)
    return [instrument.model_dump() for instrument in instruments]


@router.get("/market/{symbol}/capabilities")
async def market_history_capabilities(
    request: Request,
    symbol: str,
    provider: Literal[
        "auto", "akshare", "yfinance", "binance", "futures", "macro"
    ] = "auto",
) -> dict[str, Any]:
    """Publish documented K-line windows before a backtest request is made."""

    adapter = _providers(request).resolve(symbol, provider)
    return _history_capabilities(adapter, symbol)


@router.get("/market/{symbol}/{kind}")
async def market_data(
    request: Request,
    symbol: str,
    kind: Literal["history", "quote", "fundamentals", "capital-flow", "news"],
    provider: Literal[
        "auto", "akshare", "yfinance", "binance", "futures", "macro"
    ] = "auto",
    start: date | None = None,
    end: date | None = None,
    interval: BarInterval = "1d",
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    adapter = _providers(request).resolve(symbol, provider)
    try:
        if kind == "history":
            envelope = await adapter.history_interval(
                symbol,
                start,
                end,
                interval=interval,
            )
        elif kind == "quote":
            envelope = await adapter.quote(symbol)
        elif kind == "fundamentals":
            envelope = await adapter.fundamentals(symbol)
        elif kind == "capital-flow":
            envelope = await adapter.capital_flow(symbol)
        else:
            envelope = await adapter.news(symbol, limit)
        return envelope.model_dump(mode="json")
    except ValueError as exc:
        # A provider uses ValueError for a caller-correctable request such as an
        # unsupported interval or a documented history-window limit.  Keep it
        # distinct from an upstream outage so the shared client can give an
        # actionable correction instead of reporting a gateway failure.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (RuntimeError, LookupError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/backtests")
async def create_backtest(request: Request, body: BacktestRequest) -> dict[str, Any]:
    adapter = _providers(request).resolve(body.symbol, body.provider)
    try:
        definition, _ = get_strategy(body.strategy_id)
        parameters = {**definition.parameters, **body.parameters}
        required_warmup = strategy_warmup_bars(body.strategy_id, parameters)
        history, data, signal_history, warmup_error = await _history_for_backtest(
            adapter,
            body.symbol,
            body.start,
            body.end,
            body.interval,
            required_warmup,
        )
        history = _with_warmup_metadata(
            history,
            required_warmup,
            signal_history,
            warmup_error,
        )
        signals = strategy_signals(
            data,
            body.strategy_id,
            parameters,
            signal_history=signal_history,
        )
        config = _backtest_config(adapter, body.config, body.interval)
        if body.strategy_id in PASSIVE_STRATEGY_IDS:
            config = config.model_copy(update={"signal_delay_bars": 0})
        fixed_share_allocation = (
            float(parameters["allocation"])
            if body.strategy_id == "constant-allocation"
            else None
        )
        cycle_baseline = strategy_cycle_baseline_signals(
            body.strategy_id,
            signals,
            parameters,
        )
        result = (
            run_exposure_matched_benchmark(
                data,
                fixed_share_allocation,
                config,
            )
            if fixed_share_allocation is not None
            else run_backtest(
                data,
                signals,
                config,
                cycle_baseline_signals=cycle_baseline,
            )
        )
        benchmark_definition, benchmark_result = _benchmark(data, config)
        exposure_matched_benchmark = (
            result
            if fixed_share_allocation is not None
            else _exposure_matched_benchmark(
                data,
                result,
                config,
            )
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (RuntimeError, LookupError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    metadata = dict(history.metadata)
    if fixed_share_allocation is not None:
        metadata.update(
            {
                "execution_model": "fixed_shares",
                "initial_allocation": fixed_share_allocation,
                "rebalance_policy": "none",
            }
        )
    diagnostics = (
        {
            "signal_state": _fixed_share_signal_state(
                data,
                result,
                fixed_share_allocation,
            ),
            "trade_quality": _trade_quality(result),
            **_segment_diagnostics(result, benchmark_result),
        }
        if fixed_share_allocation is not None
        else _diagnostics(data, signals, result, benchmark_result)
    )
    payload = {
        "symbol": body.symbol,
        "interval": body.interval,
        "data_metadata": metadata,
        "strategy": definition.model_copy(
            update={
                "parameters": parameters,
                "warmup_bars": required_warmup,
            }
        ).model_dump(),
        "ohlcv": history.rows,
        "result": result.model_dump(mode="json"),
        "benchmark": {
            "strategy": benchmark_definition,
            "result": benchmark_result.model_dump(mode="json"),
        },
        "exposure_matched_benchmark": {
            "target_exposure": (
                fixed_share_allocation
                if fixed_share_allocation is not None
                else result.metrics.exposure_ratio
            ),
            "result": exposure_matched_benchmark.model_dump(mode="json"),
        },
        "comparison": _comparison(result, benchmark_result),
        "timing_comparison": _timing_comparison(
            result,
            exposure_matched_benchmark,
        ),
        "diagnostics": diagnostics,
        "citations": [item.model_dump(mode="json") for item in history.citations],
    }
    manifest_request = body.model_dump(mode="json")
    manifest_request.update(
        {
            "symbol": history.symbol,
            "provider": adapter.name,
        }
    )
    return attach_and_record_backtest_run(
        payload,
        request_body=manifest_request,
        settings=request.app.state.settings,
        store=_research_run_store(request),
        provider=adapter.name,
        canonical_symbol=history.symbol,
    )


def _optimize_with_constraints(
    data: Any,
    strategy_id: str,
    body: OptimizeBacktestRequest | StrategyRobustnessRequest,
    config: BacktestConfig,
    *,
    signal_history: Any,
) -> Any:
    return optimize_strategy(
        data,
        strategy_id,
        body.objective,
        config,
        signal_history=signal_history,
        train_ratio=body.train_ratio,
        minimum_trades=body.minimum_trades,
        minimum_trades_per_year=body.minimum_trades_per_year,
        maximum_trades_per_year=body.maximum_trades_per_year,
        minimum_exposure=body.minimum_exposure,
        minimum_annualized_return=body.minimum_annualized_return,
        maximum_drawdown=body.maximum_drawdown,
        maximum_cash_streak_ratio=body.maximum_cash_streak_ratio,
        maximum_cash_streak_bars=body.maximum_cash_streak_bars,
        minimum_profitable_fold_ratio=body.minimum_profitable_fold_ratio,
        minimum_timing_positive_fold_ratio=body.minimum_timing_positive_fold_ratio,
        walk_forward_windows=body.walk_forward_windows,
    )


async def _optimized_market_payload(
    adapter: DataProvider,
    symbol: str,
    body: OptimizeBacktestRequest | StrategyRobustnessRequest,
    *,
    settings: Settings,
    research_run_store: ResearchRunStore,
) -> dict[str, Any]:
    """Optimize one market using the exact same evidence protocol as the workbench."""

    required_warmup = _maximum_strategy_warmup(body.strategy_id)
    history, data, signal_history, warmup_error = await _history_for_backtest(
        adapter,
        symbol,
        body.start,
        body.end,
        body.interval,
        required_warmup,
    )
    definition, _ = get_strategy(body.strategy_id)
    config = _backtest_config(adapter, body.config, body.interval)
    optimization = await asyncio.to_thread(
        _optimize_with_constraints,
        data,
        body.strategy_id,
        body,
        config,
        signal_history=signal_history,
    )
    selected_warmup = strategy_warmup_bars(
        body.strategy_id,
        optimization.selected_parameters,
    )
    history = _with_warmup_metadata(
        history,
        selected_warmup,
        signal_history,
        warmup_error,
    )
    benchmark_definition, benchmark_result = _benchmark(data, config)
    exposure_matched_benchmark = _exposure_matched_benchmark(
        data,
        optimization.full_result,
        config,
    )
    signals = strategy_signals(
        data,
        body.strategy_id,
        optimization.selected_parameters,
        signal_history=signal_history,
    )
    payload = {
        "symbol": symbol,
        "interval": body.interval,
        "data_metadata": history.metadata,
        "strategy": definition.model_copy(
            update={
                "parameters": optimization.selected_parameters,
                "warmup_bars": selected_warmup,
            }
        ).model_dump(),
        "ohlcv": history.rows,
        "result": optimization.full_result.model_dump(mode="json"),
        "benchmark": {
            "strategy": benchmark_definition,
            "result": benchmark_result.model_dump(mode="json"),
        },
        "exposure_matched_benchmark": {
            "target_exposure": optimization.full_result.metrics.exposure_ratio,
            "result": exposure_matched_benchmark.model_dump(mode="json"),
        },
        "comparison": _comparison(optimization.full_result, benchmark_result),
        "timing_comparison": _timing_comparison(
            optimization.full_result,
            exposure_matched_benchmark,
        ),
        "diagnostics": _diagnostics(
            data,
            signals,
            optimization.full_result,
            benchmark_result,
        ),
        "optimization": optimization.model_dump(mode="json", exclude={"full_result"}),
        "citations": [item.model_dump(mode="json") for item in history.citations],
    }
    manifest_request = body.model_dump(mode="json")
    manifest_request.update(
        {
            "symbol": symbol,
            "provider": adapter.name,
        }
    )
    return attach_and_record_backtest_run(
        payload,
        request_body=manifest_request,
        settings=settings,
        store=research_run_store,
        provider=adapter.name,
        canonical_symbol=history.symbol,
    )


@router.post("/backtests/optimize")
async def optimize_backtest(request: Request, body: OptimizeBacktestRequest) -> dict[str, Any]:
    adapter = _providers(request).resolve(body.symbol, body.provider)
    try:
        if body.strategy_id in PASSIVE_STRATEGY_IDS:
            raise ValueError("被动配置不做参数寻优；请直接设置资金仓位后运行。")
        return await _optimized_market_payload(
            adapter,
            body.symbol,
            body,
            settings=request.app.state.settings,
            research_run_store=_research_run_store(request),
        )
    except (RuntimeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/backtests/robustness")
async def validate_strategy_across_markets(
    request: Request,
    body: StrategyRobustnessRequest,
) -> dict[str, Any]:
    """Audit one strategy family across 2–4 independently optimized markets.

    The endpoint is intentionally sequential.  Each optimization is CPU-heavy and
    independent, and a small bounded study should remain polite to the NAS rather
    than competing with the rest of the research workspace.
    """

    try:
        if body.strategy_id in PASSIVE_STRATEGY_IDS:
            raise ValueError("被动配置不做跨市场参数寻优；请使用主动策略模板。")
        definition, _ = get_strategy(body.strategy_id)
    except (RuntimeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    outcomes: list[dict[str, Any]] = []
    for market in body.markets:
        adapter = _providers(request).resolve(market.symbol, market.provider)
        try:
            backtest = await _optimized_market_payload(
                adapter,
                market.symbol,
                body,
                settings=request.app.state.settings,
                research_run_store=_research_run_store(request),
            )
            optimization = backtest["optimization"]
            outcomes.append(
                {
                    "symbol": market.symbol,
                    "provider": getattr(adapter, "name", market.provider),
                    "status": "completed",
                    "validation_passed": optimization["validation_passed"],
                    "validation_code": optimization["validation_code"],
                    "validation_reason": optimization["validation_reason"],
                    "forward_observation_eligible": optimization[
                        "forward_observation_eligible"
                    ],
                    # A cross-market study may contain four intraday datasets.
                    # Return the auditable optimization evidence, not four full
                    # OHLC/equity payloads that the workbench does not render.
                    "evidence": {
                        "data_metadata": backtest["data_metadata"],
                        "strategy": backtest["strategy"],
                        "full_sample_metrics": backtest["result"]["metrics"],
                        "optimization": optimization,
                        "citations": backtest["citations"],
                    },
                }
            )
        except ValueError as exc:
            if _is_constraint_rejection(exc):
                outcomes.append(
                    {
                        "symbol": market.symbol,
                        "provider": getattr(adapter, "name", market.provider),
                        "status": "rejected",
                        "validation_passed": False,
                        "validation_code": "constraints_too_strict",
                        "validation_reason": str(exc),
                        "forward_observation_eligible": False,
                        "evidence": None,
                    }
                )
            else:
                outcomes.append(
                    {
                        "symbol": market.symbol,
                        "provider": getattr(adapter, "name", market.provider),
                        "status": "unavailable",
                        "validation_passed": False,
                        "validation_code": "unavailable",
                        "validation_reason": str(exc),
                        "forward_observation_eligible": False,
                        "evidence": None,
                    }
                )
        except (RuntimeError, KeyError) as exc:
            outcomes.append(
                {
                    "symbol": market.symbol,
                    "provider": getattr(adapter, "name", market.provider),
                    "status": "unavailable",
                    "validation_passed": False,
                    "validation_code": "unavailable",
                    "validation_reason": str(exc),
                    "forward_observation_eligible": False,
                    "evidence": None,
                }
            )

    audited = [item for item in outcomes if item["status"] != "unavailable"]
    validated = [item for item in audited if item["validation_passed"]]
    provisional = [
        item
        for item in audited
        if not item["validation_passed"] and item["forward_observation_eligible"]
    ]
    rejected = len([item for item in outcomes if item["status"] == "rejected"])
    unavailable = len(outcomes) - len(audited)
    if unavailable:
        status = "incomplete"
        title = "跨市场证据不完整"
        reason = (
            f"{unavailable} 个市场的数据或约束无法完成审计；不能把其余市场的结果"
            "外推为跨市场有效。"
        )
    elif len(validated) == len(outcomes):
        status = "validated_across_markets"
        title = "策略族在所选市场均通过最终留出验证"
        reason = (
            "每个市场都独立完成参数寻优、走步验证与最终留出验证。"
            "这支持策略族在该小样本市场篮子中具有一致证据，但不是未来收益保证。"
        )
    elif validated:
        status = "mixed_evidence"
        title = "跨市场证据混合"
        reason = (
            "至少一个市场没有通过最终留出验证；不要把单一市场的参数或表现"
            "推广为通用策略。"
        )
    elif provisional:
        status = "provisional_only"
        title = "仅有暂存证据，不能形成跨市场结论"
        reason = (
            "没有市场通过完整最终留出验证；少量市场仅因交易样本不足而可冻结观察。"
            "这不足以支持跨市场策略结论，更不能推广参数或投入使用。"
        )
    else:
        status = "rejected_across_markets"
        title = "所选市场均未形成可用的跨市场证据"
        reason = "没有市场通过最终留出验证；应保留结果作为反证，而不是继续追逐同一批留出数据。"

    payload = {
        "strategy": definition.model_dump(),
        "interval": body.interval,
        "objective": body.objective,
        "parameter_policy": "independently_optimized_per_market",
        "parameter_policy_note": (
            "每个市场仅用自己的开发期选择参数，最终留出期不参与选参。"
            "该实验验证策略族，不证明一组参数可通用于所有市场。"
        ),
        "final_holdout_min_closed_trades": FINAL_HOLDOUT_MIN_CLOSED_TRADES,
        "markets": outcomes,
        "summary": {
            "requested": len(outcomes),
            "completed": len(audited),
            "validated": len(validated),
            "provisional": len(provisional),
            "rejected": rejected,
            "unavailable": unavailable,
        },
        "research_decision": {"status": status, "title": title, "reason": reason},
    }
    run = _experiment_store(request).create_cross_market_run(body, payload)
    return {
        **payload,
        "run_id": run.run_id,
        "run_expires_at": run.expires_at.isoformat(),
    }


@router.post("/backtests/discover")
async def discover_backtests(
    request: Request,
    body: DiscoverBacktestRequest,
) -> dict[str, Any]:
    """Shortlist on development data, then optimize and validate each candidate."""
    started_at = perf_counter()
    adapter = _providers(request).resolve(body.symbol, body.provider)
    try:
        active_strategy_ids = [
            definition.id
            for definition in STRATEGIES.values()
            if definition.id not in PASSIVE_STRATEGY_IDS
        ]
        required_warmup = max(
            _maximum_strategy_warmup(strategy_id)
            for strategy_id in active_strategy_ids
        )
        history, data, signal_history, warmup_error = await _history_for_backtest(
            adapter,
            body.symbol,
            body.start,
            body.end,
            body.interval,
            required_warmup,
        )
        minimum_bars = ceil(100 / body.train_ratio)
        if len(data) < minimum_bars:
            raise ValueError(f"策略发现至少需要 {minimum_bars} 根 K 线。")
        config = _backtest_config(adapter, body.config, body.interval)
        split_index = int(len(data) * body.train_ratio)
        research_horizon = _discovery_horizon(data, split_index, body.interval)
        development = data.iloc[:split_index]
        development_market_regime = _development_market_regime(development)
        _, development_benchmark = _benchmark(development, config)
        benchmark_definition, benchmark_result = _benchmark(data, config)
        required_screening_trades = _minimum_trade_events_for_window(
            development.index,
            config.annual_periods,
            body.minimum_trades_per_year,
            body.minimum_trades,
        )

        shortlist: list[dict[str, Any]] = []
        screening_outcomes: list[dict[str, Any]] = []
        for definition in STRATEGIES.values():
            if definition.id in PASSIVE_STRATEGY_IDS:
                continue
            full_parameter_grid = parameter_candidates(definition.id)
            initial_screening_parameters = screening_parameter_candidates(
                definition.id,
                maximum_candidates=_DISCOVERY_SCREENING_INITIAL_PARAMETER_LIMIT,
            )

            def evaluate_screening_stage(
                parameter_sets: list[dict[str, float]],
                stage: str,
                strategy_definition: Any = definition,
            ) -> list[dict[str, Any]]:
                outcomes: list[dict[str, Any]] = []
                for grid_parameters in parameter_sets:
                    merged_parameters = {
                        **strategy_definition.parameters,
                        **grid_parameters,
                    }
                    signals = strategy_signals(
                        development,
                        strategy_definition.id,
                        merged_parameters,
                        signal_history=signal_history,
                    )
                    result = run_backtest(
                        development,
                        signals,
                        config,
                        include_details=False,
                        cycle_baseline_signals=strategy_cycle_baseline_signals(
                            strategy_definition.id,
                            signals,
                            merged_parameters,
                        ),
                    )
                    exposure_matched_benchmark = _exposure_matched_benchmark(
                        development,
                        result,
                        config,
                        include_details=False,
                    )
                    score, quality = _template_score(
                        result,
                        development_benchmark,
                        exposure_matched_benchmark,
                    )
                    objective_score = _screening_objective_score(
                        result,
                        development_benchmark,
                        exposure_matched_benchmark,
                        body.objective,
                    )
                    constraint_reasons = _screening_constraint_reasons(
                        result.metrics,
                        minimum_trades=required_screening_trades,
                        body=body,
                    )
                    outcomes.append(
                        {
                            "grid_parameters": grid_parameters,
                            "parameters": merged_parameters,
                            "result": result,
                            "exposure_matched_benchmark": (
                                exposure_matched_benchmark
                            ),
                            "score": score,
                            "screening_objective_score": objective_score,
                            "quality": quality,
                            "constraint_reasons": constraint_reasons,
                            "screening_stage": stage,
                        }
                    )
                return outcomes

            coverage_outcomes = await asyncio.to_thread(
                evaluate_screening_stage,
                initial_screening_parameters,
                "coverage",
            )
            ranked_coverage = sorted(
                coverage_outcomes,
                key=lambda item: _screening_sort_key(item, body.objective),
            )
            feasible_coverage = [
                item
                for item in ranked_coverage
                if not item["constraint_reasons"]
            ]
            promoted_parameters = [
                item["grid_parameters"]
                for item in feasible_coverage[
                    :_DISCOVERY_SCREENING_PROMOTED_REGIONS
                ]
            ]
            screening_parameters = adaptive_screening_parameter_candidates(
                definition.id,
                evaluated_parameters=initial_screening_parameters,
                promoted_parameters=promoted_parameters,
                maximum_candidates=_DISCOVERY_SCREENING_PARAMETER_BUDGET,
                global_exploration_fraction=(
                    _DISCOVERY_SCREENING_GLOBAL_EXPLORATION_FRACTION
                ),
            )
            refinement_parameters = screening_parameters[
                len(initial_screening_parameters) :
            ]
            refinement_mode = (
                "feasible_neighborhood_with_global_reserve"
                if promoted_parameters
                else "global_recovery_after_no_feasible_seed"
            )
            refinement_outcomes = await asyncio.to_thread(
                evaluate_screening_stage,
                refinement_parameters,
                "adaptive_refinement",
            )
            global_refinement_candidates = (
                min(
                    len(refinement_outcomes),
                    max(
                        1,
                        ceil(
                            len(refinement_outcomes)
                            * _DISCOVERY_SCREENING_GLOBAL_EXPLORATION_FRACTION
                        ),
                    ),
                )
                if promoted_parameters and refinement_outcomes
                else len(refinement_outcomes)
            )
            screened = coverage_outcomes + refinement_outcomes
            screening_stages = [
                {
                    "stage": "coverage",
                    "data_scope": "development_only",
                    "candidate_policy": (
                        "default_center_corners_and_axis_level_coverage"
                    ),
                    "evaluated": len(coverage_outcomes),
                    "passing_base_constraints": len(feasible_coverage),
                }
            ]
            if refinement_outcomes:
                screening_stages.append(
                    {
                        "stage": "adaptive_refinement",
                        "data_scope": "development_only",
                        "candidate_policy": refinement_mode,
                        "evaluated": len(refinement_outcomes),
                        "passing_base_constraints": sum(
                            not item["constraint_reasons"]
                            for item in refinement_outcomes
                        ),
                        "promoted_regions": len(promoted_parameters),
                        "local_refinement_candidates": (
                            len(refinement_outcomes)
                            - global_refinement_candidates
                        ),
                        "global_exploration_candidates": (
                            global_refinement_candidates
                        ),
                        "global_exploration_reserve_fraction": (
                            _DISCOVERY_SCREENING_GLOBAL_EXPLORATION_FRACTION
                            if promoted_parameters
                            else 1.0
                        ),
                    }
                )
            screening_outcomes.extend(screened)
            screened.sort(key=lambda item: _screening_sort_key(item, body.objective))
            selected_screen = screened[0]
            result = selected_screen["result"]
            market_regime_fit, market_regime_fit_reason = _strategy_regime_fit(
                definition.id,
                development_market_regime,
            )
            exposure_matched_benchmark = selected_screen[
                "exposure_matched_benchmark"
            ]
            shortlist.append(
                {
                    "strategy": definition.model_copy(
                        update={"parameters": selected_screen["parameters"]}
                    ).model_dump(),
                    "metrics": result.metrics.model_dump(mode="json"),
                    "comparison": _comparison(result, development_benchmark),
                    "exposure_matched_benchmark_metrics": (
                        exposure_matched_benchmark.metrics.model_dump(mode="json")
                    ),
                    "timing_comparison": _timing_comparison(
                        result,
                        exposure_matched_benchmark,
                    ),
                    "score": selected_screen["score"],
                    "screening_objective_score": selected_screen[
                        "screening_objective_score"
                    ],
                    "screening_selected_stage": selected_screen[
                        "screening_stage"
                    ],
                    "quality": selected_screen["quality"],
                    "screening_candidates_evaluated": len(screened),
                    "screening_candidates_passing_base_constraints": sum(
                        not item["constraint_reasons"] for item in screened
                    ),
                    "screening_grid_total": len(full_parameter_grid),
                    "screening_grid_coverage_ratio": (
                        len(screened) / len(full_parameter_grid)
                    ),
                    "screening_budget": {
                        "initial_limit": min(
                            _DISCOVERY_SCREENING_INITIAL_PARAMETER_LIMIT,
                            len(full_parameter_grid),
                        ),
                        "maximum": min(
                            _DISCOVERY_SCREENING_PARAMETER_BUDGET,
                            len(full_parameter_grid),
                        ),
                        "evaluated": len(screened),
                        "full_grid_evaluated": (
                            len(screened) == len(full_parameter_grid)
                        ),
                        "truncated_by_budget": (
                            len(screened) < len(full_parameter_grid)
                        ),
                    },
                    "screening_stages": screening_stages,
                    "screening_parameter_coverage": {
                        key: {
                            "sampled_levels": len(
                                {candidate[key] for candidate in screening_parameters}
                            ),
                            "available_levels": len(
                                {candidate[key] for candidate in full_parameter_grid}
                            ),
                            "coverage_ratio": (
                                len(
                                    {
                                        candidate[key]
                                        for candidate in screening_parameters
                                    }
                                )
                                / len(
                                    {
                                        candidate[key]
                                        for candidate in full_parameter_grid
                                    }
                                )
                            ),
                            "extrema_covered": (
                                min(
                                    candidate[key]
                                    for candidate in screening_parameters
                                )
                                == min(
                                    candidate[key]
                                    for candidate in full_parameter_grid
                                )
                                and max(
                                    candidate[key]
                                    for candidate in screening_parameters
                                )
                                == max(
                                    candidate[key]
                                    for candidate in full_parameter_grid
                                )
                            ),
                        }
                        for key in sorted(screening_parameters[0])
                    },
                    "screening_constraint_reasons": selected_screen[
                        "constraint_reasons"
                    ],
                    "interval_recommended": (
                        body.interval in definition.recommended_intervals
                    ),
                    "market_regime_fit": market_regime_fit,
                    "market_regime_fit_reason": market_regime_fit_reason,
                }
            )
        shortlist.sort(
            key=lambda item: _discovery_shortlist_sort_key(item, body.objective)
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (RuntimeError, LookupError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    selected = _select_diverse_discovery_shortlist(shortlist, body.shortlist_size)
    feasible_families = _feasible_discovery_strategy_families(shortlist)
    screening_constraint_summary = _screening_constraint_summary(screening_outcomes)
    development_candidates: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for row in selected:
        strategy_id = str(row["strategy"]["id"])
        definition, _ = get_strategy(strategy_id)
        try:
            optimization = await asyncio.to_thread(
                optimize_strategy,
                development,
                strategy_id,
                body.objective,
                config,
                signal_history=signal_history,
                train_ratio=0.8,
                minimum_trades=body.minimum_trades,
                minimum_trades_per_year=body.minimum_trades_per_year,
                maximum_trades_per_year=body.maximum_trades_per_year,
                minimum_exposure=body.minimum_exposure,
                minimum_annualized_return=body.minimum_annualized_return,
                maximum_drawdown=body.maximum_drawdown,
                maximum_cash_streak_ratio=body.maximum_cash_streak_ratio,
                maximum_cash_streak_bars=body.maximum_cash_streak_bars,
                minimum_profitable_fold_ratio=body.minimum_profitable_fold_ratio,
                minimum_timing_positive_fold_ratio=(
                    body.minimum_timing_positive_fold_ratio
                ),
                walk_forward_windows=body.walk_forward_windows,
                top_n=3,
                validation_scope="development_inner",
            )
        except (RuntimeError, ValueError, KeyError) as exc:
            failures.append(
                {
                    "strategy_id": strategy_id,
                    "strategy_name": definition.name,
                    "reason": str(exc),
                }
            )
            continue
        development_candidates.append(
            {
                "definition": definition,
                "optimization": optimization,
                "selection_score": float(optimization.top_candidates[0].score),
            }
        )

    development_candidates.sort(
        key=lambda item: (
            bool(item["optimization"].validation_passed),
            float(item["selection_score"]),
        ),
        reverse=True,
    )
    development_validated = [
        item
        for item in development_candidates
        if item["optimization"].validation_passed
    ]
    development_selection_eligible = [
        item
        for item in development_candidates
        if is_development_selection_eligible(
            validation_passed=item["optimization"].validation_passed,
            validation_code=item["optimization"].validation_code,
            closed_trades=item["optimization"].validation_metrics.closed_trades,
            cost_stress_passed=item["optimization"].cost_stress_passed,
        )
    ]
    selection_trials = [
        {
            "strategy": item["definition"].model_copy(
                update={
                    "parameters": item["optimization"].selected_parameters,
                }
            ).model_dump(),
            "selection_score": item["selection_score"],
            "development_validation_passed": (
                item["optimization"].validation_passed
            ),
            "development_selection_eligible": (
                item in development_selection_eligible
            ),
            "development_validation_code": item["optimization"].validation_code,
            "development_validation_reason": (
                item["optimization"].validation_reason
            ),
            "development_validation_metrics": {
                "total_return": (
                    item["optimization"].validation_metrics.total_return
                ),
                "trades_per_year": (
                    item["optimization"].validation_metrics.trades_per_year
                ),
                "closed_trades": (
                    item["optimization"].validation_metrics.closed_trades
                ),
                "timing_excess_return": (
                    item["optimization"].validation_timing_excess_return
                ),
                "cost_stress_passed": item["optimization"].cost_stress_passed,
            },
        }
        for item in development_candidates
    ]

    candidates: list[dict[str, Any]] = []
    # The outer holdout is a one-shot audit, not a tie-breaker for templates
    # that already failed their nested development validation.  Leaving it
    # untouched preserves a clean future experiment instead of accidentally
    # promoting the least-bad development failure on a lucky final slice.
    if development_selection_eligible:
        locked = development_selection_eligible[0]
        definition = locked["definition"]
        inner_optimization = locked["optimization"]
        selected_signals = strategy_signals(
            data,
            definition.id,
            inner_optimization.selected_parameters,
            signal_history=signal_history,
        )
        selected_cycle_baseline = strategy_cycle_baseline_signals(
            definition.id,
            selected_signals,
            inner_optimization.selected_parameters,
        )
        development_result = run_backtest(
            development,
            selected_signals.iloc[:split_index],
            config,
            include_details=False,
            cycle_baseline_signals=(
                selected_cycle_baseline.iloc[:split_index]
                if selected_cycle_baseline is not None
                else None
            ),
        )
        holdout_result = run_backtest(
            data,
            selected_signals,
            config,
            evaluation_start=split_index,
            cycle_baseline_signals=selected_cycle_baseline,
        )
        _, holdout_benchmark = _benchmark(
            data,
            config,
            evaluation_start=split_index,
        )
        development_exposure_matched_benchmark = _exposure_matched_benchmark(
            development,
            development_result,
            config,
            include_details=False,
        )
        holdout_exposure_matched_benchmark = _exposure_matched_benchmark(
            data,
            holdout_result,
            config,
            include_details=False,
            evaluation_start=split_index,
        )
        full_result = run_backtest(
            data,
            selected_signals,
            config,
            cycle_baseline_signals=selected_cycle_baseline,
        )
        cost_stress_tests = run_cost_stress_tests(
            data,
            selected_signals,
            config,
            evaluation_start=split_index,
            cycle_baseline_signals=selected_cycle_baseline,
        )
        cost_stress_passed = all(item.passed for item in cost_stress_tests)
        (
            validation_passed,
            validation_code,
            validation_reason,
        ) = _validate_discovery_holdout(
            holdout_result.metrics,
            holdout_benchmark.metrics,
            holdout_exposure_matched_benchmark.metrics,
            body,
            cost_stress_passed=cost_stress_passed,
        )
        if validation_passed and not research_horizon["validation_eligible"]:
            validation_passed = False
            validation_code = "history_horizon_insufficient"
            validation_reason = str(research_horizon["reason"])
        forward_observation_eligible = is_forward_observation_eligible(
            validation_passed=validation_passed,
            validation_code=validation_code,
            closed_trades=holdout_result.metrics.closed_trades,
            cost_stress_passed=cost_stress_passed,
        )
        optimization = inner_optimization.model_copy(
            update={
                "train_ratio": body.train_ratio,
                "split_date": _date_value(data.index[split_index]),
                "train_metrics": development_result.metrics,
                "validation_metrics": holdout_result.metrics,
                "development_benchmark_metrics": development_benchmark.metrics,
                "development_exposure_matched_benchmark_metrics": (
                    development_exposure_matched_benchmark.metrics
                ),
                "validation_benchmark_metrics": holdout_benchmark.metrics,
                "validation_exposure_matched_benchmark_metrics": (
                    holdout_exposure_matched_benchmark.metrics
                ),
                "validation_excess_return": (
                    holdout_result.metrics.total_return
                    - holdout_benchmark.metrics.total_return
                ),
                "validation_timing_excess_return": (
                    holdout_result.metrics.total_return
                    - holdout_exposure_matched_benchmark.metrics.total_return
                ),
                "validation_passed": validation_passed,
                "validation_code": validation_code,
                "validation_reason": validation_reason,
                "forward_observation_eligible": forward_observation_eligible,
                "cost_stress_passed": cost_stress_passed,
                "cost_stress_tests": cost_stress_tests,
                "full_result": full_result,
            }
        )
        selected_warmup = strategy_warmup_bars(
            definition.id,
            inner_optimization.selected_parameters,
        )
        history = _with_warmup_metadata(
            history,
            selected_warmup,
            signal_history,
            warmup_error,
        )
        history = _with_discovery_horizon(history, research_horizon)
        backtest = _optimized_backtest_payload(
            body=body,
            history=history,
            data=data,
            definition=definition,
            signal_history=signal_history,
            optimization=optimization,
            benchmark_definition=benchmark_definition,
            benchmark_result=benchmark_result,
        )
        backtest["optimization"]["validation_trade_quality"] = _trade_quality(
            holdout_result
        )
        manifest_request = body.model_dump(mode="json")
        manifest_request.update(
            {
                "strategy_id": definition.id,
                "provider": adapter.name,
            }
        )
        backtest = attach_and_record_backtest_run(
            backtest,
            request_body=manifest_request,
            settings=request.app.state.settings,
            store=_research_run_store(request),
            provider=adapter.name,
            canonical_symbol=history.symbol,
        )
        candidates.append(
            {
                "strategy": backtest["strategy"],
                "validation_passed": validation_passed,
                "validation_code": validation_code,
                "validation_reason": validation_reason,
                "forward_observation_eligible": forward_observation_eligible,
                "validation_score": _validation_score(optimization),
                "selection_score": locked["selection_score"],
                "backtest": backtest,
            }
        )

    best_available = candidates[0] if candidates else None
    champion = best_available if best_available and best_available["validation_passed"] else None
    provisional = (
        best_available
        if best_available and best_available["forward_observation_eligible"]
        else None
    )
    status = (
        "validated_candidate"
        if champion is not None
        else "provisional_candidate"
        if provisional is not None
        else "no_validated_candidate"
        if best_available is not None
        else "development_rejected"
        if development_candidates
        else "constraints_too_strict"
    )
    if champion is not None:
        decision_mode = "validated_active"
        decision_title = "主动候选通过验证"
        decision_reason = (
                "主动候选已跑赢相同起点投入比例的固定份额基准并通过最终留出门槛；"
            "买入持有仍保留为持续对照。"
        )
    elif provisional is not None:
        decision_mode = "freeze_for_evidence"
        decision_title = "先用简单基准，冻结候选继续取证"
        decision_reason = (
            "主动候选仅因 5–9 笔留出交易而证据不足；"
            "在新数据补足前，不用它替代买入持有基准。"
        )
    elif development_candidates and not development_selection_eligible:
        decision_mode = "development_rejected"
        decision_title = "开发验证未通过，最终留出未触碰"
        decision_reason = (
            "短名单候选均未通过开发期内部的走步与留出验证；为保留最终未见样本的独立性，"
            "本次不会继续读取它。同期买入持有仅作为基准，不把开发期失败的主动参数包装成候选。"
        )
    elif benchmark_result.metrics.total_return > 0:
        decision_mode = "passive_baseline"
        decision_title = "当前证据支持基准优先"
        decision_reason = (
            "没有主动候选通过最终留出验证，而同期买入持有为正收益。"
            "在新的未见数据证明主动价值前，简单基准比未验证的复杂策略更可信。"
        )
    else:
        decision_mode = "no_actionable_strategy"
        decision_title = "当前没有可行动策略"
        decision_reason = (
            "主动候选没有通过验证，同期买入持有也未取得正收益；"
            "保留现金与继续研究比强行选择模板更诚实。"
        )
    passive_backtest = _passive_backtest_payload(
        body=body,
        history=history,
        data=data,
        benchmark_definition=benchmark_definition,
        benchmark_result=benchmark_result,
    )
    passive_request = body.model_dump(mode="json")
    passive_request.update(
        {
            "strategy_id": "buy-hold",
            "provider": adapter.name,
        }
    )
    passive_backtest = attach_and_record_backtest_run(
        passive_backtest,
        request_body=passive_request,
        settings=request.app.state.settings,
        store=_research_run_store(request),
        provider=adapter.name,
        canonical_symbol=history.symbol,
    )
    risk_budgeted_backtest, risk_budgeted_summary = (
        _risk_budgeted_backtest_payload(
            body=body,
            history=history,
            data=data,
            calibration_data=data.iloc[:split_index],
            benchmark_definition=benchmark_definition,
            benchmark_result=benchmark_result,
            maximum_drawdown=body.maximum_drawdown or 0.3,
            config=config,
        )
    )
    risk_budgeted_request = body.model_dump(mode="json")
    risk_budgeted_request.update(
        {
            "strategy_id": "constant-allocation",
            "provider": adapter.name,
        }
    )
    risk_budgeted_backtest = attach_and_record_backtest_run(
        risk_budgeted_backtest,
        request_body=risk_budgeted_request,
        settings=request.app.state.settings,
        store=_research_run_store(request),
        provider=adapter.name,
        canonical_symbol=history.symbol,
    )
    return {
        "symbol": body.symbol,
        "interval": body.interval,
        "objective": body.objective,
        "status": status,
        "selection_protocol": {
            "development_ratio": body.train_ratio,
            "templates_screened": len(shortlist),
            "screening_parameter_policy": (
                "deterministic_two_stage_coverage_then_adaptive_refinement"
            ),
            "screening_data_scope": "development_only",
            "screening_parameter_policy_symbol_agnostic": True,
            "screening_adaptation_scope": "requested_symbol_development_data",
            "screening_initial_parameter_limit_per_template": (
                _DISCOVERY_SCREENING_INITIAL_PARAMETER_LIMIT
            ),
            "screening_parameter_limit_per_template": (
                _DISCOVERY_SCREENING_PARAMETER_BUDGET
            ),
            "screening_parameter_grid_total": sum(
                int(item["screening_grid_total"])
                for item in shortlist
            ),
            "screening_parameter_sets_evaluated": sum(
                int(item["screening_candidates_evaluated"])
                for item in shortlist
            ),
            "screening_grid_coverage_ratio": (
                sum(
                    int(item["screening_candidates_evaluated"])
                    for item in shortlist
                )
                / sum(
                    int(item["screening_grid_total"])
                    for item in shortlist
                )
            ),
            "screening_constraints": screening_constraint_summary,
            "shortlisted": len(selected),
            "shortlist_selection_policy": "development_rank_with_feasible_family_coverage",
            "feasible_strategy_families": feasible_families,
            "shortlist_strategy_families": [
                _discovery_strategy_family(str(item["strategy"]["id"]))
                for item in selected
            ],
            "optimized": len(development_candidates),
            "development_validated": len(development_validated),
            "development_selection_eligible": len(development_selection_eligible),
            "development_selection_min_closed_trades": (
                MIN_DEVELOPMENT_SELECTION_CLOSED_TRADES
            ),
            "holdout_evaluated": len(candidates),
            "final_holdout_ratio": 1 - body.train_ratio,
            "final_holdout_min_closed_trades": FINAL_HOLDOUT_MIN_CLOSED_TRADES,
            "forward_observation_min_closed_trades": (
                FORWARD_OBSERVATION_MIN_CLOSED_TRADES
            ),
            "holdout_used_for_screening": False,
            "holdout_used_for_shortlisting": False,
            "holdout_execution_state_carried": True,
        },
        "data_metadata": _with_discovery_horizon(
            history
            if candidates
            else _with_warmup_metadata(
                history,
                required_warmup,
                signal_history,
                warmup_error,
            ),
            research_horizon,
        ).metadata,
        "development_market_regime": development_market_regime,
        "shortlist": shortlist,
        "selection_trials": selection_trials,
        "candidates": candidates,
        "failures": failures,
        "champion": champion,
        "provisional": provisional,
        "best_available": best_available,
        "research_decision": {
            "mode": decision_mode,
            "title": decision_title,
            "reason": decision_reason,
        },
        "passive_baseline": {
            "preferred": decision_mode in {"passive_baseline", "development_rejected"},
            "backtest": passive_backtest,
            "risk_budgeted": {
                **risk_budgeted_summary,
                "backtest": risk_budgeted_backtest,
            },
        },
        "elapsed_ms": round((perf_counter() - started_at) * 1_000),
        "citations": [item.model_dump(mode="json") for item in history.citations],
    }


@router.post("/backtests/compare")
async def compare_backtests(
    request: Request,
    body: CompareBacktestRequest,
) -> dict[str, Any]:
    adapter = _providers(request).resolve(body.symbol, body.provider)
    try:
        required_warmup = max(
            strategy_warmup_bars(definition.id, definition.parameters)
            for definition in STRATEGIES.values()
        )
        history, data, signal_history, warmup_error = await _history_for_backtest(
            adapter,
            body.symbol,
            body.start,
            body.end,
            body.interval,
            required_warmup,
        )
        history = _with_warmup_metadata(
            history,
            required_warmup,
            signal_history,
            warmup_error,
        )
        config = _backtest_config(adapter, body.config, body.interval)
        benchmark_definition, benchmark_result = _benchmark(data, config)
        rows: list[dict[str, Any]] = []
        for definition in STRATEGIES.values():
            strategy_config = (
                config.model_copy(update={"signal_delay_bars": 0})
                if definition.id in PASSIVE_STRATEGY_IDS
                else config
            )
            signals = strategy_signals(
                data,
                definition.id,
                definition.parameters,
                signal_history=signal_history,
            )
            cycle_baseline = strategy_cycle_baseline_signals(
                definition.id,
                signals,
                definition.parameters,
            )
            if definition.id == "constant-allocation":
                result = run_exposure_matched_benchmark(
                    data,
                    float(definition.parameters["allocation"]),
                    strategy_config,
                    include_details=False,
                )
                exposure_matched_benchmark = result
            else:
                result = run_backtest(
                    data,
                    signals,
                    strategy_config,
                    include_details=False,
                    cycle_baseline_signals=cycle_baseline,
                )
                exposure_matched_benchmark = _exposure_matched_benchmark(
                    data,
                    result,
                    strategy_config,
                    include_details=False,
                )
            score, quality = _template_score(
                result,
                benchmark_result,
                exposure_matched_benchmark,
            )
            if definition.id in PASSIVE_STRATEGY_IDS:
                quality = "baseline"
            rows.append(
                {
                    "strategy": definition.model_dump(),
                    "metrics": result.metrics.model_dump(mode="json"),
                    "comparison": _comparison(result, benchmark_result),
                    "exposure_matched_benchmark_metrics": (
                        exposure_matched_benchmark.metrics.model_dump(mode="json")
                    ),
                    "timing_comparison": _timing_comparison(
                        result,
                        exposure_matched_benchmark,
                    ),
                    "score": score,
                    "quality": quality,
                }
            )
        quality_rank = {
            "outperform": 0,
            "defensive": 1,
            "baseline": 2,
            "lagging": 3,
            "negative": 4,
        }
        rows.sort(
            key=lambda item: (
                quality_rank[str(item["quality"])],
                -float(item["score"]),
            )
        )
    except (RuntimeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "symbol": body.symbol,
        "interval": body.interval,
        "evaluation_mode": "template_default_snapshot",
        "evaluation_note": (
            "每个模板使用其公开默认参数，供理解策略结构与固定参数复现；"
            "这不是参数寻优、策略发现或推荐排行榜。"
        ),
        "data_metadata": history.metadata,
        "bars": len(history.rows),
        "benchmark": {
            "strategy": benchmark_definition,
            "metrics": benchmark_result.metrics.model_dump(mode="json"),
        },
        "results": rows,
        "citations": [item.model_dump(mode="json") for item in history.citations],
    }


@router.post("/backtests/custom")
async def create_custom_backtest(
    request: Request,
    body: CustomBacktestRequest,
) -> dict[str, Any]:
    adapter = _providers(request).resolve(body.symbol, body.provider)
    try:
        history = await adapter.history_interval(
            body.symbol,
            body.start,
            body.end,
            interval=body.interval,
        )
        data = history.to_frame()
        signals = SandboxExecutor().execute(body.code, data)
        config = _backtest_config(adapter, body.config, body.interval)
        result = run_backtest(data, signals, config)
        benchmark_definition, benchmark_result = _benchmark(data, config)
        exposure_matched_benchmark = _exposure_matched_benchmark(
            data,
            result,
            config,
        )
    except (RuntimeError, ValueError, SandboxError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    payload = {
        "symbol": body.symbol,
        "interval": body.interval,
        "data_metadata": history.metadata,
        "strategy": {
            "id": "custom",
            "name": body.name,
            "description": "由用户指令生成并在受限子进程中执行。",
            "parameters": {},
            "category": "自定义",
        },
        "strategy_code": body.code,
        "ohlcv": history.rows,
        "result": result.model_dump(mode="json"),
        "benchmark": {
            "strategy": benchmark_definition,
            "result": benchmark_result.model_dump(mode="json"),
        },
        "exposure_matched_benchmark": {
            "target_exposure": result.metrics.exposure_ratio,
            "result": exposure_matched_benchmark.model_dump(mode="json"),
        },
        "comparison": _comparison(result, benchmark_result),
        "timing_comparison": _timing_comparison(
            result,
            exposure_matched_benchmark,
        ),
        "diagnostics": _diagnostics(data, signals, result, benchmark_result),
        "citations": [item.model_dump(mode="json") for item in history.citations],
    }
    manifest_request = body.model_dump(mode="json")
    manifest_request.update(
        {
            "symbol": history.symbol,
            "provider": adapter.name,
        }
    )
    return attach_and_record_backtest_run(
        payload,
        request_body=manifest_request,
        settings=request.app.state.settings,
        store=_research_run_store(request),
        provider=adapter.name,
        canonical_symbol=history.symbol,
        run_kind="custom_backtest",
    )
