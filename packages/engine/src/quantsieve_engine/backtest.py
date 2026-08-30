from __future__ import annotations

from dataclasses import dataclass
from math import floor
from statistics import fmean
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

CALENDAR_DAYS_PER_YEAR = 365.2425
ANNUALIZED_RETURN_CAP = 1_000_000.0
"""Finite saturation value for annualized returns (+100,000,000%)."""


@dataclass(frozen=True)
class AnnualizedReturnEstimate:
    value: float
    capped: bool


class BacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_cash: float = Field(default=100_000.0, gt=0)
    fee_rate: float = Field(default=0.0003, ge=0, le=0.1)
    slippage_rate: float = Field(default=0.0002, ge=0, le=0.1)
    annual_periods: int = Field(default=252, gt=0)
    bar_interval: Literal["15m", "1h", "4h", "1d", "1wk"] = "1d"
    signal_delay_bars: int = Field(default=1, ge=0, le=5)


class BacktestMetrics(BaseModel):
    bars: int
    duration_years: float
    total_return: float
    annualized_return: float
    annualized_return_capped: bool = False
    annualized_return_cap: float = ANNUALIZED_RETURN_CAP
    annualized_volatility: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    trades: int
    closed_trades: int
    trades_per_year: float
    average_holding_bars: float
    profit_factor: float
    exposure_ratio: float
    max_cash_streak: int
    max_cash_streak_ratio: float


class BacktestResult(BaseModel):
    metrics: BacktestMetrics
    equity: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    position_cycles: list[dict[str, Any]] = Field(default_factory=list)
    config: BacktestConfig


class RiskBudgetedBenchmark(BaseModel):
    requested_max_drawdown: float = Field(gt=0, le=1)
    target_exposure: float = Field(ge=0, le=1)
    cash_reserve_ratio: float = Field(ge=0, le=1)
    budget_satisfied: bool
    result: BacktestResult


def evaluation_duration_years(index: pd.Index, annual_periods: int) -> float:
    """Measure a result window in calendar years when timestamps are available.

    CAGR and event frequency describe how much real time the result spans, so a
    sparse or suspended series must not look shorter merely because it contains
    fewer rows. Volatility and Sharpe remain bar-sampled statistics and continue
    to use ``annual_periods`` in ``run_backtest``. A single row or a non-datetime
    index has no measurable timestamp span and safely falls back to the legacy
    bar-count convention.
    """
    if annual_periods <= 0:
        raise ValueError("annual_periods must be positive.")
    periods = max(len(index), 1)
    if isinstance(index, pd.DatetimeIndex) and len(index) >= 2:
        elapsed_seconds = (
            pd.Timestamp(index[-1]) - pd.Timestamp(index[0])
        ).total_seconds()
        if np.isfinite(elapsed_seconds) and elapsed_seconds > 0:
            return float(
                elapsed_seconds / (CALENDAR_DAYS_PER_YEAR * 24 * 60 * 60)
            )
    return periods / annual_periods


def annualize_total_return(
    total_return: float,
    duration_years: float,
) -> AnnualizedReturnEstimate:
    """Annualize a compounded return without overflow or silent non-finite output.

    Positive growth is calculated in log space. Values above
    ``ANNUALIZED_RETURN_CAP`` saturate at that explicit contract limit and set
    ``capped=True`` so callers can render the result as "at least the cap".
    Returns at or below -100% retain the existing -100% floor because a real
    compounded CAGR is undefined once account equity is non-positive.
    """
    if not np.isfinite(total_return):
        raise ValueError("Total return must be finite before annualization.")
    if not np.isfinite(duration_years) or duration_years <= 0:
        raise ValueError("Duration years must be finite and positive.")
    if total_return <= -1:
        return AnnualizedReturnEstimate(value=-1.0, capped=False)

    annualized_log_growth = float(np.log1p(total_return) / duration_years)
    maximum_log_growth = float(np.log1p(ANNUALIZED_RETURN_CAP))
    if annualized_log_growth > maximum_log_growth:
        return AnnualizedReturnEstimate(
            value=ANNUALIZED_RETURN_CAP,
            capped=True,
        )
    value = float(np.expm1(annualized_log_growth))
    if not np.isfinite(value):
        raise ValueError("Annualized return calculation produced a non-finite value.")
    return AnnualizedReturnEstimate(value=value, capped=False)


