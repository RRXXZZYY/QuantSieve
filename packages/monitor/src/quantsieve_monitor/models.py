from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class EventKind(StrEnum):
    SOCIAL = "social"
    NEWS = "news"
    FILING = "filing"
    MARKET = "market"
    GEOPOLITICAL = "geopolitical"


MarketRelevance = Literal["critical", "high", "medium", "low", "unrelated", "unrated"]
ImpactDirection = Literal["up", "down", "volatile", "uncertain"]


class MarketImpact(BaseModel):
    asset: str
    direction: ImpactDirection
    reason: str


class MarketAnalysis(BaseModel):
    relevance: MarketRelevance
    summary: str
    impacts: list[MarketImpact] = Field(default_factory=list)
    method: Literal["ai", "rules"]


class MonitorProfile(BaseModel):
    id: str
    display_name: str
    handle: str | None = None
    cik: str | None = None
    pulse_symbol: str | None = None
    pulse_provider: str | None = None
    pulse_context: str | None = None
    category: str
    tags: list[str] = Field(default_factory=list)


class MonitorEvent(BaseModel):
    source: str
    source_id: str
    profile_id: str
    profile_name: str
    kind: EventKind
    title: str
    content: str
    url: str
    occurred_at: datetime
    available_at: datetime
    analysis: str | None = None
    market_relevance: MarketRelevance = "unrated"
    impact_assets: list[MarketImpact] = Field(default_factory=list)
    analysis_method: Literal["ai", "rules"] | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("occurred_at", "available_at", "created_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Monitor event timestamps must include a timezone.")
        return value.astimezone(UTC)

    @classmethod
    def delayed(
        cls,
        *,
        delay: timedelta = timedelta(minutes=30),
        **values: object,
    ) -> MonitorEvent:
        occurred_at = values.get("occurred_at")
        if not isinstance(occurred_at, datetime):
            raise TypeError("occurred_at must be a datetime")
        return cls(**values, available_at=occurred_at + delay)  # type: ignore[arg-type]
