from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .schemas import (
    ExperimentRecord,
    PaperForwardExecution,
    PaperForwardHealth,
    PaperForwardLot,
    PaperForwardState,
    PaperSignalSnapshot,
    PaperTrackRecord,
)


def _position(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)


def initialize_forward_state(
    *,
    bar_at: object,
    requested_signal: float,
    requested_cycle_baseline: float | None = None,
    initial_equity: float,
    signal_delay_bars: int,
    calculation_origin: Literal["activation", "migration"] = "activation",
) -> PaperForwardState:
    """Create a zero-return activation baseline without retroactive exposure."""
    delay = min(max(int(signal_delay_bars), 1), 5)
    requested = _position(requested_signal)
    uses_cycle_baseline = requested_cycle_baseline is not None
    baseline_requested = (
        _position(requested_cycle_baseline)
        if requested_cycle_baseline is not None
        else 0.0
    )
    if baseline_requested > requested + 1e-12:
        raise ValueError("周期核心仓位不能高于策略目标仓位。")
    bar_identity = str(bar_at)
    return PaperForwardState(
        schema_version=2,
        calculation_origin=calculation_origin,
        started_at=bar_identity,
        last_bar_at=bar_identity,
        initial_equity=initial_equity,
        equity=initial_equity,
        total_return=0,
        peak_equity=initial_equity,
        max_drawdown=0,
        benchmark_equity=initial_equity,
        benchmark_total_return=0,
        benchmark_peak_equity=initial_equity,
        benchmark_max_drawdown=0,
        excess_return=0,
        position=0,
        benchmark_position=0,
        execution_delay_bars=delay,
        pending_targets=[0.0] * (delay - 1) + [requested],
        cycle_kind=(
            "satellite_over_core" if uses_cycle_baseline else "flat_to_flat"
        ),
        cycle_return_semantics=(
            "compounded_relative_to_core"
            if uses_cycle_baseline
            else "compounded_strategy_return"
        ),
        cycle_baseline_position=0,
        pending_cycle_baseline_targets=(
            [0.0] * (delay - 1) + [baseline_requested]
        ),
        quality_calculation_origin="activation",
        quality_started_at=bar_identity,
        quality_sample_status="tracking",
        quality_reset_reason="none",
        exposure_sum=0,
    )


def migrate_forward_cycle_evidence(
    state: PaperForwardState,
    *,
    bar_at: object,
    cycle_baseline_position: float,
    pending_cycle_baseline_targets: Sequence[float],
    uses_cycle_baseline: bool,
) -> PaperForwardState:
    """Adopt independent-cycle quality semantics without reusing lot evidence.

    Version 1 counted every matched capital lot as a round trip, so partial
    reductions could manufacture several apparent quality samples. Those
    aggregates cannot be losslessly reconstructed from the compact forward
    state. Keep its paper equity, order events, and lot attribution intact, but
    restart all quality aggregates. If migration occurs inside an already-open
    position/satellite cycle, wait for the strategy to return to its baseline
    before accepting a new sample.
    """
    if state.schema_version == 2:
        return state
    delay = state.execution_delay_bars
    if len(state.pending_targets) != delay:
        raise ValueError("策略信号队列必须与执行延迟一致。")
    baseline_position = _position(cycle_baseline_position)
    baseline_targets = [_position(value) for value in pending_cycle_baseline_targets]
    if len(baseline_targets) != delay:
        raise ValueError("周期核心信号队列必须与策略执行延迟一致。")
    if baseline_position > state.position + 1e-12:
        raise ValueError("周期核心仓位不能高于当前策略仓位。")
    if any(
        baseline > target + 1e-12
        for baseline, target in zip(
            baseline_targets,
            state.pending_targets,
            strict=True,
        )
    ):
        raise ValueError("周期核心信号不能高于对应的策略目标信号。")
    bar_identity = str(bar_at)
    return state.model_copy(
        update={
            "schema_version": 2,
            "cycle_kind": (
                "satellite_over_core" if uses_cycle_baseline else "flat_to_flat"
            ),
            "cycle_return_semantics": (
                "compounded_relative_to_core"
                if uses_cycle_baseline
                else "compounded_strategy_return"
            ),
            "cycle_baseline_position": baseline_position,
            "pending_cycle_baseline_targets": baseline_targets,
            "quality_calculation_origin": "cycle_semantics_migration",
            "quality_started_at": bar_identity,
            "quality_bars": 0,
            "quality_sample_status": (
                "awaiting_baseline_reset"
                if state.position - baseline_position > 1e-12
                else "tracking"
            ),
            "quality_reset_reason": "legacy_lot_statistics_excluded",
            "open_cycle_started_at": None,
            "open_cycle_strategy_growth": 1.0,
            "open_cycle_baseline_growth": 1.0,
            "open_cycle_holding_bars": 0,
            "round_trips": 0,
            "last_closed_trade_return": None,
            "winning_round_trips": 0,
            "losing_round_trips": 0,
            "closed_trade_return_sum": 0.0,
            "closed_trade_gain_sum": 0.0,
            "closed_trade_loss_sum": 0.0,
            "execution": None,
        }
    )


