from datetime import UTC, date, datetime

import pandas as pd
import pytest
from quantsieve_api.paper import (
    _forward_duration_years,
    advance_forward_state,
    assess_forward_health,
    initialize_forward_state,
    migrate_forward_cycle_evidence,
    revise_forward_signal,
)
from quantsieve_api.schemas import ExperimentRecord
from quantsieve_engine import BacktestConfig, BacktestMetrics, run_backtest


def _metrics(
    *,
    total_return: float = 0.12,
    max_drawdown: float = -0.05,
    win_rate: float = 0.6,
    bars: int = 100,
) -> BacktestMetrics:
    return BacktestMetrics(
        bars=bars,
        duration_years=1,
        total_return=total_return,
        annualized_return=total_return,
        annualized_volatility=0.15,
        sharpe_ratio=1,
        max_drawdown=max_drawdown,
        win_rate=win_rate,
        trades=12,
        closed_trades=12,
        trades_per_year=12,
        average_holding_bars=5,
        profit_factor=1.5,
        exposure_ratio=0.5,
        max_cash_streak=10,
        max_cash_streak_ratio=0.1,
    )


def _experiment() -> ExperimentRecord:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    strategy = _metrics()
    benchmark = _metrics(total_return=0.08, win_rate=1)
    return ExperimentRecord.model_validate(
        {
            "id": "experiment",
            "name": "前向健康测试",
            "instrument": {
                "symbol": "TEST",
                "name": "测试标的",
                "market": "US",
                "exchange": "测试交易所",
                "currency": "USD",
                "provider": "yfinance",
                "asset_type": "equity",
            },
            "strategy": {
                "id": "trend-filter",
                "name": "趋势过滤",
                "category": "trend",
                "parameters": {},
            },
            "interval": "1d",
            "start": date(2025, 1, 1),
            "end": date(2026, 1, 1),
            "optimized": True,
            "run_request": {
                "maximum_drawdown": 0.1,
                "maximum_cash_streak_bars": 20,
                "minimum_trades_per_year": 2,
                "maximum_trades_per_year": 100,
            },
            "engine_config": {
                "initial_cash": 100_000,
                "fee_rate": 0.0003,
                "slippage_rate": 0.0002,
                "annual_periods": 252,
                "bar_interval": "1d",
                "signal_delay_bars": 1,
            },
            "data_metadata": {},
            "metrics": strategy,
            "benchmark_metrics": benchmark,
            "comparison": {
                "excess_return": 0.04,
                "excess_annualized_return": 0.04,
                "drawdown_improvement": 0,
                "beats_benchmark": True,
                "positive_return": True,
            },
            "diagnostics": {},
            "validation": {
                "objective": "balanced",
                "split_date": "2025-10-01",
                "validation_passed": True,
                "validation_reason": "通过",
                "development_metrics": strategy,
                "validation_metrics": strategy,
                "validation_benchmark_metrics": benchmark,
            },
            "citations": [],
            "created_at": now,
            "updated_at": now,
        }
    )


def test_forward_duration_falls_back_for_mixed_timezone_strings() -> None:
    duration = _forward_duration_years(
        "2026-01-01T00:00:00",
        "2026-06-01T00:00:00+00:00",
        bars=126,
        annual_periods=252,
    )

    assert duration == 0.5


