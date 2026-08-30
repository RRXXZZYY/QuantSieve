from __future__ import annotations

from math import sqrt
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel

from .backtest import (
    ANNUALIZED_RETURN_CAP,
    BacktestConfig,
    annualize_total_return,
)

PortfolioMethod = Literal[
    "initial_equal_hold",
    "periodic_equal",
    "periodic_inverse_volatility",
]


class PortfolioMetrics(BaseModel):
    bars: int
    duration_years: float
    total_return: float
    annualized_return: float
    annualized_return_capped: bool = False
    annualized_return_cap: float = ANNUALIZED_RETURN_CAP
    annualized_volatility: float
    sharpe_ratio: float
    max_drawdown: float
    rebalances: int
    turnover_ratio: float
    transaction_cost_ratio: float


class PortfolioBacktestResult(BaseModel):
    method: PortfolioMethod
    metrics: PortfolioMetrics
    equity: list[dict[str, Any]]
    allocations: list[dict[str, Any]]
    last_rebalance_target_weights: dict[str, float]
    ending_realized_weights: dict[str, float]
    latest_weights: dict[str, float]
    config: BacktestConfig


def run_portfolio_backtest(
    assets: dict[str, pd.DataFrame],
    method: PortfolioMethod,
    config: BacktestConfig | None = None,
    *,
    evaluation_start: Any | None = None,
    evaluation_end: Any | None = None,
    volatility_lookback: int = 60,
    rebalance_bars: int = 21,
    maximum_asset_weight: float = 0.4,
    include_details: bool = True,
) -> PortfolioBacktestResult:
    """Backtest a long-only multi-asset allocation with next-open rebalancing."""
    if not 2 <= len(assets) <= 8:
        raise ValueError("Portfolio backtests require between 2 and 8 assets.")
    if volatility_lookback < 2:
        raise ValueError("Volatility lookback must be at least 2 bars.")
    if rebalance_bars < 1:
        raise ValueError("Rebalance interval must be at least 1 bar.")
    if not 0 < maximum_asset_weight <= 1:
        raise ValueError("Maximum asset weight must be between 0 and 1.")
    if (
        method == "periodic_inverse_volatility"
        and maximum_asset_weight * len(assets) < 1
    ):
        raise ValueError("Maximum asset weight is too small for the number of assets.")

    resolved_config = config or BacktestConfig()
    normalized = {
        symbol: _normalize_asset(frame)
        for symbol, frame in assets.items()
    }
    common_index: pd.Index | None = None
    for frame in normalized.values():
        common_index = (
            frame.index
            if common_index is None
            else common_index.intersection(frame.index)
        )
    if common_index is None or common_index.empty:
        raise ValueError("Portfolio assets have no overlapping observations.")
    common_index = common_index.sort_values()
    open_prices = pd.DataFrame(
        {
            symbol: frame.reindex(common_index)["open"]
            for symbol, frame in normalized.items()
        }
    )
    close_prices = pd.DataFrame(
        {
            symbol: frame.reindex(common_index)["close"]
            for symbol, frame in normalized.items()
        }
    )
    desired_weights = _desired_weights(
        close_prices,
        method,
        volatility_lookback,
        maximum_asset_weight,
    )
    evaluation_index = common_index
    if evaluation_start is not None:
        evaluation_index = evaluation_index[
            evaluation_index >= _coerce_boundary(evaluation_start, common_index)
        ]
    if evaluation_end is not None:
        evaluation_index = evaluation_index[
            evaluation_index <= _coerce_boundary(evaluation_end, common_index)
        ]
    if evaluation_index.empty:
        raise ValueError("Portfolio evaluation interval has no overlapping observations.")
    return _simulate(
        open_prices.reindex(evaluation_index),
        close_prices.reindex(evaluation_index),
        desired_weights.reindex(evaluation_index),
        method,
        resolved_config,
        rebalance_bars,
        include_details,
    )


def _normalize_asset(data: pd.DataFrame) -> pd.DataFrame:
    frame = data.copy()
    frame.columns = [str(column).lower() for column in frame.columns]
    if frame.empty:
        raise ValueError("Portfolio prices must be non-empty and positive.")
    if frame.columns.has_duplicates:
        raise ValueError("Portfolio data columns must be unique.")
    missing = {"open", "close"} - set(frame.columns)
    if missing:
        raise ValueError(
            f"Portfolio data is missing columns: {', '.join(sorted(missing))}"
        )
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("Portfolio data must use a DatetimeIndex.")
    if frame.index.hasnans:
        raise ValueError("Portfolio data index must not contain invalid timestamps.")
    if frame.index.has_duplicates:
        raise ValueError("Portfolio data index must not contain duplicate timestamps.")
    if not frame.index.is_monotonic_increasing:
        raise ValueError("Portfolio data index must be strictly increasing.")
    frame.index = (
        frame.index.tz_localize("UTC").normalize()
        if frame.index.tz is None
        else frame.index.tz_convert("UTC").normalize()
    )
    if frame.index.has_duplicates:
        raise ValueError(
            "Portfolio data must contain at most one observation per UTC calendar date."
        )
    frame["open"] = pd.to_numeric(frame["open"], errors="raise")
    frame["close"] = pd.to_numeric(frame["close"], errors="raise")
    prices = frame[["open", "close"]].to_numpy(dtype=float)
    if not np.isfinite(prices).all():
        raise ValueError("Portfolio prices must be finite.")
    if (prices <= 0).any():
        raise ValueError("Portfolio prices must be non-empty and positive.")
    return frame


