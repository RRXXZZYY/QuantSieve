from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, time
from decimal import Decimal
from fractions import Fraction
from math import gcd
from typing import Literal, Self, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from quantsieve_providers import ExecutionQuote

from .portfolio_paper_contracts import (
    PortfolioPaperBasketContract,
    PortfolioPaperDecisionReceipt,
    canonical_payload_hash,
    require_fresh_basket_rules,
)

_HASH_PATTERN = r"^[0-9a-f]{64}$"
_ID_PATTERN = r"^[0-9a-f]{32}$"
_WEIGHT_TOLERANCE = Decimal("0.0000000001")
_EXECUTION_KIND: Literal["modeled_paper_activation_opening"] = (
    "modeled_paper_activation_opening"
)

ModelT = TypeVar("ModelT", bound=BaseModel)


def _canonical_symbol(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Modeled paper-fill symbols must be non-empty strings.")
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


def _decimal_fraction(value: Decimal) -> Fraction:
    if not value.is_finite():
        raise ValueError("Modeled paper arithmetic requires finite decimals.")
    return Fraction(value)


def _fraction_decimal(value: Fraction) -> Decimal:
    """Convert a terminating rational to Decimal without context rounding."""

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
        raise ValueError("Modeled paper arithmetic produced a non-terminating decimal.")
    scale = max(twos, fives)
    scaled_numerator = numerator
    scaled_numerator *= 5 ** (scale - fives)
    scaled_numerator *= 2 ** (scale - twos)
    sign = 1 if scaled_numerator < 0 else 0
    digits = tuple(int(digit) for digit in str(abs(scaled_numerator)))
    return _canonical_decimal(
        Decimal((sign, digits, -scale)),
        "Modeled paper arithmetic result",
    )


def _exact_product(*values: Decimal) -> Decimal:
    result = Fraction(1)
    for value in values:
        result *= _decimal_fraction(value)
    return _fraction_decimal(result)


def _exact_sum(*values: Decimal) -> Decimal:
    return _fraction_decimal(sum((_decimal_fraction(value) for value in values), Fraction()))


def _exact_difference(minuend: Decimal, subtrahend: Decimal) -> Decimal:
    return _fraction_decimal(
        _decimal_fraction(minuend) - _decimal_fraction(subtrahend)
    )


def _common_decimal_step(first: Decimal, second: Decimal) -> Decimal:
    """Return the smallest exact quantity step satisfying both exchange filters."""

    if not all(value.is_finite() and value > 0 for value in (first, second)):
        raise ValueError("Quantity steps must be finite positive decimals.")
    first_fraction = Fraction(first)
    second_fraction = Fraction(second)
    numerator = abs(
        first_fraction.numerator
        * second_fraction.numerator
        // gcd(first_fraction.numerator, second_fraction.numerator)
    )
    denominator = gcd(first_fraction.denominator, second_fraction.denominator)
    return _fraction_decimal(Fraction(numerator, denominator))


def _is_exact_multiple(value: Decimal, step: Decimal) -> bool:
    quotient = _decimal_fraction(value) / _decimal_fraction(step)
    return quotient.denominator == 1


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
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Modeled paper execution session must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            "Modeled paper execution session must be an ISO timestamp."
        ) from error
    parsed = _aware_utc(parsed, "Modeled paper execution session")
    if parsed.time() != time.min:
        raise ValueError("Modeled paper execution session must start at UTC midnight.")
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
                raise ValueError("Modeled paper hash keys must be strings.")
            ready[key] = _json_ready(item)
        return ready
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, float) and (
        value != value or value in {float("inf"), float("-inf")}
    ):
        raise ValueError("Modeled paper hashes reject non-finite floats.")
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
        raise ValueError("Modeled paper payload is not strict canonical JSON.") from error
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


def _command_payload(command: PortfolioPaperExecutionCommand) -> dict[str, object]:
    return command.model_dump(mode="python", exclude={"command_hash"})


def _fill_payload(fill: PortfolioPaperModeledFill) -> dict[str, object]:
    return fill.model_dump(mode="python", exclude={"fill_hash"})


def _batch_payload(batch: PortfolioPaperExecutionBatch) -> dict[str, object]:
    return batch.model_dump(mode="python", exclude={"batch_hash"})