def run_backtest(
    data: pd.DataFrame,
    signals: pd.Series,
    config: BacktestConfig | None = None,
    *,
    include_details: bool = True,
    evaluation_start: int = 0,
    cycle_baseline_signals: pd.Series | None = None,
) -> BacktestResult:
    """Run a long/cash backtest with next-bar-open execution by default.

    ``evaluation_start`` keeps the preceding bars as execution context but
    reports metrics only from that bar onward.  It is used for chronological
    validation windows so a position decided before the split still carries
    its first overnight move, turnover and next-open execution into the
    evaluated period.

    ``cycle_baseline_signals`` explicitly defines a core allocation whose
    excess exposure is evaluated as an independent satellite cycle. Its signals
    receive the same execution delay and cost model as the strategy. When
    omitted, the baseline is cash and the usual flat-to-position-to-flat cycle
    semantics apply.
    """
    config = config or BacktestConfig()
    frame = _normalize_data(data)
    if not 0 <= evaluation_start < len(frame):
        raise ValueError("evaluation_start must point to a bar in the supplied data.")
    aligned_signal = signals.reindex(frame.index).fillna(0).clip(0, 1).astype(float)
    position = aligned_signal.shift(config.signal_delay_bars).fillna(0)
    previous_position = position.shift(1).fillna(0)
    previous_close = frame["close"].shift(1)
    overnight_return = (frame["open"] / previous_close - 1).fillna(0)
    intraday_return = frame["close"] / frame["open"] - 1
    gross_growth = (1 + previous_position * overnight_return) * (
        1 + position * intraday_return
    )
    turnover = position.diff().abs().fillna(position.abs())
    costs = turnover * (config.fee_rate + config.slippage_rate)
    strategy_return = gross_growth - 1 - costs
    cycle_baseline_position: pd.Series | None = None
    cycle_baseline_return: pd.Series | None = None
    if cycle_baseline_signals is not None:
        aligned_cycle_baseline = (
            cycle_baseline_signals.reindex(frame.index)
            .fillna(0)
            .clip(0, 1)
            .astype(float)
        )
        cycle_baseline_position = aligned_cycle_baseline.shift(
            config.signal_delay_bars
        ).fillna(0)
        if ((cycle_baseline_position - position) > 1e-12).any():
            raise ValueError(
                "Cycle baseline allocation must not exceed strategy allocation."
            )
        previous_cycle_baseline = cycle_baseline_position.shift(1).fillna(0)
        cycle_baseline_growth = (
            1 + previous_cycle_baseline * overnight_return
        ) * (1 + cycle_baseline_position * intraday_return)
        cycle_baseline_turnover = (
            cycle_baseline_position.diff()
            .abs()
            .fillna(cycle_baseline_position.abs())
        )
        cycle_baseline_costs = cycle_baseline_turnover * (
            config.fee_rate + config.slippage_rate
        )
        cycle_baseline_return = (
            cycle_baseline_growth - 1 - cycle_baseline_costs
        )
    evaluated_return = strategy_return.iloc[evaluation_start:]
    evaluated_position = position.iloc[evaluation_start:]
    equity = config.initial_cash * (1 + evaluated_return).cumprod()

    periods = max(len(evaluated_return), 1)
    duration_years = evaluation_duration_years(
        evaluated_return.index,
        config.annual_periods,
    )
    total_return = float(equity.iloc[-1] / config.initial_cash - 1)
    annualized_return = annualize_total_return(total_return, duration_years)
    volatility = float(evaluated_return.std(ddof=0) * np.sqrt(config.annual_periods))
    sharpe = (
        float(
            evaluated_return.mean()
            / evaluated_return.std(ddof=0)
            * np.sqrt(config.annual_periods)
        )
        if evaluated_return.std(ddof=0) > 0
        else 0.0
    )
    # The account starts at ``initial_cash`` before the first evaluated return.
    # Keep that starting balance in the high-water mark even though it is not an
    # extra row in the published equity series.  Otherwise a loss or entry cost
    # on the first bar becomes the first observed "peak" and incorrectly reports
    # zero drawdown until the account falls below that already-reduced balance.
    high_watermark = equity.cummax().clip(lower=config.initial_cash)
    drawdown = equity / high_watermark - 1
    exposure_ratio = float(evaluated_position.mean())
    max_cash_streak = _longest_true_streak(evaluated_position <= 0)

    all_trade_rows = _extract_trades(
        frame["open"],
        frame["close"],
        position,
        config.fee_rate + config.slippage_rate,
    )
    # A lot opened before the validation boundary has no new entry inside the
    # evaluated window.  Excluding it from trade-quality metrics is
    # deliberately conservative and prevents development-period P&L from
    # leaking into the holdout's win-rate/profit-factor summary.
    trade_rows = [
        row for row in all_trade_rows if int(row["_entry_location"]) >= evaluation_start
    ]
    all_position_cycle_rows = _extract_position_cycles(
        strategy_return,
        position,
        baseline_return=cycle_baseline_return,
        baseline_position=cycle_baseline_position,
    )
    # A position or satellite allocation carried into a holdout is one
    # statistical sample that already began in development. Exclude that entire
    # cycle even if its exit executes in-window; otherwise its compounded return
    # would leak development-period evidence into validation quality metrics.
    position_cycle_rows = [
        row
        for row in all_position_cycle_rows
        if int(row["_entry_location"]) >= evaluation_start
    ]
    # ``trades`` is the count of actual allocation-change executions inside the
    # evaluated window.  Use turnover for every window, including a full-sample
    # run, so an entry and its later exit have the same meaning in development,
    # walk-forward, and final-holdout frequency constraints. Trade-quality
    # statistics below use independent flat-to-long-to-flat cycles or explicit
    # satellite-over-core cycles, not the capital lots retained in
    # ``result.trades`` for attribution.
    execution_trades = int((turnover.iloc[evaluation_start:] > 1e-12).sum())
    closed_position_cycles = [
        row for row in position_cycle_rows if bool(row["closed"])
    ]
    closed_cycle_returns = [
        float(row["return"]) for row in closed_position_cycles
    ]
    win_rate = (
        sum(value > 0 for value in closed_cycle_returns) / len(closed_cycle_returns)
        if closed_cycle_returns
        else 0.0
    )
    gains = sum(value for value in closed_cycle_returns if value > 0)
    losses = abs(sum(value for value in closed_cycle_returns if value < 0))
    profit_factor = gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0)
    average_holding_bars = (
        fmean(float(row["holding_bars"]) for row in closed_position_cycles)
        if closed_position_cycles
        else 0.0
    )
    equity_rows = (
        [
            {
                "date": _date_value(index),
                "equity": _finite(value),
                "return": _finite(evaluated_return.loc[index]),
                "position": _finite(evaluated_position.loc[index]),
            }
            for index, value in equity.items()
        ]
        if include_details
        else []
    )
    for row in trade_rows:
        row.pop("_entry_location")
        row.pop("_exit_location")
    for row in position_cycle_rows:
        row.pop("_entry_location")
        row.pop("_exit_location")
    return BacktestResult(
        metrics=BacktestMetrics(
            bars=periods,
            duration_years=_finite(duration_years),
            total_return=_finite(total_return),
            annualized_return=annualized_return.value,
            annualized_return_capped=annualized_return.capped,
            annualized_return_cap=ANNUALIZED_RETURN_CAP,
            annualized_volatility=_finite(volatility),
            sharpe_ratio=_finite(sharpe),
            max_drawdown=_finite(drawdown.min()),
            win_rate=_finite(win_rate),
            # Count every allocation change executed inside this window,
            # including the initial entry in a full-sample run and an exit of a
            # position opened before a validation boundary. Trade-quality P&L
            # remains restricted to independent cycles opened in-window above.
            trades=execution_trades,
            closed_trades=len(closed_cycle_returns),
            trades_per_year=_finite(
                execution_trades / max(duration_years, 1e-9)
            ),
            average_holding_bars=_finite(average_holding_bars),
            profit_factor=_finite(profit_factor),
            exposure_ratio=_finite(exposure_ratio),
            max_cash_streak=max_cash_streak,
            max_cash_streak_ratio=_finite(max_cash_streak / periods),
        ),
        equity=equity_rows,
        trades=trade_rows if include_details else [],
        position_cycles=position_cycle_rows if include_details else [],
        config=config,
    )


