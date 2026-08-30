from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from math import isfinite
from time import monotonic
from typing import Any, Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import WebSocketException

from .base import DataProvider
from .cache import SQLiteCache
from .models import (
    BarInterval,
    BinanceSettlementDailyBarEvidence,
    Citation,
    DataEnvelope,
    ExecutionQuote,
    Instrument,
)
from .symbols import instrument_score, normalize_search_text

BINANCE_MARKET_DATA_URL = "https://data-api.binance.vision"
BINANCE_REFERENCE_STREAM_URL = "wss://data-stream.binance.vision/ws"
BINANCE_API_FALLBACKS = (
    BINANCE_MARKET_DATA_URL,
    "https://api.binance.com",
    "https://api-gcp.binance.com",
)
BINANCE_INTERVAL_MS: dict[BarInterval, int] = {
    "15m": 15 * 60 * 1_000,
    "1h": 60 * 60 * 1_000,
    "4h": 4 * 60 * 60 * 1_000,
    "1d": 24 * 60 * 60 * 1_000,
    "1wk": 7 * 24 * 60 * 60 * 1_000,
}
BINANCE_MAX_DAYS: dict[BarInterval, int] = {
    "15m": 180,
    "1h": 730,
    "4h": 1_825,
    "1d": 7_300,
    "1wk": 14_600,
}
BINANCE_FINALIZATION_LAG = timedelta(minutes=2)
BINANCE_LIVE_HISTORY_CACHE_TTL = timedelta(minutes=1)
BINANCE_HISTORICAL_CACHE_TTL = timedelta(minutes=30)
BINANCE_EXECUTION_BUNDLE_TIMEOUT_SECONDS = 4.0
# A public endpoint being unreachable must not hold a research action hostage
# for six independent 20-second socket timeouts.  We still try alternative
# Binance public origins and both proxy modes, but cap the whole request.
BINANCE_PUBLIC_REQUEST_DEADLINE_SECONDS = 18.0
BINANCE_PUBLIC_REQUEST_ATTEMPT_TIMEOUT_SECONDS = 6.0
_MAX_BINANCE_DECIMAL_SOURCE_LENGTH = 128
_MAX_BINANCE_CANONICAL_DECIMAL_LENGTH = 512
_BINANCE_FIXED_POINT_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?", flags=re.ASCII)

ASSET_NAMES: dict[str, tuple[str, tuple[str, ...]]] = {
    "BTC": ("Bitcoin", ("比特币",)),
    "ETH": ("Ethereum", ("以太坊", "Ether")),
    "BNB": ("BNB", ("币安币",)),
    "SOL": ("Solana", ("索拉纳",)),
    "XRP": ("XRP", ("瑞波", "Ripple")),
    "DOGE": ("Dogecoin", ("狗狗币",)),
    "ADA": ("Cardano", ("艾达币",)),
    "AVAX": ("Avalanche", ("雪崩",)),
    "LINK": ("Chainlink", ("链链",)),
    "DOT": ("Polkadot", ("波卡",)),
    "TRX": ("TRON", ("波场",)),
    "LTC": ("Litecoin", ("莱特币",)),
    "SUI": ("Sui", ()),
    "TON": ("Toncoin", ()),
}


def _required_decimal_string(
    payload: dict[str, Any],
    field: str,
    *,
    context: str,
    allow_zero: bool = False,
) -> Decimal:
    raw_value = payload.get(field)
    if (
        not isinstance(raw_value, str)
        or not raw_value
        or raw_value != raw_value.strip()
    ):
        raise RuntimeError(f"Binance returned no valid {context} {field}.")
    try:
        value = Decimal(raw_value)
    except InvalidOperation as exc:
        raise RuntimeError(f"Binance returned an invalid {context} {field}.") from exc
    if not value.is_finite() or value < 0 or (not allow_zero and value == 0):
        raise RuntimeError(f"Binance returned an invalid {context} {field}.")
    return value


def _canonical_kline_decimal(
    raw_value: object,
    *,
    field: str,
    allow_zero: bool,
) -> tuple[str, float]:
    """Preserve one exact Binance K-line decimal beside its float projection."""

    if (
        not isinstance(raw_value, str)
        or not raw_value
        or raw_value != raw_value.strip()
        or len(raw_value) > _MAX_BINANCE_DECIMAL_SOURCE_LENGTH
        or _BINANCE_FIXED_POINT_PATTERN.fullmatch(raw_value) is None
    ):
        raise RuntimeError(f"Binance returned an invalid K-line {field}.")
    try:
        exact = Decimal(raw_value)
    except InvalidOperation as error:
        raise RuntimeError(f"Binance returned an invalid K-line {field}.") from error
    if (
        not exact.is_finite()
        or exact < 0
        or (not allow_zero and exact == 0)
    ):
        raise RuntimeError(f"Binance returned an invalid K-line {field}.")

    projected = float(exact)
    if not isfinite(projected) or (exact != 0 and projected == 0):
        raise RuntimeError(
            f"Binance K-line {field} cannot be represented by the analysis projection."
        )
    if exact == 0:
        return "0", 0.0

    canonical = format(exact, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if len(canonical) > _MAX_BINANCE_CANONICAL_DECIMAL_LENGTH:
        raise RuntimeError(f"Binance returned an invalid K-line {field}.")
    return canonical, projected


def _required_bool(
    payload: dict[str, Any],
    field: str,
    *,
    context: str,
) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise RuntimeError(f"Binance returned no valid {context} {field}.")
    return value


def _required_nonnegative_int(
    payload: dict[str, Any],
    field: str,
    *,
    context: str,
) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"Binance returned no valid {context} {field}.")
    return value


