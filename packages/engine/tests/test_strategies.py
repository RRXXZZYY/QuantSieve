from collections.abc import Sequence
from math import exp, log, sin

import pandas as pd
import pytest
from quantsieve_engine import (
    STRATEGIES,
    get_strategy,
    parameter_candidates,
    strategy_signals,
    strategy_warmup_bars,
)


def price_frame(
    closes: Sequence[float],
    *,
    start: str = "2025-01-01",
) -> pd.DataFrame:
    index = pd.date_range(start, periods=len(closes), freq="D", tz="UTC")
    close = pd.Series(closes, index=index, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1_000_000.0,
        },
        index=index,
    )


def test_strategy_warmup_tracks_parameter_lookback() -> None:
    assert strategy_warmup_bars("ema-cross", {"fast": 3, "slow": 8}) == 8
    assert strategy_warmup_bars(
        "macd",
        {"fast": 5, "slow": 20, "signal": 9},
    ) == 28
    assert strategy_warmup_bars(
        "macd-regime",
        {"fast": 5, "slow": 20, "signal": 9, "regime": 50},
    ) == 50
    assert strategy_warmup_bars(
        "breakout",
        {"entry_period": 55, "exit_period": 20},
    ) == 56
    assert strategy_warmup_bars(
        "core-trend-allocation",
        {"period": 200, "defensive_exposure": 0.35},
    ) == 200
    assert strategy_warmup_bars(
        "volatility-target-trend",
        {
            "trend_period": 50,
            "volatility_period": 20,
            "minimum_allocation": 0.25,
            "maximum_allocation": 1,
            "adjustment_band": 0.15,
        },
    ) == 81
    assert strategy_warmup_bars(
        "mean-reversion",
        {"period": 20, "entry_z": 2, "exit_z": 0, "regime": 50},
    ) == 50
    assert strategy_warmup_bars(
        "atr-trend",
        {"fast": 3, "slow": 8, "atr_period": 10, "atr_multiplier": 3},
    ) == 10
    assert strategy_warmup_bars(
        "breakout-atr",
        {
            "entry_period": 8,
            "exit_period": 3,
            "atr_period": 10,
            "atr_multiplier": 3,
        },
    ) == 10
    assert strategy_warmup_bars(
        "rsi-regime-atr",
        {
            "rsi_period": 7,
            "oversold": 30,
            "exit_rsi": 55,
            "regime": 50,
            "atr_period": 10,
            "atr_multiplier": 2,
        },
    ) == 50


def test_ema_cross_stays_in_cash_until_slow_average_is_initialized() -> None:
    data = price_frame([100 + index for index in range(20)])

    signals = strategy_signals(
        data,
        "ema-cross",
        {"fast": 3, "slow": 8},
    )

    assert signals.iloc[:7].eq(0).all()
    assert signals.iloc[7:].eq(1).all()


def test_signal_history_initializes_indicator_without_entering_evaluation_metrics() -> None:
    history = price_frame([100 + index for index in range(20)])
    evaluation = price_frame(
        [120 + index for index in range(5)],
        start="2025-01-21",
    )

    signals = strategy_signals(
        evaluation,
        "ema-cross",
        {"fast": 3, "slow": 8},
        signal_history=history,
    )

    assert signals.index.equals(evaluation.index)
    assert signals.eq(1).all()


def test_rsi_exits_when_rolling_average_loss_reaches_zero() -> None:
    data = price_frame([100, 90, 80, 70, 60, 50, 51, 52, 53, 54, 55, 56])
    _, rsi = get_strategy("rsi")

    signals = rsi(
        data,
        {
            "period": 5,
            "oversold": 30,
            "overbought": 99,
        },
    )

    assert signals.iloc[5] == 1
    assert signals.iloc[-1] == 0


def test_mean_reversion_does_not_buy_a_pullback_below_its_regime() -> None:
    data = price_frame([100, 99, 98, 97, 96, 95, 94, 93, 92, 91])

    signals = strategy_signals(
        data,
        "mean-reversion",
        {"period": 3, "entry_z": 0.5, "exit_z": 0, "regime": 5},
    )

    assert signals.iloc[:4].eq(0).all()
    assert signals.eq(0).all()


def test_mean_reversion_grid_includes_trend_regime_parameter() -> None:
    candidates = parameter_candidates("mean-reversion")

    assert {"period": 20, "entry_z": 2, "exit_z": 0, "regime": 150} in candidates
    assert all(candidate["regime"] >= 50 for candidate in candidates)


