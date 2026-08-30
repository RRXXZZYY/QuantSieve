from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from quantsieve_engine.reproducibility import (
    DatasetSnapshot,
    RunManifest,
    build_dataset_snapshot,
    build_run_manifest,
    canonical_json,
    canonical_payload_hash,
    verify_run_manifest,
)


def rows() -> list[dict[str, object]]:
    return [
        {
            "date": "2026-01-02T00:00:00+00:00",
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "volume": 1_000.0,
        },
        {
            "date": "2026-01-03T00:00:00+00:00",
            "open": 101.0,
            "high": 104.0,
            "low": 100.0,
            "close": 103.0,
            "volume": 1_200.0,
        },
    ]


def snapshot(**changes: object) -> DatasetSnapshot:
    values: dict[str, object] = {
        "provider": "binance",
        "symbol": "btcusdt",
        "interval": "1d",
        "rows": rows(),
        "metadata": {"finalized": True, "currency": "USDT"},
        "citations": [{"source": "Binance", "url": "https://example.test/data"}],
        "requested_start": date(2026, 1, 1),
        "requested_end": date(2026, 1, 3),
        "observed_at": datetime(2026, 1, 4, tzinfo=UTC),
    }
    values.update(changes)
    return build_dataset_snapshot(**values)  # type: ignore[arg-type]


def manifest(**changes: object) -> RunManifest:
    values: dict[str, object] = {
        "run_kind": "single_backtest",
        "application_version": "test-build",
        "engine_version": "vectorized-v1",
        "source_revision": "a" * 40,
        "dependency_lock_hash": "b" * 64,
        "datasets": [snapshot()],
        "run_request": {"symbol": "BTCUSDT", "interval": "1d"},
        "parameters": {"fast": 12, "slow": 26, "signal": 9},
        "engine_config": {"fee_rate": 0.0003, "slippage_rate": 0.0002},
        "cost_model": {"kind": "linear_turnover", "rate": 0.0005},
        "execution_model": "next_bar_open",
        "result": {"total_return": 0.25, "max_drawdown": -0.12},
        "recorded_at": datetime(2026, 1, 4, tzinfo=UTC),
    }
    values.update(changes)
    return build_run_manifest(**values)  # type: ignore[arg-type]


def test_canonical_json_is_order_independent_and_strict() -> None:
    left = {"b": [Decimal("1.2300"), -0.0], "a": datetime(2026, 1, 1, tzinfo=UTC)}
    right = {"a": datetime(2026, 1, 1, tzinfo=UTC), "b": [Decimal("1.23"), 0.0]}

    assert canonical_json(left) == canonical_json(right)
    assert canonical_payload_hash(left) == canonical_payload_hash(right)
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json({"bad": float("nan")})
    with pytest.raises(TypeError, match="string keys"):
        canonical_json({1: "bad"})


def test_dataset_snapshot_is_content_addressed_and_normalized() -> None:
    first = snapshot()
    second = snapshot(observed_at=datetime(2026, 1, 5, tzinfo=UTC))

    assert first.snapshot_id == second.snapshot_id
    assert first.schema_version == 2
    assert first.provider == "binance"
    assert first.symbol == "BTCUSDT"
    assert first.row_count == 2
    assert first.first_observation_at == datetime(2026, 1, 2, tzinfo=UTC)
    assert first.last_observation_at == datetime(2026, 1, 3, tzinfo=UTC)
    assert first.observed_at != second.observed_at


def test_dataset_snapshot_v2_identity_tracks_all_acquisition_evidence() -> None:
    original = snapshot()
    changed_rows = rows()
    changed_rows[1]["close"] = 103.5

    assert snapshot(rows=changed_rows).snapshot_id != original.snapshot_id
    metadata_change = snapshot(
        metadata={"finalized": True, "currency": "USD"},
        citations=[{"source": "Mirror", "url": "https://example.test/mirror"}],
    )
    assert metadata_change.snapshot_id != original.snapshot_id
    assert metadata_change.metadata_hash != original.metadata_hash
    assert metadata_change.citations_hash != original.citations_hash


def test_dataset_snapshot_ignores_dynamic_source_fields_in_analysis_hash() -> None:
    original = snapshot()
    dynamic_rows = rows()
    dynamic_rows[0]["retrieved_at"] = "2026-01-04T01:00:00+00:00"

    changed = snapshot(rows=dynamic_rows)

    assert changed.snapshot_id != original.snapshot_id
    assert changed.analysis_data_hash == original.analysis_data_hash
    assert changed.source_rows_hash != original.source_rows_hash


