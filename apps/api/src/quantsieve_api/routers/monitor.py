from __future__ import annotations

import asyncio
import math
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from quantsieve_monitor import EventStore, MonitorEvent, MonitorProfile, MonitorService
from quantsieve_monitor.analyzer import OpenAICompatibleAnalyzer, RuleBasedMarketAnalyzer
from quantsieve_monitor.models import EventKind
from quantsieve_monitor.profiles import DEFAULT_PROFILES
from quantsieve_monitor.service import EventLoader
from quantsieve_monitor.sources import (
    GdeltHeadlinesSource,
    GeopoliticalRssSource,
    HkmaPressReleaseSource,
    LinkedMarketPulseSource,
    OfacRecentActionsSource,
    OfficialIntelligenceRssSource,
    PublicRssSource,
    Sec13FSource,
    XApiSource,
)
from quantsieve_providers import DataEnvelope, SECEdgarProvider, SQLiteCache

from ..config import Settings
from ..news_translation import NewsTranslationService
from ..providers import ProviderRouter, RequestedProvider

router = APIRouter(tags=["monitor"])


class RefreshRequest(BaseModel):
    api_key: SecretStr | None = None
    base_url: str | None = None
    model: str | None = None


class MonitorTranslationReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=1_024)


class MonitorTranslationsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MonitorTranslationReference] = Field(
        min_length=1,
        max_length=50,
    )

    @model_validator(mode="after")
    def reject_duplicate_references(self) -> MonitorTranslationsRequest:
        keys = [(item.source, item.source_id) for item in self.items]
        if len(set(keys)) != len(keys):
            raise ValueError("翻译请求不能包含重复事件。")
        return self


@router.get("/monitor/profiles")
async def monitor_profiles() -> list[dict[str, Any]]:
    return [profile.model_dump() for profile in DEFAULT_PROFILES]


@router.get("/monitor/feed")
async def monitor_feed(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    profile_id: str | None = Query(default=None, min_length=1),
) -> list[dict[str, Any]]:
    store: EventStore = request.app.state.event_store
    if profile_id is None:
        candidates = [
            *store.list_visible(limit=1_000),
            *store.list_visible_source_sample(limit_per_source=12),
        ]
    else:
        candidates = store.list_visible(
            limit=min(max(limit * 12, 240), 1_000),
            profile_id=profile_id,
        )
    return [
        event.model_dump(mode="json")
        for event in _curate_events(candidates, limit=limit, profile_id=profile_id)
    ]


@router.get("/monitor/status")
async def monitor_status(request: Request) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    scheduler = request.app.state.monitor_scheduler
    translation_service: NewsTranslationService = (
        request.app.state.news_translation_service
    )
    return {
        "x_configured": bool(settings.x_bearer_token),
        "analysis_configured": bool(settings.llm_api_key),
        "translation": translation_service.status,
        "scheduler": scheduler.status,
    }