def test_forward_ledger_starts_at_zero_and_executes_next_open() -> None:
    baseline = initialize_forward_state(
        bar_at=datetime(2026, 1, 1, tzinfo=UTC),
        requested_signal=1,
        initial_equity=100,
        signal_delay_bars=1,
    )

    assert baseline.bars == 0
    assert baseline.equity == 100
    assert baseline.total_return == 0
    assert baseline.position == 0
    assert baseline.pending_targets == [1]

    entered = advance_forward_state(
        baseline,
        bar_at=datetime(2026, 1, 2, tzinfo=UTC),
        previous_close=100,
        open_price=100,
        close_price=110,
        requested_signal=0,
        fee_rate=0.01,
        slippage_rate=0,
    )

    assert entered.bars == 1
    assert entered.position == 1
    assert entered.equity == pytest.approx(109)
    assert entered.total_return == pytest.approx(0.09)
    assert entered.benchmark_total_return == pytest.approx(0.09)
    assert entered.orders == 1
    assert entered.round_trips == 0
    assert entered.execution is not None
    assert entered.execution.side == "buy"
    assert entered.execution.raw_open_price == 100
    assert entered.execution.modeled_fill_price == 101
    assert entered.execution.friction_amount == pytest.approx(1)

    exited = advance_forward_state(
        entered,
        bar_at=datetime(2026, 1, 3, tzinfo=UTC),
        previous_close=110,
        open_price=121,
        close_price=110,
        requested_signal=0,
        fee_rate=0.01,
        slippage_rate=0,
    )

    assert exited.position == 0
    assert exited.equity == pytest.approx(118.81)
    assert exited.benchmark_equity == pytest.approx(109)
    assert exited.excess_return == pytest.approx(0.0981)
    assert exited.orders == 2
    assert exited.round_trips == 1
    assert exited.execution is not None
    assert exited.execution.side == "sell"
    assert exited.execution.modeled_fill_price == pytest.approx(119.79)
    assert exited.total_friction_paid == pytest.approx(2.09)
    assert exited.open_trade_entry_price is None
    assert exited.last_closed_trade_return == pytest.approx(0.1881)
    assert exited.winning_round_trips == 1
    assert exited.losing_round_trips == 0
    assert exited.closed_trade_return_sum == pytest.approx(0.1881)
    assert exited.exposed_bars == 1
    assert exited.cash_streak_bars == 1

    index = pd.to_datetime(
        ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"]
    )
    frame = pd.DataFrame(
        {
            "open": [100, 100, 121],
            "high": [101, 111, 122],
            "low": [99, 99, 109],
            "close": [100, 110, 110],
            "volume": [1_000, 1_000, 1_000],
        },
        index=index,
    )
    engine_result = run_backtest(
        frame,
        pd.Series([1, 0, 0], index=index),
        BacktestConfig(
            initial_cash=100,
            fee_rate=0.01,
            slippage_rate=0,
            signal_delay_bars=1,
        ),
    )
    assert engine_result.equity[-1]["equity"] == pytest.approx(exited.equity)
    assert len(engine_result.position_cycles) == 1
    assert engine_result.position_cycles[0]["return"] == pytest.approx(
        exited.last_closed_trade_return
    )


def test_forward_ledger_honors_delay_and_same_bar_signal_revision() -> None:
    baseline = initialize_forward_state(
        bar_at="2026-01-01",
        requested_signal=1,
        initial_equity=100_000,
        signal_delay_bars=2,
    )

    assert baseline.pending_targets == [0, 1]
    revised = revise_forward_signal(baseline, 0)
    assert revised.pending_targets == [0, 0]
    assert revised.bars == 0
    assert revised.equity == baseline.equity

    first_bar = advance_forward_state(
        revised,
        bar_at="2026-01-02",
        previous_close=100,
        open_price=100,
        close_price=105,
        requested_signal=1,
        fee_rate=0.0003,
        slippage_rate=0.0002,
    )
    assert first_bar.position == 0
    assert first_bar.orders == 0
    assert first_bar.total_return == 0
    assert first_bar.pending_targets == [0, 1]

    second_bar = advance_forward_state(
        first_bar,
        bar_at="2026-01-03",
        previous_close=105,
        open_price=105,
        close_price=110,
        requested_signal=1,
        fee_rate=0.0003,
        slippage_rate=0.0002,
    )
    assert second_bar.position == 0
    assert second_bar.orders == 0
    assert second_bar.pending_targets == [1, 1]


def test_forward_ledger_keeps_partial_satellite_lots_in_one_relative_cycle() -> None:
    baseline = initialize_forward_state(
        bar_at="2026-01-01",
        requested_signal=0.25,
        requested_cycle_baseline=0.25,
        initial_equity=100_000,
        signal_delay_bars=1,
    )
    core = advance_forward_state(
        baseline,
        bar_at="2026-01-02",
        previous_close=100,
        open_price=100,
        close_price=100,
        requested_signal=1,
        requested_cycle_baseline=0.25,
        fee_rate=0,
        slippage_rate=0,
    )
    satellite = advance_forward_state(
        core,
        bar_at="2026-01-03",
        previous_close=100,
        open_price=110,
        close_price=110,
        requested_signal=0.25,
        requested_cycle_baseline=0.25,
        fee_rate=0,
        slippage_rate=0,
    )
    reduced = advance_forward_state(
        satellite,
        bar_at="2026-01-04",
        previous_close=110,
        open_price=121,
        close_price=121,
        requested_signal=0.25,
        requested_cycle_baseline=0.25,
        fee_rate=0,
        slippage_rate=0,
    )

    assert [lot.position_size for lot in satellite.open_lots] == pytest.approx(
        [0.25, 0.75]
    )
    assert reduced.position == pytest.approx(0.25)
    assert reduced.round_trips == 1
    assert reduced.cycle_kind == "satellite_over_core"
    assert (
        reduced.cycle_return_semantics == "compounded_relative_to_core"
    )
    assert reduced.last_closed_trade_return == pytest.approx(1.1 / 1.025 - 1)
    assert len(reduced.open_lots) == 1
    assert reduced.open_lots[0].position_size == pytest.approx(0.25)
    assert reduced.open_trade_entry_price == pytest.approx(100)
    assert reduced.exposure_sum == pytest.approx(1.5)

    health = assess_forward_health(_experiment(), reduced)
    assert health.exposure_ratio == pytest.approx(0.5)
    assert health.round_trips == 1

    index = pd.to_datetime(
        [
            "2026-01-01T00:00:00Z",
            "2026-01-02T00:00:00Z",
            "2026-01-03T00:00:00Z",
            "2026-01-04T00:00:00Z",
        ]
    )
    frame = pd.DataFrame(
        {
            "open": [100, 100, 110, 121],
            "high": [100, 100, 110, 121],
            "low": [100, 100, 110, 121],
            "close": [100, 100, 110, 121],
            "volume": [1_000] * 4,
        },
        index=index,
    )
    engine_result = run_backtest(
        frame,
        pd.Series([0.25, 1, 0.25, 0.25], index=index),
        BacktestConfig(
            initial_cash=100_000,
            fee_rate=0,
            slippage_rate=0,
            signal_delay_bars=1,
        ),
        cycle_baseline_signals=pd.Series([0.25] * 4, index=index),
    )
    assert len(engine_result.position_cycles) == 1
    assert engine_result.position_cycles[0]["return"] == pytest.approx(
        reduced.last_closed_trade_return
    )


