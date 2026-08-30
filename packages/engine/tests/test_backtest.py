import numpy as np
import pandas as pd
import pytest
from quantsieve_engine import (
    ANNUALIZED_RETURN_CAP,
    BacktestConfig,
    annualize_total_return,
    evaluation_duration_years,
    get_strategy,
    run_backtest,
    run_exposure_matched_benchmark,
    run_risk_budgeted_benchmark,
    strategy_cycle_baseline_signals,
    strategy_signals,
)


def sample_data(periods: int = 120) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=periods, freq="B")
    close = pd.Series(np.linspace(100, 150, periods), index=index)
    return pd.DataFrame(
        {
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": 1_000_000,
        },
        index=index,
    )


def test_backtest_config_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        BacktestConfig.model_validate({"commission_rate": 0})


def test_buy_and_hold_produces_positive_return() -> None:
    data = sample_data()
    _, strategy = get_strategy("buy-hold")

    result = run_backtest(
        data,
        strategy(data, {}),
        BacktestConfig(fee_rate=0, slippage_rate=0),
    )

    assert result.metrics.total_return > 0.45
    assert result.metrics.max_drawdown <= 0
    assert result.metrics.trades == 1
    assert result.metrics.exposure_ratio > 0.95
    assert result.metrics.max_cash_streak == 1


def test_max_drawdown_includes_a_loss_on_the_first_evaluated_bar() -> None:
    index = pd.date_range("2025-01-01", periods=3, freq="D")
    data = pd.DataFrame(
        {
            "open": [100.0, 50.0, 50.0],
            "high": [100.0, 50.0, 50.0],
            "low": [50.0, 50.0, 50.0],
            "close": [50.0, 50.0, 50.0],
            "volume": [1_000] * 3,
        },
        index=index,
    )

    result = run_backtest(
        data,
        pd.Series(1.0, index=index),
        BacktestConfig(
            fee_rate=0,
            slippage_rate=0,
            annual_periods=365,
            signal_delay_bars=0,
        ),
    )

    assert result.equity[0]["equity"] == pytest.approx(50_000)
    assert result.metrics.total_return == pytest.approx(-0.5)
    assert result.metrics.max_drawdown == pytest.approx(-0.5)


def test_max_drawdown_includes_first_bar_entry_cost_and_price_loss() -> None:
    index = pd.date_range("2025-01-01", periods=2, freq="D")
    data = pd.DataFrame(
        {
            "open": [100.0, 90.0],
            "high": [100.0, 90.0],
            "low": [90.0, 90.0],
            "close": [90.0, 90.0],
            "volume": [1_000, 1_000],
        },
        index=index,
    )

    result = run_backtest(
        data,
        pd.Series(1.0, index=index),
        BacktestConfig(
            fee_rate=0.01,
            slippage_rate=0.01,
            annual_periods=365,
            signal_delay_bars=0,
        ),
    )

    # The first bar loses 10% and the initial full-allocation transaction costs
    # another 2%; both are below the account's starting high-water mark.
    assert result.equity[0]["equity"] == pytest.approx(88_000)
    assert result.metrics.total_return == pytest.approx(-0.12)
    assert result.metrics.max_drawdown == pytest.approx(-0.12)


def test_cagr_uses_elapsed_calendar_time_for_sparse_datetime_data() -> None:
    config = BacktestConfig(
        fee_rate=0,
        slippage_rate=0,
        annual_periods=252,
        signal_delay_bars=0,
    )

    def result_for(index: pd.DatetimeIndex):
        data = pd.DataFrame(
            {
                "open": [100.0, 110.0],
                "high": [100.0, 110.0],
                "low": [100.0, 110.0],
                "close": [100.0, 110.0],
                "volume": [1_000, 1_000],
            },
            index=index,
        )
        return run_backtest(data, pd.Series(1.0, index=index), config)

    one_day = result_for(pd.to_datetime(["2025-01-02", "2025-01-03"]))
    one_year = result_for(pd.to_datetime(["2025-01-02", "2026-01-02"]))

    assert one_day.metrics.total_return == pytest.approx(0.1)
    assert one_year.metrics.total_return == pytest.approx(0.1)
    assert one_day.metrics.annualized_return > one_year.metrics.annualized_return
    assert one_year.metrics.duration_years == pytest.approx(365 / 365.2425)
    assert one_year.metrics.annualized_return == pytest.approx(0.1, abs=1e-3)
    assert one_year.metrics.annualized_return_capped is False
    assert one_year.metrics.annualized_return_cap == ANNUALIZED_RETURN_CAP


