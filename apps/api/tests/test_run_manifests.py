from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from quantsieve_api.config import Settings
from quantsieve_api.run_manifests import attach_backtest_run_evidence
from quantsieve_api.schemas import BacktestRequest
from quantsieve_engine import verify_run_manifest
from quantsieve_engine.reproducibility import DatasetSnapshot, RunManifest


def request_body() -> BacktestRequest:
    return BacktestRequest(
        symbol="BTCUSDT",
        provider="binance",
        strategy_id="macd",
        parameters={"fast": 12, "slow": 26, "signal": 9},
        start=date(2026, 1, 1),
        end=date(2026, 1, 3),
        interval="1d",
    )


def payload(*, finalized: bool = True) -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "interval": "1d",
        "data_metadata": {
            "finalized_bars_only": finalized,
            "source": "binance",
        },
        "strategy": {
            "id": "macd",
            "parameters": {"fast": 12, "slow": 26, "signal": 9},
        },
        "ohlcv": [
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
        ],
        "result": {
            "metrics": {"total_return": 0.02},
            "config": {"fee_rate": 0.0003, "slippage_rate": 0.0002},
        },
        "comparison": {"excess_return": 0.01},
        "citations": [
            {
                "source": "Binance",
                "retrieved_at": "2026-01-04T00:00:00+00:00",
            }
        ],
    }


def test_attach_backtest_run_evidence_is_self_verifying() -> None:
    settings = Settings(
        build_version="test-build",
        source_revision="a" * 40,
        dependency_lock_sha256="b" * 64,
    )
    body = request_body()
    result = attach_backtest_run_evidence(
        payload(),
        request_body=body,
        settings=settings,
        provider="binance",
        canonical_symbol="BTCUSDT",
        observed_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    snapshot = DatasetSnapshot.model_validate(result["dataset_snapshot"])
    manifest = RunManifest.model_validate(result["run_manifest"])

    assert snapshot.quality_status == "passed"
    assert manifest.datasets == (snapshot,)
    assert manifest.reproducibility_status == "complete"
    assert (
        verify_run_manifest(
            manifest,
            run_request=body.model_dump(mode="json"),
            parameters={"fast": 12, "slow": 26, "signal": 9},
            engine_config={"fee_rate": 0.0003, "slippage_rate": 0.0002},
            cost_model={
                "kind": "linear_turnover",
                "fee_rate": 0.0003,
                "slippage_rate": 0.0002,
            },
            result={
                "symbol": "BTCUSDT",
                "interval": "1d",
                "resolved_symbol": "BTCUSDT",
                "resolved_provider": "binance",
                "strategy": {
                    "id": "macd",
                    "parameters": {"fast": 12, "slow": 26, "signal": 9},
                },
                "result": {
                    "metrics": {"total_return": 0.02},
                    "config": {"fee_rate": 0.0003, "slippage_rate": 0.0002},
                },
                "comparison": {"excess_return": 0.01},
            },
        )
        == ()
    )


def test_attach_backtest_run_evidence_does_not_overclaim_quality() -> None:
    result = attach_backtest_run_evidence(
        payload(finalized=False),
        request_body=request_body(),
        settings=Settings(build_version="test-build"),
        provider="binance",
        canonical_symbol="BTCUSDT",
        observed_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    snapshot = DatasetSnapshot.model_validate(result["dataset_snapshot"])
    manifest = RunManifest.model_validate(result["run_manifest"])

    assert snapshot.quality_status == "degraded"
    assert snapshot.quality_issues == ("finalized_bars_not_certified",)
    assert manifest.reproducibility_status == "incomplete"
    assert manifest.missing_requirements == (
        "source_revision",
        "dependency_lock",
        "dataset_quality",
    )


def test_attach_backtest_run_evidence_rejects_missing_rows() -> None:
    invalid = payload()
    invalid["ohlcv"] = []

    with pytest.raises(ValueError, match="at least one observation"):
        attach_backtest_run_evidence(
            invalid,
            request_body=request_body(),
            settings=Settings(),
            provider="binance",
            canonical_symbol="BTCUSDT",
        )


def test_attach_backtest_run_evidence_marks_uncaptured_warmup() -> None:
    value = payload()
    metadata = value["data_metadata"]
    assert isinstance(metadata, dict)
    metadata["indicator_warmup_available_bars"] = 60

    result = attach_backtest_run_evidence(
        value,
        request_body=request_body(),
        settings=Settings(
            build_version="test-build",
            source_revision="a" * 40,
            dependency_lock_sha256="b" * 64,
        ),
        provider="binance",
        canonical_symbol="BTCUSDT",
        observed_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    manifest = RunManifest.model_validate(result["run_manifest"])

    assert manifest.uncaptured_inputs == ("indicator_warmup",)
    assert manifest.reproducibility_status == "incomplete"
    assert manifest.missing_requirements == ("indicator_warmup",)
