from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from math import copysign, isfinite
from typing import Any, Literal, Self
from uuid import uuid4

import pandas as pd
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from quantsieve_engine import (
    PortfolioForwardTarget,
    compute_next_portfolio_target,
)
from quantsieve_providers import (
    BinanceSpotTradingRules,
    ExecutionQuote,
    Instrument,
)

_CONTRACT_MAX_AGE = timedelta(minutes=15)
_BINANCE_FINALIZATION_LAG = timedelta(minutes=2)
_MAX_BINANCE_DECIMAL_SOURCE_LENGTH = 512
_MAX_BINANCE_CANONICAL_DECIMAL_LENGTH = 512
_BINANCE_FIXED_POINT_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?", flags=re.ASCII)
PORTFOLIO_TARGET_CALCULATION_VERSION: Literal["portfolio-forward-target-v1"] = (
    "portfolio-forward-target-v1"
)


def canonical_payload_hash(payload: object) -> str:
    """Hash one JSON-compatible payload without accepting NaN or unstable key order."""

    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _aware_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware.")
    return value.astimezone(UTC)


def _canonical_symbol(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Paper-contract symbols must be non-empty strings.")
    return value.strip().upper()


class PortfolioPaperInstrumentIdentity(BaseModel):
    """Stable identity for one single-level top-of-book paper instrument.

    The execution model deliberately excludes multi-level order-book depth and
    therefore never represents a sweep across additional price levels.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    symbol: str
    base_asset: str
    quote_currency: Literal["USDT"]
    provider: Literal["binance"] = "binance"
    venue: Literal["Binance Spot"] = "Binance Spot"
    market: Literal["CRYPTO"] = "CRYPTO"
    asset_type: Literal["spot"] = "spot"
    calendar_id: Literal["binance_utc_24_7"] = "binance_utc_24_7"
    execution_model: Literal[
        "single_level_top_of_book_after_persisted_decision"
    ] = (
        "single_level_top_of_book_after_persisted_decision"
    )

    @field_validator("symbol", "base_asset", mode="before")
    @classmethod
    def normalize_identifier(cls, value: object) -> str:
        return _canonical_symbol(value)

class PortfolioPaperInstrumentContract(BaseModel):
    """Immutable eligibility for the deliberately narrow top-of-book paper scope.

    Only the displayed best bid/ask level is in scope. No deeper book level is
    represented, aggregated, or assumed available by this contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    symbol: str
    base_asset: str
    quote_currency: Literal["USDT"]
    provider: Literal["binance"] = "binance"
    venue: Literal["Binance Spot"] = "Binance Spot"
    market: Literal["CRYPTO"] = "CRYPTO"
    asset_type: Literal["spot"] = "spot"
    calendar_id: Literal["binance_utc_24_7"] = "binance_utc_24_7"
    execution_model: Literal[
        "single_level_top_of_book_after_persisted_decision"
    ] = (
        "single_level_top_of_book_after_persisted_decision"
    )
    rules: BinanceSpotTradingRules
    created_at: datetime

    @field_validator("symbol", "base_asset", mode="before")
    @classmethod
    def normalize_identifier(cls, value: object) -> str:
        return _canonical_symbol(value)

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "Contract creation time")

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        created_at = _aware_utc(self.created_at, "Contract creation time")
        verified_at = _aware_utc(
            self.rules.verified_at,
            "Trading-rule verification time",
        )
        if verified_at > created_at:
            raise ValueError("Trading rules cannot be verified after contract creation.")
        if created_at - verified_at > _CONTRACT_MAX_AGE:
            raise ValueError("Binance trading rules are too old for contract creation.")
        if self.rules.symbol != self.symbol:
            raise ValueError("Trading rules do not match the contract symbol.")
        if self.rules.base_asset != self.base_asset:
            raise ValueError("Trading rules do not match the contract base asset.")
        if self.rules.quote_asset != self.quote_currency:
            raise ValueError("First-scope paper contracts require exact USDT quote currency.")
        if self.rules.status != "TRADING" or not self.rules.spot_trading_allowed:
            raise ValueError("Binance symbol is not currently eligible for spot trading.")
        if "MARKET" not in self.rules.order_types:
            raise ValueError("Binance symbol does not advertise MARKET order support.")
        return self

    @property
    def identity(self) -> PortfolioPaperInstrumentIdentity:
        return PortfolioPaperInstrumentIdentity(
            symbol=self.symbol,
            base_asset=self.base_asset,
            quote_currency=self.quote_currency,
            provider=self.provider,
            venue=self.venue,
            market=self.market,
            asset_type=self.asset_type,
            calendar_id=self.calendar_id,
            execution_model=self.execution_model,
        )


class PortfolioPaperEligibilityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[
        "eligible",
        "verification_required",
        "research_only",
        "rejected",
    ]
    instrument: Instrument
    reasons: tuple[str, ...] = Field(min_length=1)
    contract: PortfolioPaperInstrumentContract | None = None

    @model_validator(mode="after")
    def validate_assessment(self) -> Self:
        if (self.status == "eligible") != (self.contract is not None):
            raise ValueError("Only eligible instruments may carry an execution contract.")
        return self


def assess_portfolio_paper_eligibility(
    instrument: Instrument,
    *,
    rules: BinanceSpotTradingRules | None = None,
    now: datetime | None = None,
) -> PortfolioPaperEligibilityAssessment:
    """Separate broad research coverage from narrowly verified paper execution."""

    symbol = _canonical_symbol(instrument.symbol)
    provider = instrument.provider
    market = instrument.market
    asset_type = instrument.asset_type.strip().lower()
    exchange = instrument.exchange.strip()
    currency = instrument.currency.strip().upper()
    if provider != "binance" or market != "CRYPTO":
        if provider == "macro" or market in {"INDEX", "FOREX"}:
            reason = (
                "指数或参考汇率不是可直接成交报价，只能继续用于研究回测。"
            )
        elif provider == "futures" or market == "FUTURES":
            reason = (
                "连续期货缺少具体合约、乘数、到期与换月契约，只能继续用于研究回测。"
            )
        else:
            reason = (
                "股票和 ETF 尚未完成原始价格、公司行动、交易日历与实时成交契约。"
            )
        return PortfolioPaperEligibilityAssessment(
            status="research_only",
            instrument=instrument,
            reasons=(reason,),
        )
    if asset_type != "spot" or exchange != "Binance Spot":
        return PortfolioPaperEligibilityAssessment(
            status="rejected",
            instrument=instrument,
            reasons=("第一版只接受 Binance Spot 现货标的。",),
        )
    if currency != "USDT" or not symbol.endswith("USDT"):
        return PortfolioPaperEligibilityAssessment(
            status="research_only",
            instrument=instrument,
            reasons=(
                "第一版组合纸面跟踪要求所有资产精确使用 USDT 结算，不合并 USD 或 USDC。",
            ),
        )
    if rules is None:
        return PortfolioPaperEligibilityAssessment(
            status="verification_required",
            instrument=instrument,
            reasons=(
                "需要从 Binance exchangeInfo 重新验证交易状态、步长与最小名义金额。",
            ),
        )
    created_at = _aware_utc(now or datetime.now(UTC), "Assessment time")
    try:
        contract = PortfolioPaperInstrumentContract(
            symbol=symbol,
            base_asset=rules.base_asset,
            quote_currency="USDT",
            rules=rules,
            created_at=created_at,
        )
    except ValueError as error:
        return PortfolioPaperEligibilityAssessment(
            status="rejected",
            instrument=instrument,
            reasons=(str(error),),
        )
    return PortfolioPaperEligibilityAssessment(
        status="eligible",
        instrument=instrument,
        reasons=(
            "已验证为同一 Binance Spot 场所、24/7 UTC 日历和精确 USDT 结算。",
        ),
        contract=contract,
    )


