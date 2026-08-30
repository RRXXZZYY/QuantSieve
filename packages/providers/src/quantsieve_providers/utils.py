from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import pandas as pd

from .models import BarInterval

_INTERVAL_DURATIONS: dict[BarInterval, timedelta] = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1wk": timedelta(weeks=1),
}
_US_EQUITY_TIMEZONE = ZoneInfo("America/New_York")
_CN_EQUITY_TIMEZONE = ZoneInfo("Asia/Shanghai")


def finalized_ohlcv_frame(
    frame: pd.DataFrame,
    *,
    interval: BarInterval,
    market: Literal["us", "cn"],
    now: datetime | None = None,
    finalization_lag: timedelta = timedelta(minutes=2),
    weekly_timestamp_is_period_end: bool = False,
) -> pd.DataFrame:
    """Exclude a still-forming bar before it can enter a research result.

    Public equity sources do not supply Binance-style exchange-close proofs.
    We therefore use a deliberately conservative local-session clock for
    daily bars and a completed interval plus a short publication buffer for
    intraday/weekly bars.  A locally resampled weekly bar is labelled with its
    period end, while an upstream weekly bar is normally labelled with its
    period start; callers declare that difference explicitly. This is a
    source-normalization guard, not proof of an upstream settlement.
    """
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return frame.copy()
    if finalization_lag < timedelta(0):
        raise ValueError("finalization_lag must not be negative")

    reference = now or datetime.now(UTC)
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    cutoff = reference.astimezone(UTC) - finalization_lag
    index = frame.index
    index = index.tz_localize(UTC) if index.tz is None else index.tz_convert(UTC)

    if interval == "1wk" and weekly_timestamp_is_period_end:
        return frame.loc[index <= cutoff].copy()
    if interval in {"15m", "1h", "4h", "1wk"}:
        complete = index + _INTERVAL_DURATIONS[interval] <= cutoff
        return frame.loc[complete].copy()

    close_timezone = _US_EQUITY_TIMEZONE if market == "us" else _CN_EQUITY_TIMEZONE
    close_clock = time(16, 0) if market == "us" else time(15, 0)
    close_times = pd.DatetimeIndex(
        [
            datetime.combine(timestamp.date(), close_clock, tzinfo=close_timezone)
            .astimezone(UTC)
            for timestamp in index
        ]
    )
    return frame.loc[close_times <= cutoff].copy()


def frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    normalized = frame.copy()
    if not isinstance(normalized.index, pd.RangeIndex):
        index_name = normalized.index.name or "date"
        normalized = normalized.reset_index(names=index_name)
    normalized.columns = [str(column).strip().lower() for column in normalized.columns]
    for column in normalized.columns:
        if pd.api.types.is_datetime64_any_dtype(normalized[column]):
            normalized[column] = normalized[column].map(
                lambda value: value.isoformat() if pd.notna(value) else None
            )
    records = normalized.where(pd.notna(normalized), None).to_dict(orient="records")
    return cast(list[dict[str, Any]], records)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