def run_exposure_matched_benchmark(
    data: pd.DataFrame,
    exposure_ratio: float,
    config: BacktestConfig | None = None,
    *,
    include_details: bool = True,
    evaluation_start: int = 0,
) -> BacktestResult:
    """Buy once at the first open, then hold fixed shares and residual cash.

    ``exposure_ratio`` is the fraction of starting capital assigned to the
    purchase, including its one-way fee and slippage budget. No later weight
    drift is rebalanced. For a validation slice, the shares and cash established
    at the context's first bar are carried across the boundary, while reported
    returns and execution events start at ``evaluation_start``.
    """
    if not 0 <= exposure_ratio <= 1:
        raise ValueError("Exposure-matched benchmark requires a ratio between 0 and 1.")
    benchmark_config = (config or BacktestConfig()).model_copy(
        update={"signal_delay_bars": 0}
    )
    frame = _normalize_data(data)
    if not 0 <= evaluation_start < len(frame):
        raise ValueError("evaluation_start must point to a bar in the supplied data.")

    allocation = float(exposure_ratio)
    transaction_rate = benchmark_config.fee_rate + benchmark_config.slippage_rate
    allocated_capital = benchmark_config.initial_cash * allocation
    # Fees and slippage are paid once from the allocated capital. This preserves
    # the engine's existing one-way ``allocation * transaction_rate`` friction
    # while keeping residual cash non-negative even for a 100% allocation.
    invested_capital = allocated_capital * (1 - transaction_rate)
    entry_open = float(frame["open"].iloc[0])
    shares = invested_capital / entry_open
    cash = benchmark_config.initial_cash - allocated_capital
    asset_value = frame["close"] * shares
    portfolio_value = asset_value + cash
    full_return = portfolio_value.pct_change()
    full_return.iloc[0] = (
        portfolio_value.iloc[0] / benchmark_config.initial_cash - 1
    )
    full_exposure = (asset_value / portfolio_value).fillna(0.0)

    evaluated_return = full_return.iloc[evaluation_start:]
    evaluated_exposure = full_exposure.iloc[evaluation_start:]
    equity = benchmark_config.initial_cash * (1 + evaluated_return).cumprod()
    periods = max(len(evaluated_return), 1)
    duration_years = evaluation_duration_years(
        evaluated_return.index,
        benchmark_config.annual_periods,
    )
    total_return = float(equity.iloc[-1] / benchmark_config.initial_cash - 1)
    annualized_return = annualize_total_return(total_return, duration_years)
    volatility = float(
        evaluated_return.std(ddof=0) * np.sqrt(benchmark_config.annual_periods)
    )
    sharpe = (
        float(
            evaluated_return.mean()
            / evaluated_return.std(ddof=0)
            * np.sqrt(benchmark_config.annual_periods)
        )
        if evaluated_return.std(ddof=0) > 0
        else 0.0
    )
    high_watermark = equity.cummax().clip(lower=benchmark_config.initial_cash)
    drawdown = equity / high_watermark - 1
    max_cash_streak = _longest_true_streak(evaluated_exposure <= 1e-12)
    entry_in_window = allocation > 0 and evaluation_start == 0
    execution_trades = int(entry_in_window)
    holding_bars = max(len(frame) - 1, 1) if entry_in_window else 0
    trade_rows = (
        [
            {
                "entry_date": _date_value(frame.index[0]),
                "exit_date": _date_value(frame.index[-1]),
                "entry_price": entry_open / (1 - transaction_rate),
                "exit_price": float(frame["close"].iloc[-1]),
                "return": (
                    float(frame["close"].iloc[-1])
                    * (1 - transaction_rate)
                    / entry_open
                    - 1
                ),
                "holding_bars": holding_bars,
                "position_size": allocation,
                "closed": False,
            }
        ]
        if entry_in_window
        else []
    )
    position_cycle_rows = (
        [
            {
                "entry_date": _date_value(frame.index[0]),
                "exit_date": _date_value(frame.index[-1]),
                "holding_bars": holding_bars,
                "return": float((1 + full_return).prod() - 1),
                "closed": False,
                "cycle_kind": "flat_to_flat",
                "return_semantics": "compounded_strategy_return",
            }
        ]
        if entry_in_window
        else []
    )
    equity_rows = (
        [
            {
                "date": _date_value(index),
                "equity": _finite(value),
                "return": _finite(evaluated_return.loc[index]),
                "position": _finite(evaluated_exposure.loc[index]),
            }
            for index, value in equity.items()
        ]
        if include_details
        else []
    )
    return BacktestResult(
        metrics=BacktestMetrics(
            bars=periods,
            duration_years=_finite(duration_years),
            total_return=_finite(total_return),
            annualized_return=annualized_return.value,
            annualized_return_capped=annualized_return.capped,
            annualized_return_cap=ANNUALIZED_RETURN_CAP,
            annualized_volatility=_finite(volatility),
            sharpe_ratio=_finite(sharpe),
            max_drawdown=_finite(drawdown.min()),
            win_rate=0.0,
            trades=execution_trades,
            closed_trades=0,
            trades_per_year=_finite(
                execution_trades / max(duration_years, 1e-9)
            ),
            # A buy-and-hold position is still open at the final mark, so it is
            # not a completed holding-period sample.
            average_holding_bars=0.0,
            profit_factor=0.0,
            exposure_ratio=_finite(float(evaluated_exposure.mean())),
            max_cash_streak=max_cash_streak,
            max_cash_streak_ratio=_finite(max_cash_streak / periods),
        ),
        equity=equity_rows,
        trades=trade_rows if include_details else [],
        position_cycles=position_cycle_rows if include_details else [],
        config=benchmark_config,
    )


