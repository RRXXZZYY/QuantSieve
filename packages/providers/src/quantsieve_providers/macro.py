from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from typing import Any

import httpx
import pandas as pd

from .base import DataProvider
from .cache import SQLiteCache
from .models import BarInterval, Citation, DataEnvelope
from .utils import frame_records

INDEX_SERIES: dict[str, tuple[str, str]] = {
    "IXIC": ("COMP", "NASDAQ Composite"),
    "NDX": ("NDX", "NASDAQ 100"),
    "VIX": ("VIXCLS", "CBOE Volatility Index"),
}
INDEX_ALIASES = {
    "^IXIC": "IXIC",
    "^NDX": "NDX",
    "^VIX": "VIX",
}
FX_CURRENCIES = {
    "AUD",
    "CAD",
    "CHF",
    "CNY",
    "EUR",
    "GBP",
    "JPY",
    "NZD",
    "USD",
}


class MacroProvider(DataProvider):
    """Official daily reference series for indices and foreign exchange."""

    name = "macro"

    def __init__(self, cache: SQLiteCache | None = None) -> None:
        self.cache = cache or SQLiteCache()

    @classmethod
    def normalize_symbol(cls, symbol: str) -> str:
        normalized = (
            symbol.strip()
            .upper()
            .replace("/", "")
            .replace("-", "")
            .removesuffix("=X")
        )
        return INDEX_ALIASES.get(normalized, normalized)

    @classmethod
    def supports(cls, symbol: str) -> bool:
        normalized = cls.normalize_symbol(symbol)
        if normalized in INDEX_SERIES:
            return True
        return (
            len(normalized) == 6
            and normalized[:3] in FX_CURRENCIES
            and normalized[3:] in FX_CURRENCIES
            and normalized[:3] != normalized[3:]
        )

    async def history(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
    ) -> DataEnvelope:
        start = start or (date.today() - timedelta(days=365))
        end = end or date.today()
        if start > end:
            raise ValueError("start must be on or before end")
        normalized = self.normalize_symbol(symbol)
        if not self.supports(normalized):
            raise LookupError(f"Unsupported reference-market symbol: {symbol}")

        key = self.cache.make_key(
            f"{self.name}:history:v1",
            symbol=normalized,
            start=start,
            end=end,
        )
        cached = self.cache.get(key)
        if isinstance(cached, dict):
            return self._envelope(
                normalized,
                list(cached.get("rows", [])),
                str(cached.get("source_url", "")),
                str(cached.get("source", "")),
                close_only=bool(cached.get("close_only", True)),
            )

        if normalized in INDEX_SERIES:
            series, _ = INDEX_SERIES[normalized]
            if normalized == "VIX":
                frame, source_url = await asyncio.to_thread(
                    self._cboe_vix_history,
                    start,
                    end,
                )
                source = "Cboe Global Indices"
            else:
                frame, source_url = await asyncio.to_thread(
                    self._nasdaq_history,
                    series,
                    start,
                    end,
                )
                source = "Nasdaq"
            close_only = False
        else:
            frame, source_url = await asyncio.to_thread(
                self._ecb_pair_history,
                normalized[:3],
                normalized[3:],
                start,
                end,
            )
            source = "European Central Bank"
            close_only = True
        rows = frame_records(frame)
        if len(rows) < 2:
            raise RuntimeError(f"Reference source returned too few rows for {normalized}.")
        self.cache.set(
            key,
            {
                "rows": rows,
                "source": source,
                "source_url": source_url,
                "close_only": close_only,
            },
            timedelta(hours=6),
        )
        return self._envelope(
            normalized,
            rows,
            source_url,
            source,
            close_only=close_only,
        )

    async def history_interval(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        *,
        interval: BarInterval = "1d",
    ) -> DataEnvelope:
        if interval not in {"1d", "1wk"}:
            raise ValueError(
                "Official index and FX reference series support daily or weekly bars only."
            )
        envelope = await self.history(symbol, start, end)
        if interval == "1wk":
            envelope.rows = self._weekly_rows(envelope.to_frame())
            current_utc_date = datetime.now(UTC).date()
            envelope.rows = [
                row
                for row in envelope.rows
                if pd.Timestamp(row["date"]).date() < current_utc_date
            ]
            if not envelope.rows:
                raise LookupError(
                    f"No completed weekly macro bars were returned for {symbol}."
                )
            envelope.metadata["bar_finalization_policy"] = (
                "weekly_period_end_strictly_before_current_utc_date"
            )
        envelope.metadata.update({"interval": interval, "bars": len(envelope.rows)})
        return envelope

    async def quote(self, symbol: str) -> DataEnvelope:
        history = await self.history(
            symbol,
            date.today() - timedelta(days=30),
            date.today(),
        )
        return DataEnvelope(
            symbol=history.symbol,
            kind="quote",
            rows=history.rows[-1:],
            citations=history.citations,
            metadata=history.metadata,
        )

    async def fundamentals(self, symbol: str) -> DataEnvelope:
        return self._unavailable(symbol, "fundamentals")

    async def capital_flow(self, symbol: str) -> DataEnvelope:
        return self._unavailable(symbol, "capital_flow")

    async def news(self, symbol: str, limit: int = 20) -> DataEnvelope:
        return self._unavailable(symbol, "news")

    @staticmethod
    def _request_csv(
        url: str,
        *,
        params: dict[str, str],
        source: str,
    ) -> str:
        error: Exception | None = None
        for trust_environment in (False, False, True):
            try:
                with httpx.Client(
                    timeout=25,
                    trust_env=trust_environment,
                    follow_redirects=True,
                    headers={
                        "Accept": "text/csv",
                        "User-Agent": "QuantSieve/0.1 public-market-research",
                    },
                ) as client:
                    response = client.get(url, params=params)
                    response.raise_for_status()
                    if "," not in response.text.splitlines()[0]:
                        raise RuntimeError(f"{source} returned a non-CSV response.")
                    return response.text
            except Exception as exc:
                error = exc
        raise RuntimeError(f"{source} daily reference data is unavailable.") from error

    @classmethod
    def _request_json(
        cls,
        url: str,
        *,
        params: dict[str, str],
        source: str,
    ) -> dict[str, Any]:
        error: Exception | None = None
        for trust_environment in (False, False, True):
            try:
                with httpx.Client(
                    timeout=30,
                    trust_env=trust_environment,
                    follow_redirects=True,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://www.nasdaq.com/",
                        "User-Agent": (
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "Chrome/124 Safari/537.36"
                        ),
                    },
                ) as client:
                    response = client.get(url, params=params)
                    response.raise_for_status()
                    return dict(response.json())
            except Exception as exc:
                error = exc
        raise RuntimeError(f"{source} daily index data is unavailable.") from error

    @classmethod
    def _nasdaq_history(
        cls,
        series: str,
        start: date,
        end: date,
    ) -> tuple[pd.DataFrame, str]:
        source_url = (
            f"https://www.nasdaq.com/market-activity/index/{series.lower()}/historical"
        )
        payload = cls._request_json(
            f"https://api.nasdaq.com/api/quote/{series}/historical",
            params={
                "assetclass": "index",
                "fromdate": start.isoformat(),
                "todate": end.isoformat(),
                "limit": "5000",
            },
            source="Nasdaq",
        )
        data = payload.get("data") or {}
        rows = (data.get("tradesTable") or {}).get("rows") or []
        frame = cls._ohlc_frame(rows)
        if frame.empty:
            status = payload.get("status") or {}
            messages = status.get("bCodeMessage") or []
            detail = messages[0].get("errorMessage") if messages else "no observations"
            raise RuntimeError(f"Nasdaq returned {detail} for {series}.")
        return frame.loc[
            (frame.index.date >= start) & (frame.index.date <= end)
        ], source_url

    @classmethod
    def _cboe_vix_history(
        cls,
        start: date,
        end: date,
    ) -> tuple[pd.DataFrame, str]:
        source_url = (
            "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
        )
        csv_text = cls._request_csv(
            source_url,
            params={},
            source="Cboe",
        )
        raw = pd.read_csv(StringIO(csv_text))
        frame = cls._ohlc_frame(raw.to_dict(orient="records"))
        if frame.empty:
            raise RuntimeError("Cboe returned no VIX observations.")
        return frame.loc[
            (frame.index.date >= start) & (frame.index.date <= end)
        ], source_url

    @staticmethod
    def _ohlc_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
        normalized: list[dict[str, Any]] = []
        for row in rows:
            values: dict[str, Any] = {}
            for key, value in row.items():
                normalized_key = str(key).strip().casefold()
                if normalized_key == "date":
                    values["date"] = value
                elif normalized_key in {"open", "high", "low", "close", "volume"}:
                    cleaned = str(value).replace(",", "").replace("$", "").strip()
                    values[normalized_key] = 0 if cleaned in {"", "--", "nan"} else cleaned
            normalized.append(values)
        frame = pd.DataFrame(normalized)
        required = {"date", "open", "high", "low", "close"}
        if frame.empty or not required.issubset(frame.columns):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        frame["date"] = pd.to_datetime(frame["date"], utc=True, errors="coerce")
        for column in ("open", "high", "low", "close", "volume"):
            if column not in frame:
                frame[column] = 0.0
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return (
            frame.dropna(subset=["date", "open", "high", "low", "close"])
            .drop_duplicates(subset=["date"])
            .sort_values("date")
            .set_index("date")[["open", "high", "low", "close", "volume"]]
        )

    @classmethod
    def _ecb_pair_history(
        cls,
        base: str,
        quote: str,
        start: date,
        end: date,
    ) -> tuple[pd.DataFrame, str]:
        currencies = sorted({currency for currency in (base, quote) if currency != "EUR"})
        key = "+".join(currencies)
        source_url = (
            "https://data-api.ecb.europa.eu/service/data/EXR/"
            f"D.{key}.EUR.SP00.A"
        )
        csv_text = cls._request_csv(
            source_url,
            params={
                "startPeriod": start.isoformat(),
                "endPeriod": end.isoformat(),
                "format": "csvdata",
                "detail": "dataonly",
            },
            source="ECB",
        )
        raw = pd.read_csv(StringIO(csv_text))
        required = {"CURRENCY", "TIME_PERIOD", "OBS_VALUE"}
        if not required.issubset(raw.columns):
            raise RuntimeError("ECB returned an unexpected CSV schema.")
        pivot = raw.pivot(
            index="TIME_PERIOD",
            columns="CURRENCY",
            values="OBS_VALUE",
        )
        base_rate: Any = 1.0 if base == "EUR" else pivot[base]
        quote_rate: Any = 1.0 if quote == "EUR" else pivot[quote]
        close = quote_rate / base_rate
        frame = cls._close_frame(close.index, close)
        if frame.empty:
            raise RuntimeError(f"ECB returned no aligned observations for {base}{quote}.")
        return frame, source_url

    @staticmethod
    def _close_frame(dates: Any, values: Any) -> pd.DataFrame:
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime(list(dates), utc=True, errors="coerce"),
                "close": pd.to_numeric(list(values), errors="coerce"),
            }
        ).dropna(subset=["date", "close"])
        frame = frame.drop_duplicates(subset=["date"]).sort_values("date").set_index("date")
        for column in ("open", "high", "low"):
            frame[column] = frame["close"]
        frame["volume"] = 0.0
        return frame[["open", "high", "low", "close", "volume"]]

    @staticmethod
    def _weekly_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        periods = frame.index.tz_localize(None).to_period("W-FRI")
        for _, group in frame.groupby(periods):
            rows.append(
                {
                    "date": group.index[-1].isoformat(),
                    "open": float(group["open"].iloc[0]),
                    "high": float(group["high"].max()),
                    "low": float(group["low"].min()),
                    "close": float(group["close"].iloc[-1]),
                    "volume": 0.0,
                }
            )
        return rows

    @staticmethod
    def _envelope(
        symbol: str,
        rows: list[dict[str, Any]],
        source_url: str,
        source: str,
        *,
        close_only: bool,
    ) -> DataEnvelope:
        note = (
            "Daily reference observations. The source does not publish tradable "
            "OHLC quotes, so OHLC fields use the same observation value."
            if close_only
            else (
                "Official daily index OHLC observations. The index itself is not "
                "directly tradable; validate execution with a chosen ETF or derivative."
            )
        )
        current_utc_date = datetime.now(UTC).date()
        finalized_rows = [
            row
            for row in rows
            if pd.Timestamp(row["date"]).date() < current_utc_date
        ]
        if not finalized_rows:
            raise LookupError(
                f"No completed daily macro bars were returned for {symbol}."
            )
        return DataEnvelope(
            symbol=symbol,
            kind="history",
            rows=finalized_rows,
            citations=[
                Citation(
                    source=source,
                    url=source_url,
                    note=note,
                )
            ],
            metadata={
                "fallback": False,
                "reference_series": True,
                "tradable_quote": False,
                "price_basis": (
                    "daily_reference_rate"
                    if close_only
                    else "official_daily_index_ohlc"
                ),
                "ohlc_derived_from_close": close_only,
                "finalized_bars_only": True,
                "bar_finalization_policy": (
                    "source_day_strictly_before_current_utc_date"
                ),
                "bar_finalization_verified": False,
                "execution_note": (
                    "Backtests execute at the next published reference observation, "
                    "not at a broker-dealable quote."
                ),
            },
        )

    def _unavailable(self, symbol: str, kind: str) -> DataEnvelope:
        normalized = self.normalize_symbol(symbol)
        if normalized in INDEX_SERIES:
            series, _ = INDEX_SERIES[normalized]
            if normalized == "VIX":
                source = "Cboe Global Indices"
                url = (
                    "https://www.cboe.com/tradable_products/vix/"
                    "vix_historical_data/"
                )
            else:
                source = "Nasdaq"
                url = (
                    "https://www.nasdaq.com/market-activity/index/"
                    f"{series.lower()}/historical"
                )
        else:
            source = "European Central Bank"
            url = "https://data.ecb.europa.eu/"
        return DataEnvelope(
            symbol=normalized,
            kind=kind,
            rows=[],
            citations=[
                Citation(
                    source=source,
                    url=url,
                    note=f"{kind} is not published for this reference series.",
                )
            ],
        )
