from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from math import isfinite
from typing import Annotated, Literal, Self, TypeAlias, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CanonicalValue: TypeAlias = (
    bool | int | float | str | list["_CanonicalValue"] | dict[str, "_CanonicalValue"] | None
)
RiskEvaluationKind: TypeAlias = Literal[
    "research_evidence",
    "allocation_intent",
    "order_intent",
]
RiskDecisionStatus: TypeAlias = Literal["allow", "allow_with_clipping", "reject"]
RiskFindingAction: TypeAlias = Literal["observe", "clip", "reject"]
RiskFindingSeverity: TypeAlias = Literal["info", "warning", "error", "critical"]
OrderSide: TypeAlias = Literal["buy", "sell"]
OrderType: TypeAlias = Literal["market", "limit"]
KillSwitchStatus: TypeAlias = Literal["clear", "engaged", "unavailable"]
KillSwitchScope: TypeAlias = Literal[
    "global",
    "venue",
    "account",
    "strategy",
    "portfolio",
]
ResearchRiskCode: TypeAlias = Literal[
    "MIN_TRADES_NOT_MET",
    "MAX_TRADES_PER_YEAR_EXCEEDED",
    "MIN_EXPOSURE_NOT_MET",
    "MIN_ANNUALIZED_RETURN_NOT_MET",
    "MAX_DRAWDOWN_EXCEEDED",
    "MAX_CASH_STREAK_RATIO_EXCEEDED",
    "MAX_CASH_STREAK_BARS_EXCEEDED",
]


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Canonical payloads cannot contain non-finite decimals.")
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _normalize_exact_decimal(value: object, *, label: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError(f"{label} must be an exact finite decimal; floats are forbidden.")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, (int, str)):
        try:
            parsed = Decimal(str(value))
        except Exception as error:
            raise ValueError(f"{label} must be an exact finite decimal.") from error
    else:
        raise ValueError(f"{label} must be an exact finite decimal.")
    if not parsed.is_finite():
        raise ValueError(f"{label} must be an exact finite decimal.")
    if parsed == 0:
        return Decimal(0)
    sign, digits, exponent = parsed.as_tuple()
    canonical_digits = list(digits)
    canonical_exponent = cast(int, exponent)
    while canonical_digits and canonical_digits[-1] == 0:
        canonical_digits.pop()
        canonical_exponent += 1
    return Decimal((sign, tuple(canonical_digits), canonical_exponent))


def _terminating_decimal(value: Fraction) -> Decimal:
    numerator = value.numerator
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:  # pragma: no cover - finite Decimal arithmetic always terminates
        raise ValueError("Risk arithmetic produced a non-terminating decimal.")
    scale = max(twos, fives)
    scaled = numerator * (5 ** (scale - fives)) * (2 ** (scale - twos))
    sign = int(scaled < 0)
    digits = tuple(int(digit) for digit in str(abs(scaled)))
    return _normalize_exact_decimal(
        Decimal((sign, digits, -scale)),
        label="Risk arithmetic result",
    )


def _decimal_sum(*values: Decimal) -> Decimal:
    return _terminating_decimal(sum((Fraction(value) for value in values), Fraction()))


def _decimal_difference(left: Decimal, right: Decimal) -> Decimal:
    return _terminating_decimal(Fraction(left) - Fraction(right))


def _decimal_product(*values: Decimal) -> Decimal:
    result = Fraction(1)
    for value in values:
        result *= Fraction(value)
    return _terminating_decimal(result)


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
    raise TypeError(f"Canonical payloads do not support values of type {type(value).__name__}.")


def canonical_json(value: object) -> str:
    """Serialize a risk artifact deterministically for evidence hashing."""

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


def _normalize_text(value: object, *, label: str, maximum_length: int = 160) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty.")
    if len(normalized) > maximum_length:
        raise ValueError(f"{label} must not exceed {maximum_length} characters.")
    return normalized


def _normalize_optional_text(
    value: object,
    *,
    label: str,
    maximum_length: int = 160,
) -> str | None:
    if value is None:
        return None
    return _normalize_text(value, label=label, maximum_length=maximum_length)


def _canonical_symbol(value: object) -> str:
    return _normalize_text(value, label="Risk symbol", maximum_length=80).upper()


def _canonical_currency(value: object) -> str:
    normalized = _normalize_text(value, label="Quote currency", maximum_length=16).upper()
    if not normalized.replace("_", "").isalnum():
        raise ValueError("Quote currency must contain only letters, numbers, or underscores.")
    return normalized


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware.")
    return value.astimezone(UTC)


def _identity_without_hash(model: BaseModel, hash_field: str) -> dict[str, object]:
    identity = model.model_dump(mode="python")
    identity.pop(hash_field, None)
    return identity


class _FrozenContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        allow_inf_nan=False,
    )


class AssetWeight(_FrozenContract):
    """One canonical asset weight; policy validation is intentionally separate."""

    symbol: str
    weight: float

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _canonical_symbol(value)


def _normalize_weight_entries(value: object) -> tuple[AssetWeight, ...]:
    if isinstance(value, Mapping):
        raw_entries: Sequence[object] = tuple(
            {"symbol": symbol, "weight": weight} for symbol, weight in value.items()
        )
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raw_entries = value
    else:
        raise ValueError("Allocation weights must be a mapping or sequence.")

    entries: list[AssetWeight] = []
    for raw_entry in raw_entries:
        if isinstance(raw_entry, AssetWeight):
            entry = AssetWeight.model_validate(raw_entry.model_dump(mode="python"))
        else:
            entry = AssetWeight.model_validate(raw_entry)
        entries.append(entry)
    return tuple(sorted(entries, key=lambda item: (item.symbol, item.weight)))


class ResearchRiskLimits(_FrozenContract):
    """Legacy-compatible post-run evidence gates used by strategy selection."""

    minimum_trades: int = Field(default=0, ge=0)
    maximum_trades_per_year: float | None = Field(default=None, gt=0)
    minimum_exposure: float = Field(default=0, ge=0, le=1)
    minimum_annualized_return: float | None = Field(default=None, ge=-1)
    maximum_drawdown: float | None = Field(default=None, gt=0, le=1)
    maximum_cash_streak_ratio: float = Field(default=1, gt=0, le=1)
    maximum_cash_streak_bars: int | None = Field(default=None, ge=1)


class AllocationRiskLimits(_FrozenContract):
    """Validate-only limits for a fully specified target allocation."""

    minimum_assets: int = Field(default=2, ge=1, le=1_000)
    maximum_assets: int = Field(default=8, ge=1, le=1_000)
    required_weight_sum: float = Field(default=1, ge=0)
    weight_sum_tolerance: float = Field(default=1e-10, gt=0, le=1e-3)
    long_only: bool = True
    leverage_allowed: bool = False
    maximum_gross_exposure: float = Field(default=1, gt=0)
    maximum_net_exposure: float = Field(default=1, gt=0)
    maximum_asset_weight: float | None = Field(default=None, gt=0, le=1)
    maximum_turnover_ratio: float | None = Field(default=None, ge=0)
    minimum_cash_reserve_ratio: float | None = Field(default=None, ge=0, le=1)
    maximum_drawdown: float | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if self.minimum_assets > self.maximum_assets:
            raise ValueError("Minimum assets cannot exceed maximum assets.")
        if self.required_weight_sum > self.maximum_gross_exposure:
            raise ValueError("Required weight sum cannot exceed maximum gross exposure.")
        if self.required_weight_sum > self.maximum_net_exposure:
            raise ValueError("Required weight sum cannot exceed maximum net exposure.")
        if (
            not self.leverage_allowed
            and self.maximum_gross_exposure > 1 + self.weight_sum_tolerance
        ):
            raise ValueError("Maximum gross exposure cannot exceed 1 when leverage is disabled.")
        return self


class OrderRiskLimits(_FrozenContract):
    """Fail-closed limits for one exact-decimal order intent."""

    long_only: Literal[True] = True
    maximum_order_notional: Decimal = Field(gt=0)
    maximum_resulting_position: Decimal = Field(ge=0)
    maximum_active_orders: int = Field(ge=0, strict=True)
    minimum_cash_reserve: Decimal = Field(default=Decimal(0), ge=0)
    maximum_price_age_seconds: int = Field(ge=0, strict=True)
    maximum_kill_switch_age_seconds: int = Field(ge=0, strict=True)
    market_order_price_buffer_ratio: Decimal = Field(default=Decimal(0), ge=0, lt=1)
    order_fee_buffer_ratio: Decimal = Field(default=Decimal(0), ge=0, lt=1)

    @field_validator(
        "maximum_order_notional",
        "maximum_resulting_position",
        "minimum_cash_reserve",
        "market_order_price_buffer_ratio",
        "order_fee_buffer_ratio",
        mode="before",
    )
    @classmethod
    def normalize_economic_limits(cls, value: object, info: object) -> Decimal:
        field_name = getattr(info, "field_name", "order risk limit")
        return _normalize_exact_decimal(value, label=str(field_name))


