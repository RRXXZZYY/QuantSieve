from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from math import isfinite
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CanonicalValue = (
    None
    | bool
    | int
    | float
    | str
    | list["_CanonicalValue"]
    | dict[str, "_CanonicalValue"]
)


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Canonical payloads cannot contain non-finite decimals.")
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _canonical_value(value: object) -> _CanonicalValue:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Canonical datetimes must be timezone-aware.")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("Canonical payloads cannot contain non-finite floats.")
        return 0.0 if value == 0 else value
    if isinstance(value, Mapping):
        canonical: dict[str, _CanonicalValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("Canonical payload mappings require string keys.")
            canonical[key] = _canonical_value(item)
        return canonical
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_canonical_value(item) for item in value]
    raise TypeError(
        f"Canonical payloads do not support values of type {type(value).__name__}."
    )


def canonical_json(value: object) -> str:
    """Serialize strict JSON deterministically for evidence hashing."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_payload_hash(value: object) -> str:
    """Return a lowercase SHA-256 digest over canonical UTF-8 JSON."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_timestamp(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware.")
    return value.astimezone(UTC)


def _snapshot_identity(snapshot: DatasetSnapshot | Mapping[str, object]) -> dict[str, object]:
    data = (
        snapshot.model_dump(mode="python")
        if isinstance(snapshot, DatasetSnapshot)
        else dict(snapshot)
    )
    schema_version = data["schema_version"]
    identity = {
        key: data[key]
        for key in (
            "schema_version",
            "provider",
            "symbol",
            "interval",
            "first_observation_at",
            "last_observation_at",
            "row_count",
            "analysis_data_hash",
            "finalized_only",
            "quality_status",
            "quality_issues",
            "revision_of",
        )
    }
    if schema_version == 2:
        identity.update(
            {
                key: data[key]
                for key in (
                    "requested_start",
                    "requested_end",
                    "source_rows_hash",
                    "metadata_hash",
                    "citations_hash",
                )
            }
        )
    return identity