def _coerce_boundary(value: Any, index: pd.Index) -> Any:
    timestamp = pd.Timestamp(value)
    if isinstance(index, pd.DatetimeIndex):
        if index.tz is not None and timestamp.tzinfo is None:
            return timestamp.tz_localize(index.tz)
        if index.tz is None and timestamp.tzinfo is not None:
            return timestamp.tz_localize(None)
        if index.tz is not None:
            return timestamp.tz_convert(index.tz)
    return timestamp


def _desired_weights(
    close: pd.DataFrame,
    method: PortfolioMethod,
    volatility_lookback: int,
    maximum_asset_weight: float,
) -> pd.DataFrame:
    if method in {"initial_equal_hold", "periodic_equal"}:
        return pd.DataFrame(
            1 / len(close.columns),
            index=close.index,
            columns=close.columns,
        )
    if method != "periodic_inverse_volatility":
        raise ValueError(f"Unknown portfolio method: {method}")
    volatility = (
        close.pct_change()
        .rolling(volatility_lookback)
        .std(ddof=0)
        .shift(1)
    )
    inverse = 1 / volatility.replace(0, np.nan)
    rows = [
        _cap_weights(row, maximum_asset_weight)
        if row.notna().all()
        else pd.Series(np.nan, index=close.columns)
        for _, row in inverse.iterrows()
    ]
    return pd.DataFrame(rows, index=close.index, columns=close.columns)


def _cap_weights(values: pd.Series, cap: float) -> pd.Series:
    numeric = values.astype(float)
    if numeric.empty:
        raise ValueError("Weight inputs must not be empty.")
    if numeric.index.has_duplicates:
        raise ValueError("Weight inputs must use unique asset labels.")
    if not np.isfinite(numeric.to_numpy()).all() or (numeric < 0).any():
        raise ValueError("Weight inputs must be finite and non-negative.")
    if not np.isfinite(cap) or not 0 < cap <= 1:
        raise ValueError("Weight cap must be finite and between 0 and 1.")
    if cap * len(numeric) < 1 - 1e-12:
        raise ValueError("Weight cap is too small for the number of assets.")

    weights = pd.Series(0.0, index=numeric.index, dtype=float)
    active = pd.Series(True, index=numeric.index, dtype=bool)
    remaining = 1.0
    while active.any():
        active_values = numeric.loc[active]
        preference_sum = float(active_values.sum())
        if preference_sum > 0:
            proposed = active_values / preference_sum * remaining
        else:
            proposed = pd.Series(
                remaining / len(active_values),
                index=active_values.index,
                dtype=float,
            )
        over_cap = proposed > cap
        if not over_cap.any():
            weights.loc[proposed.index] = proposed
            break
        capped = proposed[over_cap].index
        weights.loc[capped] = cap
        active.loc[capped] = False
        remaining = 1 - float(weights.sum())

    if (
        not np.isfinite(weights.to_numpy()).all()
        or (weights < -1e-12).any()
        or (weights > cap + 1e-12).any()
        or not np.isclose(float(weights.sum()), 1.0, atol=1e-10)
    ):
        raise RuntimeError("Weight capping failed to produce a feasible allocation.")
    return weights


def _target_values(
    portfolio_value: float,
    current_values: pd.Series,
    weights: pd.Series,
    cost_rate: float,
) -> tuple[pd.Series, float]:
    investable = portfolio_value
    target = weights * investable
    for _ in range(20):
        cost = float((target - current_values).abs().sum() * cost_rate)
        next_investable = portfolio_value - cost
        next_target = weights * next_investable
        if abs(next_investable - investable) < 1e-8:
            target = next_target
            break
        investable = next_investable
        target = next_target
    cost = float((target - current_values).abs().sum() * cost_rate)
    return target, cost