class RiskRuleSet(_FrozenContract):
    """Immutable, content-addressed legacy research or allocation rules."""

    schema_version: Literal[1] = 1
    evaluation_kind: Literal["research_evidence", "allocation_intent"]
    rule_set_id: str
    rule_set_version: str
    compatibility_profile: str
    research_limits: ResearchRiskLimits | None = None
    allocation_limits: AllocationRiskLimits | None = None
    rules_hash: str = Field(pattern=_SHA256_PATTERN)

    @field_validator(
        "rule_set_id",
        "rule_set_version",
        "compatibility_profile",
        mode="before",
    )
    @classmethod
    def normalize_identifiers(cls, value: object, info: object) -> str:
        field_name = getattr(info, "field_name", "risk rule identifier")
        return _normalize_text(value, label=str(field_name))

    @model_validator(mode="after")
    def validate_rule_set(self) -> Self:
        if self.evaluation_kind == "research_evidence":
            if self.research_limits is None or self.allocation_limits is not None:
                raise ValueError("Research rule sets require only research risk limits.")
        elif self.allocation_limits is None or self.research_limits is not None:
            raise ValueError("Allocation rule sets require only allocation risk limits.")
        expected_hash = canonical_payload_hash(_identity_without_hash(self, "rules_hash"))
        if self.rules_hash != expected_hash:
            raise ValueError("Risk rules_hash does not match its immutable rules.")
        return self


class OrderRiskRuleSet(_FrozenContract):
    """Immutable order rules kept separate so legacy payloads remain byte-stable."""

    schema_version: Literal[1] = 1
    evaluation_kind: Literal["order_intent"] = "order_intent"
    rule_set_id: str
    rule_set_version: str
    compatibility_profile: str
    order_limits: OrderRiskLimits
    rules_hash: str = Field(pattern=_SHA256_PATTERN)

    @field_validator(
        "rule_set_id",
        "rule_set_version",
        "compatibility_profile",
        mode="before",
    )
    @classmethod
    def normalize_identifiers(cls, value: object, info: object) -> str:
        field_name = getattr(info, "field_name", "order risk rule identifier")
        return _normalize_text(value, label=str(field_name))

    @model_validator(mode="after")
    def validate_rule_set(self) -> Self:
        expected_hash = canonical_payload_hash(_identity_without_hash(self, "rules_hash"))
        if self.rules_hash != expected_hash:
            raise ValueError("Order risk rules_hash does not match its immutable rules.")
        return self


def build_research_rule_set(
    limits: ResearchRiskLimits,
    *,
    # Immutable v1 wire identifier: changing it would invalidate existing
    # content-addressed research evidence. It is independent of product branding.
    rule_set_id: str = "alphapilot.research",
    rule_set_version: str = "1.0.0",
    compatibility_profile: str = "legacy-research-v1",
) -> RiskRuleSet:
    """Build and self-verify a research ruleset."""

    safe_limits = ResearchRiskLimits.model_validate(limits.model_dump(mode="python"))
    identity: dict[str, object] = {
        "schema_version": 1,
        "evaluation_kind": "research_evidence",
        "rule_set_id": _normalize_text(rule_set_id, label="rule_set_id"),
        "rule_set_version": _normalize_text(
            rule_set_version,
            label="rule_set_version",
        ),
        "compatibility_profile": _normalize_text(
            compatibility_profile,
            label="compatibility_profile",
        ),
        "research_limits": safe_limits.model_dump(mode="python"),
        "allocation_limits": None,
    }
    return RiskRuleSet.model_validate({**identity, "rules_hash": canonical_payload_hash(identity)})


def build_allocation_rule_set(
    limits: AllocationRiskLimits,
    *,
    # Immutable v1 wire identifier: changing it would invalidate existing
    # content-addressed allocation evidence. It is independent of product branding.
    rule_set_id: str = "alphapilot.allocation",
    rule_set_version: str = "1.0.0",
    compatibility_profile: str = "legacy-portfolio-v1",
) -> RiskRuleSet:
    """Build and self-verify an allocation ruleset."""

    safe_limits = AllocationRiskLimits.model_validate(limits.model_dump(mode="python"))
    identity: dict[str, object] = {
        "schema_version": 1,
        "evaluation_kind": "allocation_intent",
        "rule_set_id": _normalize_text(rule_set_id, label="rule_set_id"),
        "rule_set_version": _normalize_text(
            rule_set_version,
            label="rule_set_version",
        ),
        "compatibility_profile": _normalize_text(
            compatibility_profile,
            label="compatibility_profile",
        ),
        "research_limits": None,
        "allocation_limits": safe_limits.model_dump(mode="python"),
    }
    return RiskRuleSet.model_validate({**identity, "rules_hash": canonical_payload_hash(identity)})


def build_order_rule_set(
    limits: OrderRiskLimits,
    *,
    rule_set_id: str = "quantsieve.order",
    rule_set_version: str = "1.0.0",
    compatibility_profile: str = "paper-order-v1",
) -> OrderRiskRuleSet:
    """Build and self-verify an exact-decimal order ruleset."""

    safe_limits = OrderRiskLimits.model_validate(limits.model_dump(mode="python"))
    identity: dict[str, object] = {
        "schema_version": 1,
        "evaluation_kind": "order_intent",
        "rule_set_id": _normalize_text(rule_set_id, label="rule_set_id"),
        "rule_set_version": _normalize_text(
            rule_set_version,
            label="rule_set_version",
        ),
        "compatibility_profile": _normalize_text(
            compatibility_profile,
            label="compatibility_profile",
        ),
        "order_limits": safe_limits.model_dump(mode="python"),
    }
    return OrderRiskRuleSet.model_validate(
        {**identity, "rules_hash": canonical_payload_hash(identity)}
    )


