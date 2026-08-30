from datetime import date

import httpx
import pandas as pd
import pytest
from quantsieve_providers import Citation, DataEnvelope, SQLiteCache, YFinanceProvider


def test_direct_chart_history_parses_and_adjusts(monkeypatch) -> None:
    requested_intervals: list[str] = []
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1_735_689_600, 1_735_776_000],
                    "indicators": {
                        "quote": [
                            {
                                "open": [100.0, 101.0],
                                "high": [102.0, 103.0],
                                "low": [99.0, 100.0],
                                "close": [101.0, 102.0],
                                "volume": [10, 11],
                            }
                        ],
                        "adjclose": [{"adjclose": [50.5, 51.0]}],
                    },
                }
            ]
        }
    }

    def respond(request: httpx.Request) -> httpx.Response:
        requested_intervals.append(request.url.params["interval"])
        return httpx.Response(200, request=request, json=payload)

    transport = httpx.MockTransport(respond)

    class MockClient(httpx.Client):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "Client", MockClient)
    frame = YFinanceProvider._direct_chart_history(
        "AAPL", date(2025, 1, 1), date(2025, 1, 2), "15m"
    )

    assert frame["close"].tolist() == [50.5, 51.0]
    assert frame["open"].tolist() == [50.0, 50.5]
    assert requested_intervals == ["15m"]


def test_eastmoney_history_parses_kline_rows(monkeypatch) -> None:
    payload = {
        "data": {
            "klines": [
                "2025-01-02,247.370,242.290,247.540,240.260,55740731,13619384576",
                "2025-01-03,241.800,241.800,242.620,240.330,40244114,9782101760",
            ]
        }
    }

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["secid"] == "105.AAPL"
        return httpx.Response(200, request=request, json=payload)

    transport = httpx.MockTransport(respond)

    class MockClient(httpx.Client):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "Client", MockClient)
    frame = YFinanceProvider._eastmoney_history(
        "AAPL", date(2025, 1, 1), date(2025, 1, 3)
    )

    assert frame["close"].tolist() == [242.29, 241.8]
    assert frame["volume"].tolist() == [55740731.0, 40244114.0]


def test_tencent_history_resolves_exchange_and_parses_rows(monkeypatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        parameter = request.url.params["param"]
        if parameter == "usAAPL,day,,,2,qfq":
            payload = {
                "data": {
                    "usAAPL": {
                        "qt": {"usAAPL": ["delay", "Apple", "AAPL.OQ"]},
                        "day": [],
                    }
                }
            }
        else:
            assert parameter.startswith("usAAPL.OQ,day,2025-01-01,2025-01-03,")
            payload = {
                "data": {
                    "usAAPL.OQ": {
                        "day": [
                            ["2025-01-02", "247.37", "242.29", "247.54", "240.26", "55740731"],
                            ["2025-01-03", "241.80", "241.80", "242.62", "240.33", "40244114"],
                        ]
                    }
                }
            }
        return httpx.Response(200, request=request, json=payload)

    transport = httpx.MockTransport(respond)

    class MockClient(httpx.Client):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "Client", MockClient)
    frame = YFinanceProvider._tencent_history(
        "AAPL", date(2025, 1, 1), date(2025, 1, 3)
    )

    assert frame["close"].tolist() == [242.29, 241.8]
    assert frame["volume"].tolist() == [55740731.0, 40244114.0]


@pytest.mark.asyncio
async def test_history_uses_and_caches_tencent_fallback(monkeypatch, tmp_path) -> None:
    provider = YFinanceProvider(SQLiteCache(tmp_path / "cache.db"))
    monkeypatch.setattr(provider, "_ticker", lambda _symbol: EmptyTicker())
    monkeypatch.setattr(
        provider,
        "_direct_chart_history",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("rate limited")),
    )
    fallback = pd.DataFrame(
        [{"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 10}],
        index=pd.to_datetime(["2025-01-02"], utc=True),
    )
    monkeypatch.setattr(provider, "_tencent_history", lambda *_args: fallback)

    first = await provider.history("aapl", date(2025, 1, 1), date(2025, 1, 3))
    monkeypatch.setattr(
        provider,
        "_tencent_history",
        lambda *_args: (_ for _ in ()).throw(AssertionError("cache was not used")),
    )
    second = await provider.history("AAPL", date(2025, 1, 1), date(2025, 1, 3))

    assert first.citations[0].source == "Tencent Finance"
    assert first.metadata["fallback"] is True
    assert second.citations[0].source == "Tencent Finance"
    assert second.rows == first.rows


class EmptyTicker:
    def history(self, **_kwargs) -> pd.DataFrame:
        return pd.DataFrame()


@pytest.mark.asyncio
async def test_weekly_history_aggregates_daily_fallback_when_chart_fails(
    monkeypatch,
    tmp_path,
) -> None:
    provider = YFinanceProvider(SQLiteCache(tmp_path / "cache.db"))
    daily_frame = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0, 104.0],
            "high": [102.0, 103.0, 104.0, 105.0, 106.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0],
            "close": [101.0, 102.0, 103.0, 104.0, 105.0],
            "volume": [10.0, 11.0, 12.0, 13.0, 14.0],
        },
        index=pd.to_datetime(
            ["2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10"],
            utc=True,
        ),
    )

    monkeypatch.setattr(
        provider,
        "_direct_chart_history",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("rate limited")),
    )

    async def daily_history(*_args, **_kwargs) -> DataEnvelope:
        return DataEnvelope(
            symbol="SPY",
            kind="history",
            rows=[
                {"date": index.isoformat(), **row}
                for index, row in daily_frame.to_dict(orient="index").items()
            ],
            citations=[Citation(source="Tencent Finance")],
        )

    monkeypatch.setattr(provider, "history", daily_history)

    weekly = await provider.history_interval(
        "SPY",
        date(2025, 1, 6),
        date(2025, 1, 10),
        interval="1wk",
    )

    assert weekly.citations[0].source == "Tencent Finance"
    assert weekly.metadata["fallback"] is True
    assert weekly.rows[0]["open"] == 100.0
    assert weekly.rows[0]["close"] == 105.0
    assert weekly.rows[0]["volume"] == 60.0


def test_us_session_4h_aligns_to_regular_open_and_closing_segment() -> None:
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
            "high": [102.0, 103.0, 104.0, 105.0, 106.0, 107.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0, 104.0],
            "close": [101.0, 102.0, 103.0, 104.0, 105.0, 106.0],
            "volume": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
        },
        index=pd.to_datetime(
            [
                "2026-07-27T13:30:00Z",
                "2026-07-27T14:30:00Z",
                "2026-07-27T16:30:00Z",
                "2026-07-27T17:30:00Z",
                "2026-07-27T18:30:00Z",
                "2026-07-27T19:30:00Z",
            ],
            utc=True,
        ),
    )

    sessions = YFinanceProvider._resample_us_session_4h(frame)

    assert sessions.index.tolist() == [
        pd.Timestamp("2026-07-27T09:30:00-04:00"),
        pd.Timestamp("2026-07-27T13:30:00-04:00"),
    ]
    assert sessions.iloc[0].to_dict() == {
        "open": 100.0,
        "high": 104.0,
        "low": 99.0,
        "close": 103.0,
        "volume": 33.0,
    }
    assert sessions.iloc[1].to_dict() == {
        "open": 103.0,
        "high": 107.0,
        "low": 102.0,
        "close": 106.0,
        "volume": 42.0,
    }