def test_extreme_short_interval_growth_has_explicit_finite_annualized_cap() -> None:
    index = pd.date_range("2025-01-01", periods=2, freq="15min")
    price = pd.Series([100.0, 200.0], index=index)
    data = pd.DataFrame(
        {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1_000,
        },
        index=index,
    )
    config = BacktestConfig(
        fee_rate=0,
        slippage_rate=0,
        annual_periods=35_040,
        bar_interval="15m",
        signal_delay_bars=0,
    )

    active = run_backtest(data, pd.Series(1.0, index=index), config)
    fixed_share = run_exposure_matched_benchmark(data, 1.0, config)

    for result in (active, fixed_share):
        assert result.metrics.total_return == pytest.approx(1)
        assert result.metrics.annualized_return == ANNUALIZED_RETURN_CAP
        assert result.metrics.annualized_return_capped is True
        assert result.metrics.annualized_return_cap == ANNUALIZED_RETURN_CAP
        assert np.isfinite(result.metrics.annualized_return)


def test_near_total_short_interval_loss_annualizes_to_finite_negative_floor() -> None:
    index = pd.date_range("2025-01-01", periods=2, freq="15min")
    price = pd.Series([100.0, 0.000001], index=index)
    data = pd.DataFrame(
        {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1_000,
        },
        index=index,
    )
    config = BacktestConfig(
        fee_rate=0,
        slippage_rate=0,
        annual_periods=35_040,
        bar_interval="15m",
        signal_delay_bars=0,
    )

    active = run_backtest(data, pd.Series(1.0, index=index), config)
    fixed_share = run_exposure_matched_benchmark(data, 1.0, config)

    for result in (active, fixed_share):
        assert -1 < result.metrics.total_return < -0.999999
        assert result.metrics.annualized_return == pytest.approx(-1)
        assert result.metrics.annualized_return_capped is False
        assert np.isfinite(result.metrics.annualized_return)


def test_log_annualization_matches_conventional_compounding() -> None:
    estimate = annualize_total_return(0.21, 2.0)

    assert estimate.value == pytest.approx(0.1)
    assert estimate.capped is False


@pytest.mark.parametrize("invalid_return", [float("inf"), float("nan")])
def test_annualization_rejects_non_finite_total_return(
    invalid_return: float,
) -> None:
    with pytest.raises(ValueError, match="Total return must be finite"):
        annualize_total_return(invalid_return, 1.0)


def test_sparse_window_trade_frequency_uses_elapsed_calendar_time() -> None:
    index = pd.to_datetime(
        ["2025-01-01", "2025-03-01", "2025-09-01", "2026-01-01"]
    )
    price = pd.Series(100.0, index=index)
    data = pd.DataFrame(
        {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1_000,
        },
        index=index,
    )

    result = run_backtest(
        data,
        pd.Series([1.0, 0.0, 1.0, 0.0], index=index),
        BacktestConfig(
            fee_rate=0,
            slippage_rate=0,
            annual_periods=252,
            signal_delay_bars=0,
        ),
    )

    expected_years = 365 / 365.2425
    assert result.metrics.duration_years == pytest.approx(expected_years)
    assert result.metrics.trades == 4
    assert result.metrics.trades_per_year == pytest.approx(4 / expected_years)


@pytest.mark.parametrize(
    ("index", "annual_periods", "expected"),
    [
        (pd.DatetimeIndex(["2025-01-01"]), 252, 1 / 252),
        (pd.Index(["first", "second"]), 252, 2 / 252),
    ],
)
def test_duration_years_falls_back_when_elapsed_time_is_not_measurable(
    index: pd.Index,
    annual_periods: int,
    expected: float,
) -> None:
    assert evaluation_duration_years(index, annual_periods) == pytest.approx(expected)


