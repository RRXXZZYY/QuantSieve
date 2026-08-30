from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from typing import Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_HASH_PATTERN = r"^[0-9a-f]{64}$"
_ACCOUNT_PATTERN = re.compile(r"^[A-Z][A-Z0-9:_-]{0,199}$")
_READABLE_SYMBOL_COMPONENT_PATTERN = re.compile(r"^[A-Z0-9_-]+$")
_MAX_READABLE_SYMBOL_COMPONENT_LENGTH = 167
_STRICT_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    strict=True,
    revalidate_instances="always",
    allow_inf_nan=False,
)


class PaperLedgerError(RuntimeError):
    """Base error for a rejected paper-ledger transition."""


class PaperLedgerConflictError(PaperLedgerError):
    """Raised when an idempotency identity is reused for different economics."""


class PaperLedgerRevisionError(PaperLedgerError):
    """Raised when an economic event targets a stale ledger revision."""


class InsufficientPaperCashError(PaperLedgerError):
    """Raised when a modeled buy would make quote-currency cash negative."""


class InsufficientPaperPositionError(PaperLedgerError):
    """Raised when a modeled sell exceeds the available long position."""


class PaperLedgerReconciliationError(PaperLedgerError):
    """Raised when journal, lots, or materialized balances disagree."""


def _canonical_identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError(f"{label} must contain between 1 and 200 characters.")
    return normalized


def _canonical_symbol(value: object) -> str:
    return _canonical_identifier(value, "Paper-ledger symbol").upper()


def _canonical_currency(value: object) -> str:
    currency = _canonical_identifier(value, "Paper-ledger currency").upper()
    if len(currency) > 16 or not currency.replace("_", "").isalnum():
        raise ValueError("Paper-ledger currency is invalid.")
    return currency


def _canonical_account(value: object) -> str:
    account = _canonical_identifier(value, "Ledger account").upper()
    if _ACCOUNT_PATTERN.fullmatch(account) is None:
        raise ValueError("Ledger account contains unsupported characters.")
    return account


def _canonical_decimal(value: object, label: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError(f"{label} must be an exact finite decimal.")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, str)):
        try:
            result = Decimal(str(value))
        except Exception as error:
            raise ValueError(f"{label} must be an exact finite decimal.") from error
    else:
        raise ValueError(f"{label} must be an exact finite decimal.")
    if not result.is_finite():
        raise ValueError(f"{label} must be an exact finite decimal.")
    if result.is_zero():
        return Decimal(0)
    decimal_tuple = result.as_tuple()
    digits = list(decimal_tuple.digits)
    exponent = int(str(decimal_tuple.exponent))
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return Decimal((decimal_tuple.sign, tuple(digits), exponent))


def _fraction(value: Decimal) -> Fraction:
    if not value.is_finite():
        raise ValueError("Paper-ledger arithmetic requires finite decimals.")
    return Fraction(value)


def _terminating_decimal(value: Fraction) -> Decimal:
    """Convert a terminating rational without consulting Decimal's context."""

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
        raise ValueError("Paper-ledger arithmetic produced a non-terminating decimal.")
    scale = max(twos, fives)
    scaled_numerator = numerator
    scaled_numerator *= 5 ** (scale - fives)
    scaled_numerator *= 2 ** (scale - twos)
    sign = 1 if scaled_numerator < 0 else 0
    digits = tuple(int(digit) for digit in str(abs(scaled_numerator)))
    return _canonical_decimal(
        Decimal((sign, digits, -scale)),
        "Paper-ledger arithmetic result",
    )


def _exact_sum(*values: Decimal) -> Decimal:
    return _terminating_decimal(sum((_fraction(value) for value in values), Fraction()))


def _exact_product(*values: Decimal) -> Decimal:
    result = Fraction(1)
    for value in values:
        result *= _fraction(value)
    return _terminating_decimal(result)


def _exact_difference(minuend: Decimal, subtrahend: Decimal) -> Decimal:
    return _terminating_decimal(_fraction(minuend) - _fraction(subtrahend))


def _exact_negate(value: Decimal) -> Decimal:
    return _terminating_decimal(-_fraction(value))


def _aware_utc(value: object, label: str) -> datetime:
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


def _decimal_text(value: Decimal) -> str:
    canonical = _canonical_decimal(value, "Canonical decimal")
    return format(canonical, "f")


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return _json_ready(value.model_dump(mode="python"))
    if isinstance(value, Decimal):
        return {"$decimal": _decimal_text(value)}
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


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _cash_account(currency: str) -> str:
    return f"ASSET:CASH:{currency}"


def _capital_account(currency: str) -> str:
    return f"EQUITY:OPENING:{currency}"


def _fee_account(currency: str) -> str:
    return f"EXPENSE:FEE:{currency}"


def _symbol_account_component(symbol: str) -> str:
    if (
        len(symbol) <= _MAX_READABLE_SYMBOL_COMPONENT_LENGTH
        and _READABLE_SYMBOL_COMPONENT_PATTERN.fullmatch(symbol) is not None
    ):
        return symbol
    return f"H{hashlib.sha256(symbol.encode('utf-8')).hexdigest().upper()}"


def _inventory_account(symbol: str, currency: str) -> str:
    return f"ASSET:INVENTORY:{_symbol_account_component(symbol)}:{currency}"


def _realized_pnl_account(symbol: str, currency: str) -> str:
    return f"PNL:REALIZED:{_symbol_account_component(symbol)}:{currency}"


