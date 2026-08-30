from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from quantsieve_engine import (
    BacktestConfig,
    PortfolioForwardAdvance,
    PortfolioForwardExecution,
    PortfolioForwardState,
    PortfolioForwardTarget,
    advance_portfolio_session,
    compute_next_portfolio_target,
    initialize_portfolio_forward_state,
    queue_portfolio_target,
    run_portfolio_backtest,
)


def varied_close_history(
    *,
    periods: int = 100,
    symbols: tuple[str, ...] = ("A", "B", "C", "D"),
) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=periods, freq="B")
    values: dict[str, np.ndarray] = {}
    observations = np.arange(periods)
    for position, symbol in enumerate(symbols, start=1):
        changes = (
            np.sin(observations / (position + 1)) * position / 200
            + np.cos(observations / (position + 3)) / 500
        )
        values[symbol] = 100 * np.cumprod(1 + changes)
    return pd.DataFrame(values, index=index)


def equal_target(
    session: object,
    *,
    method: str = "periodic_equal",
    symbols: tuple[str, ...] = ("A", "B"),
) -> PortfolioForwardTarget:
    return PortfolioForwardTarget(
        method=method,
        information_session=session,
        weights={symbol: 1 / len(symbols) for symbol in symbols},
    )


def test_initialize_is_full_cash_zero_return_serializable_state() -> None:
    state = initialize_portfolio_forward_state(
        [" btcusdt ", "ethusdt"],
        25_000,
        "periodic_equal",
    )

    assert state.schema_version == 1
    assert state.symbols == ("BTCUSDT", "ETHUSDT")
    assert state.session is None
    assert state.cash == 25_000
    assert state.equity == 25_000
    assert state.total_return == 0
    assert state.peak_equity == 25_000
    assert state.max_drawdown == 0
    assert state.shares == {"BTCUSDT": 0, "ETHUSDT": 0}
    assert state.target_weights == {"BTCUSDT": 0, "ETHUSDT": 0}
    assert state.realized_weights == {"BTCUSDT": 0, "ETHUSDT": 0}
    assert state.last_prices == {}
    assert state.valuation_count == 0
    assert state.rebalance_count == 0
    assert state.turnover_ratio == 0
    assert state.turnover_notional == 0
    assert state.fee_paid == 0
    assert state.slippage_paid == 0
    assert state.total_cost == 0
    assert PortfolioForwardState.model_validate_json(state.model_dump_json()) == state
    with pytest.raises(ValidationError):
        state.cash = 0


@pytest.mark.parametrize("method", ["initial_equal_hold", "periodic_equal"])
def test_equal_target_uses_only_the_finalized_close(method: str) -> None:
    history = varied_close_history(periods=8, symbols=("btc", "eth", "oil"))
    original = history.copy(deep=True)

    target = compute_next_portfolio_target(
        history,
        method,  # type: ignore[arg-type]
        information_session=history.index[4],
    )

    assert target.information_session == "2025-01-07T00:00:00+00:00"
    assert target.weights == pytest.approx(
        {"BTC": 1 / 3, "ETH": 1 / 3, "OIL": 1 / 3}
    )
    pd.testing.assert_frame_equal(history, original)


def test_target_requires_an_explicit_information_session() -> None:
    history = varied_close_history(periods=8, symbols=("BTC", "ETH"))

    with pytest.raises(TypeError, match="information_session"):
        compute_next_portfolio_target(  # type: ignore[call-arg]
            history,
            "periodic_equal",
        )


def test_inverse_volatility_target_matches_lookback_semantics_and_cap() -> None:
    history = varied_close_history(periods=95)
    cutoff = history.index[79]

    target = compute_next_portfolio_target(
        history,
        "periodic_inverse_volatility",
        information_session=cutoff,
        volatility_lookback=20,
        maximum_asset_weight=0.35,
    )
    volatility = (
        history.loc[:cutoff]
        .pct_change(fill_method=None)
        .tail(20)
        .std(ddof=0)
    )
    raw_weights = 1 / volatility

    assert sum(target.weights.values()) == pytest.approx(1)
    assert max(target.weights.values()) <= 0.35 + 1e-12
    uncapped_order = list(raw_weights.sort_values().index)
    target_order = list(pd.Series(target.weights).sort_values().index)
    assert target_order == uncapped_order


