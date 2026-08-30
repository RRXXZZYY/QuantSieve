from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from .base import DataProvider
from .cache import SQLiteCache
from .models import BarInterval, Citation, DataEnvelope, Instrument
from .symbols import rank_instruments
from .utils import finalized_ohlcv_frame, frame_records


class AKShareProvider(DataProvider):
    name = "akshare"

    def __init__(self, cache: SQLiteCache | None = None) -> None:
        self.cache = cache or SQLiteCache()

    @staticmethod
    def _akshare() -> Any:
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError(
                "AKShare support is optional. Install quantsieve-providers[china]."
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
        frame: pd.DataFrame | None = None
        for attempt in range(2):
            try:
                frame = await asyncio.to_thread(loader)
                break
            except Exception as exc:
                if attempt == 1:
                    raise RuntimeError(
                        f"AKShare could not load {operation}; "
                        "the upstream source may be unavailable."
                    ) from exc
                # Eastmoney's public minute endpoint intermittently closes a
                # request. Retry once, but never substitute lower-frequency
                # data for an intraday interval.
                await asyncio.sleep(0.25)
        assert frame is not None
        rows = frame_records(frame)
        self.cache.set(key, rows, ttl)
        return rows

    async def history(
        self, symbol: str, start: date | None = None, end: date | None = None
    ) -> DataEnvelope:
        start = start or (date.today() - timedelta(days=365))
        end = end or date.today()
        ak = self._akshare()
        def load_history() -> pd.DataFrame:
            try:
                return ak.stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust="qfq",
                ).rename(
                    columns={
                        "日期": "date",
                        "开盘": "open",
                        "收盘": "close",
                        "最高": "high",
                        "最低": "low",
                        "成交量": "volume",
                        "成交额": "turnover",
                    }
                )
            except Exception:
                exchange = "sh" if symbol.startswith(("5", "6", "9")) else "sz"
                return (
                    ak.stock_zh_a_daily(
                        symbol=f"{exchange}{symbol}",
                        start_date=start.strftime("%Y%m%d"),
                        end_date=end.strftime("%Y%m%d"),
                        adjust="qfq",
                    )
                    .reset_index()
                    .rename(columns={"date": "date"})
                )

        rows = await self._cached_frame(
            "history",
            load_history,
            timedelta(minutes=1) if end >= datetime.now(UTC).date() else timedelta(hours=6),
            symbol=symbol,
            start=start,
            end=end,
        )
        frame = pd.DataFrame(rows)
        if "date" in frame.columns:
            frame["date"] = pd.to_datetime(frame["date"], utc=True)
            frame = frame.set_index("date")
        rows = frame_records(finalized_ohlcv_frame(frame, interval="1d", market="cn"))
        envelope = self._envelope(symbol, "history", rows, "东方财富历史行情（AKShare 适配）")
        envelope.metadata.update(
            {
                "finalized_bars_only": True,
                "bar_finalization_policy": "estimated_cn_regular_session_close",
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

        start = start or (date.today() - timedelta(days=120))
        end = end or date.today()
        if start > end:
            raise ValueError("start must be on or before end")
        if interval == "1wk":
            daily = await self.history(symbol, start, end)
            frame = pd.DataFrame(daily.rows)
            frame["date"] = pd.to_datetime(frame["date"])
            frame = frame.set_index("date")
            rows = frame_records(self._resample_ohlcv(frame, "1W"))
            frame = pd.DataFrame(rows)
            frame["date"] = pd.to_datetime(frame["date"], utc=True)
            frame = frame.set_index("date")
            rows = frame_records(
                finalized_ohlcv_frame(
                    frame,
                    interval="1wk",
                    market="cn",
                    weekly_timestamp_is_period_end=True,
                )
            )
            envelope = self._envelope(
                symbol,
                "history",
                rows,
                "东方财富周线历史行情（由日线聚合）",
            )
            envelope.metadata.update(
                {
                    "interval": interval,
                    "bars": len(rows),
                    "finalized_bars_only": True,
                    "bar_finalization_policy": "completed_interval_with_publication_lag",
                    "bar_finalization_lag_seconds": 120,
                    "bar_finalization_verified": False,
                }
            )
            return envelope

        max_days = 120
        requested_days = (end - start).days + 1
        if requested_days > max_days:
            raise ValueError(
                f"A-share {interval} K-lines support at most {max_days} days. "
                "Shorten the date range or use a larger K-line."
            )
        ak = self._akshare()
        upstream_period = "15" if interval == "15m" else "60"

        def load_intraday() -> pd.DataFrame:
            return ak.stock_zh_a_hist_min_em(
                symbol=symbol,
                period=upstream_period,
                start_date=f"{start.isoformat()} 09:30:00",
                end_date=f"{end.isoformat()} 15:00:00",
                adjust="qfq",
            ).rename(
                columns={
                    "时间": "date",
                    "日期": "date",
                    "开盘": "open",
                    "收盘": "close",
                    "最高": "high",
                    "最低": "low",
                    "成交量": "volume",
                    "成交额": "turnover",
                }
            )

        daily_fallback: DataEnvelope | None = None
        try:
            rows = await self._cached_frame(
                "history_interval",
                load_intraday,
                timedelta(minutes=1) if end >= datetime.now(UTC).date() else timedelta(minutes=30),
                symbol=symbol,
                start=start,
                end=end,
                interval=interval,
            )
            frame = pd.DataFrame(rows)
            if "date" in frame.columns:
                frame["date"] = pd.to_datetime(frame["date"])
                frame = frame.set_index("date")
        except RuntimeError:
            if interval != "4h":
                raise
            # A mainland session contains exactly four trading hours.  When
            # AKShare's minute endpoint is temporarily unavailable, a daily
            # OHLCV row remains an honest complete-session substitute.  The
            # metadata and citation below make this source downgrade visible.
            daily_fallback = await self.history(symbol, start, end)
            frame = daily_fallback.to_frame()
        if interval == "4h":
            rows = frame_records(self._resample_cn_session_4h(frame))
            frame = pd.DataFrame(rows)
            frame["date"] = pd.to_datetime(frame["date"], utc=True)
            frame = frame.set_index("date")
        finalization_interval: BarInterval = "1d" if interval == "4h" else interval
        rows = frame_records(
            finalized_ohlcv_frame(frame, interval=finalization_interval, market="cn")
        )
        if not rows:
            raise LookupError(f"No A-share {interval} K-lines were returned for {symbol}.")
        envelope = (
            daily_fallback.model_copy(update={"rows": rows})
            if daily_fallback is not None
            else self._envelope(
                symbol,
                "history",
                rows,
                "东方财富分钟历史行情（AKShare 适配）",
            )
        )
        envelope.metadata.update(
            {
                "interval": interval,
                "market_calendar": "China exchange",
                "bars": len(rows),
                "finalized_bars_only": True,
                "bar_finalization_policy": (
                    "estimated_cn_regular_session_close"
                    if interval == "4h"
                    else "completed_interval_with_publication_lag"
                ),
                "bar_finalization_lag_seconds": 120,
                "bar_finalization_verified": False,
                "interval_semantics": (
                    "four_trading_hours_per_session" if interval == "4h" else "clock_interval"
                ),
                "fallback": daily_fallback is not None,
                "fallback_reason": (
                    "AKShare minute history was unavailable; aggregated completed daily OHLCV "
                    "as one four-trading-hour mainland session."
                    if daily_fallback is not None
                    else None
                ),
            }
        )
        return envelope

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
    def _resample_cn_session_4h(frame: pd.DataFrame) -> pd.DataFrame:
        """Aggregate one mainland session's four trading hours across lunch.

        Mainland equities trade for two hours before and two hours after the
        lunch break. Calendar-clock ``resample('4h')`` splits that into two
        misleading two-hour fragments; the research interval instead means
        one complete four-trading-hour session.
        """
        if frame.empty:
            return frame.copy()
        local = frame.copy()
        index = pd.DatetimeIndex(local.index)
        index = (
            index.tz_localize("Asia/Shanghai")
            if index.tz is None
            else index.tz_convert("Asia/Shanghai")
        )
        local.index = index
        session = local.index.normalize()
        aggregated = local.groupby(session, sort=True).agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        ).dropna(subset=["open", "high", "low", "close"])
        aggregated.index = aggregated.index + pd.Timedelta(hours=9, minutes=30)
        return aggregated

    async def quote(self, symbol: str) -> DataEnvelope:
        history = await self.history(symbol, date.today() - timedelta(days=14), date.today())
        return DataEnvelope(
            symbol=symbol,
            kind="quote",
            rows=history.rows[-1:] if history.rows else [],
            citations=history.citations,
        )

    async def fundamentals(self, symbol: str) -> DataEnvelope:
        ak = self._akshare()
        rows = await self._cached_frame(
            "fundamentals",
            lambda: ak.stock_financial_abstract_ths(symbol=symbol, indicator="按报告期"),
            timedelta(hours=24),
            symbol=symbol,
        )
        return self._envelope(symbol, "fundamentals", rows, "同花顺财务摘要（AKShare 适配）")

    async def capital_flow(self, symbol: str) -> DataEnvelope:
        ak = self._akshare()
        market = "sh" if symbol.startswith(("5", "6", "9")) else "sz"
        rows = await self._cached_frame(
            "capital_flow",
            lambda: ak.stock_individual_fund_flow(stock=symbol, market=market),
            timedelta(hours=2),
            symbol=symbol,
            market=market,
        )
        return self._envelope(symbol, "capital_flow", rows, "东方财富资金流（AKShare 适配）")

    async def news(self, symbol: str, limit: int = 20) -> DataEnvelope:
        ak = self._akshare()
        rows = await self._cached_frame(
            "news",
            lambda: ak.stock_news_em(symbol=symbol),
            timedelta(minutes=30),
            symbol=symbol,
        )
        return self._envelope(symbol, "news", rows[:limit], "东方财富个股新闻（AKShare 适配）")

    async def search(self, query: str, limit: int = 10) -> list[Instrument]:
        ak = self._akshare()
        rows = await self._cached_frame(
            "symbol_directory",
            ak.stock_info_a_code_name,
            timedelta(hours=24),
        )
        instruments: list[Instrument] = []
        for row in rows:
            raw_code = row.get("code", row.get("代码", ""))
            raw_name = row.get("name", row.get("名称", ""))
            code = str(raw_code).split(".", maxsplit=1)[0].zfill(6)
            name = str(raw_name).strip()
            if len(code) != 6 or not code.isdigit() or not name:
                continue
            instruments.append(
                Instrument(
                    symbol=code,
                    name=name,
                    market="CN",
                    exchange=(
                        "上海证券交易所"
                        if code.startswith(("5", "6", "9"))
                        else "深圳证券交易所"
                    ),
                    currency="CNY",
                    provider="akshare",
                )
            )
        return rank_instruments(instruments, query, limit=limit)

    @staticmethod
    def _envelope(symbol: str, kind: str, rows: list[dict[str, Any]], note: str) -> DataEnvelope:
        return DataEnvelope(
            symbol=symbol,
            kind=kind,
            rows=rows,
            citations=[Citation(source="AKShare", url="https://akshare.akfamily.xyz/", note=note)],
        )