def test_dataset_snapshot_v2_identity_tracks_requested_range() -> None:
    original = snapshot()

    assert (
        snapshot(requested_start=date(2026, 1, 2)).snapshot_id
        != original.snapshot_id
    )
    assert (
        snapshot(requested_end=date(2026, 1, 4)).snapshot_id
        != original.snapshot_id
    )


def test_dataset_snapshot_v1_remains_valid_for_stored_run_compatibility() -> None:
    original = snapshot(schema_version=1)
    metadata_change = snapshot(
        schema_version=1,
        metadata={"finalized": True, "currency": "USD"},
        citations=[{"source": "Mirror", "url": "https://example.test/mirror"}],
    )

    assert original.schema_version == 1
    assert metadata_change.snapshot_id == original.snapshot_id
    assert DatasetSnapshot.model_validate(
        original.model_dump(mode="python")
    ) == original


def test_dataset_snapshot_rejects_bad_observation_order_and_quality_claims() -> None:
    with pytest.raises(ValueError, match="ordered by date"):
        snapshot(rows=list(reversed(rows())))
    with pytest.raises(ValueError, match="repeat observation dates"):
        snapshot(rows=[rows()[0], rows()[0]])
    with pytest.raises(ValidationError, match="finalized"):
        snapshot(finalized_only=False)
    with pytest.raises(ValidationError, match="must explain"):
        snapshot(quality_status="degraded")
    invalid_ohlc = rows()
    invalid_ohlc[0]["high"] = 98
    with pytest.raises(ValueError, match="high must cover"):
        snapshot(rows=invalid_ohlc)


def test_dataset_snapshot_rejects_tampered_identity() -> None:
    original = snapshot()
    payload = original.model_dump(mode="python")
    payload["row_count"] = 3

    with pytest.raises(ValidationError, match="snapshot_id"):
        DatasetSnapshot.model_validate(payload)


def test_run_manifest_is_stable_across_recording_time() -> None:
    first = manifest()
    second = manifest(recorded_at=datetime(2026, 1, 5, tzinfo=UTC))

    assert first.manifest_id == second.manifest_id
    assert first.reproducibility_status == "complete"
    assert first.missing_requirements == ()
    assert first.recorded_at != second.recorded_at


def test_run_manifest_accepts_factor_research_as_a_distinct_run_kind() -> None:
    result = manifest(run_kind="factor_research")

    assert result.run_kind == "factor_research"
    assert result.reproducibility_status == "complete"


def test_run_manifest_reports_missing_reproducibility_inputs_honestly() -> None:
    result = manifest(source_revision=None, dependency_lock_hash=None)

    assert result.reproducibility_status == "incomplete"
    assert result.missing_requirements == (
        "source_revision",
        "dependency_lock",
    )


def test_run_manifest_requires_seed_exactly_for_randomized_runs() -> None:
    with pytest.raises(ValidationError, match="require a seed"):
        manifest(randomness_used=True)
    with pytest.raises(ValidationError, match="must omit"):
        manifest(random_seed=7)

    randomized = manifest(randomness_used=True, random_seed=7)
    assert randomized.random_seed == 7


def test_run_manifest_detects_tampering() -> None:
    original = manifest()
    payload = original.model_dump(mode="python")
    payload["execution_model"] = "same_bar_close"

    with pytest.raises(ValidationError, match="manifest_id"):
        RunManifest.model_validate(payload)


def test_verify_run_manifest_returns_stable_mismatch_codes() -> None:
    original = manifest()

    assert (
        verify_run_manifest(
            original,
            run_request={"symbol": "BTCUSDT", "interval": "1d"},
            parameters={"fast": 12, "slow": 26, "signal": 9},
            engine_config={"fee_rate": 0.0003, "slippage_rate": 0.0002},
            cost_model={"kind": "linear_turnover", "rate": 0.0005},
            result={"total_return": 0.25, "max_drawdown": -0.12},
        )
        == ()
    )
    assert verify_run_manifest(
        original,
        run_request={"symbol": "ETHUSDT", "interval": "1d"},
        parameters={"fast": 10, "slow": 30, "signal": 9},
        engine_config={"fee_rate": 0.0003, "slippage_rate": 0.0002},
        cost_model={"kind": "linear_turnover", "rate": 0.0005},
        result={"total_return": 0.25, "max_drawdown": -0.12},
    ) == ("run_request_hash", "parameters_hash")
