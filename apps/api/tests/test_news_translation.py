import asyncio
import json
import time
from datetime import UTC, datetime

import httpx
import pytest
from quantsieve_api.news_translation import NewsTranslationService
from quantsieve_monitor import MonitorEvent
from quantsieve_monitor.models import EventKind, MarketImpact
from quantsieve_providers import SQLiteCache


def english_event() -> MonitorEvent:
    now = datetime.now(UTC)
    return MonitorEvent(
        source="official-intel",
        source_id="fed-one",
        profile_id="central-bank-watch",
        profile_name="全球央行与宏观政策",
        kind=EventKind.NEWS,
        title="Federal Reserve holds interest rates steady",
        content="Inflation risks remain elevated as officials monitor employment.",
        url="https://example.com/fed",
        occurred_at=now,
        available_at=now,
        analysis="这项政策可能影响美元和美股估值。",
        analysis_method="rules",
        market_relevance="high",
        impact_assets=[
            MarketImpact(
                asset="USD",
                direction="volatile",
                reason="Rate expectations can change demand for the currency.",
            )
        ],
    )


@pytest.mark.asyncio
async def test_translation_service_translates_english_fields_and_reuses_cache(
    tmp_path,
) -> None:
    requests: list[list[str]] = []

    def translate(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        texts = payload["q"]
        requests.append(texts)
        return httpx.Response(
            200,
            json={"translatedText": [f"中文：{text}" for text in texts]},
        )

    service = NewsTranslationService(
        enabled=True,
        base_url="http://translate:5000",
        cache=SQLiteCache(tmp_path / "cache.db"),
        transport=httpx.MockTransport(translate),
    )

    first = (await service.translate_events([english_event()]))[0]
    second = (await service.translate_events([english_event()]))[0]

    assert first["status"] == "translated"
    assert first["translated_fields"] == 3
    assert first["cached_fields"] == 0
    assert str(first["title_zh"]).startswith("中文：Federal Reserve")
    assert first["analysis_zh"] == "这项政策可能影响美元和美股估值。"
    assert first["impact_reasons_zh"] == [
        "中文：Rate expectations can change demand for the currency."
    ]
    assert second["translated_fields"] == 3
    assert second["cached_fields"] == 3
    assert len(requests) == 1
    assert service.status["availability"] == "ready"


@pytest.mark.asyncio
async def test_translation_service_fails_open_to_original_text(tmp_path) -> None:
    service = NewsTranslationService(
        enabled=True,
        base_url="http://translate:5000",
        cache=SQLiteCache(tmp_path / "cache.db"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, json={"error": "offline"})
        ),
        upstream_batch_size=1,
    )
    event = english_event()

    result = (await service.translate_events([event]))[0]

    assert result["status"] == "partial"
    assert result["title_zh"] == event.title
    assert result["content_zh"] == event.content
    assert result["analysis_zh"] == event.analysis
    assert result["unavailable_fields"] == 3
    assert service.status["availability"] == "degraded"
    assert service.status["last_error_code"] == "translation_upstream_unavailable"


@pytest.mark.asyncio
async def test_translation_service_does_not_call_upstream_for_chinese(tmp_path) -> None:
    calls = 0

    def unexpected(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    event = english_event().model_copy(
        update={
            "title": "美联储维持利率不变",
            "content": "通胀风险仍然较高。",
            "impact_assets": [
                MarketImpact(
                    asset="美元",
                    direction="volatile",
                    reason="利率预期会影响美元需求。",
                )
            ],
        }
    )
    service = NewsTranslationService(
        enabled=True,
        base_url="http://translate:5000",
        cache=SQLiteCache(tmp_path / "cache.db"),
        transport=httpx.MockTransport(unexpected),
    )

    result = (await service.translate_events([event]))[0]

    assert result["status"] == "identity"
    assert result["title_zh"] == event.title
    assert calls == 0


@pytest.mark.asyncio
async def test_translation_service_fails_open_for_non_object_response(tmp_path) -> None:
    service = NewsTranslationService(
        enabled=True,
        base_url="http://translate:5000",
        cache=SQLiteCache(tmp_path / "cache.db"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=["unexpected"])
        ),
    )
    event = english_event()

    result = (await service.translate_events([event]))[0]

    assert result["status"] == "partial"
    assert result["title_zh"] == event.title
    assert result["fields"]["title"]["error_code"] == "translation_invalid_response"


@pytest.mark.asyncio
async def test_translation_service_enforces_total_request_deadline(tmp_path) -> None:
    async def slow_response(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"translatedText": ["不会按时返回"]})

    service = NewsTranslationService(
        enabled=True,
        base_url="http://translate:5000",
        cache=SQLiteCache(tmp_path / "cache.db"),
        timeout_seconds=0.02,
        upstream_batch_size=1,
        transport=httpx.MockTransport(slow_response),
    )
    event = english_event()

    result = (await service.translate_events([event]))[0]

    assert result["status"] == "partial"
    assert result["title_zh"] == event.title
    assert result["fields"]["title"]["error_code"] == "translation_timeout"


