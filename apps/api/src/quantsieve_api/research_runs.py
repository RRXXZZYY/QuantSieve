from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import cast
from uuid import uuid4

from pydantic import ValidationError
from quantsieve_engine import (
    DatasetSnapshot,
    RunManifest,
    build_dataset_snapshot,
    canonical_json,
    verify_run_manifest,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$", flags=re.ASCII)
_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$", flags=re.ASCII)


def _check_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
        raise TimeoutError("Research-run persistence exceeded its deadline.")


class ResearchRunStoreError(RuntimeError):
    """Base error for authoritative research-run persistence."""


class ResearchRunNotFoundError(ResearchRunStoreError):
    """The requested server-issued run receipt does not exist."""


class ResearchRunExpiredError(ResearchRunStoreError):
    """The requested server-issued run receipt exists but is no longer usable."""

    def __init__(self, run_id: str, expires_at: datetime) -> None:
        self.run_id = run_id
        self.expires_at = expires_at
        super().__init__(f"Research run {run_id} expired at {expires_at.isoformat()}.")


class ResearchRunConflictError(ResearchRunStoreError):
    """A content identity is already bound to different persisted evidence."""


class ResearchRunIntegrityError(ResearchRunStoreError):
    """Persisted or supplied research evidence failed integrity validation."""


@dataclass(frozen=True, slots=True)
class ResearchDatasetEvidence:
    """The complete evidence required to verify one immutable dataset snapshot."""

    snapshot: DatasetSnapshot
    rows: tuple[dict[str, object], ...]
    metadata: dict[str, object]
    citations: tuple[dict[str, object], ...]
    role: str = "evaluation"
    ordinal: int = 0


@dataclass(frozen=True, slots=True)
class ResearchRunReceipt:
    """One authoritative, short-lived quantitative run and all of its inputs."""

    run_id: str
    manifest: RunManifest
    datasets: tuple[ResearchDatasetEvidence, ...]
    run_request: object
    result: object
    parameters: object
    engine_config: object
    cost_model: object
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _DatasetStorage:
    evidence: ResearchDatasetEvidence
    descriptor_payload: str
    rows_payload: str
    metadata_payload: str
    citations_payload: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware.")
    return value.astimezone(UTC)


def _parse_stored_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ResearchRunIntegrityError(f"Stored {label} is not text.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ResearchRunIntegrityError(f"Stored {label} is not ISO-8601.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchRunIntegrityError(f"Stored {label} is not timezone-aware.")
    normalized = parsed.astimezone(UTC)
    if normalized.isoformat() != value:
        raise ResearchRunIntegrityError(f"Stored {label} is not canonical UTC.")
    return normalized


def _stored_text(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise ResearchRunIntegrityError(f"Stored {key} is not text.")
    return value


def _stored_integer(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResearchRunIntegrityError(f"Stored {key} is not an integer.")
    return value


def _decode_canonical_json(payload: str, *, label: str) -> object:
    try:
        decoded = cast(object, json.loads(payload))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ResearchRunIntegrityError(f"Stored {label} is not valid JSON.") from error
    try:
        encoded = canonical_json(decoded)
    except (TypeError, ValueError) as error:
        raise ResearchRunIntegrityError(
            f"Stored {label} is outside the canonical JSON contract."
        ) from error
    if encoded != payload:
        raise ResearchRunIntegrityError(f"Stored {label} is not canonical JSON.")
    return decoded


def _mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ResearchRunIntegrityError(f"Stored {label} is not a JSON object.")
    return cast(dict[str, object], value)


def _mapping_sequence(
    value: object,
    *,
    label: str,
) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise ResearchRunIntegrityError(f"Stored {label} is not a JSON array.")
    items: list[dict[str, object]] = []
    for item in value:
        items.append(_mapping(item, label=f"{label} item"))
    return tuple(items)


def _validate_role(role: str, ordinal: int) -> tuple[str, int]:
    normalized = role.strip().lower()
    if _ROLE_PATTERN.fullmatch(normalized) is None:
        raise ValueError(
            "Dataset role must start with a lowercase letter and contain only "
            "lowercase letters, digits, underscores, or hyphens."
        )
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ValueError("Dataset ordinal must be a non-negative integer.")
    return normalized, ordinal


def _validate_dataset_evidence(
    evidence: ResearchDatasetEvidence,
) -> _DatasetStorage:
    role, ordinal = _validate_role(evidence.role, evidence.ordinal)
    descriptor_payload = canonical_json(evidence.snapshot.model_dump(mode="python"))
    try:
        snapshot = DatasetSnapshot.model_validate_json(descriptor_payload)
    except (ValidationError, TypeError, ValueError) as error:
        raise ResearchRunIntegrityError("Dataset descriptor is invalid.") from error

    rows = tuple(dict(row) for row in evidence.rows)
    metadata = dict(evidence.metadata)
    citations = tuple(dict(item) for item in evidence.citations)
    try:
        rebuilt = build_dataset_snapshot(
            provider=snapshot.provider,
            symbol=snapshot.symbol,
            interval=snapshot.interval,
            rows=rows,
            metadata=metadata,
            citations=citations,
            requested_start=snapshot.requested_start,
            requested_end=snapshot.requested_end,
            finalized_only=snapshot.finalized_only,
            quality_status=snapshot.quality_status,
            quality_issues=snapshot.quality_issues,
            revision_of=snapshot.revision_of,
            observed_at=snapshot.observed_at,
            schema_version=snapshot.schema_version,
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ResearchRunIntegrityError(
            "Dataset rows or source evidence do not satisfy the snapshot descriptor."
        ) from error
    if canonical_json(rebuilt.model_dump(mode="python")) != descriptor_payload:
        raise ResearchRunIntegrityError(
            "Dataset rows or source evidence do not reproduce the snapshot descriptor."
        )

    safe_evidence = ResearchDatasetEvidence(
        snapshot=snapshot,
        rows=rows,
        metadata=metadata,
        citations=citations,
        role=role,
        ordinal=ordinal,
    )
    return _DatasetStorage(
        evidence=safe_evidence,
        descriptor_payload=descriptor_payload,
        rows_payload=canonical_json(list(rows)),
        metadata_payload=canonical_json(metadata),
        citations_payload=canonical_json(list(citations)),
    )


class ResearchRunStore:
    """SQLite-backed authority for short-lived, tamper-evident run receipts."""

    def __init__(
        self,
        path: str | Path,
        *,
        run_ttl: timedelta = timedelta(hours=24),
        expired_run_retention_grace: timedelta = timedelta(days=7),
        cleanup_batch_limit: int = 100,
        busy_timeout_ms: int = 30_000,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if run_ttl <= timedelta(0):
            raise ValueError("run_ttl must be positive.")
        if expired_run_retention_grace < timedelta(0):
            raise ValueError("expired_run_retention_grace must not be negative.")
        if (
            isinstance(cleanup_batch_limit, bool)
            or not isinstance(cleanup_batch_limit, int)
            or cleanup_batch_limit <= 0
        ):
            raise ValueError("cleanup_batch_limit must be a positive integer.")
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms <= 0
        ):
            raise ValueError("busy_timeout_ms must be a positive integer.")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_ttl = run_ttl
        self.expired_run_retention_grace = expired_run_retention_grace
        self.cleanup_batch_limit = cleanup_batch_limit
        self.busy_timeout_ms = busy_timeout_ms
        self._clock = clock
        self._lock = threading.RLock()
        self._initialize()

    def _now(self) -> datetime:
        return _aware_utc(self._clock(), label="clock result")

    def _connect(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> sqlite3.Connection:
        _check_deadline(deadline_monotonic)
        busy_timeout_ms = self.busy_timeout_ms
        if deadline_monotonic is not None:
            remaining_seconds = deadline_monotonic - monotonic()
            if remaining_seconds <= 0:
                raise TimeoutError("Research-run persistence exceeded its deadline.")
            busy_timeout_ms = max(
                1,
                min(
                    busy_timeout_ms,
                    math.ceil(remaining_seconds * 1_000),
                ),
            )
        connection = sqlite3.connect(
            self.path,
            timeout=busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            if deadline_monotonic is not None:
                connection.set_progress_handler(
                    lambda: int(monotonic() >= deadline_monotonic),
                    1_000,
                )
            connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            _check_deadline(deadline_monotonic)
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
            synchronous = connection.execute("PRAGMA synchronous").fetchone()
            if foreign_keys is None or foreign_keys[0] != 1:
                raise ResearchRunStoreError("SQLite foreign-key enforcement is unavailable.")
            if synchronous is None or synchronous[0] != 2:
                raise ResearchRunStoreError("SQLite FULL synchronous mode is unavailable.")
        except BaseException as error:
            connection.close()
            if (
                deadline_monotonic is not None
                and monotonic() >= deadline_monotonic
                and isinstance(error, sqlite3.OperationalError)
            ):
                raise TimeoutError("Research-run persistence exceeded its deadline.") from error
            raise
        return connection

    @contextmanager
    def _immediate_transaction(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> Iterator[sqlite3.Connection]:
        connection = self._connect(deadline_monotonic=deadline_monotonic)
        try:
            _check_deadline(deadline_monotonic)
            connection.execute("BEGIN IMMEDIATE")
            _check_deadline(deadline_monotonic)
            yield connection
            _check_deadline(deadline_monotonic)
        except BaseException as error:
            connection.rollback()
            if (
                deadline_monotonic is not None
                and monotonic() >= deadline_monotonic
                and isinstance(error, sqlite3.OperationalError)
            ):
                raise TimeoutError("Research-run persistence exceeded its deadline.") from error
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._immediate_transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dataset_snapshots (
                    snapshot_id TEXT PRIMARY KEY
                        CHECK(
                            length(snapshot_id) = 64
                            AND snapshot_id NOT GLOB '*[^0-9a-f]*'
                        ),
                    schema_version INTEGER NOT NULL CHECK(schema_version > 0),
                    provider TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    analysis_data_hash TEXT NOT NULL,
                    source_rows_hash TEXT NOT NULL,
                    metadata_hash TEXT NOT NULL,
                    citations_hash TEXT NOT NULL,
                    descriptor_payload TEXT NOT NULL,
                    rows_payload TEXT NOT NULL,
                    metadata_payload TEXT NOT NULL,
                    citations_payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS research_runs (
                    run_id TEXT PRIMARY KEY
                        CHECK(
                            length(run_id) = 32
                            AND run_id NOT GLOB '*[^0-9a-f]*'
                        ),
                    manifest_id TEXT NOT NULL UNIQUE
                        CHECK(
                            length(manifest_id) = 64
                            AND manifest_id NOT GLOB '*[^0-9a-f]*'
                        ),
                    run_kind TEXT NOT NULL,
                    manifest_payload TEXT NOT NULL,
                    request_payload TEXT NOT NULL,
                    result_payload TEXT NOT NULL,
                    parameters_payload TEXT NOT NULL,
                    engine_config_payload TEXT NOT NULL,
                    cost_model_payload TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    parameters_hash TEXT NOT NULL,
                    engine_config_hash TEXT NOT NULL,
                    cost_model_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS research_run_datasets (
                    run_id TEXT NOT NULL,
                    position INTEGER NOT NULL CHECK(position >= 0),
                    role TEXT NOT NULL,
                    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                    snapshot_id TEXT NOT NULL,
                    PRIMARY KEY(run_id, position),
                    UNIQUE(run_id, role, ordinal),
                    UNIQUE(run_id, snapshot_id),
                    FOREIGN KEY(run_id)
                        REFERENCES research_runs(run_id) ON DELETE CASCADE,
                    FOREIGN KEY(snapshot_id)
                        REFERENCES dataset_snapshots(snapshot_id) ON DELETE RESTRICT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS research_run_dataset_evidence (
                    run_id TEXT NOT NULL,
                    position INTEGER NOT NULL CHECK(position >= 0),
                    descriptor_payload TEXT NOT NULL,
                    rows_payload TEXT NOT NULL,
                    metadata_payload TEXT NOT NULL,
                    citations_payload TEXT NOT NULL,
                    PRIMARY KEY(run_id, position),
                    FOREIGN KEY(run_id, position)
                        REFERENCES research_run_datasets(run_id, position)
                        ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_runs_expires ON research_runs(expires_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_dataset_hash "
                "ON dataset_snapshots(analysis_data_hash)"
            )

    def _prepare_run(
        self,
        *,
        manifest: RunManifest,
        datasets: Sequence[ResearchDatasetEvidence],
        run_request: object,
        result: object,
        parameters: object,
        engine_config: object,
        cost_model: object,
    ) -> tuple[
        RunManifest,
        tuple[_DatasetStorage, ...],
        str,
        str,
        str,
        str,
        str,
        str,
    ]:
        manifest_payload = canonical_json(manifest.model_dump(mode="python"))
        try:
            safe_manifest = RunManifest.model_validate_json(manifest_payload)
        except (ValidationError, TypeError, ValueError) as error:
            raise ResearchRunIntegrityError("Run manifest is invalid.") from error
        safe_datasets = tuple(_validate_dataset_evidence(item) for item in datasets)
        if not safe_datasets:
            raise ResearchRunIntegrityError("Research runs require dataset evidence.")

        evidence_ids = tuple(item.evidence.snapshot.snapshot_id for item in safe_datasets)
        manifest_ids = tuple(item.snapshot_id for item in safe_manifest.datasets)
        if evidence_ids != manifest_ids:
            raise ResearchRunIntegrityError(
                "Dataset evidence order does not match the run manifest."
            )
        for manifest_snapshot, stored in zip(
            safe_manifest.datasets,
            safe_datasets,
            strict=True,
        ):
            if canonical_json(manifest_snapshot.model_dump(mode="python")) != (
                stored.descriptor_payload
            ):
                raise ResearchRunIntegrityError(
                    "Run manifest dataset descriptors do not match persisted evidence."
                )
        role_keys = [(item.evidence.role, item.evidence.ordinal) for item in safe_datasets]
        if len(set(role_keys)) != len(role_keys):
            raise ResearchRunIntegrityError(
                "Research run dataset role and ordinal pairs must be unique."
            )

        request_payload = canonical_json(run_request)
        result_payload = canonical_json(result)
        parameters_payload = canonical_json(parameters)
        engine_config_payload = canonical_json(engine_config)
        cost_model_payload = canonical_json(cost_model)
        mismatches = verify_run_manifest(
            safe_manifest,
            run_request=run_request,
            parameters=parameters,
            engine_config=engine_config,
            cost_model=cost_model,
            result=result,
        )
        if mismatches:
            raise ResearchRunIntegrityError(
                "Run payload hashes do not match the manifest: " + ", ".join(mismatches)
            )
        return (
            safe_manifest,
            safe_datasets,
            manifest_payload,
            request_payload,
            result_payload,
            parameters_payload,
            engine_config_payload,
            cost_model_payload,
        )

    @staticmethod
    def _snapshot_values(storage: _DatasetStorage, created_at: datetime) -> tuple[object, ...]:
        snapshot = storage.evidence.snapshot
        return (
            snapshot.snapshot_id,
            snapshot.schema_version,
            snapshot.provider,
            snapshot.symbol,
            snapshot.interval,
            snapshot.analysis_data_hash,
            snapshot.source_rows_hash,
            snapshot.metadata_hash,
            snapshot.citations_hash,
            storage.descriptor_payload,
            storage.rows_payload,
            storage.metadata_payload,
            storage.citations_payload,
            created_at.isoformat(),
        )

    def _persist_snapshot(
        self,
        connection: sqlite3.Connection,
        storage: _DatasetStorage,
        *,
        created_at: datetime,
    ) -> None:
        snapshot_id = storage.evidence.snapshot.snapshot_id
        existing = connection.execute(
            """
            SELECT
                snapshot_id, schema_version, provider, symbol, interval,
                analysis_data_hash, source_rows_hash, metadata_hash,
                citations_hash
            FROM dataset_snapshots
            WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        values = self._snapshot_values(storage, created_at)
        comparable = values[:9] if storage.evidence.snapshot.schema_version == 2 else values[:6]
        if existing is not None:
            stored = tuple(existing)[: len(comparable)]
            if stored != comparable:
                raise ResearchRunConflictError(
                    f"Dataset snapshot {snapshot_id} is already bound to different evidence."
                )
            return
        connection.execute(
            """
            INSERT INTO dataset_snapshots (
                snapshot_id, schema_version, provider, symbol, interval,
                analysis_data_hash, source_rows_hash, metadata_hash, citations_hash,
                descriptor_payload, rows_payload, metadata_payload, citations_payload,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )

    @staticmethod
    def _stored_run_payloads(row: sqlite3.Row) -> tuple[str, str, str, str, str]:
        return (
            _stored_text(row, "request_payload"),
            _stored_text(row, "result_payload"),
            _stored_text(row, "parameters_payload"),
            _stored_text(row, "engine_config_payload"),
            _stored_text(row, "cost_model_payload"),
        )

    def _cleanup_expired_runs(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime,
        current_run_id: str,
    ) -> None:
        cutoff = now - self.expired_run_retention_grace
        expired_rows = connection.execute(
            """
            SELECT run_id
            FROM research_runs
            WHERE expires_at < ?
              AND run_id <> ?
            ORDER BY expires_at, run_id
            LIMIT ?
            """,
            (
                cutoff.isoformat(),
                current_run_id,
                self.cleanup_batch_limit,
            ),
        ).fetchall()
        connection.executemany(
            "DELETE FROM research_runs WHERE run_id = ?",
            [(_stored_text(row, "run_id"),) for row in expired_rows],
        )
        connection.execute(
            """
            DELETE FROM dataset_snapshots
            WHERE NOT EXISTS (
                SELECT 1
                FROM research_run_datasets
                WHERE research_run_datasets.snapshot_id =
                      dataset_snapshots.snapshot_id
            )
            """
        )

    def create_run(
        self,
        *,
        manifest: RunManifest,
        datasets: Sequence[ResearchDatasetEvidence],
        run_request: object,
        result: object,
        parameters: object,
        engine_config: object,
        cost_model: object,
        deadline_monotonic: float | None = None,
    ) -> ResearchRunReceipt:
        _check_deadline(deadline_monotonic)
        (
            safe_manifest,
            safe_datasets,
            manifest_payload,
            request_payload,
            result_payload,
            parameters_payload,
            engine_config_payload,
            cost_model_payload,
        ) = self._prepare_run(
            manifest=manifest,
            datasets=datasets,
            run_request=run_request,
            result=result,
            parameters=parameters,
            engine_config=engine_config,
            cost_model=cost_model,
        )
        _check_deadline(deadline_monotonic)
        payloads = (
            request_payload,
            result_payload,
            parameters_payload,
            engine_config_payload,
            cost_model_payload,
        )
        now = self._now()
        expires_at = now + self.run_ttl
        _check_deadline(deadline_monotonic)
        with (
            self._lock,
            self._immediate_transaction(deadline_monotonic=deadline_monotonic) as connection,
        ):
            _check_deadline(deadline_monotonic)
            existing = connection.execute(
                "SELECT * FROM research_runs WHERE manifest_id = ?",
                (safe_manifest.manifest_id,),
            ).fetchone()
            if existing is not None:
                if self._stored_run_payloads(existing) != payloads:
                    raise ResearchRunConflictError(
                        "The manifest id is already bound to different run payloads."
                    )
                try:
                    stored_manifest = RunManifest.model_validate_json(
                        _stored_text(existing, "manifest_payload")
                    )
                except (ValidationError, TypeError, ValueError) as error:
                    raise ResearchRunIntegrityError(
                        "Stored duplicate run manifest is invalid."
                    ) from error
                stored_request_ranges = tuple(
                    (item.requested_start, item.requested_end) for item in stored_manifest.datasets
                )
                incoming_request_ranges = tuple(
                    (
                        storage.evidence.snapshot.requested_start,
                        storage.evidence.snapshot.requested_end,
                    )
                    for storage in safe_datasets
                )
                if stored_request_ranges != incoming_request_ranges:
                    raise ResearchRunConflictError(
                        "The manifest id is already bound to different evidence."
                    )
                expected_refs = [
                    (
                        position,
                        storage.evidence.role,
                        storage.evidence.ordinal,
                        storage.evidence.snapshot.snapshot_id,
                    )
                    for position, storage in enumerate(safe_datasets)
                ]
                actual_refs = [
                    (
                        _stored_integer(row, "position"),
                        _stored_text(row, "role"),
                        _stored_integer(row, "ordinal"),
                        _stored_text(row, "snapshot_id"),
                    )
                    for row in connection.execute(
                        """
                        SELECT position, role, ordinal, snapshot_id
                        FROM research_run_datasets
                        WHERE run_id = ?
                        ORDER BY position
                        """,
                        (_stored_text(existing, "run_id"),),
                    ).fetchall()
                ]
                if actual_refs != expected_refs:
                    raise ResearchRunIntegrityError(
                        "Stored run dataset references do not match the duplicate manifest."
                    )
                expected_evidence = [
                    (
                        position,
                        storage.rows_payload,
                        storage.metadata_payload,
                        storage.citations_payload,
                    )
                    for position, storage in enumerate(safe_datasets)
                ]
                actual_evidence = [
                    (
                        _stored_integer(row, "position"),
                        _stored_text(row, "rows_payload"),
                        _stored_text(row, "metadata_payload"),
                        _stored_text(row, "citations_payload"),
                    )
                    for row in connection.execute(
                        """
                        SELECT
                            position,
                            rows_payload,
                            metadata_payload,
                            citations_payload
                        FROM research_run_dataset_evidence
                        WHERE run_id = ?
                        ORDER BY position
                        """,
                        (_stored_text(existing, "run_id"),),
                    ).fetchall()
                ]
                if actual_evidence != expected_evidence:
                    raise ResearchRunConflictError(
                        "The manifest id is already bound to different dataset evidence."
                    )
                existing_expires_at = _parse_stored_timestamp(
                    existing["expires_at"],
                    label="run expires_at",
                )
                if existing_expires_at <= now:
                    connection.execute(
                        "UPDATE research_runs SET expires_at = ? WHERE run_id = ?",
                        (
                            expires_at.isoformat(),
                            _stored_text(existing, "run_id"),
                        ),
                    )
                    existing = connection.execute(
                        "SELECT * FROM research_runs WHERE manifest_id = ?",
                        (safe_manifest.manifest_id,),
                    ).fetchone()
                    if existing is None:  # pragma: no cover - transactional invariant
                        raise ResearchRunIntegrityError("Renewed research run became unreadable.")
                current_run_id = _stored_text(existing, "run_id")
                self._cleanup_expired_runs(
                    connection,
                    now=now,
                    current_run_id=current_run_id,
                )
                _check_deadline(deadline_monotonic)
                return self._load_run(
                    connection,
                    existing,
                    now=now,
                    enforce_expiry=False,
                )

            for storage in safe_datasets:
                self._persist_snapshot(connection, storage, created_at=now)

            run_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO research_runs (
                    run_id, manifest_id, run_kind, manifest_payload,
                    request_payload, result_payload, parameters_payload,
                    engine_config_payload, cost_model_payload,
                    request_hash, result_hash, parameters_hash,
                    engine_config_hash, cost_model_hash,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    safe_manifest.manifest_id,
                    safe_manifest.run_kind,
                    manifest_payload,
                    request_payload,
                    result_payload,
                    parameters_payload,
                    engine_config_payload,
                    cost_model_payload,
                    safe_manifest.run_request_hash,
                    safe_manifest.result_hash,
                    safe_manifest.parameters_hash,
                    safe_manifest.engine_config_hash,
                    safe_manifest.cost_model_hash,
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            connection.executemany(
                """
                INSERT INTO research_run_datasets (
                    run_id, position, role, ordinal, snapshot_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        position,
                        storage.evidence.role,
                        storage.evidence.ordinal,
                        storage.evidence.snapshot.snapshot_id,
                    )
                    for position, storage in enumerate(safe_datasets)
                ],
            )
            connection.executemany(
                """
                INSERT INTO research_run_dataset_evidence (
                    run_id, position, descriptor_payload, rows_payload,
                    metadata_payload, citations_payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        position,
                        storage.descriptor_payload,
                        storage.rows_payload,
                        storage.metadata_payload,
                        storage.citations_payload,
                    )
                    for position, storage in enumerate(safe_datasets)
                ],
            )
            self._cleanup_expired_runs(
                connection,
                now=now,
                current_run_id=run_id,
            )
            _check_deadline(deadline_monotonic)
            row = connection.execute(
                "SELECT * FROM research_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ResearchRunIntegrityError(
                    "Research run insert did not produce a readable receipt."
                )
            _check_deadline(deadline_monotonic)
            return self._load_run(
                connection,
                row,
                now=now,
                enforce_expiry=False,
            )

    def _load_dataset(
        self,
        connection: sqlite3.Connection,
        *,
        reference: sqlite3.Row,
        role: str,
        ordinal: int,
    ) -> ResearchDatasetEvidence:
        snapshot_id = _stored_text(reference, "snapshot_id")
        registry = connection.execute(
            "SELECT * FROM dataset_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if registry is None:
            raise ResearchRunIntegrityError(
                f"Run references missing dataset snapshot {snapshot_id}."
            )
        descriptor_payload = _stored_text(reference, "evidence_descriptor_payload")
        _decode_canonical_json(descriptor_payload, label="dataset descriptor")
        try:
            snapshot = DatasetSnapshot.model_validate_json(descriptor_payload)
        except (ValidationError, TypeError, ValueError) as error:
            raise ResearchRunIntegrityError(
                "Stored dataset descriptor failed model validation."
            ) from error
        scalar_checks: dict[str, object] = {
            "snapshot_id": snapshot.snapshot_id,
            "schema_version": snapshot.schema_version,
            "provider": snapshot.provider,
            "symbol": snapshot.symbol,
            "interval": snapshot.interval,
            "analysis_data_hash": snapshot.analysis_data_hash,
        }
        if snapshot.schema_version == 2:
            scalar_checks.update(
                {
                    "source_rows_hash": snapshot.source_rows_hash,
                    "metadata_hash": snapshot.metadata_hash,
                    "citations_hash": snapshot.citations_hash,
                }
            )
        for key, expected in scalar_checks.items():
            if registry[key] != expected:
                raise ResearchRunIntegrityError(
                    f"Stored dataset registry column {key} does not match its descriptor."
                )
        _parse_stored_timestamp(
            registry["created_at"],
            label="dataset created_at",
        )

        rows_payload = _stored_text(reference, "evidence_rows_payload")
        metadata_payload = _stored_text(reference, "evidence_metadata_payload")
        citations_payload = _stored_text(reference, "evidence_citations_payload")
        rows = _mapping_sequence(
            _decode_canonical_json(rows_payload, label="dataset rows"),
            label="dataset rows",
        )
        metadata = _mapping(
            _decode_canonical_json(metadata_payload, label="dataset metadata"),
            label="dataset metadata",
        )
        citations = _mapping_sequence(
            _decode_canonical_json(citations_payload, label="dataset citations"),
            label="dataset citations",
        )
        storage = _validate_dataset_evidence(
            ResearchDatasetEvidence(
                snapshot=snapshot,
                rows=rows,
                metadata=metadata,
                citations=citations,
                role=role,
                ordinal=ordinal,
            )
        )
        if (
            storage.descriptor_payload != descriptor_payload
            or storage.rows_payload != rows_payload
            or storage.metadata_payload != metadata_payload
            or storage.citations_payload != citations_payload
        ):
            raise ResearchRunIntegrityError(
                "Stored dataset evidence is not its canonical reconstructed form."
            )
        return storage.evidence

    def _load_run(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: datetime,
        enforce_expiry: bool,
    ) -> ResearchRunReceipt:
        run_id = _stored_text(row, "run_id")
        if _RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise ResearchRunIntegrityError("Stored research run id is invalid.")
        manifest_payload = _stored_text(row, "manifest_payload")
        _decode_canonical_json(manifest_payload, label="run manifest")
        try:
            manifest = RunManifest.model_validate_json(manifest_payload)
        except (ValidationError, TypeError, ValueError) as error:
            raise ResearchRunIntegrityError(
                "Stored run manifest failed model validation."
            ) from error
        if _stored_text(row, "manifest_id") != manifest.manifest_id:
            raise ResearchRunIntegrityError(
                "Stored manifest id column does not match the manifest."
            )
        if _SHA256_PATTERN.fullmatch(manifest.manifest_id) is None:
            raise ResearchRunIntegrityError("Stored manifest id is invalid.")
        if _stored_text(row, "run_kind") != manifest.run_kind:
            raise ResearchRunIntegrityError("Stored run kind does not match the manifest.")

        ref_rows = connection.execute(
            """
            SELECT
                datasets.position,
                datasets.role,
                datasets.ordinal,
                datasets.snapshot_id,
                evidence.descriptor_payload AS evidence_descriptor_payload,
                evidence.rows_payload AS evidence_rows_payload,
                evidence.metadata_payload AS evidence_metadata_payload,
                evidence.citations_payload AS evidence_citations_payload
            FROM research_run_datasets AS datasets
            JOIN research_run_dataset_evidence AS evidence
              ON evidence.run_id = datasets.run_id
             AND evidence.position = datasets.position
            WHERE datasets.run_id = ?
            ORDER BY datasets.position
            """,
            (run_id,),
        ).fetchall()
        if [row["position"] for row in ref_rows] != list(range(len(ref_rows))):
            raise ResearchRunIntegrityError("Stored run dataset positions are not contiguous.")
        if len(ref_rows) != len(manifest.datasets):
            raise ResearchRunIntegrityError("Stored run dataset count does not match the manifest.")
        datasets: list[ResearchDatasetEvidence] = []
        for position, ref in enumerate(ref_rows):
            role = _stored_text(ref, "role")
            ordinal = _stored_integer(ref, "ordinal")
            _validate_role(role, ordinal)
            referenced_id = _stored_text(ref, "snapshot_id")
            if referenced_id != manifest.datasets[position].snapshot_id:
                raise ResearchRunIntegrityError(
                    "Stored run dataset order does not match the manifest."
                )
            dataset = self._load_dataset(
                connection,
                reference=ref,
                role=role,
                ordinal=ordinal,
            )
            if canonical_json(dataset.snapshot.model_dump(mode="python")) != canonical_json(
                manifest.datasets[position].model_dump(mode="python")
            ):
                raise ResearchRunIntegrityError(
                    "Stored dataset descriptor does not match the manifest."
                )
            datasets.append(dataset)

        request_payload = _stored_text(row, "request_payload")
        result_payload = _stored_text(row, "result_payload")
        parameters_payload = _stored_text(row, "parameters_payload")
        engine_config_payload = _stored_text(row, "engine_config_payload")
        cost_model_payload = _stored_text(row, "cost_model_payload")
        run_request = _decode_canonical_json(request_payload, label="run request")
        result = _decode_canonical_json(result_payload, label="run result")
        parameters = _decode_canonical_json(
            parameters_payload,
            label="run parameters",
        )
        engine_config = _decode_canonical_json(
            engine_config_payload,
            label="engine config",
        )
        cost_model = _decode_canonical_json(cost_model_payload, label="cost model")
        mismatches = verify_run_manifest(
            manifest,
            run_request=run_request,
            parameters=parameters,
            engine_config=engine_config,
            cost_model=cost_model,
            result=result,
        )
        if mismatches:
            raise ResearchRunIntegrityError(
                "Stored run payload hashes do not match the manifest: " + ", ".join(mismatches)
            )
        hash_columns = {
            "request_hash": manifest.run_request_hash,
            "result_hash": manifest.result_hash,
            "parameters_hash": manifest.parameters_hash,
            "engine_config_hash": manifest.engine_config_hash,
            "cost_model_hash": manifest.cost_model_hash,
        }
        for key, expected in hash_columns.items():
            if row[key] != expected:
                raise ResearchRunIntegrityError(
                    f"Stored run hash column {key} does not match the manifest."
                )

        created_at = _parse_stored_timestamp(row["created_at"], label="run created_at")
        expires_at = _parse_stored_timestamp(row["expires_at"], label="run expires_at")
        if expires_at <= created_at:
            raise ResearchRunIntegrityError("Stored research run expiration is not after creation.")
        if enforce_expiry and expires_at <= now:
            raise ResearchRunExpiredError(run_id, expires_at)
        return ResearchRunReceipt(
            run_id=run_id,
            manifest=manifest,
            datasets=tuple(datasets),
            run_request=run_request,
            result=result,
            parameters=parameters,
            engine_config=engine_config,
            cost_model=cost_model,
            created_at=created_at,
            expires_at=expires_at,
        )

    def get_run(self, run_id: str) -> ResearchRunReceipt:
        normalized = run_id.strip().lower()
        if _RUN_ID_PATTERN.fullmatch(normalized) is None:
            raise ResearchRunNotFoundError(f"Research run {run_id!r} does not exist.")
        now = self._now()
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM research_runs WHERE run_id = ?",
                    (normalized,),
                ).fetchone()
                if row is None:
                    raise ResearchRunNotFoundError(f"Research run {normalized} does not exist.")
                return self._load_run(
                    connection,
                    row,
                    now=now,
                    enforce_expiry=True,
                )
            finally:
                connection.close()