def run_risk_budgeted_benchmark(
    data: pd.DataFrame,
    maximum_drawdown: float,
    config: BacktestConfig | None = None,
    *,
    include_details: bool = True,
) -> RiskBudgetedBenchmark:
    """Calibrate the largest constant long allocation within a historical drawdown budget."""
    if not 0 < maximum_drawdown <= 1:
        raise ValueError("Maximum drawdown budget must be between 0 and 1.")
    resolved_config = config or BacktestConfig()
    full = run_exposure_matched_benchmark(
        data,
        1.0,
        resolved_config,
        include_details=False,
    )
    if abs(full.metrics.max_drawdown) <= maximum_drawdown:
        target_exposure = 1.0
    else:
        lower = 0.0
        upper = 1.0
        for _ in range(36):
            candidate = (lower + upper) / 2
            result = run_exposure_matched_benchmark(
                data,
                candidate,
                resolved_config,
                include_details=False,
            )
            if abs(result.metrics.max_drawdown) <= maximum_drawdown:
                lower = candidate
            else:
                upper = candidate
        target_exposure = lower
    # Keep the published allocation reproducible and human-editable without
    # rounding upward through the historical risk budget.
    target_exposure = floor(target_exposure * 10_000) / 10_000
    result = run_exposure_matched_benchmark(
        data,
        target_exposure,
        resolved_config,
        include_details=include_details,
    )
    return RiskBudgetedBenchmark(
        requested_max_drawdown=maximum_drawdown,
        target_exposure=target_exposure,
        cash_reserve_ratio=1 - target_exposure,
        budget_satisfied=abs(result.metrics.max_drawdown) <= maximum_drawdown + 1e-9,
        result=result,
    )