def revise_forward_signal(
    state: PaperForwardState,
    requested_signal: float,
    requested_cycle_baseline: float | None = None,
) -> PaperForwardState:
    """Revise the current bar's queued signal without booking another return."""
    if state.schema_version != 2:
        raise ValueError("旧版纸面质量样本必须先迁移后才能继续。")
    if len(state.pending_targets) != state.execution_delay_bars:
        raise ValueError("策略信号队列必须与执行延迟一致。")
    requested = _position(requested_signal)
    if state.cycle_kind == "satellite_over_core":
        if requested_cycle_baseline is None:
            raise ValueError("核心+卫星策略刷新时必须提供核心仓位信号。")
        baseline_requested = _position(requested_cycle_baseline)
    else:
        baseline_requested = 0.0
    if baseline_requested > requested + 1e-12:
        raise ValueError("周期核心仓位不能高于策略目标仓位。")
    pending_targets = list(state.pending_targets)
    pending_targets[-1] = requested
    pending_cycle_baseline_targets = list(state.pending_cycle_baseline_targets)
    if len(pending_cycle_baseline_targets) != len(pending_targets):
        pending_cycle_baseline_targets = [0.0] * len(pending_targets)
    pending_cycle_baseline_targets[-1] = baseline_requested
    return state.model_copy(
        update={
            "pending_targets": pending_targets,
            "pending_cycle_baseline_targets": pending_cycle_baseline_targets,
            "execution": None,
        }
    )


