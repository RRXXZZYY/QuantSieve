from datetime import UTC, date, datetime, timedelta

import pytest
from quantsieve_providers import MacroProvider, SQLiteCache

ECB_CSV = """KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE
EXR.D.JPY.EUR.SP00.A,D,JPY,EUR,SP00,A,2025-01-02,132.0
EXR.D.JPY.EUR.SP00.A,D,JPY,EUR,SP00,A,2025-01-03,133.2
EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2025-01-02,1.2
EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2025-01-03,1.2
"""


def test_macro_provider_recognizes_index_and_fx_aliases() -> None:
    assert MacroProvider.normalize_symbol("^IXIC") == "IXIC"
    assert MacroProvider.normalize_symbol("EUR/USD") == "EURUSD"
    assert MacroProvider.normalize_symbol("USDJPY=X") == "USDJPY"
    assert MacroProvider.supports("NDX")
    assert MacroProvider.supports("USDJPY")
    assert not MacroProvider.supports("BTCUSDT")


def test_nasdaq_history_parses_real_index_ohlc(monkeypatch) -> None:
    monkeypatch.setattr(
        MacroProvider,
        "_request_json",
        classmethod(
            lambda *_args, **_kwargs: {
                "data": {
                    "tradesTable": {
                        "rows": [
                            {
                                "date": "01/03/2025",
                                "open": "21,100.00",
                                "high": "21,200.00",
                                "low": "21,000.00",
                                "close": "21,150.00",
                                "volume": "--",
                            },
                            {
                                "date": "01/02/2025",
                                "open": "20,900.00",
                                "high": "21,050.00",
                                "low": "20,850.00",
                                "close": "21,000.00",
                                "volume": "--",
                            },
                        ]
                    }
                },
                "status": {"rCode": 200},
            }
        ),
    )

    frame, source_url = MacroProvider._nasdaq_history(
        "COMP",
        date(2025, 1, 1),
        date(2025, 1, 3),
    )

    assert source_url.endswith("/comp/historical")
    assert frame["close"].tolist() == [21000.0, 21150.0]
    assert frame["open"].tolist() == [20900.0, 21100.0]
    assert frame["volume"].tolist() == [0.0, 0.0]


def test_ecb_history_calculates_cross_rate(monkeypatch) -> None:
    monkeypatch.setattr(
        MacroProvider,
        "_request_csv",
        staticmethod(lambda *_args, **_kwargs: ECB_CSV),
    )

    frame, source_url = MacroProvider._ecb_pair_history(
        "USD",
        "JPY",
        date(2025, 1, 1),
        date(2025, 1, 3),
    )

    assert "D.JPY+USD.EUR.SP00.A" in source_url
    assert frame["close"].tolist() == pytest.approx([110.0, 111.0])


@pytest.mark.asyncio
async def test_macro_history_discloses_reference_execution_and_resamples_weekly(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        MacroProvider,
        "_request_json",
        classmethod(
            lambda *_args, **_kwargs: {
                "data": {
                    "tradesTable": {
                        "rows": [
                            {
                                "date": "01/06/2025",
                                "open": "5920",
                                "high": "5950",
                                "low": "5910",
                                "close": "5940",
                            },
                            {
                                "date": "01/03/2025",
                                "open": "5900",
                                "high": "5945",
                                "low": "5890",
                                "close": "5920",
                            },
                            {
                                "date": "01/02/2025",
                                "open": "5880",
                                "high": "5910",
                                "low": "5870",
                                "close": "5900",
                            },
                        ]
                    }
                },
                "status": {"rCode": 200},
            }
        ),
    )
    provider = MacroProvider(SQLiteCache(tmp_path / "cache.db"))

    daily = await provider.history_interval(
        "NDX",
        date(2025, 1, 1),
        date(2025, 1, 6),
        interval="1d",
    )
    weekly = await provider.history_interval(
        "NDX",
        date(2025, 1, 1),
        date(2025, 1, 6),
        interval="1wk",
    )

    assert daily.metadata["tradable_quote"] is False
    assert daily.metadata["ohlc_derived_from_close"] is False
    assert daily.metadata["finalized_bars_only"] is True
    assert daily.metadata["bar_finalization_policy"] == (
        "source_day_strictly_before_current_utc_date"
    )
    assert daily.metadata["bar_finalization_verified"] is False
    assert daily.metadata["bars"] == 3
    assert len(weekly.rows) == 2
    assert weekly.rows[0]["open"] == 5880.0
    assert weekly.rows[0]["close"] == 5920.0


@pytest.mark.asyncio
async def test_macro_rejects_intraday_intervals(tmp_path) -> None:
    provider = MacroProvider(SQLiteCache(tmp_path / "cache.db"))

    with pytest.raises(ValueError, match="daily or weekly"):
        await provider.history_interval("NDX", interval="1h")


def test_macro_envelope_excludes_current_utc_day() -> None:
    current_day = datetime.now(UTC).date()
    completed_day = current_day - timedelta(days=1)

    envelope = MacroProvider._envelope(
        "NDX",
        [
            {
                "date": completed_day.isoformat(),
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 101,
                "volume": 0,
            },
            {
                "date": current_day.isoformat(),
                "open": 101,
                "high": 103,
                "low": 100,
                "close": 102,
                "volume": 0,
            },
        ],
        "https://example.test",
        "Fixture",
        close_only=False,
    )

    assert [row["date"] for row in envelope.rows] == [completed_day.isoformat()]
    assert envelope.metadata["finalized_bars_only"] is True