class DatasetSnapshot(BaseModel):
    """Immutable identity for the exact data evidence consumed by a run."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1, 2] = 2
    snapshot_id: str = Field(pattern=_SHA256_PATTERN)
    provider: str = Field(min_length=1, max_length=80)
    symbol: str = Field(min_length=1, max_length=80)
    interval: str = Field(min_length=1, max_length=40)
    requested_start: date | None = None
    requested_end: date | None = None
    first_observation_at: datetime
    last_observation_at: datetime
    row_count: int = Field(gt=0)
    analysis_data_hash: str = Field(pattern=_SHA256_PATTERN)
    source_rows_hash: str = Field(pattern=_SHA256_PATTERN)
    metadata_hash: str = Field(pattern=_SHA256_PATTERN)
    citations_hash: str = Field(pattern=_SHA256_PATTERN)
    finalized_only: bool
    quality_status: Literal["passed", "degraded", "failed"]
    quality_issues: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    revision_of: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    observed_at: datetime

    @field_validator("provider", mode="before")
    @classmethod
    def normalize_provider(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator(
        "first_observation_at",
        "last_observation_at",
        "observed_at",
    )
    @classmethod
    def normalize_timestamps(cls, value: datetime, info: Any) -> datetime:
        return _normalize_timestamp(value, label=info.field_name)

    @field_validator("quality_issues")
    @classmethod
    def validate_quality_issues(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("Dataset quality issues must be non-empty strings.")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Dataset quality issues must be unique.")
        return normalized

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if (
            self.requested_start is not None
            and self.requested_end is not None
            and self.requested_start > self.requested_end
        ):
            raise ValueError("Dataset requested start cannot be after requested end.")
        if self.first_observation_at > self.last_observation_at:
            raise ValueError("Dataset observations must be ordered.")
        if self.observed_at < self.last_observation_at:
            raise ValueError("A dataset cannot be observed before its final observation.")
        if self.quality_status == "passed" and self.quality_issues:
            raise ValueError("A passed dataset cannot carry quality issues.")
        if self.quality_status != "passed" and not self.quality_issues:
            raise ValueError("A degraded or failed dataset must explain its quality issues.")
        if self.quality_status == "passed" and not self.finalized_only:
            raise ValueError("A passed dataset must contain finalized observations only.")
        expected_id = canonical_payload_hash(_snapshot_identity(self))
        if self.snapshot_id != expected_id:
            raise ValueError("Dataset snapshot_id does not match its immutable identity.")
        return self


def _parse_observation_time(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("Dataset observation timestamps cannot be empty.")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed_date = date.fromisoformat(text)
            except ValueError as error:
                raise ValueError(
                    "Dataset observation timestamps must use ISO-8601."
                ) from error
            parsed = datetime.combine(parsed_date, datetime.min.time(), tzinfo=UTC)
    else:
        raise TypeError("Dataset observation timestamps must use dates or datetimes.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _epoch_nanoseconds(value: datetime) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _analysis_decimal(value: object, *, label: str, allow_zero: bool) -> str:
    if isinstance(value, bool):
        raise TypeError(f"Dataset {label} must be numeric.")
    try:
        exact = Decimal(str(value))
    except Exception as error:
        raise ValueError(f"Dataset {label} must be numeric.") from error
    if (
        not exact.is_finite()
        or exact < 0
        or (not allow_zero and exact == 0)
    ):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"Dataset {label} must be finite and {qualifier}.")
    return _canonical_decimal(exact)


def _analysis_rows(
    *,
    provider: str,
    symbol: str,
    interval: str,
    rows: Sequence[Mapping[str, object]],
) -> tuple[list[datetime], dict[str, object]]:
    observation_times: list[datetime] = []
    normalized_rows: list[list[object]] = []
    for row in rows:
        if "date" not in row:
            raise ValueError("Dataset snapshot rows require a date field.")
        missing = {"open", "high", "low", "close", "volume"} - set(row)
        if missing:
            raise ValueError(
                "Dataset snapshot rows are missing analysis columns: "
                + ", ".join(sorted(missing))
            )
        observed = _parse_observation_time(row["date"])
        open_price = _analysis_decimal(
            row["open"],
            label="open",
            allow_zero=False,
        )
        high_price = _analysis_decimal(
            row["high"],
            label="high",
            allow_zero=False,
        )
        low_price = _analysis_decimal(
            row["low"],
            label="low",
            allow_zero=False,
        )
        close_price = _analysis_decimal(
            row["close"],
            label="close",
            allow_zero=False,
        )
        volume = _analysis_decimal(
            row["volume"],
            label="volume",
            allow_zero=True,
        )
        exact_open = Decimal(open_price)
        exact_high = Decimal(high_price)
        exact_low = Decimal(low_price)
        exact_close = Decimal(close_price)
        if exact_high < max(exact_open, exact_close, exact_low):
            raise ValueError("Dataset high must cover open, low, and close.")
        if exact_low > min(exact_open, exact_close, exact_high):
            raise ValueError("Dataset low must cover open, high, and close.")
        observation_times.append(observed)
        normalized_rows.append(
            [
                _epoch_nanoseconds(observed),
                open_price,
                high_price,
                low_price,
                close_price,
                volume,
            ]
        )
    return observation_times, {
        "contract": "quantsieve.ohlcv.v1",
        "provider": provider.strip().lower(),
        "symbol": symbol.strip().upper(),
        "interval": interval,
        "columns": ["ts_ns", "open", "high", "low", "close", "volume"],
        "rows": normalized_rows,
    }


def build_dataset_snapshot(
    *,
    provider: str,
    symbol: str,
    interval: str,
    rows: Sequence[Mapping[str, object]],
    metadata: Mapping[str, object],
    citations: Sequence[Mapping[str, object]],
    requested_start: date | None = None,
    requested_end: date | None = None,
    finalized_only: bool = True,
    quality_status: Literal["passed", "degraded", "failed"] = "passed",
    quality_issues: Sequence[str] = (),
    revision_of: str | None = None,
    observed_at: datetime | None = None,
    schema_version: Literal[1, 2] = 2,
) -> DatasetSnapshot:
    """Build and self-verify a content-addressed dataset snapshot."""

    if not rows:
        raise ValueError("Dataset snapshots require at least one observation.")
    copied_rows = [dict(row) for row in rows]
    observation_times, analysis_payload = _analysis_rows(
        provider=provider,
        symbol=symbol,
        interval=interval,
        rows=copied_rows,
    )
    if observation_times != sorted(observation_times):
        raise ValueError("Dataset snapshot rows must be ordered by date.")
    if len(set(observation_times)) != len(observation_times):
        raise ValueError("Dataset snapshot rows cannot repeat observation dates.")
    resolved_observed_at = observed_at or datetime.now(UTC)
    identity: dict[str, object] = {
        "schema_version": schema_version,
        "provider": provider.strip().lower(),
        "symbol": symbol.strip().upper(),
        "interval": interval,
        "requested_start": requested_start,
        "requested_end": requested_end,
        "first_observation_at": observation_times[0],
        "last_observation_at": observation_times[-1],
        "row_count": len(copied_rows),
        "analysis_data_hash": canonical_payload_hash(analysis_payload),
        "source_rows_hash": canonical_payload_hash(copied_rows),
        "metadata_hash": canonical_payload_hash(dict(metadata)),
        "citations_hash": canonical_payload_hash([dict(item) for item in citations]),
        "finalized_only": finalized_only,
        "quality_status": quality_status,
        "quality_issues": tuple(quality_issues),
        "revision_of": revision_of,
    }
    return DatasetSnapshot.model_validate(
        {
            **identity,
            "snapshot_id": canonical_payload_hash(_snapshot_identity(identity)),
            "observed_at": resolved_observed_at,
        }
    )


def _manifest_identity(manifest: RunManifest | Mapping[str, object]) -> dict[str, object]:
    data = (
        manifest.model_dump(mode="python")
        if isinstance(manifest, RunManifest)
        else dict(manifest)
    )
    data.pop("manifest_id", None)
    data.pop("recorded_at", None)
    datasets = data.get("datasets")
    if isinstance(datasets, Sequence):
        data["datasets"] = [
            (
                item.snapshot_id
                if isinstance(item, DatasetSnapshot)
                else str(item.get("snapshot_id"))
                if isinstance(item, Mapping)
                else item
            )
            for item in datasets
        ]
    return data


class RunManifest(BaseModel):
    """Tamper-evident input and result identity for one quantitative run."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=_SHA256_PATTERN)
    run_kind: Literal[
        "single_backtest",
        "portfolio_backtest",
        "cross_market_study",
        "custom_backtest",
        "factor_research",
        "paper_decision",
    ]
    application_version: str = Field(min_length=1, max_length=120)
    engine_version: str = Field(min_length=1, max_length=120)
    source_revision: str | None = Field(
        default=None,
        min_length=7,
        max_length=64,
        pattern=r"^[0-9a-f]+$",
    )
    dependency_lock_hash: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    datasets: tuple[DatasetSnapshot, ...] = Field(min_length=1, max_length=100)
    run_request_hash: str = Field(pattern=_SHA256_PATTERN)
    parameters_hash: str = Field(pattern=_SHA256_PATTERN)
    engine_config_hash: str = Field(pattern=_SHA256_PATTERN)
    cost_model_hash: str = Field(pattern=_SHA256_PATTERN)
    execution_model: str = Field(min_length=1, max_length=120)
    randomness_used: bool = False
    random_seed: int | None = None
    result_hash: str = Field(pattern=_SHA256_PATTERN)
    uncaptured_inputs: tuple[Literal["indicator_warmup"], ...] = Field(
        default_factory=tuple,
    )
    reproducibility_status: Literal["complete", "incomplete"]
    missing_requirements: tuple[
        Literal[
            "source_revision",
            "dependency_lock",
            "dataset_quality",
            "indicator_warmup",
        ],
        ...,
    ] = Field(default_factory=tuple)
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def normalize_recorded_at(cls, value: datetime) -> datetime:
        return _normalize_timestamp(value, label="recorded_at")

    @field_validator("datasets")
    @classmethod
    def validate_datasets(
        cls,
        value: tuple[DatasetSnapshot, ...],
    ) -> tuple[DatasetSnapshot, ...]:
        snapshot_ids = [item.snapshot_id for item in value]
        if len(set(snapshot_ids)) != len(snapshot_ids):
            raise ValueError("Run manifests cannot repeat dataset snapshots.")
        return value

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.randomness_used != (self.random_seed is not None):
            raise ValueError(
                "Randomized runs require a seed and deterministic runs must omit it."
            )
        expected_missing: list[
            Literal[
                "source_revision",
                "dependency_lock",
                "dataset_quality",
                "indicator_warmup",
            ]
        ] = []
        if self.source_revision is None:
            expected_missing.append("source_revision")
        if self.dependency_lock_hash is None:
            expected_missing.append("dependency_lock")
        if any(
            item.quality_status != "passed" or not item.finalized_only
            for item in self.datasets
        ):
            expected_missing.append("dataset_quality")
        expected_missing.extend(self.uncaptured_inputs)
        if self.missing_requirements != tuple(expected_missing):
            raise ValueError(
                "Run manifest missing_requirements do not match its evidence."
            )
        expected_status = "complete" if not expected_missing else "incomplete"
        if self.reproducibility_status != expected_status:
            raise ValueError(
                "Run manifest reproducibility_status does not match its evidence."
            )
        expected_id = canonical_payload_hash(_manifest_identity(self))
        if self.manifest_id != expected_id:
            raise ValueError("Run manifest_id does not match its immutable identity.")
        return self


