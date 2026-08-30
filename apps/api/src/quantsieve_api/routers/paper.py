from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from quantsieve_engine import (
    BacktestConfig,
    run_backtest,
    strategy_cycle_baseline_signals,
    strategy_signals,
    strategy_warmup_bars,
)

from ..experiments import ExperimentStore
from ..paper import (
    PaperTrackStore,
    advance_forward_state,
    initialize_forward_state,
    migrate_forward_cycle_evidence,
    revise_forward_signal,
)
from ..providers import ProviderRouter
from ..schemas import (
    ExperimentComparison,
    PaperSignalSnapshot,
    PaperTrackCreate,
    PaperTrackRecord,
    PaperTrackStatusUpdate,
)
from .market import (
    _backtest_config,
    _benchmark,
    _comparison,
    _diagnostics,
    _history_for_backtest,
)

router = APIRouter(prefix="/paper-tracks", tags=["paper-tracking"])
MAX_CATCH_UP_BARS = 500


def _paper_store(request: Request) -> PaperTrackStore:
    return cast(PaperTrackStore, request.app.state.paper_track_store)


def _experiment_store(request: Request) -> ExperimentStore:
    return cast(ExperimentStore, request.app.state.experiment_store)


def _providers(request: Request) -> ProviderRouter:
    return cast(ProviderRouter, request.app.state.providers)


def _bar_identity(value: object) -> str:
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        return text


def _bar_date(value: object) -> date:
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError as exc:
        raise ValueError(f"无法解析 K 线时间：{value}") from exc


