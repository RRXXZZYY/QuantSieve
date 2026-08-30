from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from quantsieve_monitor.models import EventKind, MonitorProfile
from quantsieve_monitor.sources import (
    DEFAULT_OFFICIAL_FEEDS,
    FeedSpec,
    GdeltHeadlinesSource,
    GeopoliticalRssSource,
    HkmaPressReleaseSource,
    LinkedMarketPulseSource,
    OfacRecentActionsSource,
    OfficialIntelligenceRssSource,
    PublicRssSource,
    XApiSource,
)
from quantsieve_providers import Citation, DataEnvelope


@pytest.mark.asyncio
async def test_public_rss_reports_partial_source_health() -> None:
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><item>
      <guid>post-1</guid>
      <title>Public update</title>
      <description>Source content</description>
      <link>https://example.com/post-1</link>
      <pubDate>Fri, 24 Jul 2026 12:00:00 GMT</pubDate>
    </item></channel></rss>"""

    def respond(request: httpx.Request) -> httpx.Response:
        if "working" in request.url.host:
            return httpx.Response(200, request=request, text=rss)
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = PublicRssSource(
            base_urls=["https://working.example", "https://down.example"],
            client=client,
        )
        events = await source.fetch(
            [
                MonitorProfile(
                    id="one",
                    display_name="Person One",
                    handle="one",
                    category="test",
                ),
                MonitorProfile(
                    id="two",
                    display_name="Person Two",
                    handle="two",
                    category="test",
                ),
            ]
        )

    assert len(events) == 2
    assert source.last_status["attempted"] == 2
    assert source.last_status["succeeded"] == 2
    assert source.last_status["failed"] == 0


@pytest.mark.asyncio
async def test_public_rss_failure_is_visible_without_breaking_refresh() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = PublicRssSource(base_urls=["https://down.example"], client=client)
        events = await source.fetch(
            [
                MonitorProfile(
                    id="one",
                    display_name="Person One",
                    handle="one",
                    category="test",
                )
            ]
        )

    assert events == []
    assert source.last_status["attempted"] == 1
    assert source.last_status["succeeded"] == 0
    assert source.last_status["failed"] == 1


@pytest.mark.asyncio
async def test_linked_market_pulse_uses_sourced_rows_and_labels_relation() -> None:
    async def load_history(profile, start, end):
        del start, end
        return DataEnvelope(
            symbol=profile.pulse_symbol or "",
            kind="history",
            rows=[
                {"date": f"2026-07-{day:02d}T00:00:00+00:00", "close": 100 + day}
                for day in range(18, 26)
            ],
            citations=[
                Citation(source="Fixture market", url="https://example.com/market")
            ],
        )

    source = LinkedMarketPulseSource(load_history)
    events = await source.fetch(
        [
            MonitorProfile(
                id="asset",
                display_name="Asset Person",
                pulse_symbol="TEST",
                pulse_provider="fake",
                pulse_context="关联资产",
                category="test",
            )
        ]
    )

    assert len(events) == 1
    assert events[0].kind is EventKind.MARKET
    assert "真实行情" in events[0].tags
    assert "不代表该人物发言" in (events[0].analysis or "")
    assert events[0].url == "https://example.com/market"
    assert source.last_status["attempted"] == 1
    assert source.last_status["succeeded"] == 1
    assert source.last_status["failed"] == 0
    assert await source.fetch([]) == []
    assert "节流窗口" in str(source.last_status["message"])


@pytest.mark.asyncio
async def test_x_api_requires_server_token_without_substituting_market_data() -> None:
    source = XApiSource(None)
    events = await source.fetch(
        [
            MonitorProfile(
                id="elon",
                display_name="Elon Musk",
                handle="elonmusk",
                category="technology",
            )
        ]
    )

    assert events == []
    assert source.last_status["configured"] is False
    assert "Bearer Token" in str(source.last_status["message"])


@pytest.mark.asyncio
async def test_x_api_reads_original_posts_without_delay() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/by"):
            return httpx.Response(
                200,
                request=request,
                json={"data": [{"id": "42", "username": "elonmusk", "name": "Elon Musk"}]},
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "data": [
                    {
                        "id": "123456789",
                        "text": "Tesla update",
                        "created_at": "2026-07-26T12:00:00Z",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = XApiSource("token", base_url="https://api.x.test/2", client=client)
        events = await source.fetch(
            [
                MonitorProfile(
                    id="elon",
                    display_name="Elon Musk",
                    handle="elonmusk",
                    category="technology",
                )
            ]
        )

    assert len(events) == 1
    assert events[0].kind is EventKind.SOCIAL
    assert events[0].available_at == events[0].occurred_at
    assert events[0].url == "https://x.com/elonmusk/status/123456789"
    assert source.last_status["succeeded"] == 1


@pytest.mark.asyncio
async def test_geopolitical_source_filters_official_feed_for_conflict_events() -> None:
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <guid>war-1</guid><title>Missile attack threatens Red Sea shipping</title>
        <description>UN officials report escalating conflict.</description>
        <link>https://news.un.org/example-war</link>
        <pubDate>Sun, 26 Jul 2026 12:00:00 GMT</pubDate>
      </item>
      <item>
        <guid>health-1</guid><title>New health programme launched</title>
        <description>Routine public health update.</description>
        <link>https://news.un.org/example-health</link>
        <pubDate>Sun, 26 Jul 2026 11:00:00 GMT</pubDate>
      </item>
    </channel></rss>"""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, text=rss)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = GeopoliticalRssSource(
            feed_urls=["https://news.un.org/rss"],
            client=client,
        )
        events = await source.fetch([])

    assert len(events) == 1
    assert events[0].kind is EventKind.GEOPOLITICAL
    assert events[0].available_at == events[0].occurred_at
    assert events[0].url == "https://news.un.org/example-war"


@pytest.mark.asyncio
async def test_official_intelligence_isolates_feed_failures_and_labels_source() -> None:
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><item>
      <guid>policy-1</guid><title>Central bank changes interest rate guidance</title>
      <description>Official monetary policy update.</description>
      <link>https://central.example/policy-1</link>
      <pubDate>Sun, 26 Jul 2026 12:00:00 GMT</pubDate>
    </item></channel></rss>"""
    feeds = [
        FeedSpec(
            id="working",
            display_name="Working central bank",
            url="https://working.example/rss",
            profile_id="central-bank-watch",
            profile_name="Global central banks",
            kind=EventKind.NEWS,
            tags=("央行",),
        ),
        FeedSpec(
            id="down",
            display_name="Unavailable regulator",
            url="https://down.example/rss",
            profile_id="regulatory-watch",
            profile_name="Global regulation",
            kind=EventKind.NEWS,
        ),
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "working.example":
            return httpx.Response(200, request=request, text=rss)
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = OfficialIntelligenceRssSource(
            feeds=feeds,
            max_age=timedelta(days=3650),
            client=client,
        )
        events = await source.fetch([])

    assert len(events) == 1
    assert events[0].kind is EventKind.NEWS
    assert "官方来源" in events[0].tags
    assert source.last_status["succeeded"] == 1
    assert source.last_status["failed"] == 1


def test_official_intelligence_includes_white_house_policy_feed() -> None:
    feed = next(spec for spec in DEFAULT_OFFICIAL_FEEDS if spec.id == "white-house-briefings")

    assert feed.url == "https://www.whitehouse.gov/briefings-statements/feed/"
    assert feed.profile_id == "donald-trump"
    assert feed.kind is EventKind.NEWS
    assert "官方政策声明" in feed.tags


@pytest.mark.asyncio
async def test_gdelt_headlines_keep_person_mentions_out_of_person_profiles() -> None:
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Elon Musk comments on Tesla chip exports</title>
        <link>https://publisher.example/musk-tesla</link>
        <pubDate>Sun, 26 Jul 2026 12:00:00 GMT</pubDate>
      </item>
      <item>
        <title>Celebrity fashion awards announced</title>
        <link>https://publisher.example/fashion</link>
        <pubDate>Sun, 26 Jul 2026 12:01:00 GMT</pubDate>
      </item>
    </channel></rss>"""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, text=rss)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = GdeltHeadlinesSource(
            feed_url="https://gdelt.example/feed.rss",
            client=client,
        )
        events = await source.fetch([])

    assert len(events) == 1
    assert events[0].profile_id == "global-headline-watch"
    assert events[0].profile_name == "全球新闻标题"
    assert events[0].kind is EventKind.NEWS
    assert "标题级信号" in events[0].tags
    assert "标题提及：Elon Musk" in events[0].tags
    assert "标题提及：Elon Musk" in events[0].content
    assert source.last_status["succeeded"] == 1


@pytest.mark.asyncio
async def test_gdelt_headlines_merge_syndicated_copies_by_title() -> None:
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Iran conflict threatens Red Sea shipping</title>
        <link>https://publisher-a.example/red-sea</link>
        <pubDate>Sun, 26 Jul 2026 12:00:00 GMT</pubDate>
      </item>
      <item>
        <title>Iran conflict threatens Red Sea shipping!</title>
        <link>https://publisher-b.example/red-sea-copy</link>
        <pubDate>Sun, 26 Jul 2026 12:01:00 GMT</pubDate>
      </item>
      <item>
        <title>Oil supply disruption raises crude oil risk</title>
        <link>https://publisher-c.example/oil</link>
        <pubDate>Sun, 26 Jul 2026 12:02:00 GMT</pubDate>
      </item>
    </channel></rss>"""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, text=rss)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = GdeltHeadlinesSource(
            feed_url="https://gdelt.example/feed.rss",
            client=client,
            max_events=3,
        )
        events = await source.fetch([])

    assert len(events) == 2
    assert sum("Red Sea shipping" in event.title for event in events) == 1
    assert "合并 1 条跨站转载" in str(source.last_status["message"])


@pytest.mark.asyncio
async def test_gdelt_headlines_rejects_historical_and_entertainment_noise() -> None:
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Man discovers Civil War-era cannonballs on his property</title>
        <link>https://publisher.example/history</link>
        <pubDate>Sun, 26 Jul 2026 12:00:00 GMT</pubDate>
      </item>
        <item>
          <title>Iran conflict threatens Red Sea shipping</title>
          <link>https://publisher.example/red-sea</link>
          <pubDate>Sun, 26 Jul 2026 12:01:00 GMT</pubDate>
        </item>
        <item>
          <title>God of War Laufey release date confirmed for a physical disk</title>
          <link>https://publisher.example/game</link>
          <pubDate>Sun, 26 Jul 2026 12:02:00 GMT</pubDate>
        </item>
        <item>
          <title>Crunchyroll adds Romelia War Chronicle to anime lineup</title>
          <link>https://publisher.example/anime</link>
          <pubDate>Sun, 26 Jul 2026 12:03:00 GMT</pubDate>
        </item>
    </channel></rss>"""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, text=rss)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = GdeltHeadlinesSource(
            feed_url="https://gdelt.example/feed.rss",
            client=client,
        )
        events = await source.fetch([])

    assert [event.title for event in events] == ["Iran conflict threatens Red Sea shipping"]


@pytest.mark.asyncio
async def test_ofac_recent_actions_parses_official_sanctions_updates() -> None:
    published = datetime.now(UTC).strftime("%B %d, %Y")
    page = f"""
    <div class="margin-bottom-4 search-result views-row">
      <div><a href="/recent-actions/20260726">Iran-related Designations</a></div>
      <div>{published} -
        <a href="/recent-actions/sanctions-list-updates">Sanctions List Updates</a>
      </div>
    </div>
    """

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, text=page)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = OfacRecentActionsSource(
            url="https://ofac.example/recent-actions",
            client=client,
        )
        events = await source.fetch([])

    assert len(events) == 1
    assert events[0].kind is EventKind.NEWS
    assert events[0].profile_id == "sanctions-watch"
    assert "OFAC" in events[0].tags


@pytest.mark.asyncio
async def test_hkma_press_release_api_reads_recent_official_records() -> None:
    published = datetime.now(UTC).date().isoformat()

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "result": {
                    "records": [
                        {
                            "title": "HKMA announces monetary update",
                            "link": "https://hkma.example/release-1",
                            "date": published,
                        }
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = HkmaPressReleaseSource(
            url="https://hkma.example/api",
            client=client,
        )
        events = await source.fetch([])

    assert len(events) == 1
    assert events[0].kind is EventKind.NEWS
    assert events[0].profile_id == "hkma-watch"
    assert "官方来源" in events[0].tags