class PortfolioPaperBasketIdentity(BaseModel):
    """Stable all-or-nothing, single-level top-of-book execution identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    instruments: tuple[PortfolioPaperInstrumentIdentity, ...] = Field(
        min_length=2,
        max_length=6,
    )
    execution_model: Literal[
        "all_or_nothing_single_level_top_of_book"
    ] = (
        "all_or_nothing_single_level_top_of_book"
    )
    long_only: Literal[True] = True
    leverage_allowed: Literal[False] = False
    partial_execution_allowed: Literal[False] = False
    max_quote_age_seconds: int = Field(default=5, ge=1, le=30)
    max_quote_skew_seconds: int = Field(default=2, ge=0, le=10)
    max_clock_skew_seconds: int = Field(default=5, ge=1, le=30)
    execution_window_seconds: int = Field(default=900, ge=180, le=1_800)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        symbols = tuple(instrument.symbol for instrument in self.instruments)
        if len(set(symbols)) != len(symbols):
            raise ValueError("Paper basket symbols must be unique.")
        return self

    @property
    def identity_hash(self) -> str:
        return canonical_payload_hash(self.model_dump(mode="json"))


class PortfolioPaperBasketContract(BaseModel):
    """All-or-nothing paper basket limited to one quoted order-book level.

    Every intended quantity must fit the fresh best-price level. This contract
    does not model liquidity or fills from deeper order-book levels.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    instruments: tuple[PortfolioPaperInstrumentContract, ...] = Field(
        min_length=2,
        max_length=6,
    )
    execution_model: Literal[
        "all_or_nothing_single_level_top_of_book"
    ] = (
        "all_or_nothing_single_level_top_of_book"
    )
    long_only: Literal[True] = True
    leverage_allowed: Literal[False] = False
    partial_execution_allowed: Literal[False] = False
    max_quote_age_seconds: int = Field(default=5, ge=1, le=30)
    max_quote_skew_seconds: int = Field(default=2, ge=0, le=10)
    max_clock_skew_seconds: int = Field(default=5, ge=1, le=30)
    execution_window_seconds: int = Field(default=900, ge=180, le=1_800)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "Basket contract creation time")

    @model_validator(mode="after")
    def validate_basket(self) -> Self:
        _aware_utc(self.created_at, "Basket contract creation time")
        symbols = tuple(contract.symbol for contract in self.instruments)
        if len(set(symbols)) != len(symbols):
            raise ValueError("Paper basket symbols must be unique.")
        if any(contract.created_at > self.created_at for contract in self.instruments):
            raise ValueError("Instrument contracts cannot postdate the basket contract.")
        require_fresh_basket_rules(self, at=self.created_at)
        return self

    @property
    def basket_hash(self) -> str:
        return canonical_payload_hash(self.model_dump(mode="json"))

    @property
    def identity(self) -> PortfolioPaperBasketIdentity:
        return PortfolioPaperBasketIdentity(
            instruments=tuple(contract.identity for contract in self.instruments),
            execution_model=self.execution_model,
            long_only=self.long_only,
            leverage_allowed=self.leverage_allowed,
            partial_execution_allowed=self.partial_execution_allowed,
            max_quote_age_seconds=self.max_quote_age_seconds,
            max_quote_skew_seconds=self.max_quote_skew_seconds,
            max_clock_skew_seconds=self.max_clock_skew_seconds,
            execution_window_seconds=self.execution_window_seconds,
        )


def require_fresh_basket_rules(
    basket: PortfolioPaperBasketContract,
    *,
    at: datetime,
) -> None:
    checked_at = _aware_utc(at, "Basket-rule check time")
    if basket.created_at > checked_at:
        raise ValueError("Paper basket evidence cannot be created after its use.")
    for contract in basket.instruments:
        if contract.created_at > checked_at:
            raise ValueError("Instrument contract cannot be created after its use.")
        verified_at = _aware_utc(
            contract.rules.verified_at,
            "Trading-rule verification time",
        )
        if verified_at > checked_at:
            raise ValueError("Trading rules cannot be verified after their use.")
        if checked_at - verified_at > _CONTRACT_MAX_AGE:
            raise ValueError("Binance trading rules are stale for this paper decision.")


def _bar_revision_payload(
    *,
    symbol: str,
    open_time: datetime,
    close_time: datetime,
    exact_open: str,
    exact_high: str,
    exact_low: str,
    exact_close: str,
    exact_volume: str,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "provider": "binance",
        "venue": "Binance Spot",
        "symbol": symbol,
        "interval": "1d",
        "open_time": open_time.astimezone(UTC).isoformat(),
        "close_time": close_time.astimezone(UTC).isoformat(),
        "value_encoding": "canonical-decimal-string-v1",
        "open": exact_open,
        "high": exact_high,
        "low": exact_low,
        "close": exact_close,
        "volume": exact_volume,
    }


def _parse_binance_bar_number(value: object, label: str) -> float:
    """Parse one provider number without treating flags as prices or quantities."""

    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"Binance finalized bar {label} must be a finite number.")
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"Binance finalized bar {label} must be a finite number.")
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(
            f"Binance finalized bar {label} must be a finite number."
        ) from error
    if not isfinite(parsed):
        raise ValueError(f"Binance finalized bar {label} must be a finite number.")
    return parsed