async def refresh_track_record(
    store: PaperTrackStore,
    providers: ProviderRouter,
    track_id: str,
) -> PaperTrackRecord:
    track = store.get(track_id)
    if track is None:
        raise LookupError("纸面跟踪不存在。")
    experiment = track.experiment
    if experiment.strategy.id == "custom":
        raise ValueError("自定义策略需保存可审计代码后才能纸面跟踪。")
    adapter = providers.resolve(
        experiment.instrument.symbol,
        experiment.instrument.provider,
    )
    today = date.today()
    span_days = max((experiment.end - experiment.start).days, 30)
    window_start = today - timedelta(days=span_days)
    try:
        warmup_bars = strategy_warmup_bars(
            experiment.strategy.id,
            experiment.strategy.parameters,
        )
        history, data, signal_history, _warmup_error = await _history_for_backtest(
            adapter,
            experiment.instrument.symbol,
            window_start,
            today,
            experiment.interval,
            warmup_bars,
        )
        data = data.sort_index()
        if data.empty:
            raise ValueError("行情源没有返回可用于纸面跟踪的 K 线。")
        signals = strategy_signals(
            data,
            experiment.strategy.id,
            experiment.strategy.parameters,
            signal_history=signal_history,
        )
        cycle_baseline_signals = strategy_cycle_baseline_signals(
            experiment.strategy.id,
            signals,
            experiment.strategy.parameters,
        )
        requested_config = (
            BacktestConfig.model_validate(experiment.engine_config)
            if experiment.engine_config
            else None
        )
        config = _backtest_config(adapter, requested_config, experiment.interval)
        if experiment.strategy.id == "buy-hold":
            config = config.model_copy(update={"signal_delay_bars": 0})
        checked_at = datetime.now(UTC)
        latest_snapshot = track.snapshots[0] if track.snapshots else None
        forward_state = latest_snapshot.forward if latest_snapshot else None
        bar_identities = [_bar_identity(value) for value in data.index]
        same_bar_refresh = False
        if latest_snapshot:
            previous_identity = _bar_identity(latest_snapshot.data_as_of)
            try:
                previous_index = bar_identities.index(previous_identity)
            except ValueError as exc:
                raise ValueError(
                    "上次纸面快照已超出当前行情窗口，无法无损补齐中间 K 线；"
                    "请扩大数据窗口后再恢复跟踪。"
                ) from exc
            target_indices = list(range(previous_index + 1, len(data)))
            if not target_indices:
                target_indices = [previous_index]
                same_bar_refresh = True
            previous_cycle_baseline_requested = (
                float(cycle_baseline_signals.iloc[previous_index])
                if cycle_baseline_signals is not None
                else None
            )
            if forward_state is None:
                forward_state = initialize_forward_state(
                    bar_at=latest_snapshot.data_as_of,
                    requested_signal=float(
                        latest_snapshot.signal_state.get("requested_signal", 0)
                    ),
                    requested_cycle_baseline=previous_cycle_baseline_requested,
                    initial_equity=config.initial_cash,
                    signal_delay_bars=config.signal_delay_bars,
                    calculation_origin="migration",
                )
            elif forward_state.schema_version == 1:
                delay = forward_state.execution_delay_bars
                if cycle_baseline_signals is None:
                    executed_cycle_baseline = 0.0
                    pending_cycle_baseline_targets = [0.0] * delay
                else:
                    executed_index = previous_index - delay
                    executed_cycle_baseline = (
                        float(cycle_baseline_signals.iloc[executed_index])
                        if executed_index >= 0
                        else 0.0
                    )
                    pending_start = max(previous_index - delay + 1, 0)
                    pending_cycle_baseline_targets = [
                        float(value)
                        for value in cycle_baseline_signals.iloc[
                            pending_start : previous_index + 1
                        ]
                    ]
                    pending_cycle_baseline_targets = [
                        0.0
                    ] * (delay - len(pending_cycle_baseline_targets)) + (
                        pending_cycle_baseline_targets
                    )
                    # A forward ledger activates from cash, independently of
                    # the rolling history used to diagnose the strategy. Cap
                    # reconstructed core state by its actually executed paper
                    # strategy state and queue.
                    executed_cycle_baseline = min(
                        executed_cycle_baseline,
                        forward_state.position,
                    )
                    pending_cycle_baseline_targets = [
                        min(baseline, target)
                        for baseline, target in zip(
                            pending_cycle_baseline_targets,
                            forward_state.pending_targets,
                            strict=True,
                        )
                    ]
                forward_state = migrate_forward_cycle_evidence(
                    forward_state,
                    bar_at=latest_snapshot.data_as_of,
                    cycle_baseline_position=executed_cycle_baseline,
                    pending_cycle_baseline_targets=(
                        pending_cycle_baseline_targets
                    ),
                    uses_cycle_baseline=cycle_baseline_signals is not None,
                )
        else:
            target_indices = [len(data) - 1]
        target_indices = target_indices[:MAX_CATCH_UP_BARS]

        snapshots: list[PaperSignalSnapshot] = []
        for offset, target_index in enumerate(target_indices):
            prefix = data.iloc[: target_index + 1]
            prefix_signals = signals.iloc[: target_index + 1]
            prefix_cycle_baseline = (
                cycle_baseline_signals.iloc[: target_index + 1]
                if cycle_baseline_signals is not None
                else None
            )
            result = run_backtest(
                prefix,
                prefix_signals,
                config,
                cycle_baseline_signals=prefix_cycle_baseline,
            )
            _, benchmark = _benchmark(prefix, config)
            diagnostics = _diagnostics(prefix, prefix_signals, result, benchmark)
            latest_state = diagnostics["signal_state"]
            if (
                same_bar_refresh
                and latest_snapshot
                and latest_snapshot.forward is not None
                and latest_snapshot.forward.schema_version == 2
                and latest_snapshot.signal_state.get("requested_signal")
                == latest_state.get("requested_signal")
                and latest_snapshot.signal_state.get("executed_position")
                == latest_state.get("executed_position")
            ):
                return store.mark_checked(track_id, checked_at)
            bar_at = data.index[target_index]
            requested_signal = float(latest_state.get("requested_signal", 0))
            requested_cycle_baseline = (
                float(cycle_baseline_signals.iloc[target_index])
                if cycle_baseline_signals is not None
                else None
            )
            if forward_state is None:
                forward_state = initialize_forward_state(
                    bar_at=bar_at,
                    requested_signal=requested_signal,
                    requested_cycle_baseline=requested_cycle_baseline,
                    initial_equity=config.initial_cash,
                    signal_delay_bars=config.signal_delay_bars,
                )
            elif same_bar_refresh:
                forward_state = revise_forward_signal(
                    forward_state,
                    requested_signal,
                    requested_cycle_baseline,
                )
            else:
                forward_state = advance_forward_state(
                    forward_state,
                    bar_at=bar_at,
                    previous_close=float(data.iloc[target_index - 1]["close"]),
                    open_price=float(data.iloc[target_index]["open"]),
                    close_price=float(data.iloc[target_index]["close"]),
                    requested_signal=requested_signal,
                    requested_cycle_baseline=requested_cycle_baseline,
                    fee_rate=config.fee_rate,
                    slippage_rate=config.slippage_rate,
                )
            snapshots.append(
                PaperSignalSnapshot(
                    id=uuid4().hex,
                    track_id=track_id,
                    checked_at=checked_at
                    - timedelta(microseconds=len(target_indices) - offset - 1),
                    data_as_of=str(bar_at.isoformat()),
                    window_start=window_start,
                    window_end=_bar_date(bar_at),
                    latest_price=float(data.iloc[target_index]["close"]),
                    interval=experiment.interval,
                    signal_state=latest_state,
                    metrics=result.metrics,
                    benchmark_metrics=benchmark.metrics,
                    comparison=ExperimentComparison.model_validate(
                        _comparison(result, benchmark)
                    ),
                    diagnostics=diagnostics,
                    citations=[
                        item.model_dump(mode="json") for item in history.citations
                    ],
                    forward=forward_state,
                )
            )
        return store.add_snapshots(
            track_id,
            snapshots,
            expected_snapshot_count=track.snapshot_count,
        )
    except (RuntimeError, ValueError, KeyError, LookupError) as exc:
        store.set_error(track_id, str(exc))
        raise


