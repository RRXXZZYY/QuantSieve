from datetime import UTC, datetime

import pandas as pd
from quantsieve_providers.utils import finalized_ohlcv_frame


def ohlcv_frame(index: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0] * len(index),
            "high": [101.0] * len(index),
            "low": [99.0] * len(index),
            "close": [100.5] * len(index),
            "volume": [10.0] * len(index),
        },
        index=pd.to_datetime(index, utc=True),
    )


def test_us_daily_bar_waits_for_regular_session_close_and_lag() -> None:
    frame = ohlcv_frame(["2026-07-27", "2026-07-28"])

    before_lag = finalized_ohlcv_frame(
        frame,
        interval="1d",
        market="us",
        now=datetime(2026, 7, 28, 20, 1, tzinfo=UTC),
    )
    after_lag = finalized_ohlcv_frame(
        frame,
        interval="1d",
        market="us",
        now=datetime(2026, 7, 28, 20, 3, tzinfo=UTC),
    )

    assert before_lag.index.tolist() == [pd.Timestamp("2026-07-27", tz="UTC")]
    assert after_lag.index.tolist() == list(frame.index)


def test_cn_daily_bar_waits_for_regular_session_close_and_lag() -> None:
    frame = ohlcv_frame(["2026-07-27", "2026-07-28"])

    before_lag = finalized_ohlcv_frame(
        frame,
        interval="1d",
        market="cn",
        now=datetime(2026, 7, 28, 7, 1, tzinfo=UTC),
    )
    after_lag = finalized_ohlcv_frame(
        frame,
        interval="1d",
        market="cn",
        now=datetime(2026, 7, 28, 7, 3, tzinfo=UTC),
    )

    assert before_lag.index.tolist() == [pd.Timestamp("2026-07-27", tz="UTC")]
    assert after_lag.index.tolist() == list(frame.index)


def test_intraday_and_weekly_bars_require_their_full_interval() -> None:
    intraday = ohlcv_frame(["2026-07-28T18:00:00Z", "2026-07-28T19:00:00Z"])
    complete_intraday = finalized_ohlcv_frame(
        intraday,
        interval="1h",
        market="us",
        now=datetime(2026, 7, 28, 20, 1, tzinfo=UTC),
    )
    weekly = ohlcv_frame(["2026-07-19", "2026-07-26"])
    complete_weekly = finalized_ohlcv_frame(
        weekly,
        interval="1wk",
        market="cn",
        now=datetime(2026, 7, 30, 12, tzinfo=UTC),
    )
    period_end_weekly = finalized_ohlcv_frame(
        weekly,
        interval="1wk",
        market="cn",
        now=datetime(2026, 7, 30, 12, tzinfo=UTC),
        weekly_timestamp_is_period_end=True,
    )

    assert complete_intraday.index.tolist() == [pd.Timestamp("2026-07-28T18:00:00Z")]
    assert complete_weekly.index.tolist() == [pd.Timestamp("2026-07-19", tz="UTC")]
    assert period_end_weekly.index.tolist() == list(weekly.index)


def test_bar_finalization_rejects_naive_reference_time() -> None:
    frame = ohlcv_frame(["2026-07-28"])
    try:
        finalized_ohlcv_frame(
            frame,
            interval="1d",
            market="us",
            now=datetime(2026, 7, 28, 20, 3),
        )
    except ValueError as error:
        assert str(error) == "now must be timezone-aware"
    else:
        raise AssertionError("a naive reference timestamp must be rejected")
