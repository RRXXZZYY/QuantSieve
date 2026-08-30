from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import httpx
import pandas as pd

from .base import DataProvider
from .cache import SQLiteCache
from .models import BarInterval, Citation, DataEnvelope, Instrument
from .symbols import rank_instruments
from .utils import finalized_ohlcv_frame, frame_records


class YFinanceProvider(DataProvider):
    name = "yfinance"

    def __init__(self, cache: SQLiteCache | None = None) -> None:
        self.cache = cache or SQLiteCache()

    @staticmethod
    def _ticker(symbol: str) -> Any:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError(
                "yfinance support is optional. Install quantsieve-providers[us]."
            ) from exc
        return yf.Ticker(symbol)

    async def _cached(
        self,
        operation: str,
        symbol: str,
        loader: Callable[[], Any],
        ttl: timedelta,
        **parameters: Any,
    ) -> list[dict[str, Any]]:
        key = self.cache.make_key(f"{self.name}:{operation}", symbol=symbol, **parameters)
        cached = self.cache.get(key)
        if cached is not None:
            return list(cached)
        try:
            value = await asyncio.to_thread(loader)
        except Exception as exc:
            raise RuntimeError(
                f"Yahoo Finance could not load {operation}; "
                "the upstream source may be rate limited."
            ) from exc
        rows = frame_records(value) if isinstance(value, pd.DataFrame) else list(value)
        self.cache.set(key, rows, ttl)
        return rows

    async def history(
        self, symbol: str, start: date | None = None, end: date | None = None
    ) -> DataEnvelope:
        start = start or (date.today() - timedelta(days=365))
        end = end or date.today()
        normalized_symbol = symbol.strip().upper()
        key = self.cache.make_key(
            f"{self.name}:history:v2",
            symbol=normalized_symbol,
            start=start,
            end=end,
        )
        cached = self.cache.get(key)
        if isinstance(cached, dict):
            rows = list(cached.get("rows", []))
            source = str(cached.get("source", "Yahoo Finance"))
            envelope = self._history_envelope(normalized_symbol, rows, source)
            envelope.metadata.update(
                {
                    "finalized_bars_only": True,
                    "bar_finalization_policy": "estimated_us_regular_session_close",
                    "bar_finalization_lag_seconds": 120,
                    "bar_finalization_verified": False,
                }
            )
            return envelope

        def load_history() -> tuple[pd.DataFrame, str]:
            try:
                frame = self._ticker(normalized_symbol).history(
                    start=start.isoformat(),
                    end=(end + timedelta(days=1)).isoformat(),
                    auto_adjust=True,
                )
                if not frame.empty:
                    return frame, "Yahoo Finance"
            except Exception:
                pass
            try:
                frame = self._direct_chart_history(normalized_symbol, start, end)
                if not frame.empty:
                    return frame, "Yahoo Finance"
            except Exception:
                pass
            try:
                frame = self._tencent_history(normalized_symbol, start, end)
                if not frame.empty:
                    return frame, "Tencent Finance"
            except Exception:
                pass
            frame = self._eastmoney_history(normalized_symbol, start, end)
            if frame.empty:
                raise RuntimeError("No US-market history rows were returned.")
            return frame, "Eastmoney"

        try:
            frame, source = await asyncio.to_thread(load_history)
        except Exception as exc:
            raise RuntimeError(
                "US-market history is unavailable from both Yahoo Finance "
                "and the public fallback sources."
            ) from exc
        rows = frame_records(
            finalized_ohlcv_frame(frame, interval="1d", market="us")
        )
        self.cache.set(
            key,
            {"rows": rows, "source": source},
            timedelta(minutes=1) if end >= datetime.now(UTC).date() else timedelta(hours=2),
        )
        envelope = self._history_envelope(normalized_symbol, rows, source)
        envelope.metadata.update(
            {
                "finalized_bars_only": True,
                "bar_finalization_policy": "estimated_us_regular_session_close",
                "bar_finalization_lag_seconds": 120,
                "bar_finalization_verified": False,
            }
        )
        return envelope

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

        start = start or (date.today() - timedelta(days=365))
        end = end or date.today()
        if start > end:
            raise ValueError("start must be on or before end")
        max_days = {"15m": 60, "1h": 60, "4h": 60, "1wk": 7_300}[interval]
        requested_days = (end - start).days + 1
        if requested_days > max_days:
            raise ValueError(
                f"Yahoo Finance {interval} K-lines support at most {max_days} days. "
                "Shorten the date range or use a larger K-line."
            )

        normalized_symbol = symbol.strip().upper()
        key = self.cache.make_key(
            f"{self.name}:history:interval:v2",
            symbol=normalized_symbol,
            start=start,
            end=end,
            interval=interval,
        )
        cached = self.cache.get(key)
        source = "Yahoo Finance"
        weekly_timestamp_is_period_end = False
        if isinstance(cached, dict):
            rows = list(cached.get("rows", []))
            source = str(cached.get("source", source))
            weekly_timestamp_is_period_end = bool(
                cached.get("weekly_timestamp_is_period_end", False)
            )
        elif isinstance(cached, list):
            rows = list(cached)
        else:
            upstream_interval = "60m" if interval in {"1h", "4h"} else interval
            try:
                frame = await asyncio.to_thread(
                    self._direct_chart_history,
                    normalized_symbol,
                    start,
                    end,
                    upstream_interval,
                )
            except Exception as exc:
                if interval != "1wk":
                    raise RuntimeError(
                        f"Yahoo Finance {interval} K-line history is unavailable."
                    ) from exc
                daily = await self.history(normalized_symbol, start, end)
                frame = daily.to_frame()
                source = daily.citations[0].source if daily.citations else source
                weekly_timestamp_is_period_end = True
                frame = self._resample_ohlcv(frame, "1W")
            if interval == "4h":
                frame = self._resample_us_session_4h(frame)
            rows = frame_records(frame)
            self.cache.set(
                key,
                {
                    "rows": rows,
                    "source": source,
                    "weekly_timestamp_is_period_end": weekly_timestamp_is_period_end,
                },
                timedelta(minutes=1) if end >= datetime.now(UTC).date() else timedelta(minutes=30),
            )

        frame = pd.DataFrame(rows)
        if "date" in frame.columns:
            frame["date"] = pd.to_datetime(frame["date"], utc=True)
            frame = frame.set_index("date")
        finalization_interval: BarInterval = "1d" if interval == "4h" else interval
        rows = frame_records(
            finalized_ohlcv_frame(
                frame,
                interval=finalization_interval,
                market="us",
                weekly_timestamp_is_period_end=weekly_timestamp_is_period_end,
            )
        )

        envelope = self._history_envelope(normalized_symbol, rows, source)
        envelope.metadata.update(
            {
                "interval": interval,
                "market_calendar": "exchange",
                "bars": len(rows),
                "finalized_bars_only": True,
                "bar_finalization_policy": (
                    "estimated_us_regular_session_close"
                    if interval == "4h"
                    else "completed_interval_with_publication_lag"
                ),
                "bar_finalization_lag_seconds": 120,
                "bar_finalization_verified": False,
                "interval_semantics": (
                    "us_regular_session_4h_plus_close_segment"
                    if interval == "4h"
                    else "clock_interval"
                ),
            }
        )
        return envelope

    async def quote(self, symbol: str) -> DataEnvelope:
        history = await self.history(symbol, date.today() - timedelta(days=14), date.today())
        return DataEnvelope(
            symbol=symbol,
            kind="quote",
            rows=history.rows[-1:] if history.rows else [],
            citations=history.citations,
        )

    async def fundamentals(self, symbol: str) -> DataEnvelope:
        rows = await self._cached(
            "fundamentals",
            symbol,
            lambda: self._ticker(symbol).quarterly_financials.T,
            timedelta(hours=24),
        )
        return self._envelope(symbol, "fundamentals", rows)

    async def capital_flow(self, symbol: str) -> DataEnvelope:
        return DataEnvelope(
            symbol=symbol,
            kind="capital_flow",
            rows=[],
            citations=[
                Citation(
                    source="Yahoo Finance",
                    url=f"https://finance.yahoo.com/quote/{symbol}",
                    note="This provider does not expose comparable A-share capital-flow data.",
                )
            ],
        )

    async def news(self, symbol: str, limit: int = 20) -> DataEnvelope:
        rows = await self._cached(
            "news",
            symbol,
            lambda: self._ticker(symbol).news or [],
            timedelta(minutes=30),
        )
        return self._envelope(symbol, "news", rows[:limit])

    async def search(self, query: str, limit: int = 10) -> list[Instrument]:
        def load_search() -> list[dict[str, Any]]:
            error: Exception | None = None
            for trust_environment in (True, False):
                try:
                    with httpx.Client(timeout=12, trust_env=trust_environment) as client:
                        response = client.get(
                            "https://query2.finance.yahoo.com/v1/finance/search",
                            params={
                                "q": query,
                                "quotesCount": min(max(limit * 2, 10), 30),
                                "newsCount": 0,
                            },
                            headers={"User-Agent": "Mozilla/5.0 QuantSieve/0.1"},
                        )
                        response.raise_for_status()
                        return list(response.json().get("quotes", []))
                except Exception as exc:
                    error = exc
            raise RuntimeError("Yahoo symbol search is unavailable or rate limited.") from error

        rows = await self._cached(
            "symbol_search",
            query,
            load_search,
            timedelta(hours=6),
            limit=limit,
        )
        us_exchanges = {
            "ASE",
            "BTS",
            "NCM",
            "NGM",
            "NMS",
            "NYQ",
            "PCX",
        }
        instruments: list[Instrument] = []
        for row in rows:
            symbol = str(row.get("symbol", "")).strip().upper()
            exchange_code = str(row.get("exchange", "")).upper()
            quote_type = str(row.get("quoteType", "")).upper()
            if (
                not symbol
                or quote_type not in {"EQUITY", "ETF"}
                or (exchange_code and exchange_code not in us_exchanges)
            ):
                continue
            instruments.append(
                Instrument(
                    symbol=symbol,
                    name=str(
                        row.get("longname")
                        or row.get("shortname")
                        or row.get("name")
                        or symbol
                    ).strip(),
                    market="ETF" if quote_type == "ETF" else "US",
                    exchange=str(row.get("exchDisp") or exchange_code or "US"),
                    currency="USD",
                    provider="yfinance",
                    asset_type=quote_type.casefold(),
                )
            )
        return rank_instruments(instruments, query, limit=limit)

    @staticmethod
    def _envelope(symbol: str, kind: str, rows: list[dict[str, Any]]) -> DataEnvelope:
        return DataEnvelope(
            symbol=symbol,
            kind=kind,
            rows=rows,
            citations=[
                Citation(
                    source="Yahoo Finance",
                    url=f"https://finance.yahoo.com/quote/{symbol}",
                )
            ],
        )

    @staticmethod
    def _history_envelope(
        symbol: str,
        rows: list[dict[str, Any]],
        source: str,
    ) -> DataEnvelope:
        if source == "Tencent Finance":
            citation = Citation(
                source="Tencent Finance",
                url=f"https://gu.qq.com/us{symbol.replace('-', '.')}/gp",
                note=(
                    "Adjusted daily K-line fallback used because Yahoo Finance "
                    "was unavailable or rate limited."
                ),
            )
        elif source == "Eastmoney":
            citation = Citation(
                source="Eastmoney",
                url=f"https://quote.eastmoney.com/us/{symbol}.html",
                note=(
                    "Adjusted daily K-line fallback used because Yahoo Finance "
                    "was unavailable or rate limited."
                ),
            )
        else:
            citation = Citation(
                source="Yahoo Finance",
                url=f"https://finance.yahoo.com/quote/{symbol}",
            )
        return DataEnvelope(
            symbol=symbol,
            kind="history",
            rows=rows,
            citations=[citation],
            metadata={"fallback": source != "Yahoo Finance"},
        )

    @staticmethod
    def _direct_chart_history(
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> pd.DataFrame:
        period1 = int(datetime.combine(start, datetime.min.time(), tzinfo=UTC).timestamp())
        period2 = int(
            datetime.combine(
                end + timedelta(days=1), datetime.min.time(), tzinfo=UTC
            ).timestamp()
        )
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        error: Exception | None = None
        payload: dict[str, Any] | None = None
        for trust_environment in (True, False):
            try:
                with httpx.Client(timeout=20, trust_env=trust_environment) as client:
                    response = client.get(
                        url,
                        params={
                            "period1": period1,
                            "period2": period2,
                            "interval": interval,
                            "events": "div,splits",
                        },
                        headers={"User-Agent": "Mozilla/5.0 QuantSieve/0.1"},
                    )
                    response.raise_for_status()
                    payload = dict(response.json())
                    break
            except Exception as exc:
                error = exc
        if payload is None:
            raise RuntimeError("Yahoo chart endpoint is unavailable.") from error
        result = payload["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        frame = pd.DataFrame(quote)
        frame.index = pd.to_datetime(result["timestamp"], unit="s", utc=True)
        adjusted = result["indicators"].get("adjclose", [{}])[0].get("adjclose")
        if adjusted:
            factor = pd.Series(adjusted, index=frame.index) / frame["close"]
            for column in ("open", "high", "low", "close"):
                frame[column] = frame[column] * factor
        return frame.dropna(subset=["open", "high", "low", "close"])

    @staticmethod
    def _resample_ohlcv(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
        return (
            frame.resample(rule)
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

    @staticmethod
    def _resample_us_session_4h(frame: pd.DataFrame) -> pd.DataFrame:
        """Align 4h research bars to the US regular session, not UTC midnight.

        A normal US equity session has one 09:30–13:30 four-hour segment and
        a 13:30–16:00 closing segment.  The latter is shorter and must remain
        explicit rather than being mixed into arbitrary calendar-clock bins.
        """
        if frame.empty:
            return frame.copy()
        local = frame.copy()
        index = pd.DatetimeIndex(local.index)
        index = (
            index.tz_localize(UTC).tz_convert("America/New_York")
            if index.tz is None
            else index.tz_convert("America/New_York")
        )
        local.index = index
        regular = local.between_time("09:30", "15:59:59.999999")
        if regular.empty:
            return regular
        closing_segment = time(13, 30)
        segment_starts = pd.DatetimeIndex(
            [
                timestamp.normalize()
                + (
                    pd.Timedelta(hours=13, minutes=30)
                    if timestamp.time() >= closing_segment
                    else pd.Timedelta(hours=9, minutes=30)
                )
                for timestamp in regular.index
            ]
        )
        return regular.groupby(segment_starts, sort=True).agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        ).dropna(subset=["open", "high", "low", "close"])

    @staticmethod
    def _tencent_history(symbol: str, start: date, end: date) -> pd.DataFrame:
        endpoint = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        normalized_symbol = symbol.replace("-", ".")
        lookup_key = f"us{normalized_symbol}"
        headers = {"User-Agent": "Mozilla/5.0 QuantSieve/0.1"}
        with httpx.Client(timeout=20, trust_env=False) as client:
            lookup_response = client.get(
                endpoint,
                params={"param": f"{lookup_key},day,,,2,qfq"},
                headers=headers,
            )
            lookup_response.raise_for_status()
            lookup_data = (lookup_response.json().get("data") or {}).get(lookup_key) or {}
            quote = lookup_data.get("qt", {}).get(lookup_key) or []
            exchange_symbol = str(quote[2]).strip() if len(quote) > 2 else ""
            if not exchange_symbol:
                raise RuntimeError("Tencent Finance could not resolve the US exchange.")

            data_key = f"us{exchange_symbol}"
            requested_rows = min(max((end - start).days + 10, 30), 3_000)
            response = client.get(
                endpoint,
                params={
                    "param": (
                        f"{data_key},day,{start.isoformat()},{end.isoformat()},"
                        f"{requested_rows},qfq"
                    )
                },
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        data = (payload.get("data") or {}).get(data_key) or {}
        raw_rows = data.get("day") or data.get("qfqday") or []
        rows = []
        for raw_row in raw_rows:
            if len(raw_row) < 6:
                continue
            rows.append(
                {
                    "date": raw_row[0],
                    "open": float(raw_row[1]),
                    "close": float(raw_row[2]),
                    "high": float(raw_row[3]),
                    "low": float(raw_row[4]),
                    "volume": float(raw_row[5]),
                }
            )
        frame = pd.DataFrame(rows)
        if frame.empty:
            raise RuntimeError("Tencent Finance returned no US-market K-line rows.")
        frame["date"] = pd.to_datetime(frame["date"], utc=True)
        return frame.set_index("date")

    @staticmethod
    def _eastmoney_history(symbol: str, start: date, end: date) -> pd.DataFrame:
        normalized_symbol = symbol.replace("-", ".")
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "1",
            "beg": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
        }
        error: Exception | None = None
        for market_id in ("105", "106", "107"):
            try:
                with httpx.Client(timeout=20, trust_env=False) as client:
                    response = client.get(
                        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                        params={"secid": f"{market_id}.{normalized_symbol}", **params},
                        headers={"User-Agent": "Mozilla/5.0 QuantSieve/0.1"},
                    )
                    response.raise_for_status()
                    payload = response.json()
                data = payload.get("data") or {}
                raw_rows = data.get("klines") or []
                if not raw_rows:
                    continue
                rows = []
                for raw_row in raw_rows:
                    values = str(raw_row).split(",")
                    if len(values) < 7:
                        continue
                    rows.append(
                        {
                            "date": values[0],
                            "open": float(values[1]),
                            "close": float(values[2]),
                            "high": float(values[3]),
                            "low": float(values[4]),
                            "volume": float(values[5]),
                        }
                    )
                frame = pd.DataFrame(rows)
                if not frame.empty:
                    frame["date"] = pd.to_datetime(frame["date"], utc=True)
                    return frame.set_index("date")
            except Exception as exc:
                error = exc
        raise RuntimeError("Eastmoney US-market K-line endpoint is unavailable.") from error