def test_exposure_matched_benchmark_buys_fixed_fractional_capital_once() -> None:
    data = sample_data()
    full = run_exposure_matched_benchmark(
        data,
        1,
        BacktestConfig(fee_rate=0, slippage_rate=0),
    )
    quarter = run_exposure_matched_benchmark(
        data,
        0.25,
        BacktestConfig(fee_rate=0, slippage_rate=0),
    )

    # The input is an initial allocation. Its marked asset weight then drifts
    # upward with this rising market instead of being reset to 25% every bar.
    assert 0.25 < quarter.metrics.exposure_ratio < 1
    assert 0 < quarter.metrics.total_return < full.metrics.total_return
    assert abs(quarter.metrics.max_drawdown) <= abs(full.metrics.max_drawdown)
    assert quarter.metrics.trades == 1


def test_fixed_allocation_does_not_create_free_rebalancing_return() -> None:
    index = pd.date_range("2025-01-01", periods=2, freq="D")
    data = pd.DataFrame(
        {
            "open": [100.0, 200.0],
            "high": [200.0, 200.0],
            "low": [100.0, 100.0],
            "close": [200.0, 100.0],
            "volume": [1_000, 1_000],
        },
        index=index,
    )

    result = run_exposure_matched_benchmark(
        data,
        0.5,
        BacktestConfig(fee_rate=0, slippage_rate=0, annual_periods=365),
    )

    # The asset doubles and then halves back to its entry price. A one-time 50%
    # purchase therefore ends flat; a free per-bar rebalance would report 12.5%.
    assert result.metrics.total_return == pytest.approx(0)
    assert result.metrics.exposure_ratio == pytest.approx((2 / 3 + 1 / 2) / 2)
    assert result.metrics.trades == 1
    assert len(result.trades) == 1
    assert result.trades[0]["closed"] is False
    assert len(result.position_cycles) == 1
    assert result.position_cycles[0]["closed"] is False
    assert result.position_cycles[0]["return"] == pytest.approx(0)
    assert result.metrics.average_holding_bars == 0


def test_fixed_allocation_carries_shares_across_evaluation_boundary() -> None:
    index = pd.date_range("2025-01-01", periods=3, freq="D")
    data = pd.DataFrame(
        {
            "open": [100.0, 200.0, 100.0],
            "high": [100.0, 200.0, 100.0],
            "low": [100.0, 200.0, 100.0],
            "close": [100.0, 200.0, 100.0],
            "volume": [1_000] * 3,
        },
        index=index,
    )

    result = run_exposure_matched_benchmark(
        data,
        0.5,
        BacktestConfig(fee_rate=0, slippage_rate=0, annual_periods=365),
        evaluation_start=1,
    )

    # Five shares and 500 cash were established at the context's first open.
    # The holdout therefore rises from 1,000 to 1,500 and then returns to 1,000.
    assert result.equity[0]["equity"] == pytest.approx(150_000)
    assert result.equity[-1]["equity"] == pytest.approx(100_000)
    assert result.metrics.total_return == pytest.approx(0)
    assert result.metrics.exposure_ratio == pytest.approx((2 / 3 + 1 / 2) / 2)
    assert result.metrics.trades == 0
    assert result.metrics.closed_trades == 0
    assert result.trades == []
    assert result.position_cycles == []


def test_fixed_allocation_charges_initial_purchase_cost_once() -> None:
    index = pd.date_range("2025-01-01", periods=2, freq="D")
    price = pd.Series(100.0, index=index)
    data = pd.DataFrame(
        {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1_000,
        },
        index=index,
    )
    config = BacktestConfig(
        fee_rate=0.01,
        slippage_rate=0.01,
        annual_periods=365,
    )

    full = run_exposure_matched_benchmark(data, 0.5, config)
    carried = run_exposure_matched_benchmark(
        data,
        0.5,
        config,
        evaluation_start=1,
    )

    assert full.metrics.total_return == pytest.approx(-0.01)
    assert full.metrics.max_drawdown == pytest.approx(-0.01)
    assert full.metrics.trades == 1
    assert full.trades[0]["entry_price"] == pytest.approx(100 / 0.98)
    # The purchase happened before this reported slice, so no second cost or
    # fictitious event appears at the holdout boundary.
    assert carried.metrics.total_return == pytest.approx(0)
    assert carried.metrics.trades == 0
    assert carried.trades == []