def _parse_exact_binance_bar_number(
    value: object,
    label: str,
    *,
    allow_zero: bool,
) -> str:
    """Validate and canonicalize exact provider evidence without a float round-trip."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_BINANCE_DECIMAL_SOURCE_LENGTH
        or _BINANCE_FIXED_POINT_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(
            f"Binance finalized bar exact {label} must be a finite decimal string."
        )
    try:
        exact = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(
            f"Binance finalized bar exact {label} must be a finite decimal string."
        ) from error
    if (
        not exact.is_finite()
        or exact < 0
        or (not allow_zero and exact == 0)
    ):
        raise ValueError(
            f"Binance finalized bar exact {label} has an invalid sign or value."
        )
    projection = float(exact)
    if not isfinite(projection) or (exact != 0 and projection == 0):
        raise ValueError(
            f"Binance finalized bar exact {label} cannot be represented "
            "by the analysis projection."
        )
    if exact == 0:
        return "0"
    canonical = format(exact, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if len(canonical) > _MAX_BINANCE_CANONICAL_DECIMAL_LENGTH:
        raise ValueError(
            f"Binance finalized bar exact {label} is outside supported bounds."
        )
    return canonical


class CertifiedPortfolioBar(BaseModel):
    """One finalized, revision-addressed daily input used by a paper decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    provider: Literal["binance"] = "binance"
    venue: Literal["Binance Spot"] = "Binance Spot"
    symbol: str
    interval: Literal["1d"] = "1d"
    session: str
    open_time: datetime
    close_time: datetime
    finalized_at: datetime
    observed_at: datetime
    exchange_server_time: datetime
    clock_checked_at: datetime
    open: float = Field(gt=0, strict=True, allow_inf_nan=False)
    high: float = Field(gt=0, strict=True, allow_inf_nan=False)
    low: float = Field(gt=0, strict=True, allow_inf_nan=False)
    close: float = Field(gt=0, strict=True, allow_inf_nan=False)
    volume: float = Field(ge=0, strict=True, allow_inf_nan=False)
    exact_open: str
    exact_high: str
    exact_low: str
    exact_close: str
    exact_volume: str
    revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _canonical_symbol(value)

    @field_validator(
        "open_time",
        "close_time",
        "finalized_at",
        "observed_at",
        "exchange_server_time",
        "clock_checked_at",
    )
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value, "Certified bar timestamp")

    @field_validator(
        "exact_open",
        "exact_high",
        "exact_low",
        "exact_close",
        "exact_volume",
        mode="before",
    )
    @classmethod
    def normalize_exact_number(cls, value: object, info: Any) -> str:
        field_name = str(info.field_name)
        return _parse_exact_binance_bar_number(
            value,
            field_name.removeprefix("exact_"),
            allow_zero=field_name == "exact_volume",
        )

    @model_validator(mode="after")
    def validate_bar(self) -> Self:
        open_time = _aware_utc(self.open_time, "Bar open time")
        close_time = _aware_utc(self.close_time, "Bar close time")
        finalized_at = _aware_utc(self.finalized_at, "Bar finalization time")
        observed_at = _aware_utc(self.observed_at, "Bar observation time")
        exchange_server_time = _aware_utc(
            self.exchange_server_time,
            "Bar exchange server time",
        )
        clock_checked_at = _aware_utc(
            self.clock_checked_at,
            "Bar clock-check time",
        )
        if not open_time < close_time <= finalized_at <= observed_at:
            raise ValueError(
                "Bar timing must satisfy open < close <= finalized <= observed."
            )
        expected_open = pd.Timestamp(open_time).normalize().to_pydatetime()
        expected_close = open_time + timedelta(days=1) - timedelta(milliseconds=1)
        if open_time != expected_open or close_time != expected_close:
            raise ValueError(
                "Binance 1d bars must span one complete UTC midnight session."
            )
        if exchange_server_time < finalized_at:
            raise ValueError("Exchange time has not passed the bar finalization buffer.")
        if clock_checked_at > observed_at:
            raise ValueError("Bar clock check cannot occur after its observation.")
        if abs((exchange_server_time - clock_checked_at).total_seconds()) > 5:
            raise ValueError("Exchange and local clocks differ too much to certify a bar.")
        expected_session = str(pd.Timestamp(open_time).normalize().isoformat())
        if self.session != expected_session:
            raise ValueError("Bar session must equal its UTC open-date label.")
        if self.high < max(self.open, self.close) or self.low > min(
            self.open,
            self.close,
        ):
            raise ValueError("Bar OHLC values are internally inconsistent.")
        if self.high < self.low:
            raise ValueError("Bar high cannot be below its low.")
        exact_values = {
            "open": Decimal(self.exact_open),
            "high": Decimal(self.exact_high),
            "low": Decimal(self.exact_low),
            "close": Decimal(self.exact_close),
            "volume": Decimal(self.exact_volume),
        }
        if exact_values["high"] < max(
            exact_values["open"],
            exact_values["close"],
        ) or exact_values["low"] > min(
            exact_values["open"],
            exact_values["close"],
        ):
            raise ValueError("Exact bar OHLC values are internally inconsistent.")
        if exact_values["high"] < exact_values["low"]:
            raise ValueError("Exact bar high cannot be below its low.")
        projected_values = {
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }
        for field_name, exact in exact_values.items():
            expected_projection = 0.0 if exact == 0 else float(exact)
            actual_projection = projected_values[field_name]
            if actual_projection != expected_projection or (
                actual_projection == 0
                and copysign(1.0, actual_projection) < 0
            ):
                raise ValueError(
                    f"Bar {field_name} analysis projection does not match "
                    "its exact provider evidence."
                )
        expected_hash = canonical_payload_hash(
            _bar_revision_payload(
                symbol=self.symbol,
                open_time=open_time,
                close_time=close_time,
                exact_open=self.exact_open,
                exact_high=self.exact_high,
                exact_low=self.exact_low,
                exact_close=self.exact_close,
                exact_volume=self.exact_volume,
            )
        )
        if self.revision_hash != expected_hash:
            raise ValueError("Bar revision hash does not match its source values.")
        return self


