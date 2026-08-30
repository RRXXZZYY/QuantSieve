from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .models import MarketAnalysis, MonitorEvent, MonitorProfile
from .plugins import MonitorPlugin
from .store import EventStore

EventLoader = Callable[[list[MonitorProfile]], Awaitable[list[MonitorEvent]]]
EventAnalyzer = Callable[[MonitorEvent], Awaitable[MarketAnalysis]]


class MonitorService:
    def __init__(
        self,
        store: EventStore,
        profiles: list[MonitorProfile],
        loaders: list[EventLoader],
        *,
        analyzer: EventAnalyzer | None = None,
        analysis_concurrency: int = 4,
        refresh_existing_rule_analysis: bool = False,
        plugins: list[MonitorPlugin] | None = None,
    ) -> None:
        self.store = store
        self.profiles = profiles
        self.loaders = loaders
        self.analyzer = analyzer
        self.analysis_concurrency = max(1, analysis_concurrency)
        self.refresh_existing_rule_analysis = refresh_existing_rule_analysis
        self.plugins = plugins or []

    async def refresh(self) -> list[MonitorEvent]:
        events: list[MonitorEvent] = []
        results = await asyncio.gather(
            *(loader(self.profiles) for loader in self.loaders),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, list):
                events.extend(result)
        if self.analyzer:
            existing_rule_keys = (
                self.store.rule_analyzed_keys(events)
                if self.refresh_existing_rule_analysis
                else set()
            )
            pending = [
                event
                for event in events
                if event.analysis is None
                and (
                    (event.source, event.source_id) in existing_rule_keys
                    or not self.store.contains(event.source, event.source_id)
                )
            ]
            semaphore = asyncio.Semaphore(self.analysis_concurrency)

            async def analyze(event: MonitorEvent) -> MarketAnalysis:
                async with semaphore:
                    return await self.analyzer(event)  # type: ignore[misc]

            analyses = await asyncio.gather(
                *(analyze(event) for event in pending),
                return_exceptions=True,
            )
            for event, analysis in zip(pending, analyses, strict=True):
                if isinstance(analysis, MarketAnalysis):
                    event.analysis = analysis.summary
                    event.market_relevance = analysis.relevance
                    event.impact_assets = analysis.impacts
                    event.analysis_method = analysis.method
        self.store.upsert(events)
        for plugin in self.plugins:
            await plugin.on_events(events)
        return events
