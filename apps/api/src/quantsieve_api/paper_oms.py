from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Literal, Self, TypeAlias, TypeVar, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from quantsieve_engine.order_events import (
    OrderAcknowledgedEvent,
    OrderCancelAcknowledgedEvent,
    OrderCancelRejectedEvent,
    OrderCancelRequestedEvent,
    OrderEvent,
    OrderExpiredEvent,
    OrderFillEvent,
    OrderIdempotencyConflictError,
    OrderInvariantError,
    OrderRejectedEvent,
    OrderState,
    OrderTransitionError,
    create_order_state,
    reduce_order,
)
from quantsieve_engine.paper_ledger import (
    InsufficientPaperCashError,
    InsufficientPaperPositionError,
    OpeningBalanceEvent,
    PaperFillEvent,
    PaperLedgerConflictError,
    PaperLedgerReconciliationError,
    PaperLedgerRevisionError,
    PaperLedgerState,
    apply_paper_fill,
    build_opening_balance_event,
    build_paper_fill_event,
    open_paper_ledger,
    reconcile_paper_ledger,
)
from quantsieve_engine.risk import (
    KillSwitchSnapshot,
    OrderPriceEvidence,
    OrderRiskIntent,
    OrderRiskRequest,
    OrderRiskRuleSet,
    OrderRiskState,
    RiskDecision,
    build_kill_switch_snapshot,
    build_order_risk_request,
    canonical_payload_hash,
    evaluate_order_risk,
)
from quantsieve_providers import BinanceSpotTradingRules, ExecutionQuote

PaperSide: TypeAlias = Literal["buy", "sell"]
PaperOrderEventType: TypeAlias = Literal[
    "acknowledged",
    "cancel_requested",
    "cancel_acknowledged",
    "cancel_rejected",
    "rejected",
    "expired",
]

_SCHEMA_VERSION = 3
_SCHEMA_FINGERPRINT = "d3d730295b85beb9e60c538ac564d2bf098fabc54e69b0c370dcf44ec82ada84"
_MAXIMUM_CLIENT_CLOCK_LEAD = timedelta(seconds=5)
_HASH_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_MAX_LENGTH = 200
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._:/-]{0,99}$", flags=re.ASCII)
_EVENT_ADAPTER: TypeAdapter[OrderEvent] = TypeAdapter(OrderEvent)
_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    strict=True,
    revalidate_instances="always",
    allow_inf_nan=False,
)


class PaperOmsError(RuntimeError):
    """Base error for the durable paper OMS."""


class PaperOmsNotFoundError(PaperOmsError):
    """A requested paper account or order does not exist."""


class PaperOmsConflictError(PaperOmsError):
    """An idempotency or globally unique economic identity conflicts."""


class PaperOmsRevisionError(PaperOmsError):
    """A command targets an order or account revision that is no longer current."""


class PaperOmsIntegrityError(PaperOmsError):
    """Stored schema, event history, or materialized state failed verification."""


class PaperOmsTransitionError(PaperOmsError):
    """An otherwise valid command is illegal from the current order state."""


class PaperOmsInsufficientCashError(PaperOmsError):
    """A modeled buy cannot be funded by available paper cash."""


class PaperOmsInsufficientPositionError(PaperOmsError):
    """A modeled sell exceeds the available long paper position."""


class PaperOmsBusyError(PaperOmsError):
    """SQLite could not acquire its immediate writer lock before the timeout."""


class PaperOmsRiskUnavailableError(PaperOmsError):
    """Server-owned pre-trade risk evidence could not be obtained safely."""


class PaperOmsExecutionUnavailableError(PaperOmsError):
    """Server-owned simulated-execution evidence could not be obtained safely."""


def _normalize_identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > _IDENTIFIER_MAX_LENGTH:
        raise ValueError(f"{label} must contain between 1 and {_IDENTIFIER_MAX_LENGTH} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{label} must not contain control characters.")
    return normalized


def _normalize_optional_reason(value: object) -> str | None:
    if value is None:
        return None
    return _normalize_identifier(value, label="Event reason")


def _normalize_symbol(value: object) -> str:
    symbol = _normalize_identifier(value, label="Order symbol").upper()
    if _SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise ValueError("Order symbol contains unsupported characters.")
    return symbol


def _normalize_decimal(value: object, *, label: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError(f"{label} must be an exact finite decimal.")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, str)):
        try:
            result = Decimal(str(value).strip())
        except Exception as error:
            raise ValueError(f"{label} must be an exact finite decimal.") from error
    else:
        raise ValueError(f"{label} must be an exact finite decimal.")
    if not result.is_finite():
        raise ValueError(f"{label} must be an exact finite decimal.")
    if result == 0:
        return Decimal(0)
    decimal_tuple = result.as_tuple()
    digits = list(decimal_tuple.digits)
    exponent = cast(int, decimal_tuple.exponent)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return Decimal((decimal_tuple.sign, tuple(digits), exponent))


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
        raise PaperOmsIntegrityError("Exact OMS arithmetic produced a repeating decimal.")
    scale = max(twos, fives)
    adjusted = abs(numerator) * (5 ** (scale - fives)) * (2 ** (scale - twos))
    digits = tuple(int(character) for character in str(adjusted)) if adjusted else (0,)
    return _normalize_decimal(
        Decimal((1 if numerator < 0 else 0, digits, -scale)),
        label="Exact OMS arithmetic",
    )


def _exact_sum(*values: Decimal) -> Decimal:
    return _terminating_decimal(sum((Fraction(value) for value in values), Fraction(0)))


def _exact_difference(left: Decimal, right: Decimal) -> Decimal:
    return _terminating_decimal(Fraction(left) - Fraction(right))


def _exact_product(*values: Decimal) -> Decimal:
    result = Fraction(1)
    for value in values:
        result *= Fraction(value)
    return _terminating_decimal(result)


def _normalize_utc(value: object, *, label: str) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{label} must be an ISO-8601 timestamp.") from error
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError(f"{label} must be a timezone-aware timestamp.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware.")
    return parsed.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _CommandContract(BaseModel):
    model_config = _MODEL_CONFIG

    schema_version: Literal[1] = 1
    command_namespace: str
    idempotency_key: str
    account_id: str
    occurred_at: datetime

    @field_validator(
        "command_namespace",
        "idempotency_key",
        "account_id",
        mode="before",
    )
    @classmethod
    def normalize_identifier(cls, value: object, info: object) -> str:
        field_name = str(getattr(info, "field_name", "command identity"))
        return _normalize_identifier(value, label=field_name.replace("_", " ").title())

    @field_validator("occurred_at", mode="before")
    @classmethod
    def normalize_occurred_at(cls, value: object) -> datetime:
        return _normalize_utc(value, label="Command occurrence time")


class CreatePaperAccountCommand(_CommandContract):
    """Create one exact-cash paper account and its opening journal."""

    currency: str
    initial_cash: Decimal

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> str:
        currency = _normalize_identifier(value, label="Paper account currency").upper()
        if len(currency) > 16 or not currency.replace("_", "").isalnum():
            raise ValueError("Paper account currency is invalid.")
        return currency

    @field_validator("initial_cash", mode="before")
    @classmethod
    def normalize_initial_cash(cls, value: object) -> Decimal:
        cash = _normalize_decimal(value, label="Initial cash")
        if cash <= 0:
            raise ValueError("Initial cash must be positive.")
        return cash


class SubmitPaperOrderCommand(_CommandContract):
    """Register one long-only spot order before venue lifecycle events arrive."""

    order_id: str
    symbol: str
    side: PaperSide
    execution_source: str
    quantity: Decimal

    @field_validator("order_id", "execution_source", mode="before")
    @classmethod
    def normalize_order_identifier(cls, value: object, info: object) -> str:
        field_name = str(getattr(info, "field_name", "order identity"))
        return _normalize_identifier(value, label=field_name.replace("_", " ").title())

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_order_symbol(cls, value: object) -> str:
        return _normalize_symbol(value)

    @field_validator("quantity", mode="before")
    @classmethod
    def normalize_quantity(cls, value: object) -> Decimal:
        quantity = _normalize_decimal(value, label="Order quantity")
        if quantity <= 0:
            raise ValueError("Order quantity must be positive.")
        return quantity


class PaperOmsKillSwitchObservation(BaseModel):
    """One freshly read server-owned kill-switch observation."""

    model_config = _MODEL_CONFIG

    snapshot: KillSwitchSnapshot
    observed_at: datetime
    available_at: datetime

    @field_validator("snapshot", mode="before")
    @classmethod
    def revalidate_snapshot(cls, value: object) -> KillSwitchSnapshot:
        return KillSwitchSnapshot.model_validate(
            value.model_dump(mode="python") if isinstance(value, KillSwitchSnapshot) else value
        )

    @field_validator("observed_at", "available_at", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: object, info: object) -> datetime:
        field_name = str(getattr(info, "field_name", "kill-switch time"))
        return _normalize_utc(value, label=field_name.replace("_", " ").title())

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if self.available_at < self.observed_at:
            raise ValueError("Kill-switch availability cannot precede its observation.")
        return self


class PaperOmsOrderMarketEvidence(BaseModel):
    """Trusted Binance rules and quote bound to one order-risk price input."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1] = 1
    contract: Literal["quantsieve.paper-oms.order-market-evidence.v1"] = (
        "quantsieve.paper-oms.order-market-evidence.v1"
    )
    side: PaperSide
    trading_rules: BinanceSpotTradingRules
    quote: ExecutionQuote
    price_evidence: OrderPriceEvidence
    rules_snapshot_hash: str = Field(pattern=_HASH_PATTERN)
    quote_snapshot_hash: str = Field(pattern=_HASH_PATTERN)
    evidence_hash: str = Field(pattern=_HASH_PATTERN)

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        rules = self.trading_rules
        quote = self.quote
        price = self.price_evidence
        expected_reference = quote.ask_price if self.side == "buy" else quote.bid_price
        expected_snapshot_hash = canonical_payload_hash(
            {
                "contract": "quantsieve.paper-oms.binance-market-snapshot.v1",
                "rules": rules,
                "quote": quote,
            }
        )
        if (
            rules.symbol != quote.symbol
            or price.symbol != rules.symbol
            or price.quote_currency != rules.quote_asset
            or price.reference_price != expected_reference
            or price.observed_at != quote.observed_at
            or price.available_at != quote.observed_at
            or price.snapshot_hash != expected_snapshot_hash
            or self.rules_snapshot_hash != canonical_payload_hash(rules)
            or self.quote_snapshot_hash != canonical_payload_hash(quote)
        ):
            raise ValueError("Order market evidence is not bound to its rules, quote, and side.")
        expected_hash = canonical_payload_hash(
            self.model_dump(mode="python", exclude={"evidence_hash"})
        )
        if self.evidence_hash != expected_hash:
            raise ValueError("Order market evidence hash does not match its payload.")
        return self


class PaperOmsFillExecutionEvidence(BaseModel):
    """Server-constructed simulated fill economics and source evidence."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1] = 1
    contract: Literal["quantsieve.paper-oms.simulated-fill-evidence.v1"] = (
        "quantsieve.paper-oms.simulated-fill-evidence.v1"
    )
    account_id: str
    order_id: str
    symbol: str
    side: PaperSide
    quantity: Decimal
    execution_source: str
    external_fill_id: str
    reference_price: Decimal
    fill_price: Decimal
    fee_rate: Decimal
    fee: Decimal
    observed_at: datetime
    available_at: datetime
    approval_evidence_hash: str = Field(pattern=_HASH_PATTERN)
    trading_rules: BinanceSpotTradingRules
    quote: ExecutionQuote
    rules_snapshot_hash: str = Field(pattern=_HASH_PATTERN)
    quote_snapshot_hash: str = Field(pattern=_HASH_PATTERN)
    evidence_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator(
        "account_id",
        "order_id",
        "execution_source",
        "external_fill_id",
        mode="before",
    )
    @classmethod
    def normalize_identifier(cls, value: object, info: object) -> str:
        field_name = str(getattr(info, "field_name", "execution identity"))
        return _normalize_identifier(value, label=field_name.replace("_", " ").title())

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _normalize_symbol(value)

    @field_validator(
        "quantity",
        "reference_price",
        "fill_price",
        "fee_rate",
        "fee",
        mode="before",
    )
    @classmethod
    def normalize_decimal(cls, value: object, info: object) -> Decimal:
        field_name = str(getattr(info, "field_name", "execution amount"))
        return _normalize_decimal(value, label=field_name.replace("_", " ").title())

    @field_validator("observed_at", "available_at", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: object, info: object) -> datetime:
        field_name = str(getattr(info, "field_name", "execution time"))
        return _normalize_utc(value, label=field_name.replace("_", " ").title())

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        rules = self.trading_rules
        quote = self.quote
        expected_price = quote.ask_price if self.side == "buy" else quote.bid_price
        if (
            self.quantity <= 0
            or self.reference_price <= 0
            or self.fill_price <= 0
            or self.fee_rate < 0
            or self.fee < 0
            or self.available_at < self.observed_at
            or rules.symbol != self.symbol
            or quote.symbol != self.symbol
            or self.reference_price != expected_price
            or self.observed_at != quote.observed_at
            or self.available_at != quote.observed_at
            or self.fee
            != _exact_product(
                self.quantity,
                self.fill_price,
                self.fee_rate,
            )
            or self.rules_snapshot_hash != canonical_payload_hash(rules)
            or self.quote_snapshot_hash != canonical_payload_hash(quote)
        ):
            raise ValueError("Simulated fill evidence is not bound to exact provider economics.")
        expected_hash = canonical_payload_hash(
            self.model_dump(mode="python", exclude={"evidence_hash"})
        )
        if self.evidence_hash != expected_hash:
            raise ValueError("Simulated fill evidence hash does not match its payload.")
        return self


class PaperOmsActiveReservationEvidence(BaseModel):
    """One active order included in a historical reservation-state prefix."""

    model_config = _MODEL_CONFIG

    order_id: str
    symbol: str
    side: PaperSide
    order_revision: int = Field(ge=0, strict=True)
    status: str
    remaining_quantity: Decimal
    risk_decision_hash: str = Field(pattern=_HASH_PATTERN)
    risk_price: Decimal

    @field_validator("order_id", "status", mode="before")
    @classmethod
    def normalize_identifier(cls, value: object, info: object) -> str:
        field_name = str(getattr(info, "field_name", "reservation identity"))
        return _normalize_identifier(value, label=field_name.replace("_", " ").title())

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _normalize_symbol(value)

    @field_validator("remaining_quantity", "risk_price", mode="before")
    @classmethod
    def normalize_decimal(cls, value: object, info: object) -> Decimal:
        field_name = str(getattr(info, "field_name", "reservation amount"))
        result = _normalize_decimal(value, label=field_name.replace("_", " ").title())
        if result <= 0:
            raise ValueError(f"{field_name} must be positive.")
        return result


