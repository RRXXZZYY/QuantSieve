from datetime import date, timedelta

import pandas as pd
import pytest
from quantsieve_providers import Citation, DataEnvelope
from quantsieve_providers.akshare import AKShareProvider
from quantsieve_providers.cache import SQLiteCache


def test_cn_session_4h_combines_morning_and_afternoon_trading() -> None:
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0],
            "high": [101.0, 103.0, 104.0, 105.0, 105.0, 107.0, 108.0, 109.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0],
            "close": [101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0],
            "volume": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0],
        },
        index=pd.to_datetime(
            [
                "2026-07-27 09:30",
                "2026-07-27 10:30",
                "2026-07-27 11:30",
                "2026-07-27 13:00",
                "2026-07-28 09:30",
                "2026-07-28 10:30",
                "2026-07-28 11:30",
                "2026-07-28 13:00",
            ]
        ),
    )

    sessions = AKShareProvider._resample_cn_session_4h(frame)

    assert len(sessions) == 2
    assert sessions.index[0] == pd.Timestamp("2026-07-27 09:30", tz="Asia/Shanghai")
    assert sessions.iloc[0].to_dict() == {
        "open": 100.0,
        "high": 105.0,
        "low": 99.0,
        "close": 104.0,
        "volume": 46.0,
    }
    assert sessions.iloc[1].to_dict() == {
        "open": 104.0,
        "high": 109.0,
        "low": 103.0,
        "close": 108.0,
        "volume": 62.0,
    }


@pytest.mark.asyncio
async def test_cached_frame_retries_one_transient_upstream_failure(tmp_path) -> None:
    provider = AKShareProvider(SQLiteCache(tmp_path / "cache.db"))
    attempts = 0

    def loader() -> pd.DataFrame:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary upstream failure")
        return pd.DataFrame([{"date": "2026-07-27", "close": 100.0}])

    rows = await provider._cached_frame(
        "history_interval",
        loader,
        timedelta(minutes=1),
        symbol="600519",
    )

    assert attempts == 2
    assert rows == [{"date": "2026-07-27", "close": 100.0}]


@pytest.mark.asyncio
async def test_4h_history_falls_back_to_completed_daily_session(monkeypatch, tmp_path) -> None:
    provider = AKShareProvider()
    daily = DataEnvelope(
        symbol="600519",
        kind="history",
        rows=[
            {
                "date": "2026-07-27T00:00:00+00:00",
                "open": 100.0,
                "high": 105.0,
                "low": 99.0,
                "close": 104.0,
                "volume": 10.0,
            }
        ],
        citations=[Citation(source="AKShare")],
        metadata={"source": "daily"},
    )

    async def unavailable_minute_history(*_args, **_kwargs):
        raise RuntimeError("AKShare minute history is unavailable")

    async def completed_daily_history(*_args, **_kwargs):
        return daily

    monkeypatch.setattr(provider, "_cached_frame", unavailable_minute_history)
    monkeypatch.setattr(provider, "history", completed_daily_history)

    result = await provider.history_interval(
        "600519",
        date(2026, 7, 27),
        date(2026, 7, 27),
        interval="4h",
    )

    assert result.citations[0].source == "AKShare"
    assert result.metadata["fallback"] is True
    assert result.metadata["interval_semantics"] == "four_trading_hours_per_session"
    assert result.rows[0]["close"] == 104.0