def test_core_trend_allocation_keeps_defensive_capital_below_trend() -> None:
    data = price_frame([100, 101, 102, 90, 89, 110, 112])

    signals = strategy_signals(
        data,
        "core-trend-allocation",
        {"period": 3, "defensive_exposure": 0.35},
    )

    assert signals.iloc[:2].eq(0).all()
    assert set(signals.iloc[2:].unique()).issubset({0.35, 1.0})
    assert 0.35 in signals.iloc[2:].values
    assert 1.0 in signals.iloc[2:].values


def test_volatility_target_trend_scales_only_above_a_ready_trend() -> None:
    data = price_frame(
        [100, 101, 102, 103, 104, 105, 106, 108, 107, 111, 109, 114, 113, 118]
    )

    signals = strategy_signals(
        data,
        "volatility-target-trend",
        {
            "trend_period": 3,
            "volatility_period": 2,
            "minimum_allocation": 0.25,
            "maximum_allocation": 1,
            "adjustment_band": 0.15,
        },
    )

    assert signals.iloc[:8].eq(0).all()
    assert signals.iloc[8:].between(0.25, 1).all()
    assert signals.iloc[8:].lt(1).any()

    no_churn = strategy_signals(
        data,
        "volatility-target-trend",
        {
            "trend_period": 3,
            "volatility_period": 2,
            "minimum_allocation": 0.25,
            "maximum_allocation": 1,
            "adjustment_band": 1,
        },
    )
    assert no_churn.iloc[8:].nunique() == 1


def test_volatility_target_does_not_rebalance_on_round_off_volatility() -> None:
    """Constant percentage growth has zero dispersion, not a noisy risk ratio."""

    closes = [
        100.0 * exp(log(2.0) * index / 999)
        for index in range(1_000)
    ]
    data = price_frame(closes, start="2021-01-01")

    signals = strategy_signals(
        data,
        "volatility-target-trend",
        STRATEGIES["volatility-target-trend"].parameters,
    )

    transitions = int(signals.diff().abs().fillna(signals.abs()).gt(0).sum())
    assert transitions == 1


def test_volatility_target_grid_keeps_allocation_bounds_ordered() -> None:
    candidates = parameter_candidates("volatility-target-trend")

    assert {
        "trend_period": 150,
        "volatility_period": 20,
        "minimum_allocation": 0.25,
        "maximum_allocation": 1,
        "adjustment_band": 0.15,
    } in candidates
    assert all(
        candidate["minimum_allocation"] < candidate["maximum_allocation"]
        for candidate in candidates
    )


def test_atr_trend_exits_after_a_close_breaks_the_trailing_distance() -> None:
    data = price_frame([100, 102, 104, 106, 90, 89])

    signals = strategy_signals(
        data,
        "atr-trend",
        {"fast": 2, "slow": 3, "atr_period": 2, "atr_multiplier": 1},
    )

    assert signals.iloc[2:4].eq(1).all()
    assert signals.iloc[4:].eq(0).all()


def test_atr_trend_parameter_grid_keeps_trend_windows_valid() -> None:
    candidates = parameter_candidates("atr-trend")

    assert {"fast": 20, "slow": 60, "atr_period": 14, "atr_multiplier": 3} in candidates
    assert all(candidate["fast"] < candidate["slow"] for candidate in candidates)


def test_breakout_atr_exits_after_a_trailing_break() -> None:
    data = price_frame([100, 100, 100, 100, 110, 112, 90, 89])

    signals = strategy_signals(
        data,
        "breakout-atr",
        {
            "entry_period": 3,
            "exit_period": 2,
            "atr_period": 2,
            "atr_multiplier": 1,
        },
    )

    assert signals.iloc[4:6].eq(1).all()
    assert signals.iloc[6:].eq(0).all()


def test_breakout_atr_parameter_grid_keeps_channel_order_valid() -> None:
    candidates = parameter_candidates("breakout-atr")

    assert {
        "entry_period": 55,
        "exit_period": 20,
        "atr_period": 14,
        "atr_multiplier": 3,
    } in candidates
    assert all(candidate["exit_period"] < candidate["entry_period"] for candidate in candidates)


def test_rsi_regime_atr_uses_completed_bar_defence_after_a_pullback_entry() -> None:
    data = price_frame([100 + index for index in range(30)] + [129, 128, 125])

    signals = strategy_signals(
        data,
        "rsi-regime-atr",
        {
            "rsi_period": 2,
            "oversold": 40,
            "exit_rsi": 55,
            "regime": 10,
            "atr_period": 2,
            "atr_multiplier": 1,
        },
    )

    assert signals.iloc[30] == 0
    assert signals.iloc[31] == 1
    assert signals.iloc[32] == 0


