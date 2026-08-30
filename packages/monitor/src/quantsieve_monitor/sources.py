from __future__ import annotations

import asyncio
import calendar
import hashlib
import html
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from quantsieve_providers import DataEnvelope, SECEdgarProvider

from .models import EventKind, MonitorEvent, MonitorProfile

PulseHistoryLoader = Callable[[MonitorProfile, date, date], Awaitable[DataEnvelope]]


@dataclass(frozen=True)
class FeedSpec:
    id: str
    display_name: str
    url: str
    profile_id: str
    profile_name: str
    kind: EventKind
    tags: tuple[str, ...] = ()
    include_keywords: tuple[str, ...] = ()


DEFAULT_OFFICIAL_FEEDS = (
    FeedSpec(
        id="white-house-briefings",
        display_name="The White House",
        url="https://www.whitehouse.gov/briefings-statements/feed/",
        # A White House statement is an official policy source, not a social
        # post.  Showing it in the president's policy watch makes the free
        # monitor useful when the X API is unavailable without ever passing
        # the event off as a post from @realDonaldTrump.
        profile_id="donald-trump",
        profile_name="Donald Trump · 白宫官方政策",
        kind=EventKind.NEWS,
        tags=("White House", "官方政策声明", "美国政策"),
    ),
    FeedSpec(
        id="fed-monetary",
        display_name="Federal Reserve",
        url="https://www.federalreserve.gov/feeds/press_monetary.xml",
        profile_id="central-bank-watch",
        profile_name="全球央行与宏观政策",
        kind=EventKind.NEWS,
        tags=("美联储", "货币政策"),
    ),
    FeedSpec(
        id="fed-speeches",
        display_name="Federal Reserve speeches",
        url="https://www.federalreserve.gov/feeds/speeches.xml",
        profile_id="central-bank-watch",
        profile_name="全球央行与宏观政策",
        kind=EventKind.NEWS,
        tags=("美联储", "央行讲话"),
    ),
    FeedSpec(
        id="ecb-press",
        display_name="European Central Bank",
        url="https://www.ecb.europa.eu/rss/press.html",
        profile_id="central-bank-watch",
        profile_name="全球央行与宏观政策",
        kind=EventKind.NEWS,
        tags=("ECB", "欧元区"),
    ),
    FeedSpec(
        id="boe-news",
        display_name="Bank of England",
        url="https://www.bankofengland.co.uk/rss/news",
        profile_id="central-bank-watch",
        profile_name="全球央行与宏观政策",
        kind=EventKind.NEWS,
        tags=("英国央行", "英镑"),
    ),
    FeedSpec(
        id="boj-news",
        display_name="Bank of Japan",
        url="https://www.boj.or.jp/en/rss/whatsnew.xml",
        profile_id="central-bank-watch",
        profile_name="全球央行与宏观政策",
        kind=EventKind.NEWS,
        tags=("日本央行", "日元"),
    ),
    FeedSpec(
        id="rba-releases",
        display_name="Reserve Bank of Australia",
        url="https://www.rba.gov.au/rss/rss-cb-media-releases.xml",
        profile_id="central-bank-watch",
        profile_name="全球央行与宏观政策",
        kind=EventKind.NEWS,
        tags=("澳洲联储", "澳元"),
    ),
    FeedSpec(
        id="bis-press",
        display_name="Bank for International Settlements",
        url="https://www.bis.org/doclist/all_pressrels.rss",
        profile_id="central-bank-watch",
        profile_name="全球央行与宏观政策",
        kind=EventKind.NEWS,
        tags=("BIS", "金融稳定"),
    ),
    FeedSpec(
        id="sec-press",
        display_name="U.S. SEC",
        url="https://www.sec.gov/news/pressreleases.rss",
        profile_id="regulatory-watch",
        profile_name="全球金融监管",
        kind=EventKind.NEWS,
        tags=("SEC", "证券监管"),
    ),
    FeedSpec(
        id="cftc-press",
        display_name="U.S. CFTC",
        url="https://www.cftc.gov/RSS/RSSGP/rssgp.xml",
        profile_id="regulatory-watch",
        profile_name="全球金融监管",
        kind=EventKind.NEWS,
        tags=("CFTC", "期货与加密监管"),
    ),
    FeedSpec(
        id="eia-today",
        display_name="U.S. Energy Information Administration",
        url="https://www.eia.gov/rss/todayinenergy.xml",
        profile_id="energy-watch",
        profile_name="能源与大宗商品",
        kind=EventKind.NEWS,
        tags=("EIA", "能源"),
    ),
    FeedSpec(
        id="eia-press",
        display_name="U.S. EIA press releases",
        url="https://www.eia.gov/rss/press_rss.xml",
        profile_id="energy-watch",
        profile_name="能源与大宗商品",
        kind=EventKind.NEWS,
        tags=("EIA", "油气供需"),
    ),
    FeedSpec(
        id="aramco-news",
        display_name="Saudi Aramco",
        url="https://www.aramco.com/api/v1/com/rss/news?sc_lang=en",
        profile_id="energy-watch",
        profile_name="能源与大宗商品",
        kind=EventKind.NEWS,
        tags=("Saudi Aramco", "原油"),
    ),
    FeedSpec(
        id="gdacs-earthquakes",
        display_name="GDACS earthquakes",
        url="https://www.gdacs.org/xml/rss_eq_48h_med.xml",
        profile_id="disaster-watch",
        profile_name="全球重大灾害",
        kind=EventKind.GEOPOLITICAL,
        tags=("GDACS", "地震"),
    ),
    FeedSpec(
        id="gdacs-cyclones",
        display_name="GDACS tropical cyclones",
        url="https://www.gdacs.org/xml/rss_tc_7d.xml",
        profile_id="disaster-watch",
        profile_name="全球重大灾害",
        kind=EventKind.GEOPOLITICAL,
        tags=("GDACS", "热带气旋"),
    ),
    FeedSpec(
        id="gdacs-floods",
        display_name="GDACS floods",
        url="https://www.gdacs.org/xml/rss_fl_7d.xml",
        profile_id="disaster-watch",
        profile_name="全球重大灾害",
        kind=EventKind.GEOPOLITICAL,
        tags=("GDACS", "洪水"),
    ),
)