def advance_forward_state(
    state: PaperForwardState,
    *,
    bar_at: object,
    previous_close: float,
    open_price: float,
    close_price: float,
    requested_signal: float,
    requested_cycle_baseline: float | None = None,
    fee_rate: float,
    slippage_rate: float,
) -> PaperForwardState:
    """Book one newly observed bar using the engine's open-execution semantics."""
    if state.schema_version != 2:
        raise ValueError("旧版纸面质量样本必须先迁移后才能继续。")
    if min(previous_close, open_price, close_price) <= 0:
        raise ValueError("前向账本要求开盘价、收盘价和前收盘价均大于零。")
    transaction_rate = float(fee_rate) + float(slippage_rate)
    if not 0 <= transaction_rate <= 0.2:
        raise ValueError("前向账本的总交易摩擦必须位于 0% 到 20% 之间。")

    previous_position = state.position
    if len(state.pending_targets) != state.execution_delay_bars:
        raise ValueError("策略信号队列必须与执行延迟一致。")
    target_position = _position(state.pending_targets[0])
    baseline_targets = list(state.pending_cycle_baseline_targets)
    if len(baseline_targets) != state.execution_delay_bars:
        raise ValueError("周期核心信号队列必须与策略执行延迟一致。")
    previous_cycle_baseline = state.cycle_baseline_position
    target_cycle_baseline = _position(baseline_targets[0])
    requested = _position(requested_signal)
    if state.cycle_kind == "satellite_over_core":
        if requested_cycle_baseline is None:
            raise ValueError("核心+卫星策略刷新时必须提供核心仓位信号。")
        baseline_requested = _position(requested_cycle_baseline)
    else:
        baseline_requested = 0.0
    if (
        previous_cycle_baseline > previous_position + 1e-12
        or target_cycle_baseline > target_position + 1e-12
        or baseline_requested > requested + 1e-12
    ):
        raise ValueError("周期核心仓位不能高于策略仓位。")
    overnight_return = open_price / previous_close - 1
    intraday_return = close_price / open_price - 1
    gross_growth = (1 + previous_position * overnight_return) * (
        1 + target_position * intraday_return
    )
    turnover = abs(target_position - previous_position)
    friction_rate = turnover * transaction_rate
    bar_return = gross_growth - 1 - friction_rate
    cycle_baseline_growth = (
        1 + previous_cycle_baseline * overnight_return
    ) * (1 + target_cycle_baseline * intraday_return)
    cycle_baseline_turnover = abs(
        target_cycle_baseline - previous_cycle_baseline
    )
    cycle_baseline_return = (
        cycle_baseline_growth
        - 1
        - cycle_baseline_turnover * transaction_rate
    )
    if 1 + cycle_baseline_return <= 0:
        raise ValueError("周期核心账本净值已不大于零，无法继续计算。")
    equity = state.equity * (1 + bar_return)
    if equity <= 0:
        raise ValueError("前向账本净值已不大于零，无法继续计算。")
    peak_equity = max(state.peak_equity, equity)
    max_drawdown = min(state.max_drawdown, equity / peak_equity - 1)

    benchmark_target = 1.0
    benchmark_overnight = (
        state.benchmark_position * (open_price / previous_close - 1)
    )
    benchmark_intraday = benchmark_target * (close_price / open_price - 1)
    benchmark_gross_growth = (1 + benchmark_overnight) * (
        1 + benchmark_intraday
    )
    benchmark_turnover = abs(benchmark_target - state.benchmark_position)
    benchmark_friction_rate = benchmark_turnover * transaction_rate
    benchmark_bar_return = (
        benchmark_gross_growth - 1 - benchmark_friction_rate
    )
    benchmark_equity = state.benchmark_equity * (1 + benchmark_bar_return)
    if benchmark_equity <= 0:
        raise ValueError("前向买入持有基准净值已不大于零，无法继续计算。")
    benchmark_peak = max(state.benchmark_peak_equity, benchmark_equity)
    benchmark_max_drawdown = min(
        state.benchmark_max_drawdown,
        benchmark_equity / benchmark_peak - 1,
    )

    execution = None
    orders = state.orders
    open_lots = [lot.model_copy() for lot in state.open_lots]
    if (
        not open_lots
        and state.open_trade_entry_price is not None
        and previous_position > 0
    ):
        open_lots.append(
            PaperForwardLot(
                entered_at=state.started_at,
                entry_price=state.open_trade_entry_price,
                position_size=previous_position,
            )
        )
    friction_amount = state.equity * friction_rate
    if turnover > 1e-12:
        side: Literal["buy", "sell"] = (
            "buy" if target_position > previous_position else "sell"
        )
        modeled_fill_price = open_price * (
            1 + transaction_rate if side == "buy" else 1 - transaction_rate
        )
        execution = PaperForwardExecution(
            side=side,
            executed_at=str(bar_at),
            raw_open_price=open_price,
            modeled_fill_price=modeled_fill_price,
            position_before=previous_position,
            position_after=target_position,
            turnover=turnover,
            friction_rate=friction_rate,
            friction_amount=friction_amount,
        )
        orders += 1
        if side == "buy":
            open_lots.append(
                PaperForwardLot(
                    entered_at=str(bar_at),
                    entry_price=modeled_fill_price,
                    position_size=turnover,
                )
            )
        else:
            remaining = turnover
            while remaining > 1e-12 and open_lots:
                lot = open_lots[-1]
                matched_size = min(remaining, lot.position_size)
                remaining -= matched_size
                if matched_size >= lot.position_size - 1e-12:
                    open_lots.pop()
                else:
                    open_lots[-1] = lot.model_copy(
                        update={
                            "position_size": lot.position_size - matched_size,
                        }
                    )
    open_trade_entry_price = (
        open_lots[-1].entry_price if open_lots else None
    )

    pending_targets = [
        *state.pending_targets[1:],
        requested,
    ]
    pending_cycle_baseline_targets = [
        *baseline_targets[1:],
        baseline_requested,
    ]
    round_trips = state.round_trips
    last_closed_trade_return = state.last_closed_trade_return
    winning_round_trips = state.winning_round_trips
    losing_round_trips = state.losing_round_trips
    closed_trade_return_sum = state.closed_trade_return_sum
    closed_trade_gain_sum = state.closed_trade_gain_sum
    closed_trade_loss_sum = state.closed_trade_loss_sum
    quality_sample_status = state.quality_sample_status
    open_cycle_started_at = state.open_cycle_started_at
    open_cycle_strategy_growth = state.open_cycle_strategy_growth
    open_cycle_baseline_growth = state.open_cycle_baseline_growth
    open_cycle_holding_bars = state.open_cycle_holding_bars
    previous_excess = previous_position - previous_cycle_baseline
    target_excess = target_position - target_cycle_baseline
    tolerance = 1e-12
    if quality_sample_status == "legacy_pending_migration":
        raise ValueError("旧版纸面质量样本必须先完成显式迁移。")
    if quality_sample_status == "awaiting_baseline_reset":
        # The position/satellite was already open when v1 lot statistics were
        # retired. Its entry-period return is unknowable from compact state, so
        # even the eventual exit bar must not become a partial new sample.
        open_cycle_started_at = None
        open_cycle_strategy_growth = 1.0
        open_cycle_baseline_growth = 1.0
        open_cycle_holding_bars = 0
        if target_excess <= tolerance:
            quality_sample_status = "tracking"
    else:
        if open_cycle_started_at is None:
            if previous_excess > tolerance:
                raise ValueError("纸面周期状态缺少已开始周期，已停止以避免混入残缺样本。")
            if target_excess > tolerance:
                open_cycle_started_at = str(bar_at)
                open_cycle_strategy_growth = 1.0
                open_cycle_baseline_growth = 1.0
                open_cycle_holding_bars = 0
        if open_cycle_started_at is not None:
            open_cycle_strategy_growth *= 1 + bar_return
            open_cycle_baseline_growth *= 1 + cycle_baseline_return
            open_cycle_holding_bars += 1
            if not (
                math.isfinite(open_cycle_strategy_growth)
                and math.isfinite(open_cycle_baseline_growth)
                and open_cycle_strategy_growth > 0
                and open_cycle_baseline_growth > 0
            ):
                raise ValueError("纸面周期复利增长无效，已停止以避免写入错误样本。")
            if previous_excess > tolerance and target_excess <= tolerance:
                closed_return = (
                    open_cycle_strategy_growth / open_cycle_baseline_growth - 1
                )
                last_closed_trade_return = closed_return
                closed_trade_return_sum += closed_return
                if closed_return > 0:
                    winning_round_trips += 1
                    closed_trade_gain_sum += closed_return
                elif closed_return < 0:
                    losing_round_trips += 1
                    closed_trade_loss_sum += abs(closed_return)
                round_trips += 1
                open_cycle_started_at = None
                open_cycle_strategy_growth = 1.0
                open_cycle_baseline_growth = 1.0
                open_cycle_holding_bars = 0

    cash_streak_bars = state.cash_streak_bars + 1 if target_position <= 0 else 0
    exposure_sum = (
        state.exposure_sum
        if state.exposure_sum is not None
        else float(state.exposed_bars)
    ) + target_position
    total_return = equity / state.initial_equity - 1
    benchmark_total_return = benchmark_equity / state.initial_equity - 1
    return PaperForwardState(
        schema_version=2,
        calculation_origin=state.calculation_origin,
        started_at=state.started_at,
        last_bar_at=str(bar_at),
        bars=state.bars + 1,
        initial_equity=state.initial_equity,
        equity=equity,
        total_return=total_return,
        peak_equity=peak_equity,
        max_drawdown=max_drawdown,
        benchmark_equity=benchmark_equity,
        benchmark_total_return=benchmark_total_return,
        benchmark_peak_equity=benchmark_peak,
        benchmark_max_drawdown=benchmark_max_drawdown,
        excess_return=total_return - benchmark_total_return,
        position=target_position,
        benchmark_position=benchmark_target,
        execution_delay_bars=state.execution_delay_bars,
        pending_targets=pending_targets,
        cycle_kind=state.cycle_kind,
        cycle_return_semantics=state.cycle_return_semantics,
        cycle_baseline_position=target_cycle_baseline,
        pending_cycle_baseline_targets=pending_cycle_baseline_targets,
        quality_calculation_origin=state.quality_calculation_origin,
        quality_started_at=state.quality_started_at,
        quality_bars=state.quality_bars + 1,
        quality_sample_status=quality_sample_status,
        quality_reset_reason=state.quality_reset_reason,
        open_cycle_started_at=open_cycle_started_at,
        open_cycle_strategy_growth=open_cycle_strategy_growth,
        open_cycle_baseline_growth=open_cycle_baseline_growth,
        open_cycle_holding_bars=open_cycle_holding_bars,
        orders=orders,
        round_trips=round_trips,
        total_friction_paid=state.total_friction_paid + friction_amount,
        benchmark_friction_paid=(
            state.benchmark_friction_paid
            + state.benchmark_equity * benchmark_friction_rate
        ),
        open_trade_entry_price=open_trade_entry_price,
        open_lots=open_lots,
        last_closed_trade_return=last_closed_trade_return,
        winning_round_trips=winning_round_trips,
        losing_round_trips=losing_round_trips,
        closed_trade_return_sum=closed_trade_return_sum,
        closed_trade_gain_sum=closed_trade_gain_sum,
        closed_trade_loss_sum=closed_trade_loss_sum,
        exposed_bars=state.exposed_bars + (1 if target_position > 0 else 0),
        exposure_sum=exposure_sum,
        cash_streak_bars=cash_streak_bars,
        max_cash_streak_bars=max(
            state.max_cash_streak_bars,
            cash_streak_bars,
        ),
        last_bar_return=bar_return,
        benchmark_last_bar_return=benchmark_bar_return,
        execution=execution,
    )