class KillSwitchSnapshot(_FrozenContract):
    """Applicable kill-switch state supplied to a pure risk evaluation."""

    schema_version: Literal[1] = 1
    status: KillSwitchStatus
    scope: KillSwitchScope
    scope_id_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    revision: int = Field(ge=0)
    reason_code: str | None = None
    activated_at: datetime | None = None
    expires_at: datetime | None = None
    source: str
    state_hash: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("reason_code", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> str | None:
        return _normalize_optional_text(value, label="Kill-switch reason code")

    @field_validator("source", mode="before")
    @classmethod
    def normalize_source(cls, value: object) -> str:
        return _normalize_text(value, label="Kill-switch source")

    @field_validator("activated_at", "expires_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "Kill-switch timestamp")
        return _aware_utc(value, label=str(field_name))

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.scope == "global" and self.scope_id_hash is not None:
            raise ValueError("A global kill switch must not carry a scope id.")
        if self.scope != "global" and self.scope_id_hash is None:
            raise ValueError("A scoped kill switch requires an opaque scope id hash.")
        if self.status == "clear":
            if (
                self.reason_code is not None
                or self.activated_at is not None
                or self.expires_at is not None
            ):
                raise ValueError("A clear kill switch cannot carry activation details.")
        elif self.status == "engaged":
            if self.reason_code is None or self.activated_at is None:
                raise ValueError("An engaged kill switch requires a reason and activation time.")
            if self.expires_at is not None and self.expires_at <= self.activated_at:
                raise ValueError("Kill-switch expiration must follow its activation time.")
        elif (
            self.reason_code is None or self.activated_at is not None or self.expires_at is not None
        ):
            raise ValueError("An unavailable kill switch requires only a failure reason.")
        expected_hash = canonical_payload_hash(_identity_without_hash(self, "state_hash"))
        if self.state_hash != expected_hash:
            raise ValueError("Kill-switch state_hash does not match its immutable state.")
        return self


def build_kill_switch_snapshot(
    *,
    status: KillSwitchStatus = "clear",
    scope: KillSwitchScope = "global",
    scope_id_hash: str | None = None,
    revision: int = 0,
    reason_code: str | None = None,
    activated_at: datetime | None = None,
    expires_at: datetime | None = None,
    source: str = "static",
) -> KillSwitchSnapshot:
    """Build and self-verify an applicable kill-switch snapshot."""

    normalized_reason = _normalize_optional_text(
        reason_code,
        label="Kill-switch reason code",
    )
    normalized_activated = (
        None
        if activated_at is None
        else _aware_utc(activated_at, label="Kill-switch activation time")
    )
    normalized_expires = (
        None if expires_at is None else _aware_utc(expires_at, label="Kill-switch expiration time")
    )
    identity: dict[str, object] = {
        "schema_version": 1,
        "status": status,
        "scope": scope,
        "scope_id_hash": scope_id_hash,
        "revision": revision,
        "reason_code": normalized_reason,
        "activated_at": normalized_activated,
        "expires_at": normalized_expires,
        "source": _normalize_text(source, label="Kill-switch source"),
    }
    return KillSwitchSnapshot.model_validate(
        {**identity, "state_hash": canonical_payload_hash(identity)}
    )


class ResearchEvidence(_FrozenContract):
    """The exact subset of backtest metrics used by the legacy participation gate."""

    trades: int = Field(ge=0)
    trades_per_year: float = Field(ge=0)
    exposure_ratio: float = Field(ge=0, le=1)
    annualized_return: float
    max_drawdown: float = Field(ge=-1, le=0)
    max_cash_streak_ratio: float = Field(ge=0, le=1)
    max_cash_streak_bars: int = Field(ge=0)


def research_risk_breach_codes(
    *,
    trades: int,
    trades_per_year: float,
    exposure_ratio: float,
    annualized_return: float,
    max_drawdown: float,
    max_cash_streak_ratio: float,
    max_cash_streak_bars: int,
    minimum_trades: int,
    maximum_trades_per_year: float | None,
    minimum_exposure: float,
    minimum_annualized_return: float | None,
    maximum_drawdown: float | None,
    maximum_cash_streak_ratio: float,
    maximum_cash_streak_bars: int | None,
) -> tuple[ResearchRiskCode, ...]:
    """Return legacy participation breaches without allocating risk contracts.

    This is the single comparison source for optimizer hot paths and the
    explainable research evaluator. Callers that need validated, hashed evidence
    should use ``evaluate_research_risk``; this lightweight function deliberately
    preserves the optimizer's existing boundary semantics.
    """

    breaches: list[ResearchRiskCode] = []
    if not trades >= minimum_trades:
        breaches.append("MIN_TRADES_NOT_MET")
    if maximum_trades_per_year is not None and not trades_per_year <= maximum_trades_per_year:
        breaches.append("MAX_TRADES_PER_YEAR_EXCEEDED")
    if not exposure_ratio >= minimum_exposure:
        breaches.append("MIN_EXPOSURE_NOT_MET")
    if minimum_annualized_return is not None and not annualized_return >= minimum_annualized_return:
        breaches.append("MIN_ANNUALIZED_RETURN_NOT_MET")
    if maximum_drawdown is not None and not abs(max_drawdown) <= maximum_drawdown:
        breaches.append("MAX_DRAWDOWN_EXCEEDED")
    if not max_cash_streak_ratio <= maximum_cash_streak_ratio:
        breaches.append("MAX_CASH_STREAK_RATIO_EXCEEDED")
    if (
        maximum_cash_streak_bars is not None
        and not max_cash_streak_bars <= maximum_cash_streak_bars
    ):
        breaches.append("MAX_CASH_STREAK_BARS_EXCEEDED")
    return tuple(breaches)


def research_risk_allows(
    *,
    trades: int,
    trades_per_year: float,
    exposure_ratio: float,
    annualized_return: float,
    max_drawdown: float,
    max_cash_streak_ratio: float,
    max_cash_streak_bars: int,
    minimum_trades: int,
    maximum_trades_per_year: float | None,
    minimum_exposure: float,
    minimum_annualized_return: float | None,
    maximum_drawdown: float | None,
    maximum_cash_streak_ratio: float,
    maximum_cash_streak_bars: int | None,
) -> bool:
    """Return whether the shared research rules allow candidate participation."""

    return not research_risk_breach_codes(
        trades=trades,
        trades_per_year=trades_per_year,
        exposure_ratio=exposure_ratio,
        annualized_return=annualized_return,
        max_drawdown=max_drawdown,
        max_cash_streak_ratio=max_cash_streak_ratio,
        max_cash_streak_bars=max_cash_streak_bars,
        minimum_trades=minimum_trades,
        maximum_trades_per_year=maximum_trades_per_year,
        minimum_exposure=minimum_exposure,
        minimum_annualized_return=minimum_annualized_return,
        maximum_drawdown=maximum_drawdown,
        maximum_cash_streak_ratio=maximum_cash_streak_ratio,
        maximum_cash_streak_bars=maximum_cash_streak_bars,
    )


class AllocationIntent(_FrozenContract):
    """A proposed allocation plus optional state needed by enabled rules."""

    symbols: tuple[str, ...]
    requested_weights: tuple[AssetWeight, ...]
    proposed_turnover_ratio: float | None = None
    post_trade_cash_ratio: float | None = None
    current_drawdown: float | None = None

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, Sequence) or isinstance(
            value,
            (str, bytes, bytearray),
        ):
            raise ValueError("Allocation symbols must be a sequence.")
        return tuple(_canonical_symbol(symbol) for symbol in value)

    @field_validator("requested_weights", mode="before")
    @classmethod
    def normalize_requested_weights(cls, value: object) -> tuple[AssetWeight, ...]:
        return _normalize_weight_entries(value)


class OrderRiskIntent(_FrozenContract):
    """One order proposal; the evaluator never mutates or clips it."""

    order_id: str
    symbol: str
    quote_currency: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)

    @field_validator("order_id", mode="before")
    @classmethod
    def normalize_order_id(cls, value: object) -> str:
        return _normalize_text(value, label="Order id", maximum_length=200)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _canonical_symbol(value)

    @field_validator("quote_currency", mode="before")
    @classmethod
    def normalize_quote_currency(cls, value: object) -> str:
        return _canonical_currency(value)

    @field_validator("quantity", "limit_price", mode="before")
    @classmethod
    def normalize_economic_values(cls, value: object, info: object) -> Decimal | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "order amount")
        return _normalize_exact_decimal(value, label=str(field_name))

    @model_validator(mode="after")
    def validate_order_type(self) -> Self:
        if self.order_type == "market" and self.limit_price is not None:
            raise ValueError("A market order must not carry a limit price.")
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("A limit order requires a limit price.")
        return self


class OrderRiskState(_FrozenContract):
    """Account and reservation state observed before one order decision."""

    account_id_hash: str = Field(pattern=_SHA256_PATTERN)
    account_revision: int = Field(ge=0, strict=True)
    account_state_hash: str = Field(pattern=_SHA256_PATTERN)
    symbol: str
    quote_currency: str
    cash_balance: Decimal
    position_quantity: Decimal
    reserved_buy_cash: Decimal = Field(default=Decimal(0), ge=0)
    reserved_buy_quantity: Decimal = Field(default=Decimal(0), ge=0)
    reserved_sell_quantity: Decimal = Field(default=Decimal(0), ge=0)
    active_order_count: int = Field(default=0, ge=0, strict=True)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _canonical_symbol(value)

    @field_validator("quote_currency", mode="before")
    @classmethod
    def normalize_quote_currency(cls, value: object) -> str:
        return _canonical_currency(value)

    @field_validator(
        "cash_balance",
        "position_quantity",
        "reserved_buy_cash",
        "reserved_buy_quantity",
        "reserved_sell_quantity",
        mode="before",
    )
    @classmethod
    def normalize_economic_values(cls, value: object, info: object) -> Decimal:
        field_name = getattr(info, "field_name", "order state amount")
        return _normalize_exact_decimal(value, label=str(field_name))