class PublicRssSource:
    """Best-effort reader for public RSS mirrors; failure is isolated per mirror/profile."""

    name = "public-rss"

    def __init__(
        self,
        base_urls: list[str] | None = None,
        delay: timedelta = timedelta(0),
        poll_interval: timedelta = timedelta(minutes=5),
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_urls = base_urls or [
            "https://nitter.poast.org",
            "https://nitter.privacydev.net",
        ]
        self.delay = delay
        self.poll_interval = poll_interval
        self._client = client
        self._next_fetch_at: datetime | None = None
        self.last_status: dict[str, object] = {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "message": "等待连接免费人物公开 RSS 镜像。",
        }

    async def fetch(self, profiles: list[MonitorProfile]) -> list[MonitorEvent]:
        social_profiles = [profile for profile in profiles if profile.handle]
        now = datetime.now(UTC)
        if self._next_fetch_at and now < self._next_fetch_at:
            connected = self.last_status.get("succeeded", 0)
            self.last_status["message"] = (
                f"人物公开 RSS 镜像已尝试连接（上轮成功 {connected} 个对象）；"
                "当前处于 5 分钟节流窗口。"
            )
            return []
        self._next_fetch_at = now + self.poll_interval
        tasks = [self._fetch_profile(profile) for profile in social_profiles]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        events: list[MonitorEvent] = []
        succeeded = 0
        for result in results:
            if isinstance(result, list):
                succeeded += 1
                events.extend(result)
        self.last_status = {
            "attempted": len(social_profiles),
            "succeeded": succeeded,
            "failed": len(social_profiles) - succeeded,
            "message": (
                f"免费人物公开 RSS 本轮成功 {succeeded}/{len(social_profiles)}；"
                "该层是非官方聚合，必须打开原始链接复核。"
            ),
        }
        return events

    async def _fetch_profile(self, profile: MonitorProfile) -> list[MonitorEvent]:
        last_error: Exception | None = None
        for base_url in self.base_urls:
            url = f"{base_url.rstrip('/')}/{profile.handle}/rss"
            client = self._client or httpx.AsyncClient(timeout=8, follow_redirects=True)
            try:
                response = await client.get(url, headers={"User-Agent": "QuantSieve/0.1"})
                response.raise_for_status()
                parsed = feedparser.parse(response.content)
                return [self._entry_to_event(profile, entry) for entry in parsed.entries[:10]]
            except Exception as exc:
                last_error = exc
            finally:
                if self._client is None:
                    await client.aclose()
        if last_error:
            raise last_error
        return []

    def _entry_to_event(self, profile: MonitorProfile, entry: Any) -> MonitorEvent:
        published = getattr(entry, "published", None)
        occurred_at = (
            parsedate_to_datetime(published).astimezone(UTC) if published else datetime.now(UTC)
        )
        link = str(getattr(entry, "link", ""))
        content = str(getattr(entry, "summary", getattr(entry, "title", "")))
        source_id = str(
            getattr(
                entry,
                "id",
                hashlib.sha256(f"{profile.id}:{link}:{content}".encode()).hexdigest(),
            )
        )
        return MonitorEvent.delayed(
            source=self.name,
            source_id=source_id,
            profile_id=profile.id,
            profile_name=profile.display_name,
            kind=EventKind.SOCIAL,
            title=str(getattr(entry, "title", profile.display_name)),
            content=content,
            url=link,
            occurred_at=occurred_at,
            delay=self.delay,
            tags=[profile.category, *profile.tags],
        )


class OfficialIntelligenceRssSource:
    """Low-cost official policy, regulation, energy and disaster intelligence feeds."""

    name = "official-intel"

    def __init__(
        self,
        feeds: tuple[FeedSpec, ...] | list[FeedSpec] | None = None,
        *,
        max_age: timedelta = timedelta(days=7),
        poll_interval: timedelta = timedelta(minutes=5),
        max_entries_per_feed: int = 6,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.feeds = tuple(feeds or DEFAULT_OFFICIAL_FEEDS)
        self.max_age = max_age
        self.poll_interval = poll_interval
        self.max_entries_per_feed = max_entries_per_feed
        self._client = client
        self._next_fetch_at: datetime | None = None
        self.last_status: dict[str, object] = {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "message": f"等待连接 {len(self.feeds)} 个免费官方信息源。",
        }

    async def fetch(self, profiles: list[MonitorProfile]) -> list[MonitorEvent]:
        del profiles
        now = datetime.now(UTC)
        if self._next_fetch_at and now < self._next_fetch_at:
            previous_succeeded = self.last_status.get("succeeded", 0)
            connected_count = (
                previous_succeeded if isinstance(previous_succeeded, int) else 0
            )
            self.last_status["message"] = (
                f"{connected_count}/{len(self.feeds)} 个免费官方源已连接；"
                "当前处于 5 分钟节流窗口。"
            )
            return []

        self._next_fetch_at = now + self.poll_interval
        results = await asyncio.gather(
            *(self._fetch_feed(spec, now) for spec in self.feeds),
            return_exceptions=True,
        )
        events: list[MonitorEvent] = []
        connected: list[str] = []
        failed: list[str] = []
        for spec, result in zip(self.feeds, results, strict=True):
            if isinstance(result, list):
                connected.append(spec.display_name)
                events.extend(result)
            else:
                failed.append(spec.display_name)
        self.last_status = {
            "attempted": len(self.feeds),
            "succeeded": len(connected),
            "failed": len(failed),
            "providers": connected,
            "failed_providers": failed,
            "message": (
                f"{len(connected)}/{len(self.feeds)} 个免费官方源已连接，"
                f"本轮读取 {len(events)} 条政策、监管、能源与灾害快讯。"
            ),
        }
        return events

    async def _fetch_feed(self, spec: FeedSpec, now: datetime) -> list[MonitorEvent]:
        client = self._client or httpx.AsyncClient(timeout=30, follow_redirects=True)
        try:
            response = await client.get(
                spec.url,
                headers={"User-Agent": "QuantSieve/0.3 contact@example.com"},
            )
            response.raise_for_status()
            if len(response.content) > 5_000_000:
                raise ValueError(f"Feed is unexpectedly large: {spec.id}")
            parsed = feedparser.parse(response.content)
            events: list[MonitorEvent] = []
            for entry in parsed.entries[:50]:
                occurred_at = _entry_datetime(entry, now)
                if occurred_at < now - self.max_age:
                    continue
                text = " ".join(
                    (
                        str(getattr(entry, "title", "")),
                        str(getattr(entry, "summary", "")),
                    )
                ).lower()
                if spec.include_keywords and not any(
                    keyword.lower() in text for keyword in spec.include_keywords
                ):
                    continue
                events.append(self._entry_to_event(spec, entry, occurred_at))
                if len(events) >= self.max_entries_per_feed:
                    break
            return events
        finally:
            if self._client is None:
                await client.aclose()

    def _entry_to_event(
        self,
        spec: FeedSpec,
        entry: Any,
        occurred_at: datetime,
    ) -> MonitorEvent:
        title = _plain_text(str(getattr(entry, "title", spec.display_name)))
        content = _plain_text(
            str(getattr(entry, "summary", getattr(entry, "description", title)))
        )
        if not content:
            content = title
        link = urljoin(spec.url, str(getattr(entry, "link", "")))
        raw_id = str(getattr(entry, "id", link or title))
        source_id = hashlib.sha256(f"{spec.id}:{raw_id}".encode()).hexdigest()
        return MonitorEvent(
            source=self.name,
            source_id=source_id,
            profile_id=spec.profile_id,
            profile_name=spec.profile_name,
            kind=spec.kind,
            title=title,
            content=content[:1600],
            url=link,
            occurred_at=occurred_at,
            available_at=occurred_at,
            tags=[spec.display_name, "官方来源", *spec.tags],
        )


class GdeltHeadlinesSource:
    """Broad global headline firehose, filtered locally to avoid paid news APIs."""

    name = "gdelt-headlines"

    def __init__(
        self,
        feed_url: str = "http://data.gdeltproject.org/gdeltv3/gal/feed.rss",
        *,
        poll_interval: timedelta = timedelta(minutes=10),
        max_events: int = 30,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.feed_url = feed_url
        self.poll_interval = poll_interval
        self.max_events = max_events
        self._client = client
        self._next_fetch_at: datetime | None = None
        self.last_status: dict[str, object] = {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "message": "等待连接 GDELT 全球新闻标题流。",
        }

    async def fetch(self, profiles: list[MonitorProfile]) -> list[MonitorEvent]:
        del profiles
        now = datetime.now(UTC)
        if self._next_fetch_at and now < self._next_fetch_at:
            self.last_status["message"] = "GDELT 已连接；当前处于 10 分钟节流窗口。"
            return []

        self._next_fetch_at = now + self.poll_interval
        client = self._client or httpx.AsyncClient(timeout=45, follow_redirects=True)
        try:
            response = await client.get(
                self.feed_url,
                headers={"User-Agent": "QuantSieve/0.3"},
            )
            response.raise_for_status()
            if len(response.content) > 5_000_000:
                raise ValueError("GDELT headline feed is unexpectedly large.")
            parsed = feedparser.parse(response.content)
            candidates: list[tuple[int, datetime, MonitorEvent]] = []
            seen_urls: set[str] = set()
            for entry in parsed.entries:
                title = _plain_text(str(getattr(entry, "title", "")))
                score = _headline_score(title)
                if score <= 0:
                    continue
                url = str(getattr(entry, "link", ""))
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                occurred_at = _entry_datetime(entry, now)
                candidates.append(
                    (score, occurred_at, self._entry_to_event(entry, title, url, occurred_at))
                )
            candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
            unique_candidates: list[tuple[int, datetime, MonitorEvent]] = []
            seen_headlines: set[str] = set()
            for candidate in candidates:
                headline_key = _headline_identity(candidate[2].title)
                if headline_key in seen_headlines:
                    continue
                seen_headlines.add(headline_key)
                unique_candidates.append(candidate)
            events = [item[2] for item in unique_candidates[: self.max_events]]
            syndicated_duplicates = len(candidates) - len(unique_candidates)
            self.last_status = {
                "attempted": 1,
                "succeeded": 1,
                "failed": 0,
                "message": (
                    "GDELT 全球新闻标题流已连接；"
                    f"本轮从 {len(parsed.entries)} 条标题中筛出 {len(events)} 条去重后的市场信号"
                    f"，合并 {syndicated_duplicates} 条跨站转载。"
                ),
            }
            return events
        except Exception as exc:
            self.last_status = {
                "attempted": 1,
                "succeeded": 0,
                "failed": 1,
                "message": f"GDELT 标题流暂不可用：{type(exc).__name__}",
            }
            return []
        finally:
            if self._client is None:
                await client.aclose()

    def _entry_to_event(
        self,
        entry: Any,
        title: str,
        url: str,
        occurred_at: datetime,
    ) -> MonitorEvent:
        del entry
        mentions = _headline_mentions(title)
        kind = (
            EventKind.GEOPOLITICAL
            if _contains_any(title.lower(), _HEADLINE_GEOPOLITICAL_TERMS)
            else EventKind.NEWS
        )
        domain = urlparse(url).netloc.removeprefix("www.") or "unknown publisher"
        source_id = hashlib.sha256(url.encode()).hexdigest()
        return MonitorEvent(
            source=self.name,
            source_id=source_id,
            # A headline mentioning a person is not an original statement from
            # that person. Keep GDELT material out of person profiles so it
            # cannot be mistaken for X activity or a verified quote.
            profile_id="global-headline-watch",
            profile_name="全球新闻标题",
            kind=kind,
            title=title,
            content=(
                f"GDELT 在全球公开新闻流中捕获了这条标题，发布域名为 {domain}。"
                + (f"标题提及：{'、'.join(mentions)}。" if mentions else "")
                + "这是标题级聚合信号，请打开原始报道核验全文与上下文。"
            ),
            url=url,
            occurred_at=occurred_at,
            available_at=occurred_at,
            tags=[
                "GDELT",
                "全球新闻聚合",
                "标题级信号",
                domain,
                *(f"标题提及：{name}" for name in mentions),
            ],
        )


class OfacRecentActionsSource:
    """Official U.S. Treasury sanctions actions published without an RSS feed."""

    name = "ofac-actions"

    def __init__(
        self,
        url: str = "https://ofac.treasury.gov/recent-actions",
        *,
        poll_interval: timedelta = timedelta(minutes=5),
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.poll_interval = poll_interval
        self._client = client
        self._next_fetch_at: datetime | None = None
        self.last_status: dict[str, object] = {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "message": "等待连接美国财政部 OFAC 制裁动态。",
        }

    async def fetch(self, profiles: list[MonitorProfile]) -> list[MonitorEvent]:
        del profiles
        now = datetime.now(UTC)
        if self._next_fetch_at and now < self._next_fetch_at:
            self.last_status["message"] = "OFAC 官方制裁动态已连接；当前处于节流窗口。"
            return []
        self._next_fetch_at = now + self.poll_interval
        client = self._client or httpx.AsyncClient(timeout=30, follow_redirects=True)
        try:
            response = await client.get(
                self.url,
                headers={"User-Agent": "QuantSieve/0.3 contact@example.com"},
            )
            response.raise_for_status()
            events = self._parse(response.text, now)
            self.last_status = {
                "attempted": 1,
                "succeeded": 1,
                "failed": 0,
                "message": f"OFAC 官方制裁动态已连接，本轮读取 {len(events)} 条。",
            }
            return events
        except Exception as exc:
            self.last_status = {
                "attempted": 1,
                "succeeded": 0,
                "failed": 1,
                "message": f"OFAC 官方页面暂不可用：{type(exc).__name__}",
            }
            return []
        finally:
            if self._client is None:
                await client.aclose()

    def _parse(self, page: str, now: datetime) -> list[MonitorEvent]:
        pattern = re.compile(
            r'<a href="(?P<href>/recent-actions/\d{8})"[^>]*>'
            r"(?P<title>.*?)</a>.*?"
            r"(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})\s*-\s*"
            r'<a href="[^"]+">(?P<category>.*?)</a>',
            flags=re.DOTALL,
        )
        events: list[MonitorEvent] = []
        for match in pattern.finditer(page):
            occurred_at = datetime.strptime(
                match.group("date"), "%B %d, %Y"
            ).replace(tzinfo=UTC)
            if occurred_at < now - timedelta(days=14):
                continue
            title = _plain_text(match.group("title"))
            category = _plain_text(match.group("category"))
            href = match.group("href")
            events.append(
                MonitorEvent(
                    source=self.name,
                    source_id=href.rsplit("/", 1)[-1],
                    profile_id="sanctions-watch",
                    profile_name="全球制裁与金融限制",
                    kind=EventKind.NEWS,
                    title=title,
                    content=f"美国财政部 OFAC 发布新的 {category} 动态。",
                    url=urljoin(self.url, href),
                    occurred_at=occurred_at,
                    available_at=occurred_at,
                    tags=["OFAC", "制裁", "美国财政部", "官方来源"],
                )
            )
        return events[:10]


class HkmaPressReleaseSource:
    """Official Hong Kong Monetary Authority press-release API."""

    name = "hkma-press"

    def __init__(
        self,
        url: str = "https://api.hkma.gov.hk/public/press-releases?lang=en&offset=0",
        *,
        poll_interval: timedelta = timedelta(minutes=5),
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.poll_interval = poll_interval
        self._client = client
        self._next_fetch_at: datetime | None = None
        self.last_status: dict[str, object] = {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "message": "等待连接香港金管局公开 API。",
        }

    async def fetch(self, profiles: list[MonitorProfile]) -> list[MonitorEvent]:
        del profiles
        now = datetime.now(UTC)
        if self._next_fetch_at and now < self._next_fetch_at:
            self.last_status["message"] = "香港金管局公开 API 已连接；当前处于节流窗口。"
            return []
        self._next_fetch_at = now + self.poll_interval
        client = self._client or httpx.AsyncClient(timeout=20, follow_redirects=True)
        try:
            response = await client.get(
                self.url,
                headers={"User-Agent": "QuantSieve/0.3"},
            )
            response.raise_for_status()
            records = response.json().get("result", {}).get("records", [])
            events = [
                self._record_to_event(record)
                for record in records[:20]
                if _parse_datetime(record["date"]) >= now - timedelta(days=7)
            ]
            self.last_status = {
                "attempted": 1,
                "succeeded": 1,
                "failed": 0,
                "message": f"香港金管局公开 API 已连接，本轮读取 {len(events)} 条。",
            }
            return events
        except Exception as exc:
            self.last_status = {
                "attempted": 1,
                "succeeded": 0,
                "failed": 1,
                "message": f"香港金管局 API 暂不可用：{type(exc).__name__}",
            }
            return []
        finally:
            if self._client is None:
                await client.aclose()

    def _record_to_event(self, record: dict[str, Any]) -> MonitorEvent:
        url = str(record["link"])
        occurred_at = _parse_datetime(record["date"])
        title = _plain_text(str(record["title"]))
        return MonitorEvent(
            source=self.name,
            source_id=hashlib.sha256(url.encode()).hexdigest(),
            profile_id="hkma-watch",
            profile_name="香港金融与人民币市场",
            kind=EventKind.NEWS,
            title=title,
            content="香港金融管理局发布新的官方新闻稿。",
            url=url,
            occurred_at=occurred_at,
            available_at=occurred_at,
            tags=["HKMA", "香港金管局", "人民币与港元", "官方来源"],
        )


class XApiSource:
    """Official X API timeline reader with per-profile since_id deduplication."""

    name = "x-api"

    def __init__(
        self,
        bearer_token: str | None,
        *,
        base_url: str = "https://api.x.com/2",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.bearer_token = bearer_token
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._user_ids: dict[str, str] = {}
        self._since_ids: dict[str, str] = {}
        self.last_status: dict[str, object] = {
            "configured": bool(bearer_token),
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "message": (
                "已配置 X 官方 API。"
                if bearer_token
                else "未配置 QUANTSIEVE_X_BEARER_TOKEN，人物原文不会用行情替代。"
            ),
        }

    async def fetch(self, profiles: list[MonitorProfile]) -> list[MonitorEvent]:
        social_profiles = [profile for profile in profiles if profile.handle]
        if not self.bearer_token:
            self.last_status = {
                "configured": False,
                "attempted": len(social_profiles),
                "succeeded": 0,
                "failed": len(social_profiles),
                "message": "缺少 X Bearer Token；已停止伪人物信号，只保留明确标注的其他来源。",
            }
            return []

        client = self._client or httpx.AsyncClient(timeout=20, follow_redirects=True)
        try:
            await self._resolve_users(client, social_profiles)
            results = await asyncio.gather(
                *(self._fetch_profile(client, profile) for profile in social_profiles),
                return_exceptions=True,
            )
            events: list[MonitorEvent] = []
            succeeded = 0
            for result in results:
                if isinstance(result, list):
                    succeeded += 1
                    events.extend(result)
            self.last_status = {
                "configured": True,
                "attempted": len(social_profiles),
                "succeeded": succeeded,
                "failed": len(social_profiles) - succeeded,
                "message": (
                    f"X 官方 API 已连接，本轮读取 {len(events)} 条新原文。"
                    if succeeded
                    else "X 官方 API 请求失败，请检查套餐、权限或网络。"
                ),
            }
            return events
        except Exception as exc:
            self.last_status = {
                "configured": True,
                "attempted": len(social_profiles),
                "succeeded": 0,
                "failed": len(social_profiles),
                "message": f"X 官方 API 不可用：{type(exc).__name__}",
            }
            return []
        finally:
            if self._client is None:
                await client.aclose()

    async def _resolve_users(
        self,
        client: httpx.AsyncClient,
        profiles: list[MonitorProfile],
    ) -> None:
        missing = [
            profile.handle
            for profile in profiles
            if profile.handle and profile.handle.lower() not in self._user_ids
        ]
        if not missing:
            return
        response = await client.get(
            f"{self.base_url}/users/by",
            headers=self._headers,
            params={"usernames": ",".join(missing), "user.fields": "id,username,name"},
        )
        response.raise_for_status()
        for user in response.json().get("data", []):
            self._user_ids[str(user["username"]).lower()] = str(user["id"])

    async def _fetch_profile(
        self,
        client: httpx.AsyncClient,
        profile: MonitorProfile,
    ) -> list[MonitorEvent]:
        handle = profile.handle or ""
        user_id = self._user_ids.get(handle.lower())
        if not user_id:
            raise LookupError(f"X user not found: {handle}")
        params: dict[str, str | int] = {
            "max_results": 10,
            "tweet.fields": "created_at,lang,public_metrics",
            "exclude": "replies,retweets",
        }
        since_id = self._since_ids.get(profile.id)
        if since_id:
            params["since_id"] = since_id
        response = await client.get(
            f"{self.base_url}/users/{user_id}/tweets",
            headers=self._headers,
            params=params,
        )
        response.raise_for_status()
        posts = response.json().get("data", [])
        if posts:
            self._since_ids[profile.id] = max(
                (str(post["id"]) for post in posts),
                key=int,
            )
        return [self._post_to_event(profile, post) for post in posts]

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bearer_token}",
            "User-Agent": "QuantSieve/0.2",
        }

    def _post_to_event(
        self,
        profile: MonitorProfile,
        post: dict[str, Any],
    ) -> MonitorEvent:
        post_id = str(post["id"])
        content = str(post.get("text", "")).strip()
        occurred_at = _parse_datetime(post.get("created_at", datetime.now(UTC)))
        handle = profile.handle or ""
        return MonitorEvent(
            source=self.name,
            source_id=post_id,
            profile_id=profile.id,
            profile_name=profile.display_name,
            kind=EventKind.SOCIAL,
            title=f"@{handle} 发布新动态",
            content=content,
            url=f"https://x.com/{handle}/status/{post_id}",
            occurred_at=occurred_at,
            available_at=occurred_at,
            tags=["X 原文", profile.category, *profile.tags],
        )


class GeopoliticalRssSource:
    """Official conflict/geopolitical RSS reader; non-matching general news is excluded."""

    name = "un-news"

    def __init__(
        self,
        feed_urls: list[str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.feed_urls = feed_urls or [
            "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
        ]
        self._client = client
        self.last_status: dict[str, object] = {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "message": "尚未刷新。",
        }

    async def fetch(self, profiles: list[MonitorProfile]) -> list[MonitorEvent]:
        del profiles
        results = await asyncio.gather(
            *(self._fetch_feed(url) for url in self.feed_urls),
            return_exceptions=True,
        )
        events: list[MonitorEvent] = []
        succeeded = 0
        for result in results:
            if isinstance(result, list):
                succeeded += 1
                events.extend(result)
        self.last_status = {
            "attempted": len(self.feed_urls),
            "succeeded": succeeded,
            "failed": len(self.feed_urls) - succeeded,
            "message": (
                f"联合国公开源已连接，本轮匹配 {len(events)} 条地缘事件。"
                if succeeded
                else "联合国公开源暂时不可用。"
            ),
        }
        return events

    async def _fetch_feed(self, url: str) -> list[MonitorEvent]:
        client = self._client or httpx.AsyncClient(timeout=20, follow_redirects=True)
        try:
            response = await client.get(url, headers={"User-Agent": "QuantSieve/0.2"})
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
            return [
                self._entry_to_event(entry)
                for entry in parsed.entries[:60]
                if self._is_geopolitical(entry)
            ]
        finally:
            if self._client is None:
                await client.aclose()

    def _is_geopolitical(self, entry: Any) -> bool:
        text = " ".join(
            (
                str(getattr(entry, "title", "")),
                str(getattr(entry, "summary", "")),
                " ".join(
                    str(getattr(tag, "term", ""))
                    for tag in getattr(entry, "tags", [])
                ),
            )
        ).lower()
        return any(keyword in text for keyword in _GEOPOLITICAL_KEYWORDS)

    def _entry_to_event(self, entry: Any) -> MonitorEvent:
        published = getattr(entry, "published", None)
        occurred_at = (
            parsedate_to_datetime(published).astimezone(UTC)
            if published
            else datetime.now(UTC)
        )
        link = str(getattr(entry, "link", ""))
        title = _plain_text(str(getattr(entry, "title", "Geopolitical event")))
        content = _plain_text(str(getattr(entry, "summary", title)))
        source_id = str(
            getattr(
                entry,
                "id",
                hashlib.sha256(f"{link}:{title}".encode()).hexdigest(),
            )
        )
        return MonitorEvent(
            source=self.name,
            source_id=source_id,
            profile_id="geopolitical-watch",
            profile_name="全球地缘事件",
            kind=EventKind.GEOPOLITICAL,
            title=title,
            content=content,
            url=link,
            occurred_at=occurred_at,
            available_at=occurred_at,
            tags=["联合国", "战争与地缘", "公开来源"],
        )


class Sec13FSource:
    name = "sec-edgar"

    def __init__(self, provider: SECEdgarProvider) -> None:
        self.provider = provider
        self.last_status = {"attempted": 0, "succeeded": 0, "failed": 0}

    async def fetch(self, profiles: list[MonitorProfile]) -> list[MonitorEvent]:
        filing_profiles = [profile for profile in profiles if profile.cik]
        tasks = [self._fetch_profile(profile) for profile in filing_profiles]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        events = [event for event in results if isinstance(event, MonitorEvent)]
        self.last_status = {
            "attempted": len(filing_profiles),
            "succeeded": len(events),
            "failed": len(filing_profiles) - len(events),
        }
        return events

    async def _fetch_profile(self, profile: MonitorProfile) -> MonitorEvent:
        filing = await self.provider.latest_13f(profile.cik or "", profile.display_name)
        accession = str(filing.metadata["accession_number"])
        filed_at = datetime.fromisoformat(f"{filing.metadata['filing_date']}T00:00:00+00:00")
        total_value = sum(int(row.get("value_usd", 0)) for row in filing.rows)
        citation = filing.citations[0]
        return MonitorEvent.delayed(
            source=self.name,
            source_id=accession,
            profile_id=profile.id,
            profile_name=profile.display_name,
            kind=EventKind.FILING,
            title=f"{profile.display_name} filed a new 13F",
            content=(
                f"Official filing contains {len(filing.rows)} disclosed positions "
                f"with aggregate reported value USD {total_value}."
            ),
            url=citation.url or "https://www.sec.gov/edgar/search/",
            occurred_at=filed_at,
            delay=timedelta(minutes=30),
            tags=["13F", "SEC", *profile.tags],
        )


class LinkedMarketPulseSource:
    """Real delayed price pulses for an entity's explicitly labelled related asset."""

    name = "linked-market-data"

    def __init__(
        self,
        history_loader: PulseHistoryLoader,
        delay: timedelta = timedelta(minutes=30),
        poll_interval: timedelta = timedelta(minutes=5),
    ) -> None:
        self.history_loader = history_loader
        self.delay = delay
        self.poll_interval = poll_interval
        self._next_fetch_at: datetime | None = None
        self.last_status: dict[str, object] = {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "message": "等待读取关联资产真实行情。",
        }

    async def fetch(self, profiles: list[MonitorProfile]) -> list[MonitorEvent]:
        pulse_profiles = [profile for profile in profiles if profile.pulse_symbol]
        now = datetime.now(UTC)
        if self._next_fetch_at and now < self._next_fetch_at:
            self.last_status["message"] = (
                f"关联行情已连接；当前处于 {int(self.poll_interval.total_seconds() / 60)} "
                "分钟节流窗口。"
            )
            return []
        self._next_fetch_at = now + self.poll_interval
        results = await asyncio.gather(
            *(self._fetch_profile(profile) for profile in pulse_profiles),
            return_exceptions=True,
        )
        events = [event for event in results if isinstance(event, MonitorEvent)]
        self.last_status = {
            "attempted": len(pulse_profiles),
            "succeeded": len(events),
            "failed": len(pulse_profiles) - len(events),
            "message": (
                f"{len(events)}/{len(pulse_profiles)} 个关联资产行情已读取；"
                "行情只作佐证，不代表人物发言或持仓。"
            ),
        }
        return events

    async def _fetch_profile(self, profile: MonitorProfile) -> MonitorEvent:
        end = date.today()
        envelope = await self.history_loader(profile, end - timedelta(days=14), end)
        rows = sorted(
            (
                row
                for row in envelope.rows
                if row.get("date") is not None and row.get("close") is not None
            ),
            key=lambda row: _parse_datetime(row["date"]),
        )
        if len(rows) < 2:
            raise LookupError(f"Not enough market rows for {profile.pulse_symbol}.")
        latest = rows[-1]
        previous = rows[-2]
        weekly_start = rows[-6] if len(rows) >= 6 else rows[0]
        latest_close = float(latest["close"])
        previous_close = float(previous["close"])
        weekly_close = float(weekly_start["close"])
        daily_return = latest_close / previous_close - 1 if previous_close else 0.0
        weekly_return = latest_close / weekly_close - 1 if weekly_close else 0.0
        occurred_at = _parse_datetime(latest["date"])
        citation = envelope.citations[0] if envelope.citations else None
        context = profile.pulse_context or "关联市场"
        symbol = profile.pulse_symbol or envelope.symbol
        return MonitorEvent.delayed(
            source=self.name,
            source_id=f"{profile.id}:{symbol}:{occurred_at.date().isoformat()}",
            profile_id=profile.id,
            profile_name=profile.display_name,
            kind=EventKind.MARKET,
            title=f"{profile.display_name} · {symbol} 行情脉搏",
            content=(
                f"{context} {symbol} 最新收盘 {_number(latest_close)}，"
                f"单日 {_percent(daily_return)}，近 5 个数据周期 {_percent(weekly_return)}。"
            ),
            url=citation.url if citation and citation.url else "",
            occurred_at=occurred_at,
            delay=self.delay,
            analysis="这是关联资产的真实行情变化，不代表该人物发言、持仓变化或投资建议。",
            analysis_method="rules",
            tags=["真实行情", context, symbol, *profile.tags],
        )


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _entry_datetime(entry: Any, fallback: datetime) -> datetime:
    for attribute in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, attribute, None)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
    for attribute in ("published", "updated", "created"):
        value = getattr(entry, attribute, None)
        if not value:
            continue
        try:
            return parsedate_to_datetime(str(value)).astimezone(UTC)
        except (TypeError, ValueError):
            try:
                return _parse_datetime(value)
            except (TypeError, ValueError):
                continue
    return fallback


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(
        _matches_phrase(text, term)
        for term in terms
    )


def _matches_phrase(text: str, phrase: str) -> bool:
    if phrase.isascii():
        return re.search(
            rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])",
            text,
        ) is not None
    return phrase in text


def _headline_score(title: str) -> int:
    lowered = title.lower()
    if _contains_any(lowered, _HEADLINE_EXCLUSION_TERMS):
        return 0
    score = 0
    for weight, terms in _HEADLINE_WEIGHTED_TERMS:
        if _contains_any(lowered, terms):
            score += weight
    return score


def _headline_mentions(title: str) -> tuple[str, ...]:
    lowered = title.lower()
    mentions: list[str] = []
    for terms, _profile_id, profile_name in _HEADLINE_PROFILES:
        if _contains_any(lowered, terms):
            mentions.append(profile_name)
    return tuple(mentions)


def _headline_identity(title: str) -> str:
    """Collapse obvious syndication suffixes without erasing headline meaning.

    GDELT often appends a publisher after ``-`` or ``|`` while retaining an
    otherwise identical title.  Strip that suffix only when it looks like a
    publication label (for example ``- AOL`` or ``- Daily Sitka Sentinel``),
    never merely because a headline contains a dash.
    """
    canonical = title.strip()
    pieces = re.split(r"\s+(?:[-–—|])\s+", canonical)
    if len(pieces) > 1 and _looks_like_publisher_suffix(pieces[-1]):
        canonical = " ".join(pieces[:-1])
    return re.sub(r"[^\w]+", "", canonical.casefold(), flags=re.UNICODE)


def _looks_like_publisher_suffix(value: str) -> bool:
    normalized = value.casefold().strip()
    if not normalized or len(normalized) > 80:
        return False
    publisher_cues = (
        ".com",
        ".org",
        ".net",
        "news",
        "herald",
        "sentinel",
        "times",
        "journal",
        "tribune",
        "post",
        "radio",
        "aol",
        "wnyc",
    )
    return any(cue in normalized for cue in publisher_cues)


def _number(value: float) -> str:
    if abs(value) >= 1_000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _percent(value: float) -> str:
    return f"{value * 100:+.2f}%"


def _plain_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


_GEOPOLITICAL_KEYWORDS = (
    "war",
    "conflict",
    "attack",
    "airstrike",
    "missile",
    "military",
    "ceasefire",
    "sanction",
    "blockade",
    "invasion",
    "occupation",
    "hostilities",
    "ukraine",
    "gaza",
    "israel",
    "iran",
    "russia",
    "red sea",
    "strait of hormuz",
    "sudan",
    "yemen",
)

_HEADLINE_PROFILES = (
    (("elon musk", "马斯克"), "elon-musk", "Elon Musk"),
    (("donald trump", "president trump", "特朗普"), "donald-trump", "Donald Trump"),
    (("jerome powell", "鲍威尔"), "jerome-powell", "Jerome Powell"),
    (("scott bessent", "贝森特"), "scott-bessent", "Scott Bessent"),
    (("xi jinping", "习近平"), "xi-jinping", "Xi Jinping"),
    (("vladimir putin", "president putin", "普京"), "vladimir-putin", "Vladimir Putin"),
    (("volodymyr zelensky", "zelenskyy", "泽连斯基"), "zelenskyy", "Volodymyr Zelenskyy"),
    (("justin sun", "孙宇晨"), "justin-sun", "Justin Sun"),
    (("cathie wood", "木头姐"), "cathie-wood", "Cathie Wood"),
    (("ray dalio", "达利欧"), "ray-dalio", "Ray Dalio"),
    (("michael burry", "迈克尔·伯里"), "michael-burry", "Michael Burry"),
    (("bill ackman", "阿克曼"), "bill-ackman", "Bill Ackman"),
    (("vitalik buterin", "vitalik", "维塔利克"), "vitalik", "Vitalik Buterin"),
)

_HEADLINE_GEOPOLITICAL_TERMS = (
    "war",
    "airstrike",
    "missile",
    "invasion",
    "ceasefire",
    "blockade",
    "sanction",
    "red sea",
    "strait of hormuz",
    "taiwan strait",
    "ukraine",
    "gaza",
    "israel",
    "iran",
    "战争",
    "空袭",
    "导弹",
    "入侵",
    "停火",
    "封锁",
    "制裁",
    "红海",
    "霍尔木兹",
    "台海",
    "乌克兰",
    "加沙",
    "以色列",
    "伊朗",
)

_HEADLINE_EXCLUSION_TERMS = (
    "civil war-era",
    "civil war era",
    "world war ii",
    "world war i",
    "wwii",
    "wwi",
    "historical reenactment",
    "archaeological",
    "cannonball",
    "二战",
    "第一次世界大战",
    "南北战争时期",
    "古战场",
    "历史文物",
    # GDELT is title-level aggregation. These entertainment and fictional-war
    # phrases otherwise inherit a high score from the word "war" despite not
    # being an investable geopolitical development.
    "god of war",
    "war chronicle",
    "crunchyroll",
    "anime",
    "video game",
    "gameplay",
    "game release",
    "movie review",
    "film review",
    "trailer",
    "soundtrack",
    "manga",
    "comic book",
    "fictional war",
    "fantasy novel",
)

_HEADLINE_ENERGY_TERMS = (
    "opec",
    "crude oil",
    "oil price",
    "oil supply",
    "natural gas",
    "lng",
    "pipeline",
    "gold price",
    "原油",
    "油价",
    "石油供应",
    "天然气",
    "管道",
    "黄金",
)

_HEADLINE_CRYPTO_TERMS = (
    "bitcoin",
    "ethereum",
    "binance",
    "stablecoin",
    "cryptocurrency",
    "crypto market",
    "比特币",
    "以太坊",
    "币安",
    "稳定币",
    "加密货币",
)

_HEADLINE_MACRO_TERMS = (
    "federal reserve",
    "interest rate",
    "rate cut",
    "rate hike",
    "inflation",
    "tariff",
    "trade war",
    "ecb",
    "bank of japan",
    "people's bank of china",
    "pboc",
    "美联储",
    "利率",
    "降息",
    "加息",
    "通胀",
    "关税",
    "贸易战",
    "欧洲央行",
    "日本央行",
    "中国人民银行",
)

_HEADLINE_TECH_TERMS = (
    "nvidia",
    "tesla",
    "semiconductor",
    "chip export",
    "artificial intelligence",
    "cyberattack",
    "data breach",
    "英伟达",
    "特斯拉",
    "半导体",
    "芯片出口",
    "人工智能",
    "网络攻击",
    "数据泄露",
)

_HEADLINE_FIGURE_TERMS = tuple(
    term
    for terms, _, _ in _HEADLINE_PROFILES
    for term in terms
)

_HEADLINE_WEIGHTED_TERMS = (
    (6, _HEADLINE_FIGURE_TERMS),
    (5, _HEADLINE_GEOPOLITICAL_TERMS),
    (4, _HEADLINE_MACRO_TERMS),
    (3, _HEADLINE_ENERGY_TERMS),
    (3, _HEADLINE_CRYPTO_TERMS),
    (2, _HEADLINE_TECH_TERMS),
)