class LedgerPosting(BaseModel):
    """One signed quote-currency posting; debit is positive and credit negative."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[1] = 1
    account: str
    currency: str
    amount: Decimal

    @field_validator("account", mode="before")
    @classmethod
    def normalize_account(cls, value: object) -> str:
        return _canonical_account(value)

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> str:
        return _canonical_currency(value)

    @field_validator("amount", mode="before")
    @classmethod
    def normalize_amount(cls, value: object) -> Decimal:
        amount = _canonical_decimal(value, "Ledger posting amount")
        if amount == 0:
            raise ValueError("Ledger postings must be non-zero.")
        return amount


class JournalTransaction(BaseModel):
    """An exact balanced journal transaction produced by one economic event."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[1] = 1
    ledger_revision: int = Field(ge=1, strict=True)
    idempotency_key: str
    source_kind: Literal["opening_balance", "buy_fill", "sell_fill"]
    source_id: str
    source_hash: str = Field(pattern=_HASH_PATTERN)
    occurred_at: datetime
    postings: tuple[LedgerPosting, ...] = Field(min_length=2, max_length=5)
    transaction_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator("idempotency_key", "source_id", mode="before")
    @classmethod
    def normalize_identifier(cls, value: object) -> str:
        return _canonical_identifier(value, "Journal identity")

    @field_validator("occurred_at", mode="before")
    @classmethod
    def normalize_occurred_at(cls, value: object) -> datetime:
        return _aware_utc(value, "Journal occurrence time")

    @field_validator("postings", mode="before")
    @classmethod
    def revalidate_postings(cls, value: object) -> tuple[LedgerPosting, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError("Journal postings must be a sequence.")
        return tuple(
            LedgerPosting.model_validate(
                item.model_dump(mode="python") if isinstance(item, LedgerPosting) else item
            )
            for item in value
        )

    @model_validator(mode="after")
    def validate_transaction(self) -> Self:
        currencies = {posting.currency for posting in self.postings}
        if len(currencies) != 1:
            raise ValueError("A journal transaction must use one quote currency.")
        accounts = tuple(posting.account for posting in self.postings)
        if len(set(accounts)) != len(accounts):
            raise ValueError("A journal transaction cannot repeat an account.")
        if accounts != tuple(sorted(accounts)):
            raise ValueError("Journal postings must use canonical account order.")
        if _exact_sum(*(posting.amount for posting in self.postings)) != 0:
            raise ValueError("Journal transaction postings must balance exactly.")
        expected_hash = _canonical_hash(
            self.model_dump(mode="python", exclude={"transaction_hash"})
        )
        if self.transaction_hash != expected_hash:
            raise ValueError("Journal transaction hash does not match its payload.")
        return self


class PaperLot(BaseModel):
    """One immutable-origin FIFO lot with a mutable remaining projection."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[1] = 1
    id: str = Field(pattern=_HASH_PATTERN)
    symbol: str
    source_fill_id: str
    opened_revision: int = Field(ge=2, strict=True)
    opened_at: datetime
    unit_cost: Decimal
    original_quantity: Decimal
    remaining_quantity: Decimal
    original_cost: Decimal
    remaining_cost: Decimal
    lot_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _canonical_symbol(value)

    @field_validator("source_fill_id", mode="before")
    @classmethod
    def normalize_source_fill_id(cls, value: object) -> str:
        return _canonical_identifier(value, "Lot source fill id")

    @field_validator("opened_at", mode="before")
    @classmethod
    def normalize_opened_at(cls, value: object) -> datetime:
        return _aware_utc(value, "Lot opening time")

    @field_validator(
        "unit_cost",
        "original_quantity",
        "remaining_quantity",
        "original_cost",
        "remaining_cost",
        mode="before",
    )
    @classmethod
    def normalize_decimal(cls, value: object) -> Decimal:
        return _canonical_decimal(value, "Paper lot amount")

    @model_validator(mode="after")
    def validate_lot(self) -> Self:
        if self.unit_cost <= 0 or self.original_quantity <= 0:
            raise ValueError("A paper lot requires positive cost and original quantity.")
        if not 0 <= self.remaining_quantity <= self.original_quantity:
            raise ValueError("Paper lot remaining quantity is outside its original size.")
        if self.original_cost != _exact_product(
            self.original_quantity,
            self.unit_cost,
        ):
            raise ValueError("Paper lot original cost does not reconcile.")
        if self.remaining_cost != _exact_product(
            self.remaining_quantity,
            self.unit_cost,
        ):
            raise ValueError("Paper lot remaining cost does not reconcile.")
        if self.id != _canonical_hash(
            {
                "source_fill_id": self.source_fill_id,
                "symbol": self.symbol,
                "opened_revision": self.opened_revision,
                "opened_at": self.opened_at,
            }
        ):
            raise ValueError("Paper lot id does not match its immutable origin.")
        if self.lot_hash != _canonical_hash(self.model_dump(mode="python", exclude={"lot_hash"})):
            raise ValueError("Paper lot hash does not match its payload.")
        return self


class PaperLotConsumption(BaseModel):
    """Exact FIFO cost attributed from one lot to one modeled sell fill."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[1] = 1
    id: str = Field(pattern=_HASH_PATTERN)
    ledger_revision: int = Field(ge=2, strict=True)
    sequence: int = Field(ge=1, strict=True)
    event_idempotency_key: str
    fill_id: str
    lot_id: str = Field(pattern=_HASH_PATTERN)
    symbol: str
    quantity: Decimal
    cost_basis: Decimal
    consumption_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator("event_idempotency_key", "fill_id", mode="before")
    @classmethod
    def normalize_identifier(cls, value: object) -> str:
        return _canonical_identifier(value, "Lot-consumption identity")

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _canonical_symbol(value)

    @field_validator("quantity", "cost_basis", mode="before")
    @classmethod
    def normalize_decimal(cls, value: object) -> Decimal:
        return _canonical_decimal(value, "Lot-consumption amount")

    @model_validator(mode="after")
    def validate_consumption(self) -> Self:
        if self.quantity <= 0 or self.cost_basis <= 0:
            raise ValueError("Lot consumption quantity and cost must be positive.")
        expected_id = _canonical_hash(
            {
                "event_idempotency_key": self.event_idempotency_key,
                "fill_id": self.fill_id,
                "lot_id": self.lot_id,
                "sequence": self.sequence,
            }
        )
        if self.id != expected_id:
            raise ValueError("Lot-consumption id does not match its identity.")
        if self.consumption_hash != _canonical_hash(
            self.model_dump(mode="python", exclude={"consumption_hash"})
        ):
            raise ValueError("Lot-consumption hash does not match its payload.")
        return self


class PaperPosition(BaseModel):
    """FIFO lot projection for one long-only spot instrument."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[1] = 1
    symbol: str
    quantity: Decimal
    book_cost: Decimal
    lots: tuple[PaperLot, ...]

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _canonical_symbol(value)

    @field_validator("quantity", "book_cost", mode="before")
    @classmethod
    def normalize_decimal(cls, value: object) -> Decimal:
        return _canonical_decimal(value, "Paper position amount")

    @field_validator("lots", mode="before")
    @classmethod
    def revalidate_lots(cls, value: object) -> tuple[PaperLot, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError("Paper position lots must be a sequence.")
        return tuple(
            PaperLot.model_validate(
                item.model_dump(mode="python") if isinstance(item, PaperLot) else item
            )
            for item in value
        )

    @model_validator(mode="after")
    def validate_position(self) -> Self:
        if self.quantity < 0 or self.book_cost < 0:
            raise ValueError("Paper position quantity and book cost cannot be negative.")
        if any(lot.symbol != self.symbol for lot in self.lots):
            raise ValueError("Paper position contains a lot for another symbol.")
        lot_ids = tuple(lot.id for lot in self.lots)
        if len(set(lot_ids)) != len(lot_ids):
            raise ValueError("Paper position cannot repeat a lot.")
        canonical_order = tuple(sorted(self.lots, key=lambda lot: (lot.opened_revision, lot.id)))
        if self.lots != canonical_order:
            raise ValueError("Paper position lots must use deterministic FIFO order.")
        if self.quantity != _exact_sum(*(lot.remaining_quantity for lot in self.lots)):
            raise ValueError("Paper position quantity does not reconcile with its lots.")
        if self.book_cost != _exact_sum(*(lot.remaining_cost for lot in self.lots)):
            raise ValueError("Paper position book cost does not reconcile with its lots.")
        return self


class OpeningBalanceEvent(BaseModel):
    """Idempotent account-capital event used to create a paper ledger."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[1] = 1
    account_id: str
    idempotency_key: str
    currency: str
    initial_cash: Decimal
    occurred_at: datetime
    event_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator("account_id", "idempotency_key", mode="before")
    @classmethod
    def normalize_identifier(cls, value: object) -> str:
        return _canonical_identifier(value, "Opening event identity")

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> str:
        return _canonical_currency(value)

    @field_validator("initial_cash", mode="before")
    @classmethod
    def normalize_initial_cash(cls, value: object) -> Decimal:
        cash = _canonical_decimal(value, "Opening cash")
        if cash <= 0:
            raise ValueError("Opening cash must be positive.")
        return cash

    @field_validator("occurred_at", mode="before")
    @classmethod
    def normalize_occurred_at(cls, value: object) -> datetime:
        return _aware_utc(value, "Opening event time")

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.event_hash != _canonical_hash(
            self.model_dump(mode="python", exclude={"event_hash"})
        ):
            raise ValueError("Opening event hash does not match its payload.")
        return self


class PaperFillEvent(BaseModel):
    """One exact modeled fill, independently idempotent from its order."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[1] = 1
    account_id: str
    expected_revision: int = Field(ge=1, strict=True)
    idempotency_key: str
    fill_id: str
    order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: Decimal
    reference_price: Decimal
    fill_price: Decimal
    gross_notional: Decimal
    fee: Decimal
    slippage: Decimal
    occurred_at: datetime
    event_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator(
        "account_id",
        "idempotency_key",
        "fill_id",
        "order_id",
        mode="before",
    )
    @classmethod
    def normalize_identifier(cls, value: object) -> str:
        return _canonical_identifier(value, "Fill event identity")

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _canonical_symbol(value)

    @field_validator(
        "quantity",
        "reference_price",
        "fill_price",
        "gross_notional",
        "fee",
        "slippage",
        mode="before",
    )
    @classmethod
    def normalize_decimal(cls, value: object) -> Decimal:
        return _canonical_decimal(value, "Fill event amount")

    @field_validator("occurred_at", mode="before")
    @classmethod
    def normalize_occurred_at(cls, value: object) -> datetime:
        return _aware_utc(value, "Fill event time")

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.quantity <= 0:
            raise ValueError("Fill quantity must be positive.")
        if self.reference_price <= 0 or self.fill_price <= 0:
            raise ValueError("Fill prices must be positive.")
        if self.fee < 0 or self.slippage < 0:
            raise ValueError("Fill fee and slippage cannot be negative.")
        expected_notional = _exact_product(self.quantity, self.fill_price)
        if self.gross_notional != expected_notional:
            raise ValueError("Fill gross notional does not reconcile.")
        expected_slippage = _exact_product(
            self.quantity,
            _terminating_decimal(abs(_fraction(self.fill_price) - _fraction(self.reference_price))),
        )
        if self.slippage != expected_slippage:
            raise ValueError("Fill slippage does not reconcile.")
        maximum_fee = _exact_product(self.gross_notional, Decimal("0.1"))
        if self.fee > maximum_fee:
            raise ValueError("Fill fee exceeds the supported ten-percent bound.")
        if self.event_hash != _canonical_hash(
            self.model_dump(mode="python", exclude={"event_hash"})
        ):
            raise ValueError("Fill event hash does not match its payload.")
        return self


class PaperLedgerState(BaseModel):
    """Serializable exact paper account derived from balanced economic events."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[1] = 1
    account_id: str
    currency: str
    revision: int = Field(ge=1, strict=True)
    initial_cash: Decimal
    cash: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal
    positions: tuple[PaperPosition, ...]
    transactions: tuple[JournalTransaction, ...] = Field(min_length=1)
    lot_consumptions: tuple[PaperLotConsumption, ...] = ()
    applied_events: dict[str, str] = Field(min_length=1)

    @field_validator("account_id", mode="before")
    @classmethod
    def normalize_account_id(cls, value: object) -> str:
        return _canonical_identifier(value, "Paper-ledger account id")

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> str:
        return _canonical_currency(value)

    @field_validator(
        "initial_cash",
        "cash",
        "realized_pnl",
        "fees_paid",
        mode="before",
    )
    @classmethod
    def normalize_decimal(cls, value: object) -> Decimal:
        return _canonical_decimal(value, "Paper-ledger state amount")

    @field_validator("positions", mode="before")
    @classmethod
    def revalidate_positions(cls, value: object) -> tuple[PaperPosition, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError("Paper-ledger positions must be a sequence.")
        return tuple(
            PaperPosition.model_validate(
                item.model_dump(mode="python") if isinstance(item, PaperPosition) else item
            )
            for item in value
        )

    @field_validator("transactions", mode="before")
    @classmethod
    def revalidate_transactions(
        cls,
        value: object,
    ) -> tuple[JournalTransaction, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError("Paper-ledger transactions must be a sequence.")
        return tuple(
            JournalTransaction.model_validate(
                item.model_dump(mode="python") if isinstance(item, JournalTransaction) else item
            )
            for item in value
        )

    @field_validator("lot_consumptions", mode="before")
    @classmethod
    def revalidate_consumptions(
        cls,
        value: object,
    ) -> tuple[PaperLotConsumption, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError("Paper-ledger lot consumptions must be a sequence.")
        return tuple(
            PaperLotConsumption.model_validate(
                item.model_dump(mode="python") if isinstance(item, PaperLotConsumption) else item
            )
            for item in value
        )

    @field_validator("applied_events", mode="before")
    @classmethod
    def normalize_applied_events(cls, value: object) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("Applied events must be a mapping.")
        events: dict[str, str] = {}
        for raw_key, raw_hash in value.items():
            key = _canonical_identifier(raw_key, "Applied-event key")
            if not isinstance(raw_hash, str) or re.fullmatch(_HASH_PATTERN, raw_hash) is None:
                raise ValueError("Applied-event hash is invalid.")
            if key in events:
                raise ValueError("Applied-event keys must be unique.")
            events[key] = raw_hash
        return events

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.initial_cash <= 0 or self.cash < 0 or self.fees_paid < 0:
            raise ValueError("Paper-ledger cash and fees are outside their valid range.")
        if self.revision != len(self.transactions):
            raise ValueError("Ledger revision must equal its journal length.")
        if len(self.applied_events) != self.revision:
            raise ValueError("Every ledger revision requires one applied event.")
        revisions = tuple(transaction.ledger_revision for transaction in self.transactions)
        if revisions != tuple(range(1, self.revision + 1)):
            raise ValueError("Journal revisions must be gapless and ordered.")
        if self.transactions[0].source_kind != "opening_balance":
            raise ValueError("The first ledger transaction must be its opening balance.")
        if any(
            current.occurred_at < previous.occurred_at
            for previous, current in zip(
                self.transactions,
                self.transactions[1:],
                strict=False,
            )
        ):
            raise ValueError("Journal occurrence times must be monotonic.")
        transaction_keys = tuple(transaction.idempotency_key for transaction in self.transactions)
        if len(set(transaction_keys)) != len(transaction_keys):
            raise ValueError("Journal idempotency keys must be unique.")
        if set(transaction_keys) != set(self.applied_events):
            raise ValueError("Applied-event identities must exactly match the journal.")
        if any(
            self.applied_events[transaction.idempotency_key] != transaction.source_hash
            for transaction in self.transactions
        ):
            raise ValueError("Applied-event hashes must exactly match journal sources.")
        symbols = tuple(position.symbol for position in self.positions)
        if symbols != tuple(sorted(symbols)) or len(set(symbols)) != len(symbols):
            raise ValueError("Paper-ledger positions must use unique canonical symbol order.")
        consumption_ids = tuple(item.id for item in self.lot_consumptions)
        if len(set(consumption_ids)) != len(consumption_ids):
            raise ValueError("Paper-ledger lot consumptions must be unique.")
        if self.lot_consumptions != tuple(
            sorted(
                self.lot_consumptions,
                key=lambda item: (item.ledger_revision, item.sequence),
            )
        ):
            raise ValueError("Paper-ledger lot consumptions must use journal order.")
        _reconcile_state(self)
        return self


class PaperLedgerReconciliation(BaseModel):
    """Exact materialized balances independently reconstructed from the journal."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: Literal[1] = 1
    account_id: str
    revision: int = Field(ge=1, strict=True)
    currency: str
    cash: Decimal
    inventory_costs: dict[str, Decimal]
    realized_pnl: Decimal
    fees_paid: Decimal
    transaction_count: int = Field(ge=1, strict=True)

    @field_validator("account_id", mode="before")
    @classmethod
    def normalize_account_id(cls, value: object) -> str:
        return _canonical_identifier(value, "Reconciliation account id")

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> str:
        return _canonical_currency(value)

    @field_validator("cash", "realized_pnl", "fees_paid", mode="before")
    @classmethod
    def normalize_decimal(cls, value: object) -> Decimal:
        return _canonical_decimal(value, "Reconciliation amount")

    @field_validator("inventory_costs", mode="before")
    @classmethod
    def normalize_inventory_costs(cls, value: object) -> dict[str, Decimal]:
        if not isinstance(value, Mapping):
            raise ValueError("Reconciliation inventory costs must be a mapping.")
        return {
            _canonical_symbol(symbol): _canonical_decimal(
                amount,
                "Reconciliation inventory cost",
            )
            for symbol, amount in value.items()
        }


class PaperLedgerApplyResult(BaseModel):
    """Result of applying or idempotently replaying one modeled fill."""

    model_config = _STRICT_MODEL_CONFIG

    state: PaperLedgerState
    transaction: JournalTransaction
    consumptions: tuple[PaperLotConsumption, ...] = ()
    idempotent_replay: bool

    @field_validator("state", mode="before")
    @classmethod
    def revalidate_state(cls, value: object) -> PaperLedgerState:
        return PaperLedgerState.model_validate(
            value.model_dump(mode="python") if isinstance(value, PaperLedgerState) else value
        )

    @field_validator("transaction", mode="before")
    @classmethod
    def revalidate_transaction(cls, value: object) -> JournalTransaction:
        return JournalTransaction.model_validate(
            value.model_dump(mode="python") if isinstance(value, JournalTransaction) else value
        )

    @field_validator("consumptions", mode="before")
    @classmethod
    def revalidate_consumptions(
        cls,
        value: object,
    ) -> tuple[PaperLotConsumption, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError("Apply-result consumptions must be a sequence.")
        return tuple(
            PaperLotConsumption.model_validate(
                item.model_dump(mode="python") if isinstance(item, PaperLotConsumption) else item
            )
            for item in value
        )


def build_opening_balance_event(
    *,
    account_id: str,
    idempotency_key: str,
    currency: str,
    initial_cash: Decimal | int | str,
    occurred_at: datetime,
) -> OpeningBalanceEvent:
    data: dict[str, object] = {
        "schema_version": 1,
        "account_id": _canonical_identifier(account_id, "Opening event account id"),
        "idempotency_key": _canonical_identifier(
            idempotency_key,
            "Opening event idempotency key",
        ),
        "currency": _canonical_currency(currency),
        "initial_cash": _canonical_decimal(initial_cash, "Opening cash"),
        "occurred_at": _aware_utc(occurred_at, "Opening event time"),
    }
    return OpeningBalanceEvent.model_validate(
        {
            **data,
            "event_hash": _canonical_hash(data),
        }
    )


def build_paper_fill_event(
    *,
    account_id: str,
    expected_revision: int,
    idempotency_key: str,
    fill_id: str,
    order_id: str,
    symbol: str,
    side: Literal["buy", "sell"],
    quantity: Decimal | int | str,
    reference_price: Decimal | int | str,
    fill_price: Decimal | int | str,
    fee: Decimal | int | str,
    occurred_at: datetime,
) -> PaperFillEvent:
    exact_quantity = _canonical_decimal(quantity, "Fill quantity")
    exact_reference = _canonical_decimal(reference_price, "Fill reference price")
    exact_fill = _canonical_decimal(fill_price, "Fill price")
    exact_fee = _canonical_decimal(fee, "Fill fee")
    gross_notional = _exact_product(exact_quantity, exact_fill)
    price_difference = _terminating_decimal(abs(_fraction(exact_fill) - _fraction(exact_reference)))
    slippage = _exact_product(exact_quantity, price_difference)
    data: dict[str, object] = {
        "schema_version": 1,
        "account_id": _canonical_identifier(account_id, "Fill event account id"),
        "expected_revision": expected_revision,
        "idempotency_key": _canonical_identifier(
            idempotency_key,
            "Fill event idempotency key",
        ),
        "fill_id": _canonical_identifier(fill_id, "Fill id"),
        "order_id": _canonical_identifier(order_id, "Fill order id"),
        "symbol": _canonical_symbol(symbol),
        "side": side,
        "quantity": exact_quantity,
        "reference_price": exact_reference,
        "fill_price": exact_fill,
        "gross_notional": gross_notional,
        "fee": exact_fee,
        "slippage": slippage,
        "occurred_at": _aware_utc(occurred_at, "Fill event time"),
    }
    return PaperFillEvent.model_validate(
        {
            **data,
            "event_hash": _canonical_hash(data),
        }
    )


def open_paper_ledger(event: OpeningBalanceEvent) -> PaperLedgerState:
    safe_event = _validated_copy(
        event,
        OpeningBalanceEvent,
        "Opening balance event",
    )
    transaction = _build_transaction(
        revision=1,
        idempotency_key=safe_event.idempotency_key,
        source_kind="opening_balance",
        source_id=safe_event.account_id,
        source_hash=safe_event.event_hash,
        occurred_at=safe_event.occurred_at,
        postings=(
            _posting(
                _cash_account(safe_event.currency),
                safe_event.currency,
                safe_event.initial_cash,
            ),
            _posting(
                _capital_account(safe_event.currency),
                safe_event.currency,
                _exact_negate(safe_event.initial_cash),
            ),
        ),
    )
    return PaperLedgerState(
        account_id=safe_event.account_id,
        currency=safe_event.currency,
        revision=1,
        initial_cash=safe_event.initial_cash,
        cash=safe_event.initial_cash,
        realized_pnl=Decimal(0),
        fees_paid=Decimal(0),
        positions=(),
        transactions=(transaction,),
        applied_events={safe_event.idempotency_key: safe_event.event_hash},
    )


def apply_paper_fill(
    state: PaperLedgerState,
    event: PaperFillEvent,
) -> PaperLedgerApplyResult:
    safe_state = _validated_copy(state, PaperLedgerState, "Paper ledger state")
    safe_event = _validated_copy(event, PaperFillEvent, "Paper fill event")
    if safe_event.account_id != safe_state.account_id:
        raise PaperLedgerConflictError("Fill event belongs to another paper account.")
    prior_hash = safe_state.applied_events.get(safe_event.idempotency_key)
    if prior_hash is not None:
        if prior_hash != safe_event.event_hash:
            raise PaperLedgerConflictError(
                "Fill idempotency key was reused for different economics."
            )
        transaction = next(
            item
            for item in safe_state.transactions
            if item.idempotency_key == safe_event.idempotency_key
        )
        consumptions = tuple(
            item
            for item in safe_state.lot_consumptions
            if item.event_idempotency_key == safe_event.idempotency_key
        )
        return PaperLedgerApplyResult(
            state=safe_state,
            transaction=transaction,
            consumptions=consumptions,
            idempotent_replay=True,
        )
    if any(
        transaction.source_id == safe_event.fill_id
        and transaction.source_kind in {"buy_fill", "sell_fill"}
        for transaction in safe_state.transactions
    ):
        raise PaperLedgerConflictError("Fill id was reused under a different idempotency key.")
    if safe_event.expected_revision != safe_state.revision:
        raise PaperLedgerRevisionError(
            "Fill expected revision does not match the current paper ledger."
        )
    if safe_event.occurred_at < safe_state.transactions[-1].occurred_at:
        raise PaperLedgerConflictError("Fill occurrence time cannot precede the durable journal.")
    if safe_event.side == "buy":
        return _apply_buy(safe_state, safe_event)
    return _apply_sell(safe_state, safe_event)


def reconcile_paper_ledger(state: PaperLedgerState) -> PaperLedgerReconciliation:
    safe_state = _validated_copy(state, PaperLedgerState, "Paper ledger state")
    inventory = {position.symbol: position.book_cost for position in safe_state.positions}
    return PaperLedgerReconciliation(
        account_id=safe_state.account_id,
        revision=safe_state.revision,
        currency=safe_state.currency,
        cash=safe_state.cash,
        inventory_costs=inventory,
        realized_pnl=safe_state.realized_pnl,
        fees_paid=safe_state.fees_paid,
        transaction_count=len(safe_state.transactions),
    )


def _apply_buy(
    state: PaperLedgerState,
    event: PaperFillEvent,
) -> PaperLedgerApplyResult:
    cash_debit = _exact_sum(event.gross_notional, event.fee)
    if cash_debit > state.cash:
        raise InsufficientPaperCashError("Modeled buy exceeds available paper cash.")
    revision = state.revision + 1
    lot = _build_lot(event, revision=revision)
    positions = {position.symbol: position for position in state.positions}
    existing = positions.get(event.symbol)
    lots = (*existing.lots, lot) if existing is not None else (lot,)
    positions[event.symbol] = PaperPosition(
        symbol=event.symbol,
        quantity=_exact_sum(*(item.remaining_quantity for item in lots)),
        book_cost=_exact_sum(*(item.remaining_cost for item in lots)),
        lots=tuple(sorted(lots, key=lambda item: (item.opened_revision, item.id))),
    )
    transaction = _build_transaction(
        revision=revision,
        idempotency_key=event.idempotency_key,
        source_kind="buy_fill",
        source_id=event.fill_id,
        source_hash=event.event_hash,
        occurred_at=event.occurred_at,
        postings=_without_zero_postings(
            (
                (
                    _cash_account(state.currency),
                    _exact_negate(cash_debit),
                ),
                (_fee_account(state.currency), event.fee),
                (
                    _inventory_account(event.symbol, state.currency),
                    event.gross_notional,
                ),
            ),
            state.currency,
        ),
    )
    applied_events = dict(state.applied_events)
    applied_events[event.idempotency_key] = event.event_hash
    next_state = PaperLedgerState(
        account_id=state.account_id,
        currency=state.currency,
        revision=revision,
        initial_cash=state.initial_cash,
        cash=_exact_difference(state.cash, cash_debit),
        realized_pnl=state.realized_pnl,
        fees_paid=_exact_sum(state.fees_paid, event.fee),
        positions=tuple(positions[symbol] for symbol in sorted(positions)),
        transactions=(*state.transactions, transaction),
        lot_consumptions=state.lot_consumptions,
        applied_events=applied_events,
    )
    return PaperLedgerApplyResult(
        state=next_state,
        transaction=transaction,
        idempotent_replay=False,
    )


def _apply_sell(
    state: PaperLedgerState,
    event: PaperFillEvent,
) -> PaperLedgerApplyResult:
    positions = {position.symbol: position for position in state.positions}
    existing = positions.get(event.symbol)
    if existing is None or event.quantity > existing.quantity:
        raise InsufficientPaperPositionError(
            "Modeled sell exceeds the available long paper position."
        )
    revision = state.revision + 1
    remaining_to_consume = event.quantity
    updated_lots: list[PaperLot] = []
    consumptions: list[PaperLotConsumption] = []
    for lot in existing.lots:
        consumed = min(remaining_to_consume, lot.remaining_quantity)
        if consumed > 0:
            cost_basis = _exact_product(consumed, lot.unit_cost)
            consumptions.append(
                _build_consumption(
                    event=event,
                    lot=lot,
                    quantity=consumed,
                    cost_basis=cost_basis,
                    revision=revision,
                    sequence=len(consumptions) + 1,
                )
            )
            next_remaining = _exact_difference(
                lot.remaining_quantity,
                consumed,
            )
            updated_lots.append(_updated_lot(lot, next_remaining))
            remaining_to_consume = _exact_difference(
                remaining_to_consume,
                consumed,
            )
        else:
            updated_lots.append(lot)
    if remaining_to_consume != 0:  # pragma: no cover - position precheck invariant
        raise InsufficientPaperPositionError("FIFO lots could not satisfy the modeled sell.")
    cost_basis = _exact_sum(*(item.cost_basis for item in consumptions))
    cash_credit = _exact_difference(event.gross_notional, event.fee)
    realized_delta = _exact_difference(event.gross_notional, cost_basis)
    positions[event.symbol] = PaperPosition(
        symbol=event.symbol,
        quantity=_exact_sum(*(lot.remaining_quantity for lot in updated_lots)),
        book_cost=_exact_sum(*(lot.remaining_cost for lot in updated_lots)),
        lots=tuple(updated_lots),
    )
    transaction = _build_transaction(
        revision=revision,
        idempotency_key=event.idempotency_key,
        source_kind="sell_fill",
        source_id=event.fill_id,
        source_hash=event.event_hash,
        occurred_at=event.occurred_at,
        postings=_without_zero_postings(
            (
                (_cash_account(state.currency), cash_credit),
                (_fee_account(state.currency), event.fee),
                (
                    _inventory_account(event.symbol, state.currency),
                    _exact_negate(cost_basis),
                ),
                (
                    _realized_pnl_account(event.symbol, state.currency),
                    _exact_negate(realized_delta),
                ),
            ),
            state.currency,
        ),
    )
    applied_events = dict(state.applied_events)
    applied_events[event.idempotency_key] = event.event_hash
    next_state = PaperLedgerState(
        account_id=state.account_id,
        currency=state.currency,
        revision=revision,
        initial_cash=state.initial_cash,
        cash=_exact_sum(state.cash, cash_credit),
        realized_pnl=_exact_sum(state.realized_pnl, realized_delta),
        fees_paid=_exact_sum(state.fees_paid, event.fee),
        positions=tuple(positions[symbol] for symbol in sorted(positions)),
        transactions=(*state.transactions, transaction),
        lot_consumptions=(*state.lot_consumptions, *consumptions),
        applied_events=applied_events,
    )
    return PaperLedgerApplyResult(
        state=next_state,
        transaction=transaction,
        consumptions=tuple(consumptions),
        idempotent_replay=False,
    )


def _posting(account: str, currency: str, amount: Decimal) -> LedgerPosting:
    return LedgerPosting(account=account, currency=currency, amount=amount)


def _without_zero_postings(
    values: Sequence[tuple[str, Decimal]],
    currency: str,
) -> tuple[LedgerPosting, ...]:
    return tuple(
        sorted(
            (_posting(account, currency, amount) for account, amount in values if amount != 0),
            key=lambda posting: posting.account,
        )
    )


def _build_transaction(
    *,
    revision: int,
    idempotency_key: str,
    source_kind: Literal["opening_balance", "buy_fill", "sell_fill"],
    source_id: str,
    source_hash: str,
    occurred_at: datetime,
    postings: Sequence[LedgerPosting],
) -> JournalTransaction:
    canonical_postings = tuple(sorted(postings, key=lambda posting: posting.account))
    data: dict[str, object] = {
        "schema_version": 1,
        "ledger_revision": revision,
        "idempotency_key": idempotency_key,
        "source_kind": source_kind,
        "source_id": source_id,
        "source_hash": source_hash,
        "occurred_at": occurred_at,
        "postings": canonical_postings,
    }
    return JournalTransaction.model_validate(
        {
            **data,
            "transaction_hash": _canonical_hash(data),
        }
    )


def _build_lot(event: PaperFillEvent, *, revision: int) -> PaperLot:
    lot_id = _canonical_hash(
        {
            "source_fill_id": event.fill_id,
            "symbol": event.symbol,
            "opened_revision": revision,
            "opened_at": event.occurred_at,
        }
    )
    data: dict[str, object] = {
        "schema_version": 1,
        "id": lot_id,
        "symbol": event.symbol,
        "source_fill_id": event.fill_id,
        "opened_revision": revision,
        "opened_at": event.occurred_at,
        "unit_cost": event.fill_price,
        "original_quantity": event.quantity,
        "remaining_quantity": event.quantity,
        "original_cost": event.gross_notional,
        "remaining_cost": event.gross_notional,
    }
    return PaperLot.model_validate(
        {
            **data,
            "lot_hash": _canonical_hash(data),
        }
    )


def _updated_lot(lot: PaperLot, remaining_quantity: Decimal) -> PaperLot:
    data = lot.model_dump(mode="python", exclude={"lot_hash"})
    data["remaining_quantity"] = remaining_quantity
    data["remaining_cost"] = _exact_product(remaining_quantity, lot.unit_cost)
    return PaperLot.model_validate(
        {
            **data,
            "lot_hash": _canonical_hash(data),
        }
    )


def _build_consumption(
    *,
    event: PaperFillEvent,
    lot: PaperLot,
    quantity: Decimal,
    cost_basis: Decimal,
    revision: int,
    sequence: int,
) -> PaperLotConsumption:
    consumption_id = _canonical_hash(
        {
            "event_idempotency_key": event.idempotency_key,
            "fill_id": event.fill_id,
            "lot_id": lot.id,
            "sequence": sequence,
        }
    )
    data: dict[str, object] = {
        "schema_version": 1,
        "id": consumption_id,
        "ledger_revision": revision,
        "sequence": sequence,
        "event_idempotency_key": event.idempotency_key,
        "fill_id": event.fill_id,
        "lot_id": lot.id,
        "symbol": event.symbol,
        "quantity": quantity,
        "cost_basis": cost_basis,
    }
    return PaperLotConsumption.model_validate(
        {
            **data,
            "consumption_hash": _canonical_hash(data),
        }
    )


def _reconcile_state(state: PaperLedgerState) -> None:
    balances: dict[str, Decimal] = {}
    fill_source_ids: set[str] = set()
    for transaction in state.transactions:
        if transaction.source_kind in {"buy_fill", "sell_fill"}:
            if transaction.source_id in fill_source_ids:
                raise PaperLedgerReconciliationError(
                    "A fill identity appears in more than one journal transaction."
                )
            fill_source_ids.add(transaction.source_id)
        for posting in transaction.postings:
            if posting.currency != state.currency:
                raise PaperLedgerReconciliationError(
                    "Journal posting currency differs from the paper account."
                )
            balances[posting.account] = _exact_sum(
                balances.get(posting.account, Decimal(0)),
                posting.amount,
            )
    expected_accounts = {
        _cash_account(state.currency),
        _capital_account(state.currency),
        _fee_account(state.currency),
    }
    for position in state.positions:
        expected_accounts.add(_inventory_account(position.symbol, state.currency))
        expected_accounts.add(_realized_pnl_account(position.symbol, state.currency))
    unexpected = {
        account
        for account, balance in balances.items()
        if account not in expected_accounts and balance != 0
    }
    if unexpected:
        raise PaperLedgerReconciliationError(
            "Paper journal contains an unsupported non-zero account."
        )
    if balances.get(_cash_account(state.currency), Decimal(0)) != state.cash:
        raise PaperLedgerReconciliationError("Paper cash does not reconcile with journal postings.")
    if balances.get(_capital_account(state.currency), Decimal(0)) != _exact_negate(
        state.initial_cash
    ):
        raise PaperLedgerReconciliationError(
            "Opening capital does not reconcile with initial cash."
        )
    if balances.get(_fee_account(state.currency), Decimal(0)) != state.fees_paid:
        raise PaperLedgerReconciliationError("Paper fees do not reconcile with journal postings.")
    journal_realized = Decimal(0)
    lots_by_id: dict[str, PaperLot] = {}
    for position in state.positions:
        inventory_balance = balances.get(
            _inventory_account(position.symbol, state.currency),
            Decimal(0),
        )
        if inventory_balance != position.book_cost:
            raise PaperLedgerReconciliationError(
                f"Paper inventory cost for {position.symbol} does not reconcile."
            )
        journal_realized = _exact_sum(
            journal_realized,
            _exact_negate(
                balances.get(
                    _realized_pnl_account(position.symbol, state.currency),
                    Decimal(0),
                )
            ),
        )
        for lot in position.lots:
            if lot.id in lots_by_id:
                raise PaperLedgerReconciliationError(
                    "A paper lot appears in more than one position."
                )
            lots_by_id[lot.id] = lot
    if journal_realized != state.realized_pnl:
        raise PaperLedgerReconciliationError(
            "Realized PnL does not reconcile with journal postings."
        )
    consumed_quantity: dict[str, Decimal] = {}
    consumed_cost: dict[str, Decimal] = {}
    consumption_cost_by_revision: dict[int, Decimal] = {}
    transaction_by_revision = {
        transaction.ledger_revision: transaction for transaction in state.transactions
    }
    consumptions_by_revision: dict[int, list[PaperLotConsumption]] = {}
    for consumption in state.lot_consumptions:
        consumed_lot = lots_by_id.get(consumption.lot_id)
        if consumed_lot is None or consumed_lot.symbol != consumption.symbol:
            raise PaperLedgerReconciliationError(
                "Lot consumption refers to missing paper inventory."
            )
        source_transaction = transaction_by_revision.get(consumption.ledger_revision)
        if (
            source_transaction is None
            or source_transaction.source_kind != "sell_fill"
            or source_transaction.source_id != consumption.fill_id
            or source_transaction.idempotency_key != consumption.event_idempotency_key
        ):
            raise PaperLedgerReconciliationError(
                "Lot consumption is not bound to its sell journal transaction."
            )
        expected_cost = _exact_product(consumption.quantity, consumed_lot.unit_cost)
        if consumption.cost_basis != expected_cost:
            raise PaperLedgerReconciliationError(
                "Lot consumption cost does not reconcile with its FIFO lot."
            )
        consumed_quantity[consumed_lot.id] = _exact_sum(
            consumed_quantity.get(consumed_lot.id, Decimal(0)),
            consumption.quantity,
        )
        consumed_cost[consumed_lot.id] = _exact_sum(
            consumed_cost.get(consumed_lot.id, Decimal(0)),
            consumption.cost_basis,
        )
        consumption_cost_by_revision[consumption.ledger_revision] = _exact_sum(
            consumption_cost_by_revision.get(
                consumption.ledger_revision,
                Decimal(0),
            ),
            consumption.cost_basis,
        )
        consumptions_by_revision.setdefault(
            consumption.ledger_revision,
            [],
        ).append(consumption)
    for revision, revision_consumptions in consumptions_by_revision.items():
        if tuple(item.sequence for item in revision_consumptions) != tuple(
            range(1, len(revision_consumptions) + 1)
        ):
            raise PaperLedgerReconciliationError(
                "Lot-consumption sequences must be gapless within each sell."
            )
        transaction = transaction_by_revision[revision]
        inventory_postings = [
            posting
            for posting in transaction.postings
            if posting.account.startswith("ASSET:INVENTORY:")
        ]
        if len(inventory_postings) != 1 or inventory_postings[0].amount != _exact_negate(
            consumption_cost_by_revision[revision]
        ):
            raise PaperLedgerReconciliationError(
                "Sell inventory credit does not reconcile with FIFO consumption."
            )
    sell_revisions = {
        transaction.ledger_revision
        for transaction in state.transactions
        if transaction.source_kind == "sell_fill"
    }
    if sell_revisions != set(consumptions_by_revision):
        raise PaperLedgerReconciliationError(
            "Every modeled sell requires its exact FIFO consumption set."
        )
    buy_transactions = {
        transaction.source_id: transaction
        for transaction in state.transactions
        if transaction.source_kind == "buy_fill"
    }
    lot_source_ids = {lot.source_fill_id for lot in lots_by_id.values()}
    if len(lot_source_ids) != len(lots_by_id) or lot_source_ids != set(buy_transactions):
        raise PaperLedgerReconciliationError(
            "Every modeled buy requires exactly one durable FIFO lot."
        )
    for lot in lots_by_id.values():
        buy_transaction = buy_transactions[lot.source_fill_id]
        if (
            buy_transaction.ledger_revision != lot.opened_revision
            or buy_transaction.occurred_at != lot.opened_at
        ):
            raise PaperLedgerReconciliationError(
                "Paper lot opening identity differs from its buy journal."
            )
        inventory_postings = [
            posting
            for posting in buy_transaction.postings
            if posting.account == _inventory_account(lot.symbol, state.currency)
        ]
        if len(inventory_postings) != 1 or inventory_postings[0].amount != lot.original_cost:
            raise PaperLedgerReconciliationError(
                "Paper lot original cost differs from its buy journal."
            )
        expected_consumed_quantity = _exact_difference(
            lot.original_quantity,
            lot.remaining_quantity,
        )
        expected_consumed_cost = _exact_difference(
            lot.original_cost,
            lot.remaining_cost,
        )
        if (
            consumed_quantity.get(lot.id, Decimal(0)) != expected_consumed_quantity
            or consumed_cost.get(lot.id, Decimal(0)) != expected_consumed_cost
        ):
            raise PaperLedgerReconciliationError(
                "Paper lot consumption history does not reconcile."
            )
    _validate_fifo_consumption_order(state.positions, state.lot_consumptions)


def _validate_fifo_consumption_order(
    positions: tuple[PaperPosition, ...],
    consumptions: tuple[PaperLotConsumption, ...],
) -> None:
    remaining_by_lot = {
        lot.id: lot.original_quantity for position in positions for lot in position.lots
    }
    lots_by_symbol = {position.symbol: list(position.lots) for position in positions}
    for consumption in consumptions:
        symbol_lots = lots_by_symbol[consumption.symbol]
        earliest = next(
            (lot for lot in symbol_lots if remaining_by_lot[lot.id] > 0),
            None,
        )
        if earliest is None or earliest.id != consumption.lot_id:
            raise PaperLedgerReconciliationError(
                "Lot consumption violates deterministic FIFO order."
            )
        if consumption.quantity > remaining_by_lot[earliest.id]:
            raise PaperLedgerReconciliationError(
                "Lot consumption exceeds its historical FIFO remainder."
            )
        remaining_by_lot[earliest.id] = _exact_difference(
            remaining_by_lot[earliest.id],
            consumption.quantity,
        )
    for position in positions:
        for lot in position.lots:
            if remaining_by_lot[lot.id] != lot.remaining_quantity:
                raise PaperLedgerReconciliationError(
                    "Replayed FIFO quantity differs from its lot projection."
                )


ModelT = TypeVar("ModelT", bound=BaseModel)


def _validated_copy(
    value: object,
    model: type[ModelT],
    label: str,
) -> ModelT:
    if not isinstance(value, model):
        raise ValueError(f"{label} must be a {model.__name__} instance.")
    try:
        return model.model_validate(value.model_dump(mode="python"))
    except Exception as error:
        raise ValueError(f"{label} failed strict model revalidation.") from error