def _required_timestamp(
    payload: dict[str, Any],
    field: str,
    *,
    context: str,
) -> datetime:
    milliseconds = payload.get(field)
    if (
        isinstance(milliseconds, bool)
        or not isinstance(milliseconds, int)
        or milliseconds < 0
    ):
        raise RuntimeError(f"Binance returned no valid {context} {field}.")
    try:
        return datetime.fromtimestamp(milliseconds / 1_000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise RuntimeError(
            f"Binance returned an invalid {context} {field}."
        ) from exc


class BinanceSpotTradingRules(BaseModel):
    """Fresh exchangeInfo evidence required before paper execution is eligible."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    spot_trading_allowed: StrictBool
    order_types: tuple[str, ...]
    lot_step_size: Decimal = Field(gt=0)
    lot_min_quantity: Decimal = Field(gt=0)
    lot_max_quantity: Decimal = Field(gt=0)
    market_step_size: Decimal = Field(gt=0)
    market_min_quantity: Decimal = Field(gt=0)
    market_max_quantity: Decimal = Field(gt=0)
    min_notional: Decimal = Field(gt=0)
    min_notional_applies_to_market: StrictBool
    max_notional: Decimal | None = Field(default=None, gt=0)
    max_notional_applies_to_market: StrictBool
    notional_average_price_minutes: int = Field(ge=0, strict=True)
    verified_at: datetime
    source: str = "Binance Spot exchangeInfo"

    @field_validator("symbol", "base_asset", "quote_asset", "status", mode="before")
    @classmethod
    def normalize_identifier(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Binance trading-rule identifiers must be non-empty strings.")
        return value.strip().upper()

    @field_validator("order_types", mode="before")
    @classmethod
    def normalize_order_types(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("Binance order types must be a sequence.")
        if any(not isinstance(item, str) for item in value):
            raise ValueError("Binance order types must contain only strings.")
        normalized = tuple(item.strip().upper() for item in value)
        if not normalized or any(not item for item in normalized):
            raise ValueError("Binance order types must not be empty.")
        if len(normalized) != len(set(normalized)):
            raise ValueError("Binance order types must not contain duplicates.")
        return normalized

    @field_validator("verified_at")
    @classmethod
    def normalize_verified_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "Binance trading rules require a timezone-aware timestamp."
            )
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_rules(self) -> BinanceSpotTradingRules:
        decimals = [
            self.lot_step_size,
            self.lot_min_quantity,
            self.lot_max_quantity,
            self.market_step_size,
            self.market_min_quantity,
            self.min_notional,
            self.market_max_quantity,
        ]
        if self.max_notional is not None:
            decimals.append(self.max_notional)
        if not all(value.is_finite() for value in decimals):
            raise ValueError("Binance trading-rule quantities must be finite.")
        if self.lot_min_quantity > self.lot_max_quantity:
            raise ValueError("Binance LOT_SIZE minimum exceeds its maximum.")
        if self.market_min_quantity > self.market_max_quantity:
            raise ValueError("Binance MARKET_LOT_SIZE minimum exceeds its maximum.")
        if (
            self.max_notional is not None
            and self.min_notional_applies_to_market
            and self.max_notional_applies_to_market
            and self.min_notional > self.max_notional
        ):
            raise ValueError("Active Binance market notional minimum exceeds its maximum.")
        if self.max_notional is None and self.max_notional_applies_to_market:
            raise ValueError(
                "A market maximum-notional flag requires a maximum notional."
            )
        if (
            self.verified_at.tzinfo is None
            or self.verified_at.utcoffset() is None
        ):
            raise ValueError("Binance trading rules require a timezone-aware timestamp.")
        if not self.symbol.endswith(self.quote_asset):
            raise ValueError("Binance symbol does not end with its quote asset.")
        if self.symbol[: -len(self.quote_asset)] != self.base_asset:
            raise ValueError("Binance symbol does not match its base and quote assets.")
        return self


class BinanceProvider(DataProvider):
    """Public Binance Spot market data; no account or API key is used."""

    name = "binance"

    def __init__(self, cache: SQLiteCache | None = None) -> None:
        self.cache = cache or SQLiteCache()

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        normalized = symbol.strip().upper().replace("/", "").replace("-", "")
        if normalized in ASSET_NAMES:
            return f"{normalized}USDT"
        return normalized

    @staticmethod
    def _request_json(path: str, params: dict[str, Any] | None = None) -> Any:
        error: Exception | None = None
        deadline = monotonic() + BINANCE_PUBLIC_REQUEST_DEADLINE_SECONDS
        for base_url in BINANCE_API_FALLBACKS:
            for trust_environment in (True, False):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                try:
                    with httpx.Client(
                        timeout=min(
                            BINANCE_PUBLIC_REQUEST_ATTEMPT_TIMEOUT_SECONDS,
                            remaining,
                        ),
                        trust_env=trust_environment,
                    ) as client:
                        response = client.get(
                            f"{base_url}{path}",
                            params=params,
                            headers={"User-Agent": "QuantSieve/0.1"},
                        )
                        response.raise_for_status()
                        return response.json()
                except Exception as exc:
                    error = exc
            if deadline - monotonic() <= 0:
                break
        raise RuntimeError("Binance public market data is unavailable.") from error

    async def _cached_json(
        self,
        operation: str,
        ttl: timedelta,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        key = self.cache.make_key(f"{self.name}:{operation}", **(params or {}))
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        value = await asyncio.to_thread(self._request_json, f"/api/v3/{operation}", params)
        self.cache.set(key, value, ttl)
        return value

    async def history(
        self, symbol: str, start: date | None = None, end: date | None = None
    ) -> DataEnvelope:
        return await self.history_interval(symbol, start, end, interval="1d")

    async def history_interval(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        *,
        interval: BarInterval = "1d",
    ) -> DataEnvelope:
        normalized = self._normalize_symbol(symbol)
        utc_today = datetime.now(UTC).date()
        start = start or (utc_today - timedelta(days=365))
        end = end or utc_today
        if start > end:
            raise ValueError("start must be on or before end")
        requested_days = (end - start).days + 1
        max_days = BINANCE_MAX_DAYS[interval]
        if requested_days > max_days:
            raise ValueError(
                f"Binance {interval} K-lines support at most {max_days} days per "
                "backtest in QuantSieve. Shorten the date range or use a larger K-line."
            )

        start_ms = int(datetime.combine(start, time.min, tzinfo=UTC).timestamp() * 1_000)
        end_ms = int(
            datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC).timestamp() * 1_000
        ) - 1
        rows: list[dict[str, Any]] = []
        cursor = start_ms
        live_window = end >= utc_today
        exchange_server_time: datetime | None = None
        exchange_clock_checked_at: datetime | None = None
        if live_window:
            clock_payload = await asyncio.to_thread(
                self._request_json,
                "/api/v3/time",
            )
            exchange_clock_checked_at = datetime.now(UTC)
            if not isinstance(clock_payload, dict) or "serverTime" not in clock_payload:
                raise RuntimeError("Binance returned no exchange server time.")
            exchange_server_time = datetime.fromtimestamp(
                int(clock_payload["serverTime"]) / 1_000,
                tz=UTC,
            )
            if (
                abs(
                    (
                        exchange_server_time - exchange_clock_checked_at
                    ).total_seconds()
                )
                > 5
            ):
                raise RuntimeError(
                    "Binance and local clocks differ too much to certify finalized bars."
                )
        cache_ttl = (
            BINANCE_LIVE_HISTORY_CACHE_TTL
            if live_window
            else BINANCE_HISTORICAL_CACHE_TTL
        )
        while cursor <= end_ms:
            payload = await self._cached_json(
                "klines",
                cache_ttl,
                params={
                    "symbol": normalized,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                },
            )
            if not isinstance(payload, list) or not payload:
                break
            observed_at = datetime.now(UTC)
            finalization_reference = exchange_server_time or observed_at
            finalized_cutoff = finalization_reference - BINANCE_FINALIZATION_LAG
            for item in payload:
                if not isinstance(item, list) or len(item) < 7:
                    continue
                open_time = int(item[0])
                if open_time > end_ms:
                    continue
                close_time = int(item[6])
                close_timestamp = datetime.fromtimestamp(close_time / 1_000, tz=UTC)
                if close_timestamp > finalized_cutoff:
                    continue
                exact_open, open_projection = _canonical_kline_decimal(
                    item[1],
                    field="open",
                    allow_zero=False,
                )
                exact_high, high_projection = _canonical_kline_decimal(
                    item[2],
                    field="high",
                    allow_zero=False,
                )
                exact_low, low_projection = _canonical_kline_decimal(
                    item[3],
                    field="low",
                    allow_zero=False,
                )
                exact_close, close_projection = _canonical_kline_decimal(
                    item[4],
                    field="close",
                    allow_zero=False,
                )
                exact_volume, volume_projection = _canonical_kline_decimal(
                    item[5],
                    field="volume",
                    allow_zero=True,
                )
                rows.append(
                    {
                        "symbol": normalized,
                        "interval": interval,
                        "date": datetime.fromtimestamp(open_time / 1_000, tz=UTC).isoformat(),
                        "open_time": datetime.fromtimestamp(
                            open_time / 1_000,
                            tz=UTC,
                        ).isoformat(),
                        "close_time": close_timestamp.isoformat(),
                        "observed_at": observed_at.isoformat(),
                        "exchange_server_time": (
                            exchange_server_time.isoformat()
                            if exchange_server_time is not None
                            else None
                        ),
                        "clock_checked_at": (
                            exchange_clock_checked_at.isoformat()
                            if exchange_clock_checked_at is not None
                            else None
                        ),
                        "exchange_clock_verified": exchange_server_time is not None,
                        "finalized_at": (
                            close_timestamp + BINANCE_FINALIZATION_LAG
                        ).isoformat(),
                        "finalized": True,
                        "open": open_projection,
                        "high": high_projection,
                        "low": low_projection,
                        "close": close_projection,
                        "volume": volume_projection,
                        "exact_open": exact_open,
                        "exact_high": exact_high,
                        "exact_low": exact_low,
                        "exact_close": exact_close,
                        "exact_volume": exact_volume,
                        "quote_volume": float(item[7]) if len(item) > 7 else None,
                        "trades": int(item[8]) if len(item) > 8 else None,
                    }
                )
            next_cursor = int(payload[-1][0]) + BINANCE_INTERVAL_MS[interval]
            if next_cursor <= cursor or len(payload) < 1000:
                break
            cursor = next_cursor
        if not rows:
            raise LookupError(
                f"Binance returned no {interval} history for {normalized}."
            )
        envelope = self._envelope(normalized, "history", rows)
        envelope.metadata.update(
            {
                "interval": interval,
                "market_calendar": "24/7",
                "bars": len(rows),
                "finalized_bars_only": True,
                "exchange_clock_verified": exchange_server_time is not None,
                "finalization_lag_seconds": int(
                    BINANCE_FINALIZATION_LAG.total_seconds()
                ),
            }
        )
        return envelope

    async def quote(self, symbol: str) -> DataEnvelope:
        normalized = self._normalize_symbol(symbol)
        payload = await self._cached_json(
            "ticker/24hr",
            timedelta(seconds=20),
            params={"symbol": normalized},
        )
        if not isinstance(payload, dict):
            raise LookupError(f"Binance returned no quote for {normalized}.")
        row = {
            "date": datetime.now(UTC).isoformat(),
            "open": float(payload["openPrice"]),
            "high": float(payload["highPrice"]),
            "low": float(payload["lowPrice"]),
            "close": float(payload["lastPrice"]),
            "volume": float(payload["volume"]),
            "quote_volume": float(payload["quoteVolume"]),
            "price_change": float(payload["priceChange"]),
            "price_change_percent": float(payload["priceChangePercent"]),
            "bid": float(payload["bidPrice"]),
            "ask": float(payload["askPrice"]),
            "trades": int(payload["count"]),
        }
        return self._envelope(normalized, "quote", [row])

    async def trading_rules(self, symbol: str) -> BinanceSpotTradingRules:
        """Fetch uncached spot eligibility and quantity filters from exchangeInfo."""

        normalized = self._normalize_symbol(symbol)
        payload = await asyncio.to_thread(
            self._request_json,
            "/api/v3/exchangeInfo",
            {"symbol": normalized},
        )
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Binance returned invalid exchangeInfo for {normalized}."
            )
        rows = payload.get("symbols")
        if not isinstance(rows, list) or len(rows) != 1:
            raise RuntimeError(
                f"Binance returned an ambiguous exchangeInfo identity for {normalized}."
            )
        row = rows[0]
        if not isinstance(row, dict) or row.get("symbol") != normalized:
            raise RuntimeError(
                f"Binance exchangeInfo identity does not match {normalized}."
            )
        raw_filters = row.get("filters")
        if not isinstance(raw_filters, list):
            raise RuntimeError(
                f"Binance returned no trading filters for {normalized}."
            )
        filters: dict[str, dict[str, Any]] = {}
        for item in raw_filters:
            if not isinstance(item, dict):
                raise RuntimeError(
                    f"Binance returned an invalid trading filter for {normalized}."
                )
            filter_type = item.get("filterType")
            if not isinstance(filter_type, str) or not filter_type:
                raise RuntimeError(
                    f"Binance returned an unidentified trading filter for {normalized}."
                )
            if filter_type in filters:
                raise RuntimeError(
                    f"Binance returned duplicate {filter_type} filters for {normalized}."
                )
            filters[filter_type] = item
        lot_size = filters.get("LOT_SIZE")
        if not isinstance(lot_size, dict):
            raise RuntimeError(f"Binance returned no LOT_SIZE filter for {normalized}.")
        market_lot_size = filters.get("MARKET_LOT_SIZE")
        if market_lot_size is not None and not isinstance(market_lot_size, dict):
            raise RuntimeError(
                f"Binance returned an invalid MARKET_LOT_SIZE filter for {normalized}."
            )
        notional_filter = filters.get("NOTIONAL")
        minimum_notional_filter = filters.get("MIN_NOTIONAL")
        if notional_filter is None and minimum_notional_filter is None:
            raise RuntimeError(f"Binance returned no notional filter for {normalized}.")
        base_asset = row.get("baseAsset")
        quote_asset = row.get("quoteAsset")
        trading_status = row.get("status")
        order_types = row.get("orderTypes")
        if (
            not isinstance(base_asset, str)
            or not base_asset
            or not isinstance(quote_asset, str)
            or not quote_asset
            or not isinstance(trading_status, str)
            or not trading_status
            or not isinstance(order_types, list)
        ):
            raise RuntimeError(
                f"Binance returned incomplete trading identity for {normalized}."
            )
        spot_trading_allowed = _required_bool(
            row,
            "isSpotTradingAllowed",
            context="exchangeInfo",
        )
        lot_step_size = _required_decimal_string(
            lot_size,
            "stepSize",
            context="LOT_SIZE",
        )
        lot_min_quantity = _required_decimal_string(
            lot_size,
            "minQty",
            context="LOT_SIZE",
        )
        lot_max_quantity = _required_decimal_string(
            lot_size,
            "maxQty",
            context="LOT_SIZE",
        )
        if market_lot_size is None:
            market_step_size = lot_step_size
            market_min_quantity = lot_min_quantity
            market_max_quantity = lot_max_quantity
        else:
            market_step_size = _required_decimal_string(
                market_lot_size,
                "stepSize",
                context="MARKET_LOT_SIZE",
                allow_zero=True,
            )
            market_min_quantity = _required_decimal_string(
                market_lot_size,
                "minQty",
                context="MARKET_LOT_SIZE",
                allow_zero=True,
            )
            market_max_quantity = _required_decimal_string(
                market_lot_size,
                "maxQty",
                context="MARKET_LOT_SIZE",
                allow_zero=True,
            )
            if market_step_size == 0:
                market_step_size = lot_step_size
            if market_min_quantity == 0:
                market_min_quantity = lot_min_quantity
            if market_max_quantity == 0:
                market_max_quantity = lot_max_quantity

        minimum_sources: list[tuple[Decimal, bool, int]] = []
        max_notional: Decimal | None = None
        max_notional_applies_to_market = False
        notional_window: int | None = None
        if minimum_notional_filter is not None:
            minimum_sources.append(
                (
                    _required_decimal_string(
                        minimum_notional_filter,
                        "minNotional",
                        context="MIN_NOTIONAL",
                    ),
                    _required_bool(
                        minimum_notional_filter,
                        "applyToMarket",
                        context="MIN_NOTIONAL",
                    ),
                    _required_nonnegative_int(
                        minimum_notional_filter,
                        "avgPriceMins",
                        context="MIN_NOTIONAL",
                    ),
                )
            )
        if notional_filter is not None:
            notional_minimum = _required_decimal_string(
                notional_filter,
                "minNotional",
                context="NOTIONAL",
            )
            notional_minimum_applies = _required_bool(
                notional_filter,
                "applyMinToMarket",
                context="NOTIONAL",
            )
            max_notional = _required_decimal_string(
                notional_filter,
                "maxNotional",
                context="NOTIONAL",
            )
            max_notional_applies_to_market = _required_bool(
                notional_filter,
                "applyMaxToMarket",
                context="NOTIONAL",
            )
            notional_window = _required_nonnegative_int(
                notional_filter,
                "avgPriceMins",
                context="NOTIONAL",
            )
            minimum_sources.append(
                (notional_minimum, notional_minimum_applies, notional_window)
            )
        active_minimums = [item for item in minimum_sources if item[1]]
        if active_minimums:
            min_notional, _, _ = max(active_minimums, key=lambda item: item[0])
            min_notional_applies_to_market = True
        else:
            min_notional, _, _ = max(minimum_sources, key=lambda item: item[0])
            min_notional_applies_to_market = False
        active_windows = {
            average_minutes
            for _, applies, average_minutes in minimum_sources
            if applies
        }
        if max_notional_applies_to_market:
            if notional_window is None:
                raise RuntimeError(
                    f"Binance returned no NOTIONAL average window for {normalized}."
                )
            active_windows.add(notional_window)
        if len(active_windows) > 1:
            raise RuntimeError(
                "Binance returned incompatible average-price windows for active "
                f"market notional filters on {normalized}."
            )
        if active_windows:
            notional_average_price_minutes = next(iter(active_windows))
        elif notional_window is not None:
            notional_average_price_minutes = notional_window
        else:
            notional_average_price_minutes = minimum_sources[0][2]
        return BinanceSpotTradingRules(
            symbol=normalized,
            base_asset=base_asset,
            quote_asset=quote_asset,
            status=trading_status,
            spot_trading_allowed=spot_trading_allowed,
            order_types=tuple(order_types),
            lot_step_size=lot_step_size,
            lot_min_quantity=lot_min_quantity,
            lot_max_quantity=lot_max_quantity,
            market_step_size=market_step_size,
            market_min_quantity=market_min_quantity,
            market_max_quantity=market_max_quantity,
            min_notional=min_notional,
            min_notional_applies_to_market=min_notional_applies_to_market,
            max_notional=max_notional,
            max_notional_applies_to_market=max_notional_applies_to_market,
            notional_average_price_minutes=notional_average_price_minutes,
            verified_at=datetime.now(UTC),
        )

    @staticmethod
    def _execution_bundle_deadline_error() -> RuntimeError:
        return RuntimeError(
            "Binance execution evidence exceeded its four-second collection deadline."
        )

    @classmethod
    async def _execution_reference_event(
        cls,
        normalized: str,
        *,
        deadline: float,
    ) -> tuple[bool, Decimal | None, datetime, datetime]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise cls._execution_bundle_deadline_error()
        stream_url = (
            f"{BINANCE_REFERENCE_STREAM_URL}/"
            f"{normalized.lower()}@referencePrice"
        )
        try:
            async with websocket_connect(
                stream_url,
                compression=None,
                user_agent_header="QuantSieve/0.1",
                proxy=None,
                open_timeout=remaining,
                ping_interval=None,
                close_timeout=0,
                max_size=4_096,
                max_queue=1,
            ) as websocket:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise cls._execution_bundle_deadline_error()
                raw_event = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=remaining,
                )
                observed_at = datetime.now(UTC)
        except TimeoutError as exc:
            raise cls._execution_bundle_deadline_error() from exc
        except (OSError, WebSocketException) as exc:
            raise RuntimeError(
                "Binance reference-price stream is unavailable."
            ) from exc
        if monotonic() > deadline:
            raise cls._execution_bundle_deadline_error()
        if not isinstance(raw_event, str):
            raise RuntimeError("Binance returned a non-text reference-price event.")
        try:
            payload = json.loads(raw_event)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Binance returned invalid reference-price stream JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Binance returned no reference-price stream object.")
        if payload.get("e") != "referencePrice":
            raise RuntimeError("Binance returned the wrong reference-price event type.")
        if payload.get("s") != normalized:
            raise RuntimeError(
                f"Binance reference-price stream identity does not match {normalized}."
            )
        if "r" not in payload:
            raise RuntimeError(
                f"Binance returned no reference price field for {normalized}."
            )
        exchange_reference_at = _required_timestamp(
            payload,
            "t",
            context="reference price stream",
        )
        raw_reference_price = payload["r"]
        if raw_reference_price is None:
            return False, None, exchange_reference_at, observed_at
        exchange_reference_price = _required_decimal_string(
            payload,
            "r",
            context="reference price stream",
        )
        return (
            True,
            exchange_reference_price,
            exchange_reference_at,
            observed_at,
        )

    @classmethod
    async def _execution_bundle_response(
        cls,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: dict[str, Any] | None,
        deadline: float,
        context: str,
    ) -> httpx.Response:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise cls._execution_bundle_deadline_error()
        try:
            response = await client.get(path, params=params, timeout=remaining)
        except httpx.TimeoutException as exc:
            raise cls._execution_bundle_deadline_error() from exc
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"Binance {context} evidence is unavailable."
            ) from exc
        if monotonic() > deadline:
            raise cls._execution_bundle_deadline_error()
        return response

    @staticmethod
    def _execution_bundle_json(
        response: httpx.Response,
        *,
        context: str,
    ) -> Any:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Binance {context} request failed.") from exc
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Binance returned invalid {context} evidence."
            ) from exc

    @classmethod
    async def _execution_quote_bundle(
        cls,
        normalized: str,
        rules: BinanceSpotTradingRules,
        request_started_at: datetime,
        deadline: float,
        exchange_reference_available: bool,
        exchange_reference_price: Decimal | None,
        exchange_reference_at: datetime,
        exchange_reference_observed_at: datetime,
    ) -> ExecutionQuote:
        """Collect all evidence through one client and one shared deadline."""

        async with httpx.AsyncClient(
            base_url=BINANCE_MARKET_DATA_URL,
            headers={"User-Agent": "QuantSieve/0.1"},
            timeout=BINANCE_EXECUTION_BUNDLE_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            book_response = await cls._execution_bundle_response(
                client,
                "/api/v3/ticker/bookTicker",
                params={"symbol": normalized},
                deadline=deadline,
                context="book ticker",
            )
            observed_at = datetime.now(UTC)
            payload = cls._execution_bundle_json(
                book_response,
                context="book ticker",
            )
            if not isinstance(payload, dict):
                raise LookupError(f"Binance returned no book ticker for {normalized}.")
            payload_symbol = payload.get("symbol")
            if (
                not isinstance(payload_symbol, str)
                or payload_symbol.strip().upper() != normalized
            ):
                raise RuntimeError(
                    f"Binance book ticker identity does not match {normalized}."
                )
            bid_price = _required_decimal_string(
                payload,
                "bidPrice",
                context="book ticker",
            )
            ask_price = _required_decimal_string(
                payload,
                "askPrice",
                context="book ticker",
            )
            bid_quantity = _required_decimal_string(
                payload,
                "bidQty",
                context="book ticker",
                allow_zero=True,
            )
            ask_quantity = _required_decimal_string(
                payload,
                "askQty",
                context="book ticker",
                allow_zero=True,
            )

            if not exchange_reference_available:
                if rules.notional_average_price_minutes >= 1:
                    average_response = await cls._execution_bundle_response(
                        client,
                        "/api/v3/avgPrice",
                        params={"symbol": normalized},
                        deadline=deadline,
                        context="average price",
                    )
                    notional_reference_observed_at = datetime.now(UTC)
                    average_payload = cls._execution_bundle_json(
                        average_response,
                        context="average price",
                    )
                    if not isinstance(average_payload, dict):
                        raise RuntimeError(
                            f"Binance returned no average-price payload for {normalized}."
                        )
                    average_symbol = average_payload.get("symbol")
                    if average_symbol is not None and average_symbol != normalized:
                        raise RuntimeError(
                            f"Binance average-price identity does not match {normalized}."
                        )
                    average_minutes = _required_nonnegative_int(
                        average_payload,
                        "mins",
                        context="average price",
                    )
                    if average_minutes != rules.notional_average_price_minutes:
                        raise RuntimeError(
                            "Binance average-price window does not match current "
                            f"trading rules for {normalized}."
                        )
                    notional_reference_price = _required_decimal_string(
                        average_payload,
                        "price",
                        context="average price",
                    )
                    notional_reference_kind: Literal[
                        "exchange_reference",
                        "average_price",
                        "last_price",
                    ] = "average_price"
                    notional_reference_window_minutes: int | None = average_minutes
                    notional_reference_at: datetime | None = _required_timestamp(
                        average_payload,
                        "closeTime",
                        context="average price",
                    )
                else:
                    trade_response = await cls._execution_bundle_response(
                        client,
                        "/api/v3/trades",
                        params={"symbol": normalized, "limit": 1},
                        deadline=deadline,
                        context="last trade",
                    )
                    notional_reference_observed_at = datetime.now(UTC)
                    trades_payload = cls._execution_bundle_json(
                        trade_response,
                        context="last trade",
                    )
                    if (
                        not isinstance(trades_payload, list)
                        or len(trades_payload) != 1
                        or not isinstance(trades_payload[0], dict)
                    ):
                        raise RuntimeError(
                            f"Binance returned no unique last trade for {normalized}."
                        )
                    trade_payload = trades_payload[0]
                    trade_id = trade_payload.get("id")
                    if (
                        isinstance(trade_id, bool)
                        or not isinstance(trade_id, int)
                        or trade_id < 0
                    ):
                        raise RuntimeError(
                            f"Binance returned no valid last-trade id for {normalized}."
                        )
                    notional_reference_price = _required_decimal_string(
                        trade_payload,
                        "price",
                        context="last trade",
                    )
                    notional_reference_kind = "last_price"
                    notional_reference_window_minutes = 0
                    notional_reference_at = _required_timestamp(
                        trade_payload,
                        "time",
                        context="last trade",
                    )
            else:
                notional_reference_observed_at = exchange_reference_observed_at
                if exchange_reference_price is None:
                    raise RuntimeError(
                        f"Binance returned no available reference price for {normalized}."
                    )
                notional_reference_price = exchange_reference_price
                notional_reference_kind = "exchange_reference"
                notional_reference_window_minutes = None
                notional_reference_at = exchange_reference_at

            clock_response = await cls._execution_bundle_response(
                client,
                "/api/v3/time",
                params=None,
                deadline=deadline,
                context="exchange time",
            )
            clock_checked_at = datetime.now(UTC)
            clock_payload = cls._execution_bundle_json(
                clock_response,
                context="exchange time",
            )
            if not isinstance(clock_payload, dict):
                raise RuntimeError("Binance returned no exchange server time.")
            exchange_server_time = _required_timestamp(
                clock_payload,
                "serverTime",
                context="exchange time",
            )
            if notional_reference_at is None:
                raise RuntimeError(
                    f"Binance returned no timestamp for {normalized} notional evidence."
                )

        quote = ExecutionQuote(
            symbol=normalized,
            provider="binance",
            venue="Binance Spot",
            bid_price=bid_price,
            ask_price=ask_price,
            bid_quantity=bid_quantity,
            ask_quantity=ask_quantity,
            notional_reference_price=notional_reference_price,
            notional_reference_kind=notional_reference_kind,
            notional_reference_window_minutes=notional_reference_window_minutes,
            notional_reference_at=notional_reference_at,
            notional_reference_observed_at=notional_reference_observed_at,
            exchange_reference_available=exchange_reference_available,
            exchange_reference_at=exchange_reference_at,
            exchange_reference_observed_at=exchange_reference_observed_at,
            request_started_at=request_started_at,
            observed_at=observed_at,
            exchange_server_time=exchange_server_time,
            clock_checked_at=clock_checked_at,
            cache_used=False,
        )
        if monotonic() > deadline:
            raise cls._execution_bundle_deadline_error()
        return quote

    async def execution_quote(
        self,
        symbol: str,
        *,
        rules: BinanceSpotTradingRules,
    ) -> ExecutionQuote:
        """Fetch one uncached, exchange-rule-aligned execution evidence bundle."""

        normalized = self._normalize_symbol(symbol)
        safe_rules = BinanceSpotTradingRules.model_validate(
            rules.model_dump(mode="python")
        )
        if safe_rules.symbol != normalized:
            raise ValueError(
                f"Binance trading rules do not belong to {normalized}."
            )
        request_started_at = datetime.now(UTC)
        deadline = monotonic() + BINANCE_EXECUTION_BUNDLE_TIMEOUT_SECONDS
        try:
            (
                exchange_reference_available,
                exchange_reference_price,
                exchange_reference_at,
                exchange_reference_observed_at,
            ) = await self._execution_reference_event(
                normalized,
                deadline=deadline,
            )
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise self._execution_bundle_deadline_error()
            return await asyncio.wait_for(
                self._execution_quote_bundle(
                    normalized,
                    safe_rules,
                    request_started_at,
                    deadline,
                    exchange_reference_available,
                    exchange_reference_price,
                    exchange_reference_at,
                    exchange_reference_observed_at,
                ),
                timeout=remaining,
            )
        except TimeoutError as exc:
            raise self._execution_bundle_deadline_error() from exc

    @staticmethod
    def _settlement_evidence_deadline_error() -> RuntimeError:
        return RuntimeError(
            "Binance settlement evidence exceeded its shared deadline."
        )

    @classmethod
    async def _settlement_evidence_response(
        cls,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: dict[str, Any] | None,
        deadline: float,
        context: str,
    ) -> httpx.Response:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise cls._settlement_evidence_deadline_error()
        try:
            response = await client.get(
                path,
                params=params,
                timeout=remaining,
            )
        except httpx.TimeoutException as error:
            raise cls._settlement_evidence_deadline_error() from error
        except httpx.RequestError as error:
            raise RuntimeError(
                f"Binance settlement {context} evidence is unavailable."
            ) from error
        if monotonic() > deadline:
            raise cls._settlement_evidence_deadline_error()
        return response

    @classmethod
    async def _settlement_daily_bar_bundle(
        cls,
        normalized: str,
        session_open: datetime,
        *,
        deadline: float,
        request_started_at: datetime,
    ) -> BinanceSettlementDailyBarEvidence:
        session_close = session_open + timedelta(days=1) - timedelta(
            milliseconds=1
        )
        finalized_at = session_close + BINANCE_FINALIZATION_LAG
        async with httpx.AsyncClient(
            base_url=BINANCE_MARKET_DATA_URL,
            headers={"User-Agent": "QuantSieve/0.1"},
            timeout=None,
            trust_env=False,
        ) as client:
            time_response = await cls._settlement_evidence_response(
                client,
                "/api/v3/time",
                params=None,
                deadline=deadline,
                context="exchange clock",
            )
            time_payload = cls._execution_bundle_json(
                time_response,
                context="settlement exchange clock",
            )
            clock_checked_at = datetime.now(UTC)
            if not isinstance(time_payload, dict):
                raise RuntimeError(
                    "Binance returned no settlement exchange clock."
                )
            exchange_server_time = _required_timestamp(
                time_payload,
                "serverTime",
                context="settlement exchange clock",
            )
            if (
                abs(
                    (
                        exchange_server_time - clock_checked_at
                    ).total_seconds()
                )
                > 5
            ):
                raise RuntimeError(
                    "Binance and local clocks differ too much to certify "
                    "settlement evidence."
                )
            if exchange_server_time < finalized_at:
                raise RuntimeError(
                    "Binance exchange time has not passed settlement finalization."
                )

            start_ms = int(session_open.timestamp() * 1_000)
            close_ms = int(session_close.timestamp() * 1_000)
            kline_response = await cls._settlement_evidence_response(
                client,
                "/api/v3/klines",
                params={
                    "symbol": normalized,
                    "interval": "1d",
                    "startTime": start_ms,
                    "endTime": close_ms,
                    "limit": 2,
                },
                deadline=deadline,
                context="daily K-line",
            )
            kline_payload = cls._execution_bundle_json(
                kline_response,
                context="settlement daily K-line",
            )
            observed_at = datetime.now(UTC)

        if (
            not isinstance(kline_payload, list)
            or len(kline_payload) != 1
            or not isinstance(kline_payload[0], list)
            or len(kline_payload[0]) < 7
        ):
            raise RuntimeError(
                "Binance returned no unique settlement daily K-line."
            )
        row = kline_payload[0]
        raw_open_time = row[0]
        raw_close_time = row[6]
        if (
            isinstance(raw_open_time, bool)
            or not isinstance(raw_open_time, int)
            or raw_open_time != start_ms
            or isinstance(raw_close_time, bool)
            or not isinstance(raw_close_time, int)
            or raw_close_time != close_ms
        ):
            raise RuntimeError(
                "Binance settlement K-line session does not match the request."
            )
        exact_open, open_projection = _canonical_kline_decimal(
            row[1],
            field="open",
            allow_zero=False,
        )
        exact_high, high_projection = _canonical_kline_decimal(
            row[2],
            field="high",
            allow_zero=False,
        )
        exact_low, low_projection = _canonical_kline_decimal(
            row[3],
            field="low",
            allow_zero=False,
        )
        exact_close, close_projection = _canonical_kline_decimal(
            row[4],
            field="close",
            allow_zero=False,
        )
        exact_volume, volume_projection = _canonical_kline_decimal(
            row[5],
            field="volume",
            allow_zero=True,
        )
        if monotonic() > deadline:
            raise cls._settlement_evidence_deadline_error()
        evidence = BinanceSettlementDailyBarEvidence(
            symbol=normalized,
            session=session_open.isoformat(),
            request_started_at=request_started_at,
            observed_at=observed_at,
            exchange_server_time=exchange_server_time,
            clock_checked_at=clock_checked_at,
            open_time=session_open,
            close_time=session_close,
            finalized_at=finalized_at,
            open=open_projection,
            high=high_projection,
            low=low_projection,
            close=close_projection,
            volume=volume_projection,
            exact_open=exact_open,
            exact_high=exact_high,
            exact_low=exact_low,
            exact_close=exact_close,
            exact_volume=exact_volume,
        )
        if monotonic() > deadline:
            raise cls._settlement_evidence_deadline_error()
        return evidence

    async def settlement_daily_bar(
        self,
        symbol: str,
        *,
        session: date,
        deadline: float,
    ) -> BinanceSettlementDailyBarEvidence:
        """Fetch one uncached finalized UTC daily bar for paper settlement."""

        normalized = self._normalize_symbol(symbol)
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not isfinite(float(deadline))
        ):
            raise ValueError(
                "Binance settlement deadline must be a finite monotonic time."
            )
        if not isinstance(session, date) or isinstance(session, datetime):
            raise ValueError("Binance settlement session must be a UTC date.")
        absolute_deadline = float(deadline)
        remaining = absolute_deadline - monotonic()
        if remaining <= 0:
            raise self._settlement_evidence_deadline_error()
        session_open = datetime.combine(session, time.min, tzinfo=UTC)
        request_started_at = datetime.now(UTC)
        try:
            return await asyncio.wait_for(
                self._settlement_daily_bar_bundle(
                    normalized,
                    session_open,
                    deadline=absolute_deadline,
                    request_started_at=request_started_at,
                ),
                timeout=remaining,
            )
        except TimeoutError as error:
            raise self._settlement_evidence_deadline_error() from error

    async def fundamentals(self, symbol: str) -> DataEnvelope:
        return self._unsupported(symbol, "fundamentals")

    async def capital_flow(self, symbol: str) -> DataEnvelope:
        return self._unsupported(symbol, "capital_flow")

    async def news(self, symbol: str, limit: int = 20) -> DataEnvelope:
        del limit
        return self._unsupported(symbol, "news")

    async def search(self, query: str, limit: int = 10) -> list[Instrument]:
        payload = await self._cached_json("exchangeInfo", timedelta(hours=6))
        symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
        needle = normalize_search_text(query)
        matches: list[tuple[int, int, int, Instrument]] = []
        for row in symbols:
            if not isinstance(row, dict):
                continue
            if row.get("status") != "TRADING" or not row.get("isSpotTradingAllowed", True):
                continue
            symbol = str(row.get("symbol", "")).upper()
            base = str(row.get("baseAsset", "")).upper()
            quote = str(row.get("quoteAsset", "")).upper()
            if not symbol or not base or not quote:
                continue
            asset_name, aliases = ASSET_NAMES.get(base, (base, ()))
            instrument = Instrument(
                symbol=symbol,
                name=f"{asset_name} / {quote}",
                market="CRYPTO",
                exchange="Binance Spot",
                currency=quote,
                provider="binance",
                asset_type="spot",
            )
            values = (symbol, base, quote, asset_name, instrument.name, *aliases)
            normalized_values = tuple(normalize_search_text(value) for value in values)
            if needle and not any(needle in value for value in normalized_values):
                continue
            score = instrument_score(instrument, query, aliases=(base, asset_name, *aliases))
            quote_priority = {"USDT": 0, "USDC": 1, "FDUSD": 2, "BTC": 3}.get(quote, 4)
            exact_quote_match = 0 if needle and needle == normalize_search_text(quote) else 1
            matches.append((exact_quote_match, score, quote_priority, instrument))
        matches.sort(key=lambda item: (item[0], item[1], item[2], item[3].symbol))
        return [instrument for _, _, _, instrument in matches[:limit]]

    @staticmethod
    def _envelope(symbol: str, kind: str, rows: list[dict[str, Any]]) -> DataEnvelope:
        return DataEnvelope(
            symbol=symbol,
            kind=kind,
            rows=rows,
            citations=[
                Citation(
                    source="Binance Spot",
                    url="https://developers.binance.com/en/docs/products/spot/rest-api",
                    note="Public market-data endpoint; no trading credentials are used.",
                )
            ],
        )

    @staticmethod
    def _unsupported(symbol: str, kind: str) -> DataEnvelope:
        return DataEnvelope(
            symbol=symbol.strip().upper(),
            kind=kind,
            rows=[],
            citations=[
                Citation(
                    source="Binance Spot",
                    url="https://developers.binance.com/en/docs/products/spot/rest-api",
                    note=f"Binance Spot does not expose normalized {kind} data in this adapter.",
                )
            ],
        )