def _normalize_data(data: pd.DataFrame) -> pd.DataFrame:
    frame = data.copy()
    frame.columns = [str(column).lower() for column in frame.columns]
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"OHLCV data is missing columns: {', '.join(sorted(missing))}")
    if frame.empty:
        raise ValueError("OHLCV data must not be empty.")
    frame = frame.sort_index()
    if not frame.index.is_unique:
        raise ValueError("OHLCV timestamps must be unique.")

    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="raise")
        values = frame[column].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError(f"{column.title()} prices must be finite.")
        if (values <= 0).any():
            raise ValueError(f"{column.title()} prices must be positive.")

    frame["volume"] = pd.to_numeric(frame["volume"], errors="raise")
    volume = frame["volume"].to_numpy(dtype=float)
    if not np.isfinite(volume).all():
        raise ValueError("Volume must be finite.")
    if (volume < 0).any():
        raise ValueError("Volume must not be negative.")
    if (frame["high"] < frame["low"]).any():
        raise ValueError("High prices must not be below low prices.")
    if ((frame["open"] > frame["high"]) | (frame["close"] > frame["high"])).any():
        raise ValueError("Open and close prices must not exceed the high price.")
    if ((frame["open"] < frame["low"]) | (frame["close"] < frame["low"])).any():
        raise ValueError("Open and close prices must not be below the low price.")
    return frame


