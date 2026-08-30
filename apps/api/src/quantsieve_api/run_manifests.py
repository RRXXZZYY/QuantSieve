from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel
from quantsieve_engine import (
    DatasetSnapshot,
    RunManifest,
    build_dataset_snapshot,
    build_run_manifest,
)

from .config import Settings
from .research_runs import ResearchDatasetEvidence, ResearchRunStore

BACKTEST_ENGINE_VERSION = "quantsieve-vectorized-v1"


def _date_field(value: object, field_name: str) -> date | None:
    raw = (
        value.get(field_name)
        if isinstance(value, Mapping)
        else getattr(value, field_name, None)
    )
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    raise TypeError(f"{field_name} must be a date when building run evidence.")


def _request_payload(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _result_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    excluded = {
        "citations",
        "data_metadata",
        "dataset_snapshot",
        "ohlcv",
        "run_manifest",
        "run_expires_at",
        "run_id",
    }
    return {key: value for key, value in payload.items() if key not in excluded}


def _engine_config(payload: Mapping[str, Any]) -> object:
    result = payload.get("result")
    if isinstance(result, Mapping):
        config = result.get("config")
        if isinstance(config, Mapping):
            return dict(config)
    return {}


def _strategy_parameters(payload: Mapping[str, Any]) -> object:
    strategy = payload.get("strategy")
    if isinstance(strategy, Mapping):
        parameters = strategy.get("parameters")
        if isinstance(parameters, Mapping):
            return dict(parameters)
    return {}


def _cost_model(config: object) -> dict[str, object]:
    if not isinstance(config, Mapping):
        return {"kind": "unknown"}
    return {
        "kind": "linear_turnover",
        "fee_rate": config.get("fee_rate"),
        "slippage_rate": config.get("slippage_rate"),
    }


def attach_backtest_run_evidence(
    payload: Mapping[str, Any],
    *,
    request_body: object,
    settings: Settings,
    provider: str,
    canonical_symbol: str,
    run_kind: Literal["single_backtest", "custom_backtest"] = "single_backtest",
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Attach an immutable data snapshot and run manifest to one backtest payload."""

    result = dict(payload)
    result["resolved_symbol"] = canonical_symbol.strip().upper()
    result["resolved_provider"] = provider.strip().lower()
    rows = result.get("ohlcv")
    metadata = result.get("data_metadata")
    citations = result.get("citations")
    if (
        not isinstance(rows, list)
        or not all(isinstance(row, Mapping) for row in rows)
        or not isinstance(metadata, Mapping)
        or not isinstance(citations, list)
        or not all(isinstance(item, Mapping) for item in citations)
    ):
        raise ValueError("Backtest payload is missing canonical data evidence.")

    finalized_only = metadata.get("finalized_bars_only") is True
    quality_issues = () if finalized_only else ("finalized_bars_not_certified",)
    resolved_observed_at = observed_at or datetime.now(UTC)
    snapshot: DatasetSnapshot = build_dataset_snapshot(
        provider=provider,
        symbol=canonical_symbol,
        interval=str(result.get("interval", "")),
        rows=rows,
        metadata=metadata,
        citations=citations,
        requested_start=_date_field(request_body, "start"),
        requested_end=_date_field(request_body, "end"),
        finalized_only=finalized_only,
        quality_status="passed" if finalized_only else "degraded",
        quality_issues=quality_issues,
        observed_at=resolved_observed_at,
    )
    engine_config = _engine_config(result)
    execution_model = str(metadata.get("execution_model") or "next_bar_open")
    manifest: RunManifest = build_run_manifest(
        run_kind=run_kind,
        application_version=settings.build_version.strip() or "0.1.0",
        engine_version=BACKTEST_ENGINE_VERSION,
        source_revision=settings.source_revision,
        dependency_lock_hash=settings.dependency_lock_sha256,
        datasets=[snapshot],
        run_request=_request_payload(request_body),
        parameters=_strategy_parameters(result),
        engine_config=engine_config,
        cost_model=_cost_model(engine_config),
        execution_model=execution_model,
        result=_result_evidence(result),
        uncaptured_inputs=(
            ("indicator_warmup",)
            if int(metadata.get("indicator_warmup_available_bars") or 0) > 0
            else ()
        ),
        recorded_at=resolved_observed_at,
    )
    result["dataset_snapshot"] = snapshot.model_dump(mode="python")
    result["run_manifest"] = manifest.model_dump(mode="python")
    return result


def record_backtest_run(
    payload: Mapping[str, Any],
    *,
    request_body: object,
    store: ResearchRunStore,
) -> dict[str, Any]:
    """Persist one evidenced backtest and attach its server-issued receipt."""

    result = dict(payload)
    raw_snapshot = result.get("dataset_snapshot")
    raw_manifest = result.get("run_manifest")
    rows = result.get("ohlcv")
    metadata = result.get("data_metadata")
    citations = result.get("citations")
    if (
        not isinstance(raw_snapshot, Mapping)
        or not isinstance(raw_manifest, Mapping)
        or not isinstance(rows, list)
        or not all(isinstance(row, Mapping) for row in rows)
        or not isinstance(metadata, Mapping)
        or not isinstance(citations, list)
        or not all(isinstance(item, Mapping) for item in citations)
    ):
        raise ValueError("Backtest payload is missing persisted run evidence.")

    snapshot = DatasetSnapshot.model_validate(dict(raw_snapshot))
    manifest = RunManifest.model_validate(dict(raw_manifest))
    engine_config = _engine_config(result)
    receipt = store.create_run(
        manifest=manifest,
        datasets=(
            ResearchDatasetEvidence(
                snapshot=snapshot,
                rows=tuple(dict(row) for row in rows),
                metadata=dict(metadata),
                citations=tuple(dict(item) for item in citations),
            ),
        ),
        run_request=_request_payload(request_body),
        result=_result_evidence(result),
        parameters=_strategy_parameters(result),
        engine_config=engine_config,
        cost_model=_cost_model(engine_config),
    )
    authoritative_dataset = receipt.datasets[0]
    result["dataset_snapshot"] = authoritative_dataset.snapshot.model_dump(
        mode="python"
    )
    result["run_manifest"] = receipt.manifest.model_dump(mode="python")
    result["ohlcv"] = [dict(row) for row in authoritative_dataset.rows]
    result["data_metadata"] = dict(authoritative_dataset.metadata)
    result["citations"] = [
        dict(item) for item in authoritative_dataset.citations
    ]
    result["run_id"] = receipt.run_id
    result["run_expires_at"] = receipt.expires_at.isoformat()
    return result


def attach_and_record_backtest_run(
    payload: Mapping[str, Any],
    *,
    request_body: object,
    settings: Settings,
    store: ResearchRunStore,
    provider: str,
    canonical_symbol: str,
    run_kind: Literal["single_backtest", "custom_backtest"] = "single_backtest",
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Create reproducibility evidence, persist it, and return a run receipt."""

    evidenced = attach_backtest_run_evidence(
        payload,
        request_body=request_body,
        settings=settings,
        provider=provider,
        canonical_symbol=canonical_symbol,
        run_kind=run_kind,
        observed_at=observed_at,
    )
    return record_backtest_run(
        evidenced,
        request_body=request_body,
        store=store,
    )