def test_exposure_matched_benchmark_rejects_invalid_capital_ratio() -> None:
    with pytest.raises(ValueError, match="ratio between 0 and 1"):
        run_exposure_matched_benchmark(sample_data(), 1.01)


def test_risk_budgeted_benchmark_reduces_exposure_to_honor_drawdown() -> None:
    index = pd.date_range("2025-01-01", periods=20, freq="D")
    price = pd.Series(
        [100.0] * 5 + [120.0] * 5 + [60.0] * 5 + [80.0] * 5,
        index=index,
    )
    data = pd.DataFrame(
        {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1_000,
        },
        index=index,
    )

    calibrated = run_risk_budgeted_benchmark(
        data,
        0.2,
        BacktestConfig(fee_rate=0, slippage_rate=0, annual_periods=365),
    )

    assert 0 < calibrated.target_exposure < 1
    assert calibrated.cash_reserve_ratio == pytest.approx(
        1 - calibrated.target_exposure
    )
    assert calibrated.budget_satisfied is True
    assert abs(calibrated.result.metrics.max_drawdown) <= 0.2 + 1e-9
    assert calibrated.target_exposure * 10_000 == pytest.approx(
        round(calibrated.target_exposure * 10_000)
    )


def test_risk_budgeted_benchmark_counts_first_bar_loss_against_budget() -> None:
    index = pd.date_range("2025-01-01", periods=3, freq="D")
    data = pd.DataFrame(
        {
            "open": [100.0, 50.0, 50.0],
            "high": [100.0, 50.0, 50.0],
            "low": [50.0, 50.0, 50.0],
            "close": [50.0, 50.0, 50.0],
            "volume": [1_000] * 3,
        },
        index=index,
    )

    calibrated = run_risk_budgeted_benchmark(
        data,
        0.2,
        BacktestConfig(fee_rate=0, slippage_rate=0, annual_periods=365),
    )

    # The benchmark deliberately rounds exposure down to four decimals so a
    # floating-point boundary can never round upward through the risk budget.
    assert calibrated.target_exposure == pytest.approx(0.3999)
    assert calibrated.target_exposure < 1
    assert calibrated.budget_satisfied is True
    assert abs(calibrated.result.metrics.max_drawdown) == pytest.approx(
        0.2,
        abs=1e-4,
    )


@pytest.mark.parametrize("budget", [0, 1.01])
def test_risk_budgeted_benchmark_rejects_invalid_budget(budget: float) -> None:
    with pytest.raises(ValueError, match="budget must be between 0 and 1"):
        run_risk_budgeted_benchmark(sample_data(), budget)


def test_all_strategy_templates_return_aligned_signals() -> None:
    data = sample_data()
    from quantsieve_engine import STRATEGIES

    for strategy_id, definition in STRATEGIES.items():
        _, function = get_strategy(strategy_id)
        signals = function(data, definition.parameters)
        assert signals.index.equals(data.index)
        assert signals.dropna().between(0, 1).all()


def test_next_bar_open_execution_does_not_capture_pre_entry_gap() -> None:
    index = pd.date_range("2025-01-01", periods=2, freq="D")
    data = pd.DataFrame(
        {
            "open": [100.0, 200.0],
            "high": [101.0, 221.0],
            "low": [99.0, 199.0],
            "close": [100.0, 220.0],
            "volume": [1_000, 1_000],
        },
        index=index,
    )
    signals = pd.Series([1.0, 1.0], index=index)

    result = run_backtest(
        data,
        signals,
        BacktestConfig(fee_rate=0, slippage_rate=0, annual_periods=365),
    )

    assert result.metrics.total_return == pytest.approx(0.10)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("open", 0.0, "Open prices must be positive"),
        ("high", np.inf, "High prices must be finite"),
        ("volume", -1.0, "Volume must not be negative"),
    ],
)
def test_backtest_rejects_invalid_ohlcv_values(
    column: str,
    value: float,
    message: str,
) -> None:
    data = sample_data(4)
    data.loc[data.index[1], column] = value
    signals = pd.Series(0.0, index=data.index)

    with pytest.raises(ValueError, match=message):
        run_backtest(data, signals)


def test_backtest_rejects_ohlc_range_that_cannot_exist() -> None:
    data = sample_data(4)
    data.loc[data.index[1], "high"] = data.loc[data.index[1], "low"] - 1
    signals = pd.Series(0.0, index=data.index)

    with pytest.raises(ValueError, match="High prices must not be below low prices"):
        run_backtest(data, signals)


def test_backtest_rejects_duplicate_ohlcv_timestamps() -> None:
    data = sample_data(4)
    data.index = pd.DatetimeIndex([data.index[0], data.index[0], *data.index[2:]])
    signals = pd.Series(0.0, index=data.index)

    with pytest.raises(ValueError, match="OHLCV timestamps must be unique"):
        run_backtest(data, signals)


def test_validation_window_carries_position_and_overnight_state_from_development() -> None:
    index = pd.date_range("2025-01-01", periods=3, freq="D")
    data = pd.DataFrame(
        {
            "open": [100.0, 100.0, 200.0],
            "high": [100.0, 100.0, 200.0],
            "low": [100.0, 100.0, 200.0],
            "close": [100.0, 100.0, 200.0],
            "volume": [1_000] * 3,
        },
        index=index,
    )
    signals = pd.Series([1.0, 1.0, 1.0], index=index)
    config = BacktestConfig(fee_rate=0, slippage_rate=0, annual_periods=365)

    carried = run_backtest(data, signals, config, evaluation_start=2)
    reset = run_backtest(data.iloc[2:], signals.iloc[2:], config)

    # The position was already decided on the preceding completed bar.  The
    # validation period must therefore include the first holdout overnight gap
    # rather than silently resetting the account to cash at the split.
    assert carried.metrics.total_return == pytest.approx(1.0)
    assert carried.equity[0]["position"] == pytest.approx(1.0)
    assert carried.metrics.trades == 0
    assert reset.metrics.total_return == pytest.approx(0.0)


def test_validation_window_counts_carried_position_exit_as_a_window_execution() -> None:
    index = pd.date_range("2025-01-01", periods=4, freq="D")
    price = pd.Series([100.0, 100.0, 100.0, 100.0], index=index)
    data = pd.DataFrame(
        {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1_000,
        },
        index=index,
    )
    signals = pd.Series([1.0, 1.0, 0.0, 0.0], index=index)

    result = run_backtest(
        data,
        signals,
        BacktestConfig(fee_rate=0, slippage_rate=0, annual_periods=365),
        evaluation_start=3,
    )

    # The entry occurred before this window, but the next-open exit occurred
    # inside it and must therefore count toward the window's trading frequency.
    assert result.metrics.trades == 1
    assert result.metrics.closed_trades == 0
    assert result.position_cycles == []


def test_execution_event_frequency_is_identical_with_or_without_prior_context() -> None:
    index = pd.date_range("2025-01-01", periods=5, freq="D")
    price = pd.Series(100.0, index=index)
    data = pd.DataFrame(
        {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1_000,
        },
        index=index,
    )
    config = BacktestConfig(
        fee_rate=0,
        slippage_rate=0,
        annual_periods=365,
        signal_delay_bars=0,
    )

    contextual = run_backtest(
        data,
        pd.Series([0.0, 1.0, 0.0, 1.0, 0.0], index=index),
        config,
        evaluation_start=1,
    )
    isolated = run_backtest(
        data.iloc[1:],
        pd.Series([1.0, 0.0, 1.0, 0.0], index=index[1:]),
        config,
    )

    # Both evaluated windows execute the same entry, exit, entry, exit sequence.
    # Prior context must not change the definition or annualization of a trade.
    assert contextual.metrics.trades == isolated.metrics.trades == 4
    assert contextual.metrics.closed_trades == isolated.metrics.closed_trades == 2
    assert len(contextual.position_cycles) == len(isolated.position_cycles) == 2
    assert all(cycle["closed"] for cycle in contextual.position_cycles)
    assert all(cycle["closed"] for cycle in isolated.position_cycles)
    assert contextual.metrics.duration_years == pytest.approx(
        isolated.metrics.duration_years
    )
    assert contextual.metrics.trades_per_year == pytest.approx(
        isolated.metrics.trades_per_year
    )
    assert contextual.metrics.trades_per_year == pytest.approx(
        4 / (3 / 365.2425)
    )