def _extract_trades(
    open_price: pd.Series,
    close: pd.Series,
    position: pd.Series,
    transaction_rate: float,
) -> list[dict[str, Any]]:
    """Match allocation increases and reductions as LIFO capital lots."""
    lots: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    previous = 0.0
    tolerance = 1e-12
    for location, (index, current_raw) in enumerate(position.items()):
        current = float(current_raw)
        change = current - previous
        if change > tolerance:
            lots.append(
                {
                    "entry_date": index,
                    "entry_price_raw": float(open_price.loc[index]),
                    "entry_location": location,
                    "position_size": change,
                }
            )
        elif change < -tolerance:
            remaining = -change
            while remaining > tolerance and lots:
                lot = lots[-1]
                lot_size = float(lot["position_size"])
                matched_size = min(remaining, lot_size)
                entry_price = float(lot["entry_price_raw"]) * (
                    1 + transaction_rate
                )
                exit_price = float(open_price.loc[index]) * (
                    1 - transaction_rate
                )
                rows.append(
                    {
                        "entry_date": _date_value(lot["entry_date"]),
                        "exit_date": _date_value(index),
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "return": exit_price / entry_price - 1,
                        "holding_bars": max(
                            location - int(lot["entry_location"]),
                            1,
                        ),
                        "position_size": matched_size,
                        "closed": True,
                        "_entry_location": int(lot["entry_location"]),
                        "_exit_location": location,
                    }
                )
                remaining -= matched_size
                if matched_size >= lot_size - tolerance:
                    lots.pop()
                else:
                    lot["position_size"] = lot_size - matched_size
        previous = current

    exit_index = close.index[-1]
    exit_location = len(position) - 1
    for lot in lots:
        entry_price = float(lot["entry_price_raw"]) * (1 + transaction_rate)
        exit_price = float(close.loc[exit_index])
        rows.append(
            {
                "entry_date": _date_value(lot["entry_date"]),
                "exit_date": _date_value(exit_index),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "return": exit_price / entry_price - 1,
                "holding_bars": max(
                    exit_location - int(lot["entry_location"]),
                    1,
                ),
                "position_size": float(lot["position_size"]),
                "closed": False,
                "_entry_location": int(lot["entry_location"]),
                "_exit_location": exit_location,
            }
        )
    rows.sort(key=lambda row: int(row["_entry_location"]))
    return rows