class OrderPriceEvidence(_FrozenContract):
    """Point-in-time price evidence used for conservative order sizing."""

    symbol: str
    quote_currency: str
    reference_price: Decimal = Field(gt=0)
    observed_at: datetime
    available_at: datetime
    source: str
    snapshot_hash: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _canonical_symbol(value)

    @field_validator("quote_currency", mode="before")
    @classmethod
    def normalize_quote_currency(cls, value: object) -> str:
        return _canonical_currency(value)

    @field_validator("reference_price", mode="before")
    @classmethod
    def normalize_reference_price(cls, value: object) -> Decimal:
        return _normalize_exact_decimal(value, label="Reference price")

    @field_validator("observed_at", "available_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime, info: object) -> datetime:
        field_name = getattr(info, "field_name", "Price timestamp")
        return _aware_utc(value, label=str(field_name))

    @field_validator("source", mode="before")
    @classmethod
    def normalize_source(cls, value: object) -> str:
        return _normalize_text(value, label="Price evidence source")


class ResearchRiskRequest(_FrozenContract):
    """Content-addressed request for post-run research evidence evaluation."""

    schema_version: Literal[1] = 1
    evaluation_kind: Literal["research_evidence"] = "research_evidence"
    evaluation_id: str
    evaluated_at: datetime
    source_calculation_version: str
    rule_set: RiskRuleSet
    kill_switch: KillSwitchSnapshot
    evidence: ResearchEvidence
    request_hash: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("evaluation_id", "source_calculation_version", mode="before")
    @classmethod
    def normalize_identifiers(cls, value: object, info: object) -> str:
        field_name = getattr(info, "field_name", "risk request identifier")
        return _normalize_text(value, label=str(field_name))

    @field_validator("evaluated_at")
    @classmethod
    def normalize_evaluated_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, label="Risk evaluation time")

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.rule_set.evaluation_kind != self.evaluation_kind:
            raise ValueError("Research request requires a research rule set.")
        expected_hash = canonical_payload_hash(_identity_without_hash(self, "request_hash"))
        if self.request_hash != expected_hash:
            raise ValueError("Risk request_hash does not match its immutable request.")
        return self


class AllocationRiskRequest(_FrozenContract):
    """Content-addressed request for validate-only allocation evaluation."""

    schema_version: Literal[1] = 1
    evaluation_kind: Literal["allocation_intent"] = "allocation_intent"
    evaluation_id: str
    evaluated_at: datetime
    source_calculation_version: str
    rule_set: RiskRuleSet
    kill_switch: KillSwitchSnapshot
    intent: AllocationIntent
    request_hash: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("evaluation_id", "source_calculation_version", mode="before")
    @classmethod
    def normalize_identifiers(cls, value: object, info: object) -> str:
        field_name = getattr(info, "field_name", "risk request identifier")
        return _normalize_text(value, label=str(field_name))

    @field_validator("evaluated_at")
    @classmethod
    def normalize_evaluated_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, label="Risk evaluation time")

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.rule_set.evaluation_kind != self.evaluation_kind:
            raise ValueError("Allocation request requires an allocation rule set.")
        expected_hash = canonical_payload_hash(_identity_without_hash(self, "request_hash"))
        if self.request_hash != expected_hash:
            raise ValueError("Risk request_hash does not match its immutable request.")
        return self


class OrderRiskRequest(_FrozenContract):
    """Content-addressed request for one pre-trade order decision."""

    schema_version: Literal[1] = 1
    evaluation_kind: Literal["order_intent"] = "order_intent"
    evaluation_id: str
    evaluated_at: datetime
    source_calculation_version: str
    rule_set: OrderRiskRuleSet
    kill_switch: KillSwitchSnapshot
    kill_switch_observed_at: datetime
    kill_switch_available_at: datetime
    intent: OrderRiskIntent
    state: OrderRiskState
    price_evidence: OrderPriceEvidence
    request_hash: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("evaluation_id", "source_calculation_version", mode="before")
    @classmethod
    def normalize_identifiers(cls, value: object, info: object) -> str:
        field_name = getattr(info, "field_name", "risk request identifier")
        return _normalize_text(value, label=str(field_name))

    @field_validator(
        "evaluated_at",
        "kill_switch_observed_at",
        "kill_switch_available_at",
    )
    @classmethod
    def normalize_request_time(cls, value: datetime, info: object) -> datetime:
        field_name = getattr(info, "field_name", "Order risk timestamp")
        return _aware_utc(value, label=str(field_name))

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.rule_set.evaluation_kind != self.evaluation_kind:
            raise ValueError("Order request requires an order rule set.")
        expected_hash = canonical_payload_hash(_identity_without_hash(self, "request_hash"))
        if self.request_hash != expected_hash:
            raise ValueError("Risk request_hash does not match its immutable request.")
        return self


RiskRequest: TypeAlias = Annotated[
    ResearchRiskRequest | AllocationRiskRequest | OrderRiskRequest,
    Field(discriminator="evaluation_kind"),
]


class RiskFinding(_FrozenContract):
    """One stable, safely renderable rule result."""

    sequence: int = Field(ge=0)
    rule_id: str
    code: str
    action: RiskFindingAction
    severity: RiskFindingSeverity
    subject: str | None = None
    actual: str | None = None
    limit: str | None = None
    unit: str | None = None
    message_key: str

    @field_validator("rule_id", "code", "message_key", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object, info: object) -> str:
        field_name = getattr(info, "field_name", "risk finding field")
        return _normalize_text(value, label=str(field_name))

    @field_validator("subject", "actual", "limit", "unit", mode="before")
    @classmethod
    def normalize_optional_fields(cls, value: object, info: object) -> str | None:
        field_name = getattr(info, "field_name", "risk finding field")
        return _normalize_optional_text(value, label=str(field_name))


class RiskDecision(_FrozenContract):
    """Self-verifying decision; ``decision_hash`` is its sole decision identity."""

    schema_version: Literal[1] = 1
    request: RiskRequest
    decision: RiskDecisionStatus
    effective_weights: tuple[AssetWeight, ...] | None = None
    findings: tuple[RiskFinding, ...] = ()
    decision_hash: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("effective_weights", mode="before")
    @classmethod
    def normalize_effective_weights(
        cls,
        value: object,
    ) -> tuple[AssetWeight, ...] | None:
        if value is None:
            return None
        return _normalize_weight_entries(value)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        expected_sequences = tuple(range(len(self.findings)))
        actual_sequences = tuple(finding.sequence for finding in self.findings)
        if actual_sequences != expected_sequences:
            raise ValueError("Risk finding sequence must be contiguous and ordered.")
        identities = tuple((finding.code, finding.subject) for finding in self.findings)
        if len(set(identities)) != len(identities):
            raise ValueError("Risk findings must have unique code and subject pairs.")

        reject_findings = tuple(finding for finding in self.findings if finding.action == "reject")
        clip_findings = tuple(finding for finding in self.findings if finding.action == "clip")
        if self.decision == "reject":
            if not reject_findings:
                raise ValueError("A rejected risk decision requires a reject finding.")
            if self.effective_weights is not None:
                raise ValueError("A rejected risk decision cannot expose executable weights.")
        elif self.decision == "allow_with_clipping":
            if reject_findings or not clip_findings:
                raise ValueError(
                    "A clipped risk decision requires clips and cannot contain rejects."
                )
            if not isinstance(self.request, AllocationRiskRequest):
                raise ValueError("Only allocation intents can be clipped.")
            if self.effective_weights is None:
                raise ValueError("A clipped allocation requires effective weights.")
            if self.effective_weights == self.request.intent.requested_weights:
                raise ValueError("Clipped weights must differ from requested weights.")
        else:
            if reject_findings or clip_findings:
                raise ValueError("An allowed risk decision cannot reject or clip.")
            if isinstance(self.request, ResearchRiskRequest):
                if self.effective_weights is not None:
                    raise ValueError(
                        "Research evidence decisions cannot expose allocation weights."
                    )
            elif isinstance(self.request, AllocationRiskRequest):
                if self.effective_weights != self.request.intent.requested_weights:
                    raise ValueError(
                        "Validate-only allocation decisions must preserve requested weights."
                    )
            elif self.effective_weights is not None:
                raise ValueError("Order intent decisions cannot expose allocation weights.")

        expected_hash = canonical_payload_hash(_identity_without_hash(self, "decision_hash"))
        if self.decision_hash != expected_hash:
            raise ValueError("Risk decision_hash does not match its immutable decision.")
        return self

    @property
    def decision_id(self) -> str:
        """Return the immutable content hash used as the only decision id."""

        return self.decision_hash


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _validated_copy(value: _ModelT, model_type: type[_ModelT]) -> _ModelT:
    return model_type.model_validate(value.model_dump(mode="python"))


