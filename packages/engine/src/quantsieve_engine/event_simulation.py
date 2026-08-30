"""Deterministic offline bar simulation backed by order events and a paper ledger.

Version-two run receipts retain canonical bars and intents as immutable replay
evidence. ``verify_event_simulation`` rebuilds the complete run from those inputs
instead of treating a self-consistent record chain as proof of execution.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Annotated, Literal, Self, TypeAlias, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .order_events import (
    OrderAcknowledgedEvent,
    OrderCancelAcknowledgedEvent,
    OrderCancelRequestedEvent,
    OrderExpiredEvent,
    OrderFillEvent,
    OrderRejectedEvent,
    OrderState,
    apply_order_event,
    create_order_state,
)
from .paper_ledger import (
    OpeningBalanceEvent,
    PaperFillEvent,
    PaperLedgerState,
    apply_paper_fill,
    build_opening_balance_event,
    build_paper_fill_event,
    open_paper_ledger,
)
from .risk import (
    KillSwitchSnapshot,
    OrderPriceEvidence,
    OrderRiskIntent,
    OrderRiskRequest,
    OrderRiskRuleSet,
    OrderRiskState,
    RiskDecision,
    build_order_risk_request,
    evaluate_order_risk,
)

_HASH_PATTERN = r"^[0-9a-f]{64}$"
_ZERO_HASH = "0" * 64
_RISK_PRICE_SOURCE = "event-simulation.finalized-bar-close"
_RISK_CALCULATION_VERSION = "event-simulation.order-risk.v1"
_STRICT_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    strict=True,
    revalidate_instances="always",
    allow_inf_nan=False,
)


class EventSimulationError(RuntimeError):
    """Base error for deterministic event-simulation failures."""


class SimulationInputError(EventSimulationError):
    """Raised when bars or intents cannot define one deterministic simulation."""


class SimulationIntegrityError(EventSimulationError):
    """Raised when a record chain or replay projection has been altered."""


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError(f"{label} must contain between 1 and 200 characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{label} must not contain control characters.")
    return normalized


def _symbol(value: object) -> str:
    return _identifier(value, "Symbol").upper()


def _currency(value: object) -> str:
    normalized = _identifier(value, "Quote currency").upper()
    if len(normalized) > 16 or not normalized.replace("_", "").isalnum():
        raise ValueError("Quote currency is invalid.")
    return normalized


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError(f"{label} must be an exact finite decimal.")
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
    if denominator != 1:
        raise ValueError("Simulation arithmetic produced a non-terminating decimal.")
    scale = max(twos, fives)
    scaled = numerator * (5 ** (scale - fives)) * (2 ** (scale - twos))
    sign = int(scaled < 0)
    digits = tuple(int(digit) for digit in str(abs(scaled)))
    return _decimal(Decimal((sign, digits, -scale)), "Simulation arithmetic result")


def _sum(*values: Decimal) -> Decimal:
    return _terminating_decimal(sum((Fraction(value) for value in values), Fraction()))


def _difference(left: Decimal, right: Decimal) -> Decimal:
    return _terminating_decimal(Fraction(left) - Fraction(right))


def _product(*values: Decimal) -> Decimal:
    result = Fraction(1)
    for value in values:
        result *= Fraction(value)
    return _terminating_decimal(result)


def _utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a timezone-aware datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware.")
    return value.astimezone(UTC)


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return _json_ready(value.model_dump(mode="python"))
    if isinstance(value, Decimal):
        return {"$decimal": format(_decimal(value, "Canonical decimal"), "f")}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _json_ready(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _hash(value: object) -> str:
    payload = json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _derived_id(kind: str, *identity: object) -> str:
    """Bound internal identities even when a caller uses a maximum-length id."""

    return f"simulation-{kind}-{_hash({'kind': kind, 'identity': identity})}"


class SimulationConfig(BaseModel):
    """Execution assumptions for one offline, long-only paper simulation."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[1] = 1
    account_id: str
    symbol: str
    quote_currency: str
    initial_cash: Decimal
    opened_at: datetime
    order_risk_rule_set: OrderRiskRuleSet
    kill_switch: KillSwitchSnapshot
    kill_switch_observed_at: datetime
    kill_switch_available_at: datetime
    latency_bars: int = Field(default=0, ge=0, strict=True)
    volume_participation_rate: Decimal = Decimal("1")
    fee_rate: Decimal = Decimal(0)
    slippage_bps: Decimal = Decimal(0)

    @field_validator("account_id", mode="before")
    @classmethod
    def normalize_account_id(cls, value: object) -> str:
        return _identifier(value, "Account id")

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _symbol(value)

    @field_validator("quote_currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> str:
        return _currency(value)

    @field_validator(
        "initial_cash",
        "volume_participation_rate",
        "fee_rate",
        "slippage_bps",
        mode="before",
    )
    @classmethod
    def normalize_decimal(cls, value: object) -> Decimal:
        return _decimal(value, "Simulation configuration decimal")

    @field_validator("opened_at", mode="before")
    @classmethod
    def normalize_opened_at(cls, value: object) -> datetime:
        return _utc(value, "Account opening time")

    @field_validator(
        "kill_switch_observed_at",
        "kill_switch_available_at",
        mode="before",
    )
    @classmethod
    def normalize_kill_switch_time(cls, value: object) -> datetime:
        return _utc(value, "Kill-switch evidence time")

    @field_validator("order_risk_rule_set", mode="before")
    @classmethod
    def revalidate_order_rules(cls, value: object) -> OrderRiskRuleSet:
        payload = value.model_dump(mode="python") if isinstance(value, OrderRiskRuleSet) else value
        return OrderRiskRuleSet.model_validate(payload)

    @field_validator("kill_switch", mode="before")
    @classmethod
    def revalidate_kill_switch(cls, value: object) -> KillSwitchSnapshot:
        payload = (
            value.model_dump(mode="python") if isinstance(value, KillSwitchSnapshot) else value
        )
        return KillSwitchSnapshot.model_validate(payload)

    @model_validator(mode="after")
    def validate_config(self) -> Self:
        if self.initial_cash <= 0:
            raise ValueError("Initial cash must be positive.")
        if not Decimal(0) <= self.volume_participation_rate <= Decimal(1):
            raise ValueError("Volume participation rate must be between zero and one.")
        if not Decimal(0) <= self.fee_rate <= Decimal("0.1"):
            raise ValueError("Fee rate must be between zero and ten percent.")
        if not Decimal(0) <= self.slippage_bps < Decimal("10000"):
            raise ValueError("Slippage must be at least zero and below 10,000 basis points.")
        if self.order_risk_rule_set.evaluation_kind != "order_intent":
            raise ValueError("Simulation requires an order-intent risk rule set.")
        if self.fee_rate > self.order_risk_rule_set.order_limits.order_fee_buffer_ratio:
            raise ValueError("Simulation fee rate cannot exceed the order-risk fee buffer.")
        if self.kill_switch_available_at < self.kill_switch_observed_at:
            raise ValueError("Kill-switch availability cannot precede its observation.")
        return self


class SimulationBar(BaseModel):
    """One finalized OHLCV bar; intents are observed only after ``closed_at``."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[1] = 1
    index: int = Field(ge=0, strict=True)
    symbol: str
    opened_at: datetime
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    bar_hash: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")

    @model_validator(mode="before")
    @classmethod
    def normalize_and_hash(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        data.setdefault("schema_version", 1)
        if "symbol" in data:
            data["symbol"] = _symbol(data["symbol"])
        for field_name in ("open", "high", "low", "close", "volume"):
            if field_name in data:
                data[field_name] = _decimal(data[field_name], f"Bar {field_name}")
        for field_name, label in (
            ("opened_at", "Bar opening time"),
            ("closed_at", "Bar closing time"),
        ):
            if field_name in data:
                data[field_name] = _utc(data[field_name], label)
        supplied = data.get("bar_hash")
        economic = {key: item for key, item in data.items() if key != "bar_hash"}
        expected = _hash(economic)
        if supplied in (None, ""):
            data["bar_hash"] = expected
        elif supplied != expected:
            raise ValueError("Bar hash does not match its payload.")
        return data

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _symbol(value)

    @field_validator("opened_at", "closed_at", mode="before")
    @classmethod
    def normalize_time(cls, value: object) -> datetime:
        return _utc(value, "Bar time")

    @field_validator("open", "high", "low", "close", "volume", mode="before")
    @classmethod
    def normalize_price_or_volume(cls, value: object) -> Decimal:
        return _decimal(value, "Bar decimal")

    @model_validator(mode="after")
    def validate_bar(self) -> Self:
        if self.closed_at <= self.opened_at:
            raise ValueError("Bar closing time must follow its opening time.")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive.")
        if self.volume < 0:
            raise ValueError("Bar volume cannot be negative.")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("Bar high/low do not contain open and close.")
        if self.low > self.high:
            raise ValueError("Bar low cannot exceed bar high.")
        return self


class SubmitOrderIntent(BaseModel):
    """A close-time order signal, never eligible on its own signal bar."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[1] = 1
    intent_type: Literal["submit"] = "submit"
    sequence: int = Field(ge=1, strict=True)
    idempotency_key: str
    order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    quantity: Decimal
    limit_price: Decimal | None = None
    created_at: datetime
    expires_after_bars: int | None = Field(default=None, ge=1, strict=True)
    intent_hash: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")

    @model_validator(mode="before")
    @classmethod
    def normalize_and_hash(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        data.setdefault("schema_version", 1)
        data.setdefault("intent_type", "submit")
        data.setdefault("limit_price", None)
        data.setdefault("expires_after_bars", None)
        for field_name in ("idempotency_key", "order_id"):
            if field_name in data:
                data[field_name] = _identifier(
                    data[field_name], field_name.replace("_", " ").title()
                )
        if "symbol" in data:
            data["symbol"] = _symbol(data["symbol"])
        for field_name in ("quantity", "limit_price"):
            if data.get(field_name) is not None:
                data[field_name] = _decimal(data[field_name], field_name.replace("_", " ").title())
        if "created_at" in data:
            data["created_at"] = _utc(data["created_at"], "Intent creation time")
        supplied = data.get("intent_hash")
        economic = {key: item for key, item in data.items() if key != "intent_hash"}
        expected = _hash(economic)
        if supplied in (None, ""):
            data["intent_hash"] = expected
        elif supplied != expected:
            raise ValueError("Submit intent hash does not match its payload.")
        return data

    @field_validator("idempotency_key", "order_id", mode="before")
    @classmethod
    def normalize_identifier(cls, value: object) -> str:
        return _identifier(value, "Submit intent identity")

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _symbol(value)

    @field_validator("quantity", "limit_price", mode="before")
    @classmethod
    def normalize_decimal(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        return _decimal(value, "Submit intent decimal")

    @field_validator("created_at", mode="before")
    @classmethod
    def normalize_created_at(cls, value: object) -> datetime:
        return _utc(value, "Intent creation time")

    @model_validator(mode="after")
    def validate_intent(self) -> Self:
        if self.quantity <= 0:
            raise ValueError("Order quantity must be positive.")
        if self.order_type == "limit":
            if self.limit_price is None or self.limit_price <= 0:
                raise ValueError("A limit order requires a positive limit price.")
        elif self.limit_price is not None:
            raise ValueError("A market order cannot include a limit price.")
        return self


class CancelOrderIntent(BaseModel):
    """A close-time request to cancel one known non-terminal simulated order."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[1] = 1
    intent_type: Literal["cancel"] = "cancel"
    sequence: int = Field(ge=1, strict=True)
    idempotency_key: str
    order_id: str
    created_at: datetime
    intent_hash: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")

    @model_validator(mode="before")
    @classmethod
    def normalize_and_hash(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        data.setdefault("schema_version", 1)
        data.setdefault("intent_type", "cancel")
        for field_name in ("idempotency_key", "order_id"):
            if field_name in data:
                data[field_name] = _identifier(
                    data[field_name], field_name.replace("_", " ").title()
                )
        if "created_at" in data:
            data["created_at"] = _utc(data["created_at"], "Intent creation time")
        supplied = data.get("intent_hash")
        economic = {key: item for key, item in data.items() if key != "intent_hash"}
        expected = _hash(economic)
        if supplied in (None, ""):
            data["intent_hash"] = expected
        elif supplied != expected:
            raise ValueError("Cancel intent hash does not match its payload.")
        return data

    @field_validator("idempotency_key", "order_id", mode="before")
    @classmethod
    def normalize_identifier(cls, value: object) -> str:
        return _identifier(value, "Cancel intent identity")

    @field_validator("created_at", mode="before")
    @classmethod
    def normalize_created_at(cls, value: object) -> datetime:
        return _utc(value, "Intent creation time")


SimulationIntent: TypeAlias = Annotated[
    SubmitOrderIntent | CancelOrderIntent,
    Field(discriminator="intent_type"),
]
_INTENT_ADAPTER: TypeAdapter[SimulationIntent] = TypeAdapter(SimulationIntent)


class _RecordBase(BaseModel):
    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[1] = 1
    sequence: int = Field(ge=1, strict=True)
    record_type: str
    occurred_at: datetime
    bar_index: int | None = Field(default=None, ge=0, strict=True)
    previous_hash: str = Field(pattern=_HASH_PATTERN)
    record_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator("occurred_at", mode="before")
    @classmethod
    def normalize_occurred_at(cls, value: object) -> datetime:
        return _utc(value, "Simulation record time")

    @model_validator(mode="after")
    def validate_record_hash(self) -> Self:
        expected = _hash(self.model_dump(mode="python", exclude={"record_hash"}))
        if self.record_hash != expected:
            raise ValueError("Simulation record hash does not match its payload.")
        return self


class AccountOpenedRecord(_RecordBase):
    record_type: Literal["account_opened"] = "account_opened"
    opening_event: OpeningBalanceEvent


class OrderSubmittedRecord(_RecordBase):
    record_type: Literal["order_submitted"] = "order_submitted"
    intent: SubmitOrderIntent
    eligible_bar_index: int = Field(ge=0, strict=True)
    risk_decision: RiskDecision

    @model_validator(mode="after")
    def validate_risk_allow(self) -> Self:
        if self.risk_decision.decision != "allow":
            raise ValueError("Submitted orders require a complete allow risk decision.")
        request = self.risk_decision.request
        if not isinstance(request, OrderRiskRequest):
            raise ValueError("Submitted orders require an order-intent risk request.")
        if (
            request.intent.order_id != self.intent.order_id
            or request.intent.symbol != self.intent.symbol
            or request.intent.side != self.intent.side
            or request.intent.order_type != self.intent.order_type
            or request.intent.quantity != self.intent.quantity
            or request.intent.limit_price != self.intent.limit_price
        ):
            raise ValueError("Submitted intent differs from its risk decision.")
        return self


class OrderRiskRejectedRecord(_RecordBase):
    record_type: Literal["order_risk_rejected"] = "order_risk_rejected"
    intent: SubmitOrderIntent
    risk_decision: RiskDecision

    @model_validator(mode="after")
    def validate_risk_rejection(self) -> Self:
        if self.risk_decision.decision != "reject":
            raise ValueError("Risk-rejection records require a reject decision.")
        request = self.risk_decision.request
        if not isinstance(request, OrderRiskRequest):
            raise ValueError("Risk-rejection records require an order-intent request.")
        if (
            request.intent.order_id != self.intent.order_id
            or request.intent.symbol != self.intent.symbol
            or request.intent.side != self.intent.side
            or request.intent.order_type != self.intent.order_type
            or request.intent.quantity != self.intent.quantity
            or request.intent.limit_price != self.intent.limit_price
        ):
            raise ValueError("Rejected intent differs from its risk decision.")
        return self


NonFillOrderEvent: TypeAlias = Annotated[
    OrderAcknowledgedEvent | OrderRejectedEvent | OrderExpiredEvent,
    Field(discriminator="event_type"),
]


class OrderEventRecord(_RecordBase):
    record_type: Literal["order_event"] = "order_event"
    order_event: NonFillOrderEvent


class OrderCancellationRecord(_RecordBase):
    record_type: Literal["order_cancelled"] = "order_cancelled"
    intent: CancelOrderIntent
    request_event: OrderCancelRequestedEvent
    acknowledgement_event: OrderCancelAcknowledgedEvent


class AtomicFillRecord(_RecordBase):
    record_type: Literal["atomic_fill"] = "atomic_fill"
    order_event: OrderFillEvent
    ledger_event: PaperFillEvent

    @model_validator(mode="after")
    def validate_atomic_economics(self) -> Self:
        if (
            self.order_event.order_id != self.ledger_event.order_id
            or self.order_event.fill_id != self.ledger_event.fill_id
            or self.order_event.fill_quantity != self.ledger_event.quantity
            or self.order_event.fill_price != self.ledger_event.fill_price
        ):
            raise ValueError("Atomic order and ledger fill economics do not match.")
        if self.occurred_at != self.ledger_event.occurred_at:
            raise ValueError("Atomic fill time differs from its ledger event.")
        return self


SimulationRecord: TypeAlias = Annotated[
    AccountOpenedRecord
    | OrderSubmittedRecord
    | OrderRiskRejectedRecord
    | OrderEventRecord
    | OrderCancellationRecord
    | AtomicFillRecord,
    Field(discriminator="record_type"),
]
_RECORD_ADAPTER: TypeAdapter[SimulationRecord] = TypeAdapter(SimulationRecord)


class SimulatedOrderProjection(BaseModel):
    model_config = _STRICT_MODEL_CONFIG

    intent: SubmitOrderIntent
    eligible_bar_index: int = Field(ge=0, strict=True)
    risk_decision: RiskDecision
    state: OrderState

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        if (
            self.intent.order_id != self.state.order_id
            or self.intent.quantity != self.state.requested_quantity
        ):
            raise ValueError("Order projection differs from its submitted intent.")
        if self.risk_decision.decision != "allow":
            raise ValueError("Executable order projections require an allow risk decision.")
        request = self.risk_decision.request
        if not isinstance(request, OrderRiskRequest):
            raise ValueError("Executable order projections require order-intent risk.")
        if (
            request.intent.order_id != self.intent.order_id
            or request.intent.symbol != self.intent.symbol
            or request.intent.side != self.intent.side
            or request.intent.order_type != self.intent.order_type
            or request.intent.quantity != self.intent.quantity
            or request.intent.limit_price != self.intent.limit_price
        ):
            raise ValueError("Order projection intent differs from its risk decision.")
        return self


class SimulationReplay(BaseModel):
    model_config = _STRICT_MODEL_CONFIG

    orders: tuple[SimulatedOrderProjection, ...]
    ledger_state: PaperLedgerState
    final_record_hash: str = Field(pattern=_HASH_PATTERN)
    projection_hash: str = Field(pattern=_HASH_PATTERN)


class EventSimulationRun(BaseModel):
    """Self-contained receipt with canonical inputs and a hash-chained replay log."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[2] = 2
    config: SimulationConfig
    bars: tuple[SimulationBar, ...] = Field(min_length=1)
    intents: tuple[SimulationIntent, ...]
    bars_hash: str = Field(pattern=_HASH_PATTERN)
    intents_hash: str = Field(pattern=_HASH_PATTERN)
    records: tuple[SimulationRecord, ...] = Field(min_length=1)
    orders: tuple[SimulatedOrderProjection, ...]
    ledger_state: PaperLedgerState
    final_record_hash: str = Field(pattern=_HASH_PATTERN)
    replay_hash: str = Field(pattern=_HASH_PATTERN)

    @model_validator(mode="after")
    def validate_input_evidence_hashes(self) -> Self:
        if self.bars_hash != _hash(self.bars):
            raise ValueError("Canonical bars do not match bars_hash.")
        if self.intents_hash != _hash(self.intents):
            raise ValueError("Canonical intents do not match intents_hash.")
        return self


def _validated_model(value: object, model: type[BaseModel], label: str) -> BaseModel:
    if not isinstance(value, model):
        raise SimulationInputError(f"{label} must be a {model.__name__} instance.")
    try:
        return model.model_validate(value.model_dump(mode="python"))
    except Exception as error:
        raise SimulationInputError(f"{label} failed strict revalidation.") from error


RecordT = TypeVar("RecordT", bound=_RecordBase)


def _build_record(
    record_class: type[RecordT],
    *,
    sequence: int,
    previous_hash: str,
    occurred_at: datetime,
    bar_index: int | None,
    payload: Mapping[str, object],
) -> RecordT:
    data: dict[str, object] = {
        "schema_version": 1,
        "sequence": sequence,
        "occurred_at": occurred_at,
        "bar_index": bar_index,
        "previous_hash": previous_hash,
        **payload,
    }
    return record_class.model_validate({**data, "record_hash": _hash(data)})


def _projection_hash(
    orders: Sequence[SimulatedOrderProjection],
    ledger_state: PaperLedgerState,
) -> str:
    return _hash({"orders": tuple(orders), "ledger_state": ledger_state})


def _position_quantity(ledger: PaperLedgerState, symbol: str) -> Decimal:
    return next(
        (position.quantity for position in ledger.positions if position.symbol == symbol),
        Decimal(0),
    )


def _order_risk_intent(
    config: SimulationConfig,
    intent: SubmitOrderIntent,
) -> OrderRiskIntent:
    return OrderRiskIntent.model_validate(
        {
            "order_id": intent.order_id,
            "symbol": intent.symbol,
            "quote_currency": config.quote_currency,
            "side": intent.side,
            "order_type": intent.order_type,
            "quantity": intent.quantity,
            "limit_price": intent.limit_price,
        }
    )


def _decision_risk_price(decision: RiskDecision) -> Decimal:
    request = decision.request
    if not isinstance(request, OrderRiskRequest):
        raise SimulationIntegrityError("Executable order lost its order-intent risk request.")
    if request.intent.order_type == "limit":
        limit_price = request.intent.limit_price
        if limit_price is None:  # pragma: no cover - risk contract invariant
            raise SimulationIntegrityError("Limit risk intent lost its limit price.")
        if request.intent.side == "sell":
            return max(limit_price, request.price_evidence.reference_price)
        return limit_price
    return _product(
        request.price_evidence.reference_price,
        _sum(
            Decimal(1),
            request.rule_set.order_limits.market_order_price_buffer_ratio,
        ),
    )


def _decision_execution_price_boundary(decision: RiskDecision) -> Decimal:
    """Return the worst approved fill price in the order's economic direction."""

    request = decision.request
    if not isinstance(request, OrderRiskRequest):
        raise SimulationIntegrityError("Executable order lost its order-intent risk request.")
    if request.intent.order_type == "limit":
        limit_price = request.intent.limit_price
        if limit_price is None:  # pragma: no cover - risk contract invariant
            raise SimulationIntegrityError("Limit risk intent lost its limit price.")
        return limit_price
    buffer_ratio = request.rule_set.order_limits.market_order_price_buffer_ratio
    multiplier = (
        _sum(Decimal(1), buffer_ratio)
        if request.intent.side == "buy"
        else _difference(Decimal(1), buffer_ratio)
    )
    return _product(request.price_evidence.reference_price, multiplier)


def _decision_fee_buffer_ratio(decision: RiskDecision) -> Decimal:
    request = decision.request
    if not isinstance(request, OrderRiskRequest):
        raise SimulationIntegrityError("Executable order lost its order-intent risk request.")
    return request.rule_set.order_limits.order_fee_buffer_ratio


def _risk_reservation_state(
    projections: Mapping[str, SimulatedOrderProjection],
) -> tuple[
    Decimal,
    Decimal,
    Decimal,
    int,
    tuple[dict[str, object], ...],
]:
    reserved_buy_cash = Decimal(0)
    reserved_buy_quantity = Decimal(0)
    reserved_sell_quantity = Decimal(0)
    reservation_rows: list[dict[str, object]] = []
    active = tuple(
        sorted(
            (
                projection
                for projection in projections.values()
                if projection.state.status in {"pending_submit", "working", "partially_filled"}
            ),
            key=lambda item: (item.intent.sequence, item.intent.order_id),
        )
    )
    for projection in active:
        remaining = _difference(
            projection.state.requested_quantity,
            projection.state.filled_quantity,
        )
        risk_price = _decision_risk_price(projection.risk_decision)
        reservation_cash = Decimal(0)
        fee_buffer_ratio = _decision_fee_buffer_ratio(projection.risk_decision)
        if projection.intent.side == "buy":
            fee_multiplier = _sum(
                Decimal(1),
                fee_buffer_ratio,
            )
            reservation_cash = _product(
                remaining,
                risk_price,
                fee_multiplier,
            )
            reserved_buy_cash = _sum(reserved_buy_cash, reservation_cash)
            reserved_buy_quantity = _sum(reserved_buy_quantity, remaining)
        else:
            reserved_sell_quantity = _sum(reserved_sell_quantity, remaining)
        reservation_rows.append(
            {
                "order_id": projection.intent.order_id,
                "side": projection.intent.side,
                "remaining_quantity": remaining,
                "risk_price": risk_price,
                "order_fee_buffer_ratio": fee_buffer_ratio,
                "reserved_buy_cash": reservation_cash,
                "reserved_sell_quantity": (
                    remaining if projection.intent.side == "sell" else Decimal(0)
                ),
            }
        )
    return (
        reserved_buy_cash,
        reserved_buy_quantity,
        reserved_sell_quantity,
        len(active),
        tuple(reservation_rows),
    )


def _ledger_economic_state(ledger: PaperLedgerState) -> dict[str, object]:
    return {
        "account_id": ledger.account_id,
        "currency": ledger.currency,
        "revision": ledger.revision,
        "initial_cash": ledger.initial_cash,
        "cash": ledger.cash,
        "realized_pnl": ledger.realized_pnl,
        "fees_paid": ledger.fees_paid,
        "positions": tuple(
            {
                "symbol": position.symbol,
                "quantity": position.quantity,
                "book_cost": position.book_cost,
                "lots": tuple(
                    {
                        "opened_revision": lot.opened_revision,
                        "opened_at": lot.opened_at,
                        "unit_cost": lot.unit_cost,
                        "original_quantity": lot.original_quantity,
                        "remaining_quantity": lot.remaining_quantity,
                        "original_cost": lot.original_cost,
                        "remaining_cost": lot.remaining_cost,
                    }
                    for lot in position.lots
                ),
            }
            for position in ledger.positions
        ),
    }


def _order_risk_state(
    config: SimulationConfig,
    ledger: PaperLedgerState,
    projections: Mapping[str, SimulatedOrderProjection],
) -> OrderRiskState:
    (
        reserved_buy_cash,
        reserved_buy_quantity,
        reserved_sell_quantity,
        active_order_count,
        reservation_rows,
    ) = _risk_reservation_state(projections)
    position_quantity = _position_quantity(ledger, config.symbol)
    economic_state: dict[str, object] = {
        "ledger": _ledger_economic_state(ledger),
        "symbol": config.symbol,
        "position_quantity": position_quantity,
        "reserved_buy_cash": reserved_buy_cash,
        "reserved_buy_quantity": reserved_buy_quantity,
        "reserved_sell_quantity": reserved_sell_quantity,
        "active_order_count": active_order_count,
        "reservations": reservation_rows,
    }
    return OrderRiskState.model_validate(
        {
            "account_id_hash": _hash({"account_id": config.account_id}),
            "account_revision": ledger.revision,
            "account_state_hash": _hash(economic_state),
            "symbol": config.symbol,
            "quote_currency": config.quote_currency,
            "cash_balance": ledger.cash,
            "position_quantity": position_quantity,
            "reserved_buy_cash": reserved_buy_cash,
            "reserved_buy_quantity": reserved_buy_quantity,
            "reserved_sell_quantity": reserved_sell_quantity,
            "active_order_count": active_order_count,
        }
    )


def _risk_evaluation_id(
    config: SimulationConfig,
    intent: SubmitOrderIntent,
    state: OrderRiskState,
    price_evidence: OrderPriceEvidence,
) -> str:
    return _derived_id(
        "order-risk",
        config.account_id,
        intent.intent_hash,
        state.account_state_hash,
        price_evidence.snapshot_hash,
        config.order_risk_rule_set.rules_hash,
        config.kill_switch.state_hash,
        config.kill_switch_observed_at,
        config.kill_switch_available_at,
    )


def _evaluate_submit_risk(
    config: SimulationConfig,
    intent: SubmitOrderIntent,
    state: OrderRiskState,
    price_evidence: OrderPriceEvidence,
    *,
    evaluated_at: datetime,
) -> RiskDecision:
    request = build_order_risk_request(
        evaluation_id=_risk_evaluation_id(
            config,
            intent,
            state,
            price_evidence,
        ),
        evaluated_at=evaluated_at,
        source_calculation_version=_RISK_CALCULATION_VERSION,
        rule_set=config.order_risk_rule_set,
        kill_switch=config.kill_switch,
        kill_switch_observed_at=config.kill_switch_observed_at,
        kill_switch_available_at=config.kill_switch_available_at,
        intent=_order_risk_intent(config, intent),
        state=state,
        price_evidence=price_evidence,
    )
    return evaluate_order_risk(request)


def _validate_recorded_risk_decision(
    config: SimulationConfig,
    ledger: PaperLedgerState,
    projections: Mapping[str, SimulatedOrderProjection],
    intent: SubmitOrderIntent,
    decision: RiskDecision,
    *,
    occurred_at: datetime,
    expected_decision: Literal["allow", "reject"],
) -> None:
    request = decision.request
    if not isinstance(request, OrderRiskRequest):
        raise SimulationIntegrityError("Recorded decision is not an order-intent decision.")
    price_evidence = request.price_evidence
    if (
        request.rule_set != config.order_risk_rule_set
        or request.kill_switch != config.kill_switch
        or request.kill_switch_observed_at != config.kill_switch_observed_at
        or request.kill_switch_available_at != config.kill_switch_available_at
        or request.evaluated_at != occurred_at
        or price_evidence.observed_at != occurred_at
        or price_evidence.available_at != occurred_at
        or price_evidence.source != _RISK_PRICE_SOURCE
    ):
        raise SimulationIntegrityError("Recorded risk policy or price timeline is invalid.")
    expected_state = _order_risk_state(config, ledger, projections)
    expected = _evaluate_submit_risk(
        config,
        intent,
        expected_state,
        price_evidence,
        evaluated_at=occurred_at,
    )
    if (
        request.intent != _order_risk_intent(config, intent)
        or request.state != expected_state
        or decision != expected
        or decision.decision_hash != expected.decision_hash
        or decision.decision != expected_decision
    ):
        raise SimulationIntegrityError("Recorded risk decision differs from deterministic replay.")


def _execution_kill_switch_rejection_reason(
    config: SimulationConfig,
    *,
    occurred_at: datetime,
) -> str | None:
    """Recheck the immutable switch evidence at one execution boundary."""

    switch = config.kill_switch
    if switch.status == "engaged":
        return "execution kill-switch: engaged"
    if switch.status == "unavailable":
        return "execution kill-switch: state unavailable"
    if config.kill_switch_observed_at > occurred_at:
        return "execution kill-switch: observation is in the future"
    if config.kill_switch_available_at > occurred_at:
        return "execution kill-switch: snapshot is not yet available"
    maximum_age = timedelta(
        seconds=config.order_risk_rule_set.order_limits.maximum_kill_switch_age_seconds
    )
    if occurred_at - config.kill_switch_observed_at > maximum_age:
        return "execution kill-switch: snapshot is stale"
    return None


def replay_simulation_records(
    config: SimulationConfig,
    records: Iterable[SimulationRecord],
) -> SimulationReplay:
    """Verify record-chain self-consistency without authenticating source inputs.

    This low-level helper has no canonical bars or intents and must not replace
    ``verify_event_simulation`` for a version-two run receipt.
    """

    safe_config = cast(
        SimulationConfig,
        _validated_model(config, SimulationConfig, "Simulation config"),
    )
    previous_hash = _ZERO_HASH
    previous_time: datetime | None = None
    previous_bar_index: int | None = None
    ledger_state: PaperLedgerState | None = None
    projections: dict[str, SimulatedOrderProjection] = {}
    seen_order_ids: set[str] = set()
    intent_keys: dict[str, str] = {}
    intent_sequences: set[int] = set()
    expected_sequence = 1

    for raw_record in records:
        try:
            record = _RECORD_ADAPTER.validate_python(
                raw_record.model_dump(mode="python")
                if isinstance(raw_record, _RecordBase)
                else raw_record
            )
        except Exception as error:
            raise SimulationIntegrityError(
                "Simulation record failed strict revalidation."
            ) from error
        if record.sequence != expected_sequence or record.previous_hash != previous_hash:
            raise SimulationIntegrityError("Simulation record chain is not contiguous.")
        if previous_time is not None and record.occurred_at < previous_time:
            raise SimulationIntegrityError("Simulation record times are not monotonic.")
        if record.bar_index is not None:
            if previous_bar_index is not None and record.bar_index < previous_bar_index:
                raise SimulationIntegrityError("Simulation record bar indices are not monotonic.")
            previous_bar_index = record.bar_index
        previous_time = record.occurred_at
        expected_sequence += 1
        previous_hash = record.record_hash

        if isinstance(record, AccountOpenedRecord):
            if ledger_state is not None or record.sequence != 1 or record.bar_index is not None:
                raise SimulationIntegrityError("The account opening must be the first record.")
            if (
                record.opening_event.account_id != safe_config.account_id
                or record.opening_event.currency != safe_config.quote_currency
                or record.opening_event.initial_cash != safe_config.initial_cash
                or record.opening_event.occurred_at != safe_config.opened_at
                or record.occurred_at != safe_config.opened_at
            ):
                raise SimulationIntegrityError("Opening record differs from simulation config.")
            ledger_state = open_paper_ledger(record.opening_event)
            continue

        if ledger_state is None:
            raise SimulationIntegrityError("Simulation records precede the account opening.")

        if isinstance(record, OrderSubmittedRecord):
            intent = record.intent
            if (
                intent.symbol != safe_config.symbol
                or intent.order_id in seen_order_ids
                or intent.idempotency_key in intent_keys
                or intent.sequence in intent_sequences
            ):
                raise SimulationIntegrityError("Submitted order identity is invalid or duplicated.")
            if (
                record.occurred_at != intent.created_at
                or record.bar_index is None
                or record.eligible_bar_index != record.bar_index + 1 + safe_config.latency_bars
            ):
                raise SimulationIntegrityError("Submitted order record timing is invalid.")
            _validate_recorded_risk_decision(
                safe_config,
                ledger_state,
                projections,
                intent,
                record.risk_decision,
                occurred_at=record.occurred_at,
                expected_decision="allow",
            )
            projections[intent.order_id] = SimulatedOrderProjection(
                intent=intent,
                eligible_bar_index=record.eligible_bar_index,
                risk_decision=record.risk_decision,
                state=create_order_state(
                    order_id=intent.order_id,
                    requested_quantity=intent.quantity,
                ),
            )
            seen_order_ids.add(intent.order_id)
            intent_keys[intent.idempotency_key] = intent.intent_hash
            intent_sequences.add(intent.sequence)
            continue

        if isinstance(record, OrderRiskRejectedRecord):
            intent = record.intent
            if (
                intent.symbol != safe_config.symbol
                or intent.order_id in seen_order_ids
                or intent.idempotency_key in intent_keys
                or intent.sequence in intent_sequences
                or record.occurred_at != intent.created_at
                or record.bar_index is None
            ):
                raise SimulationIntegrityError("Risk-rejected order identity or timing is invalid.")
            _validate_recorded_risk_decision(
                safe_config,
                ledger_state,
                projections,
                intent,
                record.risk_decision,
                occurred_at=record.occurred_at,
                expected_decision="reject",
            )
            seen_order_ids.add(intent.order_id)
            intent_keys[intent.idempotency_key] = intent.intent_hash
            intent_sequences.add(intent.sequence)
            continue

        if isinstance(record, OrderCancellationRecord):
            projection = projections.get(record.intent.order_id)
            if projection is None:
                raise SimulationIntegrityError("Cancellation references an unknown order.")
            if (
                record.intent.created_at != record.occurred_at
                or record.request_event.order_id != projection.state.order_id
                or record.acknowledgement_event.order_id != projection.state.order_id
                or record.bar_index is None
            ):
                raise SimulationIntegrityError("Cancellation record identities do not match.")
            prior_intent_hash = intent_keys.get(record.intent.idempotency_key)
            if (
                prior_intent_hash is not None and prior_intent_hash != record.intent.intent_hash
            ) or record.intent.sequence in intent_sequences:
                raise SimulationIntegrityError("Cancellation intent identity is duplicated.")
            try:
                next_state = apply_order_event(projection.state, record.request_event)
                next_state = apply_order_event(next_state, record.acknowledgement_event)
            except Exception as error:
                raise SimulationIntegrityError(
                    "Cancellation cannot replay from order state."
                ) from error
            projections[projection.state.order_id] = projection.model_copy(
                update={"state": next_state}
            )
            intent_keys[record.intent.idempotency_key] = record.intent.intent_hash
            intent_sequences.add(record.intent.sequence)
            continue

        if isinstance(record, OrderEventRecord):
            projection = projections.get(record.order_event.order_id)
            if (
                projection is None
                or record.bar_index is None
                or record.bar_index < projection.eligible_bar_index
            ):
                raise SimulationIntegrityError("Order event references an unknown order.")
            if (
                isinstance(record.order_event, OrderAcknowledgedEvent)
                and _execution_kill_switch_rejection_reason(
                    safe_config,
                    occurred_at=record.occurred_at,
                )
                is not None
            ):
                raise SimulationIntegrityError(
                    "Order acknowledgement used invalid execution-time kill-switch evidence."
                )
            try:
                next_state = apply_order_event(projection.state, record.order_event)
            except Exception as error:
                raise SimulationIntegrityError(
                    "Order event cannot replay from order state."
                ) from error
            projections[projection.state.order_id] = projection.model_copy(
                update={"state": next_state}
            )
            continue

        if isinstance(record, AtomicFillRecord):
            projection = projections.get(record.order_event.order_id)
            if (
                projection is None
                or record.bar_index is None
                or record.bar_index < projection.eligible_bar_index
            ):
                raise SimulationIntegrityError("Fill references an unknown order.")
            if (
                record.ledger_event.account_id != safe_config.account_id
                or record.ledger_event.symbol != safe_config.symbol
                or record.ledger_event.side != projection.intent.side
                or record.ledger_event.expected_revision != ledger_state.revision
            ):
                raise SimulationIntegrityError("Atomic ledger fill targets the wrong projection.")
            if (
                _execution_kill_switch_rejection_reason(
                    safe_config,
                    occurred_at=record.occurred_at,
                )
                is not None
            ):
                raise SimulationIntegrityError(
                    "Atomic fill used invalid execution-time kill-switch evidence."
                )
            expected_fee = _product(
                record.order_event.fill_quantity,
                record.order_event.fill_price,
                safe_config.fee_rate,
            )
            envelope_rejection = _execution_envelope_rejection_reason(
                safe_config,
                ledger_state,
                projections,
                projection,
                candidate_fill_price=record.order_event.fill_price,
            )
            if record.ledger_event.fee != expected_fee or envelope_rejection is not None:
                raise SimulationIntegrityError(
                    "Atomic fill breaches its execution-time risk envelope."
                )
            try:
                next_order = apply_order_event(projection.state, record.order_event)
                next_ledger = apply_paper_fill(ledger_state, record.ledger_event).state
            except Exception as error:
                raise SimulationIntegrityError(
                    "Atomic fill cannot replay from current state."
                ) from error
            projections[projection.state.order_id] = projection.model_copy(
                update={"state": next_order}
            )
            ledger_state = next_ledger
            continue

        raise SimulationIntegrityError("Unsupported simulation record type.")

    if ledger_state is None:
        raise SimulationIntegrityError("Simulation record stream has no account opening.")
    ordered = tuple(sorted(projections.values(), key=lambda item: item.intent.sequence))
    return SimulationReplay(
        orders=ordered,
        ledger_state=ledger_state,
        final_record_hash=previous_hash,
        projection_hash=_projection_hash(ordered, ledger_state),
    )


def verify_event_simulation(run: EventSimulationRun) -> SimulationReplay:
    """Rebuild a stored run from canonical evidence and compare every output."""

    try:
        safe_run = EventSimulationRun.model_validate(run.model_dump(mode="python"))
    except Exception as error:
        raise SimulationIntegrityError("Simulation run failed strict revalidation.") from error
    try:
        expected_run = _simulate_event_driven(
            safe_run.config,
            safe_run.bars,
            safe_run.intents,
            verify_result=False,
        )
        replay = replay_simulation_records(safe_run.config, safe_run.records)
    except SimulationIntegrityError:
        raise
    except Exception as error:
        raise SimulationIntegrityError(
            "Canonical simulation evidence cannot be reproduced."
        ) from error
    if safe_run != expected_run:
        raise SimulationIntegrityError(
            "Simulation outputs differ from deterministic canonical-input replay."
        )
    return replay


def _execution_candidate(
    intent: SubmitOrderIntent,
    bar: SimulationBar,
) -> tuple[datetime, Decimal] | None:
    if intent.order_type == "market":
        return bar.opened_at, bar.open
    limit_price = intent.limit_price
    if limit_price is None:  # pragma: no cover - model invariant
        raise SimulationInputError("Validated limit order lost its limit price.")
    if intent.side == "buy":
        if bar.open <= limit_price:
            return bar.opened_at, bar.open
        return (bar.closed_at, limit_price) if bar.low <= limit_price else None
    if bar.open >= limit_price:
        return bar.opened_at, bar.open
    return (bar.closed_at, limit_price) if bar.high >= limit_price else None


def _fill_price(
    intent: SubmitOrderIntent,
    reference_price: Decimal,
    slippage_bps: Decimal,
) -> Decimal:
    slip = _product(slippage_bps, Decimal("0.0001"))
    if intent.side == "buy":
        modeled = _product(reference_price, _sum(Decimal(1), slip))
        if intent.limit_price is not None:
            return min(modeled, intent.limit_price)
        return modeled
    modeled = _product(reference_price, _difference(Decimal(1), slip))
    if modeled <= 0:
        raise SimulationInputError("Sell slippage produced a non-positive fill price.")
    if intent.limit_price is not None:
        return max(modeled, intent.limit_price)
    return modeled


def _cash_requirement(quantity: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    gross = _product(quantity, price)
    return _sum(gross, _product(gross, fee_rate))


def _available_cash(
    ledger: PaperLedgerState,
    projections: Mapping[str, SimulatedOrderProjection],
    *,
    current: SimulatedOrderProjection,
) -> Decimal:
    reserved = Decimal(0)
    for projection in projections.values():
        if (
            projection.intent.order_id == current.intent.order_id
            or projection.intent.sequence >= current.intent.sequence
        ):
            continue
        if projection.intent.side != "buy" or projection.state.status not in {
            "pending_submit",
            "working",
            "partially_filled",
        }:
            continue
        remaining = _difference(
            projection.state.requested_quantity,
            projection.state.filled_quantity,
        )
        fee_multiplier = _sum(
            Decimal(1),
            _decision_fee_buffer_ratio(projection.risk_decision),
        )
        reserved = _sum(
            reserved,
            _product(
                remaining,
                _decision_risk_price(projection.risk_decision),
                fee_multiplier,
            ),
        )
    return _difference(ledger.cash, reserved)


def _available_position(
    ledger: PaperLedgerState,
    projections: Mapping[str, SimulatedOrderProjection],
    symbol: str,
    *,
    current: SimulatedOrderProjection,
) -> Decimal:
    reserved = Decimal(0)
    for projection in projections.values():
        if (
            projection.intent.order_id == current.intent.order_id
            or projection.intent.sequence >= current.intent.sequence
        ):
            continue
        if projection.intent.side != "sell" or projection.state.status not in {
            "pending_submit",
            "working",
            "partially_filled",
        }:
            continue
        reserved = _sum(
            reserved,
            _difference(
                projection.state.requested_quantity,
                projection.state.filled_quantity,
            ),
        )
    return _difference(_position_quantity(ledger, symbol), reserved)


def _execution_envelope_rejection_reason(
    config: SimulationConfig,
    ledger: PaperLedgerState,
    projections: Mapping[str, SimulatedOrderProjection],
    projection: SimulatedOrderProjection,
    *,
    candidate_fill_price: Decimal,
) -> str | None:
    """Check actual execution economics without creating a second risk decision."""

    intent = projection.intent
    state = projection.state
    limits = config.order_risk_rule_set.order_limits
    if intent.order_type == "limit":
        limit_price = intent.limit_price
        if limit_price is None:  # pragma: no cover - model invariant
            raise SimulationIntegrityError("Limit order lost its limit price.")
        if intent.side == "buy" and candidate_fill_price > limit_price:
            return "execution risk envelope: candidate price exceeds buy limit"
        if intent.side == "sell" and candidate_fill_price < limit_price:
            return "execution risk envelope: candidate price is below sell limit"
    approved_risk_price = _decision_execution_price_boundary(
        projection.risk_decision
    )
    if intent.side == "buy" and candidate_fill_price > approved_risk_price:
        return "execution risk envelope: candidate price exceeds approved risk price"
    if intent.side == "sell" and candidate_fill_price < approved_risk_price:
        return "execution risk envelope: candidate price is below approved risk price"

    remaining = _difference(state.requested_quantity, state.filled_quantity)
    projected_order_notional = _sum(
        state.filled_notional,
        _product(remaining, candidate_fill_price),
    )
    if projected_order_notional > limits.maximum_order_notional:
        return "execution risk envelope: maximum order notional exceeded"

    if intent.side == "buy":
        available_cash = _available_cash(
            ledger,
            projections,
            current=projection,
        )
        required_cash = _cash_requirement(
            remaining,
            candidate_fill_price,
            config.fee_rate,
        )
        if required_cash > available_cash:
            return "execution risk envelope: insufficient cash"
        if _difference(available_cash, required_cash) < limits.minimum_cash_reserve:
            return "execution risk envelope: minimum cash reserve breached"
        return None

    available_position = _available_position(
        ledger,
        projections,
        config.symbol,
        current=projection,
    )
    if remaining > available_position:
        return "execution risk envelope: insufficient long position"
    return None


def _acknowledgement_envelope_price(
    intent: SubmitOrderIntent,
    bar: SimulationBar,
    slippage_bps: Decimal,
) -> Decimal:
    if intent.order_type == "market":
        reference = bar.open
    else:
        limit_price = intent.limit_price
        if limit_price is None:  # pragma: no cover - model invariant
            raise SimulationInputError("Validated limit order lost its limit price.")
        opens_through_limit = (
            bar.open <= limit_price if intent.side == "buy" else bar.open >= limit_price
        )
        reference = bar.open if opens_through_limit else limit_price
    return _fill_price(intent, reference, slippage_bps)


def _validate_inputs(
    config: SimulationConfig,
    bars: Iterable[SimulationBar],
    intents: Iterable[SimulationIntent],
) -> tuple[SimulationConfig, tuple[SimulationBar, ...], tuple[SimulationIntent, ...]]:
    safe_config = cast(
        SimulationConfig,
        _validated_model(config, SimulationConfig, "Simulation config"),
    )
    safe_bars: list[SimulationBar] = []
    for raw_bar in bars:
        safe_bars.append(
            cast(
                SimulationBar,
                _validated_model(raw_bar, SimulationBar, "Simulation bar"),
            )
        )
    if not safe_bars:
        raise SimulationInputError("At least one finalized bar is required.")
    for expected_index, bar in enumerate(safe_bars):
        if bar.index != expected_index:
            raise SimulationInputError("Bar indices must be zero-based, contiguous, and ordered.")
        if bar.symbol != safe_config.symbol:
            raise SimulationInputError("Every bar must match the configured symbol.")
        if expected_index and bar.opened_at < safe_bars[expected_index - 1].closed_at:
            raise SimulationInputError("Bars must be ordered and cannot overlap.")
    if safe_config.opened_at > safe_bars[0].opened_at:
        raise SimulationInputError("Paper account must open no later than the first bar.")

    safe_intents: list[SimulationIntent] = []
    by_idempotency_key: dict[str, SimulationIntent] = {}
    for raw_intent in intents:
        try:
            intent = _INTENT_ADAPTER.validate_python(
                raw_intent.model_dump(mode="python")
                if isinstance(raw_intent, (SubmitOrderIntent, CancelOrderIntent))
                else raw_intent
            )
        except Exception as error:
            raise SimulationInputError("Simulation intent failed strict revalidation.") from error
        prior = by_idempotency_key.get(intent.idempotency_key)
        if prior is not None:
            if prior.intent_hash != intent.intent_hash:
                raise SimulationInputError(
                    "Intent idempotency key was reused for a different payload."
                )
            continue
        by_idempotency_key[intent.idempotency_key] = intent
        safe_intents.append(intent)

    canonical_order = sorted(safe_intents, key=lambda item: (item.created_at, item.sequence))
    if safe_intents != canonical_order:
        raise SimulationInputError("Intents must be ordered by creation time and sequence.")
    sequences = tuple(intent.sequence for intent in safe_intents)
    if len(set(sequences)) != len(sequences):
        raise SimulationInputError("Intent sequence numbers must be unique.")
    closes = {bar.closed_at for bar in safe_bars}
    for intent in safe_intents:
        if intent.created_at not in closes:
            raise SimulationInputError("Every intent must be tied to a finalized bar close.")
        if isinstance(intent, SubmitOrderIntent) and intent.symbol != safe_config.symbol:
            raise SimulationInputError("Every submitted order must match the configured symbol.")
    return safe_config, tuple(safe_bars), tuple(safe_intents)


def _simulate_event_driven(
    config: SimulationConfig,
    bars: Iterable[SimulationBar],
    intents: Iterable[SimulationIntent],
    *,
    verify_result: bool,
) -> EventSimulationRun:
    safe_config, safe_bars, safe_intents = _validate_inputs(config, bars, intents)
    records: list[SimulationRecord] = []
    projections: dict[str, SimulatedOrderProjection] = {}
    seen_order_ids: set[str] = set()
    intents_by_close: dict[datetime, list[SimulationIntent]] = {}
    for intent in safe_intents:
        intents_by_close.setdefault(intent.created_at, []).append(intent)

    def append_record(
        record_class: type[RecordT],
        *,
        occurred_at: datetime,
        bar_index: int | None,
        payload: Mapping[str, object],
    ) -> RecordT:
        record = _build_record(
            record_class,
            sequence=len(records) + 1,
            previous_hash=records[-1].record_hash if records else _ZERO_HASH,
            occurred_at=occurred_at,
            bar_index=bar_index,
            payload=payload,
        )
        records.append(cast(SimulationRecord, record))
        return record

    opening = build_opening_balance_event(
        account_id=safe_config.account_id,
        idempotency_key=_derived_id("open", safe_config.account_id),
        currency=safe_config.quote_currency,
        initial_cash=safe_config.initial_cash,
        occurred_at=safe_config.opened_at,
    )
    append_record(
        AccountOpenedRecord,
        occurred_at=safe_config.opened_at,
        bar_index=None,
        payload={"record_type": "account_opened", "opening_event": opening},
    )
    ledger = open_paper_ledger(opening)

    def apply_non_fill(
        projection: SimulatedOrderProjection,
        event: NonFillOrderEvent,
        *,
        occurred_at: datetime,
        bar_index: int,
    ) -> SimulatedOrderProjection:
        append_record(
            OrderEventRecord,
            occurred_at=occurred_at,
            bar_index=bar_index,
            payload={"record_type": "order_event", "order_event": event},
        )
        next_projection = projection.model_copy(
            update={"state": apply_order_event(projection.state, event)}
        )
        projections[projection.intent.order_id] = next_projection
        return next_projection

    for bar in safe_bars:
        remaining_volume = _product(bar.volume, safe_config.volume_participation_rate)
        active = sorted(projections.values(), key=lambda item: item.intent.sequence)

        # Venue acknowledgements happen at the eligible bar open. Resource reservations
        # are allocated in submission order before any order can consume this bar.
        for projection in active:
            intent = projection.intent
            state = projection.state
            if state.status in {"filled", "cancelled", "rejected", "expired"}:
                continue
            if bar.index < projection.eligible_bar_index:
                continue

            if state.status == "pending_submit":
                acknowledgement_price = _acknowledgement_envelope_price(
                    intent,
                    bar,
                    safe_config.slippage_bps,
                )
                rejection_reason = _execution_kill_switch_rejection_reason(
                    safe_config,
                    occurred_at=bar.opened_at,
                )
                if rejection_reason is None:
                    rejection_reason = _execution_envelope_rejection_reason(
                        safe_config,
                        ledger,
                        projections,
                        projection,
                        candidate_fill_price=acknowledgement_price,
                    )
                if rejection_reason is not None:
                    rejection = OrderRejectedEvent.model_validate(
                        {
                            "order_id": intent.order_id,
                            "idempotency_key": _derived_id(
                                "reject",
                                intent.order_id,
                            ),
                            "reason": rejection_reason,
                        }
                    )
                    projection = apply_non_fill(
                        projection,
                        rejection,
                        occurred_at=bar.opened_at,
                        bar_index=bar.index,
                    )
                    continue
                acknowledgement = OrderAcknowledgedEvent.model_validate(
                    {
                        "order_id": intent.order_id,
                        "idempotency_key": _derived_id("ack", intent.order_id),
                    }
                )
                projection = apply_non_fill(
                    projection,
                    acknowledgement,
                    occurred_at=bar.opened_at,
                    bar_index=bar.index,
                )
                state = projection.state

        candidates: list[tuple[datetime, int, str, Decimal]] = []
        for projection in projections.values():
            if bar.index < projection.eligible_bar_index or projection.state.status not in {
                "working",
                "partially_filled",
            }:
                continue
            candidate = _execution_candidate(projection.intent, bar)
            if candidate is not None:
                occurred_at, reference = candidate
                candidates.append(
                    (
                        occurred_at,
                        projection.intent.sequence,
                        projection.intent.order_id,
                        reference,
                    )
                )

        # Gap/open executions precede intrabar limit touches. A touch inferred from
        # high/low is timestamped at close, when that finalized information exists.
        for occurred_at, _, order_id, reference in sorted(candidates):
            if remaining_volume <= 0:
                break
            projection = projections[order_id]
            intent = projection.intent
            state = projection.state
            fill_price = _fill_price(intent, reference, safe_config.slippage_bps)
            remaining_quantity = _difference(
                state.requested_quantity,
                state.filled_quantity,
            )
            terminal_reason = _execution_kill_switch_rejection_reason(
                safe_config,
                occurred_at=occurred_at,
            )
            if terminal_reason is None:
                terminal_reason = _execution_envelope_rejection_reason(
                    safe_config,
                    ledger,
                    projections,
                    projection,
                    candidate_fill_price=fill_price,
                )
            if terminal_reason is not None:
                expiry = OrderExpiredEvent.model_validate(
                    {
                        "order_id": intent.order_id,
                        "idempotency_key": _derived_id(
                            "resource-expiry",
                            intent.order_id,
                        ),
                        "reason": terminal_reason,
                    }
                )
                apply_non_fill(
                    projection,
                    expiry,
                    occurred_at=occurred_at,
                    bar_index=bar.index,
                )
                continue

            fill_quantity = min(remaining_quantity, remaining_volume)
            fill_number = 1 + sum(
                receipt.event_type == "fill" for receipt in projection.state.event_receipts
            )
            fill_id = _derived_id("fill", intent.order_id, fill_number)
            idempotency_key = _derived_id(
                "fill-event",
                intent.order_id,
                fill_number,
            )
            order_fill = OrderFillEvent.model_validate(
                {
                    "order_id": intent.order_id,
                    "idempotency_key": idempotency_key,
                    "fill_id": fill_id,
                    "fill_quantity": fill_quantity,
                    "fill_price": fill_price,
                }
            )
            gross = _product(fill_quantity, fill_price)
            ledger_fill = build_paper_fill_event(
                account_id=safe_config.account_id,
                expected_revision=ledger.revision,
                idempotency_key=idempotency_key,
                fill_id=fill_id,
                order_id=intent.order_id,
                symbol=safe_config.symbol,
                side=intent.side,
                quantity=fill_quantity,
                reference_price=reference,
                fill_price=fill_price,
                fee=_product(gross, safe_config.fee_rate),
                occurred_at=occurred_at,
            )
            append_record(
                AtomicFillRecord,
                occurred_at=occurred_at,
                bar_index=bar.index,
                payload={
                    "record_type": "atomic_fill",
                    "order_event": order_fill,
                    "ledger_event": ledger_fill,
                },
            )
            next_order = apply_order_event(projection.state, order_fill)
            next_ledger = apply_paper_fill(ledger, ledger_fill).state
            projections[intent.order_id] = projection.model_copy(update={"state": next_order})
            ledger = next_ledger
            remaining_volume = _difference(remaining_volume, fill_quantity)

        for projection in sorted(
            projections.values(),
            key=lambda item: item.intent.sequence,
        ):
            intent = projection.intent
            expires = intent.expires_after_bars
            expiry_index = (
                projection.eligible_bar_index + expires - 1 if expires is not None else None
            )
            if (
                expiry_index is not None
                and bar.index >= expiry_index
                and projection.state.status in {"working", "partially_filled"}
            ):
                expiry = OrderExpiredEvent.model_validate(
                    {
                        "order_id": intent.order_id,
                        "idempotency_key": _derived_id(
                            "expire",
                            intent.order_id,
                        ),
                        "reason": "time in force elapsed",
                    }
                )
                apply_non_fill(
                    projection,
                    expiry,
                    occurred_at=bar.closed_at,
                    bar_index=bar.index,
                )

        for intent in intents_by_close.get(bar.closed_at, []):
            if isinstance(intent, SubmitOrderIntent):
                if intent.order_id in seen_order_ids:
                    raise SimulationInputError("Order ids must be unique.")
                seen_order_ids.add(intent.order_id)
                risk_state = _order_risk_state(
                    safe_config,
                    ledger,
                    projections,
                )
                price_evidence = OrderPriceEvidence(
                    symbol=safe_config.symbol,
                    quote_currency=safe_config.quote_currency,
                    reference_price=bar.close,
                    observed_at=bar.closed_at,
                    available_at=bar.closed_at,
                    source=_RISK_PRICE_SOURCE,
                    snapshot_hash=bar.bar_hash,
                )
                risk_decision = _evaluate_submit_risk(
                    safe_config,
                    intent,
                    risk_state,
                    price_evidence,
                    evaluated_at=bar.closed_at,
                )
                if risk_decision.decision == "reject":
                    append_record(
                        OrderRiskRejectedRecord,
                        occurred_at=bar.closed_at,
                        bar_index=bar.index,
                        payload={
                            "record_type": "order_risk_rejected",
                            "intent": intent,
                            "risk_decision": risk_decision,
                        },
                    )
                    continue
                if risk_decision.decision != "allow":  # pragma: no cover - order contract
                    raise SimulationIntegrityError(
                        "Order risk evaluation returned an unsupported clipped decision."
                    )
                eligible_bar_index = bar.index + 1 + safe_config.latency_bars
                projection = SimulatedOrderProjection(
                    intent=intent,
                    eligible_bar_index=eligible_bar_index,
                    risk_decision=risk_decision,
                    state=create_order_state(
                        order_id=intent.order_id,
                        requested_quantity=intent.quantity,
                    ),
                )
                projections[intent.order_id] = projection
                append_record(
                    OrderSubmittedRecord,
                    occurred_at=bar.closed_at,
                    bar_index=bar.index,
                    payload={
                        "record_type": "order_submitted",
                        "intent": intent,
                        "eligible_bar_index": eligible_bar_index,
                        "risk_decision": risk_decision,
                    },
                )
                continue

            cancellation_projection = projections.get(intent.order_id)
            if cancellation_projection is None:
                raise SimulationInputError("Cancellation references an unknown order.")
            if cancellation_projection.state.status in {
                "filled",
                "cancelled",
                "rejected",
                "expired",
            }:
                raise SimulationInputError("Cancellation references a terminal order.")
            request = OrderCancelRequestedEvent.model_validate(
                {
                    "order_id": intent.order_id,
                    "idempotency_key": _derived_id(
                        "cancel-request",
                        intent.idempotency_key,
                    ),
                }
            )
            cancel_acknowledgement = OrderCancelAcknowledgedEvent.model_validate(
                {
                    "order_id": intent.order_id,
                    "idempotency_key": _derived_id(
                        "cancel-ack",
                        intent.idempotency_key,
                    ),
                }
            )
            append_record(
                OrderCancellationRecord,
                occurred_at=bar.closed_at,
                bar_index=bar.index,
                payload={
                    "record_type": "order_cancelled",
                    "intent": intent,
                    "request_event": request,
                    "acknowledgement_event": cancel_acknowledgement,
                },
            )
            next_state = apply_order_event(cancellation_projection.state, request)
            next_state = apply_order_event(next_state, cancel_acknowledgement)
            projections[intent.order_id] = cancellation_projection.model_copy(
                update={"state": next_state}
            )

    replay = replay_simulation_records(safe_config, records)
    bars_hash = _hash(safe_bars)
    intents_hash = _hash(safe_intents)
    replay_hash = _hash(
        {
            "config": safe_config,
            "bars_hash": bars_hash,
            "intents_hash": intents_hash,
            "final_record_hash": replay.final_record_hash,
            "projection_hash": replay.projection_hash,
        }
    )
    run = EventSimulationRun(
        config=safe_config,
        bars=safe_bars,
        intents=safe_intents,
        bars_hash=bars_hash,
        intents_hash=intents_hash,
        records=tuple(records),
        orders=replay.orders,
        ledger_state=replay.ledger_state,
        final_record_hash=replay.final_record_hash,
        replay_hash=replay_hash,
    )
    if verify_result:
        verify_event_simulation(run)
    return run


def simulate_event_driven(
    config: SimulationConfig,
    bars: Iterable[SimulationBar],
    intents: Iterable[SimulationIntent],
) -> EventSimulationRun:
    """Run an offline deterministic next-bar simulator without any venue connection."""

    return _simulate_event_driven(
        config,
        bars,
        intents,
        verify_result=True,
    )