class PortfolioPaperExecutionCommand(BaseModel):
    """Quote-independent identity and inputs for one modeled paper opening."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    execution_kind: Literal["modeled_paper_activation_opening"] = _EXECUTION_KIND
    track_id: str = Field(pattern=_ID_PATTERN)
    decision_id: str = Field(pattern=_ID_PATTERN)
    execution_session: str
    state_revision: int = Field(ge=0, strict=True)
    configuration_hash: str = Field(pattern=_HASH_PATTERN)
    basket_hash: str = Field(pattern=_HASH_PATTERN)
    basket_identity_hash: str = Field(pattern=_HASH_PATTERN)
    certified_basket_hash: str = Field(pattern=_HASH_PATTERN)
    certificate_hash: str = Field(pattern=_HASH_PATTERN)
    target_hash: str = Field(pattern=_HASH_PATTERN)
    symbols: tuple[str, ...] = Field(min_length=2, max_length=6)
    target_weights: dict[str, Decimal] = Field(min_length=2, max_length=6)
    available_cash: Decimal = Field(gt=0)
    fee_rate: Decimal = Field(ge=0, le=Decimal("0.1"))
    slippage_rate: Decimal = Field(ge=0, le=Decimal("0.1"))
    accepted_at: datetime
    idempotency_key: str = Field(pattern=_HASH_PATTERN)
    command_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator("execution_session", mode="before")
    @classmethod
    def normalize_execution_session(cls, value: object) -> str:
        return _canonical_session(value)

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("Modeled paper command symbols must be a sequence.")
        symbols = tuple(_canonical_symbol(item) for item in value)
        if len(symbols) != len(set(symbols)):
            raise ValueError("Modeled paper command symbols must be unique.")
        return symbols

    @field_validator("target_weights", mode="before")
    @classmethod
    def normalize_weights(cls, value: object) -> dict[str, Decimal]:
        if not isinstance(value, Mapping):
            raise ValueError("Modeled paper target weights must be a mapping.")
        weights: dict[str, Decimal] = {}
        for raw_symbol, raw_weight in value.items():
            symbol = _canonical_symbol(raw_symbol)
            if symbol in weights:
                raise ValueError(
                    "Modeled paper target weights contain duplicate symbols."
                )
            weights[symbol] = _canonical_decimal(
                raw_weight,
                "Modeled paper target weight",
            )
        return weights

    @field_validator(
        "available_cash",
        "fee_rate",
        "slippage_rate",
        mode="before",
    )
    @classmethod
    def normalize_decimal(cls, value: object) -> Decimal:
        return _canonical_decimal(value, "Modeled paper command amount")

    @field_validator("accepted_at", mode="before")
    @classmethod
    def normalize_accepted_at(cls, value: object) -> datetime:
        return _aware_utc(value, "Modeled paper quote acceptance time")

    @model_validator(mode="after")
    def validate_command(self) -> Self:
        if self.state_revision != 0:
            raise ValueError(
                "Modeled paper opening commands are limited to activation revision zero."
            )
        if self.basket_hash != self.certified_basket_hash:
            raise ValueError(
                "Modeled paper opening basket must exactly match its certified basket."
            )
        accepted_session = datetime.combine(
            self.accepted_at.date(),
            time.min,
            tzinfo=UTC,
        ).isoformat()
        if accepted_session != self.execution_session:
            raise ValueError(
                "Modeled paper acceptance must occur in its opening session."
            )
        if set(self.target_weights) != set(self.symbols):
            raise ValueError(
                "Modeled paper target weights must exactly match command symbols."
            )
        if any(weight <= 0 for weight in self.target_weights.values()):
            raise ValueError(
                "Every activation-opening symbol requires a strictly positive weight."
            )
        weight_sum = sum(
            (
                _decimal_fraction(weight)
                for weight in self.target_weights.values()
            ),
            Fraction(),
        )
        if abs(weight_sum - 1) > Fraction(_WEIGHT_TOLERANCE):
            raise ValueError("Modeled paper target weights must sum to one.")
        expected_idempotency_key = _canonical_hash(
            {
                "schema_version": self.schema_version,
                "execution_kind": self.execution_kind,
                "track_id": self.track_id,
                "decision_id": self.decision_id,
                "execution_session": self.execution_session,
                "state_revision": self.state_revision,
                "configuration_hash": self.configuration_hash,
                "basket_hash": self.basket_hash,
                "basket_identity_hash": self.basket_identity_hash,
                "certificate_hash": self.certificate_hash,
                "target_hash": self.target_hash,
            }
        )
        if self.idempotency_key != expected_idempotency_key:
            raise ValueError(
                "Modeled paper command idempotency key does not match its identity."
            )
        if self.command_hash != _canonical_hash(_command_payload(self)):
            raise ValueError("Modeled paper command hash does not match its payload.")
        return self


class PortfolioPaperModeledFill(BaseModel):
    """A deterministic paper model result, never an exchange-reported fill."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    fill_kind: Literal["modeled_paper_fill"] = "modeled_paper_fill"
    symbol: str
    side: Literal["buy"] = "buy"
    quantity: Decimal = Field(gt=0)
    common_step_size: Decimal = Field(gt=0)
    target_weight: Decimal = Field(gt=0, le=1)
    target_cash_budget: Decimal = Field(gt=0)
    ask_price: Decimal = Field(gt=0)
    modeled_fill_price: Decimal = Field(gt=0)
    raw_notional: Decimal = Field(gt=0)
    notional_reference_price: Decimal = Field(gt=0)
    reference_notional: Decimal = Field(gt=0)
    fee_rate: Decimal = Field(ge=0, le=Decimal("0.1"))
    slippage_rate: Decimal = Field(ge=0, le=Decimal("0.1"))
    fee: Decimal = Field(ge=0)
    slippage: Decimal = Field(ge=0)
    cash_debit: Decimal = Field(gt=0)
    quote: ExecutionQuote
    quote_hash: str = Field(pattern=_HASH_PATTERN)
    fill_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _canonical_symbol(value)

    @field_validator(
        "quantity",
        "common_step_size",
        "target_weight",
        "target_cash_budget",
        "ask_price",
        "modeled_fill_price",
        "raw_notional",
        "notional_reference_price",
        "reference_notional",
        "fee_rate",
        "slippage_rate",
        "fee",
        "slippage",
        "cash_debit",
        mode="before",
    )
    @classmethod
    def normalize_decimal(cls, value: object) -> Decimal:
        return _canonical_decimal(value, "Modeled paper fill amount")

    @field_validator("quote", mode="before")
    @classmethod
    def revalidate_quote(cls, value: object) -> ExecutionQuote:
        return _validated_copy(value, ExecutionQuote, "Modeled paper quote")

    @model_validator(mode="after")
    def validate_fill(self) -> Self:
        if self.quote.symbol != self.symbol:
            raise ValueError("Modeled paper fill symbol does not match its quote.")
        if self.quote.ask_price != self.ask_price:
            raise ValueError("Modeled paper fill ask price does not match its quote.")
        if self.quote.notional_reference_price != self.notional_reference_price:
            raise ValueError(
                "Modeled paper fill reference price does not match its quote."
            )
        if self.quantity > self.quote.ask_quantity:
            raise ValueError(
                "Modeled paper quantity exceeds the displayed best-ask quantity."
            )
        if not _is_exact_multiple(self.quantity, self.common_step_size):
            raise ValueError(
                "Modeled paper quantity is not aligned to its common exchange step."
            )
        expected_raw = _exact_product(self.quantity, self.ask_price)
        expected_modeled_price = _exact_product(
            self.ask_price,
            _exact_sum(Decimal(1), self.slippage_rate),
        )
        expected_reference = _exact_product(
            self.quantity,
            self.notional_reference_price,
        )
        expected_fee = _exact_product(expected_raw, self.fee_rate)
        expected_slippage = _exact_product(expected_raw, self.slippage_rate)
        expected_debit = _exact_sum(
            expected_raw,
            expected_fee,
            expected_slippage,
        )
        if self.raw_notional != expected_raw:
            raise ValueError("Modeled paper raw notional does not reconcile.")
        if self.modeled_fill_price != expected_modeled_price:
            raise ValueError("Modeled paper fill price does not reconcile.")
        if self.reference_notional != expected_reference:
            raise ValueError("Modeled paper reference notional does not reconcile.")
        if self.fee != expected_fee:
            raise ValueError("Modeled paper fee does not reconcile with raw notional.")
        if self.slippage != expected_slippage:
            raise ValueError(
                "Modeled paper slippage does not reconcile with raw notional."
            )
        if self.cash_debit != expected_debit:
            raise ValueError("Modeled paper cash debit does not reconcile.")
        if self.cash_debit > self.target_cash_budget:
            raise ValueError("Modeled paper fill exceeds its target cash budget.")
        if self.quote_hash != _canonical_hash(self.quote):
            raise ValueError("Modeled paper quote hash does not match its evidence.")
        if self.fill_hash != _canonical_hash(_fill_payload(self)):
            raise ValueError("Modeled paper fill hash does not match its payload.")
        return self


class PortfolioPaperExecutionBatch(BaseModel):
    """An all-or-nothing batch of modeled paper buys, not venue executions."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    batch_kind: Literal["modeled_paper_fill_batch"] = "modeled_paper_fill_batch"
    command: PortfolioPaperExecutionCommand
    fills: tuple[PortfolioPaperModeledFill, ...] = Field(
        min_length=2,
        max_length=6,
    )
    quote_set_hash: str = Field(pattern=_HASH_PATTERN)
    fill_set_hash: str = Field(pattern=_HASH_PATTERN)
    total_raw_notional: Decimal = Field(gt=0)
    total_fee: Decimal = Field(ge=0)
    total_slippage: Decimal = Field(ge=0)
    total_cash_debit: Decimal = Field(gt=0)
    ending_cash: Decimal = Field(ge=0)
    batch_hash: str = Field(pattern=_HASH_PATTERN)

    @field_validator("command", mode="before")
    @classmethod
    def revalidate_command(cls, value: object) -> PortfolioPaperExecutionCommand:
        return _validated_copy(
            value,
            PortfolioPaperExecutionCommand,
            "Modeled paper command",
        )

    @field_validator("fills", mode="before")
    @classmethod
    def revalidate_fills(
        cls,
        value: object,
    ) -> tuple[PortfolioPaperModeledFill, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError("Modeled paper fills must be a sequence.")
        return tuple(
            _validated_copy(item, PortfolioPaperModeledFill, "Modeled paper fill")
            for item in value
        )

    @field_validator(
        "total_raw_notional",
        "total_fee",
        "total_slippage",
        "total_cash_debit",
        "ending_cash",
        mode="before",
    )
    @classmethod
    def normalize_decimal(cls, value: object) -> Decimal:
        return _canonical_decimal(value, "Modeled paper batch amount")

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        fill_symbols = tuple(fill.symbol for fill in self.fills)
        if fill_symbols != self.command.symbols:
            raise ValueError(
                "Modeled paper fills must exactly follow canonical command symbol order."
            )
        if len(fill_symbols) != len(set(fill_symbols)):
            raise ValueError("Modeled paper batch cannot repeat a symbol.")
        for fill in self.fills:
            if fill.target_weight != self.command.target_weights[fill.symbol]:
                raise ValueError(
                    "Modeled paper fill weight does not match its command."
                )
            expected_budget = _exact_product(
                self.command.available_cash,
                fill.target_weight,
            )
            if fill.target_cash_budget != expected_budget:
                raise ValueError(
                    "Modeled paper fill budget does not match its command."
                )
            if (
                fill.fee_rate != self.command.fee_rate
                or fill.slippage_rate != self.command.slippage_rate
            ):
                raise ValueError(
                    "Modeled paper fill costs do not match command rates."
                )
        expected_quote_set_hash = _canonical_hash(
            {
                "quotes": [
                    {"symbol": fill.symbol, "quote_hash": fill.quote_hash}
                    for fill in self.fills
                ]
            }
        )
        expected_fill_set_hash = _canonical_hash(
            {
                "fills": [
                    {"symbol": fill.symbol, "fill_hash": fill.fill_hash}
                    for fill in self.fills
                ]
            }
        )
        if self.quote_set_hash != expected_quote_set_hash:
            raise ValueError("Modeled paper quote-set hash does not match its fills.")
        if self.fill_set_hash != expected_fill_set_hash:
            raise ValueError("Modeled paper fill-set hash does not match its fills.")
        expected_raw = _exact_sum(*(fill.raw_notional for fill in self.fills))
        expected_fee = _exact_sum(*(fill.fee for fill in self.fills))
        expected_slippage = _exact_sum(*(fill.slippage for fill in self.fills))
        expected_debit = _exact_sum(*(fill.cash_debit for fill in self.fills))
        expected_ending_cash = _exact_difference(
            self.command.available_cash,
            expected_debit,
        )
        if expected_ending_cash < 0:
            raise ValueError("Modeled paper batch exceeds available cash.")
        if self.total_raw_notional != expected_raw:
            raise ValueError("Modeled paper batch raw notional does not reconcile.")
        if self.total_fee != expected_fee:
            raise ValueError("Modeled paper batch fee does not reconcile.")
        if self.total_slippage != expected_slippage:
            raise ValueError("Modeled paper batch slippage does not reconcile.")
        if self.total_cash_debit != expected_debit:
            raise ValueError("Modeled paper batch cash debit does not reconcile.")
        if self.ending_cash != expected_ending_cash:
            raise ValueError("Modeled paper batch ending cash does not reconcile.")
        if self.batch_hash != _canonical_hash(_batch_payload(self)):
            raise ValueError("Modeled paper batch hash does not match its payload.")
        return self


def build_portfolio_paper_activation_opening_batch(
    *,
    track_id: str,
    state_revision: int,
    available_cash: Decimal | float | int,
    configuration_hash: str,
    decision_receipt: PortfolioPaperDecisionReceipt,
    basket: PortfolioPaperBasketContract,
    execution_quotes: Mapping[str, ExecutionQuote],
    fee_rate: Decimal | float | int,
    slippage_rate: Decimal | float | int,
    accepted_at: datetime,
) -> PortfolioPaperExecutionBatch:
    """Build the first all-or-nothing opening as modeled paper evidence.

    The caller must first authenticate the decision receipt and strictly validate
    the complete quote set against that receipt. This pure function revalidates
    every model instance, sizes only displayed best-ask liquidity, and never
    represents the result as an exchange-reported execution.
    """

    receipt = _validated_copy(
        decision_receipt,
        PortfolioPaperDecisionReceipt,
        "Portfolio paper decision receipt",
    )
    safe_basket = _validated_copy(
        basket,
        PortfolioPaperBasketContract,
        "Portfolio paper basket",
    )
    accepted = _aware_utc(accepted_at, "Modeled paper quote acceptance time")
    cash = _canonical_decimal(available_cash, "Modeled paper available cash")
    exact_fee_rate = _canonical_decimal(fee_rate, "Modeled paper fee rate")
    exact_slippage_rate = _canonical_decimal(
        slippage_rate,
        "Modeled paper slippage rate",
    )
    if cash <= 0:
        raise ValueError("Modeled paper available cash must be positive.")
    if (
        exact_fee_rate < 0
        or exact_fee_rate > Decimal("0.1")
        or exact_slippage_rate < 0
        or exact_slippage_rate > Decimal("0.1")
    ):
        raise ValueError("Modeled paper fee and slippage rates are out of range.")
    if isinstance(state_revision, bool) or state_revision != 0:
        raise ValueError(
            "Modeled paper activation opening requires state revision zero."
        )
    if receipt.track_id != track_id:
        raise ValueError("Modeled paper decision receipt belongs to another track.")
    certificate = receipt.certificate
    if certificate.configuration_hash != configuration_hash:
        raise ValueError(
            "Modeled paper configuration hash does not match the certificate."
        )
    if safe_basket.identity.identity_hash != certificate.basket_identity_hash:
        raise ValueError(
            "Modeled paper execution basket does not match the certified identity."
        )
    if safe_basket.basket_hash != certificate.basket_hash:
        raise ValueError(
            "Modeled paper execution basket must exactly match the certified basket."
        )
    if accepted < receipt.persisted_at:
        raise ValueError("Modeled paper acceptance cannot predate decision persistence.")
    if (
        datetime.combine(accepted.date(), time.min, tzinfo=UTC).isoformat()
        != certificate.execution_session
        or accepted > certificate.execution_deadline
    ):
        raise ValueError(
            "Modeled paper acceptance is outside the certified opening window."
        )
    require_fresh_basket_rules(safe_basket, at=accepted)

    symbols = tuple(contract.symbol for contract in safe_basket.instruments)
    if not 2 <= len(symbols) <= 6:
        raise ValueError("Modeled paper activation requires between 2 and 6 symbols.")
    if set(certificate.target.weights) != set(symbols):
        raise ValueError(
            "Modeled paper target must exactly match the execution basket."
        )
    weights = {
        symbol: _canonical_decimal(
            certificate.target.weights[symbol],
            "Modeled paper target weight",
        )
        for symbol in symbols
    }
    if any(weight <= 0 for weight in weights.values()):
        raise ValueError(
            "Activation opening requires one positive buy for every basket symbol."
        )

    if not isinstance(execution_quotes, Mapping):
        raise ValueError("Modeled paper execution quotes must be a mapping.")
    quotes: dict[str, ExecutionQuote] = {}
    for raw_symbol, raw_quote in execution_quotes.items():
        symbol = _canonical_symbol(raw_symbol)
        if symbol in quotes:
            raise ValueError("Modeled paper quote map contains duplicate symbols.")
        quote = _validated_copy(
            raw_quote,
            ExecutionQuote,
            "Modeled paper execution quote",
        )
        if quote.symbol != symbol:
            raise ValueError("Modeled paper quote key does not match quote symbol.")
        quotes[symbol] = quote
    if set(quotes) != set(symbols):
        raise ValueError(
            "Modeled paper quote set must exactly match the execution basket."
        )

    target_hash = _canonical_hash(certificate.target.model_dump(mode="json"))
    idempotency_key = _canonical_hash(
        {
            "schema_version": 1,
            "execution_kind": _EXECUTION_KIND,
            "track_id": track_id,
            "decision_id": receipt.decision_id,
            "execution_session": certificate.execution_session,
            "state_revision": state_revision,
            "configuration_hash": configuration_hash,
            "basket_hash": safe_basket.basket_hash,
            "basket_identity_hash": safe_basket.identity.identity_hash,
            "certificate_hash": receipt.certificate_hash,
            "target_hash": target_hash,
        }
    )
    command_data: dict[str, object] = {
        "schema_version": 1,
        "execution_kind": _EXECUTION_KIND,
        "track_id": track_id,
        "decision_id": receipt.decision_id,
        "execution_session": certificate.execution_session,
        "state_revision": state_revision,
        "configuration_hash": configuration_hash,
        "basket_hash": safe_basket.basket_hash,
        "basket_identity_hash": safe_basket.identity.identity_hash,
        "certified_basket_hash": certificate.basket_hash,
        "certificate_hash": receipt.certificate_hash,
        "target_hash": target_hash,
        "symbols": symbols,
        "target_weights": weights,
        "available_cash": cash,
        "fee_rate": exact_fee_rate,
        "slippage_rate": exact_slippage_rate,
        "accepted_at": accepted,
        "idempotency_key": idempotency_key,
    }
    command_data["command_hash"] = _canonical_hash(command_data)
    command = PortfolioPaperExecutionCommand.model_validate(command_data)

    contracts = {contract.symbol: contract for contract in safe_basket.instruments}
    fills: list[PortfolioPaperModeledFill] = []
    unit_cost_multiplier = _exact_sum(
        Decimal(1),
        exact_fee_rate,
        exact_slippage_rate,
    )
    for symbol in symbols:
        contract = contracts[symbol]
        quote = quotes[symbol]
        if quote.provider != contract.provider or quote.venue != contract.venue:
            raise ValueError(
                "Modeled paper quote provider or venue does not match its contract."
            )
        target_budget = _exact_product(cash, weights[symbol])
        unit_cash_debit = _exact_product(quote.ask_price, unit_cost_multiplier)
        common_step = _common_decimal_step(
            contract.rules.lot_step_size,
            contract.rules.market_step_size,
        )
        steps = (
            _decimal_fraction(target_budget)
            / _decimal_fraction(unit_cash_debit)
            / _decimal_fraction(common_step)
        ).__floor__()
        quantity = _fraction_decimal(Fraction(steps) * Fraction(common_step))
        minimum_quantity = max(
            contract.rules.lot_min_quantity,
            contract.rules.market_min_quantity,
        )
        maximum_quantity = min(
            contract.rules.lot_max_quantity,
            contract.rules.market_max_quantity,
        )
        reference_notional = _exact_product(
            quantity,
            quote.notional_reference_price,
        )
        if (
            quantity <= 0
            or not _is_exact_multiple(quantity, contract.rules.lot_step_size)
            or not _is_exact_multiple(quantity, contract.rules.market_step_size)
            or quantity < minimum_quantity
            or quantity > maximum_quantity
            or quantity > quote.ask_quantity
            or (
                contract.rules.min_notional_applies_to_market
                and reference_notional < contract.rules.min_notional
            )
            or (
                contract.rules.max_notional_applies_to_market
                and (
                    contract.rules.max_notional is None
                    or reference_notional > contract.rules.max_notional
                )
            )
        ):
            raise ValueError(
                "Fresh quotes cannot satisfy every all-or-nothing modeled paper buy."
            )
        raw_notional = _exact_product(quantity, quote.ask_price)
        fee = _exact_product(raw_notional, exact_fee_rate)
        slippage = _exact_product(raw_notional, exact_slippage_rate)
        cash_debit = _exact_sum(raw_notional, fee, slippage)
        modeled_fill_price = _exact_product(
            quote.ask_price,
            _exact_sum(Decimal(1), exact_slippage_rate),
        )
        quote_hash = _canonical_hash(quote)
        fill_data: dict[str, object] = {
            "schema_version": 1,
            "fill_kind": "modeled_paper_fill",
            "symbol": symbol,
            "side": "buy",
            "quantity": quantity,
            "common_step_size": common_step,
            "target_weight": weights[symbol],
            "target_cash_budget": target_budget,
            "ask_price": quote.ask_price,
            "modeled_fill_price": modeled_fill_price,
            "raw_notional": raw_notional,
            "notional_reference_price": quote.notional_reference_price,
            "reference_notional": reference_notional,
            "fee_rate": exact_fee_rate,
            "slippage_rate": exact_slippage_rate,
            "fee": fee,
            "slippage": slippage,
            "cash_debit": cash_debit,
            "quote": quote,
            "quote_hash": quote_hash,
        }
        fill_data["fill_hash"] = _canonical_hash(fill_data)
        fills.append(PortfolioPaperModeledFill.model_validate(fill_data))

    quote_set_hash = _canonical_hash(
        {
            "quotes": [
                {"symbol": fill.symbol, "quote_hash": fill.quote_hash}
                for fill in fills
            ]
        }
    )
    fill_set_hash = _canonical_hash(
        {
            "fills": [
                {"symbol": fill.symbol, "fill_hash": fill.fill_hash}
                for fill in fills
            ]
        }
    )
    total_raw_notional = _exact_sum(*(fill.raw_notional for fill in fills))
    total_fee = _exact_sum(*(fill.fee for fill in fills))
    total_slippage = _exact_sum(*(fill.slippage for fill in fills))
    total_cash_debit = _exact_sum(*(fill.cash_debit for fill in fills))
    ending_cash = _exact_difference(cash, total_cash_debit)
    if ending_cash < 0:
        raise ValueError("All-or-nothing modeled paper buys exceed available cash.")
    batch_data: dict[str, object] = {
        "schema_version": 1,
        "batch_kind": "modeled_paper_fill_batch",
        "command": command,
        "fills": tuple(fills),
        "quote_set_hash": quote_set_hash,
        "fill_set_hash": fill_set_hash,
        "total_raw_notional": total_raw_notional,
        "total_fee": total_fee,
        "total_slippage": total_slippage,
        "total_cash_debit": total_cash_debit,
        "ending_cash": ending_cash,
    }
    batch_data["batch_hash"] = _canonical_hash(batch_data)
    return PortfolioPaperExecutionBatch.model_validate(batch_data)