@pytest.mark.asyncio
async def test_translation_service_treats_cache_read_failure_as_miss(
    tmp_path,
) -> None:
    class ReadFailureCache(SQLiteCache):
        def get_many(self, keys: list[str]) -> dict[str, object]:
            raise RuntimeError("cache read failed")

    service = NewsTranslationService(
        enabled=True,
        base_url="http://translate:5000",
        cache=ReadFailureCache(tmp_path / "cache.db"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "translatedText": [
                        f"中文：{text}"
                        for text in json.loads(request.content)["q"]
                    ]
                },
            )
        ),
    )

    result = (await service.translate_events([english_event()]))[0]

    assert result["status"] == "translated"
    assert str(result["title_zh"]).startswith("中文：Federal Reserve")


@pytest.mark.asyncio
async def test_translation_service_returns_translation_when_cache_write_fails(
    tmp_path,
) -> None:
    class WriteFailureCache(SQLiteCache):
        def set_many(self, values, ttl) -> None:
            raise RuntimeError("cache write failed")

    service = NewsTranslationService(
        enabled=True,
        base_url="http://translate:5000",
        cache=WriteFailureCache(tmp_path / "cache.db"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "translatedText": [
                        f"中文：{text}"
                        for text in json.loads(request.content)["q"]
                    ]
                },
            )
        ),
    )

    result = (await service.translate_events([english_event()]))[0]

    assert result["status"] == "translated"
    assert service.status["last_error_code"] == "translation_cache_write_failed"


@pytest.mark.asyncio
async def test_translation_service_deadline_includes_cache_io(tmp_path) -> None:
    class SlowReadCache(SQLiteCache):
        def get_many(self, keys: list[str]) -> dict[str, object]:
            time.sleep(0.1)
            return {}

    service = NewsTranslationService(
        enabled=True,
        base_url="http://translate:5000",
        cache=SlowReadCache(tmp_path / "cache.db"),
        timeout_seconds=0.02,
        transport=httpx.MockTransport(
            lambda request: pytest.fail("upstream must not run after the deadline")
        ),
    )
    started_at = time.monotonic()

    result = (await service.translate_events([english_event()]))[0]

    assert time.monotonic() - started_at < 0.08
    assert result["status"] == "partial"
    assert result["fields"]["title"]["error_code"] == "translation_timeout"


def test_translation_service_rejects_invalid_base_url(tmp_path) -> None:
    with pytest.raises(ValueError, match="invalid"):
        NewsTranslationService(
            enabled=True,
            base_url="http://example.com:not-a-port",
            cache=SQLiteCache(tmp_path / "cache.db"),
        )


@pytest.mark.asyncio
async def test_translation_service_translates_short_english_title(tmp_path) -> None:
    event = english_event().model_copy(
        update={
            "title": "Oil up",
            "content": "原油价格上涨。",
            "impact_assets": [],
        }
    )
    service = NewsTranslationService(
        enabled=True,
        base_url="http://translate:5000",
        cache=SQLiteCache(tmp_path / "cache.db"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"translatedText": ["原油上涨"]},
            )
        ),
    )

    result = (await service.translate_events([event]))[0]

    assert result["title_zh"] == "原油上涨"
    assert result["fields"]["title"]["status"] == "translated"


@pytest.mark.asyncio
async def test_translation_service_reports_latest_failure_as_degraded(
    tmp_path,
) -> None:
    calls = 0

    def translate(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            texts = json.loads(request.content)["q"]
            return httpx.Response(
                200,
                json={"translatedText": [f"中文：{text}" for text in texts]},
            )
        return httpx.Response(503, json={"error": "offline"})

    service = NewsTranslationService(
        enabled=True,
        base_url="http://translate:5000",
        cache=SQLiteCache(tmp_path / "cache.db"),
        transport=httpx.MockTransport(translate),
    )
    await service.translate_events([english_event()])
    changed_event = english_event().model_copy(
        update={
            "source_id": "fed-two",
            "title": "Federal Reserve changes its policy outlook",
            "content": "Officials published a materially different assessment.",
            "impact_assets": [],
        }
    )

    await service.translate_events([changed_event])

    assert service.status["availability"] == "degraded"
    assert service.status["last_error_code"] == "translation_upstream_unavailable"


@pytest.mark.asyncio
async def test_translation_service_classifies_connection_failure(tmp_path) -> None:
    def disconnected(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    service = NewsTranslationService(
        enabled=True,
        base_url="http://translate:5000",
        cache=SQLiteCache(tmp_path / "cache.db"),
        transport=httpx.MockTransport(disconnected),
    )

    result = (await service.translate_events([english_event()]))[0]

    assert (
        result["fields"]["title"]["error_code"]
        == "translation_upstream_unavailable"
    )
