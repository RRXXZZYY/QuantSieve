from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import copysign, isfinite
from typing import Any, Literal

import pandas as pd
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

Market = Literal["CN", "US", "ETF", "INDEX", "FOREX", "CRYPTO", "FUTURES"]
ProviderName = Literal["akshare", "yfinance", "binance", "futures", "macro"]
BarInterval = Literal["15m", "1h", "4h", "1d", "1wk"]


class Citation(BaseModel):
    source: str
    url: str | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    as_of: datetime | None = None
    note: str | None = None


class DataEnvelope(BaseModel):
    symbol: str
    kind: str
    rows: list[dict[str, Any]]
    citations: list[Citation]
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_frame(self) -> pd.DataFrame:
        frame = pd.DataFrame(self.rows)
        if "date" in frame.columns:
            frame["date"] = pd.to_datetime(frame["date"], utc=True)
            frame = frame.set_index("date")
        return frame


class BinanceSettlementDailyBarEvidence(BaseModel):
    """Uncached, exchange-clock-certified Binance UTC daily settlement bar."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    provider: Literal["binance"] = "binance"
    venue: Literal["Binance Spot"] = "Binance Spot"
    symbol: str
    session: str
    interval: Literal["1d"] = "1d"
    cache_used: Literal[False] = False
    request_started_at: datetime
    observed_at: datetime
    exchange_server_time: datetime
    clock_checked_at: datetime
    exchange_clock_verified: Literal[True] = True
    open_time: datetime
    close_time: datetime
    finalized_at: datetime
    finalization_lag_seconds: Literal[120] = 120
    finalized: Literal[True] = True
    open: float = Field(gt=0, strict=True, allow_inf_nan=False)
    high: float = Field(gt=0, strict=True, allow_inf_nan=False)
    low: float = Field(gt=0, strict=True, allow_inf_nan=False)
    close: float = Field(gt=0, strict=True, allow_inf_nan=False)
    volume: float = Field(ge=0, strict=True, allow_inf_nan=False)
    exact_open: str = Field(min_length=1, max_length=512)
    exact_high: str = Field(min_length=1, max_length=512)
    exact_low: str = Field(min_length=1, max_length=512)
    exact_close: str = Field(min_length=1, max_length=512)
    exact_volume: str = Field(min_length=1, max_length=512)

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: object) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip().upper()
        ):
            raise ValueError(
                "Binance settlement evidence symbol must be canonical."
            )
        return value

    @field_validator("session", mode="before")
    @classmethod
    def validate_session(cls, value: object) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(
                "Binance settlement evidence session must be canonical."
            )
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(
                "Binance settlement evidence session must be canonical."
            ) from error
        if (
            parsed.tzinfo is None
            or parsed.utcoffset() is None
            or parsed.astimezone(UTC).isoformat() != value
            or parsed.astimezone(UTC).time() != datetime.min.time()
        ):
            raise ValueError(
                "Binance settlement evidence session must be canonical UTC midnight."
            )
        return value

    @field_validator(
        "request_started_at",
        "observed_at",
        "exchange_server_time",
        "clock_checked_at",
        "open_time",
        "close_time",
        "finalized_at",
    )
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "Binance settlement evidence timestamps must be timezone-aware."
            )
        return value.astimezone(UTC)

    @field_validator(
        "exact_open",
        "exact_high",
        "exact_low",
        "exact_close",
        "exact_volume",
        mode="before",
    )
    @classmethod
    def validate_exact_number(cls, value: object, info: Any) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
        ):
            raise ValueError(
                "Binance settlement exact values must be canonical decimals."
            )
        try:
            exact = Decimal(value)
        except InvalidOperation as error:
            raise ValueError(
                "Binance settlement exact values must be canonical decimals."
            ) from error
        allow_zero = info.field_name == "exact_volume"
        if (
            not exact.is_finite()
            or exact < 0
            or (not allow_zero and exact == 0)
        ):
            raise ValueError(
                "Binance settlement exact values have an invalid value."
            )
        canonical = "0" if exact == 0 else format(exact, "f")
        if "." in canonical:
            canonical = canonical.rstrip("0").rstrip(".")
        if canonical != value:
            raise ValueError(
                "Binance settlement exact values must use canonical decimals."
            )
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> BinanceSettlementDailyBarEvidence:
        expected_open = datetime.combine(
            self.open_time.date(),
            datetime.min.time(),
            tzinfo=UTC,
        )
        expected_close = self.open_time + timedelta(days=1) - timedelta(
            milliseconds=1
        )
        if (
            self.open_time != expected_open
            or self.close_time != expected_close
            or self.session != self.open_time.isoformat()
            or self.finalized_at
            != self.close_time + timedelta(seconds=self.finalization_lag_seconds)
        ):
            raise ValueError(
                "Binance settlement evidence does not span its exact UTC session."
            )
        if not (
            self.request_started_at
            <= self.clock_checked_at
            <= self.observed_at
        ):
            raise ValueError(
                "Binance settlement provenance timestamps are out of order."
            )
        if (
            abs(
                (
                    self.exchange_server_time - self.clock_checked_at
                ).total_seconds()
            )
            > 5
        ):
            raise ValueError(
                "Binance and local clocks differ too much for settlement evidence."
            )
        if self.exchange_server_time < self.finalized_at:
            raise ValueError(
                "Binance exchange time has not passed settlement finalization."
            )
        exact_values = {
            "open": Decimal(self.exact_open),
            "high": Decimal(self.exact_high),
            "low": Decimal(self.exact_low),
            "close": Decimal(self.exact_close),
            "volume": Decimal(self.exact_volume),
        }
        projected_values = {
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }
        if exact_values["high"] < max(
            exact_values["open"],
            exact_values["close"],
        ) or exact_values["low"] > min(
            exact_values["open"],
            exact_values["close"],
        ):
            raise ValueError(
                "Binance settlement exact OHLC values are inconsistent."
            )
        if exact_values["high"] < exact_values["low"]:
            raise ValueError(
                "Binance settlement exact high cannot be below its low."
            )
        for field_name, exact in exact_values.items():
            projection = float(exact)
            actual = projected_values[field_name]
            if (
                not isfinite(projection)
                or (exact != 0 and projection == 0)
                or actual != projection
                or (actual == 0 and copysign(1.0, actual) < 0)
            ):
                raise ValueError(
                    "Binance settlement analysis projection does not match "
                    f"exact {field_name} evidence."
                )
        return self


class Instrument(BaseModel):
    symbol: str
    name: str
    market: Market
    exchange: str
    currency: str
    provider: ProviderName
    asset_type: str = "equity"


class ExecutionQuote(BaseModel):
    """An uncached, receive-time-bounded quote for paper execution only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    provider: ProviderName
    venue: str
    bid_price: Decimal = Field(gt=0)
    ask_price: Decimal = Field(gt=0)
    bid_quantity: Decimal = Field(ge=0)
    ask_quantity: Decimal = Field(ge=0)
    notional_reference_price: Decimal = Field(gt=0)
    notional_reference_kind: Literal[
        "exchange_reference",
        "average_price",
        "last_price",
    ]
    notional_reference_window_minutes: StrictInt | None
    notional_reference_at: datetime
    notional_reference_observed_at: datetime
    exchange_reference_available: StrictBool
    exchange_reference_at: datetime
    exchange_reference_observed_at: datetime
    request_started_at: datetime
    observed_at: datetime
    exchange_server_time: datetime
    clock_checked_at: datetime
    cache_used: Literal[False] = False

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Execution quote symbol must be a non-empty string.")
        return value.strip().upper()

    @field_validator(
        "notional_reference_at",
        "notional_reference_observed_at",
        "exchange_reference_at",
        "exchange_reference_observed_at",
        "request_started_at",
        "observed_at",
        "exchange_server_time",
        "clock_checked_at",
    )
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Execution quote timestamps must be timezone-aware.")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_quote(self) -> ExecutionQuote:
        decimal_values = (
            self.bid_price,
            self.ask_price,
            self.bid_quantity,
            self.ask_quantity,
            self.notional_reference_price,
        )
        if not all(value.is_finite() for value in decimal_values):
            raise ValueError("Execution quote prices and quantities must be finite.")
        if self.ask_price < self.bid_price:
            raise ValueError("Execution quote ask price cannot be below bid price.")
        if self.exchange_reference_available:
            if self.notional_reference_kind != "exchange_reference":
                raise ValueError(
                    "An available exchange reference must be the chosen reference."
                )
            if (
                self.notional_reference_at != self.exchange_reference_at
                or self.notional_reference_observed_at
                != self.exchange_reference_observed_at
            ):
                raise ValueError(
                    "Chosen exchange-reference evidence must retain its raw timestamps."
                )
        elif self.notional_reference_kind == "exchange_reference":
            raise ValueError(
                "Unavailable exchange-reference evidence requires a fallback price."
            )
        if self.notional_reference_kind == "exchange_reference":
            if self.notional_reference_window_minutes is not None:
                raise ValueError(
                    "Exchange reference-price evidence cannot have an average window."
                )
        elif self.notional_reference_kind == "average_price":
            if (
                isinstance(self.notional_reference_window_minutes, bool)
                or not isinstance(self.notional_reference_window_minutes, int)
                or self.notional_reference_window_minutes < 1
            ):
                raise ValueError(
                    "Average-price evidence requires a positive integer window."
                )
        elif self.notional_reference_window_minutes != 0:
            raise ValueError(
                "Last-price evidence requires an explicit zero-minute window."
            )
        timestamps = (
            self.request_started_at,
            self.observed_at,
            self.notional_reference_at,
            self.notional_reference_observed_at,
            self.exchange_reference_at,
            self.exchange_reference_observed_at,
            self.exchange_server_time,
            self.clock_checked_at,
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("Execution quote timestamps must be timezone-aware.")
        if self.observed_at < self.request_started_at:
            raise ValueError("Execution quote cannot arrive before its request starts.")
        bounded_observation_times = (
            self.observed_at,
            self.notional_reference_observed_at,
            self.exchange_reference_observed_at,
        )
        if any(
            value < self.request_started_at or value > self.clock_checked_at
            for value in bounded_observation_times
        ):
            raise ValueError(
                "Execution observations must fall between request start and clock check."
            )
        if (
            self.notional_reference_at > self.exchange_server_time
            or self.exchange_reference_at > self.exchange_server_time
        ):
            raise ValueError(
                "Price evidence cannot postdate exchange server time."
            )
        return self
