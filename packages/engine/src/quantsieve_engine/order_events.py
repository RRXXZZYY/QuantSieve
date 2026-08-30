from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Annotated, Literal, Self, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OrderStatus: TypeAlias = Literal[
    "pending_submit",
    "working",
    "partially_filled",
    "pending_cancel",
    "filled",
    "cancelled",
    "rejected",
    "expired",
]
ActiveOrderStatus: TypeAlias = Literal[
    "pending_submit",
    "working",
    "partially_filled",
]
OrderEventType: TypeAlias = Literal[
    "acknowledged",
    "fill",
    "cancel_requested",
    "cancel_acknowledged",
    "cancel_rejected",
    "rejected",
    "expired",
]

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_DECIMAL_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?$")
_AVERAGE_PRICE_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset(
    {"filled", "cancelled", "rejected", "expired"}
)
_CanonicalValue: TypeAlias = (
    bool | int | str | list["_CanonicalValue"] | dict[str, "_CanonicalValue"] | None
)


class OrderStateMachineError(ValueError):
    """Base error for deterministic order-event reduction failures."""


class OrderTransitionError(OrderStateMachineError):
    """Raised when a new event is not legal from the current order status."""


class OrderIdempotencyConflictError(OrderStateMachineError):
    """Raised when an idempotency key is reused for a different event payload."""


class OrderInvariantError(OrderStateMachineError):
    """Raised when applying an event would violate an economic invariant."""


def _normalize_text(value: object, *, label: str, maximum_length: int = 200) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty.")
    if len(normalized) > maximum_length:
        raise ValueError(f"{label} must not exceed {maximum_length} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{label} must not contain control characters.")
    return normalized


def _normalize_optional_text(
    value: object,
    *,
    label: str,
    maximum_length: int = 500,
) -> str | None:
    if value is None:
        return None
    return _normalize_text(value, label=label, maximum_length=maximum_length)


def _canonical_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("Order decimals must be finite.")
    if value == 0:
        return Decimal(0)
    sign, digits, exponent = value.as_tuple()
    canonical_digits = list(digits)
    canonical_exponent = cast(int, exponent)
    while canonical_digits[-1] == 0:
        canonical_digits.pop()
        canonical_exponent += 1
    return Decimal((sign, tuple(canonical_digits), canonical_exponent))


def _normalize_decimal(value: object, *, label: str) -> Decimal:
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not _DECIMAL_PATTERN.fullmatch(text):
            raise ValueError(f"{label} must be a base-10 decimal.")
        parsed = Decimal(text)
    else:
        raise ValueError(f"{label} must be a Decimal or base-10 decimal string.")
    try:
        return _canonical_decimal(parsed)
    except ValueError as error:
        raise ValueError(f"{label} must be finite.") from error


def _decimal_coefficient_and_exponent(value: Decimal) -> tuple[int, int]:
    canonical = _canonical_decimal(value)
    sign, digits, exponent = canonical.as_tuple()
    coefficient = int("".join(str(digit) for digit in digits))
    if sign:
        coefficient = -coefficient
    return coefficient, cast(int, exponent)


def _decimal_from_coefficient(coefficient: int, exponent: int) -> Decimal:
    if coefficient == 0:
        return Decimal(0)
    sign = int(coefficient < 0)
    digits = tuple(int(character) for character in str(abs(coefficient)))
    return _canonical_decimal(Decimal((sign, digits, exponent)))


def _exact_add(left: Decimal, right: Decimal) -> Decimal:
    left_coefficient, left_exponent = _decimal_coefficient_and_exponent(left)
    right_coefficient, right_exponent = _decimal_coefficient_and_exponent(right)
    common_exponent = min(left_exponent, right_exponent)
    coefficient = left_coefficient * (
        10 ** (left_exponent - common_exponent)
    ) + right_coefficient * (10 ** (right_exponent - common_exponent))
    return _decimal_from_coefficient(coefficient, common_exponent)


def _exact_multiply(left: Decimal, right: Decimal) -> Decimal:
    left_coefficient, left_exponent = _decimal_coefficient_and_exponent(left)
    right_coefficient, right_exponent = _decimal_coefficient_and_exponent(right)
    return _decimal_from_coefficient(
        left_coefficient * right_coefficient,
        left_exponent + right_exponent,
    )


def _average_price(notional: Decimal, quantity: Decimal) -> Decimal:
    with localcontext(_AVERAGE_PRICE_CONTEXT):
        return _canonical_decimal(notional / quantity)