def test_partial_reduction_remains_inside_one_flat_to_flat_cycle() -> None:
    baseline = initialize_forward_state(
        bar_at="2026-01-01",
        requested_signal=1,
        initial_equity=100_000,
        signal_delay_bars=1,
    )
    entered = advance_forward_state(
        baseline,
        bar_at="2026-01-02",
        previous_close=100,
        open_price=100,
        close_price=110,
        requested_signal=0.5,
        fee_rate=0,
        slippage_rate=0,
    )
    reduced = advance_forward_state(
        entered,
        bar_at="2026-01-03",
        previous_close=110,
        open_price=110,
        close_price=121,
        requested_signal=0,
        fee_rate=0,
        slippage_rate=0,
    )
    exited = advance_forward_state(
        reduced,
        bar_at="2026-01-04",
        previous_close=121,
        open_price=121,
        close_price=121,
        requested_signal=0,
        fee_rate=0,
        slippage_rate=0,
    )

    assert reduced.position == pytest.approx(0.5)
    assert reduced.orders == 2
    assert reduced.round_trips == 0
    assert reduced.open_cycle_started_at == "2026-01-02"
    assert exited.orders == 3
    assert exited.round_trips == 1
    assert exited.last_closed_trade_return == pytest.approx(0.155)


def test_legacy_lot_evidence_is_reset_and_open_cycle_is_not_partially_reused() -> None:
    baseline = initialize_forward_state(
        bar_at="2026-01-01",
        requested_signal=1,
        initial_equity=100_000,
        signal_delay_bars=1,
    )
    entered = advance_forward_state(
        baseline,
        bar_at="2026-01-02",
        previous_close=100,
        open_price=100,
        close_price=105,
        requested_signal=0,
        fee_rate=0,
        slippage_rate=0,
    )
    unversioned_payload = entered.model_dump(mode="python")
    unversioned_payload.pop("schema_version")
    assert type(entered).model_validate(unversioned_payload).schema_version == 1
    legacy = entered.model_copy(
        update={
            "schema_version": 1,
            "round_trips": 7,
            "winning_round_trips": 6,
            "losing_round_trips": 1,
            "closed_trade_return_sum": 0.5,
            "closed_trade_gain_sum": 0.6,
            "closed_trade_loss_sum": 0.1,
        }
    )

    legacy_health = assess_forward_health(_experiment(), legacy)
    assert legacy_health.round_trips == 0
    assert legacy_health.quality_sample_status == "legacy_pending_migration"
    assert any("资金批次" in reason for reason in legacy_health.reasons)

    migrated = migrate_forward_cycle_evidence(
        legacy,
        bar_at="2026-01-02",
        cycle_baseline_position=0,
        pending_cycle_baseline_targets=[0],
        uses_cycle_baseline=False,
    )
    assert migrated.schema_version == 2
    assert migrated.round_trips == 0
    assert migrated.quality_bars == 0
    assert migrated.quality_sample_status == "awaiting_baseline_reset"
    exited_legacy_cycle = advance_forward_state(
        migrated,
        bar_at="2026-01-03",
        previous_close=105,
        open_price=105,
        close_price=105,
        requested_signal=1,
        fee_rate=0,
        slippage_rate=0,
    )
    assert exited_legacy_cycle.round_trips == 0
    assert exited_legacy_cycle.quality_sample_status == "tracking"

    entered_new_cycle = advance_forward_state(
        exited_legacy_cycle,
        bar_at="2026-01-04",
        previous_close=105,
        open_price=105,
        close_price=110,
        requested_signal=0,
        fee_rate=0,
        slippage_rate=0,
    )
    exited_new_cycle = advance_forward_state(
        entered_new_cycle,
        bar_at="2026-01-05",
        previous_close=110,
        open_price=110,
        close_price=110,
        requested_signal=0,
        fee_rate=0,
        slippage_rate=0,
    )
    assert exited_new_cycle.round_trips == 1
    assert exited_new_cycle.winning_round_trips == 1
    assert exited_new_cycle.last_closed_trade_return == pytest.approx(110 / 105 - 1)


