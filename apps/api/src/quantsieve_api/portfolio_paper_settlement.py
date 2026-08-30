from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from math import isfinite
from typing import Literal, Self, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from quantsieve_engine import PortfolioForwardState

from .portfolio_paper_contracts import (
    CertifiedPortfolioBar,
    canonical_payload_hash,
)
from .portfolio_paper_execution import PortfolioPaperExecutionBatch

_HASH_PATTERN = r"^[0-9a-f]{64}$"
_ID_PATTERN = r"^[0-9a-f]{32}$"
_VALUATION_KIND: Literal["modeled_paper_close_valuation"] = (
    "modeled_paper_close_valuation"
)
_WEIGHT_TOLERANCE = Fraction(1, 10_000_000_000)
ModelT = TypeVar("ModelT", bound=BaseModel)


def _canonical_symbol(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Modeled paper close-valuation symbols must be non-empty.")
    return value.strip().upper()


def _canonical_decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite decimal.")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, float, str)):
        try:
            result = Decimal(str(value))
        except Exception as error:
            raise ValueError(f"{label} must be a finite decimal.") from error
    else:
        raise ValueError(f"{label} must be a finite decimal.")
    if not result.is_finite():
        raise ValueError(f"{label} must be a finite decimal.")
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
        raise ValueError("Modeled paper close accounting requires finite decimals.")
    return Fraction(value)


def _terminating_decimal(value: Fraction) -> Decimal:
    """Convert a terminating rational without consulting Decimal's global context."""

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
        raise ValueError(
            "Modeled paper close accounting produced a non-terminating amount."
        )
    scale = max(twos, fives)
    scaled_numerator = numerator
    scaled_numerator *= 5 ** (scale - fives)
    scaled_numerator *= 2 ** (scale - twos)
    sign = 1 if scaled_numerator < 0 else 0
    digits = tuple(int(digit) for digit in str(abs(scaled_numerator)))
    return _canonical_decimal(
        Decimal((sign, digits, -scale)),
        "Modeled paper close accounting result",
    )


def _exact_sum(*values: Decimal) -> Decimal:
    return _terminating_decimal(
        sum((_fraction(value) for value in values), Fraction())
    )


def _exact_product(*values: Decimal) -> Decimal:
    result = Fraction(1)
    for value in values:
        result *= _fraction(value)
    return _terminating_decimal(result)


def _aware_utc(value: object, label: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(
                f"{label} must be a timezone-aware datetime."
            ) from error
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a timezone-aware datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be a timezone-aware datetime.")
    return value.astimezone(UTC)


def _canonical_session(value: object) -> str:
    parsed = _aware_utc(value, "Modeled paper close-valuation session")
    if parsed != datetime.combine(parsed.date(), datetime.min.time(), tzinfo=UTC):
        raise ValueError(
            "Modeled paper close-valuation session must start at UTC midnight."
        )
    return parsed.isoformat()


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return _json_ready(value.model_dump(mode="python"))
    if isinstance(value, Decimal):
        return str(_canonical_decimal(value, "Hash decimal"))
    if isinstance(value, datetime):
        return _aware_utc(value, "Hash timestamp").isoformat()
    if isinstance(value, Mapping):
        ready: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Modeled paper close hash keys must be strings.")
            ready[key] = _json_ready(item)
        return ready
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("Modeled paper close hashes reject non-finite floats.")
    return value


def _canonical_hash(value: object) -> str:
    ready = _json_ready(value)
    try:
        json.dumps(
            ready,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(
            "Modeled paper close payload is not strict canonical JSON."
        ) from error
    return canonical_payload_hash(ready)


def _validated_copy(value: object, model: type[ModelT], label: str) -> ModelT:
    payload: object
    if isinstance(value, model):
        payload = value.model_dump(mode="python")
    elif isinstance(value, Mapping):
        payload = value
    else:
        raise ValueError(
            f"{label} must be a {model.__name__} instance or model mapping."
        )
    try:
        return model.model_validate(payload)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} failed strict model revalidation.") from error


def _validated_opening_batch(value: object) -> PortfolioPaperExecutionBatch:
    """Revalidate the context-independent opening contract before settlement."""

    payload: object
    if isinstance(value, PortfolioPaperExecutionBatch):
        payload = value.model_dump(mode="python")
    elif isinstance(value, Mapping):
        payload = value
    else:
        raise ValueError(
            "Modeled paper opening batch must be a model instance or mapping."
        )
    try:
        return PortfolioPaperExecutionBatch.model_validate(payload)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "Modeled paper opening batch failed strict model revalidation."
        ) from error


def _finite_float(value: Decimal | Fraction, label: str) -> float:
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{label} cannot be represented by the engine state.")
    return result


class PortfolioPaperExactRatio(BaseModel):
    """Reduced exact rational used where a decimal expansion may not terminate."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    numerator: int = Field(strict=True)
    denominator: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def validate_ratio(self) -> Self:
        reduced = Fraction(self.numerator, self.denominator)
        if (
            reduced.numerator != self.numerator
            or reduced.denominator != self.denominator
        ):
            raise ValueError("Exact ratios must use their reduced canonical form.")
        return self

    @property
    def fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)


def _exact_ratio(value: Fraction) -> PortfolioPaperExactRatio:
    return PortfolioPaperExactRatio(
        numerator=value.numerator,
        denominator=value.denominator,
    )


class PortfolioPaperCloseBarSet(BaseModel):
    """Finalized daily evidence for a modeled paper close valuation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    evidence_kind: Literal["modeled_paper_close_valuation_bars"] = (
        "modeled_paper_close_valuation_bars"
    )
    execution_session: str
    symbols: tuple[str, ...] = Field(min_length=2, max_length=6)
    bars: tuple[CertifiedPortfolioBar, ...] = Field(min_length=2, max_length=6)
    accepted_at: datetime
    revision_set_hash: str = Field(pattern=_HASH_PATTERN)
    bar_set_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator("execution_session", mode="before")
    @classmethod
    def normalize_session(cls, value: object) -> str:
        return _canonical_session(value)

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("Modeled paper close symbols must be a sequence.")
        symbols = tuple(_canonical_symbol(item) for item in value)
        if len(symbols) != len(set(symbols)):
            raise ValueError("Modeled paper close symbols must be unique.")
        return symbols

    @field_validator("bars", mode="before")
    @classmethod
    def revalidate_bars(
        cls,
        value: object,
    ) -> tuple[CertifiedPortfolioBar, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError("Modeled paper close bars must be a sequence.")
        return tuple(
            _validated_copy(item, CertifiedPortfolioBar, "Certified close bar")
            for item in value
        )

    @field_validator("accepted_at", mode="before")
    @classmethod
    def normalize_accepted_at(cls, value: object) -> datetime:
        return _aware_utc(value, "Modeled paper close evidence acceptance")

    @model_validator(mode="after")
    def validate_bar_set(self) -> Self:
        bar_symbols = tuple(bar.symbol for bar in self.bars)
        if bar_symbols != self.symbols:
            raise ValueError(
                "Certified close bars must exactly follow command symbol order."
            )
        if any(bar.session != self.execution_session for bar in self.bars):
            raise ValueError(
                "Every certified close bar must match the execution session."
            )
        if any(
            bar.finalized_at != bar.close_time + timedelta(minutes=2)
            for bar in self.bars
        ):
            raise ValueError(
                "Every Binance close bar requires its exact two-minute "
                "finalization buffer."
            )
        latest_evidence = max(
            max(
                bar.finalized_at,
                bar.observed_at,
                bar.exchange_server_time,
                bar.clock_checked_at,
            )
            for bar in self.bars
        )
        if self.accepted_at < latest_evidence:
            raise ValueError(
                "Close evidence cannot be accepted before every certified timestamp."
            )
        expected_revision_set_hash = _bar_revision_set_hash(self.bars)
        if self.revision_set_hash != expected_revision_set_hash:
            raise ValueError(
                "Modeled paper close bar-revision set hash does not match."
            )
        if self.bar_set_hash != _canonical_hash(_bar_set_payload(self)):
            raise ValueError("Modeled paper close bar-set hash does not match.")
        return self


class PortfolioPaperCloseValuationCommand(BaseModel):
    """Stable identity for valuation only; it is never a close-order command."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    valuation_kind: Literal["modeled_paper_close_valuation"] = _VALUATION_KIND
    track_id: str = Field(pattern=_ID_PATTERN)
    decision_id: str = Field(pattern=_ID_PATTERN)
    opening_batch_id: str = Field(pattern=_HASH_PATTERN)
    opening_batch_hash: str = Field(pattern=_HASH_PATTERN)
    execution_session: str
    source_state_revision: Literal[0] = 0
    target_state_revision: Literal[1] = 1
    source_state_hash: str = Field(pattern=_HASH_PATTERN)
    configuration_hash: str = Field(pattern=_HASH_PATTERN)
    target_hash: str = Field(pattern=_HASH_PATTERN)
    symbols: tuple[str, ...] = Field(min_length=2, max_length=6)
    initial_cash: Decimal = Field(gt=0)
    opening_ending_cash: Decimal = Field(ge=0)
    opening_raw_notional: Decimal = Field(gt=0)
    opening_fee: Decimal = Field(ge=0)
    opening_slippage: Decimal = Field(ge=0)
    opening_cash_debit: Decimal = Field(gt=0)
    fee_rate: Decimal = Field(ge=0, le=Decimal("0.1"))
    slippage_rate: Decimal = Field(ge=0, le=Decimal("0.1"))
    target_weights: dict[str, Decimal] = Field(min_length=2, max_length=6)
    bar_revision_set_hash: str = Field(pattern=_HASH_PATTERN)
    evidence_accepted_at: datetime
    idempotency_key: str = Field(pattern=_HASH_PATTERN)
    command_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator("execution_session", mode="before")
    @classmethod
    def normalize_session(cls, value: object) -> str:
        return _canonical_session(value)

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("Close-valuation command symbols must be a sequence.")
        symbols = tuple(_canonical_symbol(item) for item in value)
        if len(symbols) != len(set(symbols)):
            raise ValueError("Close-valuation command symbols must be unique.")
        return symbols

    @field_validator("target_weights", mode="before")
    @classmethod
    def normalize_target_weights(cls, value: object) -> dict[str, Decimal]:
        if not isinstance(value, Mapping):
            raise ValueError("Close-valuation target weights must be a mapping.")
        result: dict[str, Decimal] = {}
        for raw_symbol, raw_weight in value.items():
            symbol = _canonical_symbol(raw_symbol)
            if symbol in result:
                raise ValueError("Close-valuation target weights repeat a symbol.")
            result[symbol] = _canonical_decimal(
                raw_weight,
                "Close-valuation target weight",
            )
        return result

    @field_validator(
        "initial_cash",
        "opening_ending_cash",
        "opening_raw_notional",
        "opening_fee",
        "opening_slippage",
        "opening_cash_debit",
        "fee_rate",
        "slippage_rate",
        mode="before",
    )
    @classmethod
    def normalize_amounts(cls, value: object) -> Decimal:
        return _canonical_decimal(value, "Close-valuation command amount")

    @field_validator("evidence_accepted_at", mode="before")
    @classmethod
    def normalize_accepted_at(cls, value: object) -> datetime:
        return _aware_utc(value, "Close-valuation evidence acceptance")

    @model_validator(mode="after")
    def validate_command(self) -> Self:
        if set(self.target_weights) != set(self.symbols):
            raise ValueError(
                "Close-valuation target weights must exactly match symbols."
            )
        if any(weight <= 0 for weight in self.target_weights.values()):
            raise ValueError("Close-valuation target weights must be positive.")
        weight_sum = sum(
            (_fraction(weight) for weight in self.target_weights.values()),
            Fraction(),
        )
        if abs(weight_sum - 1) > _WEIGHT_TOLERANCE:
            raise ValueError("Close-valuation target weights must sum to one.")
        if self.opening_cash_debit != _exact_sum(
            self.opening_raw_notional,
            self.opening_fee,
            self.opening_slippage,
        ):
            raise ValueError(
                "Close-valuation opening debit does not reconcile with its components."
            )
        if (
            self.opening_cash_debit > self.initial_cash
            or self.opening_ending_cash > self.initial_cash
        ):
            raise ValueError(
                "Close-valuation opening amounts exceed initial cash."
            )
        if self.idempotency_key != _canonical_hash(
            _settlement_identity_payload(self)
        ):
            raise ValueError(
                "Close-valuation idempotency key does not match its stable identity."
            )
        if self.command_hash != _canonical_hash(_command_payload(self)):
            raise ValueError("Close-valuation command hash does not match.")
        return self


class PortfolioPaperModeledClosePosition(BaseModel):
    """Exact close valuation of an opening fill; this is not a close fill."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    symbol: str
    quantity: Decimal = Field(gt=0)
    certified_close_price_decimal: Decimal = Field(
        gt=0,
        description=(
            "The exact canonical Binance close carried by CertifiedPortfolioBar v2; "
            "the analytical float projection is never used as accounting evidence."
        ),
    )
    market_value: Decimal = Field(gt=0)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _canonical_symbol(value)

    @field_validator(
        "quantity",
        "certified_close_price_decimal",
        "market_value",
        mode="before",
    )
    @classmethod
    def normalize_amount(cls, value: object) -> Decimal:
        return _canonical_decimal(value, "Modeled paper close position amount")

    @model_validator(mode="after")
    def validate_position(self) -> Self:
        if self.market_value != _exact_product(
            self.quantity,
            self.certified_close_price_decimal,
        ):
            raise ValueError("Modeled paper close position value does not reconcile.")
        return self


class PortfolioPaperModeledCloseAccount(BaseModel):
    """Exact post-opening account valued at certified closes, with zero close trades."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    account_kind: Literal["modeled_paper_close_valuation_account"] = (
        "modeled_paper_close_valuation_account"
    )
    opening_batch_id: str = Field(pattern=_HASH_PATTERN)
    opening_batch_hash: str = Field(pattern=_HASH_PATTERN)
    execution_session: str
    symbols: tuple[str, ...] = Field(min_length=2, max_length=6)
    initial_cash: Decimal = Field(gt=0)
    cash: Decimal = Field(ge=0)
    positions: tuple[PortfolioPaperModeledClosePosition, ...] = Field(
        min_length=2,
        max_length=6,
    )
    holdings_value: Decimal = Field(gt=0)
    equity: Decimal = Field(gt=0)
    peak_equity: Decimal = Field(gt=0)
    total_return: PortfolioPaperExactRatio
    current_drawdown: PortfolioPaperExactRatio
    max_drawdown: PortfolioPaperExactRatio
    target_weights: dict[str, Decimal] = Field(min_length=2, max_length=6)
    realized_weights: dict[str, PortfolioPaperExactRatio] = Field(
        min_length=2,
        max_length=6,
    )
    opening_fill_count: int = Field(ge=2, le=6, strict=True)
    opening_raw_notional: Decimal = Field(gt=0)
    opening_fee: Decimal = Field(ge=0)
    opening_slippage: Decimal = Field(ge=0)
    opening_total_cost: Decimal = Field(ge=0)
    opening_cash_debit: Decimal = Field(gt=0)
    turnover_notional: Decimal = Field(gt=0)
    turnover_ratio: PortfolioPaperExactRatio
    close_trade_count: Literal[0] = 0
    close_fee: Decimal = Field(ge=0)
    close_slippage: Decimal = Field(ge=0)
    account_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator("execution_session", mode="before")
    @classmethod
    def normalize_session(cls, value: object) -> str:
        return _canonical_session(value)

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("Modeled paper close account symbols must be a sequence.")
        symbols = tuple(_canonical_symbol(item) for item in value)
        if len(symbols) != len(set(symbols)):
            raise ValueError("Modeled paper close account symbols must be unique.")
        return symbols

    @field_validator("positions", mode="before")
    @classmethod
    def revalidate_positions(
        cls,
        value: object,
    ) -> tuple[PortfolioPaperModeledClosePosition, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError("Modeled paper close positions must be a sequence.")
        return tuple(
            _validated_copy(
                item,
                PortfolioPaperModeledClosePosition,
                "Modeled paper close position",
            )
            for item in value
        )

    @field_validator("target_weights", mode="before")
    @classmethod
    def normalize_target_weights(cls, value: object) -> dict[str, Decimal]:
        if not isinstance(value, Mapping):
            raise ValueError("Modeled paper close target weights must be a mapping.")
        result: dict[str, Decimal] = {}
        for raw_symbol, weight in value.items():
            symbol = _canonical_symbol(raw_symbol)
            if symbol in result:
                raise ValueError(
                    "Modeled paper close target weights repeat a symbol."
                )
            result[symbol] = _canonical_decimal(
                weight,
                "Modeled paper close target weight",
            )
        return result

    @field_validator("realized_weights", mode="before")
    @classmethod
    def revalidate_realized_weights(
        cls,
        value: object,
    ) -> dict[str, PortfolioPaperExactRatio]:
        if not isinstance(value, Mapping):
            raise ValueError("Modeled paper realized weights must be a mapping.")
        result: dict[str, PortfolioPaperExactRatio] = {}
        for raw_symbol, ratio in value.items():
            symbol = _canonical_symbol(raw_symbol)
            if symbol in result:
                raise ValueError(
                    "Modeled paper realized weights repeat a symbol."
                )
            result[symbol] = _validated_copy(
                ratio,
                PortfolioPaperExactRatio,
                "Modeled paper realized weight",
            )
        return result

    @field_validator(
        "initial_cash",
        "cash",
        "holdings_value",
        "equity",
        "peak_equity",
        "opening_raw_notional",
        "opening_fee",
        "opening_slippage",
        "opening_total_cost",
        "opening_cash_debit",
        "turnover_notional",
        "close_fee",
        "close_slippage",
        mode="before",
    )
    @classmethod
    def normalize_amounts(cls, value: object) -> Decimal:
        return _canonical_decimal(value, "Modeled paper close account amount")

    @model_validator(mode="after")
    def validate_account(self) -> Self:
        position_symbols = tuple(position.symbol for position in self.positions)
        if position_symbols != self.symbols:
            raise ValueError(
                "Modeled paper positions must exactly follow account symbol order."
            )
        if self.opening_fill_count != len(self.positions):
            raise ValueError("Opening fill count does not match close positions.")
        if set(self.target_weights) != set(self.symbols) or set(
            self.realized_weights
        ) != set(self.symbols):
            raise ValueError(
                "Modeled paper close account maps must exactly match symbols."
            )
        expected_holdings = _exact_sum(
            *(position.market_value for position in self.positions)
        )
        expected_equity = _exact_sum(self.cash, expected_holdings)
        if self.holdings_value != expected_holdings:
            raise ValueError("Close holdings value does not reconcile.")
        if self.equity != expected_equity:
            raise ValueError("Close equity does not reconcile.")
        if self.peak_equity != max(self.initial_cash, self.equity):
            raise ValueError("Close peak equity does not reconcile.")
        expected_return = Fraction(
            _fraction(self.equity) - _fraction(self.initial_cash),
            _fraction(self.initial_cash),
        )
        if self.total_return.fraction != expected_return:
            raise ValueError("Exact total return does not reconcile.")
        expected_drawdown = (
            _fraction(self.equity) / _fraction(self.peak_equity) - 1
        )
        if self.current_drawdown.fraction != expected_drawdown:
            raise ValueError("Exact current drawdown does not reconcile.")
        if self.max_drawdown.fraction != min(Fraction(), expected_drawdown):
            raise ValueError("Exact maximum drawdown does not reconcile.")
        for position in self.positions:
            expected_weight = _fraction(position.market_value) / _fraction(
                self.equity
            )
            if self.realized_weights[position.symbol].fraction != expected_weight:
                raise ValueError(
                    f"Exact realized weight for {position.symbol} does not reconcile."
                )
        expected_cost = _exact_sum(self.opening_fee, self.opening_slippage)
        expected_debit = _exact_sum(
            self.opening_raw_notional,
            expected_cost,
        )
        if self.opening_total_cost != expected_cost:
            raise ValueError("Opening aggregate cost does not reconcile.")
        if self.opening_cash_debit != expected_debit:
            raise ValueError("Opening cash debit does not reconcile.")
        if self.cash > self.initial_cash or expected_debit > self.initial_cash:
            raise ValueError("Opening cash amounts exceed initial cash.")
        if self.turnover_notional != self.opening_raw_notional:
            raise ValueError("Close valuation cannot add turnover.")
        expected_turnover = _fraction(self.turnover_notional) / _fraction(
            self.initial_cash
        )
        if self.turnover_ratio.fraction != expected_turnover:
            raise ValueError("Exact turnover ratio does not reconcile.")
        if (
            self.close_trade_count != 0
            or self.close_fee != 0
            or self.close_slippage != 0
        ):
            raise ValueError(
                "Modeled paper close valuation must not create a second trade."
            )
        if self.account_hash != _canonical_hash(_account_payload(self)):
            raise ValueError("Modeled paper close account hash does not match.")
        return self


class PortfolioPaperModeledCloseSettlement(BaseModel):
    """Auditable close valuation of modeled opening fills, never a venue close fill."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    settlement_kind: Literal["modeled_paper_close_valuation"] = _VALUATION_KIND
    command: PortfolioPaperCloseValuationCommand
    bar_set: PortfolioPaperCloseBarSet
    account: PortfolioPaperModeledCloseAccount
    forward_state: PortfolioForwardState
    forward_state_hash: str = Field(pattern=_HASH_PATTERN)
    settled_at: datetime
    settlement_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator("command", mode="before")
    @classmethod
    def revalidate_command(
        cls,
        value: object,
    ) -> PortfolioPaperCloseValuationCommand:
        return _validated_copy(
            value,
            PortfolioPaperCloseValuationCommand,
            "Modeled paper close command",
        )

    @field_validator("bar_set", mode="before")
    @classmethod
    def revalidate_bar_set(cls, value: object) -> PortfolioPaperCloseBarSet:
        return _validated_copy(
            value,
            PortfolioPaperCloseBarSet,
            "Modeled paper close bar set",
        )

    @field_validator("account", mode="before")
    @classmethod
    def revalidate_account(
        cls,
        value: object,
    ) -> PortfolioPaperModeledCloseAccount:
        return _validated_copy(
            value,
            PortfolioPaperModeledCloseAccount,
            "Modeled paper close account",
        )

    @field_validator("forward_state", mode="before")
    @classmethod
    def revalidate_forward_state(cls, value: object) -> PortfolioForwardState:
        return _validated_copy(
            value,
            PortfolioForwardState,
            "Modeled paper derived forward state",
        )

    @field_validator("settled_at", mode="before")
    @classmethod
    def normalize_settled_at(cls, value: object) -> datetime:
        return _aware_utc(value, "Modeled paper close settlement time")

    @model_validator(mode="after")
    def validate_settlement(self) -> Self:
        command = self.command
        if (
            self.bar_set.execution_session != command.execution_session
            or self.bar_set.symbols != command.symbols
            or self.bar_set.revision_set_hash
            != command.bar_revision_set_hash
            or self.bar_set.accepted_at != command.evidence_accepted_at
        ):
            raise ValueError(
                "Modeled paper close bar evidence does not match its command."
            )
        account = self.account
        if (
            account.opening_batch_id != command.opening_batch_id
            or account.opening_batch_hash != command.opening_batch_hash
            or account.execution_session != command.execution_session
            or account.symbols != command.symbols
            or account.initial_cash != command.initial_cash
            or account.cash != command.opening_ending_cash
            or account.opening_raw_notional != command.opening_raw_notional
            or account.opening_fee != command.opening_fee
            or account.opening_slippage != command.opening_slippage
            or account.opening_cash_debit != command.opening_cash_debit
            or account.target_weights != command.target_weights
        ):
            raise ValueError(
                "Modeled paper close account does not match its command."
            )
        for position, bar in zip(
            account.positions,
            self.bar_set.bars,
            strict=True,
        ):
            expected_close = _certified_close_decimal(bar)
            if (
                position.symbol != bar.symbol
                or position.certified_close_price_decimal != expected_close
            ):
                raise ValueError(
                    "Modeled paper close positions must use their exact "
                    "certified bar closes."
                )
        if self.settled_at < command.evidence_accepted_at:
            raise ValueError(
                "Modeled paper close cannot settle before evidence acceptance."
            )
        if self.forward_state_hash != _canonical_hash(self.forward_state):
            raise ValueError("Derived portfolio state hash does not match.")
        _validate_forward_state_against_settlement(
            self.forward_state,
            command,
            account,
        )
        if self.settlement_hash != _canonical_hash(_settlement_payload(self)):
            raise ValueError("Modeled paper close settlement hash does not match.")
        return self


def build_portfolio_paper_close_valuation(
    *,
    opening_batch: PortfolioPaperExecutionBatch,
    bars: Sequence[CertifiedPortfolioBar],
    initial_state: PortfolioForwardState,
    accepted_at: datetime,
    settled_at: datetime,
) -> PortfolioPaperModeledCloseSettlement:
    """Value modeled opening fills at their finalized daily closes.

    The close is valuation evidence only. It never synthesizes a close order, close
    fill, fee, slippage charge, or exchange execution.
    """

    batch = _validated_opening_batch(opening_batch)
    state = _validated_copy(
        initial_state,
        PortfolioForwardState,
        "Initial portfolio forward state",
    )
    if isinstance(bars, (str, bytes)) or not isinstance(bars, Sequence):
        raise ValueError("Modeled paper settlement bars must be a sequence.")
    safe_bars = tuple(
        _validated_copy(bar, CertifiedPortfolioBar, "Certified settlement bar")
        for bar in bars
    )
    accepted = _aware_utc(
        accepted_at,
        "Modeled paper close evidence acceptance",
    )
    settled = _aware_utc(settled_at, "Modeled paper close settlement time")
    _validate_activation_state(state, batch)

    symbols = batch.command.symbols
    bar_set_data: dict[str, object] = {
        "schema_version": 1,
        "evidence_kind": "modeled_paper_close_valuation_bars",
        "execution_session": batch.command.execution_session,
        "symbols": symbols,
        "bars": safe_bars,
        "accepted_at": accepted,
        "revision_set_hash": _bar_revision_set_hash(safe_bars),
    }
    bar_set_data["bar_set_hash"] = _canonical_hash(bar_set_data)
    bar_set = PortfolioPaperCloseBarSet.model_validate(bar_set_data)
    if settled < accepted:
        raise ValueError(
            "Modeled paper close cannot settle before evidence acceptance."
        )

    source_state_hash = _canonical_hash(state)
    command_data: dict[str, object] = {
        "schema_version": 1,
        "valuation_kind": _VALUATION_KIND,
        "track_id": batch.command.track_id,
        "decision_id": batch.command.decision_id,
        "opening_batch_id": batch.command.idempotency_key,
        "opening_batch_hash": batch.batch_hash,
        "execution_session": batch.command.execution_session,
        "source_state_revision": 0,
        "target_state_revision": 1,
        "source_state_hash": source_state_hash,
        "configuration_hash": batch.command.configuration_hash,
        "target_hash": batch.command.target_hash,
        "symbols": symbols,
        "initial_cash": batch.command.available_cash,
        "opening_ending_cash": batch.ending_cash,
        "opening_raw_notional": batch.total_raw_notional,
        "opening_fee": batch.total_fee,
        "opening_slippage": batch.total_slippage,
        "opening_cash_debit": batch.total_cash_debit,
        "fee_rate": batch.command.fee_rate,
        "slippage_rate": batch.command.slippage_rate,
        "target_weights": batch.command.target_weights,
        "bar_revision_set_hash": bar_set.revision_set_hash,
        "evidence_accepted_at": accepted,
    }
    command_data["idempotency_key"] = _canonical_hash(
        _settlement_identity_mapping(command_data)
    )
    command_data["command_hash"] = _canonical_hash(command_data)
    command = PortfolioPaperCloseValuationCommand.model_validate(command_data)

    positions = tuple(
        _position_from_fill_and_bar(fill, bar)
        for fill, bar in zip(batch.fills, bar_set.bars, strict=True)
    )
    holdings_value = _exact_sum(
        *(position.market_value for position in positions)
    )
    equity = _exact_sum(batch.ending_cash, holdings_value)
    initial_cash = batch.command.available_cash
    peak_equity = max(initial_cash, equity)
    total_return = (
        _fraction(equity) - _fraction(initial_cash)
    ) / _fraction(initial_cash)
    current_drawdown = _fraction(equity) / _fraction(peak_equity) - 1
    realized_weights = {
        position.symbol: _exact_ratio(
            _fraction(position.market_value) / _fraction(equity)
        )
        for position in positions
    }
    turnover_ratio = _fraction(batch.total_raw_notional) / _fraction(
        initial_cash
    )
    account_data: dict[str, object] = {
        "schema_version": 1,
        "account_kind": "modeled_paper_close_valuation_account",
        "opening_batch_id": batch.command.idempotency_key,
        "opening_batch_hash": batch.batch_hash,
        "execution_session": batch.command.execution_session,
        "symbols": symbols,
        "initial_cash": initial_cash,
        "cash": batch.ending_cash,
        "positions": positions,
        "holdings_value": holdings_value,
        "equity": equity,
        "peak_equity": peak_equity,
        "total_return": _exact_ratio(total_return),
        "current_drawdown": _exact_ratio(current_drawdown),
        "max_drawdown": _exact_ratio(min(Fraction(), current_drawdown)),
        "target_weights": batch.command.target_weights,
        "realized_weights": realized_weights,
        "opening_fill_count": len(batch.fills),
        "opening_raw_notional": batch.total_raw_notional,
        "opening_fee": batch.total_fee,
        "opening_slippage": batch.total_slippage,
        "opening_total_cost": _exact_sum(
            batch.total_fee,
            batch.total_slippage,
        ),
        "opening_cash_debit": batch.total_cash_debit,
        "turnover_notional": batch.total_raw_notional,
        "turnover_ratio": _exact_ratio(turnover_ratio),
        "close_trade_count": 0,
        "close_fee": Decimal(0),
        "close_slippage": Decimal(0),
    }
    account_data["account_hash"] = _canonical_hash(account_data)
    account = PortfolioPaperModeledCloseAccount.model_validate(account_data)
    next_state = _derive_forward_state(state, command, account)
    forward_state_hash = _canonical_hash(next_state)
    settlement_data: dict[str, object] = {
        "schema_version": 1,
        "settlement_kind": _VALUATION_KIND,
        "command": command,
        "bar_set": bar_set,
        "account": account,
        "forward_state": next_state,
        "forward_state_hash": forward_state_hash,
        "settled_at": settled,
    }
    settlement_data["settlement_hash"] = _canonical_hash(settlement_data)
    return PortfolioPaperModeledCloseSettlement.model_validate(settlement_data)


def _position_from_fill_and_bar(
    fill: object,
    bar: CertifiedPortfolioBar,
) -> PortfolioPaperModeledClosePosition:
    from .portfolio_paper_execution import PortfolioPaperModeledFill

    if not isinstance(fill, PortfolioPaperModeledFill):
        raise ValueError("Opening fill failed post-validation type checking.")
    if fill.symbol != bar.symbol:
        raise ValueError(
            "Opening fills and certified close bars must have identical order."
        )
    close_price = _certified_close_decimal(bar)
    return PortfolioPaperModeledClosePosition(
        symbol=fill.symbol,
        quantity=fill.quantity,
        certified_close_price_decimal=close_price,
        market_value=_exact_product(fill.quantity, close_price),
    )


def _certified_close_decimal(bar: CertifiedPortfolioBar) -> Decimal:
    # CertifiedPortfolioBar v2 has already validated and canonicalized this exact
    # provider string independently of its float analysis projection.
    return _canonical_decimal(
        bar.exact_close,
        "Certified exact close price",
    )


def _validate_activation_state(
    state: PortfolioForwardState,
    batch: PortfolioPaperExecutionBatch,
) -> None:
    command = batch.command
    zero_maps = (
        state.shares,
        state.target_weights,
        state.realized_weights,
    )
    if (
        state.symbols != command.symbols
        or state.valuation_count != 0
        or state.rebalance_count != 0
        or state.last_prices
        or any(value != 0 for values in zero_maps for value in values.values())
        or state.total_return != 0
        or state.max_drawdown != 0
        or state.turnover_ratio != 0
        or state.turnover_notional != 0
        or state.fee_paid != 0
        or state.slippage_paid != 0
        or state.total_cost != 0
        or state.pending_target is None
    ):
        raise ValueError(
            "Close valuation requires the exact full-cash activation state."
        )
    if (
        state.initial_cash
        != _finite_float(command.available_cash, "Initial command cash")
        or state.cash != _finite_float(command.available_cash, "Initial command cash")
        or state.equity
        != _finite_float(command.available_cash, "Initial command cash")
        or state.peak_equity
        != _finite_float(command.available_cash, "Initial command cash")
        or state.fee_rate != _finite_float(command.fee_rate, "Opening fee rate")
        or state.slippage_rate
        != _finite_float(command.slippage_rate, "Opening slippage rate")
    ):
        raise ValueError(
            "Initial state cash or cost configuration does not match the opening."
        )
    pending = state.pending_target
    if pending.method != state.method:
        raise ValueError("Initial state method does not match its pending target.")
    pending_weights = {
        symbol: _canonical_decimal(
            str(pending.weights[symbol]),
            "Initial pending target weight",
        )
        for symbol in command.symbols
    }
    if (
        tuple(pending.weights) != command.symbols
        or pending_weights != command.target_weights
        or _canonical_hash(pending) != command.target_hash
    ):
        raise ValueError(
            "Initial pending target does not match the certified opening command."
        )
    if state.session is None:
        raise ValueError("Initial activation state must carry its information session.")
    information_session = _aware_utc(
        state.session,
        "Initial information session",
    )
    execution_session = _aware_utc(
        command.execution_session,
        "Opening execution session",
    )
    if information_session + timedelta(days=1) != execution_session:
        raise ValueError(
            "Opening close session must immediately follow the information session."
        )


def _derive_forward_state(
    initial: PortfolioForwardState,
    command: PortfolioPaperCloseValuationCommand,
    account: PortfolioPaperModeledCloseAccount,
) -> PortfolioForwardState:
    state_data = initial.model_dump(mode="python")
    state_data.update(
        {
            "session": command.execution_session,
            "cash": _finite_float(account.cash, "Close cash"),
            "shares": {
                position.symbol: _finite_float(
                    position.quantity,
                    f"Shares for {position.symbol}",
                )
                for position in account.positions
            },
            "equity": _finite_float(account.equity, "Close equity"),
            "total_return": _finite_float(
                account.total_return.fraction,
                "Total return",
            ),
            "peak_equity": _finite_float(account.peak_equity, "Peak equity"),
            "max_drawdown": _finite_float(
                account.max_drawdown.fraction,
                "Maximum drawdown",
            ),
            "last_prices": {
                position.symbol: _finite_float(
                    position.certified_close_price_decimal,
                    f"Close price for {position.symbol}",
                )
                for position in account.positions
            },
            "target_weights": {
                symbol: _finite_float(weight, f"Target weight for {symbol}")
                for symbol, weight in command.target_weights.items()
            },
            "realized_weights": {
                symbol: _finite_float(
                    ratio.fraction,
                    f"Realized weight for {symbol}",
                )
                for symbol, ratio in account.realized_weights.items()
            },
            "pending_target": None,
            "valuation_count": command.target_state_revision,
            "rebalance_count": 1,
            "turnover_ratio": _finite_float(
                account.turnover_ratio.fraction,
                "Turnover ratio",
            ),
            "turnover_notional": _finite_float(
                account.turnover_notional,
                "Turnover notional",
            ),
            "fee_paid": _finite_float(account.opening_fee, "Opening fee"),
            "slippage_paid": _finite_float(
                account.opening_slippage,
                "Opening slippage",
            ),
            "total_cost": _finite_float(
                account.opening_total_cost,
                "Opening total cost",
            ),
        }
    )
    return PortfolioForwardState.model_validate(state_data)


def _validate_forward_state_against_settlement(
    state: PortfolioForwardState,
    command: PortfolioPaperCloseValuationCommand,
    account: PortfolioPaperModeledCloseAccount,
) -> None:
    expected = {
        "cash": _finite_float(account.cash, "Close cash"),
        "equity": _finite_float(account.equity, "Close equity"),
        "total_return": _finite_float(
            account.total_return.fraction,
            "Total return",
        ),
        "peak_equity": _finite_float(account.peak_equity, "Peak equity"),
        "max_drawdown": _finite_float(
            account.max_drawdown.fraction,
            "Maximum drawdown",
        ),
        "turnover_ratio": _finite_float(
            account.turnover_ratio.fraction,
            "Turnover ratio",
        ),
        "turnover_notional": _finite_float(
            account.turnover_notional,
            "Turnover notional",
        ),
        "fee_paid": _finite_float(account.opening_fee, "Opening fee"),
        "slippage_paid": _finite_float(
            account.opening_slippage,
            "Opening slippage",
        ),
        "total_cost": _finite_float(
            account.opening_total_cost,
            "Opening total cost",
        ),
    }
    if (
        state.symbols != command.symbols
        or state.session != command.execution_session
        or state.valuation_count != command.target_state_revision
        or state.rebalance_count != 1
        or state.pending_target is not None
        or state.cash != expected["cash"]
        or state.equity != expected["equity"]
        or state.total_return != expected["total_return"]
        or state.peak_equity != expected["peak_equity"]
        or state.max_drawdown != expected["max_drawdown"]
        or state.turnover_ratio != expected["turnover_ratio"]
        or state.turnover_notional != expected["turnover_notional"]
        or state.fee_paid != expected["fee_paid"]
        or state.slippage_paid != expected["slippage_paid"]
        or state.total_cost != expected["total_cost"]
    ):
        raise ValueError(
            "Derived portfolio forward state does not reconcile with exact account."
        )
    expected_shares = {
        position.symbol: _finite_float(
            position.quantity,
            f"Shares for {position.symbol}",
        )
        for position in account.positions
    }
    expected_prices = {
        position.symbol: _finite_float(
            position.certified_close_price_decimal,
            f"Close price for {position.symbol}",
        )
        for position in account.positions
    }
    expected_target_weights = {
        symbol: _finite_float(weight, f"Target weight for {symbol}")
        for symbol, weight in command.target_weights.items()
    }
    expected_realized_weights = {
        symbol: _finite_float(ratio.fraction, f"Realized weight for {symbol}")
        for symbol, ratio in account.realized_weights.items()
    }
    if (
        state.shares != expected_shares
        or state.last_prices != expected_prices
        or state.target_weights != expected_target_weights
        or state.realized_weights != expected_realized_weights
    ):
        raise ValueError(
            "Derived portfolio forward state maps do not match exact account."
        )


def _bar_revision_set_hash(bars: Sequence[CertifiedPortfolioBar]) -> str:
    return _canonical_hash(
        {
            "bar_revisions": [
                {
                    "symbol": bar.symbol,
                    "revision_hash": bar.revision_hash,
                }
                for bar in bars
            ]
        }
    )


def _bar_set_payload(bar_set: PortfolioPaperCloseBarSet) -> dict[str, object]:
    return bar_set.model_dump(mode="python", exclude={"bar_set_hash"})


def _settlement_identity_mapping(
    command: Mapping[str, object],
) -> dict[str, object]:
    keys = (
        "schema_version",
        "valuation_kind",
        "track_id",
        "decision_id",
        "opening_batch_id",
        "opening_batch_hash",
        "execution_session",
        "source_state_revision",
        "target_state_revision",
        "source_state_hash",
        "configuration_hash",
        "target_hash",
        "symbols",
        "initial_cash",
        "opening_ending_cash",
        "opening_raw_notional",
        "opening_fee",
        "opening_slippage",
        "opening_cash_debit",
        "fee_rate",
        "slippage_rate",
        "target_weights",
        "bar_revision_set_hash",
    )
    defaults: dict[str, object] = {
        "schema_version": 1,
        "valuation_kind": _VALUATION_KIND,
    }
    return {
        key: command[key] if key in command else defaults[key]
        for key in keys
    }


def _settlement_identity_payload(
    command: PortfolioPaperCloseValuationCommand,
) -> dict[str, object]:
    return _settlement_identity_mapping(command.model_dump(mode="python"))


def _command_payload(
    command: PortfolioPaperCloseValuationCommand,
) -> dict[str, object]:
    return command.model_dump(mode="python", exclude={"command_hash"})


def _account_payload(
    account: PortfolioPaperModeledCloseAccount,
) -> dict[str, object]:
    return account.model_dump(mode="python", exclude={"account_hash"})


def _settlement_payload(
    settlement: PortfolioPaperModeledCloseSettlement,
) -> dict[str, object]:
    return settlement.model_dump(mode="python", exclude={"settlement_hash"})