def _simulate(
    open_prices: pd.DataFrame,
    close_prices: pd.DataFrame,
    desired_weights: pd.DataFrame,
    method: PortfolioMethod,
    config: BacktestConfig,
    rebalance_bars: int,
    include_details: bool,
) -> PortfolioBacktestResult:
    cash = config.initial_cash
    shares = pd.Series(0.0, index=open_prices.columns)
    cost_rate = config.fee_rate + config.slippage_rate
    equity_values: list[float] = []
    allocation_rows: list[dict[str, Any]] = []
    turnover = 0.0
    total_cost = 0.0
    rebalances = 0
    last_rebalance_target_weights = pd.Series(0.0, index=open_prices.columns)
    for bar, index in enumerate(open_prices.index):
        open_row = open_prices.loc[index]
        current_values = shares * open_row
        portfolio_value = float(cash + current_values.sum())
        should_rebalance = bar == 0 or (
            method != "initial_equal_hold" and bar % rebalance_bars == 0
        )
        weights = desired_weights.loc[index]
        if should_rebalance and weights.notna().all():
            target, cost = _target_values(
                portfolio_value,
                current_values,
                weights,
                cost_rate,
            )
            traded = float((target - current_values).abs().sum())
            shares = target / open_row
            cash = portfolio_value - float(target.sum()) - cost
            turnover += traded / max(portfolio_value, 1e-9)
            total_cost += cost
            rebalances += 1
            last_rebalance_target_weights = weights.astype(float)
            if include_details:
                allocation_rows.append(
                    {
                        "date": _date_value(index),
                        "weights": {
                            symbol: _finite(value)
                            for symbol, value in last_rebalance_target_weights.items()
                        },
                        "turnover": _finite(traded / max(portfolio_value, 1e-9)),
                        "cost": _finite(cost),
                    }
                )
        equity_value = float(cash + (shares * close_prices.loc[index]).sum())
        if not np.isfinite(equity_value):
            raise ValueError("Portfolio simulation produced non-finite equity.")
        equity_values.append(equity_value)
    equity = pd.Series(equity_values, index=open_prices.index)
    returns = equity.pct_change().fillna(equity.iloc[0] / config.initial_cash - 1)
    total_return = float(equity.iloc[-1] / config.initial_cash - 1)
    elapsed_seconds = (
        pd.Timestamp(equity.index[-1]) - pd.Timestamp(equity.index[0])
    ).total_seconds()
    duration_years = max(
        elapsed_seconds / (365.2425 * 24 * 60 * 60),
        1 / config.annual_periods,
    )
    annualized_return = annualize_total_return(total_return, duration_years)
    volatility = float(returns.std(ddof=0) * sqrt(config.annual_periods))
    sharpe = (
        float(returns.mean() / returns.std(ddof=0) * sqrt(config.annual_periods))
        if returns.std(ddof=0) > 0
        else 0.0
    )
    equity_with_initial = np.concatenate(
        ([config.initial_cash], equity.to_numpy(dtype=float))
    )
    drawdown = equity_with_initial / np.maximum.accumulate(equity_with_initial) - 1
    ending_values = shares * close_prices.iloc[-1]
    ending_portfolio_value = float(cash + ending_values.sum())
    ending_realized_weights = (
        ending_values / ending_portfolio_value
        if ending_portfolio_value > 0
        else pd.Series(0.0, index=open_prices.columns)
    )
    previous = config.initial_cash
    equity_rows: list[dict[str, Any]] = []
    if include_details:
        for index, value in equity.items():
            equity_rows.append(
                {
                    "date": _date_value(index),
                    "equity": _finite(value),
                    "return": _finite(value / previous - 1),
                }
            )
            previous = value
    return PortfolioBacktestResult(
        method=method,
        metrics=PortfolioMetrics(
            bars=len(equity),
            duration_years=_finite(duration_years),
            total_return=_finite(total_return),
            annualized_return=annualized_return.value,
            annualized_return_capped=annualized_return.capped,
            annualized_return_cap=ANNUALIZED_RETURN_CAP,
            annualized_volatility=_finite(volatility),
            sharpe_ratio=_finite(sharpe),
            max_drawdown=_finite(float(drawdown.min())),
            rebalances=rebalances,
            turnover_ratio=_finite(turnover),
            transaction_cost_ratio=_finite(total_cost / config.initial_cash),
        ),
        equity=equity_rows,
        allocations=allocation_rows,
        last_rebalance_target_weights={
            symbol: _finite(value)
            for symbol, value in last_rebalance_target_weights.items()
        },
        ending_realized_weights={
            symbol: _finite(value)
            for symbol, value in ending_realized_weights.items()
        },
        latest_weights={
            symbol: _finite(value)
            for symbol, value in ending_realized_weights.items()
        },
        config=config,
    )


def _date_value(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _finite(value: float) -> float:
    return float(value) if np.isfinite(value) else 0.0