def test_future_closes_cannot_change_or_invalidate_a_past_target() -> None:
    history = varied_close_history(periods=100)
    cutoff = history.index[74]
    target = compute_next_portfolio_target(
        history,
        "periodic_inverse_volatility",
        information_session=cutoff,
        volatility_lookback=30,
    )
    changed_future = history.copy(deep=True)
    changed_future.loc[changed_future.index > cutoff, "A"] = np.nan
    changed_future.loc[changed_future.index > cutoff, "B"] = np.inf
    changed_future.loc[changed_future.index > cutoff, "C"] *= 1_000_000
    changed_future.loc[changed_future.index > cutoff, "D"] /= 1_000_000

    changed_target = compute_next_portfolio_target(
        changed_future,
        "periodic_inverse_volatility",
        information_session=cutoff,
        volatility_lookback=30,
    )

    assert changed_target == target


@pytest.mark.parametrize(
    ("history", "match"),
    [
        (
            pd.DataFrame(
                {"A": [100, 101], "B": [100, np.nan]},
                index=pd.date_range("2025-01-01", periods=2),
            ),
            "finite",
        ),
        (
            pd.DataFrame(
                {"A": [100, 101], "B": [100, 101]},
                index=pd.DatetimeIndex(["2025-01-01", "2025-01-01"]),
            ),
            "duplicate",
        ),
        (
            pd.DataFrame(
                {"A": [100, 101], "B": [100, 101]},
                index=pd.DatetimeIndex(["2025-01-02", "2025-01-01"]),
            ),
            "strictly increasing",
        ),
    ],
)
def test_target_rejects_invalid_finalized_history(
    history: pd.DataFrame,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        compute_next_portfolio_target(
            history,
            "periodic_equal",
            information_session=history.index[-1],
        )


def test_inverse_target_rejects_insufficient_and_zero_volatility_history() -> None:
    short = varied_close_history(periods=20, symbols=("A", "B"))
    with pytest.raises(ValueError, match="at least 21"):
        compute_next_portfolio_target(
            short,
            "periodic_inverse_volatility",
            information_session=short.index[-1],
            volatility_lookback=20,
            maximum_asset_weight=0.5,
        )

    flat = pd.DataFrame(
        {"A": [100.0] * 21, "B": [200.0] * 21},
        index=pd.date_range("2025-01-01", periods=21),
    )
    with pytest.raises(ValueError, match="positive volatility"):
        compute_next_portfolio_target(
            flat,
            "periodic_inverse_volatility",
            information_session=flat.index[-1],
            volatility_lookback=20,
            maximum_asset_weight=0.5,
        )


def test_queue_target_returns_a_new_state_and_preserves_input() -> None:
    state = initialize_portfolio_forward_state(
        ["A", "B"],
        10_000,
        "periodic_equal",
        session="2025-01-02",
    )
    target = equal_target("2025-01-02")
    snapshot = state.model_dump(mode="python")

    queued = queue_portfolio_target(state, target)

    assert state.model_dump(mode="python") == snapshot
    assert state.pending_target is None
    assert queued is not state
    assert queued.pending_target == target


def test_advance_executes_at_open_self_financing_and_values_at_close() -> None:
    state = initialize_portfolio_forward_state(
        ["A", "B"],
        1_000,
        "periodic_equal",
        fee_rate=0.001,
        slippage_rate=0.002,
        session="2025-01-02",
    )
    target = equal_target("2025-01-02")
    snapshot = state.model_dump(mode="python")

    result = advance_portfolio_session(
        state,
        session="2025-01-03",
        open_prices={"A": 100, "B": 100},
        close_prices={"A": 110, "B": 90},
        pending_target=target,
    )

    assert state.model_dump(mode="python") == snapshot
    assert result.state.pending_target is None
    assert result.state.session == "2025-01-03T00:00:00+00:00"
    assert result.state.valuation_count == 1
    assert result.state.rebalance_count == 1
    assert len(result.executions) == 2
    assert all(execution.side == "buy" for execution in result.executions)
    traded = sum(execution.traded_notional for execution in result.executions)
    fees = sum(execution.fee for execution in result.executions)
    slippage = sum(execution.slippage for execution in result.executions)
    assert fees == pytest.approx(traded * 0.001)
    assert slippage == pytest.approx(traded * 0.002)
    assert result.state.total_cost == pytest.approx(fees + slippage)
    assert result.state.turnover_notional == pytest.approx(traded)
    assert result.state.turnover_ratio == pytest.approx(traded / 1_000)
    assert (
        result.state.cash
        + sum(execution.target_value for execution in result.executions)
        + result.state.total_cost
        == pytest.approx(1_000)
    )
    reconciled_close = result.state.cash + sum(
        result.state.shares[symbol] * result.state.last_prices[symbol]
        for symbol in result.state.symbols
    )
    assert result.state.equity == pytest.approx(reconciled_close)
    assert result.state.max_drawdown == pytest.approx(
        min(0, result.state.equity / 1_000 - 1)
    )


def test_execution_rejects_forged_value_fill_and_hold_costs() -> None:
    valid_buy = {
        "symbol": "A",
        "session": "2025-01-03",
        "side": "buy",
        "raw_open_price": 100,
        "modeled_fill_price": 100.2,
        "shares_before": 1,
        "shares_after": 2,
        "shares_delta": 1,
        "current_value": 100,
        "target_value": 200,
        "traded_notional": 100,
        "fee": 0.1,
        "slippage": 0.2,
        "total_cost": 0.3,
    }
    PortfolioForwardExecution.model_validate(valid_buy)

    forged_value = {**valid_buy, "target_value": 250, "traded_notional": 150}
    with pytest.raises(ValidationError, match="target value"):
        PortfolioForwardExecution.model_validate(forged_value)

    forged_fill = {**valid_buy, "modeled_fill_price": 100.1}
    with pytest.raises(ValidationError, match="fill price"):
        PortfolioForwardExecution.model_validate(forged_fill)

    forged_hold_cost = {
        **valid_buy,
        "side": "hold",
        "modeled_fill_price": 100,
        "shares_after": 1,
        "shares_delta": 0,
        "target_value": 100,
        "traded_notional": 0,
        "fee": 0.1,
        "slippage": 0,
        "total_cost": 0.1,
    }
    with pytest.raises(ValidationError, match="Hold executions"):
        PortfolioForwardExecution.model_validate(forged_hold_cost)


def test_advance_rejects_executions_forged_against_post_state_rates() -> None:
    state = initialize_portfolio_forward_state(
        ["A", "B"],
        1_000,
        "periodic_equal",
        fee_rate=0.001,
        slippage_rate=0.002,
        session="2025-01-02",
    )
    result = advance_portfolio_session(
        state,
        session="2025-01-03",
        open_prices={"A": 100, "B": 100},
        close_prices={"A": 100, "B": 100},
        pending_target=equal_target("2025-01-02"),
    )
    original = result.executions[0]
    remaining = result.executions[1:]

    forged_fee = original.model_copy(
        update={
            "fee": original.fee * 2,
            "total_cost": original.fee * 2 + original.slippage,
        }
    )
    with pytest.raises(ValidationError, match="does not match state fee rate"):
        PortfolioForwardAdvance(
            state=result.state,
            executions=(forged_fee, *remaining),
        )

    forged_slippage = original.model_copy(
        update={
            "slippage": original.slippage * 2,
            "total_cost": original.fee + original.slippage * 2,
            "modeled_fill_price": (
                original.raw_open_price * (1 + state.slippage_rate * 2)
            ),
        }
    )
    with pytest.raises(ValidationError, match="does not match state slippage rate"):
        PortfolioForwardAdvance(
            state=result.state,
            executions=(forged_slippage, *remaining),
        )

    forged_fill = original.model_copy(
        update={"modeled_fill_price": original.modeled_fill_price + 1}
    )
    with pytest.raises(ValidationError, match="fill price"):
        PortfolioForwardAdvance(
            state=result.state,
            executions=(forged_fill, *remaining),
        )


def test_state_rejects_impossible_drawdown_history() -> None:
    unvalued = initialize_portfolio_forward_state(
        ["A", "B"],
        1_000,
        "periodic_equal",
    ).model_dump(mode="python")
    unvalued["max_drawdown"] = -0.01
    with pytest.raises(ValidationError, match="zero max drawdown"):
        PortfolioForwardState.model_validate(unvalued)

    state = initialize_portfolio_forward_state(
        ["A", "B"],
        1_000,
        "periodic_equal",
        fee_rate=0,
        slippage_rate=0,
        session="2025-01-02",
    )
    valued = advance_portfolio_session(
        state,
        session="2025-01-03",
        open_prices={"A": 100, "B": 100},
        close_prices={"A": 80, "B": 80},
        pending_target=equal_target("2025-01-02"),
    ).state
    assert valued.max_drawdown == pytest.approx(-0.2)

    impossible = valued.model_dump(mode="python")
    impossible["max_drawdown"] = -0.1
    with pytest.raises(ValidationError, match="current drawdown"):
        PortfolioForwardState.model_validate(impossible)

    historical_low = valued.model_dump(mode="python")
    historical_low["max_drawdown"] = -0.3
    accepted = PortfolioForwardState.model_validate(historical_low)
    assert accepted.max_drawdown == -0.3


def test_advance_without_target_holds_cash_and_emits_hold_audit_rows() -> None:
    state = initialize_portfolio_forward_state(
        ["A", "B"],
        5_000,
        "periodic_equal",
    )

    result = advance_portfolio_session(
        state,
        session="2025-01-03",
        open_prices={"A": 100, "B": 200},
        close_prices={"A": 110, "B": 180},
    )

    assert result.state.cash == 5_000
    assert result.state.equity == 5_000
    assert result.state.rebalance_count == 0
    assert result.state.valuation_count == 1
    assert result.state.total_cost == 0
    assert all(execution.side == "hold" for execution in result.executions)
    assert all(execution.traded_notional == 0 for execution in result.executions)


@pytest.mark.parametrize(
    ("open_prices", "close_prices", "match"),
    [
        ({"A": 100}, {"A": 100, "B": 100}, "exactly match"),
        (
            {"A": 100, "B": 100, "C": 100},
            {"A": 100, "B": 100},
            "exactly match",
        ),
        ({"A": np.nan, "B": 100}, {"A": 100, "B": 100}, "finite"),
        ({"A": 100, "B": 100}, {"A": 0, "B": 100}, "positive"),
    ],
)
def test_advance_requires_complete_finite_positive_price_maps(
    open_prices: dict[str, float],
    close_prices: dict[str, float],
    match: str,
) -> None:
    state = initialize_portfolio_forward_state(
        ["A", "B"],
        1_000,
        "periodic_equal",
    )
    with pytest.raises(ValueError, match=match):
        advance_portfolio_session(
            state,
            session="2025-01-03",
            open_prices=open_prices,
            close_prices=close_prices,
        )


def test_advance_rejects_replayed_session_stale_target_and_invalid_state() -> None:
    state = initialize_portfolio_forward_state(
        ["A", "B"],
        1_000,
        "periodic_equal",
        session="2025-01-03",
    )
    prices = {"A": 100.0, "B": 100.0}
    with pytest.raises(ValueError, match="strictly increasing"):
        advance_portfolio_session(
            state,
            session="2025-01-03",
            open_prices=prices,
            close_prices=prices,
        )
    with pytest.raises(ValueError, match="equal the portfolio state session"):
        advance_portfolio_session(
            state,
            session="2025-01-06",
            open_prices=prices,
            close_prices=prices,
            pending_target=equal_target("2025-01-02"),
        )

    invalid = state.model_dump(mode="python")
    invalid["cash"] = -1
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        PortfolioForwardState.model_validate(invalid)


def test_forward_ledger_matches_historical_engine_for_same_targets() -> None:
    close = varied_close_history(periods=110)
    observations = np.arange(len(close))
    open_prices = close.mul(
        1 + np.cos(observations / 7)[:, np.newaxis] / 1_000,
        axis="index",
    )
    assets = {
        symbol: pd.DataFrame(
            {"open": open_prices[symbol], "close": close[symbol]},
            index=close.index,
        )
        for symbol in close.columns
    }
    config = BacktestConfig(
        initial_cash=123_456,
        fee_rate=0.0007,
        slippage_rate=0.0004,
    )
    evaluation_start = close.index[70]
    historical = run_portfolio_backtest(
        assets,
        "periodic_inverse_volatility",
        config,
        evaluation_start=evaluation_start,
        volatility_lookback=60,
        rebalance_bars=10,
        maximum_asset_weight=0.4,
    )

    state = initialize_portfolio_forward_state(
        list(close.columns),
        config.initial_cash,
        "periodic_inverse_volatility",
        fee_rate=config.fee_rate,
        slippage_rate=config.slippage_rate,
        volatility_lookback=60,
        maximum_asset_weight=0.4,
        session=close.index[69],
    )
    evaluation = close.index[70:]
    for bar, session in enumerate(evaluation):
        target = (
            compute_next_portfolio_target(
                close,
                "periodic_inverse_volatility",
                information_session=close.index[69 + bar],
                volatility_lookback=60,
                maximum_asset_weight=0.4,
            )
            if bar % 10 == 0
            else None
        )
        state = advance_portfolio_session(
            state,
            session=session,
            open_prices=open_prices.loc[session].to_dict(),
            close_prices=close.loc[session].to_dict(),
            pending_target=target,
        ).state

    assert state.equity == pytest.approx(historical.equity[-1]["equity"])
    assert state.rebalance_count == historical.metrics.rebalances
    assert state.turnover_ratio == pytest.approx(historical.metrics.turnover_ratio)
    assert state.total_cost == pytest.approx(
        historical.metrics.transaction_cost_ratio * config.initial_cash
    )
    assert state.target_weights == pytest.approx(
        historical.last_rebalance_target_weights
    )
    assert state.realized_weights == pytest.approx(
        historical.ending_realized_weights
    )


def test_randomized_long_run_preserves_accounting_and_cost_invariants() -> None:
    random = np.random.default_rng(20260726)
    symbols = ("A", "B", "C", "D")
    prices = np.full(len(symbols), 100.0)
    state = initialize_portfolio_forward_state(
        list(symbols),
        50_000,
        "periodic_inverse_volatility",
        fee_rate=0.0008,
        slippage_rate=0.0005,
        maximum_asset_weight=1,
        session="2025-01-01",
    )
    sessions = pd.date_range("2025-01-02", periods=300, freq="D")

    for index, session in enumerate(sessions, start=1):
        open_values = prices * np.exp(random.normal(0, 0.01, len(symbols)))
        close_values = open_values * np.exp(random.normal(0, 0.015, len(symbols)))
        target = None
        if index % 7 == 0:
            assert state.session is not None
            target = PortfolioForwardTarget(
                method="periodic_inverse_volatility",
                information_session=state.session,
                weights=dict(
                    zip(
                        symbols,
                        random.dirichlet(np.ones(len(symbols))),
                        strict=True,
                    )
                ),
            )
        previous = state
        previous_snapshot = previous.model_dump(mode="python")
        result = advance_portfolio_session(
            previous,
            session=session,
            open_prices=dict(zip(symbols, open_values, strict=True)),
            close_prices=dict(zip(symbols, close_values, strict=True)),
            pending_target=target,
        )
        state = result.state
        assert previous.model_dump(mode="python") == previous_snapshot
        assert state.cash >= 0
        assert np.isfinite(state.equity)
        assert state.equity == pytest.approx(
            state.cash
            + sum(
                state.shares[symbol] * state.last_prices[symbol]
                for symbol in symbols
            )
        )
        assert state.total_cost == pytest.approx(
            state.fee_paid + state.slippage_paid
        )
        assert state.total_cost - previous.total_cost == pytest.approx(
            sum(execution.total_cost for execution in result.executions)
        )
        assert state.turnover_notional - previous.turnover_notional == pytest.approx(
            sum(execution.traded_notional for execution in result.executions)
        )
        prices = close_values

    assert state.valuation_count == 300
    assert state.rebalance_count == 42