async def _refresh_track(request: Request, track_id: str) -> PaperTrackRecord:
    try:
        return await refresh_track_record(
            _paper_store(request),
            _providers(request),
            track_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class PaperTrackScheduler:
    def __init__(
        self,
        store: PaperTrackStore,
        providers: ProviderRouter,
        *,
        enabled: bool,
        poll_seconds: int,
    ) -> None:
        self.store = store
        self.providers = providers
        self.enabled = enabled
        self.poll_seconds = max(poll_seconds, 30)
        self.task: asyncio.Task[None] | None = None
        self.last_run_at: datetime | None = None
        self.last_refreshed = 0
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self) -> None:
        if self.enabled and not self.running:
            self.task = asyncio.create_task(self._run(), name="paper-track-scheduler")

    async def stop(self) -> None:
        if self.task is None:
            return
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        self.task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh_due()
            except Exception as exc:  # pragma: no cover - defensive task boundary
                self.last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(self.poll_seconds)

    async def refresh_due(self, *, now: datetime | None = None) -> int:
        checked_at = now or datetime.now(UTC)
        refreshed = 0
        errors: list[str] = []
        for track in self.store.list(limit=200):
            if track.status != "active" or not self._is_due(track, checked_at):
                continue
            try:
                await refresh_track_record(self.store, self.providers, track.id)
                refreshed += 1
            except (RuntimeError, ValueError, KeyError, LookupError) as exc:
                errors.append(f"{track.id}: {exc}")
        self.last_run_at = checked_at
        self.last_refreshed = refreshed
        self.last_error = "; ".join(errors) if errors else None
        return refreshed

    @staticmethod
    def _is_due(track: PaperTrackRecord, now: datetime) -> bool:
        if track.last_checked_at is None:
            return True
        cadence = {
            "15m": timedelta(minutes=15),
            "1h": timedelta(hours=1),
            "4h": timedelta(hours=4),
            "1d": timedelta(hours=1),
            "1wk": timedelta(hours=6),
        }[track.experiment.interval]
        return now - track.last_checked_at >= cadence

    def status(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "poll_seconds": self.poll_seconds,
            "last_run_at": (
                self.last_run_at.isoformat() if self.last_run_at else None
            ),
            "last_refreshed": self.last_refreshed,
            "last_error": self.last_error,
        }


@router.get("")
async def list_paper_tracks(
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[PaperTrackRecord]:
    return _paper_store(request).list(limit=limit)


@router.get("/scheduler")
async def paper_scheduler_status(request: Request) -> dict[str, object]:
    scheduler = cast(PaperTrackScheduler, request.app.state.paper_track_scheduler)
    return scheduler.status()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_paper_track(
    request: Request,
    body: PaperTrackCreate,
) -> PaperTrackRecord:
    experiment = _experiment_store(request).get(body.experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="实验记录不存在。")
    if experiment.strategy.id == "custom":
        raise HTTPException(
            status_code=422,
            detail="自定义策略需保存可审计代码后才能纸面跟踪。",
        )
    validation = experiment.validation
    if validation is None:
        raise HTTPException(
            status_code=422,
            detail="纸面跟踪需要先完成样本外验证，不能从未验证的历史结果直接启动。",
        )
    exposure_matched = validation.validation_exposure_matched_benchmark_metrics
    timing_passed = (
        exposure_matched is not None
        and validation.validation_metrics.total_return
        > exposure_matched.total_return
    )
    validated_candidate = (
        validation.validation_passed
        and validation.validation_metrics.closed_trades >= 10
        and timing_passed
    )
    provisional_candidate = (
        not validation.validation_passed
        and validation.forward_observation_eligible
        and validation.validation_code == "sample_insufficient"
        and 5 <= validation.validation_metrics.closed_trades < 10
        and timing_passed
    )
    if not validated_candidate and not provisional_candidate:
        raise HTTPException(
            status_code=422,
            detail="该实验未通过最终留出验证，也不满足冻结参数前向观察条件。",
        )
    try:
        track = _paper_store(request).create(experiment)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        return await _refresh_track(request, track.id)
    except HTTPException:
        persisted = _paper_store(request).get(track.id)
        if persisted is None:
            raise
        return persisted


@router.post("/{track_id}/refresh")
async def refresh_paper_track(request: Request, track_id: str) -> PaperTrackRecord:
    return await _refresh_track(request, track_id)


@router.patch("/{track_id}")
async def update_paper_track(
    request: Request,
    track_id: str,
    body: PaperTrackStatusUpdate,
) -> PaperTrackRecord:
    track = _paper_store(request).update_status(track_id, body.status)
    if track is None:
        raise HTTPException(status_code=404, detail="纸面跟踪不存在。")
    return track


@router.delete("/{track_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_paper_track(request: Request, track_id: str) -> Response:
    if not _paper_store(request).delete(track_id):
        raise HTTPException(status_code=404, detail="纸面跟踪不存在。")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
