from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import Any

import pandas as pd

from .base import DataProvider
from .cache import SQLiteCache
from .models import BarInterval, Citation, DataEnvelope, Instrument
from .symbols import rank_instruments
from .utils import frame_records

AKSHARE_FUTURES_DOCS = "https://akshare.akfamily.xyz/data/futures/futures.html"

# The upstream directory identifies a foreign-futures *instrument series*, but
# does not provide a selected expiry, roll rule, margin schedule, or broker
# execution specification.  Keep that boundary attached to every envelope so
# downstream research cannot accidentally present the series as a trade-ready
# futures contract.
FOREIGN_FUTURES_RESEARCH_METADATA: dict[str, Any] = {
    "instrument_scope": "foreign_futures_reference_series",
    "instrument_scope_note": (
        "Public foreign-futures price series for research only. The upstream code "
        "does not identify a selected expiry; QuantSieve does not model margin, "
        "contract rolls, expiry, currency conversion, or broker execution."
    ),
    "contract_month_identified": False,
    "contract_roll_modeled": False,
    "execution_ready": False,
}


class FuturesProvider(DataProvider):
    """Global commodity futures through AKShare's Sina Finance adapter."""

    name = "futures"

    def __init__(self, cache: SQLiteCache | None = None) -> None:
        self.cache = cache or SQLiteCache()

    @staticmethod
    def _akshare() -> Any:
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError(
                "Global futures support requires quantsieve-providers[china]."
            ) from exc
        return ak

    async def _cached_frame(
        self,
        operation: str,
        loader: Callable[[], pd.DataFrame],
        ttl: timedelta,
        **parameters: Any,
    ) -> list[dict[str, Any]]:
        key = self.cache.make_key(f"{self.name}:{operation}", **parameters)
        cached = self.cache.get(key)
        if cached is not None:
            return list(cached)
        try:
            frame = await asyncio.to_thread(loader)
        except Exception as exc:
            raise RuntimeError(
                f"Global futures data could not load {operation}; "
                "the upstream source may be unavailable."
            ) from exc
        rows = frame_records(frame)
        self.cache.set(key, rows, ttl)
        return rows

    async def history(
        self, symbol: str, start: date | None = None, end: date | None = None
    ) -> DataEnvelope:
        normalized = symbol.strip().upper()
        start = start or (date.today() - timedelta(days=365))
        end = end or date.today()
        if start > end:
            raise ValueError("start must be on or before end")
        ak = self._akshare()
        all_rows = await self._cached_frame(
            "history",
            lambda: ak.futures_foreign_hist(symbol=normalized),
            timedelta(hours=6),
            symbol=normalized,
        )
        # A foreign-futures row carrying the current UTC date can still be an
        # in-progress session, especially while US contracts are trading.  The
        # public adapter does not expose an exchange close timestamp, so use a
        # deliberately conservative completed-day boundary.
        current_utc_date = datetime.now(UTC).date()
        rows = [
            row
            for row in all_rows
            if row.get("date")
            and start <= pd.Timestamp(row["date"]).date() <= end
            and pd.Timestamp(row["date"]).date() < current_utc_date
        ]
        if not rows:
            raise LookupError(f"No global-futures history was returned for {normalized}.")
        rows, repair_metadata = self._repair_ohlc_bounds(rows, normalized)
        repair_metadata.update(
            {
                "finalized_bars_only": True,
                "bar_finalization_policy": (
                    "source_day_strictly_before_current_utc_date"
                ),
                "bar_finalization_verified": False,
            }
        )
        return self._envelope(
            normalized,
            "history",
            rows,
            "Sina Finance global-futures daily history through AKShare.",
            metadata=repair_metadata,
        )

    async def history_interval(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        *,
        interval: BarInterval = "1d",
    ) -> DataEnvelope:
        if interval == "1d":
            envelope = await self.history(symbol, start, end)
            envelope.metadata.update({"interval": interval, "bars": len(envelope.rows)})
            return envelope
        if interval != "1wk":
            raise ValueError(
                "Global futures currently support daily and weekly K-lines in QuantSieve."
            )
        daily = await self.history(symbol, start, end)
        frame = pd.DataFrame(daily.rows)
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.set_index("date")
        rows = frame_records(
            frame.resample("1W")
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna(subset=["open", "high", "low", "close"])
        )
        current_utc_date = datetime.now(UTC).date()
        rows = [
            row
            for row in rows
            if pd.Timestamp(row["date"]).date() < current_utc_date
        ]
        if not rows:
            raise LookupError(
                f"No completed weekly global-futures bars were returned for {symbol}."
            )
        envelope = self._envelope(
            symbol,
            "history",
            rows,
            "Sina Finance global-futures weekly history aggregated from daily bars.",
            metadata=daily.metadata,
        )
        envelope.metadata.update(
            {
                "interval": interval,
                "bars": len(rows),
                "bar_finalization_policy": (
                    "weekly_period_end_strictly_before_current_utc_date"
                ),
            }
        )
        return envelope

    async def quote(self, symbol: str) -> DataEnvelope:
        normalized = symbol.strip().upper()
        ak = self._akshare()
        rows = await self._cached_frame(
            "quote",
            lambda: ak.futures_foreign_commodity_realtime(symbol=[normalized]),
            timedelta(seconds=30),
            symbol=normalized,
        )
        normalized_rows = [
            {
                "date": f"{row.get('日期')}T{row.get('行情时间')}",
                "open": row.get("开盘价"),
                "high": row.get("最高价"),
                "low": row.get("最低价"),
                "close": row.get("最新价"),
                "settlement": row.get("昨日结算价"),
                "price_change": row.get("涨跌额"),
                "price_change_percent": row.get("涨跌幅"),
                "bid": row.get("买价"),
                "ask": row.get("卖价"),
                "open_interest": row.get("持仓量"),
                "name": row.get("名称"),
            }
            for row in rows
        ]
        return self._envelope(
            normalized,
            "quote",
            normalized_rows,
            "Sina Finance global-futures realtime quote through AKShare.",
        )

    async def fundamentals(self, symbol: str) -> DataEnvelope:
        return self._unsupported(symbol, "fundamentals")

    async def capital_flow(self, symbol: str) -> DataEnvelope:
        return self._unsupported(symbol, "capital_flow")

    async def news(self, symbol: str, limit: int = 20) -> DataEnvelope:
        del limit
        return self._unsupported(symbol, "news")

    async def search(self, query: str, limit: int = 10) -> list[Instrument]:
        ak = self._akshare()
        rows = await self._cached_frame(
            "symbol_directory",
            ak.futures_hq_subscribe_exchange_symbol,
            timedelta(hours=24),
        )
        instruments: list[Instrument] = []
        for row in rows:
            symbol = str(row.get("code", "")).strip().upper()
            name = str(row.get("symbol", "")).strip()
            if not symbol or not name or symbol == "BTC":
                continue
            instruments.append(
                Instrument(
                    symbol=symbol,
                    name=name,
                    market="FUTURES",
                    exchange=self._exchange(symbol, name),
                    currency="USD",
                    provider="futures",
                    asset_type="commodity_future",
                )
            )
        return rank_instruments(instruments, query, limit=limit)

    @staticmethod
    def _exchange(symbol: str, name: str) -> str:
        if "COMEX" in name:
            return "COMEX"
        if "NYMEX" in name or symbol in {"CL", "NG"}:
            return "NYMEX"
        if "CBOT" in name:
            return "CBOT"
        if "LME" in name:
            return "LME"
        if symbol == "OIL":
            return "ICE"
        return "Global Futures"

    @staticmethod
    def _envelope(
        symbol: str,
        kind: str,
        rows: list[dict[str, Any]],
        note: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> DataEnvelope:
        envelope_metadata = dict(FOREIGN_FUTURES_RESEARCH_METADATA)
        if metadata:
            envelope_metadata.update(metadata)
        return DataEnvelope(
            symbol=symbol,
            kind=kind,
            rows=rows,
            citations=[
                Citation(source="AKShare / Sina Finance", url=AKSHARE_FUTURES_DOCS, note=note)
            ],
            metadata=envelope_metadata,
        )

    @staticmethod
    def _repair_ohlc_bounds(
        rows: list[dict[str, Any]],
        symbol: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Minimally restore the OHLC bound invariant without changing prices.

        Sina's foreign-futures history can occasionally return a high below the
        reported open or close (or a low above it).  The engine correctly rejects
        such a bar.  We retain the source open and close exactly, and only widen
        the inconsistent bound to include them; the count and policy travel with
        the result so this is never an invisible data correction.
        """

        repaired: list[dict[str, Any]] = []
        repaired_dates: list[str] = []
        for row in rows:
            try:
                open_price = float(row["open"])
                high_price = float(row["high"])
                low_price = float(row["low"])
                close_price = float(row["close"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Global futures {symbol} returned a non-numeric OHLC row."
                ) from exc
            if (
                not all(
                    isfinite(value)
                    for value in (open_price, high_price, low_price, close_price)
                )
                or min(open_price, high_price, low_price, close_price) <= 0
            ):
                raise RuntimeError(
                    f"Global futures {symbol} returned a non-positive or non-finite OHLC row."
                )

            corrected_high = max(high_price, open_price, close_price)
            corrected_low = min(low_price, open_price, close_price)
            if corrected_high == high_price and corrected_low == low_price:
                repaired.append(row)
                continue
            corrected = dict(row)
            corrected["high"] = corrected_high
            corrected["low"] = corrected_low
            repaired.append(corrected)
            repaired_dates.append(str(row["date"]))

        return repaired, {
            "ohlc_bound_repairs": len(repaired_dates),
            "ohlc_bound_repair_policy": (
                "When an upstream high/low did not enclose its reported open/close, "
                "QuantSieve widened only that bound to the nearest valid value; "
                "source open and close were not changed."
            ),
            "ohlc_bound_repair_first_date": repaired_dates[0]
            if repaired_dates
            else None,
            "ohlc_bound_repair_last_date": repaired_dates[-1]
            if repaired_dates
            else None,
        }

    @staticmethod
    def _unsupported(symbol: str, kind: str) -> DataEnvelope:
        return DataEnvelope(
            symbol=symbol.strip().upper(),
            kind=kind,
            rows=[],
            citations=[
                Citation(
                    source="AKShare / Sina Finance",
                    url=AKSHARE_FUTURES_DOCS,
                    note=f"Global commodity futures do not expose normalized {kind} data.",
                )
            ],
        )