def _canonical_decimal_text(value: Decimal) -> str:
    canonical = _canonical_decimal(value)
    return "0" if canonical == 0 else format(canonical, "f")


def _canonical_value(value: object) -> _CanonicalValue:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, Decimal):
        return _canonical_decimal_text(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Mapping):
        canonical: dict[str, _CanonicalValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("Canonical order-event mappings require string keys.")
            canonical[key] = _canonical_value(item)
        return canonical
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_value(item) for item in value]
    raise TypeError(f"Canonical order-event payloads do not support {type(value).__name__}.")


def _canonical_json(value: object) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def order_event_payload_hash(value: BaseModel | Mapping[str, object]) -> str:
    """Hash an event's economic payload, excluding its delivery idempotency key."""

    payload = value.model_dump(mode="python") if isinstance(value, BaseModel) else dict(value)
    payload.pop("idempotency_key", None)
    payload.pop("payload_hash", None)
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class _FrozenContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        allow_inf_nan=False,
    )


class _OrderEventContract(_FrozenContract):
    schema_version: Literal[1] = 1
    event_type: OrderEventType
    order_id: str
    idempotency_key: str
    payload_hash: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="before")
    @classmethod
    def normalize_and_hash_payload(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        normalized = dict(value)
        for field_name, field in cls.model_fields.items():
            if (
                field_name not in normalized
                and field_name != "payload_hash"
                and not field.is_required()
            ):
                normalized[field_name] = field.get_default(call_default_factory=True)
        if "order_id" in normalized:
            normalized["order_id"] = _normalize_text(
                normalized["order_id"],
                label="Order id",
            )
        if "idempotency_key" in normalized:
            normalized["idempotency_key"] = _normalize_text(
                normalized["idempotency_key"],
                label="Event idempotency key",
            )
        if "fill_id" in normalized:
            normalized["fill_id"] = _normalize_text(
                normalized["fill_id"],
                label="Fill id",
            )
        for decimal_field, label in (
            ("fill_quantity", "Fill quantity"),
            ("fill_price", "Fill price"),
        ):
            if decimal_field in normalized:
                normalized[decimal_field] = _normalize_decimal(
                    normalized[decimal_field],
                    label=label,
                )
        if "reason" in normalized:
            normalized["reason"] = _normalize_optional_text(
                normalized["reason"],
                label="Event reason",
            )

        expected_hash = order_event_payload_hash(normalized)
        supplied_hash = normalized.get("payload_hash")
        if supplied_hash is None:
            normalized["payload_hash"] = expected_hash
        elif supplied_hash != expected_hash:
            raise ValueError("Order event payload_hash does not match its payload.")
        return normalized

    @field_validator("order_id", mode="before")
    @classmethod
    def normalize_order_id(cls, value: object) -> str:
        return _normalize_text(value, label="Order id")

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def normalize_idempotency_key(cls, value: object) -> str:
        return _normalize_text(value, label="Event idempotency key")


class OrderAcknowledgedEvent(_OrderEventContract):
    event_type: Literal["acknowledged"] = "acknowledged"


class OrderFillEvent(_OrderEventContract):
    event_type: Literal["fill"] = "fill"
    fill_id: str
    fill_quantity: Decimal = Field(gt=0)
    fill_price: Decimal = Field(gt=0)

    @field_validator("fill_id", mode="before")
    @classmethod
    def normalize_fill_id(cls, value: object) -> str:
        return _normalize_text(value, label="Fill id")

    @field_validator("fill_quantity", "fill_price", mode="before")
    @classmethod
    def normalize_fill_decimals(cls, value: object, info: object) -> Decimal:
        field_name = getattr(info, "field_name", "fill decimal")
        label = "Fill quantity" if field_name == "fill_quantity" else "Fill price"
        return _normalize_decimal(value, label=label)


class OrderCancelRequestedEvent(_OrderEventContract):
    event_type: Literal["cancel_requested"] = "cancel_requested"


class OrderCancelAcknowledgedEvent(_OrderEventContract):
    event_type: Literal["cancel_acknowledged"] = "cancel_acknowledged"


class OrderCancelRejectedEvent(_OrderEventContract):
    event_type: Literal["cancel_rejected"] = "cancel_rejected"
    reason: str

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> str:
        return _normalize_text(value, label="Cancel rejection reason", maximum_length=500)


class OrderRejectedEvent(_OrderEventContract):
    event_type: Literal["rejected"] = "rejected"
    reason: str

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> str:
        return _normalize_text(value, label="Order rejection reason", maximum_length=500)


class OrderExpiredEvent(_OrderEventContract):
    event_type: Literal["expired"] = "expired"
    reason: str | None = None

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> str | None:
        return _normalize_optional_text(value, label="Expiry reason", maximum_length=500)


OrderEvent: TypeAlias = Annotated[
    OrderAcknowledgedEvent
    | OrderFillEvent
    | OrderCancelRequestedEvent
    | OrderCancelAcknowledgedEvent
    | OrderCancelRejectedEvent
    | OrderRejectedEvent
    | OrderExpiredEvent,
    Field(discriminator="event_type"),
]

_ORDER_EVENT_CLASSES = (
    OrderAcknowledgedEvent,
    OrderFillEvent,
    OrderCancelRequestedEvent,
    OrderCancelAcknowledgedEvent,
    OrderCancelRejectedEvent,
    OrderRejectedEvent,
    OrderExpiredEvent,
)


class OrderEventReceipt(_FrozenContract):
    idempotency_key: str
    payload_hash: str = Field(pattern=_SHA256_PATTERN)
    event_type: OrderEventType
    fill_id: str | None = None
    revision: int = Field(gt=0)

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def normalize_idempotency_key(cls, value: object) -> str:
        return _normalize_text(value, label="Event receipt idempotency key")

    @field_validator("fill_id", mode="before")
    @classmethod
    def normalize_fill_id(cls, value: object) -> str | None:
        if value is None:
            return None
        return _normalize_text(value, label="Event receipt fill id")

    @model_validator(mode="after")
    def validate_economic_identity(self) -> Self:
        if self.event_type == "fill" and self.fill_id is None:
            raise ValueError("A fill receipt requires its economic fill id.")
        if self.event_type != "fill" and self.fill_id is not None:
            raise ValueError("Only a fill receipt may carry a fill id.")
        return self


class OrderState(_FrozenContract):
    """The complete deterministic projection of one order's event history."""

    schema_version: Literal[1] = 1
    order_id: str
    requested_quantity: Decimal = Field(gt=0)
    status: OrderStatus = "pending_submit"
    filled_quantity: Decimal = Field(default=Decimal(0), ge=0)
    filled_notional: Decimal = Field(default=Decimal(0), ge=0)
    average_fill_price: Decimal | None = Field(default=None, gt=0)
    status_before_cancel: ActiveOrderStatus | None = None
    terminal_reason: str | None = None
    revision: int = Field(default=0, ge=0)
    event_receipts: tuple[OrderEventReceipt, ...] = ()

    @field_validator("order_id", mode="before")
    @classmethod
    def normalize_order_id(cls, value: object) -> str:
        return _normalize_text(value, label="Order id")

    @field_validator(
        "requested_quantity",
        "filled_quantity",
        "filled_notional",
        "average_fill_price",
        mode="before",
    )
    @classmethod
    def normalize_decimals(cls, value: object, info: object) -> Decimal | None:
        if value is None:
            return None
        field_name = str(getattr(info, "field_name", "order decimal"))
        return _normalize_decimal(value, label=field_name.replace("_", " ").title())

    @field_validator("terminal_reason", mode="before")
    @classmethod
    def normalize_terminal_reason(cls, value: object) -> str | None:
        return _normalize_optional_text(value, label="Terminal reason", maximum_length=500)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.filled_quantity > self.requested_quantity:
            raise ValueError("Filled quantity cannot exceed requested quantity.")
        if self.revision != len(self.event_receipts):
            raise ValueError("Order revision must equal the number of applied event receipts.")
        receipt_keys: set[str] = set()
        fill_ids: set[str] = set()
        for expected_revision, receipt in enumerate(self.event_receipts, start=1):
            if receipt.revision != expected_revision:
                raise ValueError("Order event receipt revisions must be contiguous and ordered.")
            if receipt.idempotency_key in receipt_keys:
                raise ValueError("Order event receipt idempotency keys must be unique.")
            receipt_keys.add(receipt.idempotency_key)
            if receipt.fill_id is not None:
                if receipt.fill_id in fill_ids:
                    raise ValueError("Order fill economic identities must be unique.")
                fill_ids.add(receipt.fill_id)

        if self.filled_quantity == 0:
            if self.filled_notional != 0 or self.average_fill_price is not None:
                raise ValueError("An unfilled order cannot carry fill notional or average price.")
        else:
            if self.filled_notional <= 0 or self.average_fill_price is None:
                raise ValueError("A filled quantity requires positive notional and average price.")
            expected_average = _average_price(
                self.filled_notional,
                self.filled_quantity,
            )
            if self.average_fill_price != expected_average:
                raise ValueError("Average fill price must match cumulative fill economics.")

        if self.status == "pending_cancel":
            if self.status_before_cancel is None:
                raise ValueError("A pending cancellation must retain its prior active status.")
            if self.filled_quantity > 0 and self.status_before_cancel != "partially_filled":
                raise ValueError(
                    "A partially filled pending cancellation must retain partially_filled."
                )
            if self.filled_quantity == 0 and self.status_before_cancel == "partially_filled":
                raise ValueError("An unfilled pending cancellation cannot retain partially_filled.")
        elif self.status_before_cancel is not None:
            raise ValueError("Only a pending cancellation may retain a prior active status.")

        if self.status in {"pending_submit", "working", "rejected"} and self.filled_quantity != 0:
            raise ValueError(f"Order status {self.status} cannot carry fills.")
        if self.status == "partially_filled" and not (
            Decimal(0) < self.filled_quantity < self.requested_quantity
        ):
            raise ValueError("A partially_filled order requires an incomplete positive fill.")
        if self.status == "filled" and self.filled_quantity != self.requested_quantity:
            raise ValueError("A filled order must exactly match its requested quantity.")
        if self.status != "filled" and self.filled_quantity == self.requested_quantity:
            raise ValueError("A completely filled quantity requires filled status.")

        if self.status == "rejected":
            if self.terminal_reason is None:
                raise ValueError("A rejected order requires a terminal reason.")
        elif self.status != "expired" and self.terminal_reason is not None:
            raise ValueError("Only rejected or expired orders may carry a terminal reason.")
        return self


def create_order_state(*, order_id: str, requested_quantity: Decimal | str) -> OrderState:
    """Create the immutable revision-zero state for a newly submitted order."""

    return OrderState.model_validate(
        {
            "order_id": order_id,
            "requested_quantity": requested_quantity,
        }
    )


def _revalidate_event(event: OrderEvent) -> OrderEvent:
    for event_class in _ORDER_EVENT_CLASSES:
        if isinstance(event, event_class):
            return event_class.model_validate(event.model_dump(mode="python"))
    raise TypeError(f"Unsupported order event type: {type(event).__name__}.")


def _next_state(
    state: OrderState,
    event: OrderEvent,
    **updates: object,
) -> OrderState:
    values = state.model_dump(mode="python")
    revision = state.revision + 1
    values.update(updates)
    values["revision"] = revision
    values["event_receipts"] = (
        *state.event_receipts,
        OrderEventReceipt(
            idempotency_key=event.idempotency_key,
            payload_hash=event.payload_hash,
            event_type=event.event_type,
            fill_id=event.fill_id if isinstance(event, OrderFillEvent) else None,
            revision=revision,
        ),
    )
    return OrderState.model_validate(values)


def _require_status(
    state: OrderState,
    event: OrderEvent,
    allowed: frozenset[OrderStatus],
) -> None:
    if state.status not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise OrderTransitionError(
            f"Event {event.event_type} is not legal from {state.status}; "
            f"expected one of: {allowed_text}."
        )


def reduce_order(state: OrderState, event: OrderEvent) -> OrderState:
    """Apply one validated event, returning a new state or an exact replay no-op."""

    state = OrderState.model_validate(state.model_dump(mode="python"))
    event = _revalidate_event(event)
    if event.order_id != state.order_id:
        raise OrderInvariantError(
            f"Event order {event.order_id!r} does not match state order {state.order_id!r}."
        )

    for receipt in state.event_receipts:
        if receipt.idempotency_key != event.idempotency_key:
            continue
        if receipt.payload_hash == event.payload_hash:
            return state
        raise OrderIdempotencyConflictError(
            f"Idempotency key {event.idempotency_key!r} was reused with a different payload."
        )

    if isinstance(event, OrderFillEvent):
        for receipt in state.event_receipts:
            if receipt.fill_id != event.fill_id:
                continue
            if receipt.payload_hash == event.payload_hash:
                return state
            raise OrderIdempotencyConflictError(
                f"Fill id {event.fill_id!r} was reused with different economics."
            )

    if state.status in _TERMINAL_STATUSES:
        raise OrderTransitionError(
            f"Terminal order {state.order_id!r} in status {state.status} "
            "accepts only exact idempotent event replays."
        )

    if isinstance(event, OrderAcknowledgedEvent):
        if state.status == "pending_cancel":
            if state.status_before_cancel == "pending_submit":
                return _next_state(
                    state,
                    event,
                    status_before_cancel="working",
                )
            if state.status_before_cancel == "partially_filled":
                return _next_state(state, event)
            raise OrderTransitionError(
                "An acknowledgement during cancellation is legal only before the "
                "submission was acknowledged or after an implicit fill acknowledgement."
            )
        _require_status(state, event, frozenset({"pending_submit"}))
        return _next_state(state, event, status="working")

    if isinstance(event, OrderFillEvent):
        _require_status(
            state,
            event,
            frozenset({"working", "partially_filled", "pending_cancel"}),
        )
        filled_quantity = _exact_add(state.filled_quantity, event.fill_quantity)
        if filled_quantity > state.requested_quantity:
            raise OrderInvariantError(
                "Fill would exceed the order's requested quantity "
                f"({filled_quantity} > {state.requested_quantity})."
            )
        filled_notional = _exact_add(
            state.filled_notional,
            _exact_multiply(event.fill_quantity, event.fill_price),
        )
        if filled_quantity == state.requested_quantity:
            status: OrderStatus = "filled"
            status_before_cancel: ActiveOrderStatus | None = None
        elif state.status == "pending_cancel":
            status = "pending_cancel"
            status_before_cancel = "partially_filled"
        else:
            status = "partially_filled"
            status_before_cancel = None
        return _next_state(
            state,
            event,
            status=status,
            filled_quantity=filled_quantity,
            filled_notional=filled_notional,
            average_fill_price=_average_price(filled_notional, filled_quantity),
            status_before_cancel=status_before_cancel,
        )

    if isinstance(event, OrderCancelRequestedEvent):
        _require_status(
            state,
            event,
            frozenset({"pending_submit", "working", "partially_filled"}),
        )
        return _next_state(
            state,
            event,
            status="pending_cancel",
            status_before_cancel=cast(ActiveOrderStatus, state.status),
        )

    if isinstance(event, OrderCancelAcknowledgedEvent):
        _require_status(state, event, frozenset({"pending_cancel"}))
        return _next_state(
            state,
            event,
            status="cancelled",
            status_before_cancel=None,
        )

    if isinstance(event, OrderCancelRejectedEvent):
        _require_status(state, event, frozenset({"pending_cancel"}))
        restored_status = state.status_before_cancel
        if restored_status is None:
            raise OrderInvariantError("Pending cancellation lost its prior active status.")
        return _next_state(
            state,
            event,
            status=restored_status,
            status_before_cancel=None,
        )

    if isinstance(event, OrderRejectedEvent):
        if state.status == "pending_cancel":
            if state.status_before_cancel != "pending_submit" or state.filled_quantity != 0:
                raise OrderTransitionError(
                    "A submission rejection during cancellation is legal only for an "
                    "unacknowledged, unfilled order."
                )
            return _next_state(
                state,
                event,
                status="rejected",
                status_before_cancel=None,
                terminal_reason=event.reason,
            )
        _require_status(state, event, frozenset({"pending_submit"}))
        return _next_state(
            state,
            event,
            status="rejected",
            terminal_reason=event.reason,
        )

    if isinstance(event, OrderExpiredEvent):
        _require_status(
            state,
            event,
            frozenset({"working", "partially_filled", "pending_cancel"}),
        )
        return _next_state(
            state,
            event,
            status="expired",
            status_before_cancel=None,
            terminal_reason=event.reason,
        )

    raise TypeError(f"Unsupported order event type: {type(event).__name__}.")


def apply_order_event(state: OrderState, event: OrderEvent) -> OrderState:
    """Readable alias for the pure order reducer."""

    return reduce_order(state, event)


def replay_order_events(
    initial_state: OrderState,
    events: Iterable[OrderEvent],
) -> OrderState:
    """Reduce an ordered event stream from a caller-supplied initial state."""

    state = initial_state
    for event in events:
        state = reduce_order(state, event)
    return state