def test_forward_health_waits_for_evidence_and_respects_drawdown_budget() -> None:
    experiment = _experiment()
    baseline = initialize_forward_state(
        bar_at="2026-01-01",
        requested_signal=1,
        initial_equity=100_000,
        signal_delay_bars=1,
    )

    baseline_health = assess_forward_health(experiment, baseline)
    assert baseline_health.status == "baseline"
    assert baseline_health.assessment_ready is False
    assert baseline_health.drawdown_limit == 0.1
    assert baseline_health.drawdown_limit_source == "saved_constraint"

    collecting = baseline.model_copy(
        update={
            "bars": 12,
            "quality_bars": 12,
            "total_return": -0.01,
            "benchmark_total_return": 0.02,
            "excess_return": -0.03,
            "max_drawdown": -0.04,
            "round_trips": 2,
            "winning_round_trips": 1,
            "losing_round_trips": 1,
            "closed_trade_return_sum": -0.01,
            "closed_trade_gain_sum": 0.02,
            "closed_trade_loss_sum": 0.03,
        }
    )
    collecting_health = assess_forward_health(experiment, collecting)
    assert collecting_health.status == "collecting"
    assert collecting_health.assessment_ready is False
    assert collecting_health.warning_codes == []
    assert any("还需 18 根" in reason for reason in collecting_health.reasons)

    breached = collecting.model_copy(
        update={
            "max_drawdown": -0.11,
            "cash_streak_bars": 21,
        }
    )
    breached_health = assess_forward_health(experiment, breached)
    assert breached_health.status == "review"
    assert "drawdown_constraint_breached" in breached_health.warning_codes
    assert "cash_streak_constraint_breached" in breached_health.warning_codes
    assert breached_health.assessment_ready is False


def test_forward_health_distinguishes_healthy_lagging_and_trade_decay() -> None:
    experiment = _experiment()
    baseline = initialize_forward_state(
        bar_at="2026-01-01",
        requested_signal=1,
        initial_equity=100_000,
        signal_delay_bars=1,
    )
    ready = baseline.model_copy(
        update={
            "bars": 40,
            "quality_bars": 40,
            "equity": 108_000,
            "total_return": 0.08,
            "peak_equity": 110_000,
            "max_drawdown": -0.04,
            "benchmark_equity": 105_000,
            "benchmark_total_return": 0.05,
            "benchmark_peak_equity": 106_000,
            "benchmark_max_drawdown": -0.03,
            "excess_return": 0.03,
            "round_trips": 5,
            "winning_round_trips": 3,
            "losing_round_trips": 2,
            "closed_trade_return_sum": 0.08,
                "closed_trade_gain_sum": 0.14,
                "closed_trade_loss_sum": 0.06,
                "exposed_bars": 24,
                "exposure_sum": 24,
            }
        )

    healthy = assess_forward_health(experiment, ready)
    assert healthy.status == "healthy"
    assert healthy.assessment_ready is True
    assert healthy.win_rate == pytest.approx(0.6)
    assert healthy.expectancy == pytest.approx(0.016)
    assert healthy.profit_factor == pytest.approx(0.14 / 0.06)
    assert healthy.exposure_ratio == pytest.approx(0.6)
    assert healthy.expected_return_reference is not None

    lagging = ready.model_copy(
        update={
            "equity": 103_000,
            "total_return": 0.03,
            "benchmark_equity": 108_000,
            "benchmark_total_return": 0.08,
            "excess_return": -0.05,
        }
    )
    lagging_health = assess_forward_health(experiment, lagging)
    assert lagging_health.status == "watch"
    assert "lagging_benchmark" in lagging_health.warning_codes

    decayed = ready.model_copy(
        update={
            "round_trips": 10,
            "winning_round_trips": 1,
            "losing_round_trips": 9,
            "closed_trade_return_sum": -0.2,
            "closed_trade_gain_sum": 0.02,
            "closed_trade_loss_sum": 0.22,
        }
    )
    decayed_health = assess_forward_health(experiment, decayed)
    assert decayed_health.status == "review"
    assert "negative_trade_expectancy" in decayed_health.warning_codes
    assert decayed_health.win_rate_confidence_high is not None
    assert decayed_health.win_rate_confidence_high < 0.5
