from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from quantsieve_providers import FuturesProvider, SQLiteCache


class FakeAKShare:
    @staticmethod
    def futures_foreign_hist(symbol: str) -> pd.DataFrame:
        assert symbol == "CL"
        return pd.DataFrame(
            [
                {
                    "date": "2025-01-01",
                    "open": 70.0,
                    "high": 72.0,
                    "low": 69.0,
                    "close": 71.0,
                    "volume": 100,
                },
                {
                    "date": "2025-01-02",
                    "open": 71.0,
                    "high": 73.0,
                    "low": 70.0,
                    "close": 72.0,
                    "volume": 120,
                },
            ]
        )

    @staticmethod
    def futures_foreign_commodity_realtime(symbol: list[str]) -> pd.DataFrame:
        assert symbol == ["CL"]
        return pd.DataFrame(
            [
                {
                    "名称": "NYMEX原油",
                    "最新价": 72.5,
                    "涨跌额": 1.5,
                    "涨跌幅": 2.1,
                    "开盘价": 71.0,
                    "最高价": 73.0,
                    "最低价": 70.0,
                    "昨日结算价": 71.0,
                    "持仓量": 1000,
                    "买价": 72.4,
                    "卖价": 72.5,
                    "行情时间": "12:00:00",
                    "日期": "2025-01-02",
                }
            ]
        )

    @staticmethod
    def futures_hq_subscribe_exchange_symbol() -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"symbol": "NYMEX原油", "code": "CL"},
                {"symbol": "COMEX黄金", "code": "GC"},
            ]
        )


@pytest.mark.asyncio
async def test_futures_search_history_and_quote(monkeypatch, tmp_path) -> None:
    provider = FuturesProvider(SQLiteCache(tmp_path / "cache.db"))
    monkeypatch.setattr(provider, "_akshare", lambda: FakeAKShare)

    matches = await provider.search("原油")
    history = await provider.history("cl", date(2025, 1, 2), date(2025, 1, 2))
    quote = await provider.quote("CL")

    assert matches[0].symbol == "CL"
    assert matches[0].market == "FUTURES"
    assert history.rows == [
        {
            "date": "2025-01-02",
            "open": 71.0,
            "high": 73.0,
            "low": 70.0,
            "close": 72.0,
            "volume": 120,
        }
    ]
    assert quote.rows[0]["close"] == 72.5
    assert quote.citations[0].source == "AKShare / Sina Finance"
    assert history.metadata == {
        "instrument_scope": "foreign_futures_reference_series",
        "instrument_scope_note": (
            "Public foreign-futures price series for research only. The upstream code "
            "does not identify a selected expiry; QuantSieve does not model margin, "
            "contract rolls, expiry, currency conversion, or broker execution."
        ),
        "contract_month_identified": False,
        "contract_roll_modeled": False,
        "execution_ready": False,
        "finalized_bars_only": True,
        "bar_finalization_policy": "source_day_strictly_before_current_utc_date",
        "bar_finalization_verified": False,
        "ohlc_bound_repairs": 0,
        "ohlc_bound_repair_policy": (
            "When an upstream high/low did not enclose its reported open/close, "
            "QuantSieve widened only that bound to the nearest valid value; "
            "source open and close were not changed."
        ),
        "ohlc_bound_repair_first_date": None,
        "ohlc_bound_repair_last_date": None,
    }


@pytest.mark.asyncio
async def test_futures_history_repairs_only_inconsistent_high_low_bounds(
    monkeypatch, tmp_path
) -> None:
    class InconsistentOHLC(FakeAKShare):
        @staticmethod
        def futures_foreign_hist(symbol: str) -> pd.DataFrame:
            assert symbol == "CL"
            return pd.DataFrame(
                [
                    {
                        "date": "2025-01-02",
                        "open": 71.0,
                        "high": 70.5,
                        "low": 72.0,
                        "close": 72.5,
                        "volume": 120,
                    }
                ]
            )

    provider = FuturesProvider(SQLiteCache(tmp_path / "cache.db"))
    monkeypatch.setattr(provider, "_akshare", lambda: InconsistentOHLC)

    history = await provider.history("CL", date(2025, 1, 2), date(2025, 1, 2))

    assert history.rows == [
        {
            "date": "2025-01-02",
            "open": 71.0,
            "high": 72.5,
            "low": 71.0,
            "close": 72.5,
            "volume": 120,
        }
    ]
    assert history.metadata["ohlc_bound_repairs"] == 1
    assert history.metadata["finalized_bars_only"] is True
    assert history.metadata["bar_finalization_verified"] is False
    assert history.metadata["ohlc_bound_repair_first_date"] == "2025-01-02"
    assert history.metadata["ohlc_bound_repair_last_date"] == "2025-01-02"


@pytest.mark.asyncio
async def test_futures_history_excludes_current_utc_day(
    monkeypatch,
    tmp_path,
) -> None:
    current_day = datetime.now(UTC).date()
    completed_day = current_day - timedelta(days=1)

    class CurrentDayAKShare(FakeAKShare):
        @staticmethod
        def futures_foreign_hist(symbol: str) -> pd.DataFrame:
            assert symbol == "CL"
            return pd.DataFrame(
                [
                    {
                        "date": completed_day.isoformat(),
                        "open": 70.0,
                        "high": 72.0,
                        "low": 69.0,
                        "close": 71.0,
                        "volume": 100,
                    },
                    {
                        "date": current_day.isoformat(),
                        "open": 71.0,
                        "high": 73.0,
                        "low": 70.0,
                        "close": 72.0,
                        "volume": 120,
                    },
                ]
            )

    provider = FuturesProvider(SQLiteCache(tmp_path / "cache.db"))
    monkeypatch.setattr(provider, "_akshare", lambda: CurrentDayAKShare)

    history = await provider.history(
        "CL",
        completed_day,
        current_day,
    )

    assert [row["date"] for row in history.rows] == [completed_day.isoformat()]
    assert history.metadata["finalized_bars_only"] is True