class PaperOmsReservationStateEvidence(BaseModel):
    """Replayable active-order prefix used by a pre-trade decision."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1] = 1
    account_id_hash: str = Field(pattern=_HASH_PATTERN)
    target_symbol: str
    event_horizon_id: int = Field(ge=0, strict=True)
    prior_evaluation_horizon_id: int = Field(ge=0, strict=True)
    active_orders: tuple[PaperOmsActiveReservationEvidence, ...]
    reserved_buy_cash: Decimal
    reserved_buy_quantity: Decimal
    reserved_sell_quantity: Decimal
    active_order_count: int = Field(ge=0, strict=True)
    evidence_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator("target_symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _normalize_symbol(value)

    @field_validator(
        "reserved_buy_cash",
        "reserved_buy_quantity",
        "reserved_sell_quantity",
        mode="before",
    )
    @classmethod
    def normalize_decimal(cls, value: object, info: object) -> Decimal:
        field_name = str(getattr(info, "field_name", "reservation total"))
        result = _normalize_decimal(value, label=field_name.replace("_", " ").title())
        if result < 0:
            raise ValueError(f"{field_name} cannot be negative.")
        return result

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.active_order_count != len(self.active_orders):
            raise ValueError("Reservation active-order count differs from its order evidence.")
        if tuple(order.order_id for order in self.active_orders) != tuple(
            sorted(order.order_id for order in self.active_orders)
        ):
            raise ValueError("Reservation active orders must be sorted by order id.")
        expected_hash = canonical_payload_hash(
            self.model_dump(mode="python", exclude={"evidence_hash"})
        )
        if self.evidence_hash != expected_hash:
            raise ValueError("Reservation-state evidence hash does not match its payload.")
        return self


class ServerOwnedPaperKillSwitch:
    """SQLite-backed single authority read freshly for every submit and fill.

    A new database starts engaged at revision one.  Clearing it is an explicit
    in-process operator action; restarting the API never clears the authority.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
        source: str = "quantsieve.paper-oms.server-kill-switch",
        busy_timeout_ms: int = 30_000,
    ) -> None:
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms <= 0
        ):
            raise ValueError("busy_timeout_ms must be a positive integer.")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._source = _normalize_identifier(
            source,
            label="Kill-switch source",
        )
        self._busy_timeout_ms = busy_timeout_ms
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self._busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _state_payload(
        *,
        status: Literal["clear", "engaged"],
        revision: int,
        reason_code: str | None,
        activated_at: datetime | None,
        source: str,
        updated_at: datetime,
    ) -> str:
        return json.dumps(
            {
                "activated_at": (activated_at.isoformat() if activated_at is not None else None),
                "reason_code": reason_code,
                "revision": revision,
                "source": source,
                "status": status,
                "updated_at": updated_at.isoformat(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def _initialize(self) -> None:
        now = _normalize_utc(self._clock(), label="Kill-switch initialization time")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                authority_table_existed = (
                    connection.execute(
                        """
                        SELECT 1
                        FROM sqlite_master
                        WHERE type = 'table' AND name = 'paper_oms_kill_switch'
                        """
                    ).fetchone()
                    is not None
                )
                meta_table_exists = (
                    connection.execute(
                        """
                        SELECT 1
                        FROM sqlite_master
                        WHERE type = 'table' AND name = 'paper_oms_meta'
                        """
                    ).fetchone()
                    is not None
                )
                if not authority_table_existed and meta_table_exists:
                    meta = connection.execute(
                        "SELECT schema_version FROM paper_oms_meta WHERE singleton = 1"
                    ).fetchone()
                    if meta is not None and meta[0] == _SCHEMA_VERSION:
                        raise PaperOmsIntegrityError(
                            "Paper OMS kill-switch table is missing from a v3 database."
                        )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS paper_oms_kill_switch (
                        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                        status TEXT NOT NULL CHECK(status IN ('clear', 'engaged')),
                        revision INTEGER NOT NULL CHECK(revision >= 1),
                        reason_code TEXT,
                        activated_at TEXT,
                        source TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        state_payload TEXT NOT NULL,
                        state_hash TEXT NOT NULL CHECK(
                            length(state_hash) = 64
                            AND state_hash NOT GLOB '*[^0-9a-f]*'
                        ),
                        CHECK(
                            (
                                status = 'engaged'
                                AND reason_code IS NOT NULL
                                AND activated_at IS NOT NULL
                            )
                            OR
                            (
                                status = 'clear'
                                AND reason_code IS NULL
                                AND activated_at IS NULL
                            )
                        )
                    ) STRICT
                    """
                )
                if not authority_table_existed:
                    reason = "UNINITIALIZED_FAIL_CLOSED"
                    payload = self._state_payload(
                        status="engaged",
                        revision=1,
                        reason_code=reason,
                        activated_at=now,
                        source=self._source,
                        updated_at=now,
                    )
                    connection.execute(
                        """
                        INSERT INTO paper_oms_kill_switch (
                            singleton, status, revision, reason_code, activated_at,
                            source, updated_at, state_payload, state_hash
                        ) VALUES (1, 'engaged', 1, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            reason,
                            now.isoformat(),
                            self._source,
                            now.isoformat(),
                            payload,
                            _payload_hash(payload),
                        ),
                    )
                rows = connection.execute("SELECT * FROM paper_oms_kill_switch").fetchall()
                if len(rows) != 1:
                    raise PaperOmsIntegrityError(
                        "Paper OMS kill-switch authority is missing or ambiguous."
                    )
                self._observation_from_row(rows[0], observed_at=now)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _observation_from_row(
        self,
        row: sqlite3.Row,
        *,
        observed_at: datetime,
    ) -> PaperOmsKillSwitchObservation:
        if _stored_integer(row, "singleton") != 1:
            raise PaperOmsIntegrityError("Kill-switch singleton identity is invalid.")
        status = _stored_text(row, "status")
        if status not in {"clear", "engaged"}:
            raise PaperOmsIntegrityError("Kill-switch status is invalid.")
        revision = _stored_integer(row, "revision")
        if revision < 1:
            raise PaperOmsIntegrityError("Kill-switch revision is invalid.")
        reason = _stored_optional_text(row, "reason_code")
        activated = (
            _stored_timestamp(row, "activated_at") if row["activated_at"] is not None else None
        )
        source = _stored_text(row, "source")
        updated_at = _stored_timestamp(row, "updated_at")
        payload = self._state_payload(
            status=cast(Literal["clear", "engaged"], status),
            revision=revision,
            reason_code=reason,
            activated_at=activated,
            source=source,
            updated_at=updated_at,
        )
        if (
            payload != _stored_text(row, "state_payload")
            or _payload_hash(payload) != _stored_text(row, "state_hash")
            or source != self._source
        ):
            raise PaperOmsIntegrityError("Kill-switch state evidence is invalid.")
        snapshot = build_kill_switch_snapshot(
            status=cast(Literal["clear", "engaged"], status),
            scope="global",
            revision=revision,
            reason_code=reason,
            activated_at=activated,
            source=source,
        )
        return PaperOmsKillSwitchObservation(
            snapshot=snapshot,
            observed_at=observed_at,
            available_at=observed_at,
        )

    def _transition(
        self,
        *,
        status: Literal["clear", "engaged"],
        reason_code: str | None,
        expected_revision: int | None = None,
    ) -> PaperOmsKillSwitchObservation:
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer.")
        now = _normalize_utc(self._clock(), label="Kill-switch transition time")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM paper_oms_kill_switch WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    raise PaperOmsIntegrityError("Kill-switch authority row is missing.")
                current = self._observation_from_row(row, observed_at=now)
                if expected_revision is not None and current.snapshot.revision != expected_revision:
                    raise PaperOmsRevisionError(
                        "Kill-switch authority does not match the expected revision."
                    )
                if (
                    current.snapshot.status == status
                    and current.snapshot.reason_code == reason_code
                ):
                    connection.commit()
                    return current
                revision = current.snapshot.revision + 1
                activated_at = now if status == "engaged" else None
                payload = self._state_payload(
                    status=status,
                    revision=revision,
                    reason_code=reason_code,
                    activated_at=activated_at,
                    source=self._source,
                    updated_at=now,
                )
                cursor = connection.execute(
                    """
                    UPDATE paper_oms_kill_switch
                    SET status = ?, revision = ?, reason_code = ?,
                        activated_at = ?, updated_at = ?,
                        state_payload = ?, state_hash = ?
                    WHERE singleton = 1 AND revision = ?
                    """,
                    (
                        status,
                        revision,
                        reason_code,
                        activated_at.isoformat() if activated_at is not None else None,
                        now.isoformat(),
                        payload,
                        _payload_hash(payload),
                        current.snapshot.revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PaperOmsRevisionError(
                        "Kill-switch revision changed during operator transition."
                    )
                updated = connection.execute(
                    "SELECT * FROM paper_oms_kill_switch WHERE singleton = 1"
                ).fetchone()
                if updated is None:  # pragma: no cover - same transaction invariant
                    raise PaperOmsIntegrityError("Kill-switch authority row disappeared.")
                observation = self._observation_from_row(updated, observed_at=now)
                connection.commit()
                return observation
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def engage(self, *, reason_code: str) -> PaperOmsKillSwitchObservation:
        reason = _normalize_identifier(reason_code, label="Kill-switch reason")
        return self._transition(status="engaged", reason_code=reason)

    def clear(self) -> PaperOmsKillSwitchObservation:
        """Explicit internal operator action; intentionally not exposed as an API."""

        return self._transition(status="clear", reason_code=None)

    def engage_if_revision(
        self,
        *,
        reason_code: str,
        expected_revision: int,
    ) -> PaperOmsKillSwitchObservation:
        """Atomically engage only when the durable authority revision matches."""

        reason = _normalize_identifier(reason_code, label="Kill-switch reason")
        return self._transition(
            status="engaged",
            reason_code=reason,
            expected_revision=expected_revision,
        )

    def clear_if_revision(
        self,
        *,
        expected_revision: int,
    ) -> PaperOmsKillSwitchObservation:
        """Atomically clear only when the durable authority revision matches."""

        return self._transition(
            status="clear",
            reason_code=None,
            expected_revision=expected_revision,
        )

    def read(self) -> PaperOmsKillSwitchObservation:
        with self._lock:
            observed_at = _normalize_utc(
                self._clock(),
                label="Kill-switch observation time",
            )
            connection = self._connect()
            try:
                rows = connection.execute("SELECT * FROM paper_oms_kill_switch").fetchall()
                if len(rows) != 1:
                    raise PaperOmsIntegrityError("Kill-switch authority is missing or ambiguous.")
                return self._observation_from_row(rows[0], observed_at=observed_at)
            finally:
                connection.close()


class RecordPaperOrderEventCommand(_CommandContract):
    """Apply one non-fill lifecycle event to an exact expected order revision."""

    order_id: str
    expected_order_revision: int = Field(ge=0, strict=True)
    event_type: PaperOrderEventType
    reason: str | None = None

    @field_validator("order_id", mode="before")
    @classmethod
    def normalize_order_id(cls, value: object) -> str:
        return _normalize_identifier(value, label="Order id")

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> str | None:
        return _normalize_optional_reason(value)

    @model_validator(mode="after")
    def validate_reason_contract(self) -> Self:
        if self.event_type in {"cancel_rejected", "rejected"} and self.reason is None:
            raise ValueError(f"Event {self.event_type} requires a reason.")
        if (
            self.event_type in {"acknowledged", "cancel_requested", "cancel_acknowledged"}
            and self.reason is not None
        ):
            raise ValueError(f"Event {self.event_type} does not accept a reason.")
        return self


class RecordPaperFillCommand(_CommandContract):
    """Atomically apply one namespaced execution to an order and its account."""

    order_id: str
    expected_order_revision: int = Field(ge=0, strict=True)
    expected_account_revision: int = Field(ge=1, strict=True)
    execution_source: str
    external_fill_id: str
    quantity: Decimal
    reference_price: Decimal
    fill_price: Decimal
    fee: Decimal = Decimal(0)

    @field_validator(
        "order_id",
        "execution_source",
        "external_fill_id",
        mode="before",
    )
    @classmethod
    def normalize_fill_identifier(cls, value: object, info: object) -> str:
        field_name = str(getattr(info, "field_name", "fill identity"))
        return _normalize_identifier(value, label=field_name.replace("_", " ").title())

    @field_validator(
        "quantity",
        "reference_price",
        "fill_price",
        "fee",
        mode="before",
    )
    @classmethod
    def normalize_fill_decimal(cls, value: object, info: object) -> Decimal:
        field_name = str(getattr(info, "field_name", "fill amount"))
        return _normalize_decimal(value, label=field_name.replace("_", " ").title())

    @model_validator(mode="after")
    def validate_fill_economics(self) -> Self:
        if self.quantity <= 0:
            raise ValueError("Fill quantity must be positive.")
        if self.reference_price <= 0 or self.fill_price <= 0:
            raise ValueError("Fill prices must be positive.")
        if self.fee < 0:
            raise ValueError("Fill fee cannot be negative.")
        return self


class PaperAccountRecord(BaseModel):
    """Strictly reconstructed account state returned by the store."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1] = 1
    account_id: str
    currency: str
    ledger: PaperLedgerState
    created_at: datetime
    updated_at: datetime

    @field_validator("account_id", mode="before")
    @classmethod
    def normalize_account_id(cls, value: object) -> str:
        return _normalize_identifier(value, label="Paper account id")

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> str:
        return _normalize_identifier(value, label="Paper account currency").upper()

    @field_validator("ledger", mode="before")
    @classmethod
    def revalidate_ledger(cls, value: object) -> PaperLedgerState:
        return PaperLedgerState.model_validate(
            value.model_dump(mode="python") if isinstance(value, PaperLedgerState) else value
        )

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: object, info: object) -> datetime:
        field_name = str(getattr(info, "field_name", "record time"))
        return _normalize_utc(value, label=field_name.replace("_", " ").title())

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if self.ledger.account_id != self.account_id:
            raise ValueError("Account record and ledger identities differ.")
        if self.ledger.currency != self.currency:
            raise ValueError("Account record and ledger currencies differ.")
        if self.updated_at < self.created_at:
            raise ValueError("Account update time cannot precede creation.")
        reconcile_paper_ledger(self.ledger)
        return self


class PaperOrderRiskEvaluationRecord(BaseModel):
    """Durable, replayable pre-trade decision for one submit command."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1] = 1
    evaluation_id: int = Field(ge=1, strict=True)
    command: SubmitPaperOrderCommand
    outcome: Literal["allow", "reject"]
    request: OrderRiskRequest
    decision: RiskDecision
    market_evidence: PaperOmsOrderMarketEvidence | None = None
    reservation_state: PaperOmsReservationStateEvidence | None = None
    recorded_at: datetime

    @field_validator("command", mode="before")
    @classmethod
    def revalidate_command(cls, value: object) -> SubmitPaperOrderCommand:
        return SubmitPaperOrderCommand.model_validate(
            value.model_dump(mode="python") if isinstance(value, SubmitPaperOrderCommand) else value
        )

    @field_validator("request", mode="before")
    @classmethod
    def revalidate_request(cls, value: object) -> OrderRiskRequest:
        return OrderRiskRequest.model_validate(
            value.model_dump(mode="python") if isinstance(value, OrderRiskRequest) else value
        )

    @field_validator("decision", mode="before")
    @classmethod
    def revalidate_decision(cls, value: object) -> RiskDecision:
        return RiskDecision.model_validate(
            value.model_dump(mode="python") if isinstance(value, RiskDecision) else value
        )

    @field_validator("market_evidence", mode="before")
    @classmethod
    def revalidate_market_evidence(
        cls,
        value: object,
    ) -> PaperOmsOrderMarketEvidence | None:
        if value is None:
            return None
        return PaperOmsOrderMarketEvidence.model_validate(
            value.model_dump(mode="python")
            if isinstance(value, PaperOmsOrderMarketEvidence)
            else value
        )

    @field_validator("reservation_state", mode="before")
    @classmethod
    def revalidate_reservation_state(
        cls,
        value: object,
    ) -> PaperOmsReservationStateEvidence | None:
        if value is None:
            return None
        return PaperOmsReservationStateEvidence.model_validate(
            value.model_dump(mode="python")
            if isinstance(value, PaperOmsReservationStateEvidence)
            else value
        )

    @field_validator("recorded_at", mode="before")
    @classmethod
    def normalize_recorded_at(cls, value: object) -> datetime:
        return _normalize_utc(value, label="Risk evaluation record time")

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.decision.request != self.request:
            raise ValueError("Risk decision does not embed the stored request.")
        if self.market_evidence is not None and (
            self.market_evidence.price_evidence != self.request.price_evidence
            or self.market_evidence.side != self.command.side
        ):
            raise ValueError("Risk request differs from its trusted market evidence.")
        if self.reservation_state is not None and (
            self.reservation_state.account_id_hash != self.request.state.account_id_hash
            or self.reservation_state.target_symbol != self.command.symbol
            or self.reservation_state.reserved_buy_cash != self.request.state.reserved_buy_cash
            or self.reservation_state.reserved_buy_quantity
            != self.request.state.reserved_buy_quantity
            or self.reservation_state.reserved_sell_quantity
            != self.request.state.reserved_sell_quantity
            or self.reservation_state.active_order_count != self.request.state.active_order_count
        ):
            raise ValueError("Risk request differs from its reservation-state evidence.")
        expected_outcome = "allow" if self.decision.decision == "allow" else "reject"
        if self.outcome != expected_outcome:
            raise ValueError("Risk evaluation outcome differs from its decision.")
        intent = self.request.intent
        if (
            intent.order_id != self.command.order_id
            or intent.symbol != self.command.symbol
            or intent.side != self.command.side
            or intent.quantity != self.command.quantity
            or intent.order_type != "market"
            or intent.limit_price is not None
        ):
            raise ValueError("Risk intent differs from its submit command.")
        if self.request.evaluated_at < self.command.occurred_at:
            raise ValueError("Risk evaluation cannot precede order occurrence.")
        if self.recorded_at < self.request.evaluated_at:
            raise ValueError("Risk record cannot precede its evaluation.")
        return self


class PaperOmsRiskRejectedError(PaperOmsError):
    """A complete server-owned risk evaluation rejected order creation."""

    def __init__(
        self,
        evaluation: PaperOrderRiskEvaluationRecord,
        *,
        idempotent_replay: bool,
    ) -> None:
        self.evaluation = evaluation
        self.idempotent_replay = idempotent_replay
        codes = ", ".join(finding.code for finding in evaluation.decision.findings)
        super().__init__(f"Order risk rejected the submit command ({codes}).")


class PaperOrderRecord(BaseModel):
    """Strictly replayed order state plus execution-routing metadata."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1] = 1
    account_id: str
    order_id: str
    symbol: str
    side: PaperSide
    execution_source: str
    state: OrderState
    risk_evaluation: PaperOrderRiskEvaluationRecord | None = None
    submitted_at: datetime
    committed_at: datetime
    updated_at: datetime
    updated_committed_at: datetime

    @field_validator(
        "account_id",
        "order_id",
        "execution_source",
        mode="before",
    )
    @classmethod
    def normalize_identifier(cls, value: object, info: object) -> str:
        field_name = str(getattr(info, "field_name", "order record identity"))
        return _normalize_identifier(value, label=field_name.replace("_", " ").title())

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _normalize_symbol(value)

    @field_validator("state", mode="before")
    @classmethod
    def revalidate_state(cls, value: object) -> OrderState:
        return OrderState.model_validate(
            value.model_dump(mode="python") if isinstance(value, OrderState) else value
        )

    @field_validator("risk_evaluation", mode="before")
    @classmethod
    def revalidate_risk_evaluation(
        cls,
        value: object,
    ) -> PaperOrderRiskEvaluationRecord | None:
        if value is None:
            return None
        return PaperOrderRiskEvaluationRecord.model_validate(
            value.model_dump(mode="python")
            if isinstance(value, PaperOrderRiskEvaluationRecord)
            else value
        )

    @field_validator(
        "submitted_at",
        "committed_at",
        "updated_at",
        "updated_committed_at",
        mode="before",
    )
    @classmethod
    def normalize_timestamp(cls, value: object, info: object) -> datetime:
        field_name = str(getattr(info, "field_name", "order record time"))
        return _normalize_utc(value, label=field_name.replace("_", " ").title())

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if self.state.order_id != self.order_id:
            raise ValueError("Order record and state identities differ.")
        if self.risk_evaluation is not None and (
            self.risk_evaluation.outcome != "allow"
            or self.risk_evaluation.command.account_id != self.account_id
            or self.risk_evaluation.command.order_id != self.order_id
            or self.risk_evaluation.command.symbol != self.symbol
            or self.risk_evaluation.command.side != self.side
        ):
            raise ValueError("Order record differs from its allow risk evaluation.")
        if self.updated_at < self.submitted_at:
            raise ValueError("Order update time cannot precede submission.")
        if self.updated_committed_at < self.committed_at:
            raise ValueError("Order server update cannot precede server submission.")
        return self


class PaperOrderEventRecord(BaseModel):
    """One durable event-log row and any atomically coupled ledger fill."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1] = 1
    event_id: int = Field(ge=1, strict=True)
    command_namespace: str
    idempotency_key: str
    account_id: str
    order_id: str
    order_revision: int = Field(ge=1, strict=True)
    account_revision: int | None = Field(default=None, ge=2, strict=True)
    order_event: OrderEvent
    ledger_fill: PaperFillEvent | None = None
    execution_source: str | None = None
    external_fill_id: str | None = None
    fill_execution_evidence: PaperOmsFillExecutionEvidence | None = None
    occurred_at: datetime
    received_at: datetime
    committed_at: datetime

    @field_validator(
        "command_namespace",
        "idempotency_key",
        "account_id",
        "order_id",
        mode="before",
    )
    @classmethod
    def normalize_identifier(cls, value: object, info: object) -> str:
        field_name = str(getattr(info, "field_name", "event record identity"))
        return _normalize_identifier(value, label=field_name.replace("_", " ").title())

    @field_validator("execution_source", "external_fill_id", mode="before")
    @classmethod
    def normalize_optional_identifier(
        cls,
        value: object,
        info: object,
    ) -> str | None:
        if value is None:
            return None
        field_name = str(getattr(info, "field_name", "fill identity"))
        return _normalize_identifier(value, label=field_name.replace("_", " ").title())

    @field_validator("order_event", mode="before")
    @classmethod
    def revalidate_order_event(cls, value: object) -> OrderEvent:
        payload = value.model_dump(mode="python") if isinstance(value, BaseModel) else value
        return _EVENT_ADAPTER.validate_python(payload)

    @field_validator("ledger_fill", mode="before")
    @classmethod
    def revalidate_ledger_fill(cls, value: object) -> PaperFillEvent | None:
        if value is None:
            return None
        return PaperFillEvent.model_validate(
            value.model_dump(mode="python") if isinstance(value, PaperFillEvent) else value
        )

    @field_validator("fill_execution_evidence", mode="before")
    @classmethod
    def revalidate_fill_execution_evidence(
        cls,
        value: object,
    ) -> PaperOmsFillExecutionEvidence | None:
        if value is None:
            return None
        return PaperOmsFillExecutionEvidence.model_validate(
            value.model_dump(mode="python")
            if isinstance(value, PaperOmsFillExecutionEvidence)
            else value
        )

    @field_validator("occurred_at", "received_at", "committed_at", mode="before")
    @classmethod
    def normalize_event_time(cls, value: object, info: object) -> datetime:
        field_name = str(getattr(info, "field_name", "event time"))
        return _normalize_utc(value, label=field_name.replace("_", " ").title())

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if self.order_event.order_id != self.order_id:
            raise ValueError("Event record and order-event identities differ.")
        internal_key = _internal_command_key(
            self.command_namespace,
            self.idempotency_key,
        )
        if self.order_event.idempotency_key != internal_key:
            raise ValueError("Stored order event is not bound to its command identity.")
        is_fill = isinstance(self.order_event, OrderFillEvent)
        fill_fields = (
            self.ledger_fill,
            self.account_revision,
            self.execution_source,
            self.external_fill_id,
        )
        if is_fill and any(value is None for value in fill_fields):
            raise ValueError("A fill event record requires complete ledger identity.")
        if not is_fill and any(value is not None for value in fill_fields):
            raise ValueError("A non-fill event record cannot carry ledger identity.")
        if not is_fill and self.fill_execution_evidence is not None:
            raise ValueError("A non-fill event cannot carry execution evidence.")
        if self.committed_at < self.received_at:
            raise ValueError("Event commit time cannot precede server receive time.")
        if not is_fill:
            return self
        assert isinstance(self.order_event, OrderFillEvent)
        assert self.ledger_fill is not None
        assert self.account_revision is not None
        assert self.execution_source is not None
        assert self.external_fill_id is not None
        if self.fill_execution_evidence is not None and (
            self.fill_execution_evidence.account_id != self.account_id
            or self.fill_execution_evidence.order_id != self.order_id
            or self.fill_execution_evidence.quantity != self.order_event.fill_quantity
            or self.fill_execution_evidence.fill_price != self.order_event.fill_price
            or self.fill_execution_evidence.execution_source != self.execution_source
            or self.fill_execution_evidence.external_fill_id != self.external_fill_id
        ):
            raise ValueError("Stored fill differs from its trusted execution evidence.")
        namespaced_fill_id = _internal_fill_id(
            self.execution_source,
            self.external_fill_id,
        )
        if (
            self.order_event.fill_id != namespaced_fill_id
            or self.ledger_fill.fill_id != namespaced_fill_id
        ):
            raise ValueError("Stored fill is not bound to its namespaced identity.")
        if (
            self.ledger_fill.account_id != self.account_id
            or self.ledger_fill.order_id != self.order_id
            or self.ledger_fill.idempotency_key != internal_key
            or self.ledger_fill.quantity != self.order_event.fill_quantity
            or self.ledger_fill.fill_price != self.order_event.fill_price
            or self.ledger_fill.expected_revision + 1 != self.account_revision
        ):
            raise ValueError("Order and ledger fill economics do not agree.")
        return self


class PaperAccountMutationResult(BaseModel):
    model_config = _MODEL_CONFIG

    account: PaperAccountRecord
    idempotent_replay: bool


class PaperOrderMutationResult(BaseModel):
    model_config = _MODEL_CONFIG

    account: PaperAccountRecord
    order: PaperOrderRecord
    idempotent_replay: bool


class PaperEventMutationResult(BaseModel):
    model_config = _MODEL_CONFIG

    account: PaperAccountRecord
    order: PaperOrderRecord
    event: PaperOrderEventRecord
    idempotent_replay: bool


CommandModel: TypeAlias = (
    CreatePaperAccountCommand
    | SubmitPaperOrderCommand
    | RecordPaperOrderEventCommand
    | RecordPaperFillCommand
)
PaperOmsKillSwitchReader: TypeAlias = Callable[
    [],
    PaperOmsKillSwitchObservation,
]
PaperOmsPriceEvidenceReader: TypeAlias = Callable[
    [SubmitPaperOrderCommand, str],
    PaperOmsOrderMarketEvidence,
]
PaperOmsFillEvidenceReader: TypeAlias = Callable[
    [RecordPaperFillCommand, PaperOrderRecord],
    PaperOmsFillExecutionEvidence,
]
ModelT = TypeVar("ModelT", bound=BaseModel)


def _canonical_payload(value: BaseModel) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _payload_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _identity_hash(kind: str, values: Mapping[str, str]) -> str:
    payload = json.dumps(
        {"kind": kind, **dict(values)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _payload_hash(payload)


def _internal_command_key(namespace: str, idempotency_key: str) -> str:
    return _identity_hash(
        "paper_oms_command",
        {"namespace": namespace, "idempotency_key": idempotency_key},
    )


def _internal_fill_id(execution_source: str, external_fill_id: str) -> str:
    return _identity_hash(
        "paper_oms_fill",
        {
            "execution_source": execution_source,
            "external_fill_id": external_fill_id,
        },
    )


def _fill_economic_hash(command: RecordPaperFillCommand) -> str:
    economic_payload = {
        key: value
        for key, value in command.model_dump(mode="json").items()
        if key
        not in {
            "command_namespace",
            "idempotency_key",
            "expected_order_revision",
            "expected_account_revision",
        }
    }
    return _payload_hash(
        json.dumps(
            economic_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _validated_model(value: object, model: type[ModelT], *, label: str) -> ModelT:
    if not isinstance(value, model):
        raise ValueError(f"{label} must be a {model.__name__}.")
    return model.model_validate(value.model_dump(mode="python"))


def _parse_model_payload(
    payload: object,
    model: type[ModelT],
    *,
    label: str,
) -> ModelT:
    if not isinstance(payload, str):
        raise PaperOmsIntegrityError(f"Stored {label} is not text.")
    try:
        parsed = model.model_validate_json(payload)
    except (
        ValidationError,
        TypeError,
        ValueError,
        PaperLedgerReconciliationError,
    ) as error:
        raise PaperOmsIntegrityError(f"Stored {label} is invalid.") from error
    if _canonical_payload(parsed) != payload:
        raise PaperOmsIntegrityError(f"Stored {label} is not canonical.")
    return parsed


def _parse_order_event_payload(payload: object) -> OrderEvent:
    if not isinstance(payload, str):
        raise PaperOmsIntegrityError("Stored order event payload is not text.")
    try:
        event = _EVENT_ADAPTER.validate_json(payload)
    except (ValidationError, TypeError, ValueError) as error:
        raise PaperOmsIntegrityError("Stored order event payload is invalid.") from error
    if _canonical_payload(event) != payload:
        raise PaperOmsIntegrityError("Stored order event payload is not canonical.")
    return event


def _stored_text(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise PaperOmsIntegrityError(f"Stored {key} is not text.")
    return value


def _stored_optional_text(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise PaperOmsIntegrityError(f"Stored {key} is not text.")
    return value


def _stored_integer(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PaperOmsIntegrityError(f"Stored {key} is not an integer.")
    return value


def _stored_optional_integer(row: sqlite3.Row, key: str) -> int | None:
    value = row[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PaperOmsIntegrityError(f"Stored {key} is not an integer.")
    return value


def _stored_timestamp(row: sqlite3.Row, key: str) -> datetime:
    value = _stored_text(row, key)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise PaperOmsIntegrityError(f"Stored {key} is not ISO-8601.") from error
    normalized = _normalize_utc(parsed, label=f"Stored {key}")
    if normalized.isoformat() != value:
        raise PaperOmsIntegrityError(f"Stored {key} is not canonical UTC.")
    return normalized


class PaperOmsStore:
    """SQLite authority for continuous, transactionally coupled paper trading."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 30_000,
        clock: Callable[[], datetime] = _utc_now,
        order_risk_rule_set: OrderRiskRuleSet | None = None,
        kill_switch_reader: PaperOmsKillSwitchReader | None = None,
        price_evidence_reader: PaperOmsPriceEvidenceReader | None = None,
        fill_evidence_reader: PaperOmsFillEvidenceReader | None = None,
        allow_historical_client_timestamps: bool = False,
        maximum_client_clock_lead: timedelta = _MAXIMUM_CLIENT_CLOCK_LEAD,
    ) -> None:
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms <= 0
        ):
            raise ValueError("busy_timeout_ms must be a positive integer.")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self._clock = clock
        self._order_risk_rule_set = (
            None
            if order_risk_rule_set is None
            else OrderRiskRuleSet.model_validate(order_risk_rule_set.model_dump(mode="python"))
        )
        self._kill_switch_reader = kill_switch_reader
        self._price_evidence_reader = price_evidence_reader
        self._fill_evidence_reader = fill_evidence_reader
        if not isinstance(allow_historical_client_timestamps, bool):
            raise ValueError("allow_historical_client_timestamps must be boolean.")
        if (
            not isinstance(maximum_client_clock_lead, timedelta)
            or maximum_client_clock_lead < timedelta(0)
            or maximum_client_clock_lead > timedelta(minutes=1)
        ):
            raise ValueError("maximum_client_clock_lead must be between zero and one minute.")
        self._allow_historical_client_timestamps = allow_historical_client_timestamps
        self._maximum_client_clock_lead = maximum_client_clock_lead
        self._lock = threading.RLock()
        self._initialize()

    def _now(self) -> datetime:
        return _normalize_utc(self._clock(), label="Paper OMS clock")

    def _receive_command(self, command: CommandModel) -> datetime:
        received_at = self._now()
        if command.occurred_at > received_at + self._maximum_client_clock_lead:
            raise PaperOmsConflictError(
                "Client occurrence time exceeds the server clock tolerance."
            )
        return received_at

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1_000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys=ON")
            journal_mode_row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            connection.execute("PRAGMA synchronous=FULL")
            foreign_keys_row = connection.execute("PRAGMA foreign_keys").fetchone()
            synchronous_row = connection.execute("PRAGMA synchronous").fetchone()
        except sqlite3.Error as error:
            raise PaperOmsError("Could not configure the paper OMS database.") from error
        if (
            journal_mode_row is None
            or str(journal_mode_row[0]).lower() != "wal"
            or foreign_keys_row is None
            or foreign_keys_row[0] != 1
            or synchronous_row is None
            or synchronous_row[0] != 2
        ):
            connection.close()
            raise PaperOmsError("Required SQLite durability settings are unavailable.")
        return connection

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        began = False
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
                began = True
            except sqlite3.OperationalError as error:
                if "locked" in str(error).lower() or "busy" in str(error).lower():
                    raise PaperOmsBusyError("Paper OMS writer lock timed out.") from error
                raise PaperOmsError("Could not begin the paper OMS transaction.") from error
            yield connection
        except BaseException:
            if began:
                connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")')}

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )

    @classmethod
    def _stage_schema_rebuild(cls, connection: sqlite3.Connection) -> bool:
        """Move a pre-v3/developmental-v3 schema aside inside this transaction."""

        meta = connection.execute(
            "SELECT schema_version FROM paper_oms_meta WHERE singleton = 1"
        ).fetchone()
        if meta is None:
            raise PaperOmsIntegrityError("Paper OMS schema version is missing.")
        version = _stored_integer(meta, "schema_version")
        core_exists = cls._table_exists(connection, "paper_oms_order_events")
        markers_present = (
            core_exists
            and "fill_evidence_generation"
            in cls._table_columns(connection, "paper_oms_order_events")
            and cls._table_exists(connection, "paper_oms_order_risk_evaluations")
            and "evidence_generation"
            in cls._table_columns(
                connection,
                "paper_oms_order_risk_evaluations",
            )
        )
        rebuild = version in {1, 2} or (
            version == _SCHEMA_VERSION and core_exists and not markers_present
        )
        if not rebuild:
            return False
        for trigger in (
            "trg_paper_oms_v3_risk_evidence_insert",
            "trg_paper_oms_risk_generation_immutable",
            "trg_paper_oms_v3_fill_evidence_insert",
            "trg_paper_oms_fill_generation_immutable",
        ):
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        connection.execute('ALTER TABLE "paper_oms_meta" RENAME TO "__legacy_paper_oms_meta"')
        connection.execute(
            """
            CREATE TABLE paper_oms_meta (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                schema_version INTEGER NOT NULL CHECK(schema_version > 0)
            ) STRICT
            """
        )
        connection.execute(
            "INSERT INTO paper_oms_meta(singleton, schema_version) VALUES (1, ?)",
            (version,),
        )
        connection.execute('DROP TABLE "__legacy_paper_oms_meta"')
        for table in (
            "paper_oms_accounts",
            "paper_oms_orders",
            "paper_oms_order_events",
            "paper_oms_commands",
            "paper_oms_order_risk_evaluations",
        ):
            if cls._table_exists(connection, table):
                connection.execute(f'ALTER TABLE "{table}" RENAME TO "__legacy_{table}"')
        for index in (
            "idx_paper_oms_fill_identity",
            "idx_paper_oms_account_revision",
            "idx_paper_oms_order_event_log",
            "idx_paper_oms_commands_event",
            "idx_paper_oms_allowed_order_risk",
        ):
            connection.execute(f'DROP INDEX IF EXISTS "{index}"')
        for trigger in (
            "trg_paper_oms_v3_risk_evidence_insert",
            "trg_paper_oms_risk_generation_immutable",
            "trg_paper_oms_v3_fill_evidence_insert",
            "trg_paper_oms_fill_generation_immutable",
        ):
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        return True

    @classmethod
    def _copy_staged_schema(cls, connection: sqlite3.Connection) -> None:
        """Copy every legacy authority row into fully constrained v3 tables."""

        legacy_accounts = "__legacy_paper_oms_accounts"
        if not cls._table_exists(connection, legacy_accounts):
            return
        connection.execute(
            f"""
            INSERT INTO paper_oms_accounts
            SELECT account_id, currency, ledger_revision,
                   opening_event_payload, opening_event_hash,
                   ledger_state_payload, ledger_state_hash,
                   created_at, updated_at
            FROM "{legacy_accounts}"
            """
        )

        legacy_orders = "__legacy_paper_oms_orders"
        order_columns = cls._table_columns(connection, legacy_orders)
        committed_at = "committed_at" if "committed_at" in order_columns else "submitted_at"
        updated_committed_at = (
            "updated_committed_at" if "updated_committed_at" in order_columns else "updated_at"
        )
        connection.execute(
            f"""
            INSERT INTO paper_oms_orders (
                account_id, order_id, symbol, side, execution_source,
                requested_quantity, order_revision, order_state_payload,
                order_state_hash, submitted_at, committed_at, updated_at,
                updated_committed_at
            )
            SELECT account_id, order_id, symbol, side, execution_source,
                   requested_quantity, order_revision, order_state_payload,
                   order_state_hash, submitted_at, {committed_at}, updated_at,
                   {updated_committed_at}
            FROM "{legacy_orders}"
            """
        )

        legacy_events = "__legacy_paper_oms_order_events"
        event_columns = cls._table_columns(connection, legacy_events)
        execution_payload = (
            "fill_execution_evidence_payload"
            if "fill_execution_evidence_payload" in event_columns
            else "NULL"
        )
        execution_hash = (
            "fill_execution_evidence_hash"
            if "fill_execution_evidence_hash" in event_columns
            else "NULL"
        )
        occurred_at = "occurred_at" if "occurred_at" in event_columns else "committed_at"
        received_at = "received_at" if "received_at" in event_columns else "committed_at"
        connection.execute(
            f"""
            INSERT INTO paper_oms_order_events (
                event_id, account_id, order_id, order_revision,
                account_revision, origin_namespace, origin_idempotency_key,
                event_type, order_event_payload, order_event_hash,
                ledger_event_payload, ledger_event_hash, execution_source,
                external_fill_id, fill_economic_hash,
                fill_execution_evidence_payload,
                fill_execution_evidence_hash, fill_evidence_generation,
                occurred_at, received_at, committed_at
            )
            SELECT event_id, account_id, order_id, order_revision,
                   account_revision, origin_namespace, origin_idempotency_key,
                   event_type, order_event_payload, order_event_hash,
                   ledger_event_payload, ledger_event_hash, execution_source,
                   external_fill_id, fill_economic_hash,
                   {execution_payload}, {execution_hash},
                   CASE
                       WHEN {execution_payload} IS NULL AND {execution_hash} IS NULL
                           THEN 0
                       WHEN {execution_payload} IS NOT NULL AND {execution_hash} IS NOT NULL
                           THEN 1
                       ELSE -1
                   END,
                   {occurred_at}, {received_at}, committed_at
            FROM "{legacy_events}"
            """
        )

        legacy_risk = "__legacy_paper_oms_order_risk_evaluations"
        if cls._table_exists(connection, legacy_risk):
            risk_columns = cls._table_columns(connection, legacy_risk)
            market_payload = (
                "market_evidence_payload" if "market_evidence_payload" in risk_columns else "NULL"
            )
            market_hash = (
                "market_evidence_hash" if "market_evidence_hash" in risk_columns else "NULL"
            )
            reservation_payload = (
                "reservation_state_payload"
                if "reservation_state_payload" in risk_columns
                else "NULL"
            )
            reservation_hash = (
                "reservation_state_hash" if "reservation_state_hash" in risk_columns else "NULL"
            )
            connection.execute(
                f"""
                INSERT INTO paper_oms_order_risk_evaluations (
                    evaluation_id, origin_namespace, origin_idempotency_key,
                    command_payload, command_hash, account_id, order_id,
                    outcome, risk_request_payload, risk_request_hash,
                    risk_decision_payload, risk_decision_hash,
                    market_evidence_payload, market_evidence_hash,
                    reservation_state_payload, reservation_state_hash,
                    evidence_generation, recorded_at
                )
                SELECT evaluation_id, origin_namespace, origin_idempotency_key,
                       command_payload, command_hash, account_id, order_id,
                       outcome, risk_request_payload, risk_request_hash,
                       risk_decision_payload, risk_decision_hash,
                       {market_payload}, {market_hash},
                       {reservation_payload}, {reservation_hash},
                       CASE
                           WHEN {market_payload} IS NULL
                                AND {market_hash} IS NULL
                                AND {reservation_payload} IS NULL
                                AND {reservation_hash} IS NULL
                               THEN 0
                           WHEN {market_payload} IS NOT NULL
                                AND {market_hash} IS NOT NULL
                                AND {reservation_payload} IS NOT NULL
                                AND {reservation_hash} IS NOT NULL
                               THEN 1
                           ELSE -1
                       END,
                       recorded_at
                FROM "{legacy_risk}"
                """
            )

        legacy_commands = "__legacy_paper_oms_commands"
        command_columns = cls._table_columns(connection, legacy_commands)
        received_at = "received_at" if "received_at" in command_columns else "recorded_at"
        connection.execute(
            f"""
            INSERT INTO paper_oms_commands (
                command_namespace, idempotency_key, command_kind,
                command_payload, command_hash, account_id, order_id,
                result_order_revision, result_account_revision,
                result_event_id, received_at, recorded_at
            )
            SELECT command_namespace, idempotency_key, command_kind,
                   command_payload, command_hash, account_id, order_id,
                   result_order_revision, result_account_revision,
                   result_event_id, {received_at}, recorded_at
            FROM "{legacy_commands}"
            """
        )
        for table in (
            "__legacy_paper_oms_commands",
            "__legacy_paper_oms_order_risk_evaluations",
            "__legacy_paper_oms_order_events",
            "__legacy_paper_oms_orders",
            "__legacy_paper_oms_accounts",
        ):
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')

    @staticmethod
    def _schema_fingerprint(connection: sqlite3.Connection) -> str:
        rows = connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE sql IS NOT NULL
              AND (
                  name IN (
                      'paper_oms_meta',
                      'paper_oms_accounts',
                      'paper_oms_orders',
                      'paper_oms_order_events',
                      'paper_oms_commands',
                      'paper_oms_order_risk_evaluations'
                  )
                  OR name LIKE 'idx_paper_oms_%'
                  OR name LIKE 'trg_paper_oms_%'
              )
            ORDER BY type, name
            """
        ).fetchall()
        payload = [
            {
                "type": str(row["type"]),
                "name": str(row["name"]),
                "table": str(row["tbl_name"]),
                "sql": " ".join(str(row["sql"]).split()),
            }
            for row in rows
        ]
        return _payload_hash(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        )

    @staticmethod
    def _create_evidence_generation_triggers(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_paper_oms_v3_risk_evidence_insert
            BEFORE INSERT ON paper_oms_order_risk_evaluations
            WHEN (
                SELECT schema_version
                FROM paper_oms_meta
                WHERE singleton = 1
            ) = 3
            AND NEW.evidence_generation <> 1
            BEGIN
                SELECT RAISE(ABORT, 'schema-v3 risk evidence generation is required');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_paper_oms_risk_generation_immutable
            BEFORE UPDATE OF evidence_generation
            ON paper_oms_order_risk_evaluations
            WHEN NEW.evidence_generation <> OLD.evidence_generation
            BEGIN
                SELECT RAISE(ABORT, 'risk evidence generation is immutable');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_paper_oms_v3_fill_evidence_insert
            BEFORE INSERT ON paper_oms_order_events
            WHEN (
                SELECT schema_version
                FROM paper_oms_meta
                WHERE singleton = 1
            ) = 3
            AND NEW.event_type = 'fill'
            AND NEW.fill_evidence_generation <> 1
            BEGIN
                SELECT RAISE(ABORT, 'schema-v3 fill evidence generation is required');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_paper_oms_fill_generation_immutable
            BEFORE UPDATE OF fill_evidence_generation
            ON paper_oms_order_events
            WHEN NEW.fill_evidence_generation <> OLD.fill_evidence_generation
            BEGIN
                SELECT RAISE(ABORT, 'fill evidence generation is immutable');
            END
            """
        )

    @classmethod
    def _validate_schema_fingerprint(cls, connection: sqlite3.Connection) -> None:
        actual = cls._schema_fingerprint(connection)
        if actual != _SCHEMA_FINGERPRINT:
            raise PaperOmsIntegrityError(f"Paper OMS schema fingerprint mismatch: {actual}.")

    def _initialize(self) -> None:
        with self._lock, self._write_transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_oms_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    schema_version INTEGER NOT NULL CHECK(schema_version > 0)
                ) STRICT
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO paper_oms_meta(singleton, schema_version)
                VALUES (1, ?)
                """,
                (_SCHEMA_VERSION,),
            )
            initial_meta_rows = connection.execute(
                "SELECT singleton, schema_version FROM paper_oms_meta"
            ).fetchall()
            if (
                len(initial_meta_rows) != 1
                or initial_meta_rows[0]["singleton"] != 1
                or initial_meta_rows[0]["schema_version"] not in {1, 2, _SCHEMA_VERSION}
            ):
                raise PaperOmsIntegrityError("Paper OMS schema version is missing or unsupported.")
            staged_schema = self._stage_schema_rebuild(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_oms_accounts (
                    account_id TEXT PRIMARY KEY,
                    currency TEXT NOT NULL,
                    ledger_revision INTEGER NOT NULL CHECK(ledger_revision >= 1),
                    opening_event_payload TEXT NOT NULL,
                    opening_event_hash TEXT NOT NULL
                        CHECK(
                            length(opening_event_hash) = 64
                            AND opening_event_hash NOT GLOB '*[^0-9a-f]*'
                        ),
                    ledger_state_payload TEXT NOT NULL,
                    ledger_state_hash TEXT NOT NULL
                        CHECK(
                            length(ledger_state_hash) = 64
                            AND ledger_state_hash NOT GLOB '*[^0-9a-f]*'
                        ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_oms_orders (
                    account_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
                    execution_source TEXT NOT NULL,
                    requested_quantity TEXT NOT NULL,
                    order_revision INTEGER NOT NULL CHECK(order_revision >= 0),
                    order_state_payload TEXT NOT NULL,
                    order_state_hash TEXT NOT NULL
                        CHECK(
                            length(order_state_hash) = 64
                            AND order_state_hash NOT GLOB '*[^0-9a-f]*'
                        ),
                    submitted_at TEXT NOT NULL,
                    committed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_committed_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, order_id),
                    FOREIGN KEY(account_id)
                        REFERENCES paper_oms_accounts(account_id) ON DELETE RESTRICT
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_oms_order_events (
                    event_id INTEGER PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    order_revision INTEGER NOT NULL CHECK(order_revision >= 1),
                    account_revision INTEGER CHECK(account_revision >= 2),
                    origin_namespace TEXT NOT NULL,
                    origin_idempotency_key TEXT NOT NULL,
                    event_type TEXT NOT NULL CHECK(
                        event_type IN (
                            'acknowledged',
                            'fill',
                            'cancel_requested',
                            'cancel_acknowledged',
                            'cancel_rejected',
                            'rejected',
                            'expired'
                        )
                    ),
                    order_event_payload TEXT NOT NULL,
                    order_event_hash TEXT NOT NULL
                        CHECK(
                            length(order_event_hash) = 64
                            AND order_event_hash NOT GLOB '*[^0-9a-f]*'
                        ),
                    ledger_event_payload TEXT,
                    ledger_event_hash TEXT
                        CHECK(
                            ledger_event_hash IS NULL
                            OR (
                                length(ledger_event_hash) = 64
                                AND ledger_event_hash NOT GLOB '*[^0-9a-f]*'
                            )
                        ),
                    execution_source TEXT,
                    external_fill_id TEXT,
                    fill_economic_hash TEXT
                        CHECK(
                            fill_economic_hash IS NULL
                            OR (
                                length(fill_economic_hash) = 64
                                AND fill_economic_hash NOT GLOB '*[^0-9a-f]*'
                            )
                        ),
                    fill_execution_evidence_payload TEXT,
                    fill_execution_evidence_hash TEXT
                        CHECK(
                            fill_execution_evidence_hash IS NULL
                            OR (
                                length(fill_execution_evidence_hash) = 64
                                AND fill_execution_evidence_hash NOT GLOB '*[^0-9a-f]*'
                            )
                        ),
                    fill_evidence_generation INTEGER NOT NULL
                        CHECK(fill_evidence_generation IN (0, 1)),
                    occurred_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    committed_at TEXT NOT NULL,
                    UNIQUE(account_id, order_id, order_revision),
                    UNIQUE(origin_namespace, origin_idempotency_key),
                    FOREIGN KEY(account_id, order_id)
                        REFERENCES paper_oms_orders(account_id, order_id)
                        ON DELETE RESTRICT,
                    CHECK(
                        (
                            event_type = 'fill'
                            AND account_revision IS NOT NULL
                            AND ledger_event_payload IS NOT NULL
                            AND ledger_event_hash IS NOT NULL
                            AND execution_source IS NOT NULL
                            AND external_fill_id IS NOT NULL
                            AND fill_economic_hash IS NOT NULL
                            AND (
                                (
                                    fill_evidence_generation = 0
                                    AND fill_execution_evidence_payload IS NULL
                                    AND fill_execution_evidence_hash IS NULL
                                )
                                OR
                                (
                                    fill_evidence_generation = 1
                                    AND fill_execution_evidence_payload IS NOT NULL
                                    AND fill_execution_evidence_hash IS NOT NULL
                                )
                            )
                        )
                        OR
                        (
                            event_type <> 'fill'
                            AND account_revision IS NULL
                            AND ledger_event_payload IS NULL
                            AND ledger_event_hash IS NULL
                            AND execution_source IS NULL
                            AND external_fill_id IS NULL
                            AND fill_economic_hash IS NULL
                            AND fill_evidence_generation = 0
                            AND fill_execution_evidence_payload IS NULL
                            AND fill_execution_evidence_hash IS NULL
                        )
                    )
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_oms_fill_identity
                ON paper_oms_order_events(execution_source, external_fill_id)
                WHERE external_fill_id IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_oms_account_revision
                ON paper_oms_order_events(account_id, account_revision)
                WHERE account_revision IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_paper_oms_order_event_log
                ON paper_oms_order_events(account_id, order_id, order_revision)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_oms_commands (
                    command_namespace TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    command_kind TEXT NOT NULL CHECK(
                        command_kind IN (
                            'create_account',
                            'submit_order',
                            'order_event',
                            'fill'
                        )
                    ),
                    command_payload TEXT NOT NULL,
                    command_hash TEXT NOT NULL
                        CHECK(
                            length(command_hash) = 64
                            AND command_hash NOT GLOB '*[^0-9a-f]*'
                        ),
                    account_id TEXT NOT NULL,
                    order_id TEXT,
                    result_order_revision INTEGER CHECK(result_order_revision >= 0),
                    result_account_revision INTEGER NOT NULL
                        CHECK(result_account_revision >= 1),
                    result_event_id INTEGER,
                    received_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(command_namespace, idempotency_key),
                    FOREIGN KEY(account_id)
                        REFERENCES paper_oms_accounts(account_id) ON DELETE RESTRICT,
                    FOREIGN KEY(account_id, order_id)
                        REFERENCES paper_oms_orders(account_id, order_id)
                        ON DELETE RESTRICT,
                    FOREIGN KEY(result_event_id)
                        REFERENCES paper_oms_order_events(event_id)
                        ON DELETE RESTRICT,
                    CHECK(
                        (
                            command_kind = 'create_account'
                            AND order_id IS NULL
                            AND result_order_revision IS NULL
                            AND result_event_id IS NULL
                        )
                        OR
                        (
                            command_kind = 'submit_order'
                            AND order_id IS NOT NULL
                            AND result_order_revision = 0
                            AND result_event_id IS NULL
                        )
                        OR
                        (
                            command_kind IN ('order_event', 'fill')
                            AND order_id IS NOT NULL
                            AND result_order_revision >= 1
                            AND result_event_id IS NOT NULL
                        )
                    )
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_paper_oms_commands_event
                ON paper_oms_commands(result_event_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_oms_order_risk_evaluations (
                    evaluation_id INTEGER PRIMARY KEY,
                    origin_namespace TEXT NOT NULL,
                    origin_idempotency_key TEXT NOT NULL,
                    command_payload TEXT NOT NULL,
                    command_hash TEXT NOT NULL
                        CHECK(
                            length(command_hash) = 64
                            AND command_hash NOT GLOB '*[^0-9a-f]*'
                        ),
                    account_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('allow', 'reject')),
                    risk_request_payload TEXT NOT NULL,
                    risk_request_hash TEXT NOT NULL
                        CHECK(
                            length(risk_request_hash) = 64
                            AND risk_request_hash NOT GLOB '*[^0-9a-f]*'
                        ),
                    risk_decision_payload TEXT NOT NULL,
                    risk_decision_hash TEXT NOT NULL
                        CHECK(
                            length(risk_decision_hash) = 64
                            AND risk_decision_hash NOT GLOB '*[^0-9a-f]*'
                        ),
                    market_evidence_payload TEXT,
                    market_evidence_hash TEXT
                        CHECK(
                            market_evidence_hash IS NULL
                            OR (
                                length(market_evidence_hash) = 64
                                AND market_evidence_hash NOT GLOB '*[^0-9a-f]*'
                            )
                        ),
                    reservation_state_payload TEXT,
                    reservation_state_hash TEXT
                        CHECK(
                            reservation_state_hash IS NULL
                            OR (
                                length(reservation_state_hash) = 64
                                AND reservation_state_hash NOT GLOB '*[^0-9a-f]*'
                            )
                        ),
                    evidence_generation INTEGER NOT NULL
                        CHECK(evidence_generation IN (0, 1)),
                    recorded_at TEXT NOT NULL,
                    UNIQUE(origin_namespace, origin_idempotency_key),
                    FOREIGN KEY(account_id)
                        REFERENCES paper_oms_accounts(account_id)
                        ON DELETE RESTRICT,
                    CHECK(
                        (
                            evidence_generation = 0
                            AND market_evidence_payload IS NULL
                            AND market_evidence_hash IS NULL
                            AND reservation_state_payload IS NULL
                            AND reservation_state_hash IS NULL
                        )
                        OR
                        (
                            evidence_generation = 1
                            AND market_evidence_payload IS NOT NULL
                            AND market_evidence_hash IS NOT NULL
                            AND reservation_state_payload IS NOT NULL
                            AND reservation_state_hash IS NOT NULL
                        )
                    )
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_oms_allowed_order_risk
                ON paper_oms_order_risk_evaluations(account_id, order_id)
                WHERE outcome = 'allow'
                """
            )
            order_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(paper_oms_orders)")
            }
            if "committed_at" not in order_columns:
                connection.execute("ALTER TABLE paper_oms_orders ADD COLUMN committed_at TEXT")
                connection.execute("UPDATE paper_oms_orders SET committed_at = submitted_at")
            if "updated_committed_at" not in order_columns:
                connection.execute(
                    "ALTER TABLE paper_oms_orders ADD COLUMN updated_committed_at TEXT"
                )
                connection.execute("UPDATE paper_oms_orders SET updated_committed_at = updated_at")
            event_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(paper_oms_order_events)")
            }
            for column, declaration in (
                ("fill_execution_evidence_payload", "TEXT"),
                ("fill_execution_evidence_hash", "TEXT"),
                ("occurred_at", "TEXT"),
                ("received_at", "TEXT"),
            ):
                if column not in event_columns:
                    connection.execute(
                        f"ALTER TABLE paper_oms_order_events ADD COLUMN {column} {declaration}"
                    )
            connection.execute(
                """
                UPDATE paper_oms_order_events
                SET occurred_at = COALESCE(occurred_at, committed_at),
                    received_at = COALESCE(received_at, committed_at)
                """
            )
            command_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(paper_oms_commands)")
            }
            if "received_at" not in command_columns:
                connection.execute("ALTER TABLE paper_oms_commands ADD COLUMN received_at TEXT")
                connection.execute("UPDATE paper_oms_commands SET received_at = recorded_at")
            risk_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(paper_oms_order_risk_evaluations)")
            }
            for column in (
                "market_evidence_payload",
                "market_evidence_hash",
                "reservation_state_payload",
                "reservation_state_hash",
            ):
                if column not in risk_columns:
                    connection.execute(
                        f"ALTER TABLE paper_oms_order_risk_evaluations ADD COLUMN {column} TEXT"
                    )
            if staged_schema:
                try:
                    self._copy_staged_schema(connection)
                except sqlite3.Error as error:
                    raise PaperOmsIntegrityError(
                        "Legacy paper OMS rows do not satisfy the complete v3 schema."
                    ) from error
            self._create_evidence_generation_triggers(connection)
            pre_upgrade_integrity = connection.execute("PRAGMA integrity_check").fetchone()
            pre_upgrade_foreign_key_issues = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if (
                pre_upgrade_integrity is None
                or pre_upgrade_integrity[0] != "ok"
                or pre_upgrade_foreign_key_issues
            ):
                raise PaperOmsIntegrityError(
                    "Rebuilt paper OMS schema failed pre-upgrade integrity checks."
                )
            self._validate_schema_fingerprint(connection)
            if initial_meta_rows[0]["schema_version"] in {1, 2}:
                connection.execute(
                    """
                    UPDATE paper_oms_meta
                    SET schema_version = ?
                    WHERE singleton = 1 AND schema_version IN (1, 2)
                    """,
                    (_SCHEMA_VERSION,),
                )
            meta_rows = connection.execute(
                "SELECT singleton, schema_version FROM paper_oms_meta"
            ).fetchall()
            if len(meta_rows) != 1 or tuple(meta_rows[0]) != (1, _SCHEMA_VERSION):
                raise PaperOmsIntegrityError("Paper OMS schema version is missing or unsupported.")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
            if integrity is None or integrity[0] != "ok" or foreign_key_issues:
                raise PaperOmsIntegrityError("Paper OMS database failed SQLite integrity checks.")
            self._validate_all(connection)

    @staticmethod
    def _command_kind(command: CommandModel) -> str:
        if isinstance(command, CreatePaperAccountCommand):
            return "create_account"
        if isinstance(command, SubmitPaperOrderCommand):
            return "submit_order"
        if isinstance(command, RecordPaperOrderEventCommand):
            return "order_event"
        if isinstance(command, RecordPaperFillCommand):
            return "fill"
        raise TypeError(f"Unsupported paper OMS command: {type(command).__name__}.")

    @staticmethod
    def _command_model(kind: str) -> type[CommandModel]:
        models: dict[str, type[CommandModel]] = {
            "create_account": CreatePaperAccountCommand,
            "submit_order": SubmitPaperOrderCommand,
            "order_event": RecordPaperOrderEventCommand,
            "fill": RecordPaperFillCommand,
        }
        try:
            return models[kind]
        except KeyError as error:
            raise PaperOmsIntegrityError(f"Stored command kind {kind!r} is unsupported.") from error

    def _validate_command_row(self, row: sqlite3.Row) -> CommandModel:
        kind = _stored_text(row, "command_kind")
        model = self._command_model(kind)
        command = _parse_model_payload(
            row["command_payload"],
            model,
            label=f"{kind} command payload",
        )
        payload = _canonical_payload(command)
        stored_hash = _stored_text(row, "command_hash")
        if _payload_hash(payload) != stored_hash or stored_hash != _payload_hash(
            _stored_text(row, "command_payload")
        ):
            raise PaperOmsIntegrityError("Stored command hash does not match its payload.")
        if (
            command.command_namespace != _stored_text(row, "command_namespace")
            or command.idempotency_key != _stored_text(row, "idempotency_key")
            or command.account_id != _stored_text(row, "account_id")
        ):
            raise PaperOmsIntegrityError("Stored command columns do not match the command payload.")
        stored_order_id = _stored_optional_text(row, "order_id")
        command_order_id = (
            command.order_id
            if isinstance(
                command,
                (
                    SubmitPaperOrderCommand,
                    RecordPaperOrderEventCommand,
                    RecordPaperFillCommand,
                ),
            )
            else None
        )
        if stored_order_id != command_order_id:
            raise PaperOmsIntegrityError(
                "Stored command order identity does not match its payload."
            )
        result_order_revision = _stored_optional_integer(
            row,
            "result_order_revision",
        )
        result_account_revision = _stored_integer(row, "result_account_revision")
        result_event_id = _stored_optional_integer(row, "result_event_id")
        received_at = _stored_timestamp(row, "received_at")
        recorded_at = _stored_timestamp(row, "recorded_at")
        if recorded_at < received_at:
            raise PaperOmsIntegrityError(
                "Stored command commit time precedes its server receive time."
            )
        if isinstance(command, CreatePaperAccountCommand):
            if (
                result_order_revision is not None
                or result_event_id is not None
                or result_account_revision != 1
            ):
                raise PaperOmsIntegrityError(
                    "Stored account-creation result columns are inconsistent."
                )
        elif isinstance(command, SubmitPaperOrderCommand):
            if (
                result_order_revision != 0
                or result_event_id is not None
                or result_account_revision < 1
            ):
                raise PaperOmsIntegrityError(
                    "Stored order-submission result columns are inconsistent."
                )
        elif result_order_revision is None or result_order_revision < 1 or result_event_id is None:
            raise PaperOmsIntegrityError("Stored event-command result columns are inconsistent.")
        return command

    def _find_command(
        self,
        connection: sqlite3.Connection,
        command: CommandModel,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            """
            SELECT *
            FROM paper_oms_commands
            WHERE command_namespace = ? AND idempotency_key = ?
            """,
            (command.command_namespace, command.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        stored_command = self._validate_command_row(row)
        expected_kind = self._command_kind(command)
        stored_kind = _stored_text(row, "command_kind")
        if stored_kind != expected_kind or _canonical_payload(stored_command) != _canonical_payload(
            command
        ):
            raise PaperOmsConflictError(
                "Command idempotency identity is already bound to a different payload."
            )
        return cast(sqlite3.Row, row)

    def _insert_command(
        self,
        connection: sqlite3.Connection,
        command: CommandModel,
        *,
        result_account_revision: int,
        result_order_revision: int | None,
        result_event_id: int | None,
        received_at: datetime,
    ) -> None:
        payload = _canonical_payload(command)
        committed_at = self._now()
        if committed_at < received_at:
            raise PaperOmsIntegrityError("Server command commit time precedes receive time.")
        try:
            connection.execute(
                """
                INSERT INTO paper_oms_commands (
                    command_namespace,
                    idempotency_key,
                    command_kind,
                    command_payload,
                    command_hash,
                    account_id,
                    order_id,
                    result_order_revision,
                    result_account_revision,
                    result_event_id,
                    received_at,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.command_namespace,
                    command.idempotency_key,
                    self._command_kind(command),
                    payload,
                    _payload_hash(payload),
                    command.account_id,
                    command.order_id
                    if isinstance(
                        command,
                        (
                            SubmitPaperOrderCommand,
                            RecordPaperOrderEventCommand,
                            RecordPaperFillCommand,
                        ),
                    )
                    else None,
                    result_order_revision,
                    result_account_revision,
                    result_event_id,
                    received_at.isoformat(),
                    committed_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise PaperOmsConflictError(
                "Command identity or result reference conflicts with durable state."
            ) from error

    @staticmethod
    def _account_id_hash(account_id: str) -> str:
        return _identity_hash(
            "paper_oms_account",
            {"account_id": account_id},
        )

    def _rebuild_account_ledger(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        *,
        through_revision: int | None = None,
    ) -> PaperLedgerState:
        row = connection.execute(
            "SELECT * FROM paper_oms_accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise PaperOmsNotFoundError(f"Paper account {account_id!r} does not exist.")
        opening_payload = _stored_text(row, "opening_event_payload")
        opening_event = _parse_model_payload(
            opening_payload,
            OpeningBalanceEvent,
            label="opening balance event",
        )
        if (
            opening_event.event_hash != _stored_text(row, "opening_event_hash")
            or opening_event.account_id != account_id
        ):
            raise PaperOmsIntegrityError("Stored opening event does not match its account row.")
        try:
            rebuilt = open_paper_ledger(opening_event)
        except (ValidationError, ValueError, PaperLedgerReconciliationError) as error:
            raise PaperOmsIntegrityError(
                "Stored opening event cannot rebuild its ledger."
            ) from error
        target_revision = (
            _stored_integer(row, "ledger_revision")
            if through_revision is None
            else through_revision
        )
        if (
            isinstance(target_revision, bool)
            or not isinstance(target_revision, int)
            or target_revision < 1
            or target_revision > _stored_integer(row, "ledger_revision")
        ):
            raise PaperOmsIntegrityError(
                "Risk evaluation refers to an unavailable account revision."
            )
        event_rows = connection.execute(
            """
            SELECT *
            FROM paper_oms_order_events
            WHERE
                account_id = ?
                AND account_revision IS NOT NULL
                AND account_revision <= ?
            ORDER BY account_revision
            """,
            (account_id, target_revision),
        ).fetchall()
        expected_account_revision = 2
        for event_row in event_rows:
            event = self._event_from_row(event_row)
            if event.account_revision != expected_account_revision or event.ledger_fill is None:
                raise PaperOmsIntegrityError("Paper account event revisions are not gapless.")
            try:
                applied = apply_paper_fill(rebuilt, event.ledger_fill)
            except (
                ValidationError,
                ValueError,
                PaperLedgerConflictError,
                PaperLedgerRevisionError,
                InsufficientPaperCashError,
                InsufficientPaperPositionError,
            ) as error:
                raise PaperOmsIntegrityError(
                    "Stored fill history cannot rebuild its paper ledger."
                ) from error
            if applied.idempotent_replay or applied.state.revision != expected_account_revision:
                raise PaperOmsIntegrityError(
                    "Stored fill history did not advance its ledger exactly once."
                )
            rebuilt = applied.state
            expected_account_revision += 1
        if rebuilt.revision != target_revision:
            raise PaperOmsIntegrityError(
                "Risk evaluation account revision is not present in fill history."
            )
        return rebuilt

    def _risk_evaluation_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> PaperOrderRiskEvaluationRecord:
        command_payload = _stored_text(row, "command_payload")
        command = _parse_model_payload(
            command_payload,
            SubmitPaperOrderCommand,
            label="order-risk submit command",
        )
        if _payload_hash(command_payload) != _stored_text(row, "command_hash"):
            raise PaperOmsIntegrityError(
                "Stored order-risk command hash does not match its payload."
            )
        request_payload = _stored_text(row, "risk_request_payload")
        request = _parse_model_payload(
            request_payload,
            OrderRiskRequest,
            label="order-risk request",
        )
        decision_payload = _stored_text(row, "risk_decision_payload")
        decision = _parse_model_payload(
            decision_payload,
            RiskDecision,
            label="order-risk decision",
        )
        if (
            request.request_hash != _stored_text(row, "risk_request_hash")
            or decision.decision_hash != _stored_text(row, "risk_decision_hash")
            or decision.request != request
        ):
            raise PaperOmsIntegrityError("Stored order-risk hashes or request binding are invalid.")
        market_payload = _stored_optional_text(row, "market_evidence_payload")
        market_hash = _stored_optional_text(row, "market_evidence_hash")
        evidence_generation = _stored_integer(row, "evidence_generation")
        if (market_payload is None) != (market_hash is None):
            raise PaperOmsIntegrityError(
                "Stored order market evidence payload and hash presence differ."
            )
        market_evidence = (
            _parse_model_payload(
                market_payload,
                PaperOmsOrderMarketEvidence,
                label="order market evidence",
            )
            if market_payload is not None
            else None
        )
        if market_evidence is not None and market_evidence.evidence_hash != market_hash:
            raise PaperOmsIntegrityError("Stored order market evidence hash is invalid.")
        reservation_payload = _stored_optional_text(row, "reservation_state_payload")
        reservation_hash = _stored_optional_text(row, "reservation_state_hash")
        if (reservation_payload is None) != (reservation_hash is None):
            raise PaperOmsIntegrityError(
                "Stored reservation-state payload and hash presence differ."
            )
        evidence_values = (
            market_payload,
            market_hash,
            reservation_payload,
            reservation_hash,
        )
        if (
            (evidence_generation == 0 and any(value is not None for value in evidence_values))
            or (evidence_generation == 1 and any(value is None for value in evidence_values))
            or evidence_generation not in {0, 1}
        ):
            raise PaperOmsIntegrityError("Stored order-risk evidence generation is invalid.")
        reservation_state = (
            _parse_model_payload(
                reservation_payload,
                PaperOmsReservationStateEvidence,
                label="reservation-state evidence",
            )
            if reservation_payload is not None
            else None
        )
        if reservation_state is not None and reservation_state.evidence_hash != reservation_hash:
            raise PaperOmsIntegrityError("Stored reservation-state evidence hash is invalid.")
        try:
            evaluation = PaperOrderRiskEvaluationRecord.model_validate(
                {
                    "evaluation_id": _stored_integer(row, "evaluation_id"),
                    "command": command,
                    "outcome": _stored_text(row, "outcome"),
                    "request": request,
                    "decision": decision,
                    "market_evidence": market_evidence,
                    "reservation_state": reservation_state,
                    "recorded_at": _stored_timestamp(row, "recorded_at"),
                }
            )
        except (ValidationError, ValueError) as error:
            raise PaperOmsIntegrityError(
                "Stored paper order-risk evaluation is invalid."
            ) from error
        if (
            evaluation.command.command_namespace != _stored_text(row, "origin_namespace")
            or evaluation.command.idempotency_key != _stored_text(row, "origin_idempotency_key")
            or evaluation.command.account_id != _stored_text(row, "account_id")
            or evaluation.command.order_id != _stored_text(row, "order_id")
        ):
            raise PaperOmsIntegrityError("Stored order-risk columns differ from their command.")
        historical_ledger = self._rebuild_account_ledger(
            connection,
            evaluation.command.account_id,
            through_revision=request.state.account_revision,
        )
        position_quantity = next(
            (
                position.quantity
                for position in historical_ledger.positions
                if position.symbol == evaluation.command.symbol
            ),
            Decimal(0),
        )
        historical_payload = _canonical_payload(historical_ledger)
        if (
            request.state.account_id_hash != self._account_id_hash(evaluation.command.account_id)
            or request.state.account_state_hash != _payload_hash(historical_payload)
            or request.state.cash_balance != historical_ledger.cash
            or request.state.position_quantity != position_quantity
            or request.state.symbol != evaluation.command.symbol
            or request.state.quote_currency != historical_ledger.currency
            or request.intent.quote_currency != historical_ledger.currency
        ):
            raise PaperOmsIntegrityError(
                "Stored order-risk account state differs from durable history."
            )
        replayed = evaluate_order_risk(request)
        if replayed != decision:
            raise PaperOmsIntegrityError(
                "Stored order-risk decision differs from deterministic replay."
            )
        if reservation_state is not None:
            rebuilt_reservations = self._build_reservation_evidence(
                connection,
                account_id=evaluation.command.account_id,
                symbol=evaluation.command.symbol,
                event_horizon_id=reservation_state.event_horizon_id,
                prior_evaluation_horizon_id=reservation_state.prior_evaluation_horizon_id,
            )
            if rebuilt_reservations != reservation_state:
                raise PaperOmsIntegrityError(
                    "Stored reservation-state evidence differs from event-prefix replay."
                )
        return evaluation

    def _find_risk_evaluation(
        self,
        connection: sqlite3.Connection,
        command: SubmitPaperOrderCommand,
    ) -> PaperOrderRiskEvaluationRecord | None:
        row = connection.execute(
            """
            SELECT *
            FROM paper_oms_order_risk_evaluations
            WHERE origin_namespace = ? AND origin_idempotency_key = ?
            """,
            (command.command_namespace, command.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        evaluation = self._risk_evaluation_from_row(connection, row)
        if _canonical_payload(evaluation.command) != _canonical_payload(command):
            raise PaperOmsConflictError(
                "Command idempotency identity is already bound to a different payload."
            )
        return evaluation

    def _allowed_risk_for_order(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        order_id: str,
    ) -> PaperOrderRiskEvaluationRecord | None:
        rows = connection.execute(
            """
            SELECT *
            FROM paper_oms_order_risk_evaluations
            WHERE account_id = ? AND order_id = ? AND outcome = 'allow'
            """,
            (account_id, order_id),
        ).fetchall()
        if len(rows) > 1:
            raise PaperOmsIntegrityError("A paper order has multiple allow risk evaluations.")
        if not rows:
            return None
        return self._risk_evaluation_from_row(connection, rows[0])

    def _insert_risk_evaluation(
        self,
        connection: sqlite3.Connection,
        *,
        command: SubmitPaperOrderCommand,
        request: OrderRiskRequest,
        decision: RiskDecision,
        market_evidence: PaperOmsOrderMarketEvidence,
        reservation_state: PaperOmsReservationStateEvidence,
        recorded_at: datetime,
    ) -> PaperOrderRiskEvaluationRecord:
        command_payload = _canonical_payload(command)
        request_payload = _canonical_payload(request)
        decision_payload = _canonical_payload(decision)
        market_payload = _canonical_payload(market_evidence)
        reservation_payload = _canonical_payload(reservation_state)
        outcome = "allow" if decision.decision == "allow" else "reject"
        try:
            cursor = connection.execute(
                """
                INSERT INTO paper_oms_order_risk_evaluations (
                    origin_namespace,
                    origin_idempotency_key,
                    command_payload,
                    command_hash,
                    account_id,
                    order_id,
                    outcome,
                    risk_request_payload,
                    risk_request_hash,
                    risk_decision_payload,
                    risk_decision_hash,
                    market_evidence_payload,
                    market_evidence_hash,
                    reservation_state_payload,
                    reservation_state_hash,
                    evidence_generation,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.command_namespace,
                    command.idempotency_key,
                    command_payload,
                    _payload_hash(command_payload),
                    command.account_id,
                    command.order_id,
                    outcome,
                    request_payload,
                    request.request_hash,
                    decision_payload,
                    decision.decision_hash,
                    market_payload,
                    market_evidence.evidence_hash,
                    reservation_payload,
                    reservation_state.evidence_hash,
                    1,
                    recorded_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise PaperOmsConflictError(
                "Order-risk evaluation identity conflicts with durable state."
            ) from error
        evaluation_id = cursor.lastrowid
        if (
            isinstance(evaluation_id, bool)
            or not isinstance(evaluation_id, int)
            or evaluation_id < 1
        ):
            raise PaperOmsIntegrityError(
                "SQLite did not issue a valid order-risk evaluation identity."
            )
        row = connection.execute(
            """
            SELECT *
            FROM paper_oms_order_risk_evaluations
            WHERE evaluation_id = ?
            """,
            (evaluation_id,),
        ).fetchone()
        if row is None:
            raise PaperOmsIntegrityError("Inserted order-risk evaluation could not be read back.")
        return self._risk_evaluation_from_row(connection, row)

    def _event_from_row(self, row: sqlite3.Row) -> PaperOrderEventRecord:
        order_event_payload = _stored_text(row, "order_event_payload")
        order_event = _parse_order_event_payload(order_event_payload)
        order_event_hash = _stored_text(row, "order_event_hash")
        if order_event.payload_hash != order_event_hash:
            raise PaperOmsIntegrityError("Stored order-event hash does not match its payload.")
        if order_event.event_type != _stored_text(row, "event_type"):
            raise PaperOmsIntegrityError("Stored order-event type differs from its payload.")
        ledger_payload = _stored_optional_text(row, "ledger_event_payload")
        ledger_fill = (
            _parse_model_payload(
                ledger_payload,
                PaperFillEvent,
                label="ledger fill event",
            )
            if ledger_payload is not None
            else None
        )
        ledger_hash = _stored_optional_text(row, "ledger_event_hash")
        if (ledger_fill is None) != (ledger_hash is None):
            raise PaperOmsIntegrityError("Stored ledger event payload and hash presence differ.")
        if ledger_fill is not None and ledger_fill.event_hash != ledger_hash:
            raise PaperOmsIntegrityError("Stored ledger-event hash does not match its payload.")
        execution_payload = _stored_optional_text(
            row,
            "fill_execution_evidence_payload",
        )
        execution_hash = _stored_optional_text(
            row,
            "fill_execution_evidence_hash",
        )
        evidence_generation = _stored_integer(row, "fill_evidence_generation")
        if (execution_payload is None) != (execution_hash is None):
            raise PaperOmsIntegrityError(
                "Stored fill execution evidence payload and hash presence differ."
            )
        if (
            (evidence_generation == 0 and execution_payload is not None)
            or (evidence_generation == 1 and execution_payload is None)
            or evidence_generation not in {0, 1}
        ):
            raise PaperOmsIntegrityError("Stored fill execution evidence generation is invalid.")
        execution_evidence = (
            _parse_model_payload(
                execution_payload,
                PaperOmsFillExecutionEvidence,
                label="fill execution evidence",
            )
            if execution_payload is not None
            else None
        )
        if execution_evidence is not None and execution_evidence.evidence_hash != execution_hash:
            raise PaperOmsIntegrityError("Stored fill execution evidence hash is invalid.")
        try:
            return PaperOrderEventRecord.model_validate(
                {
                    "event_id": _stored_integer(row, "event_id"),
                    "command_namespace": _stored_text(row, "origin_namespace"),
                    "idempotency_key": _stored_text(
                        row,
                        "origin_idempotency_key",
                    ),
                    "account_id": _stored_text(row, "account_id"),
                    "order_id": _stored_text(row, "order_id"),
                    "order_revision": _stored_integer(row, "order_revision"),
                    "account_revision": _stored_optional_integer(
                        row,
                        "account_revision",
                    ),
                    "order_event": order_event,
                    "ledger_fill": ledger_fill,
                    "execution_source": _stored_optional_text(
                        row,
                        "execution_source",
                    ),
                    "external_fill_id": _stored_optional_text(
                        row,
                        "external_fill_id",
                    ),
                    "fill_execution_evidence": execution_evidence,
                    "occurred_at": _stored_timestamp(row, "occurred_at"),
                    "received_at": _stored_timestamp(row, "received_at"),
                    "committed_at": _stored_timestamp(row, "committed_at"),
                }
            )
        except (ValidationError, ValueError) as error:
            raise PaperOmsIntegrityError("Stored paper order event is invalid.") from error

    def _event_by_id(
        self,
        connection: sqlite3.Connection,
        event_id: int,
    ) -> PaperOrderEventRecord:
        row = connection.execute(
            "SELECT * FROM paper_oms_order_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise PaperOmsIntegrityError("A command receipt refers to a missing paper order event.")
        return self._event_from_row(row)

    def _load_account(
        self,
        connection: sqlite3.Connection,
        account_id: str,
    ) -> PaperAccountRecord:
        row = connection.execute(
            "SELECT * FROM paper_oms_accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise PaperOmsNotFoundError(f"Paper account {account_id!r} does not exist.")
        opening_payload = _stored_text(row, "opening_event_payload")
        opening_event = _parse_model_payload(
            opening_payload,
            OpeningBalanceEvent,
            label="opening balance event",
        )
        if (
            opening_event.event_hash != _stored_text(row, "opening_event_hash")
            or opening_event.account_id != account_id
        ):
            raise PaperOmsIntegrityError("Stored opening event does not match its account row.")
        try:
            rebuilt = open_paper_ledger(opening_event)
        except (ValidationError, ValueError, PaperLedgerReconciliationError) as error:
            raise PaperOmsIntegrityError(
                "Stored opening event cannot rebuild its ledger."
            ) from error
        event_rows = connection.execute(
            """
            SELECT *
            FROM paper_oms_order_events
            WHERE account_id = ? AND account_revision IS NOT NULL
            ORDER BY account_revision
            """,
            (account_id,),
        ).fetchall()
        expected_account_revision = 2
        for event_row in event_rows:
            event = self._event_from_row(event_row)
            if event.account_revision != expected_account_revision or event.ledger_fill is None:
                raise PaperOmsIntegrityError("Paper account event revisions are not gapless.")
            try:
                applied = apply_paper_fill(rebuilt, event.ledger_fill)
            except (
                ValidationError,
                ValueError,
                PaperLedgerConflictError,
                PaperLedgerRevisionError,
                InsufficientPaperCashError,
                InsufficientPaperPositionError,
            ) as error:
                raise PaperOmsIntegrityError(
                    "Stored fill history cannot rebuild its paper ledger."
                ) from error
            if applied.idempotent_replay or applied.state.revision != expected_account_revision:
                raise PaperOmsIntegrityError(
                    "Stored fill history did not advance its ledger exactly once."
                )
            rebuilt = applied.state
            expected_account_revision += 1
        stored_revision = _stored_integer(row, "ledger_revision")
        if rebuilt.revision != stored_revision:
            raise PaperOmsIntegrityError("Stored account revision differs from its fill history.")
        stored_payload = _stored_text(row, "ledger_state_payload")
        if _payload_hash(stored_payload) != _stored_text(row, "ledger_state_hash"):
            raise PaperOmsIntegrityError("Stored ledger-state hash does not match its payload.")
        stored_state = _parse_model_payload(
            stored_payload,
            PaperLedgerState,
            label="paper ledger state",
        )
        try:
            reconcile_paper_ledger(stored_state)
        except (ValidationError, ValueError, PaperLedgerReconciliationError) as error:
            raise PaperOmsIntegrityError("Stored paper ledger state does not reconcile.") from error
        if stored_state != rebuilt:
            raise PaperOmsIntegrityError(
                "Stored paper ledger projection differs from event replay."
            )
        currency = _stored_text(row, "currency")
        if currency != rebuilt.currency:
            raise PaperOmsIntegrityError("Stored account currency differs from its ledger.")
        try:
            return PaperAccountRecord.model_validate(
                {
                    "account_id": account_id,
                    "currency": currency,
                    "ledger": stored_state,
                    "created_at": _stored_timestamp(row, "created_at"),
                    "updated_at": _stored_timestamp(row, "updated_at"),
                }
            )
        except (
            ValidationError,
            ValueError,
            PaperLedgerReconciliationError,
        ) as error:
            raise PaperOmsIntegrityError("Stored paper account record is invalid.") from error

    def _load_order(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        order_id: str,
    ) -> PaperOrderRecord:
        row = connection.execute(
            """
            SELECT *
            FROM paper_oms_orders
            WHERE account_id = ? AND order_id = ?
            """,
            (account_id, order_id),
        ).fetchone()
        if row is None:
            raise PaperOmsNotFoundError(f"Paper order {account_id!r}/{order_id!r} does not exist.")
        requested_quantity = _stored_text(row, "requested_quantity")
        try:
            rebuilt = create_order_state(
                order_id=order_id,
                requested_quantity=requested_quantity,
            )
        except (ValidationError, ValueError) as error:
            raise PaperOmsIntegrityError(
                "Stored order request cannot create its initial state."
            ) from error
        event_rows = connection.execute(
            """
            SELECT *
            FROM paper_oms_order_events
            WHERE account_id = ? AND order_id = ?
            ORDER BY order_revision
            """,
            (account_id, order_id),
        ).fetchall()
        execution_source = _stored_text(row, "execution_source")
        symbol = _stored_text(row, "symbol")
        side = _stored_text(row, "side")
        if (
            _normalize_symbol(symbol) != symbol
            or _normalize_identifier(
                execution_source,
                label="Stored execution source",
            )
            != execution_source
        ):
            raise PaperOmsIntegrityError("Stored order routing metadata is not canonical.")
        if format(rebuilt.requested_quantity, "f") != requested_quantity:
            raise PaperOmsIntegrityError("Stored requested quantity is not canonical.")
        submitted_at = _stored_timestamp(row, "submitted_at")
        previous_time = submitted_at
        expected_order_revision = 1
        for event_row in event_rows:
            event = self._event_from_row(event_row)
            if event.order_revision != expected_order_revision:
                raise PaperOmsIntegrityError("Paper order event revisions are not gapless.")
            if event.occurred_at < previous_time:
                raise PaperOmsIntegrityError(
                    "Paper order event occurrence times are not monotonic."
                )
            if isinstance(event.order_event, OrderFillEvent) and (
                event.execution_source != execution_source
                or event.ledger_fill is None
                or event.ledger_fill.symbol != symbol
                or event.ledger_fill.side != side
                or event.ledger_fill.account_id != account_id
            ):
                raise PaperOmsIntegrityError("Stored fill differs from its order routing metadata.")
            try:
                next_state = reduce_order(rebuilt, event.order_event)
            except (
                ValidationError,
                ValueError,
                OrderIdempotencyConflictError,
                OrderInvariantError,
                OrderTransitionError,
            ) as error:
                raise PaperOmsIntegrityError(
                    "Stored event history cannot rebuild its order."
                ) from error
            if next_state.revision != expected_order_revision:
                raise PaperOmsIntegrityError(
                    "Stored order event did not advance exactly one revision."
                )
            rebuilt = next_state
            previous_time = event.occurred_at
            expected_order_revision += 1
        stored_revision = _stored_integer(row, "order_revision")
        if rebuilt.revision != stored_revision:
            raise PaperOmsIntegrityError("Stored order revision differs from its event history.")
        stored_payload = _stored_text(row, "order_state_payload")
        if _payload_hash(stored_payload) != _stored_text(row, "order_state_hash"):
            raise PaperOmsIntegrityError("Stored order-state hash does not match its payload.")
        stored_state = _parse_model_payload(
            stored_payload,
            OrderState,
            label="paper order state",
        )
        if stored_state != rebuilt:
            raise PaperOmsIntegrityError("Stored paper order projection differs from event replay.")
        risk_evaluation = self._allowed_risk_for_order(
            connection,
            account_id,
            order_id,
        )
        try:
            return PaperOrderRecord.model_validate(
                {
                    "account_id": account_id,
                    "order_id": order_id,
                    "symbol": symbol,
                    "side": side,
                    "execution_source": execution_source,
                    "state": stored_state,
                    "risk_evaluation": risk_evaluation,
                    "submitted_at": submitted_at,
                    "committed_at": _stored_timestamp(row, "committed_at"),
                    "updated_at": _stored_timestamp(row, "updated_at"),
                    "updated_committed_at": _stored_timestamp(
                        row,
                        "updated_committed_at",
                    ),
                }
            )
        except (ValidationError, ValueError) as error:
            raise PaperOmsIntegrityError("Stored paper order record is invalid.") from error

    def _validate_all(self, connection: sqlite3.Connection) -> None:
        account_ids = [
            _stored_text(row, "account_id")
            for row in connection.execute(
                "SELECT account_id FROM paper_oms_accounts ORDER BY account_id"
            ).fetchall()
        ]
        for account_id in account_ids:
            self._load_account(connection, account_id)
        order_keys = [
            (
                _stored_text(row, "account_id"),
                _stored_text(row, "order_id"),
            )
            for row in connection.execute(
                """
                SELECT account_id, order_id
                FROM paper_oms_orders
                ORDER BY account_id, order_id
                """
            ).fetchall()
        ]
        for account_id, order_id in order_keys:
            self._load_order(connection, account_id, order_id)
        command_rows = connection.execute(
            """
            SELECT *
            FROM paper_oms_commands
            ORDER BY command_namespace, idempotency_key
            """
        ).fetchall()
        for row in command_rows:
            command = self._validate_command_row(row)
            event_id = _stored_optional_integer(row, "result_event_id")
            if event_id is not None:
                event = self._event_by_id(connection, event_id)
                if (
                    event.account_id != command.account_id
                    or not isinstance(
                        command,
                        (RecordPaperOrderEventCommand, RecordPaperFillCommand),
                    )
                    or event.order_id != command.order_id
                    or event.order_revision != _stored_integer(row, "result_order_revision")
                    or (
                        event.account_revision
                        if event.account_revision is not None
                        else _stored_integer(row, "result_account_revision")
                    )
                    != _stored_integer(row, "result_account_revision")
                ):
                    raise PaperOmsIntegrityError(
                        "Stored command receipt differs from its result event."
                    )
                if isinstance(command, RecordPaperFillCommand):
                    event_row = connection.execute(
                        """
                        SELECT fill_economic_hash
                        FROM paper_oms_order_events
                        WHERE event_id = ?
                        """,
                        (event.event_id,),
                    ).fetchone()
                    if event_row is None or _stored_text(
                        event_row, "fill_economic_hash"
                    ) != _fill_economic_hash(command):
                        raise PaperOmsIntegrityError(
                            "Stored fill command alias differs from its economic event."
                        )
        risk_rows = connection.execute(
            """
            SELECT *
            FROM paper_oms_order_risk_evaluations
            ORDER BY evaluation_id
            """
        ).fetchall()
        for row in risk_rows:
            evaluation = self._risk_evaluation_from_row(connection, row)
            command_row = connection.execute(
                """
                SELECT *
                FROM paper_oms_commands
                WHERE command_namespace = ? AND idempotency_key = ?
                """,
                (
                    evaluation.command.command_namespace,
                    evaluation.command.idempotency_key,
                ),
            ).fetchone()
            if evaluation.outcome == "allow":
                if command_row is None:
                    raise PaperOmsIntegrityError(
                        "Allowed order-risk evaluation has no submit receipt."
                    )
                stored_command = self._validate_command_row(command_row)
                if (
                    not isinstance(stored_command, SubmitPaperOrderCommand)
                    or stored_command != evaluation.command
                ):
                    raise PaperOmsIntegrityError(
                        "Allowed order-risk evaluation differs from its submit receipt."
                    )
            elif command_row is not None:
                raise PaperOmsIntegrityError(
                    "Rejected order-risk evaluation unexpectedly created a command receipt."
                )
        event_rows = connection.execute(
            "SELECT * FROM paper_oms_order_events ORDER BY event_id"
        ).fetchall()
        for row in event_rows:
            event = self._event_from_row(row)
            origin = connection.execute(
                """
                SELECT *
                FROM paper_oms_commands
                WHERE command_namespace = ? AND idempotency_key = ?
                """,
                (event.command_namespace, event.idempotency_key),
            ).fetchone()
            if origin is None:
                raise PaperOmsIntegrityError(
                    "A paper order event has no originating command receipt."
                )
            command = self._validate_command_row(origin)
            if _stored_optional_integer(origin, "result_event_id") != event.event_id:
                raise PaperOmsIntegrityError("An originating command does not reference its event.")
            if isinstance(command, RecordPaperFillCommand):
                if not isinstance(event.order_event, OrderFillEvent) or _stored_text(
                    row, "fill_economic_hash"
                ) != _fill_economic_hash(command):
                    raise PaperOmsIntegrityError(
                        "A stored fill event differs from its economic command."
                    )
            elif not isinstance(command, RecordPaperOrderEventCommand):
                raise PaperOmsIntegrityError("A stored order event has an invalid command kind.")

    @staticmethod
    def _build_order_event(
        command: RecordPaperOrderEventCommand,
    ) -> OrderEvent:
        base = {
            "order_id": command.order_id,
            "idempotency_key": _internal_command_key(
                command.command_namespace,
                command.idempotency_key,
            ),
        }
        if command.event_type == "acknowledged":
            return OrderAcknowledgedEvent.model_validate(base)
        if command.event_type == "cancel_requested":
            return OrderCancelRequestedEvent.model_validate(base)
        if command.event_type == "cancel_acknowledged":
            return OrderCancelAcknowledgedEvent.model_validate(base)
        if command.event_type == "cancel_rejected":
            return OrderCancelRejectedEvent.model_validate({**base, "reason": command.reason})
        if command.event_type == "rejected":
            return OrderRejectedEvent.model_validate({**base, "reason": command.reason})
        return OrderExpiredEvent.model_validate({**base, "reason": command.reason})

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        command: RecordPaperOrderEventCommand | RecordPaperFillCommand,
        order_revision: int,
        account_revision: int | None,
        order_event: OrderEvent,
        ledger_fill: PaperFillEvent | None,
        fill_economic_hash: str | None,
        fill_execution_evidence: PaperOmsFillExecutionEvidence | None,
        received_at: datetime,
        committed_at: datetime,
    ) -> int:
        order_payload = _canonical_payload(order_event)
        ledger_payload = _canonical_payload(ledger_fill) if ledger_fill is not None else None
        execution_payload = (
            _canonical_payload(fill_execution_evidence)
            if fill_execution_evidence is not None
            else None
        )
        if committed_at < received_at:
            raise PaperOmsIntegrityError("Event commit time precedes server receive time.")
        try:
            cursor = connection.execute(
                """
                INSERT INTO paper_oms_order_events (
                    account_id,
                    order_id,
                    order_revision,
                    account_revision,
                    origin_namespace,
                    origin_idempotency_key,
                    event_type,
                    order_event_payload,
                    order_event_hash,
                    ledger_event_payload,
                    ledger_event_hash,
                    execution_source,
                    external_fill_id,
                    fill_economic_hash,
                    fill_execution_evidence_payload,
                    fill_execution_evidence_hash,
                    fill_evidence_generation,
                    occurred_at,
                    received_at,
                    committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.account_id,
                    command.order_id,
                    order_revision,
                    account_revision,
                    command.command_namespace,
                    command.idempotency_key,
                    order_event.event_type,
                    order_payload,
                    order_event.payload_hash,
                    ledger_payload,
                    ledger_fill.event_hash if ledger_fill is not None else None,
                    command.execution_source
                    if isinstance(command, RecordPaperFillCommand)
                    else None,
                    command.external_fill_id
                    if isinstance(command, RecordPaperFillCommand)
                    else None,
                    fill_economic_hash,
                    execution_payload,
                    (
                        fill_execution_evidence.evidence_hash
                        if fill_execution_evidence is not None
                        else None
                    ),
                    1 if fill_execution_evidence is not None else 0,
                    command.occurred_at.isoformat(),
                    received_at.isoformat(),
                    committed_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise PaperOmsConflictError(
                "Order event identity, revision, or fill identity conflicts."
            ) from error
        event_id = cursor.lastrowid
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 1:
            raise PaperOmsIntegrityError("SQLite did not issue a valid event identity.")
        return event_id

    @staticmethod
    def _update_order_projection(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        order_id: str,
        expected_revision: int,
        state: OrderState,
        updated_at: datetime,
        updated_committed_at: datetime,
    ) -> None:
        payload = _canonical_payload(state)
        cursor = connection.execute(
            """
            UPDATE paper_oms_orders
            SET
                order_revision = ?,
                order_state_payload = ?,
                order_state_hash = ?,
                updated_at = ?,
                updated_committed_at = ?
            WHERE
                account_id = ?
                AND order_id = ?
                AND order_revision = ?
            """,
            (
                state.revision,
                payload,
                _payload_hash(payload),
                updated_at.isoformat(),
                updated_committed_at.isoformat(),
                account_id,
                order_id,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise PaperOmsRevisionError(
                "Order revision changed before the event could be committed."
            )

    @staticmethod
    def _update_account_projection(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        expected_revision: int,
        ledger: PaperLedgerState,
        updated_at: datetime,
    ) -> None:
        payload = _canonical_payload(ledger)
        cursor = connection.execute(
            """
            UPDATE paper_oms_accounts
            SET
                ledger_revision = ?,
                ledger_state_payload = ?,
                ledger_state_hash = ?,
                updated_at = ?
            WHERE account_id = ? AND ledger_revision = ?
            """,
            (
                ledger.revision,
                payload,
                _payload_hash(payload),
                updated_at.isoformat(),
                account_id,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise PaperOmsRevisionError(
                "Account revision changed before the fill could be committed."
            )

    def create_account(
        self,
        command: CreatePaperAccountCommand,
    ) -> PaperAccountMutationResult:
        safe_command = _validated_model(
            command,
            CreatePaperAccountCommand,
            label="Create-account command",
        )
        received_at = self._receive_command(safe_command)
        with self._lock, self._write_transaction() as connection:
            existing = self._find_command(connection, safe_command)
            if existing is not None:
                account = self._load_account(connection, safe_command.account_id)
                return PaperAccountMutationResult(
                    account=account,
                    idempotent_replay=True,
                )
            if (
                connection.execute(
                    "SELECT 1 FROM paper_oms_accounts WHERE account_id = ?",
                    (safe_command.account_id,),
                ).fetchone()
                is not None
            ):
                raise PaperOmsConflictError(
                    f"Paper account {safe_command.account_id!r} already exists."
                )
            try:
                opening_event = build_opening_balance_event(
                    account_id=safe_command.account_id,
                    idempotency_key=_internal_command_key(
                        safe_command.command_namespace,
                        safe_command.idempotency_key,
                    ),
                    currency=safe_command.currency,
                    initial_cash=safe_command.initial_cash,
                    occurred_at=safe_command.occurred_at,
                )
                ledger = open_paper_ledger(opening_event)
            except (ValidationError, ValueError) as error:
                raise PaperOmsIntegrityError(
                    "Validated account command could not build its opening ledger."
                ) from error
            opening_payload = _canonical_payload(opening_event)
            ledger_payload = _canonical_payload(ledger)
            try:
                connection.execute(
                    """
                    INSERT INTO paper_oms_accounts (
                        account_id,
                        currency,
                        ledger_revision,
                        opening_event_payload,
                        opening_event_hash,
                        ledger_state_payload,
                        ledger_state_hash,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        safe_command.account_id,
                        safe_command.currency,
                        ledger.revision,
                        opening_payload,
                        opening_event.event_hash,
                        ledger_payload,
                        _payload_hash(ledger_payload),
                        safe_command.occurred_at.isoformat(),
                        safe_command.occurred_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise PaperOmsConflictError(
                    "Paper account identity conflicts with durable state."
                ) from error
            self._insert_command(
                connection,
                safe_command,
                result_account_revision=ledger.revision,
                result_order_revision=None,
                result_event_id=None,
                received_at=received_at,
            )
            account = self._load_account(connection, safe_command.account_id)
            return PaperAccountMutationResult(
                account=account,
                idempotent_replay=False,
            )

    @staticmethod
    def _risk_price_from_request(request: OrderRiskRequest) -> Decimal:
        if request.intent.order_type == "limit":
            limit_price = request.intent.limit_price
            if limit_price is None:  # pragma: no cover - risk model invariant
                raise PaperOmsIntegrityError("Limit risk intent lost its limit price.")
            if request.intent.side == "sell":
                return max(limit_price, request.price_evidence.reference_price)
            return limit_price
        if request.intent.side == "sell":
            return _exact_product(
                request.price_evidence.reference_price,
                _exact_difference(
                    Decimal(1),
                    request.rule_set.order_limits.market_order_price_buffer_ratio,
                ),
            )
        return _exact_product(
            request.price_evidence.reference_price,
            _exact_sum(
                Decimal(1),
                request.rule_set.order_limits.market_order_price_buffer_ratio,
            ),
        )

    @classmethod
    def _risk_price(cls, evaluation: PaperOrderRiskEvaluationRecord) -> Decimal:
        return cls._risk_price_from_request(evaluation.request)

    def _build_reservation_evidence(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        symbol: str,
        event_horizon_id: int | None = None,
        prior_evaluation_horizon_id: int | None = None,
    ) -> PaperOmsReservationStateEvidence:
        if event_horizon_id is None:
            row = connection.execute(
                "SELECT COALESCE(MAX(event_id), 0) AS horizon FROM paper_oms_order_events"
            ).fetchone()
            event_horizon_id = 0 if row is None else _stored_integer(row, "horizon")
        if prior_evaluation_horizon_id is None:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(evaluation_id), 0) AS horizon
                FROM paper_oms_order_risk_evaluations
                """
            ).fetchone()
            prior_evaluation_horizon_id = 0 if row is None else _stored_integer(row, "horizon")
        risk_rows = connection.execute(
            """
            SELECT *
            FROM paper_oms_order_risk_evaluations
            WHERE account_id = ? AND outcome = 'allow' AND evaluation_id <= ?
            ORDER BY order_id
            """,
            (account_id, prior_evaluation_horizon_id),
        ).fetchall()
        active: list[PaperOmsActiveReservationEvidence] = []
        reserved_buy_cash = Decimal(0)
        reserved_buy_quantity = Decimal(0)
        reserved_sell_quantity = Decimal(0)
        for risk_row in risk_rows:
            command_payload = _stored_text(risk_row, "command_payload")
            command = _parse_model_payload(
                command_payload,
                SubmitPaperOrderCommand,
                label="reservation-prefix submit command",
            )
            request_payload = _stored_text(risk_row, "risk_request_payload")
            request = _parse_model_payload(
                request_payload,
                OrderRiskRequest,
                label="reservation-prefix risk request",
            )
            decision_payload = _stored_text(risk_row, "risk_decision_payload")
            decision = _parse_model_payload(
                decision_payload,
                RiskDecision,
                label="reservation-prefix risk decision",
            )
            if (
                _payload_hash(command_payload) != _stored_text(risk_row, "command_hash")
                or request.request_hash != _stored_text(risk_row, "risk_request_hash")
                or decision.decision_hash != _stored_text(risk_row, "risk_decision_hash")
                or decision.request != request
                or evaluate_order_risk(request) != decision
                or command.account_id != account_id
                or command.order_id != _stored_text(risk_row, "order_id")
            ):
                raise PaperOmsIntegrityError("Reservation prefix contains invalid risk evidence.")
            order_row = connection.execute(
                """
                SELECT *
                FROM paper_oms_orders
                WHERE account_id = ? AND order_id = ?
                """,
                (account_id, command.order_id),
            ).fetchone()
            if order_row is None:
                raise PaperOmsIntegrityError(
                    "Allowed reservation-prefix risk evidence has no order."
                )
            try:
                state = create_order_state(
                    order_id=command.order_id,
                    requested_quantity=_stored_text(order_row, "requested_quantity"),
                )
            except (ValidationError, ValueError) as error:
                raise PaperOmsIntegrityError(
                    "Reservation-prefix order cannot be reconstructed."
                ) from error
            event_rows = connection.execute(
                """
                SELECT *
                FROM paper_oms_order_events
                WHERE account_id = ? AND order_id = ? AND event_id <= ?
                ORDER BY order_revision
                """,
                (account_id, command.order_id, event_horizon_id),
            ).fetchall()
            for event_row in event_rows:
                event = self._event_from_row(event_row)
                try:
                    state = reduce_order(state, event.order_event)
                except (
                    ValidationError,
                    ValueError,
                    OrderIdempotencyConflictError,
                    OrderInvariantError,
                    OrderTransitionError,
                ) as error:
                    raise PaperOmsIntegrityError(
                        "Reservation-prefix order events do not replay."
                    ) from error
            if state.status not in {
                "pending_submit",
                "working",
                "partially_filled",
                "pending_cancel",
            }:
                continue
            remaining = _exact_difference(
                state.requested_quantity,
                state.filled_quantity,
            )
            risk_price = self._risk_price_from_request(request)
            active.append(
                PaperOmsActiveReservationEvidence(
                    order_id=command.order_id,
                    symbol=command.symbol,
                    side=command.side,
                    order_revision=state.revision,
                    status=state.status,
                    remaining_quantity=remaining,
                    risk_decision_hash=decision.decision_hash,
                    risk_price=risk_price,
                )
            )
            if command.side == "buy":
                reserved_buy_cash = _exact_sum(
                    reserved_buy_cash,
                    _exact_product(
                        remaining,
                        risk_price,
                        _exact_sum(
                            Decimal(1),
                            request.rule_set.order_limits.order_fee_buffer_ratio,
                        ),
                    ),
                )
                if command.symbol == symbol:
                    reserved_buy_quantity = _exact_sum(
                        reserved_buy_quantity,
                        remaining,
                    )
            elif command.symbol == symbol:
                reserved_sell_quantity = _exact_sum(
                    reserved_sell_quantity,
                    remaining,
                )
        active.sort(key=lambda item: item.order_id)
        payload = {
            "schema_version": 1,
            "account_id_hash": self._account_id_hash(account_id),
            "target_symbol": symbol,
            "event_horizon_id": event_horizon_id,
            "prior_evaluation_horizon_id": prior_evaluation_horizon_id,
            "active_orders": tuple(active),
            "reserved_buy_cash": reserved_buy_cash,
            "reserved_buy_quantity": reserved_buy_quantity,
            "reserved_sell_quantity": reserved_sell_quantity,
            "active_order_count": len(active),
        }
        return PaperOmsReservationStateEvidence.model_validate(
            {
                **payload,
                "evidence_hash": canonical_payload_hash(payload),
            }
        )

    def _reservation_state(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        symbol: str,
        exclude_order_id: str | None = None,
    ) -> tuple[Decimal, Decimal, Decimal, int]:
        if exclude_order_id is None:
            evidence = self._build_reservation_evidence(
                connection,
                account_id=account_id,
                symbol=symbol,
            )
            return (
                evidence.reserved_buy_cash,
                evidence.reserved_buy_quantity,
                evidence.reserved_sell_quantity,
                evidence.active_order_count,
            )
        reserved_buy_cash = Decimal(0)
        reserved_buy_quantity = Decimal(0)
        reserved_sell_quantity = Decimal(0)
        active_order_count = 0
        order_ids = [
            _stored_text(row, "order_id")
            for row in connection.execute(
                """
                SELECT order_id
                FROM paper_oms_orders
                WHERE account_id = ?
                ORDER BY submitted_at, order_id
                """,
                (account_id,),
            ).fetchall()
        ]
        for order_id in order_ids:
            if order_id == exclude_order_id:
                continue
            order = self._load_order(connection, account_id, order_id)
            if order.state.status not in {
                "pending_submit",
                "working",
                "partially_filled",
                "pending_cancel",
            }:
                continue
            active_order_count += 1
            evaluation = order.risk_evaluation
            if evaluation is None:
                raise PaperOmsRiskUnavailableError(
                    "An active legacy order has no verifiable risk evidence; "
                    "cancel or terminate it before submitting another order."
                )
            remaining = _exact_difference(
                order.state.requested_quantity,
                order.state.filled_quantity,
            )
            if remaining <= 0:
                raise PaperOmsIntegrityError(
                    "An active order has no remaining reservable quantity."
                )
            if order.side == "buy":
                risk_price = self._risk_price(evaluation)
                reservation = _exact_product(
                    remaining,
                    risk_price,
                    _exact_sum(
                        Decimal(1),
                        evaluation.request.rule_set.order_limits.order_fee_buffer_ratio,
                    ),
                )
                reserved_buy_cash = _exact_sum(reserved_buy_cash, reservation)
                if order.symbol == symbol:
                    reserved_buy_quantity = _exact_sum(
                        reserved_buy_quantity,
                        remaining,
                    )
            elif order.symbol == symbol:
                reserved_sell_quantity = _exact_sum(
                    reserved_sell_quantity,
                    remaining,
                )
        return (
            reserved_buy_cash,
            reserved_buy_quantity,
            reserved_sell_quantity,
            active_order_count,
        )

    def _validate_fill_risk_envelope(
        self,
        connection: sqlite3.Connection,
        *,
        command: RecordPaperFillCommand,
        account: PaperAccountRecord,
        order: PaperOrderRecord,
    ) -> None:
        evaluation = order.risk_evaluation
        if evaluation is None:
            raise PaperOmsRiskUnavailableError(
                "Legacy orders without verifiable risk evidence cannot be filled."
            )
        if evaluation.market_evidence is None or evaluation.reservation_state is None:
            raise PaperOmsRiskUnavailableError(
                "Orders without schema-v3 market and reservation evidence cannot be filled."
            )
        if self._kill_switch_reader is None:
            raise PaperOmsRiskUnavailableError(
                "Server kill-switch authority is unavailable for fill execution."
            )
        try:
            switch = PaperOmsKillSwitchObservation.model_validate(
                self._kill_switch_reader().model_dump(mode="python")
            )
        except Exception as error:
            raise PaperOmsRiskUnavailableError(
                "Server kill-switch state is unavailable for fill execution."
            ) from error
        checked_at = self._now()
        maximum_switch_age = (
            evaluation.request.rule_set.order_limits.maximum_kill_switch_age_seconds
        )
        if (
            switch.snapshot.status != "clear"
            or switch.snapshot.scope != "global"
            or switch.available_at > checked_at
            or switch.observed_at > checked_at
            or (checked_at - switch.observed_at).total_seconds() > maximum_switch_age
        ):
            raise PaperOmsTransitionError(
                "Fill rejected because the current server kill switch is not "
                "fresh, global, and clear."
            )

        limits = evaluation.request.rule_set.order_limits
        approved_risk_price = self._risk_price(evaluation)
        if order.side == "buy" and command.fill_price > approved_risk_price:
            raise PaperOmsTransitionError(
                "Fill price exceeds the order's approved risk-price envelope."
            )
        if order.side == "sell" and command.fill_price < approved_risk_price:
            raise PaperOmsTransitionError(
                "Sell fill price is below the order's approved risk-price envelope."
            )
        remaining = _exact_difference(
            order.state.requested_quantity,
            order.state.filled_quantity,
        )
        projected_notional = _exact_sum(
            order.state.filled_notional,
            _exact_product(remaining, command.fill_price),
        )
        if projected_notional > limits.maximum_order_notional:
            raise PaperOmsTransitionError(
                "Fill would exceed the order's maximum-notional envelope."
            )
        current_gross = _exact_product(command.quantity, command.fill_price)
        maximum_current_fee = _exact_product(
            current_gross,
            limits.order_fee_buffer_ratio,
        )
        if command.fee > maximum_current_fee:
            raise PaperOmsTransitionError("Fill fee exceeds the order's approved fee buffer.")
        (
            reserved_buy_cash,
            reserved_buy_quantity,
            reserved_sell_quantity,
            _,
        ) = self._reservation_state(
            connection,
            account_id=command.account_id,
            symbol=order.symbol,
            exclude_order_id=order.order_id,
        )
        position_quantity = next(
            (
                position.quantity
                for position in account.ledger.positions
                if position.symbol == order.symbol
            ),
            Decimal(0),
        )
        if order.side == "buy":
            available_cash = _exact_difference(
                account.ledger.cash,
                reserved_buy_cash,
            )
            required_cash = _exact_product(
                remaining,
                command.fill_price,
                _exact_sum(Decimal(1), limits.order_fee_buffer_ratio),
            )
            if (
                required_cash > available_cash
                or _exact_difference(available_cash, required_cash) < limits.minimum_cash_reserve
            ):
                raise PaperOmsTransitionError(
                    "Fill would breach available cash or the minimum cash reserve."
                )
            resulting_position = _exact_sum(
                position_quantity,
                reserved_buy_quantity,
                remaining,
            )
            if resulting_position > limits.maximum_resulting_position:
                raise PaperOmsTransitionError("Fill would exceed the maximum resulting position.")
        else:
            available_position = _exact_difference(
                position_quantity,
                reserved_sell_quantity,
            )
            if remaining > available_position:
                raise PaperOmsTransitionError("Fill would exceed the unreserved long position.")

    @staticmethod
    def _quantity_matches_step(quantity: Decimal, step: Decimal) -> bool:
        ratio = Fraction(quantity) / Fraction(step)
        return ratio.denominator == 1

    @classmethod
    def _validate_market_filters(
        cls,
        *,
        quantity: Decimal,
        evidence: PaperOmsOrderMarketEvidence,
    ) -> None:
        rules = evidence.trading_rules
        quote = evidence.quote
        if not cls._quantity_matches_step(
            quantity, rules.lot_step_size
        ) or not cls._quantity_matches_step(quantity, rules.market_step_size):
            raise PaperOmsTransitionError(
                "Order quantity does not satisfy Binance LOT_SIZE and MARKET_LOT_SIZE steps."
            )
        if not (
            rules.lot_min_quantity <= quantity <= rules.lot_max_quantity
            and rules.market_min_quantity <= quantity <= rules.market_max_quantity
        ):
            raise PaperOmsTransitionError(
                "Order quantity is outside Binance market quantity filters."
            )
        notional = _exact_product(quantity, quote.notional_reference_price)
        if rules.min_notional_applies_to_market and notional < rules.min_notional:
            raise PaperOmsTransitionError(
                "Order notional is below Binance's active market minimum."
            )
        if (
            rules.max_notional_applies_to_market
            and rules.max_notional is not None
            and notional > rules.max_notional
        ):
            raise PaperOmsTransitionError("Order notional exceeds Binance's active market maximum.")
        if (
            rules.status != "TRADING"
            or not rules.spot_trading_allowed
            or "MARKET" not in rules.order_types
            or rules.verified_at > quote.observed_at
        ):
            raise PaperOmsTransitionError(
                "Binance market rules are not eligible at the trusted quote time."
            )

    def _validate_post_risk_timeline(
        self,
        *,
        command: RecordPaperOrderEventCommand | RecordPaperFillCommand,
        order: PaperOrderRecord,
        received_at: datetime,
    ) -> None:
        evaluation = order.risk_evaluation
        if evaluation is None:
            raise PaperOmsRiskUnavailableError(
                "Legacy orders without risk evidence cannot accept new events."
            )
        earliest = max(
            evaluation.request.evaluated_at,
            evaluation.request.price_evidence.available_at,
            evaluation.request.kill_switch_available_at,
            order.committed_at,
        )
        if received_at < earliest:
            raise PaperOmsIntegrityError(
                "Server received an order event before its approval became available."
            )
        if not self._allow_historical_client_timestamps and command.occurred_at < earliest:
            raise PaperOmsConflictError(
                "Order event occurrence time predates its server approval evidence."
            )

    def _evaluate_submit_order(
        self,
        connection: sqlite3.Connection,
        *,
        command: SubmitPaperOrderCommand,
        account: PaperAccountRecord,
        price_evidence: PaperOmsOrderMarketEvidence | None,
    ) -> PaperOrderRiskEvaluationRecord:
        if self._order_risk_rule_set is None or self._kill_switch_reader is None:
            raise PaperOmsRiskUnavailableError("Paper OMS order-risk policy is not configured.")
        supplied_market = price_evidence
        if supplied_market is None and self._price_evidence_reader is not None:
            try:
                supplied_market = self._price_evidence_reader(
                    command,
                    account.currency,
                )
            except Exception as error:
                raise PaperOmsRiskUnavailableError(
                    "Trusted paper OMS price evidence is unavailable."
                ) from error
        if supplied_market is None:
            raise PaperOmsRiskUnavailableError(
                "Trusted paper OMS price evidence is required for a new order."
            )
        try:
            safe_market = PaperOmsOrderMarketEvidence.model_validate(
                supplied_market.model_dump(mode="python")
            )
        except (ValidationError, ValueError, AttributeError) as error:
            raise PaperOmsRiskUnavailableError(
                "Trusted paper OMS price evidence is invalid."
            ) from error
        if (
            safe_market.side != command.side
            or safe_market.trading_rules.symbol != command.symbol
            or safe_market.trading_rules.quote_asset != account.currency
        ):
            raise PaperOmsRiskUnavailableError(
                "Trusted paper OMS market evidence belongs to a different order."
            )
        self._validate_market_filters(
            quantity=command.quantity,
            evidence=safe_market,
        )
        legacy_order_ids = [
            _stored_text(row, "order_id")
            for row in connection.execute(
                """
                SELECT order_id
                FROM paper_oms_orders
                WHERE account_id = ?
                ORDER BY order_id
                """,
                (command.account_id,),
            ).fetchall()
        ]
        for legacy_order_id in legacy_order_ids:
            legacy_order = self._load_order(
                connection,
                command.account_id,
                legacy_order_id,
            )
            if legacy_order.risk_evaluation is None and legacy_order.state.status in {
                "pending_submit",
                "working",
                "partially_filled",
                "pending_cancel",
            }:
                raise PaperOmsRiskUnavailableError(
                    "An active legacy order has no verifiable risk evidence; "
                    "cancel or terminate it before submitting another order."
                )
        try:
            kill_switch = PaperOmsKillSwitchObservation.model_validate(
                self._kill_switch_reader().model_dump(mode="python")
            )
        except Exception as error:
            raise PaperOmsRiskUnavailableError(
                "Server kill-switch state is unavailable."
            ) from error
        evaluated_at = self._now()
        if command.occurred_at > evaluated_at:
            raise PaperOmsConflictError(
                "Order occurrence time cannot be later than the server risk clock."
            )
        reservation_state = self._build_reservation_evidence(
            connection,
            account_id=command.account_id,
            symbol=command.symbol,
        )
        position_quantity = next(
            (
                position.quantity
                for position in account.ledger.positions
                if position.symbol == command.symbol
            ),
            Decimal(0),
        )
        ledger_payload = _canonical_payload(account.ledger)
        try:
            request = build_order_risk_request(
                evaluation_id=_internal_command_key(
                    command.command_namespace,
                    command.idempotency_key,
                ),
                evaluated_at=evaluated_at,
                source_calculation_version="paper-oms.order-risk.v1",
                rule_set=self._order_risk_rule_set,
                kill_switch=kill_switch.snapshot,
                kill_switch_observed_at=kill_switch.observed_at,
                kill_switch_available_at=kill_switch.available_at,
                intent=OrderRiskIntent(
                    order_id=command.order_id,
                    symbol=command.symbol,
                    quote_currency=account.currency,
                    side=command.side,
                    order_type="market",
                    quantity=command.quantity,
                    limit_price=None,
                ),
                state=OrderRiskState(
                    account_id_hash=self._account_id_hash(command.account_id),
                    account_revision=account.ledger.revision,
                    account_state_hash=_payload_hash(ledger_payload),
                    symbol=command.symbol,
                    quote_currency=account.currency,
                    cash_balance=account.ledger.cash,
                    position_quantity=position_quantity,
                    reserved_buy_cash=reservation_state.reserved_buy_cash,
                    reserved_buy_quantity=reservation_state.reserved_buy_quantity,
                    reserved_sell_quantity=reservation_state.reserved_sell_quantity,
                    active_order_count=reservation_state.active_order_count,
                ),
                price_evidence=safe_market.price_evidence,
            )
            decision = evaluate_order_risk(request)
        except (ValidationError, ValueError) as error:
            raise PaperOmsRiskUnavailableError(
                "Paper OMS could not construct a valid pre-trade risk request."
            ) from error
        return self._insert_risk_evaluation(
            connection,
            command=command,
            request=request,
            decision=decision,
            market_evidence=safe_market,
            reservation_state=reservation_state,
            recorded_at=self._now(),
        )

    def replay_order_submission(
        self,
        command: SubmitPaperOrderCommand,
    ) -> PaperOrderMutationResult | None:
        """Return a prior allow/reject before any provider call, if one exists."""

        safe_command = _validated_model(
            command,
            SubmitPaperOrderCommand,
            label="Submit-order command",
        )
        with self._lock, self._read_transaction() as connection:
            existing_command = self._find_command(connection, safe_command)
            if existing_command is not None:
                return PaperOrderMutationResult(
                    account=self._load_account(connection, safe_command.account_id),
                    order=self._load_order(
                        connection,
                        safe_command.account_id,
                        safe_command.order_id,
                    ),
                    idempotent_replay=True,
                )
            evaluation = self._find_risk_evaluation(connection, safe_command)
            if evaluation is None:
                return None
            if evaluation.outcome == "reject":
                raise PaperOmsRiskRejectedError(
                    evaluation,
                    idempotent_replay=True,
                )
            raise PaperOmsIntegrityError(
                "Allowed order-risk evaluation has no durable submit receipt."
            )

    def replay_fill_identity(
        self,
        *,
        command_namespace: str,
        idempotency_key: str,
        account_id: str,
        order_id: str,
        expected_order_revision: int,
        expected_account_revision: int,
        quantity: Decimal,
    ) -> PaperEventMutationResult | None:
        """Replay a server-constructed fill before fetching another quote."""

        namespace = _normalize_identifier(command_namespace, label="Command namespace")
        key = _normalize_identifier(idempotency_key, label="Idempotency key")
        account = _normalize_identifier(account_id, label="Paper account id")
        order = _normalize_identifier(order_id, label="Paper order id")
        safe_quantity = _normalize_decimal(quantity, label="Fill quantity")
        if safe_quantity <= 0:
            raise ValueError("Fill quantity must be positive.")
        if (
            isinstance(expected_order_revision, bool)
            or not isinstance(expected_order_revision, int)
            or expected_order_revision < 0
        ):
            raise ValueError("Expected order revision must be a non-negative integer.")
        if (
            isinstance(expected_account_revision, bool)
            or not isinstance(expected_account_revision, int)
            or expected_account_revision < 1
        ):
            raise ValueError("Expected account revision must be a positive integer.")
        with self._lock, self._read_transaction() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM paper_oms_commands
                WHERE command_namespace = ? AND idempotency_key = ?
                """,
                (namespace, key),
            ).fetchone()
            if row is None:
                return None
            command = self._validate_command_row(row)
            if (
                not isinstance(command, RecordPaperFillCommand)
                or command.account_id != account
                or command.order_id != order
                or command.quantity != safe_quantity
                or command.expected_order_revision != expected_order_revision
                or command.expected_account_revision != expected_account_revision
            ):
                raise PaperOmsConflictError(
                    "Fill idempotency identity is bound to a different request."
                )
            event_id = _stored_optional_integer(row, "result_event_id")
            if event_id is None:
                raise PaperOmsIntegrityError("Fill command receipt is missing its event identity.")
            return PaperEventMutationResult(
                account=self._load_account(connection, account),
                order=self._load_order(connection, account, order),
                event=self._event_by_id(connection, event_id),
                idempotent_replay=True,
            )

    def submit_order(
        self,
        command: SubmitPaperOrderCommand,
        *,
        price_evidence: PaperOmsOrderMarketEvidence | None = None,
    ) -> PaperOrderMutationResult:
        safe_command = _validated_model(
            command,
            SubmitPaperOrderCommand,
            label="Submit-order command",
        )
        received_at = self._receive_command(safe_command)
        rejection: PaperOrderRiskEvaluationRecord | None = None
        rejection_replay = False
        mutation: PaperOrderMutationResult | None = None
        with self._lock, self._write_transaction() as connection:
            existing = self._find_command(connection, safe_command)
            if existing is not None:
                account = self._load_account(connection, safe_command.account_id)
                order = self._load_order(
                    connection,
                    safe_command.account_id,
                    safe_command.order_id,
                )
                mutation = PaperOrderMutationResult(
                    account=account,
                    order=order,
                    idempotent_replay=True,
                )
            else:
                prior_evaluation = self._find_risk_evaluation(
                    connection,
                    safe_command,
                )
                if prior_evaluation is not None:
                    if prior_evaluation.outcome == "reject":
                        rejection = prior_evaluation
                        rejection_replay = True
                    else:
                        raise PaperOmsIntegrityError(
                            "Allowed order-risk evaluation has no durable submit receipt."
                        )
                else:
                    account = self._load_account(connection, safe_command.account_id)
                    if safe_command.occurred_at < account.created_at:
                        raise PaperOmsConflictError(
                            "Order submission cannot precede account creation."
                        )
                    if (
                        connection.execute(
                            """
                            SELECT 1
                            FROM paper_oms_orders
                            WHERE account_id = ? AND order_id = ?
                            """,
                            (safe_command.account_id, safe_command.order_id),
                        ).fetchone()
                        is not None
                    ):
                        raise PaperOmsConflictError(
                            "Paper order identity already exists in this account."
                        )
                    risk_evaluation = self._evaluate_submit_order(
                        connection,
                        command=safe_command,
                        account=account,
                        price_evidence=price_evidence,
                    )
                    if risk_evaluation.outcome == "reject":
                        rejection = risk_evaluation
                    else:
                        try:
                            state = create_order_state(
                                order_id=safe_command.order_id,
                                requested_quantity=safe_command.quantity,
                            )
                        except (ValidationError, ValueError) as error:
                            raise PaperOmsIntegrityError(
                                "Validated order command could not create its state."
                            ) from error
                        state_payload = _canonical_payload(state)
                        committed_at = self._now()
                        if committed_at < received_at:
                            raise PaperOmsIntegrityError(
                                "Order commit time precedes server receive time."
                            )
                        try:
                            connection.execute(
                                """
                                INSERT INTO paper_oms_orders (
                                    account_id,
                                    order_id,
                                    symbol,
                                    side,
                                    execution_source,
                                    requested_quantity,
                                    order_revision,
                                    order_state_payload,
                                    order_state_hash,
                                    submitted_at,
                                    committed_at,
                                    updated_at,
                                    updated_committed_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    safe_command.account_id,
                                    safe_command.order_id,
                                    safe_command.symbol,
                                    safe_command.side,
                                    safe_command.execution_source,
                                    format(safe_command.quantity, "f"),
                                    state.revision,
                                    state_payload,
                                    _payload_hash(state_payload),
                                    safe_command.occurred_at.isoformat(),
                                    committed_at.isoformat(),
                                    safe_command.occurred_at.isoformat(),
                                    committed_at.isoformat(),
                                ),
                            )
                        except sqlite3.IntegrityError as error:
                            raise PaperOmsConflictError(
                                "Paper order identity conflicts with durable state."
                            ) from error
                        self._insert_command(
                            connection,
                            safe_command,
                            result_account_revision=account.ledger.revision,
                            result_order_revision=state.revision,
                            result_event_id=None,
                            received_at=received_at,
                        )
                        order = self._load_order(
                            connection,
                            safe_command.account_id,
                            safe_command.order_id,
                        )
                        mutation = PaperOrderMutationResult(
                            account=account,
                            order=order,
                            idempotent_replay=False,
                        )
        if rejection is not None:
            raise PaperOmsRiskRejectedError(
                rejection,
                idempotent_replay=rejection_replay,
            )
        if mutation is None:
            raise PaperOmsIntegrityError(
                "Order submission produced neither a rejection nor an order."
            )
        return mutation

    def record_order_event(
        self,
        command: RecordPaperOrderEventCommand,
    ) -> PaperEventMutationResult:
        safe_command = _validated_model(
            command,
            RecordPaperOrderEventCommand,
            label="Record-order-event command",
        )
        received_at = self._receive_command(safe_command)
        with self._lock, self._write_transaction() as connection:
            existing = self._find_command(connection, safe_command)
            if existing is not None:
                event_id = _stored_optional_integer(existing, "result_event_id")
                if event_id is None:
                    raise PaperOmsIntegrityError(
                        "Event command receipt is missing its event identity."
                    )
                return PaperEventMutationResult(
                    account=self._load_account(connection, safe_command.account_id),
                    order=self._load_order(
                        connection,
                        safe_command.account_id,
                        safe_command.order_id,
                    ),
                    event=self._event_by_id(connection, event_id),
                    idempotent_replay=True,
                )
            account = self._load_account(connection, safe_command.account_id)
            order = self._load_order(
                connection,
                safe_command.account_id,
                safe_command.order_id,
            )
            if order.state.revision != safe_command.expected_order_revision:
                raise PaperOmsRevisionError("Expected order revision does not match durable state.")
            self._validate_post_risk_timeline(
                command=safe_command,
                order=order,
                received_at=received_at,
            )
            if safe_command.occurred_at < order.updated_at:
                raise PaperOmsConflictError(
                    "Order event time cannot precede the durable order history."
                )
            event = self._build_order_event(safe_command)
            try:
                next_state = reduce_order(order.state, event)
            except OrderIdempotencyConflictError as error:
                raise PaperOmsConflictError(str(error)) from error
            except (OrderInvariantError, OrderTransitionError) as error:
                raise PaperOmsTransitionError(str(error)) from error
            if next_state.revision != order.state.revision + 1:
                raise PaperOmsIntegrityError(
                    "A new order event did not advance exactly one revision."
                )
            committed_at = self._now()
            event_id = self._insert_event(
                connection,
                command=safe_command,
                order_revision=next_state.revision,
                account_revision=None,
                order_event=event,
                ledger_fill=None,
                fill_economic_hash=None,
                fill_execution_evidence=None,
                received_at=received_at,
                committed_at=committed_at,
            )
            self._update_order_projection(
                connection,
                account_id=safe_command.account_id,
                order_id=safe_command.order_id,
                expected_revision=order.state.revision,
                state=next_state,
                updated_at=safe_command.occurred_at,
                updated_committed_at=committed_at,
            )
            self._insert_command(
                connection,
                safe_command,
                result_account_revision=account.ledger.revision,
                result_order_revision=next_state.revision,
                result_event_id=event_id,
                received_at=received_at,
            )
            return PaperEventMutationResult(
                account=self._load_account(connection, safe_command.account_id),
                order=self._load_order(
                    connection,
                    safe_command.account_id,
                    safe_command.order_id,
                ),
                event=self._event_by_id(connection, event_id),
                idempotent_replay=False,
            )

    def record_fill(
        self,
        command: RecordPaperFillCommand,
        *,
        execution_evidence: PaperOmsFillExecutionEvidence | None = None,
    ) -> PaperEventMutationResult:
        safe_command = _validated_model(
            command,
            RecordPaperFillCommand,
            label="Record-fill command",
        )
        received_at = self._receive_command(safe_command)
        economic_hash = _fill_economic_hash(safe_command)
        with self._lock, self._write_transaction() as connection:
            existing_command = connection.execute(
                """
                SELECT *
                FROM paper_oms_commands
                WHERE command_namespace = ? AND idempotency_key = ?
                """,
                (safe_command.command_namespace, safe_command.idempotency_key),
            ).fetchone()
            if existing_command is not None:
                stored_command = self._validate_command_row(existing_command)
                if (
                    not isinstance(stored_command, RecordPaperFillCommand)
                    or stored_command.account_id != safe_command.account_id
                    or stored_command.order_id != safe_command.order_id
                    or stored_command.quantity != safe_command.quantity
                    or stored_command.expected_order_revision
                    != safe_command.expected_order_revision
                    or stored_command.expected_account_revision
                    != safe_command.expected_account_revision
                ):
                    raise PaperOmsConflictError(
                        "Fill idempotency identity is bound to a different request."
                    )
                event_id = _stored_optional_integer(
                    existing_command,
                    "result_event_id",
                )
                if event_id is None:
                    raise PaperOmsIntegrityError(
                        "Fill command receipt is missing its event identity."
                    )
                return PaperEventMutationResult(
                    account=self._load_account(connection, safe_command.account_id),
                    order=self._load_order(
                        connection,
                        safe_command.account_id,
                        safe_command.order_id,
                    ),
                    event=self._event_by_id(connection, event_id),
                    idempotent_replay=True,
                )
            existing_fill_row = connection.execute(
                """
                SELECT *
                FROM paper_oms_order_events
                WHERE execution_source = ? AND external_fill_id = ?
                """,
                (
                    safe_command.execution_source,
                    safe_command.external_fill_id,
                ),
            ).fetchone()
            if existing_fill_row is not None:
                existing_fill = self._event_from_row(existing_fill_row)
                if (
                    existing_fill.account_id != safe_command.account_id
                    or existing_fill.order_id != safe_command.order_id
                    or _stored_text(existing_fill_row, "fill_economic_hash") != economic_hash
                ):
                    raise PaperOmsConflictError(
                        "Namespaced fill identity is already bound to different economics."
                    )
                self._insert_command(
                    connection,
                    safe_command,
                    result_account_revision=cast(
                        int,
                        existing_fill.account_revision,
                    ),
                    result_order_revision=existing_fill.order_revision,
                    result_event_id=existing_fill.event_id,
                    received_at=received_at,
                )
                return PaperEventMutationResult(
                    account=self._load_account(connection, safe_command.account_id),
                    order=self._load_order(
                        connection,
                        safe_command.account_id,
                        safe_command.order_id,
                    ),
                    event=existing_fill,
                    idempotent_replay=True,
                )
            account = self._load_account(connection, safe_command.account_id)
            order = self._load_order(
                connection,
                safe_command.account_id,
                safe_command.order_id,
            )
            supplied_execution = execution_evidence
            if supplied_execution is None and self._fill_evidence_reader is not None:
                try:
                    supplied_execution = self._fill_evidence_reader(
                        safe_command,
                        order,
                    )
                except Exception as error:
                    raise PaperOmsExecutionUnavailableError(
                        "Trusted simulated-fill evidence is unavailable."
                    ) from error
            if supplied_execution is None:
                raise PaperOmsExecutionUnavailableError(
                    "Trusted simulated-fill evidence is required."
                )
            try:
                safe_execution = PaperOmsFillExecutionEvidence.model_validate(
                    supplied_execution.model_dump(mode="python")
                )
            except (ValidationError, ValueError, AttributeError) as error:
                raise PaperOmsExecutionUnavailableError(
                    "Trusted simulated-fill evidence is invalid."
                ) from error
            approval = order.risk_evaluation
            if approval is None or approval.market_evidence is None:
                raise PaperOmsRiskUnavailableError(
                    "Fill execution has no schema-v3 approval evidence."
                )
            if (
                safe_execution.account_id != safe_command.account_id
                or safe_execution.order_id != safe_command.order_id
                or safe_execution.symbol != order.symbol
                or safe_execution.side != order.side
                or safe_execution.quantity != safe_command.quantity
                or safe_execution.execution_source != safe_command.execution_source
                or safe_execution.external_fill_id != safe_command.external_fill_id
                or safe_execution.reference_price != safe_command.reference_price
                or safe_execution.fill_price != safe_command.fill_price
                or safe_execution.fee != safe_command.fee
                or safe_execution.approval_evidence_hash != approval.market_evidence.evidence_hash
                or safe_execution.available_at > received_at
                or safe_execution.observed_at > received_at
            ):
                raise PaperOmsExecutionUnavailableError(
                    "Trusted simulated-fill evidence differs from the durable order or command."
                )
            if (
                not self._allow_historical_client_timestamps
                and safe_command.occurred_at < safe_execution.available_at
            ):
                raise PaperOmsConflictError(
                    "Fill occurrence time predates its trusted execution evidence."
                )
            market_evidence_payload = {
                "schema_version": 1,
                "contract": "quantsieve.paper-oms.order-market-evidence.v1",
                "side": order.side,
                "trading_rules": safe_execution.trading_rules,
                "quote": safe_execution.quote,
                "price_evidence": OrderPriceEvidence(
                    symbol=order.symbol,
                    quote_currency=safe_execution.trading_rules.quote_asset,
                    reference_price=safe_execution.reference_price,
                    observed_at=safe_execution.observed_at,
                    available_at=safe_execution.available_at,
                    source="quantsieve.paper-oms.simulated-fill",
                    snapshot_hash=canonical_payload_hash(
                        {
                            "contract": "quantsieve.paper-oms.binance-market-snapshot.v1",
                            "rules": safe_execution.trading_rules,
                            "quote": safe_execution.quote,
                        }
                    ),
                ),
                "rules_snapshot_hash": safe_execution.rules_snapshot_hash,
                "quote_snapshot_hash": safe_execution.quote_snapshot_hash,
            }
            fill_market_evidence = PaperOmsOrderMarketEvidence.model_validate(
                {
                    **market_evidence_payload,
                    "evidence_hash": canonical_payload_hash(market_evidence_payload),
                }
            )
            self._validate_market_filters(
                quantity=order.state.requested_quantity,
                evidence=fill_market_evidence,
            )
            if order.execution_source != safe_command.execution_source:
                raise PaperOmsConflictError(
                    "Fill execution source differs from the submitted order."
                )
            if order.state.revision != safe_command.expected_order_revision:
                raise PaperOmsRevisionError("Expected order revision does not match durable state.")
            if account.ledger.revision != safe_command.expected_account_revision:
                raise PaperOmsRevisionError(
                    "Expected account revision does not match durable state."
                )
            self._validate_post_risk_timeline(
                command=safe_command,
                order=order,
                received_at=received_at,
            )
            if (
                safe_command.occurred_at < order.updated_at
                or safe_command.occurred_at < account.ledger.transactions[-1].occurred_at
            ):
                raise PaperOmsConflictError(
                    "Fill time cannot precede durable order or ledger history."
                )
            self._validate_fill_risk_envelope(
                connection,
                command=safe_command,
                account=account,
                order=order,
            )
            internal_key = _internal_command_key(
                safe_command.command_namespace,
                safe_command.idempotency_key,
            )
            namespaced_fill_id = _internal_fill_id(
                safe_command.execution_source,
                safe_command.external_fill_id,
            )
            try:
                order_event = OrderFillEvent.model_validate(
                    {
                        "order_id": safe_command.order_id,
                        "idempotency_key": internal_key,
                        "fill_id": namespaced_fill_id,
                        "fill_quantity": safe_command.quantity,
                        "fill_price": safe_command.fill_price,
                    }
                )
                ledger_fill = build_paper_fill_event(
                    account_id=safe_command.account_id,
                    expected_revision=safe_command.expected_account_revision,
                    idempotency_key=internal_key,
                    fill_id=namespaced_fill_id,
                    order_id=safe_command.order_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=safe_command.quantity,
                    reference_price=safe_command.reference_price,
                    fill_price=safe_command.fill_price,
                    fee=safe_command.fee,
                    occurred_at=safe_command.occurred_at,
                )
            except (ValidationError, ValueError) as error:
                raise PaperOmsTransitionError(
                    "Fill economics do not satisfy the paper-ledger contract."
                ) from error
            try:
                next_order = reduce_order(order.state, order_event)
            except OrderIdempotencyConflictError as error:
                raise PaperOmsConflictError(str(error)) from error
            except (OrderInvariantError, OrderTransitionError) as error:
                raise PaperOmsTransitionError(str(error)) from error
            try:
                ledger_result = apply_paper_fill(account.ledger, ledger_fill)
            except InsufficientPaperCashError as error:
                raise PaperOmsInsufficientCashError(str(error)) from error
            except InsufficientPaperPositionError as error:
                raise PaperOmsInsufficientPositionError(str(error)) from error
            except PaperLedgerConflictError as error:
                raise PaperOmsConflictError(str(error)) from error
            except PaperLedgerRevisionError as error:
                raise PaperOmsRevisionError(str(error)) from error
            if (
                next_order.revision != order.state.revision + 1
                or ledger_result.idempotent_replay
                or ledger_result.state.revision != account.ledger.revision + 1
            ):
                raise PaperOmsIntegrityError(
                    "A new fill did not advance order and account exactly once."
                )
            committed_at = self._now()
            event_id = self._insert_event(
                connection,
                command=safe_command,
                order_revision=next_order.revision,
                account_revision=ledger_result.state.revision,
                order_event=order_event,
                ledger_fill=ledger_fill,
                fill_economic_hash=economic_hash,
                fill_execution_evidence=safe_execution,
                received_at=received_at,
                committed_at=committed_at,
            )
            self._update_order_projection(
                connection,
                account_id=safe_command.account_id,
                order_id=safe_command.order_id,
                expected_revision=order.state.revision,
                state=next_order,
                updated_at=safe_command.occurred_at,
                updated_committed_at=committed_at,
            )
            self._update_account_projection(
                connection,
                account_id=safe_command.account_id,
                expected_revision=account.ledger.revision,
                ledger=ledger_result.state,
                updated_at=safe_command.occurred_at,
            )
            self._insert_command(
                connection,
                safe_command,
                result_account_revision=ledger_result.state.revision,
                result_order_revision=next_order.revision,
                result_event_id=event_id,
                received_at=received_at,
            )
            return PaperEventMutationResult(
                account=self._load_account(connection, safe_command.account_id),
                order=self._load_order(
                    connection,
                    safe_command.account_id,
                    safe_command.order_id,
                ),
                event=self._event_by_id(connection, event_id),
                idempotent_replay=False,
            )

    def get_account(self, account_id: str) -> PaperAccountRecord:
        normalized = _normalize_identifier(account_id, label="Paper account id")
        with self._read_transaction() as connection:
            return self._load_account(connection, normalized)

    def get_order(self, account_id: str, order_id: str) -> PaperOrderRecord:
        normalized_account = _normalize_identifier(
            account_id,
            label="Paper account id",
        )
        normalized_order = _normalize_identifier(order_id, label="Paper order id")
        with self._read_transaction() as connection:
            return self._load_order(
                connection,
                normalized_account,
                normalized_order,
            )

    def get_order_risk_evaluation(
        self,
        *,
        command_namespace: str,
        idempotency_key: str,
        account_id: str,
    ) -> PaperOrderRiskEvaluationRecord:
        normalized_namespace = _normalize_identifier(
            command_namespace,
            label="Command namespace",
        )
        normalized_key = _normalize_identifier(
            idempotency_key,
            label="Idempotency key",
        )
        normalized_account = _normalize_identifier(
            account_id,
            label="Paper account id",
        )
        with self._read_transaction() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM paper_oms_order_risk_evaluations
                WHERE origin_namespace = ? AND origin_idempotency_key = ?
                """,
                (normalized_namespace, normalized_key),
            ).fetchone()
            if row is None:
                raise PaperOmsNotFoundError("Paper order-risk evaluation does not exist.")
            evaluation = self._risk_evaluation_from_row(connection, row)
            if evaluation.command.account_id != normalized_account:
                raise PaperOmsNotFoundError("Paper order-risk evaluation does not exist.")
            return evaluation

    def list_orders(self, account_id: str) -> tuple[PaperOrderRecord, ...]:
        normalized = _normalize_identifier(account_id, label="Paper account id")
        with self._read_transaction() as connection:
            self._load_account(connection, normalized)
            order_ids = [
                _stored_text(row, "order_id")
                for row in connection.execute(
                    """
                    SELECT order_id
                    FROM paper_oms_orders
                    WHERE account_id = ?
                    ORDER BY order_id
                    """,
                    (normalized,),
                ).fetchall()
            ]
            return tuple(
                self._load_order(connection, normalized, order_id) for order_id in order_ids
            )