def certify_binance_daily_bar(
    row: Mapping[str, Any],
    contract: PortfolioPaperInstrumentContract,
) -> CertifiedPortfolioBar:
    """Fail closed unless a provider row carries the finalized-bar evidence."""

    try:
        row_symbol = _canonical_symbol(row["symbol"])
        row_interval = str(row["interval"]).strip()
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Binance finalized bar identity is incomplete.") from error
    if row_symbol != contract.symbol:
        raise ValueError("Binance bar symbol does not match its execution contract.")
    if row_interval != "1d":
        raise ValueError("Portfolio paper accepts only Binance 1d source bars.")
    if row.get("finalized") is not True:
        raise ValueError("Binance bar is not explicitly marked finalized.")
    if row.get("exchange_clock_verified") is not True:
        raise ValueError("Binance bar lacks exchange-clock finalization evidence.")
    try:
        open_time = pd.Timestamp(row["open_time"]).to_pydatetime()
        close_time = pd.Timestamp(row["close_time"]).to_pydatetime()
        finalized_at = pd.Timestamp(row["finalized_at"]).to_pydatetime()
        observed_at = pd.Timestamp(row["observed_at"]).to_pydatetime()
        exchange_server_time = pd.Timestamp(
            row["exchange_server_time"]
        ).to_pydatetime()
        clock_checked_at = pd.Timestamp(row["clock_checked_at"]).to_pydatetime()
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Binance finalized bar evidence is incomplete.") from error
    try:
        open_price = _parse_binance_bar_number(row["open"], "open")
        high_price = _parse_binance_bar_number(row["high"], "high")
        low_price = _parse_binance_bar_number(row["low"], "low")
        close_price = _parse_binance_bar_number(row["close"], "close")
        volume = _parse_binance_bar_number(row["volume"], "volume")
        exact_open = _parse_exact_binance_bar_number(
            row["exact_open"],
            "open",
            allow_zero=False,
        )
        exact_high = _parse_exact_binance_bar_number(
            row["exact_high"],
            "high",
            allow_zero=False,
        )
        exact_low = _parse_exact_binance_bar_number(
            row["exact_low"],
            "low",
            allow_zero=False,
        )
        exact_close = _parse_exact_binance_bar_number(
            row["exact_close"],
            "close",
            allow_zero=False,
        )
        exact_volume = _parse_exact_binance_bar_number(
            row["exact_volume"],
            "volume",
            allow_zero=True,
        )
    except KeyError as error:
        raise ValueError("Binance finalized bar evidence is incomplete.") from error
    open_time = _aware_utc(open_time, "Bar open time")
    close_time = _aware_utc(close_time, "Bar close time")
    finalized_at = _aware_utc(finalized_at, "Bar finalization time")
    observed_at = _aware_utc(observed_at, "Bar observation time")
    exchange_server_time = _aware_utc(
        exchange_server_time,
        "Bar exchange server time",
    )
    clock_checked_at = _aware_utc(clock_checked_at, "Bar clock-check time")
    if finalized_at != close_time + _BINANCE_FINALIZATION_LAG:
        raise ValueError("Binance bar finalization buffer is not the required two minutes.")
    revision_hash = canonical_payload_hash(
        _bar_revision_payload(
            symbol=contract.symbol,
            open_time=open_time,
            close_time=close_time,
            exact_open=exact_open,
            exact_high=exact_high,
            exact_low=exact_low,
            exact_close=exact_close,
            exact_volume=exact_volume,
        )
    )
    return CertifiedPortfolioBar(
        symbol=contract.symbol,
        session=str(pd.Timestamp(open_time).normalize().isoformat()),
        open_time=open_time,
        close_time=close_time,
        finalized_at=finalized_at,
        observed_at=observed_at,
        exchange_server_time=exchange_server_time,
        clock_checked_at=clock_checked_at,
        open=open_price,
        high=high_price,
        low=low_price,
        close=close_price,
        volume=volume,
        exact_open=exact_open,
        exact_high=exact_high,
        exact_low=exact_low,
        exact_close=exact_close,
        exact_volume=exact_volume,
        revision_hash=revision_hash,
    )