def build_research_risk_request(
    *,
    evaluation_id: str,
    evaluated_at: datetime,
    source_calculation_version: str,
    rule_set: RiskRuleSet,
    kill_switch: KillSwitchSnapshot,
    evidence: ResearchEvidence,
) -> ResearchRiskRequest:
    """Build a self-verifying research evaluation request."""

    safe_rules = _validated_copy(rule_set, RiskRuleSet)
    safe_switch = _validated_copy(kill_switch, KillSwitchSnapshot)
    safe_evidence = _validated_copy(evidence, ResearchEvidence)
    identity: dict[str, object] = {
        "schema_version": 1,
        "evaluation_kind": "research_evidence",
        "evaluation_id": _normalize_text(evaluation_id, label="evaluation_id"),
        "evaluated_at": _aware_utc(
            evaluated_at,
            label="Risk evaluation time",
        ),
        "source_calculation_version": _normalize_text(
            source_calculation_version,
            label="source_calculation_version",
        ),
        "rule_set": safe_rules.model_dump(mode="python"),
        "kill_switch": safe_switch.model_dump(mode="python"),
        "evidence": safe_evidence.model_dump(mode="python"),
    }
    return ResearchRiskRequest.model_validate(
        {**identity, "request_hash": canonical_payload_hash(identity)}
    )


def build_allocation_risk_request(
    *,
    evaluation_id: str,
    evaluated_at: datetime,
    source_calculation_version: str,
    rule_set: RiskRuleSet,
    kill_switch: KillSwitchSnapshot,
    intent: AllocationIntent,
) -> AllocationRiskRequest:
    """Build a self-verifying allocation evaluation request."""

    safe_rules = _validated_copy(rule_set, RiskRuleSet)
    safe_switch = _validated_copy(kill_switch, KillSwitchSnapshot)
    safe_intent = _validated_copy(intent, AllocationIntent)
    identity: dict[str, object] = {
        "schema_version": 1,
        "evaluation_kind": "allocation_intent",
        "evaluation_id": _normalize_text(evaluation_id, label="evaluation_id"),
        "evaluated_at": _aware_utc(
            evaluated_at,
            label="Risk evaluation time",
        ),
        "source_calculation_version": _normalize_text(
            source_calculation_version,
            label="source_calculation_version",
        ),
        "rule_set": safe_rules.model_dump(mode="python"),
        "kill_switch": safe_switch.model_dump(mode="python"),
        "intent": safe_intent.model_dump(mode="python"),
    }
    return AllocationRiskRequest.model_validate(
        {**identity, "request_hash": canonical_payload_hash(identity)}
    )


def build_order_risk_request(
    *,
    evaluation_id: str,
    evaluated_at: datetime,
    source_calculation_version: str,
    rule_set: OrderRiskRuleSet,
    kill_switch: KillSwitchSnapshot,
    kill_switch_observed_at: datetime,
    kill_switch_available_at: datetime,
    intent: OrderRiskIntent,
    state: OrderRiskState,
    price_evidence: OrderPriceEvidence,
) -> OrderRiskRequest:
    """Build a self-verifying pre-trade order evaluation request."""

    safe_rules = _validated_copy(rule_set, OrderRiskRuleSet)
    safe_switch = _validated_copy(kill_switch, KillSwitchSnapshot)
    safe_intent = _validated_copy(intent, OrderRiskIntent)
    safe_state = _validated_copy(state, OrderRiskState)
    safe_price = _validated_copy(price_evidence, OrderPriceEvidence)
    identity: dict[str, object] = {
        "schema_version": 1,
        "evaluation_kind": "order_intent",
        "evaluation_id": _normalize_text(evaluation_id, label="evaluation_id"),
        "evaluated_at": _aware_utc(
            evaluated_at,
            label="Risk evaluation time",
        ),
        "source_calculation_version": _normalize_text(
            source_calculation_version,
            label="source_calculation_version",
        ),
        "rule_set": safe_rules.model_dump(mode="python"),
        "kill_switch": safe_switch.model_dump(mode="python"),
        "kill_switch_observed_at": _aware_utc(
            kill_switch_observed_at,
            label="Kill-switch observation time",
        ),
        "kill_switch_available_at": _aware_utc(
            kill_switch_available_at,
            label="Kill-switch availability time",
        ),
        "intent": safe_intent.model_dump(mode="python"),
        "state": safe_state.model_dump(mode="python"),
        "price_evidence": safe_price.model_dump(mode="python"),
    }
    return OrderRiskRequest.model_validate(
        {**identity, "request_hash": canonical_payload_hash(identity)}
    )


def _render_number(value: Decimal | int | float) -> str:
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    return canonical_json(value)


def _append_finding(
    findings: list[RiskFinding],
    *,
    rule_id: str,
    code: str,
    action: RiskFindingAction = "reject",
    severity: RiskFindingSeverity = "error",
    subject: str | None = None,
    actual: str | None = None,
    limit: str | None = None,
    unit: str | None = None,
    message_key: str,
) -> None:
    findings.append(
        RiskFinding(
            sequence=len(findings),
            rule_id=rule_id,
            code=code,
            action=action,
            severity=severity,
            subject=subject,
            actual=actual,
            limit=limit,
            unit=unit,
            message_key=message_key,
        )
    )


def _append_execution_kill_switch(
    findings: list[RiskFinding],
    snapshot: KillSwitchSnapshot,
) -> None:
    if snapshot.status == "engaged":
        _append_finding(
            findings,
            rule_id="system.kill_switch",
            code="KILL_SWITCH_ENGAGED",
            severity="critical",
            subject=snapshot.scope,
            actual=snapshot.status,
            limit="clear",
            message_key="risk.kill_switch.engaged",
        )
    elif snapshot.status == "unavailable":
        _append_finding(
            findings,
            rule_id="system.kill_switch",
            code="KILL_SWITCH_STATE_UNAVAILABLE",
            severity="critical",
            subject=snapshot.scope,
            actual=snapshot.status,
            limit="clear",
            message_key="risk.kill_switch.state_unavailable",
        )


def _build_decision(
    request: ResearchRiskRequest | AllocationRiskRequest | OrderRiskRequest,
    *,
    findings: Sequence[RiskFinding],
    effective_weights: Sequence[AssetWeight] | None,
) -> RiskDecision:
    safe_findings = tuple(
        RiskFinding.model_validate(finding.model_dump(mode="python")) for finding in findings
    )
    decision: RiskDecisionStatus = (
        "reject"
        if any(finding.action == "reject" for finding in safe_findings)
        else "allow_with_clipping"
        if any(finding.action == "clip" for finding in safe_findings)
        else "allow"
    )
    safe_weights = (
        None
        if effective_weights is None
        else tuple(
            AssetWeight.model_validate(weight.model_dump(mode="python"))
            for weight in effective_weights
        )
    )
    identity: dict[str, object] = {
        "schema_version": 1,
        "request": request.model_dump(mode="python"),
        "decision": decision,
        "effective_weights": (
            None
            if safe_weights is None
            else tuple(weight.model_dump(mode="python") for weight in safe_weights)
        ),
        "findings": tuple(finding.model_dump(mode="python") for finding in safe_findings),
    }
    return RiskDecision.model_validate(
        {**identity, "decision_hash": canonical_payload_hash(identity)}
    )


def _append_research_breach_finding(
    findings: list[RiskFinding],
    code: ResearchRiskCode,
    evidence: ResearchEvidence,
    limits: ResearchRiskLimits,
) -> None:
    if code == "MIN_TRADES_NOT_MET":
        _append_finding(
            findings,
            rule_id="research.minimum_trades",
            code=code,
            actual=_render_number(evidence.trades),
            limit=_render_number(limits.minimum_trades),
            unit="trades",
            message_key="risk.research.minimum_trades_not_met",
        )
    elif code == "MAX_TRADES_PER_YEAR_EXCEEDED":
        limit = limits.maximum_trades_per_year
        if limit is None:  # pragma: no cover - breach source guarantees a limit
            raise RuntimeError("Maximum trades breach lost its configured limit.")
        _append_finding(
            findings,
            rule_id="research.maximum_trades_per_year",
            code=code,
            actual=_render_number(evidence.trades_per_year),
            limit=_render_number(limit),
            unit="trades_per_year",
            message_key="risk.research.maximum_trades_per_year_exceeded",
        )
    elif code == "MIN_EXPOSURE_NOT_MET":
        _append_finding(
            findings,
            rule_id="research.minimum_exposure",
            code=code,
            actual=_render_number(evidence.exposure_ratio),
            limit=_render_number(limits.minimum_exposure),
            unit="ratio",
            message_key="risk.research.minimum_exposure_not_met",
        )
    elif code == "MIN_ANNUALIZED_RETURN_NOT_MET":
        limit = limits.minimum_annualized_return
        if limit is None:  # pragma: no cover - breach source guarantees a limit
            raise RuntimeError("Annualized return breach lost its configured limit.")
        _append_finding(
            findings,
            rule_id="research.minimum_annualized_return",
            code=code,
            actual=_render_number(evidence.annualized_return),
            limit=_render_number(limit),
            unit="ratio",
            message_key="risk.research.minimum_annualized_return_not_met",
        )
    elif code == "MAX_DRAWDOWN_EXCEEDED":
        limit = limits.maximum_drawdown
        if limit is None:  # pragma: no cover - breach source guarantees a limit
            raise RuntimeError("Drawdown breach lost its configured limit.")
        _append_finding(
            findings,
            rule_id="research.maximum_drawdown",
            code=code,
            actual=_render_number(abs(evidence.max_drawdown)),
            limit=_render_number(limit),
            unit="ratio",
            message_key="risk.research.maximum_drawdown_exceeded",
        )
    elif code == "MAX_CASH_STREAK_RATIO_EXCEEDED":
        _append_finding(
            findings,
            rule_id="research.maximum_cash_streak_ratio",
            code=code,
            actual=_render_number(evidence.max_cash_streak_ratio),
            limit=_render_number(limits.maximum_cash_streak_ratio),
            unit="ratio",
            message_key="risk.research.maximum_cash_streak_ratio_exceeded",
        )
    else:
        limit = limits.maximum_cash_streak_bars
        if limit is None:  # pragma: no cover - breach source guarantees a limit
            raise RuntimeError("Cash-streak breach lost its configured limit.")
        _append_finding(
            findings,
            rule_id="research.maximum_cash_streak_bars",
            code=code,
            actual=_render_number(evidence.max_cash_streak_bars),
            limit=_render_number(limit),
            unit="bars",
            message_key="risk.research.maximum_cash_streak_bars_exceeded",
        )