def test_rsi_regime_atr_parameter_grid_keeps_rsi_thresholds_ordered() -> None:
    candidates = parameter_candidates("rsi-regime-atr")

    assert {
        "rsi_period": 14,
        "oversold": 30,
        "exit_rsi": 55,
        "regime": 150,
        "atr_period": 14,
        "atr_multiplier": 2,
    } in candidates
    assert all(candidate["oversold"] < candidate["exit_rsi"] for candidate in candidates)


def test_constant_allocation_returns_requested_fraction() -> None:
    data = price_frame([100, 101, 102])

    signals = strategy_signals(
        data,
        "constant-allocation",
        {"allocation": 0.37},
    )

    assert signals.eq(0.37).all()


def test_constant_allocation_rejects_out_of_range_fraction() -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        strategy_signals(
            price_frame([100, 101]),
            "constant-allocation",
            {"allocation": 1.2},
        )


@pytest.mark.parametrize(
    ("strategy_id", "parameters", "message"),
    [
        ("sma-cross", {"fast": 60, "slow": 20}, "fast must be shorter"),
        ("rsi", {"oversold": 70, "overbought": 30}, "oversold must be below"),
        ("breakout", {"entry_period": 20, "exit_period": 20}, "exit_period"),
        (
            "volatility-target-trend",
            {"minimum_allocation": 1, "maximum_allocation": 0.75},
            "allocation bounds",
        ),
        ("macd", {"fast": 12.5}, "whole-number"),
        ("buy-hold", {"entry_peroid": 20}, "does not accept parameter"),
    ],
)
def test_manual_strategy_parameters_reject_invalid_or_unknown_values(
    strategy_id: str,
    parameters: dict[str, float],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        strategy_signals(price_frame([100 + index for index in range(80)]), strategy_id, parameters)


def test_warmup_uses_the_same_manual_parameter_validation() -> None:
    with pytest.raises(ValueError, match="fast must be shorter"):
        strategy_warmup_bars("ema-cross", {"fast": 30, "slow": 20})


@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
def test_strategy_signals_are_prefix_invariant(strategy_id: str) -> None:
    """A completed bar must never be revised by data that arrives later.

    Walk-forward re-optimization calculates a strategy over a historical
    training prefix and then uses the same strategy on a later validation
    window.  This guard catches accidental centred indicators, backfills, or
    other future-data dependencies in every built-in template.
    """
    closes = [
        100 + index * 0.12 + ((index % 23) - 11) * 0.7 + (index % 7) * 0.2
        for index in range(420)
    ]
    data = price_frame(closes)
    parameters = STRATEGIES[strategy_id].parameters
    full_signals = strategy_signals(data, strategy_id, parameters)

    for prefix_length in (240, 360):
        prefix_signals = strategy_signals(
            data.iloc[:prefix_length],
            strategy_id,
            parameters,
        )
        pd.testing.assert_series_equal(
            prefix_signals,
            full_signals.iloc[:prefix_length],
        )


def test_default_templates_are_not_signal_identical_across_mixed_regimes() -> None:
    """A template catalogue must not silently contain renamed duplicates."""
    closes = [
        100
        + index * 0.07
        + 12 * sin(index / 18)
        + 6 * sin(index / 5)
        + (-0.22 * (index - 260) if 260 < index < 370 else 0)
        + (0.16 * (index - 480) if index > 480 else 0)
        for index in range(720)
    ]
    data = price_frame(closes)
    data["volume"] = [1_000_000 + 180_000 * sin(index / 7) for index in range(len(data))]
    signals = {
        strategy_id: strategy_signals(data, strategy_id, definition.parameters)
        for strategy_id, definition in STRATEGIES.items()
    }

    identical_pairs = [
        (first, second)
        for index, first in enumerate(signals)
        for second in list(signals)[index + 1 :]
        if signals[first].equals(signals[second])
    ]

    assert identical_pairs == []


@pytest.mark.parametrize("strategy_id", ["macd", "macd-regime"])
def test_macd_templates_do_not_trade_on_floating_point_convergence(
    strategy_id: str,
) -> None:
    """A smooth trend must not create dozens of machine-noise crossovers."""

    closes = [100.0 + 400.0 * index / 1_259 for index in range(1_260)]
    data = price_frame(closes, start="2021-01-01")

    signals = strategy_signals(
        data,
        strategy_id,
        STRATEGIES[strategy_id].parameters,
    )

    transitions = int(signals.diff().abs().fillna(signals.abs()).gt(0).sum())
    assert transitions <= 1