def _market_input_payload(bars: Sequence[CertifiedPortfolioBar]) -> dict[str, object]:
    ordered = sorted(bars, key=lambda bar: (bar.session, bar.symbol))
    return {
        "bars": [bar.model_dump(mode="json") for bar in ordered],
    }


class PortfolioPaperDecisionCertificate(BaseModel):
    """Proof that a target used finalized inputs and existed before any quote fill."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    decision_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    target: PortfolioForwardTarget
    calculation_version: Literal["portfolio-forward-target-v1"] = (
        PORTFOLIO_TARGET_CALCULATION_VERSION
    )
    bars: tuple[CertifiedPortfolioBar, ...] = Field(
        min_length=2,
        max_length=6 * (252 + 1),
    )
    volatility_lookback: int = Field(ge=2, le=252)
    maximum_asset_weight: float = Field(gt=0, le=1)
    basket: PortfolioPaperBasketContract
    basket_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    basket_identity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decided_at: datetime
    execution_session: str
    execution_deadline: datetime

    @field_validator("decided_at", "execution_deadline")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value, "Decision timestamp")

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        decided_at = _aware_utc(self.decided_at, "Decision time")
        basket_symbols = tuple(contract.symbol for contract in self.basket.instruments)
        if set(self.target.weights) != set(basket_symbols):
            raise ValueError(
                "Decision target symbols must exactly match its fresh basket evidence."
            )
        target_symbols = basket_symbols
        if self.basket_hash != self.basket.basket_hash:
            raise ValueError("Decision basket hash does not match its evidence.")
        if self.basket_identity_hash != self.basket.identity.identity_hash:
            raise ValueError("Decision basket identity hash does not match its evidence.")
        require_fresh_basket_rules(self.basket, at=decided_at)
        bar_symbols = {bar.symbol for bar in self.bars}
        if bar_symbols != set(target_symbols):
            raise ValueError("Decision bars must exactly match target symbols.")
        identities = [(bar.symbol, bar.session) for bar in self.bars]
        if len(set(identities)) != len(identities):
            raise ValueError("Decision bars cannot repeat a symbol/session pair.")
        grouped = {
            symbol: sorted(
                (bar for bar in self.bars if bar.symbol == symbol),
                key=lambda bar: pd.Timestamp(bar.session),
            )
            for symbol in target_symbols
        }
        session_grid = tuple(bar.session for bar in grouped[target_symbols[0]])
        if not session_grid or any(
            tuple(bar.session for bar in grouped[symbol]) != session_grid
            for symbol in target_symbols[1:]
        ):
            raise ValueError(
                "Decision bars must form one complete synchronized session grid."
            )
        session_timestamps = tuple(pd.Timestamp(session) for session in session_grid)
        if any(
            current - previous != pd.Timedelta(days=1)
            for previous, current in pairwise(session_timestamps)
        ):
            raise ValueError(
                "Binance daily decision windows must contain consecutive UTC sessions."
            )
        required_sessions = (
            self.volatility_lookback + 1
            if self.target.method == "periodic_inverse_volatility"
            else 1
        )
        if len(session_grid) != required_sessions:
            raise ValueError(
                "Decision certificate must contain exactly the target calculation window."
            )
        if session_grid[-1] != self.target.information_session:
            raise ValueError(
                "Decision calculation window must end at its information session."
            )
        expected_execution = (
            pd.Timestamp(self.target.information_session) + pd.Timedelta(days=1)
        )
        if self.execution_session != expected_execution.isoformat():
            raise ValueError(
                "Decision execution session must be the next Binance UTC session."
            )
        deadline = _aware_utc(self.execution_deadline, "Decision execution deadline")
        expected_deadline = expected_execution.to_pydatetime() + timedelta(
            seconds=self.basket.execution_window_seconds
        )
        if deadline != expected_deadline:
            raise ValueError(
                "Decision execution deadline does not match its basket policy."
            )
        if decided_at > deadline:
            raise ValueError("Decision was calculated after its execution window.")
        if any(bar.observed_at > decided_at for bar in self.bars):
            raise ValueError("A decision cannot predate one of its market observations.")
        close_history = pd.DataFrame(
            {
                symbol: [bar.close for bar in grouped[symbol]]
                for symbol in target_symbols
            },
            index=pd.DatetimeIndex(pd.Timestamp(session) for session in session_grid),
        )
        expected_target = compute_next_portfolio_target(
            close_history,
            self.target.method,
            information_session=self.target.information_session,
            volatility_lookback=self.volatility_lookback,
            maximum_asset_weight=self.maximum_asset_weight,
        )
        if expected_target != self.target:
            raise ValueError(
                "Decision target cannot be reproduced from its certified close history."
            )
        expected_hash = canonical_payload_hash(_market_input_payload(self.bars))
        if self.market_input_hash != expected_hash:
            raise ValueError("Decision market-input hash does not match its bars.")
        return self


def certify_portfolio_paper_decision(
    *,
    target: PortfolioForwardTarget,
    bars: Sequence[CertifiedPortfolioBar],
    basket: PortfolioPaperBasketContract,
    configuration_hash: str,
    volatility_lookback: int,
    maximum_asset_weight: float,
    decided_at: datetime | None = None,
) -> PortfolioPaperDecisionCertificate:
    safe_bars = tuple(
        CertifiedPortfolioBar.model_validate(bar.model_dump(mode="python"))
        for bar in bars
    )
    safe_basket = PortfolioPaperBasketContract.model_validate(
        basket.model_dump(mode="python")
    )
    safe_target = PortfolioForwardTarget.model_validate(
        target.model_dump(mode="python")
    )
    safe_decided_at = _aware_utc(
        decided_at or datetime.now(UTC),
        "Decision time",
    )
    if {
        contract.symbol for contract in safe_basket.instruments
    } != set(safe_target.weights):
        raise ValueError("Basket, target, and bar symbols must match exactly.")
    require_fresh_basket_rules(safe_basket, at=safe_decided_at)
    execution_session = (
        pd.Timestamp(safe_target.information_session) + pd.Timedelta(days=1)
    )
    return PortfolioPaperDecisionCertificate(
        decision_id=uuid4().hex,
        target=safe_target,
        bars=safe_bars,
        volatility_lookback=volatility_lookback,
        maximum_asset_weight=maximum_asset_weight,
        basket=safe_basket,
        basket_hash=safe_basket.basket_hash,
        basket_identity_hash=safe_basket.identity.identity_hash,
        configuration_hash=configuration_hash,
        market_input_hash=canonical_payload_hash(_market_input_payload(safe_bars)),
        decided_at=safe_decided_at,
        execution_session=execution_session.isoformat(),
        execution_deadline=execution_session.to_pydatetime()
        + timedelta(seconds=safe_basket.execution_window_seconds),
    )


class PortfolioPaperDecisionReceipt(BaseModel):
    """Store-issued evidence that a certified decision is durably readable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    track_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    decision_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    certificate: PortfolioPaperDecisionCertificate
    certificate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    persisted_at: datetime

    @field_validator("persisted_at")
    @classmethod
    def normalize_persisted_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "Decision persistence time")

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        persisted_at = _aware_utc(self.persisted_at, "Decision persistence time")
        if self.decision_id != self.certificate.decision_id:
            raise ValueError("Decision receipt id does not match its certificate.")
        if self.certificate_hash != canonical_payload_hash(
            self.certificate.model_dump(mode="json")
        ):
            raise ValueError("Decision receipt hash does not match its certificate.")
        if persisted_at < self.certificate.decided_at:
            raise ValueError("Decision receipt cannot predate its calculation.")
        if persisted_at >= self.certificate.execution_deadline:
            raise ValueError(
                "Decision receipt must precede its certified execution deadline."
            )
        return self


