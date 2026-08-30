import asyncio
from datetime import UTC, datetime

import pytest
from quantsieve_monitor import EventStore, MonitorService
from quantsieve_monitor.models import (
    EventKind,
    MarketAnalysis,
    MonitorEvent,
)


@pytest.mark.asyncio
async def test_refresh_runs_independent_sources_concurrently(tmp_path) -> None:
    both_started = asyncio.Event()
    starts = 0

    async def loader(_profiles):
        nonlocal starts
        starts += 1
        if starts == 2:
            both_started.set()
        await both_started.wait()
        return []

    service = MonitorService(
        EventStore(tmp_path / "events.db"),
        [],
        [loader, loader],
    )

    assert await asyncio.wait_for(service.refresh(), timeout=1) == []


@pytest.mark.asyncio
async def test_refresh_analyzes_new_events_concurrently(tmp_path) -> None:
    both_started = asyncio.Event()
    starts = 0

    async def analyzer(_event):
        nonlocal starts
        starts += 1
        if starts == 2:
            both_started.set()
        await both_started.wait()
        return MarketAnalysis(
            relevance="high",
            summary="并发分析完成",
            impacts=[],
            method="rules",
        )

    now = datetime.now(UTC)
    events = [
        MonitorEvent(
            source="fixture",
            source_id=str(index),
            profile_id="fixture",
            profile_name="Fixture",
            kind=EventKind.NEWS,
            title=f"Event {index}",
            content="Content",
            url=f"https://example.com/{index}",
            occurred_at=now,
            available_at=now,
        )
        for index in range(2)
    ]

    async def loader(_profiles):
        return events

    service = MonitorService(
        EventStore(tmp_path / "events.db"),
        [],
        [loader],
        analyzer=analyzer,
        analysis_concurrency=2,
    )

    refreshed = await asyncio.wait_for(service.refresh(), timeout=1)

    assert all(event.analysis == "并发分析完成" for event in refreshed)


@pytest.mark.asyncio
async def test_refresh_reanalyzes_existing_rule_events_when_enabled(tmp_path) -> None:
    now = datetime.now(UTC)
    stored = MonitorEvent(
        source="fixture",
        source_id="existing",
        profile_id="fixture",
        profile_name="Fixture",
        kind=EventKind.NEWS,
        title="Event",
        content="Content",
        url="https://example.com/existing",
        occurred_at=now,
        available_at=now,
        analysis="旧规则结论",
        market_relevance="medium",
        analysis_method="rules",
    )
    store = EventStore(tmp_path / "events.db")
    store.upsert([stored])
    fresh = stored.model_copy(update={"analysis": None, "analysis_method": None})

    async def loader(_profiles):
        return [fresh]

    async def analyzer(_event):
        return MarketAnalysis(
            relevance="low",
            summary="新规则结论",
            impacts=[],
            method="rules",
        )

    service = MonitorService(
        store,
        [],
        [loader],
        analyzer=analyzer,
        refresh_existing_rule_analysis=True,
    )

    refreshed = await service.refresh()

    assert refreshed[0].analysis == "新规则结论"
    visible = store.list_visible(now=now)
    assert visible[0].analysis == "新规则结论"
    assert visible[0].market_relevance == "low"