def test_win_rate_is_calculated_from_closed_trades() -> None:
    index = pd.date_range("2025-01-01", periods=4, freq="D")
    data = pd.DataFrame(
        {
            "open": [100.0, 110.0, 100.0, 90.0],
            "high": [111.0, 111.0, 101.0, 91.0],
            "low": [99.0, 109.0, 89.0, 89.0],
            "close": [105.0, 110.0, 95.0, 90.0],
            "volume": [1_000] * 4,
        },
        index=index,
    )
    signals = pd.Series([1.0, 0.0, 1.0, 0.0], index=index)

    result = run_backtest(
        data,
        signals,
        BacktestConfig(
            fee_rate=0,
            slippage_rate=0,
            annual_periods=365,
            signal_delay_bars=0,
        ),
    )

    assert result.metrics.closed_trades == 2
    assert result.metrics.win_rate == pytest.approx(0.5)
    assert result.metrics.average_holding_bars == pytest.approx(1.0)
    assert len(result.position_cycles) == 2
    assert all(cycle["closed"] for cycle in result.position_cycles)
    assert {
        cycle["cycle_kind"] for cycle in result.position_cycles
    } == {"flat_to_flat"}
    assert {
        cycle["return_semantics"] for cycle in result.position_cycles
    } == {"compounded_strategy_return"}


def test_fractional_allocation_tracks_satellite_lots_without_duplicating_core() -> None:
    index = pd.date_range("2025-01-01", periods=5, freq="D")
    price = pd.Series([100.0, 101.0, 110.0, 111.0, 120.0], index=index)
    data = pd.DataFrame(
        {
            "open": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": 1_000,
        },
        index=index,
    )
    signals = pd.Series([0.25, 1.0, 0.25, 1.0, 0.25], index=index)

    result = run_backtest(
        data,
        signals,
        BacktestConfig(
            fee_rate=0,
            slippage_rate=0,
            annual_periods=365,
            signal_delay_bars=0,
        ),
    )

    assert result.metrics.trades == 5
    # Lot attribution still closes two satellite allocations, but the core
    # position never returns to cash, so there is no independent closed sample.
    assert result.metrics.closed_trades == 0
    assert result.metrics.exposure_ratio == pytest.approx(0.55)
    assert [trade["position_size"] for trade in result.trades] == pytest.approx(
        [0.25, 0.75, 0.75]
    )
    assert result.trades[0]["closed"] is False
    assert all(trade["closed"] for trade in result.trades[1:])
    assert len(result.position_cycles) == 1
    assert result.position_cycles[0]["closed"] is False
    assert result.metrics.average_holding_bars == 0


def test_gradual_allocation_and_one_exit_is_one_closed_position_cycle() -> None:
    index = pd.date_range("2025-01-01", periods=11, freq="D")
    price = pd.Series(100.0, index=index)
    data = pd.DataFrame(
        {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1_000,
        },
        index=index,
    )
    signals = pd.Series(
        [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 0.0],
        index=index,
    )

    result = run_backtest(
        data,
        signals,
        BacktestConfig(
            fee_rate=0.001,
            slippage_rate=0,
            annual_periods=365,
            signal_delay_bars=0,
        ),
    )

    expected_cycle_return = (1 - 0.0001) ** 10 * (1 - 0.001) - 1
    assert result.metrics.trades == 11
    assert len(result.trades) == 10
    assert all(trade["closed"] for trade in result.trades)
    assert result.metrics.closed_trades == 1
    assert len(result.position_cycles) == 1
    assert result.position_cycles[0]["closed"] is True
    assert result.position_cycles[0]["holding_bars"] == 10
    assert result.position_cycles[0]["return"] == pytest.approx(
        expected_cycle_return
    )
    assert result.position_cycles[0]["return"] == pytest.approx(
        result.metrics.total_return
    )
    assert result.metrics.average_holding_bars == pytest.approx(10)