def _validate_execution_quotes_against_receipt(
    *,
    basket: PortfolioPaperBasketContract,
    decision: PortfolioPaperDecisionReceipt,
    quotes: Sequence[ExecutionQuote],
    accepted_at: datetime | None = None,
) -> dict[str, ExecutionQuote]:
    """Validate fresh single-level top-of-book evidence against a receipt.

    This helper does not infer any liquidity beyond the quoted best bid/ask level.
    Callers must first authenticate the receipt in storage.
    """

    safe_basket = PortfolioPaperBasketContract.model_validate(
        basket.model_dump(mode="python")
    )
    receipt = PortfolioPaperDecisionReceipt.model_validate(
        decision.model_dump(mode="python")
    )
    certificate = receipt.certificate
    accepted = _aware_utc(accepted_at or datetime.now(UTC), "Quote acceptance time")
    if certificate.basket_identity_hash != safe_basket.identity.identity_hash:
        raise ValueError("Decision receipt does not belong to this paper basket.")
    if accepted < receipt.persisted_at:
        raise ValueError("Quote acceptance cannot predate decision persistence.")
    if (
        pd.Timestamp(accepted).normalize().isoformat()
        != certificate.execution_session
        or accepted > certificate.execution_deadline
    ):
        raise ValueError("Execution quote is outside the certified execution window.")
    require_fresh_basket_rules(safe_basket, at=accepted)
    if certificate.basket != safe_basket:
        raise ValueError(
            "Execution basket must exactly match the certified trading-rule snapshot."
        )
    quote_map: dict[str, ExecutionQuote] = {}
    for quote in quotes:
        safe_quote = ExecutionQuote.model_validate(quote.model_dump(mode="python"))
        symbol = _canonical_symbol(safe_quote.symbol)
        if symbol in quote_map:
            raise ValueError("Execution quotes must contain each symbol once.")
        quote_map[symbol] = safe_quote
    expected_symbols = tuple(contract.symbol for contract in safe_basket.instruments)
    if set(certificate.target.weights) != set(expected_symbols):
        raise ValueError("Decision target does not exactly match the paper basket.")
    if set(quote_map) != set(expected_symbols):
        raise ValueError("Execution quote set must exactly match the paper basket.")
    observations: list[datetime] = []
    contracts = {
        contract.symbol: contract for contract in safe_basket.instruments
    }
    for symbol in expected_symbols:
        quote = quote_map[symbol]
        contract = contracts[symbol]
        if quote.provider != contract.provider or quote.venue != contract.venue:
            raise ValueError(
                "Single-level top-of-book quote provider or venue does not match contract."
            )
        request_started_at = _aware_utc(
            quote.request_started_at,
            "Quote request start time",
        )
        if request_started_at < receipt.persisted_at:
            raise ValueError("Execution quote request predates decision persistence.")
        if (
            accepted - request_started_at
        ).total_seconds() > safe_basket.max_quote_age_seconds:
            raise ValueError("Execution quote request exceeded the freshness budget.")
        observed_at = _aware_utc(quote.observed_at, "Quote observation time")
        if accepted < observed_at:
            raise ValueError("Execution quote cannot be observed in the future.")
        if (
            accepted - observed_at
        ).total_seconds() > safe_basket.max_quote_age_seconds:
            raise ValueError("Execution quote is too old for paper execution.")
        reference_observed_at = _aware_utc(
            quote.notional_reference_observed_at,
            "Notional reference observation time",
        )
        if accepted < reference_observed_at:
            raise ValueError("Notional reference evidence cannot postdate acceptance.")
        if (
            accepted - reference_observed_at
        ).total_seconds() > safe_basket.max_quote_age_seconds:
            raise ValueError("Notional reference evidence is too old for paper execution.")
        exchange_reference_observed_at = _aware_utc(
            quote.exchange_reference_observed_at,
            "Exchange-reference observation time",
        )
        if accepted < exchange_reference_observed_at:
            raise ValueError(
                "Exchange-reference evidence cannot postdate acceptance."
            )
        if (
            accepted - exchange_reference_observed_at
        ).total_seconds() > safe_basket.max_quote_age_seconds:
            raise ValueError(
                "Exchange-reference evidence is too old for paper execution."
            )
        if (
            quote.notional_reference_kind == "average_price"
            and quote.notional_reference_window_minutes
            != contract.rules.notional_average_price_minutes
        ):
            raise ValueError(
                "Average-price evidence does not match the trading-rule window."
            )
        if (
            quote.notional_reference_kind == "last_price"
            and contract.rules.notional_average_price_minutes != 0
        ):
            raise ValueError(
                "Last-price evidence requires a zero-minute trading-rule window."
            )
        clock_checked_at = _aware_utc(
            quote.clock_checked_at,
            "Quote clock-check time",
        )
        exchange_time = _aware_utc(
            quote.exchange_server_time,
            "Exchange server time",
        )
        if accepted < clock_checked_at:
            raise ValueError("Quote clock evidence cannot postdate quote acceptance.")
        if (
            abs((exchange_time - clock_checked_at).total_seconds())
            > safe_basket.max_clock_skew_seconds
        ):
            raise ValueError("Exchange and local clocks differ beyond the contract limit.")
        exchange_reference_at = _aware_utc(
            quote.exchange_reference_at,
            "Exchange-reference event time",
        )
        if quote.notional_reference_at > exchange_time:
            raise ValueError("Notional reference timestamp cannot postdate exchange time.")
        if exchange_reference_at > exchange_time:
            raise ValueError(
                "Exchange-reference event timestamp cannot postdate exchange time."
            )
        if (
            accepted - exchange_reference_at
        ).total_seconds() > (
            safe_basket.max_quote_age_seconds
            + safe_basket.max_clock_skew_seconds
        ):
            raise ValueError(
                "Exchange-reference event is too old for paper execution."
            )
        if quote.cache_used:
            raise ValueError(
                "Cached top-of-book quotes cannot be used for single-level paper execution."
            )
        observations.append(observed_at)
    if (
        max(observations) - min(observations)
    ).total_seconds() > safe_basket.max_quote_skew_seconds:
        raise ValueError("Execution quote observations are too far apart.")
    return {symbol: quote_map[symbol] for symbol in expected_symbols}