def evaluate_research_risk(request: ResearchRiskRequest) -> RiskDecision:
    """Evaluate legacy research participation gates without mutating metrics."""

    safe_request = _validated_copy(request, ResearchRiskRequest)
    limits = safe_request.rule_set.research_limits
    if limits is None:  # pragma: no cover - guaranteed by the request contract
        raise ValueError("Research request lost its research risk limits.")
    evidence = safe_request.evidence
    findings: list[RiskFinding] = []

    breach_codes = research_risk_breach_codes(
        trades=evidence.trades,
        trades_per_year=evidence.trades_per_year,
        exposure_ratio=evidence.exposure_ratio,
        annualized_return=evidence.annualized_return,
        max_drawdown=evidence.max_drawdown,
        max_cash_streak_ratio=evidence.max_cash_streak_ratio,
        max_cash_streak_bars=evidence.max_cash_streak_bars,
        minimum_trades=limits.minimum_trades,
        maximum_trades_per_year=limits.maximum_trades_per_year,
        minimum_exposure=limits.minimum_exposure,
        minimum_annualized_return=limits.minimum_annualized_return,
        maximum_drawdown=limits.maximum_drawdown,
        maximum_cash_streak_ratio=limits.maximum_cash_streak_ratio,
        maximum_cash_streak_bars=limits.maximum_cash_streak_bars,
    )
    for code in breach_codes:
        _append_research_breach_finding(findings, code, evidence, limits)

    # A kill switch fences risk-increasing execution, not deterministic research.
    return _build_decision(
        safe_request,
        findings=findings,
        effective_weights=None,
    )


def evaluate_allocation_risk(request: AllocationRiskRequest) -> RiskDecision:
    """Validate a target allocation without changing or silently clipping it."""

    safe_request = _validated_copy(request, AllocationRiskRequest)
    limits = safe_request.rule_set.allocation_limits
    if limits is None:  # pragma: no cover - guaranteed by the request contract
        raise ValueError("Allocation request lost its allocation risk limits.")
    intent = safe_request.intent
    findings: list[RiskFinding] = []
    _append_execution_kill_switch(findings, safe_request.kill_switch)

    symbol_counts = Counter(intent.symbols)
    expected_symbols = set(intent.symbols)
    supplied_counts = Counter(weight.symbol for weight in intent.requested_weights)
    supplied_symbols = set(supplied_counts)
    asset_count = len(intent.symbols)

    if not limits.minimum_assets <= asset_count <= limits.maximum_assets:
        _append_finding(
            findings,
            rule_id="allocation.asset_count",
            code="ASSET_COUNT_OUT_OF_RANGE",
            actual=_render_number(asset_count),
            limit=f"{limits.minimum_assets}..{limits.maximum_assets}",
            unit="assets",
            message_key="risk.allocation.asset_count_out_of_range",
        )
    for symbol in sorted(symbol for symbol, count in symbol_counts.items() if count > 1):
        _append_finding(
            findings,
            rule_id="allocation.symbols_unique",
            code="DUPLICATE_ALLOCATION_SYMBOL",
            subject=symbol,
            actual=_render_number(symbol_counts[symbol]),
            limit="1",
            unit="occurrences",
            message_key="risk.allocation.duplicate_symbol",
        )
    for symbol in sorted(symbol for symbol, count in supplied_counts.items() if count > 1):
        _append_finding(
            findings,
            rule_id="allocation.weight_symbols_unique",
            code="DUPLICATE_WEIGHT_SYMBOL",
            subject=symbol,
            actual=_render_number(supplied_counts[symbol]),
            limit="1",
            unit="occurrences",
            message_key="risk.allocation.duplicate_weight_symbol",
        )
    for symbol in sorted(expected_symbols - supplied_symbols):
        _append_finding(
            findings,
            rule_id="allocation.weight_symbol_set",
            code="WEIGHT_SYMBOL_MISSING",
            subject=symbol,
            actual="missing",
            limit="present",
            message_key="risk.allocation.weight_symbol_missing",
        )
    for symbol in sorted(supplied_symbols - expected_symbols):
        _append_finding(
            findings,
            rule_id="allocation.weight_symbol_set",
            code="WEIGHT_SYMBOL_UNEXPECTED",
            subject=symbol,
            actual="present",
            limit="absent",
            message_key="risk.allocation.weight_symbol_unexpected",
        )

    grouped_weights: dict[str, list[float]] = {}
    for weight in intent.requested_weights:
        grouped_weights.setdefault(weight.symbol, []).append(weight.weight)
    if limits.long_only:
        for symbol in sorted(grouped_weights):
            minimum_weight = min(grouped_weights[symbol])
            if minimum_weight < 0:
                _append_finding(
                    findings,
                    rule_id="allocation.long_only",
                    code="LONG_ONLY_VIOLATION",
                    subject=symbol,
                    actual=_render_number(minimum_weight),
                    limit="0",
                    unit="weight",
                    message_key="risk.allocation.long_only_violation",
                )

    weight_values = tuple(weight.weight for weight in intent.requested_weights)
    net_exposure = sum(weight_values)
    gross_exposure = sum(abs(weight) for weight in weight_values)
    if abs(net_exposure - limits.required_weight_sum) > limits.weight_sum_tolerance:
        _append_finding(
            findings,
            rule_id="allocation.required_weight_sum",
            code="WEIGHT_SUM_MISMATCH",
            actual=_render_number(net_exposure),
            limit=_render_number(limits.required_weight_sum),
            unit="weight",
            message_key="risk.allocation.weight_sum_mismatch",
        )
    gross_limit = min(
        limits.maximum_gross_exposure,
        1.0 if not limits.leverage_allowed else limits.maximum_gross_exposure,
    )
    if gross_exposure > gross_limit + limits.weight_sum_tolerance:
        _append_finding(
            findings,
            rule_id="allocation.maximum_gross_exposure",
            code="GROSS_EXPOSURE_EXCEEDED",
            actual=_render_number(gross_exposure),
            limit=_render_number(gross_limit),
            unit="ratio",
            message_key="risk.allocation.gross_exposure_exceeded",
        )
    if abs(net_exposure) > limits.maximum_net_exposure + limits.weight_sum_tolerance:
        _append_finding(
            findings,
            rule_id="allocation.maximum_net_exposure",
            code="NET_EXPOSURE_EXCEEDED",
            actual=_render_number(abs(net_exposure)),
            limit=_render_number(limits.maximum_net_exposure),
            unit="ratio",
            message_key="risk.allocation.net_exposure_exceeded",
        )

    if limits.maximum_asset_weight is not None:
        distinct_assets = len(expected_symbols)
        if (
            distinct_assets == 0
            or limits.maximum_asset_weight * distinct_assets
            < limits.required_weight_sum - limits.weight_sum_tolerance
        ):
            _append_finding(
                findings,
                rule_id="allocation.maximum_asset_weight",
                code="ASSET_WEIGHT_CAP_INFEASIBLE",
                actual=_render_number(limits.maximum_asset_weight),
                limit=(
                    "undefined"
                    if distinct_assets == 0
                    else _render_number(limits.required_weight_sum / distinct_assets)
                ),
                unit="weight",
                message_key="risk.allocation.asset_weight_cap_infeasible",
            )
        for symbol in sorted(grouped_weights):
            maximum_weight = max(grouped_weights[symbol])
            if maximum_weight > limits.maximum_asset_weight + limits.weight_sum_tolerance:
                _append_finding(
                    findings,
                    rule_id="allocation.maximum_asset_weight",
                    code="ASSET_WEIGHT_EXCEEDED",
                    subject=symbol,
                    actual=_render_number(maximum_weight),
                    limit=_render_number(limits.maximum_asset_weight),
                    unit="weight",
                    message_key="risk.allocation.asset_weight_exceeded",
                )

    if limits.maximum_turnover_ratio is not None:
        if intent.proposed_turnover_ratio is None:
            _append_finding(
                findings,
                rule_id="allocation.maximum_turnover_ratio",
                code="TURNOVER_EVIDENCE_MISSING",
                actual="missing",
                limit=_render_number(limits.maximum_turnover_ratio),
                unit="ratio",
                message_key="risk.allocation.turnover_evidence_missing",
            )
        elif intent.proposed_turnover_ratio < 0:
            _append_finding(
                findings,
                rule_id="allocation.maximum_turnover_ratio",
                code="TURNOVER_RATIO_INVALID",
                actual=_render_number(intent.proposed_turnover_ratio),
                limit="0..infinity",
                unit="ratio",
                message_key="risk.allocation.turnover_ratio_invalid",
            )
        elif intent.proposed_turnover_ratio > limits.maximum_turnover_ratio:
            _append_finding(
                findings,
                rule_id="allocation.maximum_turnover_ratio",
                code="MAX_TURNOVER_EXCEEDED",
                actual=_render_number(intent.proposed_turnover_ratio),
                limit=_render_number(limits.maximum_turnover_ratio),
                unit="ratio",
                message_key="risk.allocation.maximum_turnover_exceeded",
            )

    if limits.minimum_cash_reserve_ratio is not None:
        if intent.post_trade_cash_ratio is None:
            _append_finding(
                findings,
                rule_id="allocation.minimum_cash_reserve_ratio",
                code="CASH_RESERVE_EVIDENCE_MISSING",
                actual="missing",
                limit=_render_number(limits.minimum_cash_reserve_ratio),
                unit="ratio",
                message_key="risk.allocation.cash_reserve_evidence_missing",
            )
        elif not 0 <= intent.post_trade_cash_ratio <= 1:
            _append_finding(
                findings,
                rule_id="allocation.minimum_cash_reserve_ratio",
                code="POST_TRADE_CASH_RATIO_INVALID",
                actual=_render_number(intent.post_trade_cash_ratio),
                limit="0..1",
                unit="ratio",
                message_key="risk.allocation.post_trade_cash_ratio_invalid",
            )
        elif intent.post_trade_cash_ratio < limits.minimum_cash_reserve_ratio:
            _append_finding(
                findings,
                rule_id="allocation.minimum_cash_reserve_ratio",
                code="CASH_FLOOR_BREACH",
                actual=_render_number(intent.post_trade_cash_ratio),
                limit=_render_number(limits.minimum_cash_reserve_ratio),
                unit="ratio",
                message_key="risk.allocation.cash_floor_breach",
            )

    if limits.maximum_drawdown is not None:
        if intent.current_drawdown is None:
            _append_finding(
                findings,
                rule_id="allocation.maximum_drawdown",
                code="DRAWDOWN_EVIDENCE_MISSING",
                actual="missing",
                limit=_render_number(limits.maximum_drawdown),
                unit="ratio",
                message_key="risk.allocation.drawdown_evidence_missing",
            )
        elif not -1 <= intent.current_drawdown <= 0:
            _append_finding(
                findings,
                rule_id="allocation.maximum_drawdown",
                code="DRAWDOWN_STATE_INVALID",
                actual=_render_number(intent.current_drawdown),
                limit="-1..0",
                unit="ratio",
                message_key="risk.allocation.drawdown_state_invalid",
            )
        elif abs(intent.current_drawdown) > limits.maximum_drawdown:
            _append_finding(
                findings,
                rule_id="allocation.maximum_drawdown",
                code="DRAWDOWN_HALT",
                severity="critical",
                actual=_render_number(abs(intent.current_drawdown)),
                limit=_render_number(limits.maximum_drawdown),
                unit="ratio",
                message_key="risk.allocation.drawdown_halt",
            )

    return _build_decision(
        safe_request,
        findings=findings,
        effective_weights=(None if findings else safe_request.intent.requested_weights),
    )