def build_run_manifest(
    *,
    run_kind: Literal[
        "single_backtest",
        "portfolio_backtest",
        "cross_market_study",
        "custom_backtest",
        "factor_research",
        "paper_decision",
    ],
    application_version: str,
    engine_version: str,
    datasets: Sequence[DatasetSnapshot],
    run_request: object,
    parameters: object,
    engine_config: object,
    cost_model: object,
    execution_model: str,
    result: object,
    source_revision: str | None = None,
    dependency_lock_hash: str | None = None,
    randomness_used: bool = False,
    random_seed: int | None = None,
    uncaptured_inputs: Sequence[Literal["indicator_warmup"]] = (),
    recorded_at: datetime | None = None,
) -> RunManifest:
    """Create a self-verifying manifest without embedding bulky run payloads."""

    safe_datasets = tuple(
        DatasetSnapshot.model_validate(item.model_dump(mode="python"))
        for item in datasets
    )
    missing_requirements: list[
        Literal[
            "source_revision",
            "dependency_lock",
            "dataset_quality",
            "indicator_warmup",
        ]
    ] = []
    if source_revision is None:
        missing_requirements.append("source_revision")
    if dependency_lock_hash is None:
        missing_requirements.append("dependency_lock")
    if any(
        item.quality_status != "passed" or not item.finalized_only
        for item in safe_datasets
    ):
        missing_requirements.append("dataset_quality")
    safe_uncaptured_inputs = tuple(uncaptured_inputs)
    if len(set(safe_uncaptured_inputs)) != len(safe_uncaptured_inputs):
        raise ValueError("Run manifest uncaptured inputs must be unique.")
    missing_requirements.extend(safe_uncaptured_inputs)
    identity: dict[str, object] = {
        "schema_version": 1,
        "run_kind": run_kind,
        "application_version": application_version,
        "engine_version": engine_version,
        "source_revision": source_revision,
        "dependency_lock_hash": dependency_lock_hash,
        "datasets": safe_datasets,
        "run_request_hash": canonical_payload_hash(run_request),
        "parameters_hash": canonical_payload_hash(parameters),
        "engine_config_hash": canonical_payload_hash(engine_config),
        "cost_model_hash": canonical_payload_hash(cost_model),
        "execution_model": execution_model,
        "randomness_used": randomness_used,
        "random_seed": random_seed,
        "result_hash": canonical_payload_hash(result),
        "uncaptured_inputs": safe_uncaptured_inputs,
        "reproducibility_status": (
            "complete" if not missing_requirements else "incomplete"
        ),
        "missing_requirements": tuple(missing_requirements),
    }
    return RunManifest.model_validate(
        {
            **identity,
            "manifest_id": canonical_payload_hash(_manifest_identity(identity)),
            "recorded_at": recorded_at or datetime.now(UTC),
        }
    )


def verify_run_manifest(
    manifest: RunManifest,
    *,
    run_request: object,
    parameters: object,
    engine_config: object,
    cost_model: object,
    result: object,
) -> tuple[str, ...]:
    """Return stable mismatch codes for payloads referenced by a manifest."""

    checks = {
        "run_request_hash": canonical_payload_hash(run_request),
        "parameters_hash": canonical_payload_hash(parameters),
        "engine_config_hash": canonical_payload_hash(engine_config),
        "cost_model_hash": canonical_payload_hash(cost_model),
        "result_hash": canonical_payload_hash(result),
    }
    return tuple(
        field_name
        for field_name, actual in checks.items()
        if getattr(manifest, field_name) != actual
    )