def _mapping_number(payload: dict[str, object], key: str) -> float | None:
    value = payload.get(key)
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _wilson_interval(successes: int, samples: int) -> tuple[float, float]:
    if samples <= 0:
        return 0.0, 1.0
    probability = successes / samples
    z = 1.959963984540054
    denominator = 1 + z**2 / samples
    center = (probability + z**2 / (2 * samples)) / denominator
    margin = (
        z
        * math.sqrt(
            probability * (1 - probability) / samples
            + z**2 / (4 * samples**2)
        )
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _forward_duration_years(
    started_at: str | None,
    last_bar_at: str,
    bars: int,
    annual_periods: float,
) -> float | None:
    if bars <= 0:
        return None
    if started_at:
        try:
            start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(last_bar_at.replace("Z", "+00:00"))
            elapsed_seconds = (end - start).total_seconds()
            if math.isfinite(elapsed_seconds) and elapsed_seconds > 0:
                return elapsed_seconds / (365.2425 * 24 * 60 * 60)
        except (TypeError, ValueError):
            pass
    return bars / annual_periods


def assess_forward_health(
    experiment: ExperimentRecord,
    state: PaperForwardState,
) -> PaperForwardHealth:
    """Grade forward evidence without treating small samples as strategy failure."""
    minimum_bars = 30
    minimum_round_trips = 5
    configured_drawdown = _mapping_number(
        experiment.run_request,
        "maximum_drawdown",
    )
    holdout = experiment.validation.validation_metrics if experiment.validation else None
    holdout_drawdown = abs(holdout.max_drawdown) if holdout else 0.0
    if configured_drawdown and 0 < configured_drawdown <= 1:
        drawdown_limit = configured_drawdown
        drawdown_source: Literal[
            "saved_constraint",
            "holdout_reference",
            "default_reference",
        ] = "saved_constraint"
    elif holdout_drawdown > 0:
        drawdown_limit = min(max(holdout_drawdown * 2, 0.1), 0.3)
        drawdown_source = "holdout_reference"
    else:
        drawdown_limit = 0.2
        drawdown_source = "default_reference"

    expected_return_reference = None
    if holdout and holdout.bars > 0 and holdout.total_return > -1:
        expected_return_reference = (
            (1 + holdout.total_return) ** (state.bars / holdout.bars) - 1
        )

    annual_periods = _mapping_number(experiment.engine_config, "annual_periods")
    annual_periods = annual_periods if annual_periods and annual_periods > 0 else 252
    ledger_duration_years = _forward_duration_years(
        state.started_at,
        state.last_bar_at,
        state.bars,
        annual_periods,
    )
    trades_per_year = (
        state.orders / ledger_duration_years
        if ledger_duration_years is not None and ledger_duration_years > 0
        else None
    )
    legacy_quality = state.schema_version == 1
    quality_bars = 0 if legacy_quality else state.quality_bars
    round_trips = 0 if legacy_quality else state.round_trips
    quality_origin: Literal[
        "activation",
        "cycle_semantics_migration",
        "legacy_lot_statistics",
    ] = (
        "legacy_lot_statistics"
        if legacy_quality
        else state.quality_calculation_origin
    )
    quality_status: Literal[
        "tracking",
        "awaiting_baseline_reset",
        "legacy_pending_migration",
    ] = (
        "legacy_pending_migration"
        if legacy_quality
        else state.quality_sample_status
    )
    cycle_kind: Literal["flat_to_flat", "satellite_over_core"] = (
        "satellite_over_core"
        if legacy_quality and experiment.strategy.id == "core-trend-allocation"
        else state.cycle_kind
    )
    cycle_return_semantics: Literal[
        "compounded_strategy_return",
        "compounded_relative_to_core",
    ] = (
        "compounded_relative_to_core"
        if cycle_kind == "satellite_over_core"
        else "compounded_strategy_return"
    )
    exposure_total = (
        state.exposure_sum
        if state.exposure_sum is not None
        else float(state.exposed_bars)
    )
    exposure_ratio = exposure_total / state.bars if state.bars > 0 else None
    win_rate = (
        state.winning_round_trips / round_trips
        if round_trips > 0
        else None
    )
    confidence_low = None
    confidence_high = None
    expectancy = None
    profit_factor = None
    if round_trips > 0:
        confidence_low, confidence_high = _wilson_interval(
            state.winning_round_trips,
            round_trips,
        )
        expectancy = state.closed_trade_return_sum / round_trips
        if state.closed_trade_loss_sum > 0:
            profit_factor = (
                state.closed_trade_gain_sum / state.closed_trade_loss_sum
            )
        elif state.closed_trade_gain_sum > 0:
            profit_factor = 999.0
        else:
            profit_factor = 0.0

    assessment_ready = (
        quality_bars >= minimum_bars
        and round_trips >= minimum_round_trips
    )

    def health(
        *,
        status: Literal["baseline", "collecting", "healthy", "watch", "review"],
        title: str,
        summary: str,
        warning_codes: list[str],
        reasons: list[str],
    ) -> PaperForwardHealth:
        return PaperForwardHealth(
            status=status,
            title=title,
            summary=summary,
            assessment_ready=assessment_ready,
            evidence_bars=quality_bars,
            minimum_evidence_bars=minimum_bars,
            round_trips=round_trips,
            minimum_round_trips=minimum_round_trips,
            cycle_kind=cycle_kind,
            cycle_return_semantics=cycle_return_semantics,
            quality_calculation_origin=quality_origin,
            quality_sample_status=quality_status,
            quality_started_at=(
                None if legacy_quality else state.quality_started_at
            ),
            drawdown_limit=drawdown_limit,
            drawdown_limit_source=drawdown_source,
            expected_return_reference=expected_return_reference,
            trades_per_year=trades_per_year,
            exposure_ratio=exposure_ratio,
            win_rate=win_rate,
            win_rate_confidence_low=confidence_low,
            win_rate_confidence_high=confidence_high,
            expectancy=expectancy,
            profit_factor=profit_factor,
            warning_codes=warning_codes,
            reasons=reasons,
        )

    if state.bars == 0:
        return health(
            status="baseline",
            title="仅建立激活基线",
            summary="还没有激活后新 K 线，当前不能评价策略是否退化。",
            warning_codes=[],
            reasons=["首个快照只确定起点，不包含任何前向收益或成交样本。"],
        )

    review_codes: list[str] = []
    watch_codes: list[str] = []
    alert_reasons: list[str] = []

    def review(code: str, reason: str) -> None:
        if code not in review_codes:
            review_codes.append(code)
            alert_reasons.append(reason)

    def watch(code: str, reason: str) -> None:
        if code not in watch_codes and code not in review_codes:
            watch_codes.append(code)
            alert_reasons.append(reason)

    observed_drawdown = abs(state.max_drawdown)
    if observed_drawdown >= drawdown_limit:
        if drawdown_source == "saved_constraint":
            review(
                "drawdown_constraint_breached",
                "前向最大回撤已经超过实验保存的最大可接受回撤。",
            )
        else:
            watch(
                "drawdown_reference_breached",
                "前向最大回撤已经超过依据留出期或默认值生成的复核参考线。",
            )
    elif (
        drawdown_source == "saved_constraint"
        and state.bars >= 5
        and observed_drawdown >= drawdown_limit * 0.75
    ):
        watch(
            "drawdown_near_constraint",
            "前向最大回撤已用掉至少 75% 的保存回撤预算。",
        )
    if (
        holdout_drawdown > 0
        and state.bars >= 10
        and observed_drawdown >= max(holdout_drawdown * 2, 0.05)
        and observed_drawdown < drawdown_limit
    ):
        watch(
            "drawdown_worse_than_holdout",
            "前向最大回撤已达到最终留出期回撤的两倍以上。",
        )

    configured_cash_limit = _mapping_number(
        experiment.run_request,
        "maximum_cash_streak_bars",
    )
    if (
        configured_cash_limit
        and configured_cash_limit >= 1
        and state.cash_streak_bars > int(configured_cash_limit)
    ):
        review(
            "cash_streak_constraint_breached",
            "当前连续空仓 K 线已经超过实验保存的最长空仓约束。",
        )

    frequency_ready_bars = max(30, math.ceil(annual_periods / 4))
    if state.bars >= frequency_ready_bars and trades_per_year is not None:
        minimum_frequency = _mapping_number(
            experiment.run_request,
            "minimum_trades_per_year",
        )
        maximum_frequency = _mapping_number(
            experiment.run_request,
            "maximum_trades_per_year",
        )
        if minimum_frequency and trades_per_year < minimum_frequency:
            watch(
                "trade_frequency_below_constraint",
                "按当前前向样本折算的年交易次数低于保存的最低频率。",
            )
        if maximum_frequency and trades_per_year > maximum_frequency:
            watch(
                "trade_frequency_above_constraint",
                "按当前前向样本折算的年交易次数高于保存的最高频率。",
            )
    if state.bars >= minimum_bars and exposure_ratio is not None:
        minimum_exposure = _mapping_number(
            experiment.run_request,
            "minimum_exposure",
        )
        if minimum_exposure and exposure_ratio < minimum_exposure:
            watch(
                "exposure_below_constraint",
                "前向持仓率低于实验保存的最低参与度约束。",
            )

    if round_trips >= 10 and expectancy is not None and confidence_high is not None:
        if expectancy < 0 and confidence_high < 0.5:
            review(
                "negative_trade_expectancy",
                "至少 10 个前向独立决策周期的期望为负，且胜率区间上界低于 50%。",
            )
        elif expectancy < 0:
            watch(
                "negative_trade_expectancy",
                "至少 10 个前向独立决策周期的平均收益为负。",
            )
        if (
            holdout
            and confidence_high < holdout.win_rate
            and "negative_trade_expectancy" not in review_codes
        ):
            watch(
                "win_rate_below_holdout",
                "前向胜率 95% 区间上界仍低于最终留出期的历史胜率。",
            )

    if assessment_ready:
        if state.total_return < 0 and state.excess_return < 0:
            material_loss = max(0.05, drawdown_limit * 0.5)
            if state.total_return <= -material_loss:
                review(
                    "negative_and_lagging",
                    "达到最低证据门槛后，策略仍为负收益并同时落后买入持有。",
                )
            else:
                watch(
                    "negative_and_lagging",
                    "策略当前为负收益，并同时落后同起点买入持有。",
                )
        elif state.total_return < 0:
            watch(
                "negative_return",
                "达到最低证据门槛后，策略纸面收益仍为负。",
            )
        elif state.excess_return < 0:
            watch(
                "lagging_benchmark",
                "策略纸面收益为正，但仍落后同起点买入持有。",
            )

    evidence_reasons: list[str] = []
    if legacy_quality:
        evidence_reasons.append(
            "旧版按资金批次统计的往返已从质量证据中排除；"
            "下次连续刷新会迁移到独立决策周期口径。"
        )
    elif quality_status == "awaiting_baseline_reset":
        reset_target = (
            "防御核心仓位"
            if cycle_kind == "satellite_over_core"
            else "空仓"
        )
        evidence_reasons.append(
            f"迁移发生在旧周期中途；先等待仓位回到{reset_target}，"
            "再从下一次完整周期开始计样本。"
        )
    if quality_bars < minimum_bars:
        evidence_reasons.append(
            f"还需 {minimum_bars - quality_bars} 根新口径 K 线达到首批评估门槛。"
        )
    if round_trips < minimum_round_trips:
        evidence_reasons.append(
            f"还需积累到至少 {minimum_round_trips} 个已闭合独立决策周期；"
            f"当前为 {round_trips} 个。"
        )

    if review_codes:
        status: Literal["baseline", "collecting", "healthy", "watch", "review"] = (
            "review"
        )
        title = "建议暂停新增使用并复核"
        summary = "前向证据已经触发一项或多项强风险条件；这不是自动下单指令。"
    elif watch_codes:
        status = "watch"
        title = "出现需要关注的前向偏离"
        summary = "当前证据出现偏离，但仍应结合样本量、市场状态和数据连续性复核。"
    elif not assessment_ready:
        status = "collecting"
        title = "前向证据积累中"
        summary = "尚未同时达到 30 根新口径 K 线与 5 个独立决策周期，不评价策略优劣。"
    else:
        status = "healthy"
        title = "当前未发现明显退化"
        summary = "在现有前向样本内尚未触发收益、基准、回撤、频率或成交质量预警。"
        alert_reasons.append(
            "该状态只表示当前规则未触发，不代表策略未来有效或可以实盘。"
        )

    return health(
        status=status,
        title=title,
        summary=summary,
        warning_codes=[*review_codes, *watch_codes],
        reasons=[*alert_reasons, *evidence_reasons],
    )


class PaperTrackStore:
    """Store forward-only signal checks and explicitly modeled paper P&L."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_tracks (
                    id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL UNIQUE,
                    experiment_payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    last_error TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_signal_snapshots (
                    id TEXT PRIMARY KEY,
                    track_id TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(track_id) REFERENCES paper_tracks(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_snapshots_track "
                "ON paper_signal_snapshots(track_id, checked_at DESC)"
            )

    def create(self, experiment: ExperimentRecord) -> PaperTrackRecord:
        now = datetime.now(UTC)
        track_id = uuid4().hex
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO paper_tracks (
                        id, experiment_id, experiment_payload, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        track_id,
                        experiment.id,
                        json.dumps(experiment.model_dump(mode="json"), ensure_ascii=False),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("该实验已经在纸面跟踪中。") from exc
        return PaperTrackRecord(
            id=track_id,
            experiment=experiment,
            status="active",
            created_at=now,
            updated_at=now,
        )

    def list(self, *, limit: int = 100) -> list[PaperTrackRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM paper_tracks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_track(row) for row in rows]

    def get(self, track_id: str) -> PaperTrackRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM paper_tracks WHERE id = ?",
                (track_id,),
            ).fetchone()
        return self._row_to_track(row) if row else None

    def update_status(self, track_id: str, status: str) -> PaperTrackRecord | None:
        now = datetime.now(UTC)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE paper_tracks SET status = ?, updated_at = ? WHERE id = ?",
                (status, now.isoformat(), track_id),
            )
        return self.get(track_id) if cursor.rowcount else None

    def add_snapshot(
        self,
        track_id: str,
        snapshot: PaperSignalSnapshot,
    ) -> PaperTrackRecord:
        return self.add_snapshots(track_id, [snapshot])

    def add_snapshots(
        self,
        track_id: str,
        snapshots: Sequence[PaperSignalSnapshot],
        *,
        expected_snapshot_count: int | None = None,
    ) -> PaperTrackRecord:
        if not snapshots:
            raise ValueError("批量快照不能为空。")
        now = datetime.now(UTC)
        with self._lock, self._connect() as connection:
            if expected_snapshot_count is not None:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM paper_signal_snapshots "
                    "WHERE track_id = ?",
                    (track_id,),
                ).fetchone()
                actual_count = int(row["count"]) if row else 0
                if actual_count != expected_snapshot_count:
                    track = self.get(track_id)
                    if track is None:
                        raise LookupError("纸面跟踪不存在。")
                    return track
            connection.executemany(
                """
                INSERT INTO paper_signal_snapshots (id, track_id, checked_at, payload)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        snapshot.id,
                        track_id,
                        snapshot.checked_at.isoformat(),
                        json.dumps(
                            snapshot.model_dump(mode="json"),
                            ensure_ascii=False,
                        ),
                    )
                    for snapshot in snapshots
                ],
            )
            latest_checked_at = max(snapshot.checked_at for snapshot in snapshots)
            connection.execute(
                """
                UPDATE paper_tracks
                SET last_checked_at = ?, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (latest_checked_at.isoformat(), now.isoformat(), track_id),
            )
        track = self.get(track_id)
        if track is None:
            raise LookupError("纸面跟踪不存在。")
        return track

    def mark_checked(self, track_id: str, checked_at: datetime) -> PaperTrackRecord:
        now = datetime.now(UTC)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE paper_tracks
                SET last_checked_at = ?, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (checked_at.isoformat(), now.isoformat(), track_id),
            )
        track = self.get(track_id)
        if track is None:
            raise LookupError("纸面跟踪不存在。")
        return track

    def set_error(self, track_id: str, message: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE paper_tracks SET last_error = ?, updated_at = ? WHERE id = ?",
                (message[:1_000], now, track_id),
            )

    def delete(self, track_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM paper_tracks WHERE id = ?",
                (track_id,),
            )
            return cursor.rowcount > 0

    def _snapshots(
        self,
        track_id: str,
        limit: int = 20,
    ) -> Sequence[PaperSignalSnapshot]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM paper_signal_snapshots
                WHERE track_id = ? ORDER BY checked_at DESC LIMIT ?
                """,
                (track_id, limit),
            ).fetchall()
        return [
            PaperSignalSnapshot.model_validate(json.loads(row["payload"])) for row in rows
        ]

    def _snapshot_count(self, track_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM paper_signal_snapshots "
                "WHERE track_id = ?",
                (track_id,),
            ).fetchone()
        return int(row["count"]) if row else 0

    def _observed_bar_count(self, track_id: str) -> int:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM paper_signal_snapshots WHERE track_id = ?",
                (track_id,),
            ).fetchall()
        identities: set[str] = set()
        for row in rows:
            value = str(json.loads(row["payload"])["data_as_of"]).replace(
                "Z",
                "+00:00",
            )
            try:
                identities.add(datetime.fromisoformat(value).isoformat())
            except ValueError:
                identities.add(value)
        return len(identities)

    def _row_to_track(self, row: sqlite3.Row) -> PaperTrackRecord:
        experiment = ExperimentRecord.model_validate(
            json.loads(row["experiment_payload"])
        )
        snapshots = list(self._snapshots(row["id"]))
        latest_forward = snapshots[0].forward if snapshots else None
        return PaperTrackRecord(
            id=row["id"],
            experiment=experiment,
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_checked_at=(
                datetime.fromisoformat(row["last_checked_at"])
                if row["last_checked_at"]
                else None
            ),
            last_error=row["last_error"],
            snapshot_count=self._snapshot_count(row["id"]),
            observed_bar_count=self._observed_bar_count(row["id"]),
            forward_health=(
                assess_forward_health(experiment, latest_forward)
                if latest_forward
                else None
            ),
            snapshots=snapshots,
        )
