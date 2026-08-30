from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
import quantsieve_api.research_runs as research_runs_module
from quantsieve_api.research_runs import (
    ResearchDatasetEvidence,
    ResearchRunConflictError,
    ResearchRunExpiredError,
    ResearchRunIntegrityError,
    ResearchRunNotFoundError,
    ResearchRunStore,
)
from quantsieve_engine import (
    DatasetSnapshot,
    RunManifest,
    build_dataset_snapshot,
    build_run_manifest,
    canonical_json,
)

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
ROWS: tuple[dict[str, object], ...] = (
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
)
METADATA: dict[str, object] = {
    "finalized_bars_only": True,
    "market_calendar": "24/7",
}
CITATIONS: tuple[dict[str, object], ...] = (
    {
        "source": "test-provider",
        "retrieved_at": "2026-07-29T11:59:00+00:00",
    },
)
RUN_REQUEST: dict[str, object] = {
    "symbol": "BTCUSDT",
    "provider": "binance",
    "start": "2026-01-01",
    "end": "2026-01-03",
    "interval": "1d",
}
PARAMETERS: dict[str, object] = {"fast": 12, "slow": 26, "signal": 9}
ENGINE_CONFIG: dict[str, object] = {
    "fee_rate": 0.0003,
    "slippage_rate": 0.0002,
    "signal_delay_bars": 1,
}
COST_MODEL: dict[str, object] = {
    "kind": "linear_turnover",
    "fee_rate": 0.0003,
    "slippage_rate": 0.0002,
}
RESULT: dict[str, object] = {
    "symbol": "BTCUSDT",
    "metrics": {"total_return": 0.02, "max_drawdown": -0.01},
}


class Clock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


def snapshot(
    *,
    symbol: str = "BTCUSDT",
    rows: tuple[dict[str, object], ...] = ROWS,
    requested_start: date = date(2026, 1, 1),
    observed_at: datetime = NOW,
    schema_version: Literal[1, 2] = 2,
) -> DatasetSnapshot:
    return build_dataset_snapshot(
        provider="binance",
        symbol=symbol,
        interval="1d",
        rows=rows,
        metadata=METADATA,
        citations=CITATIONS,
        requested_start=requested_start,
        requested_end=date(2026, 1, 3),
        finalized_only=True,
        quality_status="passed",
        observed_at=observed_at,
        schema_version=schema_version,
    )


def evidence(
    item: DatasetSnapshot | None = None,
    *,
    rows: tuple[dict[str, object], ...] = ROWS,
    role: str = "evaluation",
    ordinal: int = 0,
) -> ResearchDatasetEvidence:
    return ResearchDatasetEvidence(
        snapshot=item or snapshot(rows=rows),
        rows=rows,
        metadata=dict(METADATA),
        citations=tuple(dict(citation) for citation in CITATIONS),
        role=role,
        ordinal=ordinal,
    )


def manifest(
    datasets: tuple[ResearchDatasetEvidence, ...],
    *,
    recorded_at: datetime = NOW,
) -> RunManifest:
    return build_run_manifest(
        run_kind="single_backtest",
        application_version="test-build",
        engine_version="quantsieve-vectorized-v1",
        source_revision="a" * 40,
        dependency_lock_hash="b" * 64,
        datasets=[item.snapshot for item in datasets],
        run_request=RUN_REQUEST,
        parameters=PARAMETERS,
        engine_config=ENGINE_CONFIG,
        cost_model=COST_MODEL,
        execution_model="next_bar_open",
        result=RESULT,
        recorded_at=recorded_at,
    )


def create_run(
    store: ResearchRunStore,
    datasets: tuple[ResearchDatasetEvidence, ...] | None = None,
    *,
    run_manifest: RunManifest | None = None,
):
    safe_datasets = datasets or (evidence(),)
    return store.create_run(
        manifest=run_manifest or manifest(safe_datasets),
        datasets=safe_datasets,
        run_request=RUN_REQUEST,
        result=RESULT,
        parameters=PARAMETERS,
        engine_config=ENGINE_CONFIG,
        cost_model=COST_MODEL,
    )