def _extract_position_cycles(
    strategy_return: pd.Series,
    position: pd.Series,
    *,
    baseline_return: pd.Series | None = None,
    baseline_position: pd.Series | None = None,
) -> list[dict[str, Any]]:
    """Extract independent position or satellite-allocation periods.

    With no baseline, a cycle starts when the executed position moves from flat
    to non-zero and ends when it returns to flat. With an explicit baseline, a
    cycle starts when executed strategy allocation exceeds the core allocation
    and ends when it returns to that baseline. Partial changes stay inside the
    same sample.

    A flat cycle compounds actual strategy returns. A satellite cycle measures
    compounded wealth relative to the separately costed core-only baseline, so
    the core asset move is not misreported as evidence for satellite timing.
    """
    if (baseline_return is None) != (baseline_position is None):
        raise ValueError(
            "Cycle baseline return and position must be supplied together."
        )
    if baseline_position is None:
        resolved_baseline_position = pd.Series(0.0, index=position.index)
        cycle_kind = "flat_to_flat"
        return_semantics = "compounded_strategy_return"
    else:
        resolved_baseline_position = baseline_position
        cycle_kind = "satellite_over_core"
        return_semantics = "compounded_relative_to_core"

    rows: list[dict[str, Any]] = []
    entry_location: int | None = None
    previous_excess = 0.0
    tolerance = 1e-12
    excess_position = position - resolved_baseline_position
    for location, (_index, current_raw) in enumerate(excess_position.items()):
        current_excess = float(current_raw)
        if previous_excess <= tolerance and current_excess > tolerance:
            entry_location = location
        elif (
            previous_excess > tolerance
            and current_excess <= tolerance
            and entry_location is not None
        ):
            rows.append(
                _position_cycle_row(
                    strategy_return,
                    baseline_return=baseline_return,
                    entry_location=entry_location,
                    exit_location=location,
                    closed=True,
                    cycle_kind=cycle_kind,
                    return_semantics=return_semantics,
                )
            )
            entry_location = None
        previous_excess = current_excess

    if entry_location is not None:
        rows.append(
            _position_cycle_row(
                strategy_return,
                baseline_return=baseline_return,
                entry_location=entry_location,
                exit_location=len(position) - 1,
                closed=False,
                cycle_kind=cycle_kind,
                return_semantics=return_semantics,
            )
        )
    return rows


def _position_cycle_row(
    strategy_return: pd.Series,
    *,
    baseline_return: pd.Series | None,
    entry_location: int,
    exit_location: int,
    closed: bool,
    cycle_kind: str,
    return_semantics: str,
) -> dict[str, Any]:
    cycle_returns = strategy_return.iloc[entry_location : exit_location + 1]
    strategy_growth = float((1 + cycle_returns).prod())
    if baseline_return is None:
        cycle_return = strategy_growth - 1
    else:
        baseline_cycle_returns = baseline_return.iloc[
            entry_location : exit_location + 1
        ]
        baseline_growth = float((1 + baseline_cycle_returns).prod())
        cycle_return = (
            strategy_growth / baseline_growth - 1
            if abs(baseline_growth) > 1e-12
            else 0.0
        )
    return {
        "entry_date": _date_value(strategy_return.index[entry_location]),
        # For an open cycle this is the final mark date, matching ``trades``.
        "exit_date": _date_value(strategy_return.index[exit_location]),
        "holding_bars": max(exit_location - entry_location, 1),
        "return": _finite(cycle_return),
        "closed": closed,
        "cycle_kind": cycle_kind,
        "return_semantics": return_semantics,
        "_entry_location": entry_location,
        "_exit_location": exit_location,
    }


def _date_value(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _finite(value: float) -> float:
    return float(value) if np.isfinite(value) else 0.0


def _longest_true_streak(values: pd.Series) -> int:
    longest = 0
    current = 0
    for value in values.astype(bool):
        if value:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest
