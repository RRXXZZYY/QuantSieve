from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any, cast

from quantsieve_monitor import EventStore, MonitorService
from quantsieve_monitor.analyzer import OpenAICompatibleAnalyzer, RuleBasedMarketAnalyzer
from quantsieve_monitor.models import MonitorProfile
from quantsieve_monitor.profiles import DEFAULT_PROFILES
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

from .config import Settings
from .providers import ProviderRouter, RequestedProvider


class MonitorScheduler:
    """Polls time-sensitive sources in the API process and persists deduplicated events."""

    def __init__(
        self,
        settings: Settings,
        store: EventStore,
        providers: ProviderRouter,
    ) -> None:
        self.settings = settings
        self.store = store
        self.x_source = XApiSource(
            settings.x_bearer_token,
            base_url=settings.x_api_base_url,
        )
        self.geopolitical_source = GeopoliticalRssSource()
        self.public_rss_source = PublicRssSource()
        self.official_intel_source = OfficialIntelligenceRssSource()
        self.gdelt_source = GdeltHeadlinesSource()
        self.ofac_source = OfacRecentActionsSource()
        self.hkma_source = HkmaPressReleaseSource()
        self.sec_source = Sec13FSource(
            SECEdgarProvider(settings.sec_user_agent, SQLiteCache(settings.cache_path))
        )

        async def load_pulse_history(
            profile: MonitorProfile,
            start: date,
            end: date,
        ) -> DataEnvelope:
            provider = cast(RequestedProvider, profile.pulse_provider or "auto")
            adapter = providers.resolve(profile.pulse_symbol or "", provider)
            return await adapter.history(profile.pulse_symbol or "", start, end)

        self.linked_market_source = LinkedMarketPulseSource(load_pulse_history)
        rules = RuleBasedMarketAnalyzer()
        if settings.llm_api_key:
            analyzer = OpenAICompatibleAnalyzer(
                settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                fallback=rules,
            ).analyze
        else:
            analyzer = rules.analyze
        self.service = MonitorService(
            store,
            DEFAULT_PROFILES,
            [
                self.x_source.fetch,
                self.public_rss_source.fetch,
                self.geopolitical_source.fetch,
                self.official_intel_source.fetch,
                self.gdelt_source.fetch,
                self.ofac_source.fetch,
                self.hkma_source.fetch,
                self.sec_source.fetch,
                self.linked_market_source.fetch,
            ],
            analyzer=analyzer,
            refresh_existing_rule_analysis=not bool(settings.llm_api_key),
        )
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._status: dict[str, Any] = {
            "enabled": settings.monitor_scheduler_enabled,
            "running": False,
            "poll_seconds": max(settings.monitor_poll_seconds, 30),
            "last_refresh_at": None,
            "last_event_count": 0,
            "last_error": None,
        }

    @property
    def status(self) -> dict[str, Any]:
        return {
            **self._status,
            "sources": {
                self.x_source.name: self.x_source.last_status,
                self.public_rss_source.name: self.public_rss_source.last_status,
                self.geopolitical_source.name: self.geopolitical_source.last_status,
                self.official_intel_source.name: self.official_intel_source.last_status,
                self.gdelt_source.name: self.gdelt_source.last_status,
                self.ofac_source.name: self.ofac_source.last_status,
                self.hkma_source.name: self.hkma_source.last_status,
                self.sec_source.name: self.sec_source.last_status,
                self.linked_market_source.name: self.linked_market_source.last_status,
            },
        }

    async def start(self) -> None:
        if not self.settings.monitor_scheduler_enabled or self._task:
            return
        self._stop.clear()
        self._status["running"] = True
        self._task = asyncio.create_task(self._run(), name="quantsieve-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None
        self._status["running"] = False

    async def refresh_once(self) -> int:
        events = await self.service.refresh()
        self._status.update(
            {
                "last_refresh_at": datetime.now(UTC).isoformat(),
                "last_event_count": len(events),
                "last_error": None,
            }
        )
        return len(events)

    async def _run(self) -> None:
        interval = max(self.settings.monitor_poll_seconds, 30)
        while not self._stop.is_set():
            try:
                await self.refresh_once()
            except Exception as exc:
                self._status.update(
                    {
                        "last_refresh_at": datetime.now(UTC).isoformat(),
                        "last_error": f"{type(exc).__name__}: {exc}",
                    }
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue
