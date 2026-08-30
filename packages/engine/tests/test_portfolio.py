from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from quantsieve_engine import (
    ANNUALIZED_RETURN_CAP,
    BacktestConfig,
    run_portfolio_backtest,
)
from quantsieve_engine.portfolio import _cap_weights


def asset(prices: list[float]) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=len(prices), freq="B")
    close = pd.Series(prices, index=index)
    return pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close,
            "low": close,
            "close": close,
            "volume": 1_000,
        },
        index=index,
    )


def test_initial_equal_portfolio_invests_once_and_reports_weights() -> None:
    result = run_portfolio_backtest(
        {
            "UP": asset(list(np.linspace(100, 120, 20))),
            "FLAT": asset([100.0] * 20),
        },
        "initial_equal_hold",
        BacktestConfig(fee_rate=0, slippage_rate=0),
    )

    assert result.metrics.total_return > 0
    assert result.metrics.rebalances == 1
    assert result.metrics.turnover_ratio == pytest.approx(1)
    assert result.last_rebalance_target_weights == pytest.approx(
        {"UP": 0.5, "FLAT": 0.5}
    )
    assert result.ending_realized_weights == pytest.approx(
        {"UP": 6 / 11, "FLAT": 5 / 11}
    )
    assert result.latest_weights == result.ending_realized_weights
    assert len(result.equity) == 20
    assert result.allocations[0]["weights"] == pytest.approx(
        {"UP": 0.5, "FLAT": 0.5}
    )


def test_periodic_equal_portfolio_rebalances_on_requested_schedule() -> None:
    result = run_portfolio_backtest(
        {
            "UP": asset(list(np.linspace(100, 140, 21))),
            "DOWN": asset(list(np.linspace(100, 80, 21))),
        },
        "periodic_equal",
        BacktestConfig(fee_rate=0, slippage_rate=0),
        rebalance_bars=5,
    )

    assert result.metrics.rebalances == 5
    assert len(result.allocations) == 5


def test_inverse_volatility_uses_pre_evaluation_history_and_honors_cap() -> None:
    index = pd.date_range("2024-01-01", periods=100, freq="B")
    assets: dict[str, pd.DataFrame] = {}
    for position, symbol in enumerate(("A", "B", "C", "D"), start=1):
        changes = np.sin(np.arange(100) / (position + 1)) * position / 100
        close = pd.Series(100 * np.cumprod(1 + changes), index=index)
        assets[symbol] = pd.DataFrame(
            {"open": close, "close": close},
            index=index,
        )

    result = run_portfolio_backtest(
        assets,
        "periodic_inverse_volatility",
        BacktestConfig(fee_rate=0, slippage_rate=0),
        evaluation_start=index[70],
        volatility_lookback=60,
        rebalance_bars=10,
        maximum_asset_weight=0.4,
    )

    assert result.metrics.bars == 30
    assert result.metrics.rebalances == 3
    assert sum(result.last_rebalance_target_weights.values()) == pytest.approx(1)
    assert max(result.last_rebalance_target_weights.values()) <= 0.4 + 1e-9
    assert result.latest_weights == result.ending_realized_weights
    for allocation in result.allocations:
        assert sum(allocation["weights"].values()) == pytest.approx(1)
        assert max(allocation["weights"].values()) <= 0.4 + 1e-9


def test_portfolio_rejects_an_impossible_weight_cap() -> None:
    with pytest.raises(ValueError, match="too small"):
        run_portfolio_backtest(
            {"A": asset([100, 101]), "B": asset([100, 101])},
            "periodic_inverse_volatility",
            maximum_asset_weight=0.4,
        )


def test_weight_cap_water_filling_does_not_reopen_capped_assets() -> None:
    weights = _cap_weights(
        pd.Series({"A": 1.0, "B": 4.0, "C": 0.01}),
        0.4,
    )

    assert weights.to_dict() == pytest.approx(
        {"A": 0.4, "B": 0.4, "C": 0.2}
    )


def test_weight_cap_randomized_constraints() -> None:
    random = np.random.default_rng(20260726)
    for asset_count in range(2, 9):
        for iteration in range(150):
            cap = (
                1 / asset_count
                if iteration % 10 == 0
                else float(random.uniform(1 / asset_count, 1))
            )
            preferences = np.exp(random.uniform(-20, 20, asset_count))
            preferences[random.random(asset_count) < 0.15] = 0
            weights = _cap_weights(pd.Series(preferences), cap)

            assert np.isfinite(weights.to_numpy()).all()
            assert (weights >= 0).all()
            assert float(weights.sum()) == pytest.approx(1, abs=1e-10)
            assert float(weights.max()) <= cap + 1e-12


@pytest.mark.parametrize(
    ("column", "invalid_value"),
    [
        ("open", np.nan),
        ("open", np.inf),
        ("close", -np.inf),
        ("close", np.nan),
    ],
)
def test_portfolio_rejects_non_finite_prices(
    column: str,
    invalid_value: float,
) -> None:
    invalid = asset([100, 101, 102])
    invalid[column] = invalid[column].astype(float)
    invalid.loc[invalid.index[1], column] = invalid_value

    with pytest.raises(ValueError, match="finite"):
        run_portfolio_backtest(
            {"INVALID": invalid, "VALID": asset([100, 101, 102])},
            "initial_equal_hold",
        )


