from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .reproducibility import canonical_payload_hash

AssetClass = Literal[
    "equity",
    "etf",
    "index",
    "crypto",
    "future",
    "commodity",
    "fx",
    "rate",
    "macro",
]
FutureContractKind = Literal["dated", "continuous"]
FutureSettlementType = Literal["cash", "physical"]
RollRule = Literal["calendar", "volume", "open_interest", "hybrid", "custom"]
RollAdjustment = Literal["none", "difference", "ratio"]

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_INSTRUMENT_UID_PATTERN = r"^[a-z0-9][a-z0-9._:-]{2,127}$"
_MARKET_CODE_PATTERN = r"^[A-Z0-9][A-Z0-9._:/-]{0,79}$"
_CURRENCY_PATTERN = r"^[A-Z][A-Z0-9]{2,11}$"


def _utc_timestamp(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware.")
    return value.astimezone(UTC)


def _reject_surrounding_whitespace(value: str, *, label: str) -> str:
    if value != value.strip():
        raise ValueError(f"{label} cannot contain surrounding whitespace.")
    return value


class FuturesRollMetadata(BaseModel):
    """Auditable rule for selecting a dated contract behind a continuous future."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )

    rule: RollRule
    target_contract_offset: int = Field(default=1, ge=1, le=24)
    roll_offset_business_days: int = Field(default=0, ge=0, le=60)
    adjustment: RollAdjustment = "none"
    custom_rule_id: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("custom_rule_id")
    @classmethod
    def validate_custom_rule_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _reject_surrounding_whitespace(value, label="custom_rule_id")

    @model_validator(mode="after")
    def validate_roll_rule(self) -> Self:
        if self.rule == "custom" and self.custom_rule_id is None:
            raise ValueError("A custom roll rule requires custom_rule_id.")
        if self.rule != "custom" and self.custom_rule_id is not None:
            raise ValueError("custom_rule_id is only valid for a custom roll rule.")
        return self


class FuturesMetadata(BaseModel):
    """Contract metadata with explicit dated-versus-continuous semantics."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )

    contract_kind: FutureContractKind
    underlying_canonical_symbol: str = Field(
        min_length=1,
        max_length=80,
        pattern=_MARKET_CODE_PATTERN,
    )
    expiry: date | None = None
    last_trade_at: datetime | None = None
    settlement_at: datetime | None = None
    settlement_type: FutureSettlementType | None = None
    roll: FuturesRollMetadata | None = None

    @field_validator("underlying_canonical_symbol")
    @classmethod
    def validate_underlying_symbol(cls, value: str) -> str:
        return _reject_surrounding_whitespace(
            value,
            label="underlying_canonical_symbol",
        )

    @field_validator("last_trade_at", "settlement_at")
    @classmethod
    def normalize_event_time(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return None
        return _utc_timestamp(value, label=info.field_name)

    @model_validator(mode="after")
    def validate_contract_kind(self) -> Self:
        if self.contract_kind == "continuous":
            if self.expiry is not None:
                raise ValueError("A continuous future cannot declare an expiry.")
            if self.last_trade_at is not None:
                raise ValueError("A continuous future cannot declare last_trade_at.")
            if self.settlement_at is not None or self.settlement_type is not None:
                raise ValueError("A continuous future cannot declare settlement details.")
            if self.roll is None:
                raise ValueError("A continuous future requires roll metadata.")
            return self

        if self.expiry is None:
            raise ValueError("A dated future requires an expiry.")
        if self.roll is not None:
            raise ValueError("Roll metadata is only valid for a continuous future.")
        if self.last_trade_at is not None and self.last_trade_at.date() > self.expiry:
            raise ValueError("A future cannot stop trading after its expiry.")
        if self.settlement_at is not None:
            if self.settlement_type is None:
                raise ValueError("settlement_at requires settlement_type.")
            if self.settlement_at.date() < self.expiry:
                raise ValueError("A future cannot settle before its expiry.")
            if self.last_trade_at is not None and self.settlement_at < self.last_trade_at:
                raise ValueError("A future cannot settle before its last trade.")
        elif self.settlement_type is not None:
            raise ValueError("settlement_type requires settlement_at.")
        return self


def _version_identity(
    version: InstrumentVersion | dict[str, object],
) -> dict[str, object]:
    data = (
        version.model_dump(mode="python")
        if isinstance(version, InstrumentVersion)
        else dict(version)
    )
    data.pop("version_id", None)
    return data


class _InstrumentVersionContent(BaseModel):
    """Validated instrument content before its content address is attached."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    instrument_uid: str = Field(
        min_length=3,
        max_length=128,
        pattern=_INSTRUMENT_UID_PATTERN,
    )
    revision_of: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    asset_class: AssetClass
    canonical_symbol: str = Field(
        min_length=1,
        max_length=80,
        pattern=_MARKET_CODE_PATTERN,
    )
    venue: str = Field(
        min_length=1,
        max_length=80,
        pattern=_MARKET_CODE_PATTERN,
    )
    currency: str = Field(
        min_length=3,
        max_length=12,
        pattern=_CURRENCY_PATTERN,
    )
    timezone: str = Field(min_length=1, max_length=80)
    calendar_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=_MARKET_CODE_PATTERN,
    )
    price_multiplier: Decimal
    contract_multiplier: Decimal
    tick_size: Decimal
    lot_size: Decimal
    valid_from: datetime
    valid_to: datetime | None = None
    source: str = Field(min_length=1, max_length=200)
    source_as_of: datetime
    futures: FuturesMetadata | None = None

    @field_validator(
        "instrument_uid",
        "canonical_symbol",
        "venue",
        "currency",
        "calendar_id",
        "source",
    )
    @classmethod
    def validate_text_fields(cls, value: str, info: Any) -> str:
        return _reject_surrounding_whitespace(value, label=info.field_name)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        _reject_surrounding_whitespace(value, label="timezone")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("timezone must be a valid IANA timezone name.") from error
        return value

    @field_validator(
        "price_multiplier",
        "contract_multiplier",
        "tick_size",
        "lot_size",
    )
    @classmethod
    def validate_positive_decimal(cls, value: Decimal, info: Any) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError(f"{info.field_name} must be a finite positive Decimal.")
        return value

    @field_validator("valid_from", "valid_to", "source_as_of")
    @classmethod
    def normalize_validity_time(
        cls,
        value: datetime | None,
        info: Any,
    ) -> datetime | None:
        if value is None:
            return None
        return _utc_timestamp(value, label=info.field_name)

    @model_validator(mode="after")
    def validate_version(self) -> Self:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from.")
        if self.source_as_of > self.valid_from:
            raise ValueError("source_as_of cannot be later than the point-in-time valid_from.")
        if self.asset_class == "future" and self.futures is None:
            raise ValueError("A future instrument requires futures metadata.")
        if self.asset_class != "future" and self.futures is not None:
            raise ValueError("Futures metadata is only valid for future instruments.")
        return self


class InstrumentVersion(_InstrumentVersionContent):
    """Immutable, content-addressed instrument reference data for one time slice."""

    version_id: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_version_id(self) -> Self:
        expected_id = canonical_payload_hash(_version_identity(self))
        if self.version_id != expected_id:
            raise ValueError("version_id does not match the immutable instrument content.")
        return self


def build_instrument_version(
    *,
    instrument_uid: str,
    asset_class: AssetClass,
    canonical_symbol: str,
    venue: str,
    currency: str,
    timezone: str,
    calendar_id: str,
    price_multiplier: Decimal,
    contract_multiplier: Decimal,
    tick_size: Decimal,
    lot_size: Decimal,
    valid_from: datetime,
    source: str,
    source_as_of: datetime,
    valid_to: datetime | None = None,
    revision_of: str | None = None,
    futures: FuturesMetadata | None = None,
) -> InstrumentVersion:
    """Build a version and derive its immutable identity from validated content."""

    payload: dict[str, object] = {
        "schema_version": 1,
        "instrument_uid": instrument_uid,
        "revision_of": revision_of,
        "asset_class": asset_class,
        "canonical_symbol": canonical_symbol,
        "venue": venue,
        "currency": currency,
        "timezone": timezone,
        "calendar_id": calendar_id,
        "price_multiplier": price_multiplier,
        "contract_multiplier": contract_multiplier,
        "tick_size": tick_size,
        "lot_size": lot_size,
        "valid_from": _utc_timestamp(valid_from, label="valid_from"),
        "valid_to": (None if valid_to is None else _utc_timestamp(valid_to, label="valid_to")),
        "source": source,
        "source_as_of": _utc_timestamp(source_as_of, label="source_as_of"),
        "futures": futures,
    }
    content = _InstrumentVersionContent.model_validate(payload)
    identity = content.model_dump(mode="python")
    return InstrumentVersion.model_validate(
        {**identity, "version_id": canonical_payload_hash(identity)}
    )


def _set_identity(
    version_set: InstrumentVersionSet | dict[str, object],
) -> dict[str, object]:
    data = (
        version_set.model_dump(mode="python")
        if isinstance(version_set, InstrumentVersionSet)
        else dict(version_set)
    )
    data.pop("set_id", None)
    versions = data["versions"]
    if not isinstance(versions, (tuple, list)):
        raise TypeError("versions must be a sequence.")
    data["versions"] = [
        (version.version_id if isinstance(version, InstrumentVersion) else version["version_id"])
        for version in versions
    ]
    return data


class InstrumentNotFoundError(LookupError):
    """Raised when no instrument version is valid at the requested instant."""


class InstrumentVersionSet(BaseModel):
    """A complete, non-overlapping point-in-time revision chain."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    set_id: str = Field(pattern=_SHA256_PATTERN)
    instrument_uid: str = Field(
        min_length=3,
        max_length=128,
        pattern=_INSTRUMENT_UID_PATTERN,
    )
    versions: tuple[InstrumentVersion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_version_chain(self) -> Self:
        if any(version.instrument_uid != self.instrument_uid for version in self.versions):
            raise ValueError("Every version must belong to the version set instrument_uid.")
        if any(
            version.asset_class != self.versions[0].asset_class for version in self.versions[1:]
        ):
            raise ValueError("An instrument revision chain cannot change asset_class.")
        if tuple(sorted(self.versions, key=lambda item: item.valid_from)) != self.versions:
            raise ValueError("Instrument versions must be ordered by valid_from.")
        if self.versions[0].revision_of is not None:
            raise ValueError("The first instrument version cannot revise another version.")
        for previous, current in zip(self.versions, self.versions[1:], strict=False):
            if previous.valid_to is None:
                raise ValueError("Only the final instrument version may be open-ended.")
            if previous.valid_to > current.valid_from:
                raise ValueError("Instrument validity intervals cannot overlap.")
            if current.revision_of != previous.version_id:
                raise ValueError(
                    "Each instrument revision must reference the immediately prior version."
                )
        expected_id = canonical_payload_hash(_set_identity(self))
        if self.set_id != expected_id:
            raise ValueError("set_id does not match the immutable version chain.")
        return self

    def resolve(self, as_of: datetime) -> InstrumentVersion:
        """Resolve [valid_from, valid_to), failing closed for gaps or unknown times."""

        instant = _utc_timestamp(as_of, label="as_of")
        matches = tuple(
            version
            for version in self.versions
            if version.valid_from <= instant
            and (version.valid_to is None or instant < version.valid_to)
        )
        if len(matches) != 1:
            raise InstrumentNotFoundError(
                f"No instrument version is valid for {self.instrument_uid} at "
                f"{instant.isoformat()}."
            )
        return matches[0]


def build_instrument_version_set(
    versions: tuple[InstrumentVersion, ...] | list[InstrumentVersion],
) -> InstrumentVersionSet:
    """Build a self-verifying version set without silently sorting its lineage."""

    safe_versions = tuple(
        InstrumentVersion.model_validate(item.model_dump(mode="python")) for item in versions
    )
    if not safe_versions:
        raise ValueError("Instrument version sets require at least one version.")
    payload: dict[str, object] = {
        "schema_version": 1,
        "instrument_uid": safe_versions[0].instrument_uid,
        "versions": safe_versions,
    }
    return InstrumentVersionSet.model_validate(
        {**payload, "set_id": canonical_payload_hash(_set_identity(payload))}
    )