def test_satellite_cycle_return_is_relative_to_explicit_core_baseline() -> None:
    index = pd.date_range("2025-01-01", periods=3, freq="D")
    price = pd.Series([100.0, 200.0, 100.0], index=index)
    data = pd.DataFrame(
        {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1_000,
        },
        index=index,
    )
    core_signals = pd.Series(0.35, index=index)
    strategy_signals_with_satellite = pd.Series([1.0, 1.0, 0.35], index=index)
    config = BacktestConfig(
        fee_rate=0,
        slippage_rate=0,
        annual_periods=365,
        signal_delay_bars=0,
    )

    result = run_backtest(
        data,
        strategy_signals_with_satellite,
        config,
        cycle_baseline_signals=core_signals,
    )
    core_only = run_backtest(data, core_signals, config)

    expected_relative_return = (
        (1 + result.metrics.total_return) / (1 + core_only.metrics.total_return) - 1
    )
    assert result.metrics.closed_trades == 1
    assert len(result.position_cycles) == 1
    cycle = result.position_cycles[0]
    assert cycle["cycle_kind"] == "satellite_over_core"
    assert cycle["return_semantics"] == "compounded_relative_to_core"
    assert cycle["return"] == pytest.approx(expected_relative_return)
    assert cycle["return"] != pytest.approx(result.metrics.total_return)


def test_validation_excludes_satellite_cycle_carried_across_boundary() -> None:
    index = pd.date_range("2025-01-01", periods=4, freq="D")
    price = pd.Series(100.0, index=index)
    data = pd.DataFrame(
        {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1_000,
        },
        index=index,
    )
    core_signals = pd.Series(0.35, index=index)

    result = run_backtest(
        data,
        pd.Series([1.0, 1.0, 0.35, 0.35], index=index),
        BacktestConfig(
            fee_rate=0,
            slippage_rate=0,
            annual_periods=365,
            signal_delay_bars=0,
        ),
        evaluation_start=2,
        cycle_baseline_signals=core_signals,
    )

    assert result.metrics.trades == 1
    assert result.metrics.closed_trades == 0
    assert result.position_cycles == []


def test_core_trend_crossings_create_closed_satellite_cycles() -> None:
    index = pd.date_range("2025-01-01", periods=11, freq="D")
    price = pd.Series(
        [100.0, 100.0, 100.0, 120.0, 80.0, 120.0, 80.0, 120.0, 80.0, 120.0, 80.0],
        index=index,
    )
    data = pd.DataFrame(
        {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1_000,
        },
        index=index,
    )
    parameters = {"period": 3, "defensive_exposure": 0.35}
    signals = strategy_signals(data, "core-trend-allocation", parameters)
    cycle_baseline = strategy_cycle_baseline_signals(
        "core-trend-allocation",
        signals,
        parameters,
    )

    assert cycle_baseline is not None
    result = run_backtest(
        data,
        signals,
        BacktestConfig(
            fee_rate=0,
            slippage_rate=0,
            annual_periods=365,
            signal_delay_bars=0,
        ),
        cycle_baseline_signals=cycle_baseline,
    )

    assert result.metrics.closed_trades == 4
    assert len(result.position_cycles) == 4
    assert all(cycle["closed"] for cycle in result.position_cycles)
    assert {
        cycle["cycle_kind"] for cycle in result.position_cycles
    } == {"satellite_over_core"}
    assert {
        cycle["return_semantics"] for cycle in result.position_cycles
    } == {"compounded_relative_to_core"}