def test_portfolio_rejects_duplicate_non_monotonic_and_non_datetime_indices() -> None:
    duplicate = asset([100, 101, 102])
    duplicate.index = pd.DatetimeIndex(
        ["2025-01-01", "2025-01-01", "2025-01-03"]
    )
    with pytest.raises(ValueError, match="duplicate"):
        run_portfolio_backtest(
            {"INVALID": duplicate, "VALID": asset([100, 101, 102])},
            "initial_equal_hold",
        )

    non_monotonic = asset([100, 101, 102]).iloc[[1, 0, 2]]
    with pytest.raises(ValueError, match="strictly increasing"):
        run_portfolio_backtest(
            {"INVALID": non_monotonic, "VALID": asset([100, 101, 102])},
            "initial_equal_hold",
        )

    non_datetime = asset([100, 101, 102]).reset_index(drop=True)
    with pytest.raises(ValueError, match="DatetimeIndex"):
        run_portfolio_backtest(
            {"INVALID": non_datetime, "VALID": asset([100, 101, 102])},
            "initial_equal_hold",
        )


def test_portfolio_rejects_two_rows_for_the_same_utc_calendar_date() -> None:
    duplicate_session = asset([100, 101])
    duplicate_session.index = pd.DatetimeIndex(
        ["2025-01-01T00:00:00Z", "2025-01-01T05:00:00Z"]
    )

    with pytest.raises(ValueError, match="UTC calendar date"):
        run_portfolio_backtest(
            {"INVALID": duplicate_session, "VALID": asset([100, 101])},
            "initial_equal_hold",
        )


def test_daily_session_labels_align_after_utc_calendar_normalization() -> None:
    utc_asset = asset([100, 101, 102])
    utc_asset.index = pd.date_range(
        "2025-01-02",
        periods=3,
        freq="B",
        tz="UTC",
    )
    new_york_asset = asset([100, 101, 102])
    new_york_asset.index = pd.date_range(
        "2025-01-02",
        periods=3,
        freq="B",
        tz="America/New_York",
    )

    result = run_portfolio_backtest(
        {"UTC": utc_asset, "NEW_YORK": new_york_asset},
        "initial_equal_hold",
        BacktestConfig(fee_rate=0, slippage_rate=0),
    )

    assert result.metrics.bars == 3
    assert [row["date"] for row in result.equity] == [
        "2025-01-02T00:00:00+00:00",
        "2025-01-03T00:00:00+00:00",
        "2025-01-06T00:00:00+00:00",
    ]


def test_max_drawdown_includes_initial_cash_high_water_mark() -> None:
    index = pd.DatetimeIndex(["2025-01-02"])
    crash = pd.DataFrame({"open": [100.0], "close": [50.0]}, index=index)

    result = run_portfolio_backtest(
        {"A": crash, "B": crash.copy()},
        "initial_equal_hold",
        BacktestConfig(fee_rate=0, slippage_rate=0),
    )

    assert result.metrics.total_return == pytest.approx(-0.5)
    assert result.metrics.max_drawdown == pytest.approx(-0.5)


def test_ending_realized_weights_capture_price_drift() -> None:
    result = run_portfolio_backtest(
        {
            "UP": asset([100, 200]),
            "FLAT": asset([100, 100]),
        },
        "initial_equal_hold",
        BacktestConfig(fee_rate=0, slippage_rate=0),
    )

    assert result.last_rebalance_target_weights == pytest.approx(
        {"UP": 0.5, "FLAT": 0.5}
    )
    assert result.ending_realized_weights == pytest.approx(
        {"UP": 2 / 3, "FLAT": 1 / 3}
    )
    assert result.latest_weights == result.ending_realized_weights


def test_duration_and_cagr_use_elapsed_calendar_time() -> None:
    index = pd.date_range("2025-01-01", periods=365, freq="D", tz="UTC")
    opens = pd.Series(100 * 1.001 ** np.arange(365), index=index)
    closes = opens * 1.001
    daily_asset = pd.DataFrame({"open": opens, "close": closes}, index=index)

    result = run_portfolio_backtest(
        {"A": daily_asset, "B": daily_asset.copy()},
        "initial_equal_hold",
        BacktestConfig(fee_rate=0, slippage_rate=0, annual_periods=252),
    )

    expected_years = 364 / 365.2425
    expected_return = 1.001**365 - 1
    assert result.metrics.duration_years == pytest.approx(expected_years)
    assert result.metrics.total_return == pytest.approx(expected_return)
    assert result.metrics.annualized_return == pytest.approx(
        (1 + expected_return) ** (1 / expected_years) - 1
    )
    assert result.metrics.annualized_return_capped is False


def test_short_calendar_portfolio_growth_has_explicit_annualized_cap() -> None:
    index = pd.date_range("2025-01-01", periods=2, freq="D", tz="UTC")
    price = pd.Series([100.0, 200.0], index=index)
    fast_asset = pd.DataFrame({"open": price, "close": price}, index=index)

    result = run_portfolio_backtest(
        {"A": fast_asset, "B": fast_asset.copy()},
        "initial_equal_hold",
        BacktestConfig(
            fee_rate=0,
            slippage_rate=0,
            annual_periods=252,
        ),
    )

    assert result.metrics.total_return == pytest.approx(1)
    assert result.metrics.annualized_return == ANNUALIZED_RETURN_CAP
    assert result.metrics.annualized_return_capped is True
    assert result.metrics.annualized_return_cap == ANNUALIZED_RETURN_CAP