def create_distinct_run(
    store: ResearchRunStore,
    key: str,
    *,
    datasets: tuple[ResearchDatasetEvidence, ...] | None = None,
):
    safe_datasets = datasets or (evidence(snapshot(symbol=f"ASSET{key.upper()}")),)
    run_request = {
        **RUN_REQUEST,
        "run_key": key,
        "symbol": safe_datasets[0].snapshot.symbol,
    }
    result = {
        **RESULT,
        "run_key": key,
        "symbol": safe_datasets[0].snapshot.symbol,
    }
    run_manifest = build_run_manifest(
        run_kind="single_backtest",
        application_version="test-build",
        engine_version="quantsieve-vectorized-v1",
        source_revision="a" * 40,
        dependency_lock_hash="b" * 64,
        datasets=[item.snapshot for item in safe_datasets],
        run_request=run_request,
        parameters=PARAMETERS,
        engine_config=ENGINE_CONFIG,
        cost_model=COST_MODEL,
        execution_model="next_bar_open",
        result=result,
        recorded_at=NOW,
    )
    return store.create_run(
        manifest=run_manifest,
        datasets=safe_datasets,
        run_request=run_request,
        result=result,
        parameters=PARAMETERS,
        engine_config=ENGINE_CONFIG,
        cost_model=COST_MODEL,
    )


def connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    return connection


def test_create_run_deadline_rolls_back_all_evidence_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "deadline-rollback.db"
    store = ResearchRunStore(database, clock=Clock())
    monotonic_clock = {"now": 0.0}
    original_cleanup = store._cleanup_expired_runs

    monkeypatch.setattr(
        research_runs_module,
        "monotonic",
        lambda: monotonic_clock["now"],
    )

    def expire_before_commit(
        connection: sqlite3.Connection,
        *,
        now: datetime,
        current_run_id: str,
    ) -> None:
        original_cleanup(
            connection,
            now=now,
            current_run_id=current_run_id,
        )
        monotonic_clock["now"] = 2.0

    monkeypatch.setattr(store, "_cleanup_expired_runs", expire_before_commit)
    datasets = (evidence(),)

    with pytest.raises(TimeoutError, match="deadline"):
        store.create_run(
            manifest=manifest(datasets),
            datasets=datasets,
            run_request=RUN_REQUEST,
            result=RESULT,
            parameters=PARAMETERS,
            engine_config=ENGINE_CONFIG,
            cost_model=COST_MODEL,
            deadline_monotonic=1.0,
        )

    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM dataset_snapshots").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM research_run_dataset_evidence").fetchone()[0]
            == 0
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_research_run_round_trip_persists_canonical_authoritative_evidence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "research-runs.db"
    clock = Clock()
    store = ResearchRunStore(database, run_ttl=timedelta(hours=2), clock=clock)
    first = evidence()
    warmup_rows = (
        {
            "date": "2026-01-01T00:00:00+00:00",
            "open": 98.0,
            "high": 101.0,
            "low": 97.0,
            "close": 100.0,
            "volume": 900.0,
        },
    )
    warmup_snapshot = snapshot(
        rows=warmup_rows,
        requested_start=date(2025, 12, 1),
    )
    warmup = evidence(
        warmup_snapshot,
        rows=warmup_rows,
        role="indicator_warmup",
    )
    datasets = (first, warmup)

    created = create_run(store, datasets)
    loaded = store.get_run(created.run_id)

    assert loaded == created
    assert loaded.created_at == NOW
    assert loaded.expires_at == NOW + timedelta(hours=2)
    assert [item.role for item in loaded.datasets] == [
        "evaluation",
        "indicator_warmup",
    ]
    assert loaded.run_request == RUN_REQUEST
    assert loaded.result == RESULT

    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM dataset_snapshots").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM research_run_datasets").fetchone()[0] == 2
        payload_columns = connection.execute(
            """
            SELECT
                manifest_payload, request_payload, result_payload,
                parameters_payload, engine_config_payload, cost_model_payload
            FROM research_runs
            """
        ).fetchone()
        dataset_columns = connection.execute(
            """
            SELECT
                descriptor_payload, rows_payload, metadata_payload, citations_payload
            FROM dataset_snapshots
            ORDER BY snapshot_id
            """
        ).fetchall()
    assert payload_columns is not None
    for value in payload_columns:
        assert value == canonical_json(json.loads(value))
    for row in dataset_columns:
        for value in row:
            assert value == canonical_json(json.loads(value))


def test_store_enables_required_sqlite_durability_guards(tmp_path: Path) -> None:
    store = ResearchRunStore(tmp_path / "durability.db")
    connection = store._connect()
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert {
        "dataset_snapshots",
        "research_runs",
        "research_run_datasets",
        "research_run_dataset_evidence",
    }.issubset(tables)


def test_duplicate_manifest_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "idempotent.db"
    store = ResearchRunStore(database, clock=Clock())
    datasets = (evidence(),)
    run_manifest = manifest(datasets)

    first = create_run(store, datasets, run_manifest=run_manifest)
    second = create_run(store, datasets, run_manifest=run_manifest)

    assert second == first
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM dataset_snapshots").fetchone()[0] == 1


def test_duplicate_manifest_ignores_fresh_observation_envelope(tmp_path: Path) -> None:
    database = tmp_path / "reobserved.db"
    store = ResearchRunStore(database, clock=Clock())
    first_datasets = (evidence(),)
    first = create_run(
        store,
        first_datasets,
        run_manifest=manifest(first_datasets),
    )
    later_snapshot = snapshot(observed_at=NOW + timedelta(minutes=1))
    later_datasets = (evidence(later_snapshot),)
    later_manifest = manifest(
        later_datasets,
        recorded_at=NOW + timedelta(minutes=1),
    )

    assert later_manifest.manifest_id == first.manifest.manifest_id
    replay = create_run(
        store,
        later_datasets,
        run_manifest=later_manifest,
    )

    assert replay.run_id == first.run_id
    assert replay.manifest == first.manifest