def _timedelta_decimal_seconds(value: timedelta) -> Decimal:
    microseconds = value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
    return _terminating_decimal(Fraction(microseconds, 1_000_000))


def evaluate_order_risk(request: OrderRiskRequest) -> RiskDecision:
    """Evaluate one order intent without mutation, clipping, I/O, or hidden state."""

    safe_request = _validated_copy(request, OrderRiskRequest)
    limits = safe_request.rule_set.order_limits
    if limits is None:  # pragma: no cover - guaranteed by the request contract
        raise ValueError("Order request lost its order risk limits.")
    intent = safe_request.intent
    state = safe_request.state
    price = safe_request.price_evidence
    findings: list[RiskFinding] = []

    _append_execution_kill_switch(findings, safe_request.kill_switch)

    if safe_request.kill_switch.scope == "account":
        if safe_request.kill_switch.scope_id_hash != state.account_id_hash:
            _append_finding(
                findings,
                rule_id="order.kill_switch_scope",
                code="KILL_SWITCH_SCOPE_MISMATCH",
                subject="account",
                actual=safe_request.kill_switch.scope_id_hash or "missing",
                limit=state.account_id_hash,
                message_key="risk.order.kill_switch_scope_mismatch",
            )
    elif safe_request.kill_switch.scope != "global":
        _append_finding(
            findings,
            rule_id="order.kill_switch_scope",
            code="KILL_SWITCH_SCOPE_UNVERIFIABLE",
            subject=safe_request.kill_switch.scope,
            actual="unbound",
            limit="global_or_matching_account",
            message_key="risk.order.kill_switch_scope_unverifiable",
        )

    if safe_request.kill_switch_available_at < safe_request.kill_switch_observed_at:
        _append_finding(
            findings,
            rule_id="order.kill_switch_timeline",
            code="KILL_SWITCH_TIMELINE_INVALID",
            actual=safe_request.kill_switch_available_at.isoformat(),
            limit=f">={safe_request.kill_switch_observed_at.isoformat()}",
            unit="timestamp",
            message_key="risk.order.kill_switch_timeline_invalid",
        )
    if safe_request.kill_switch_observed_at > safe_request.evaluated_at:
        _append_finding(
            findings,
            rule_id="order.kill_switch_observed_at",
            code="KILL_SWITCH_OBSERVATION_IN_FUTURE",
            actual=safe_request.kill_switch_observed_at.isoformat(),
            limit=safe_request.evaluated_at.isoformat(),
            unit="timestamp",
            message_key="risk.order.kill_switch_observation_in_future",
        )
    if safe_request.kill_switch_available_at > safe_request.evaluated_at:
        _append_finding(
            findings,
            rule_id="order.kill_switch_available_at",
            code="KILL_SWITCH_NOT_AVAILABLE",
            actual=safe_request.kill_switch_available_at.isoformat(),
            limit=safe_request.evaluated_at.isoformat(),
            unit="timestamp",
            message_key="risk.order.kill_switch_not_available",
        )
    if safe_request.kill_switch_observed_at <= safe_request.evaluated_at:
        switch_age = safe_request.evaluated_at - safe_request.kill_switch_observed_at
        maximum_switch_age = timedelta(seconds=limits.maximum_kill_switch_age_seconds)
        if switch_age > maximum_switch_age:
            _append_finding(
                findings,
                rule_id="order.maximum_kill_switch_age",
                code="KILL_SWITCH_STATE_STALE",
                actual=_render_number(_timedelta_decimal_seconds(switch_age)),
                limit=_render_number(limits.maximum_kill_switch_age_seconds),
                unit="seconds",
                message_key="risk.order.kill_switch_state_stale",
            )

    if state.symbol != intent.symbol:
        _append_finding(
            findings,
            rule_id="order.account_symbol",
            code="ACCOUNT_POSITION_SYMBOL_MISMATCH",
            subject=intent.symbol,
            actual=state.symbol,
            limit=intent.symbol,
            message_key="risk.order.account_position_symbol_mismatch",
        )
    if state.quote_currency != intent.quote_currency:
        _append_finding(
            findings,
            rule_id="order.account_quote_currency",
            code="ACCOUNT_QUOTE_CURRENCY_MISMATCH",
            subject=intent.symbol,
            actual=state.quote_currency,
            limit=intent.quote_currency,
            message_key="risk.order.account_quote_currency_mismatch",
        )

    price_usable = True
    if price.symbol != intent.symbol:
        price_usable = False
        _append_finding(
            findings,
            rule_id="order.price_symbol",
            code="PRICE_SYMBOL_MISMATCH",
            subject=intent.symbol,
            actual=price.symbol,
            limit=intent.symbol,
            message_key="risk.order.price_symbol_mismatch",
        )
    if price.quote_currency != intent.quote_currency:
        price_usable = False
        _append_finding(
            findings,
            rule_id="order.price_quote_currency",
            code="PRICE_QUOTE_CURRENCY_MISMATCH",
            subject=intent.symbol,
            actual=price.quote_currency,
            limit=intent.quote_currency,
            message_key="risk.order.price_quote_currency_mismatch",
        )
    if price.available_at < price.observed_at:
        price_usable = False
        _append_finding(
            findings,
            rule_id="order.price_timeline",
            code="PRICE_TIMELINE_INVALID",
            subject=intent.symbol,
            actual=price.available_at.isoformat(),
            limit=f">={price.observed_at.isoformat()}",
            unit="timestamp",
            message_key="risk.order.price_timeline_invalid",
        )
    if price.observed_at > safe_request.evaluated_at:
        price_usable = False
        _append_finding(
            findings,
            rule_id="order.price_observed_at",
            code="PRICE_OBSERVATION_IN_FUTURE",
            subject=intent.symbol,
            actual=price.observed_at.isoformat(),
            limit=safe_request.evaluated_at.isoformat(),
            unit="timestamp",
            message_key="risk.order.price_observation_in_future",
        )
    if price.available_at > safe_request.evaluated_at:
        price_usable = False
        _append_finding(
            findings,
            rule_id="order.price_available_at",
            code="PRICE_NOT_AVAILABLE",
            subject=intent.symbol,
            actual=price.available_at.isoformat(),
            limit=safe_request.evaluated_at.isoformat(),
            unit="timestamp",
            message_key="risk.order.price_not_available",
        )
    if price.observed_at <= safe_request.evaluated_at:
        price_age = safe_request.evaluated_at - price.observed_at
        maximum_price_age = timedelta(seconds=limits.maximum_price_age_seconds)
        if price_age > maximum_price_age:
            price_usable = False
            _append_finding(
                findings,
                rule_id="order.maximum_price_age",
                code="REFERENCE_PRICE_STALE",
                subject=intent.symbol,
                actual=_render_number(_timedelta_decimal_seconds(price_age)),
                limit=_render_number(limits.maximum_price_age_seconds),
                unit="seconds",
                message_key="risk.order.reference_price_stale",
            )

    resulting_active_orders = state.active_order_count + 1
    if resulting_active_orders > limits.maximum_active_orders:
        _append_finding(
            findings,
            rule_id="order.maximum_active_orders",
            code="MAXIMUM_ACTIVE_ORDERS_EXCEEDED",
            actual=_render_number(resulting_active_orders),
            limit=_render_number(limits.maximum_active_orders),
            unit="orders",
            message_key="risk.order.maximum_active_orders_exceeded",
        )

    available_cash = _decimal_difference(state.cash_balance, state.reserved_buy_cash)
    if state.cash_balance < 0:
        _append_finding(
            findings,
            rule_id="order.nonnegative_cash_state",
            code="ACCOUNT_CASH_NEGATIVE",
            actual=_render_number(state.cash_balance),
            limit="0",
            unit="quote_currency",
            message_key="risk.order.account_cash_negative",
        )
    if available_cash < 0:
        _append_finding(
            findings,
            rule_id="order.existing_buy_cash_reservation",
            code="EXISTING_BUY_RESERVATION_EXCEEDS_CASH",
            actual=_render_number(state.reserved_buy_cash),
            limit=_render_number(state.cash_balance),
            unit="quote_currency",
            message_key="risk.order.existing_buy_reservation_exceeds_cash",
        )

    if limits.long_only and state.position_quantity < 0:
        _append_finding(
            findings,
            rule_id="order.long_only",
            code="LONG_ONLY_STATE_VIOLATION",
            subject=intent.symbol,
            actual=_render_number(state.position_quantity),
            limit="0",
            unit="base_quantity",
            message_key="risk.order.long_only_state_violation",
        )
    available_position = _decimal_difference(
        state.position_quantity,
        state.reserved_sell_quantity,
    )
    if state.reserved_sell_quantity > state.position_quantity:
        _append_finding(
            findings,
            rule_id="order.existing_sell_position_reservation",
            code="EXISTING_SELL_RESERVATION_EXCEEDS_POSITION",
            subject=intent.symbol,
            actual=_render_number(state.reserved_sell_quantity),
            limit=_render_number(state.position_quantity),
            unit="base_quantity",
            message_key="risk.order.existing_sell_reservation_exceeds_position",
        )

    if intent.side == "buy":
        maximum_resulting_position = _decimal_sum(
            state.position_quantity,
            state.reserved_buy_quantity,
            intent.quantity,
        )
        if maximum_resulting_position > limits.maximum_resulting_position:
            _append_finding(
                findings,
                rule_id="order.maximum_resulting_position",
                code="MAXIMUM_RESULTING_POSITION_EXCEEDED",
                subject=intent.symbol,
                actual=_render_number(maximum_resulting_position),
                limit=_render_number(limits.maximum_resulting_position),
                unit="base_quantity",
                message_key="risk.order.maximum_resulting_position_exceeded",
            )
    elif intent.quantity > available_position:
        _append_finding(
            findings,
            rule_id="order.available_position",
            code="INSUFFICIENT_AVAILABLE_POSITION",
            subject=intent.symbol,
            actual=_render_number(available_position),
            limit=_render_number(intent.quantity),
            unit="base_quantity",
            message_key="risk.order.insufficient_available_position",
        )

    if price_usable:
        if intent.order_type == "market":
            risk_price = _decimal_product(
                price.reference_price,
                _decimal_sum(Decimal(1), limits.market_order_price_buffer_ratio),
            )
        else:
            if intent.limit_price is None:  # pragma: no cover - guaranteed by intent
                raise ValueError("Limit order lost its limit price.")
            risk_price = (
                intent.limit_price
                if intent.side == "buy"
                else max(intent.limit_price, price.reference_price)
            )
        order_notional = _decimal_product(intent.quantity, risk_price)

        if order_notional > limits.maximum_order_notional:
            _append_finding(
                findings,
                rule_id="order.maximum_order_notional",
                code="MAXIMUM_ORDER_NOTIONAL_EXCEEDED",
                subject=intent.symbol,
                actual=_render_number(order_notional),
                limit=_render_number(limits.maximum_order_notional),
                unit="quote_currency",
                message_key="risk.order.maximum_order_notional_exceeded",
            )

        if intent.side == "buy":
            cash_requirement = _decimal_product(
                order_notional,
                _decimal_sum(Decimal(1), limits.order_fee_buffer_ratio),
            )
            cash_after_order = _decimal_difference(available_cash, cash_requirement)
            if cash_requirement > available_cash:
                _append_finding(
                    findings,
                    rule_id="order.available_cash",
                    code="INSUFFICIENT_AVAILABLE_CASH",
                    actual=_render_number(available_cash),
                    limit=_render_number(cash_requirement),
                    unit="quote_currency",
                    message_key="risk.order.insufficient_available_cash",
                )
            if cash_after_order < limits.minimum_cash_reserve:
                _append_finding(
                    findings,
                    rule_id="order.minimum_cash_reserve",
                    code="MINIMUM_CASH_RESERVE_BREACH",
                    actual=_render_number(cash_after_order),
                    limit=_render_number(limits.minimum_cash_reserve),
                    unit="quote_currency",
                    message_key="risk.order.minimum_cash_reserve_breach",
                )

    return _build_decision(
        safe_request,
        findings=findings,
        effective_weights=None,
    )