@router.post("/monitor/translations")
async def monitor_translations(
    request: Request,
    body: MonitorTranslationsRequest,
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    if len(body.items) > settings.translation_batch_limit:
        raise HTTPException(
            status_code=422,
            detail=(
                "单次最多翻译 "
                f"{settings.translation_batch_limit} 条当前可见事件。"
            ),
        )
    keys = [(item.source, item.source_id) for item in body.items]
    store: EventStore = request.app.state.event_store
    events = await asyncio.to_thread(store.get_visible_by_keys, keys)
    translation_service: NewsTranslationService = (
        request.app.state.news_translation_service
    )
    translated = await translation_service.translate_events(events)
    by_key = {
        (str(item["source"]), str(item["source_id"])): item
        for item in translated
    }
    items: list[dict[str, Any]] = []
    for source, source_id in keys:
        item = by_key.get((source, source_id))
        if item is None:
            item = {
                "source": source,
                "source_id": source_id,
                "source_fingerprint": None,
                "status": "not_found",
                "title_zh": None,
                "content_zh": None,
                "analysis_zh": None,
                "impact_reasons_zh": [],
                "fields": {},
                "translated_fields": 0,
                "cached_fields": 0,
                "unavailable_fields": 0,
            }
        items.append(item)
    status = translation_service.status
    return {
        "target_language": "zh-Hans",
        "engine": status["engine"],
        "availability": status["availability"],
        "items": items,
    }


@router.post("/monitor/refresh")
async def refresh_monitor(request: Request, body: RefreshRequest) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    cache = SQLiteCache(settings.cache_path)
    sec = SECEdgarProvider(settings.sec_user_agent, cache)
    public_rss = PublicRssSource()
    x_api = XApiSource(
        settings.x_bearer_token,
        base_url=settings.x_api_base_url,
    )
    geopolitics = GeopoliticalRssSource()
    official_intel = OfficialIntelligenceRssSource()
    gdelt = GdeltHeadlinesSource()
    ofac = OfacRecentActionsSource()
    hkma = HkmaPressReleaseSource()
    sec_filings = Sec13FSource(sec)

    async def load_pulse_history(
        profile: MonitorProfile,
        start: date,
        end: date,
    ) -> DataEnvelope:
        provider_router: ProviderRouter = request.app.state.providers
        provider = cast(RequestedProvider, profile.pulse_provider or "auto")
        adapter = provider_router.resolve(profile.pulse_symbol or "", provider)
        return await adapter.history(profile.pulse_symbol or "", start, end)

    linked_market = LinkedMarketPulseSource(load_pulse_history)
    loaders: list[EventLoader] = [
        x_api.fetch,
        public_rss.fetch,
        geopolitics.fetch,
        official_intel.fetch,
        gdelt.fetch,
        ofac.fetch,
        hkma.fetch,
        sec_filings.fetch,
        linked_market.fetch,
    ]
    key = body.api_key.get_secret_value() if body.api_key else settings.llm_api_key
    rules = RuleBasedMarketAnalyzer()
    analyzer = rules.analyze
    if key:
        llm = OpenAICompatibleAnalyzer(
            key,
            base_url=body.base_url or settings.llm_base_url,
            model=body.model or settings.llm_model,
            fallback=rules,
        )
        analyzer = llm.analyze
    service = MonitorService(
        request.app.state.event_store,
        DEFAULT_PROFILES,
        loaders,
        analyzer=analyzer,
        refresh_existing_rule_analysis=not bool(key),
    )
    events = await service.refresh()
    now = datetime.now(UTC)
    return {
        "fetched": len(events),
        "visible": sum(event.available_at <= now for event in events),
        "delayed": sum(event.available_at > now for event in events),
        "sources": dict(Counter(event.source for event in events)),
        "kinds": dict(Counter(event.kind.value for event in events)),
        "source_status": {
            x_api.name: x_api.last_status,
            public_rss.name: public_rss.last_status,
            geopolitics.name: geopolitics.last_status,
            official_intel.name: official_intel.last_status,
            gdelt.name: gdelt.last_status,
            ofac.name: ofac.last_status,
            hkma.name: hkma.last_status,
            sec_filings.name: sec_filings.last_status,
            linked_market.name: linked_market.last_status,
        },
        "analysis_method": "ai" if key else "rules",
    }


def ensure_demo_event(store: EventStore) -> None:
    occurred_at = datetime.now(UTC) - timedelta(days=1)
    store.upsert(
        [
            MonitorEvent(
                source="recorded-demo",
                source_id="welcome-snapshot",
                profile_id="berkshire",
                profile_name="Berkshire Hathaway",
                kind=EventKind.FILING,
                title="SEC 13F workflow ready",
                content=(
                    "This recorded card demonstrates the monitor layout. "
                    "Refresh to retrieve the latest official filing."
                ),
                url="https://www.sec.gov/edgar/search/",
                occurred_at=occurred_at,
                available_at=occurred_at,
                analysis=(
                    "A new filing can reveal changes in reported holdings; "
                    "review the official filing before drawing conclusions."
                ),
                tags=["demo", "SEC", "13F"],
            )
        ]
    )


_SOURCE_WEIGHT = {
    "x-api": 48,
    "sec-edgar": 42,
    "official-intel": 38,
    "ofac-actions": 38,
    "hkma-press": 38,
    "un-news": 34,
    "linked-market-data": 20,
    "gdelt-headlines": 8,
    "public-rss": 6,
    "recorded-demo": -80,
}
_RELEVANCE_WEIGHT = {
    "critical": 80,
    "high": 58,
    "medium": 38,
    "low": 14,
    "unrated": 18,
    "unrelated": 0,
}
_KIND_WEIGHT = {
    EventKind.SOCIAL: 16,
    EventKind.FILING: 14,
    EventKind.GEOPOLITICAL: 10,
    EventKind.NEWS: 7,
    EventKind.MARKET: 2,
}


def _curate_events(
    events: list[MonitorEvent],
    *,
    limit: int,
    profile_id: str | None,
) -> list[MonitorEvent]:
    """Keep one noisy headline firehose from hiding official and market evidence."""
    now = datetime.now(UTC)

    def score(event: MonitorEvent) -> float:
        occurred_at = event.occurred_at.astimezone(UTC)
        age_hours = max((now - occurred_at).total_seconds() / 3_600, 0)
        freshness = max(36 - age_hours * 0.75, -36)
        return (
            _RELEVANCE_WEIGHT[event.market_relevance]
            + _SOURCE_WEIGHT.get(event.source, 12)
            + _KIND_WEIGHT[event.kind]
            + freshness
        )

    def rank_key(event: MonitorEvent) -> tuple[float, float, str, str]:
        return (
            -score(event),
            -event.occurred_at.timestamp(),
            event.source,
            event.source_id,
        )

    def identity(event: MonitorEvent) -> str:
        # Related-market cards often share a provider URL even though each
        # profile and symbol is independent evidence.
        if event.kind is EventKind.MARKET:
            return f"event:{event.source}:{event.source_id}"
        if event.url:
            return f"url:{event.url.strip().casefold()}"
        title = re.sub(r"\s+", " ", event.title).strip().casefold()
        return f"title:{title}"

    representatives: dict[str, MonitorEvent] = {}
    for event in events:
        key = identity(event)
        current = representatives.get(key)
        if current is None or rank_key(event) < rank_key(current):
            representatives[key] = event
    if profile_id is not None:
        return sorted(
            representatives.values(),
            key=lambda event: (
                -event.occurred_at.timestamp(),
                event.source,
                event.source_id,
            ),
        )[:limit]

    ranked = sorted(representatives.values(), key=rank_key)
    selected: list[MonitorEvent] = []
    selected_keys: set[tuple[str, str]] = set()
    source_counts: Counter[str] = Counter()
    profile_counts: Counter[str] = Counter()
    gdelt_cap = max(6, math.ceil(limit * 0.32))
    profile_cap = max(4, math.ceil(limit * 0.24))

    def add(event: MonitorEvent) -> bool:
        key = (event.source, event.source_id)
        if (
            len(selected) >= limit
            or key in selected_keys
            or (
                event.source == "gdelt-headlines"
                and source_counts[event.source] >= gdelt_cap
            )
            or profile_counts[event.profile_id] >= profile_cap
        ):
            return False
        selected.append(event)
        selected_keys.add(key)
        source_counts[event.source] += 1
        profile_counts[event.profile_id] += 1
        return True

    meaningful = [
        event
        for event in ranked
        if event.market_relevance != "unrelated" and event.source != "recorded-demo"
    ]
    anchors: dict[tuple[str, str], MonitorEvent] = {}
    for kind in (
        EventKind.SOCIAL,
        EventKind.FILING,
        EventKind.MARKET,
        EventKind.NEWS,
        EventKind.GEOPOLITICAL,
    ):
        anchor = next((event for event in meaningful if event.kind is kind), None)
        if anchor:
            anchors[(anchor.source, anchor.source_id)] = anchor
    for source in (
        "x-api",
        "sec-edgar",
        "official-intel",
        "ofac-actions",
        "hkma-press",
        "un-news",
    ):
        anchor = next((event for event in meaningful if event.source == source), None)
        if anchor:
            anchors[(anchor.source, anchor.source_id)] = anchor
    for anchor in sorted(anchors.values(), key=rank_key):
        add(anchor)

    for event in [*meaningful, *ranked]:
        if len(selected) >= limit:
            break
        add(event)
    selected = selected[:limit]
    source_tops: dict[str, MonitorEvent] = {}
    for event in selected:
        if event.source == "recorded-demo":
            continue
        current = source_tops.get(event.source)
        if current is None or rank_key(event) < rank_key(current):
            source_tops[event.source] = event
    head = sorted(source_tops.values(), key=rank_key)
    head_keys = {(event.source, event.source_id) for event in head}
    tail = sorted(
        (
            event
            for event in selected
            if (event.source, event.source_id) not in head_keys
        ),
        key=rank_key,
    )
    return [*head, *tail][:limit]