def test_rerunning_an_expired_identical_manifest_renews_its_receipt(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = ResearchRunStore(
        tmp_path / "renewed.db",
        run_ttl=timedelta(minutes=5),
        expired_run_retention_grace=timedelta(0),
        cleanup_batch_limit=1,
        clock=clock,
    )
    datasets = (evidence(),)
    run_manifest = manifest(datasets)
    first = create_run(store, datasets, run_manifest=run_manifest)
    clock.current = NOW + timedelta(minutes=6)

    renewed = create_run(store, datasets, run_manifest=run_manifest)

    assert renewed.run_id == first.run_id
    assert renewed.created_at == first.created_at
    assert renewed.expires_at == clock.current + timedelta(minutes=5)
    assert renewed.expires_at > first.expires_at
    assert store.get_run(first.run_id).expires_at == renewed.expires_at
    with connect(store.path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM research_runs WHERE run_id = ?",
                (first.run_id,),
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM dataset_snapshots").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_recently_expired_run_is_retained_during_create_cleanup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recent-expiry.db"
    clock = Clock()
    store = ResearchRunStore(
        database,
        run_ttl=timedelta(minutes=5),
        expired_run_retention_grace=timedelta(minutes=10),
        cleanup_batch_limit=1,
        clock=clock,
    )
    recent = create_distinct_run(store, "recent")
    clock.current = NOW + timedelta(minutes=14)

    current = create_distinct_run(store, "current")

    with pytest.raises(ResearchRunExpiredError):
        store.get_run(recent.run_id)
    assert store.get_run(current.run_id).run_id == current.run_id
    with connect(database) as connection:
        run_ids = {
            row[0] for row in connection.execute("SELECT run_id FROM research_runs").fetchall()
        }
        assert run_ids == {recent.run_id, current.run_id}
        assert connection.execute("SELECT COUNT(*) FROM dataset_snapshots").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_run_beyond_retention_grace_and_its_orphan_snapshot_are_deleted(
    tmp_path: Path,
) -> None:
    database = tmp_path / "expired-cleanup.db"
    clock = Clock()
    store = ResearchRunStore(
        database,
        run_ttl=timedelta(minutes=5),
        expired_run_retention_grace=timedelta(minutes=10),
        cleanup_batch_limit=10,
        clock=clock,
    )
    expired = create_distinct_run(store, "expired")
    expired_snapshot_id = expired.datasets[0].snapshot.snapshot_id
    clock.current = NOW + timedelta(minutes=16)

    current = create_distinct_run(store, "current")

    with pytest.raises(ResearchRunNotFoundError):
        store.get_run(expired.run_id)
    with connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM research_runs WHERE run_id = ?",
                (current.run_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM dataset_snapshots WHERE snapshot_id = ?",
                (expired_snapshot_id,),
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM dataset_snapshots").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cleanup_preserves_snapshot_shared_with_a_live_run(tmp_path: Path) -> None:
    database = tmp_path / "shared-snapshot.db"
    clock = Clock()
    store = ResearchRunStore(
        database,
        run_ttl=timedelta(minutes=10),
        expired_run_retention_grace=timedelta(minutes=5),
        cleanup_batch_limit=10,
        clock=clock,
    )
    shared_datasets = (evidence(),)
    expired = create_distinct_run(
        store,
        "shared-expired",
        datasets=shared_datasets,
    )
    shared_snapshot_id = expired.datasets[0].snapshot.snapshot_id
    clock.current = NOW + timedelta(minutes=11)
    live = create_distinct_run(
        store,
        "shared-live",
        datasets=shared_datasets,
    )
    clock.current = NOW + timedelta(minutes=16)

    trigger = create_distinct_run(store, "trigger")

    with pytest.raises(ResearchRunNotFoundError):
        store.get_run(expired.run_id)
    assert store.get_run(live.run_id).run_id == live.run_id
    assert store.get_run(trigger.run_id).run_id == trigger.run_id
    with connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM dataset_snapshots WHERE snapshot_id = ?",
                (shared_snapshot_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM research_run_datasets WHERE snapshot_id = ?",
                (shared_snapshot_id,),
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cleanup_batch_limit_bounds_each_create_transaction(tmp_path: Path) -> None:
    database = tmp_path / "bounded-cleanup.db"
    clock = Clock()
    store = ResearchRunStore(
        database,
        run_ttl=timedelta(minutes=1),
        expired_run_retention_grace=timedelta(0),
        cleanup_batch_limit=2,
        clock=clock,
    )
    expired_runs = tuple(create_distinct_run(store, f"expired-{index}") for index in range(3))
    expired_ids = {run.run_id for run in expired_runs}
    clock.current = NOW + timedelta(minutes=2)

    first_trigger = create_distinct_run(store, "trigger-1")

    with connect(database) as connection:
        remaining_expired = {
            row[0]
            for row in connection.execute(
                "SELECT run_id FROM research_runs WHERE run_id IN (?, ?, ?)",
                tuple(expired_ids),
            ).fetchall()
        }
        assert len(remaining_expired) == 1
        assert connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM dataset_snapshots").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    second_trigger = create_distinct_run(store, "trigger-2")

    with connect(database) as connection:
        remaining_expired = {
            row[0]
            for row in connection.execute(
                "SELECT run_id FROM research_runs WHERE run_id IN (?, ?, ?)",
                tuple(expired_ids),
            ).fetchall()
        }
        assert not remaining_expired
        assert {
            row[0] for row in connection.execute("SELECT run_id FROM research_runs").fetchall()
        } == {first_trigger.run_id, second_trigger.run_id}
        assert connection.execute("SELECT COUNT(*) FROM dataset_snapshots").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_same_snapshot_id_with_different_descriptor_fails_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "snapshot-conflict.db"
    store = ResearchRunStore(database, clock=Clock())
    first_snapshot = snapshot(
        requested_start=date(2026, 1, 1),
        schema_version=1,
    )
    second_snapshot = snapshot(
        requested_start=date(2025, 12, 31),
        schema_version=1,
    )
    assert first_snapshot.snapshot_id == second_snapshot.snapshot_id
    first_datasets = (evidence(first_snapshot),)
    second_datasets = (evidence(second_snapshot),)

    create_run(store, first_datasets, run_manifest=manifest(first_datasets))
    with pytest.raises(ResearchRunConflictError, match="different evidence"):
        create_run(store, second_datasets, run_manifest=manifest(second_datasets))

    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM dataset_snapshots").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 1


def test_legacy_v1_duplicate_manifest_compares_complete_evidence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-duplicate-evidence.db"
    store = ResearchRunStore(database, clock=Clock())
    first_snapshot = build_dataset_snapshot(
        provider="binance",
        symbol="BTCUSDT",
        interval="1d",
        rows=ROWS,
        metadata=METADATA,
        citations=CITATIONS,
        requested_start=date(2026, 1, 1),
        requested_end=date(2026, 1, 3),
        observed_at=NOW,
        schema_version=1,
    )
    changed_metadata = {**METADATA, "execution_context": "changed"}
    second_snapshot = build_dataset_snapshot(
        provider="binance",
        symbol="BTCUSDT",
        interval="1d",
        rows=ROWS,
        metadata=changed_metadata,
        citations=CITATIONS,
        requested_start=date(2026, 1, 1),
        requested_end=date(2026, 1, 3),
        observed_at=NOW,
        schema_version=1,
    )
    assert first_snapshot.snapshot_id == second_snapshot.snapshot_id
    first_evidence = evidence(first_snapshot)
    second_evidence = ResearchDatasetEvidence(
        snapshot=second_snapshot,
        rows=ROWS,
        metadata=changed_metadata,
        citations=CITATIONS,
    )
    first_manifest = manifest((first_evidence,))
    second_manifest = manifest((second_evidence,))
    assert first_manifest.manifest_id == second_manifest.manifest_id

    created = create_run(
        store,
        (first_evidence,),
        run_manifest=first_manifest,
    )
    with pytest.raises(
        ResearchRunConflictError,
        match="different dataset evidence",
    ):
        create_run(
            store,
            (second_evidence,),
            run_manifest=second_manifest,
        )

    assert store.get_run(created.run_id).datasets[0].metadata == METADATA


def test_v2_evidence_change_creates_distinct_snapshot_manifest_and_receipt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "evidence-envelopes.db"
    store = ResearchRunStore(database, clock=Clock())
    first_evidence = evidence()
    first_request = {**RUN_REQUEST, "strategy_id": "macd"}
    first_manifest = build_run_manifest(
        run_kind="single_backtest",
        application_version="test-build",
        engine_version="quantsieve-vectorized-v1",
        source_revision="a" * 40,
        dependency_lock_hash="b" * 64,
        datasets=[first_evidence.snapshot],
        run_request=first_request,
        parameters=PARAMETERS,
        engine_config=ENGINE_CONFIG,
        cost_model=COST_MODEL,
        execution_model="next_bar_open",
        result=RESULT,
        recorded_at=NOW,
    )
    first = store.create_run(
        manifest=first_manifest,
        datasets=(first_evidence,),
        run_request=first_request,
        result=RESULT,
        parameters=PARAMETERS,
        engine_config=ENGINE_CONFIG,
        cost_model=COST_MODEL,
    )

    second_metadata = {**METADATA, "execution_context": "fixed_shares"}
    second_snapshot = build_dataset_snapshot(
        provider="binance",
        symbol="BTCUSDT",
        interval="1d",
        rows=ROWS,
        metadata=second_metadata,
        citations=CITATIONS,
        requested_start=date(2026, 1, 1),
        requested_end=date(2026, 1, 3),
        observed_at=NOW,
    )
    assert second_snapshot.snapshot_id != first_evidence.snapshot.snapshot_id
    second_evidence = ResearchDatasetEvidence(
        snapshot=second_snapshot,
        rows=ROWS,
        metadata=second_metadata,
        citations=CITATIONS,
    )
    second_request = dict(first_request)
    second_manifest = build_run_manifest(
        run_kind="single_backtest",
        application_version="test-build",
        engine_version="quantsieve-vectorized-v1",
        source_revision="a" * 40,
        dependency_lock_hash="b" * 64,
        datasets=[second_snapshot],
        run_request=second_request,
        parameters=PARAMETERS,
        engine_config=ENGINE_CONFIG,
        cost_model=COST_MODEL,
        execution_model="next_bar_open",
        result=RESULT,
        recorded_at=NOW,
    )
    second = store.create_run(
        manifest=second_manifest,
        datasets=(second_evidence,),
        run_request=second_request,
        result=RESULT,
        parameters=PARAMETERS,
        engine_config=ENGINE_CONFIG,
        cost_model=COST_MODEL,
    )

    assert first.run_id != second.run_id
    assert first.manifest.manifest_id != second.manifest.manifest_id
    assert store.get_run(second.run_id).datasets[0].metadata == second_metadata
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM dataset_snapshots").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 2
        assert (
            connection.execute("SELECT COUNT(*) FROM research_run_dataset_evidence").fetchone()[0]
            == 2
        )


def test_missing_and_expired_receipts_are_distinct(tmp_path: Path) -> None:
    clock = Clock()
    store = ResearchRunStore(
        tmp_path / "ttl.db",
        run_ttl=timedelta(minutes=5),
        clock=clock,
    )
    with pytest.raises(ResearchRunNotFoundError):
        store.get_run("f" * 32)

    created = create_run(store)
    clock.current = NOW + timedelta(minutes=5)
    with pytest.raises(ResearchRunExpiredError) as expired:
        store.get_run(created.run_id)
    assert expired.value.run_id == created.run_id
    assert expired.value.expires_at == created.expires_at

    clock.current = NOW + timedelta(minutes=4, seconds=59)
    assert store.get_run(created.run_id).run_id == created.run_id


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("request_payload", {"symbol": "ETHUSDT"}),
        ("result_payload", {"metrics": {"total_return": 999}}),
        ("parameters_payload", {"fast": 1, "slow": 2, "signal": 1}),
        ("engine_config_payload", {"fee_rate": 0}),
        ("cost_model_payload", {"kind": "none"}),
    ],
)
def test_corrupted_hashed_run_payloads_are_rejected(
    tmp_path: Path,
    column: str,
    replacement: dict[str, object],
) -> None:
    database = tmp_path / f"corrupt-{column}.db"
    store = ResearchRunStore(database, clock=Clock())
    created = create_run(store)
    with connect(database) as connection:
        connection.execute(
            f"UPDATE research_runs SET {column} = ? WHERE run_id = ?",
            (canonical_json(replacement), created.run_id),
        )

    with pytest.raises(ResearchRunIntegrityError, match="hashes do not match"):
        store.get_run(created.run_id)


def test_corrupted_rows_are_rejected(tmp_path: Path) -> None:
    database = tmp_path / "corrupt-rows.db"
    store = ResearchRunStore(database, clock=Clock())
    created = create_run(store)
    corrupted = [dict(row) for row in ROWS]
    corrupted[1]["close"] = 102.5
    with connect(database) as connection:
        connection.execute(
            "UPDATE research_run_dataset_evidence SET rows_payload = ? WHERE run_id = ?",
            (canonical_json(corrupted), created.run_id),
        )

    with pytest.raises(ResearchRunIntegrityError, match="do not reproduce"):
        store.get_run(created.run_id)


def test_corrupted_v2_snapshot_registry_evidence_hash_is_rejected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corrupt-snapshot-registry.db"
    store = ResearchRunStore(database, clock=Clock())
    created = create_run(store)
    snapshot_id = created.datasets[0].snapshot.snapshot_id
    with connect(database) as connection:
        connection.execute(
            """
            UPDATE dataset_snapshots
            SET metadata_hash = ?
            WHERE snapshot_id = ?
            """,
            ("f" * 64, snapshot_id),
        )

    with pytest.raises(
        ResearchRunIntegrityError,
        match="metadata_hash does not match",
    ):
        store.get_run(created.run_id)


def test_corrupted_manifest_and_dataset_reference_are_rejected(
    tmp_path: Path,
) -> None:
    manifest_database = tmp_path / "corrupt-manifest.db"
    manifest_store = ResearchRunStore(manifest_database, clock=Clock())
    manifest_run = create_run(manifest_store)
    manifest_payload = manifest_run.manifest.model_dump(mode="json")
    manifest_payload["execution_model"] = "same_bar_close"
    with connect(manifest_database) as connection:
        connection.execute(
            "UPDATE research_runs SET manifest_payload = ? WHERE run_id = ?",
            (canonical_json(manifest_payload), manifest_run.run_id),
        )
    with pytest.raises(ResearchRunIntegrityError, match="manifest"):
        manifest_store.get_run(manifest_run.run_id)

    reference_database = tmp_path / "corrupt-reference.db"
    reference_store = ResearchRunStore(reference_database, clock=Clock())
    reference_run = create_run(reference_store)
    with connect(reference_database) as connection:
        connection.execute(
            "DELETE FROM research_run_datasets WHERE run_id = ?",
            (reference_run.run_id,),
        )
    with pytest.raises(ResearchRunIntegrityError, match="dataset count"):
        reference_store.get_run(reference_run.run_id)


def test_noncanonical_persisted_json_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "noncanonical.db"
    store = ResearchRunStore(database, clock=Clock())
    created = create_run(store)
    with connect(database) as connection:
        connection.execute(
            "UPDATE research_runs SET request_payload = ? WHERE run_id = ?",
            (json.dumps(RUN_REQUEST, indent=2), created.run_id),
        )

    with pytest.raises(ResearchRunIntegrityError, match="not canonical JSON"):
        store.get_run(created.run_id)
