from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit

import httpx
from quantsieve_monitor import MonitorEvent
from quantsieve_providers import SQLiteCache

TranslationFieldStatus = Literal[
    "translated",
    "identity",
    "unavailable",
    "too_long",
]

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HAN_CHARACTERS = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_CHARACTERS = re.compile(r"[A-Za-z]")
_CACHE_NAMESPACE = "monitor-translation"
_TRANSLATION_ENGINE = "libretranslate-argos"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranslationField:
    text: str
    status: TranslationFieldStatus
    cache_hit: bool = False
    error_code: str | None = None

    def model_dump(self) -> dict[str, object]:
        return {
            "text": self.text,
            "status": self.status,
            "cache_hit": self.cache_hit,
            "error_code": self.error_code,
        }


class NewsTranslationService:
    """Translate persisted monitor evidence without modifying its source text."""

    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str,
        cache: SQLiteCache,
        timeout_seconds: float = 45,
        cache_ttl_days: int = 90,
        contract_version: str = "argos-en-zh-v1",
        upstream_batch_size: int = 8,
        maximum_field_characters: int = 4_000,
        maximum_request_characters: int = 24_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized_base_url = base_url.strip().rstrip("/")
        try:
            parsed_base_url = urlsplit(normalized_base_url)
            _ = parsed_base_url.port
        except ValueError as error:
            raise ValueError("Translation base URL is invalid.") from error
        if (
            parsed_base_url.scheme not in {"http", "https"}
            or not parsed_base_url.hostname
            or parsed_base_url.username is not None
            or parsed_base_url.password is not None
            or parsed_base_url.query
            or parsed_base_url.fragment
        ):
            raise ValueError("Translation base URL must use HTTP or HTTPS.")
        if timeout_seconds <= 0:
            raise ValueError("Translation timeout must be positive.")
        if upstream_batch_size < 1:
            raise ValueError("Translation upstream batch size must be positive.")
        self.enabled = enabled
        self.base_url = normalized_base_url
        self.cache = cache
        self.timeout_seconds = timeout_seconds
        self.cache_ttl = timedelta(days=cache_ttl_days)
        self.contract_version = contract_version.strip()
        self.upstream_batch_size = upstream_batch_size
        self.maximum_field_characters = maximum_field_characters
        self.maximum_request_characters = maximum_request_characters
        self._transport = transport
        self._semaphore = asyncio.Semaphore(1)
        self._failure_count = 0
        self._circuit_open_until = 0.0
        self._last_success_at: datetime | None = None
        self._last_error_code: str | None = None

    @property
    def status(self) -> dict[str, object]:
        if not self.enabled:
            availability = "disabled"
        elif self._circuit_open_until > monotonic() or self._last_error_code is not None:
            availability = "degraded"
        elif self._last_success_at is not None:
            availability = "ready"
        else:
            availability = "configured"
        return {
            "enabled": self.enabled,
            "engine": _TRANSLATION_ENGINE,
            "availability": availability,
            "last_success_at": (
                self._last_success_at.isoformat()
                if self._last_success_at is not None
                else None
            ),
            "last_error_code": self._last_error_code,
        }

    async def translate_events(
        self,
        events: list[MonitorEvent],
    ) -> list[dict[str, object]]:
        try:
            return await asyncio.wait_for(
                self._translate_events(events),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            error_code = "translation_timeout"
            self._record_failure(error_code)
            return [
                self._failed_event_result(event, error_code)
                for event in events
            ]

    async def _translate_events(
        self,
        events: list[MonitorEvent],
    ) -> list[dict[str, object]]:
        field_sources: dict[str, str] = {}
        event_fields: list[dict[str, str]] = []
        for event_index, event in enumerate(events):
            qualified = _event_source_fields(event, event_index)
            field_sources.update(qualified)
            event_fields.append(qualified)

        translated_fields = await self._translate_fields(field_sources)
        return [
            self._event_result(
                event,
                qualified,
                translated_fields,
            )
            for event, qualified in zip(events, event_fields, strict=True)
        ]

    async def _translate_fields(
        self,
        fields: dict[str, str],
    ) -> dict[str, TranslationField]:
        results: dict[str, TranslationField] = {}
        pending_by_text: dict[str, list[str]] = {}
        original_by_field: dict[str, str] = {}
        total_characters = 0
        for field_name, original in fields.items():
            normalized = _normalize_text(original)
            original_by_field[field_name] = original
            if not _needs_chinese_translation(normalized):
                results[field_name] = TranslationField(
                    text=original,
                    status="identity",
                )
                continue
            if len(normalized) > self.maximum_field_characters:
                results[field_name] = TranslationField(
                    text=original,
                    status="too_long",
                    error_code="field_too_long",
                )
                continue
            total_characters += len(normalized)
            if total_characters > self.maximum_request_characters:
                results[field_name] = TranslationField(
                    text=original,
                    status="too_long",
                    error_code="request_too_large",
                )
                continue
            pending_by_text.setdefault(normalized, []).append(field_name)

        cache_keys = {
            text: self._cache_key(text)
            for text in pending_by_text
        }
        try:
            cached = await asyncio.to_thread(
                self.cache.get_many,
                list(cache_keys.values()),
            )
        except Exception:
            self._last_error_code = "translation_cache_read_failed"
            _LOGGER.warning("News translation cache read failed.", exc_info=True)
            cached = {}
        uncached_texts: list[str] = []
        for text, field_names in pending_by_text.items():
            payload = cached.get(cache_keys[text])
            cached_text = payload.get("text") if isinstance(payload, dict) else None
            translated = cached_text.strip() if isinstance(cached_text, str) else ""
            if translated:
                for field_name in field_names:
                    results[field_name] = TranslationField(
                        text=translated,
                        status="translated",
                        cache_hit=True,
                    )
            else:
                uncached_texts.append(text)

        fresh = await self._translate_uncached(uncached_texts)
        cache_values: dict[str, dict[str, str]] = {}
        for text, translation in fresh.items():
            field_names = pending_by_text[text]
            if translation.status == "translated":
                cache_values[cache_keys[text]] = {
                    "text": translation.text,
                    "engine": _TRANSLATION_ENGINE,
                    "contract": self.contract_version,
                }
            for field_name in field_names:
                if translation.status == "translated":
                    results[field_name] = translation
                else:
                    results[field_name] = TranslationField(
                        text=original_by_field[field_name],
                        status=translation.status,
                        error_code=translation.error_code,
                    )
        if cache_values:
            try:
                await asyncio.to_thread(
                    self.cache.set_many,
                    cache_values,
                    self.cache_ttl,
                )
            except Exception:
                self._last_error_code = "translation_cache_write_failed"
                _LOGGER.warning("News translation cache write failed.", exc_info=True)
        return results

    async def _translate_uncached(
        self,
        texts: list[str],
    ) -> dict[str, TranslationField]:
        if not texts:
            return {}
        if not self.enabled:
            return _failed_translations(texts, "translation_disabled")
        if self._circuit_open_until > monotonic():
            return _failed_translations(texts, "translation_temporarily_unavailable")

        async with self._semaphore:
            if self._circuit_open_until > monotonic():
                return _failed_translations(
                    texts,
                    "translation_temporarily_unavailable",
                )
            results: dict[str, TranslationField] = {}
            timeout = httpx.Timeout(
                self.timeout_seconds,
                connect=min(3.0, self.timeout_seconds),
                pool=min(2.0, self.timeout_seconds),
                write=min(10.0, self.timeout_seconds),
            )
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                for offset in range(0, len(texts), self.upstream_batch_size):
                    batch = texts[offset : offset + self.upstream_batch_size]
                    try:
                        response = await client.post(
                            f"{self.base_url}/translate",
                            json={
                                "q": batch,
                                "source": "en",
                                "target": "zh",
                                "format": "text",
                            },
                        )
                        response.raise_for_status()
                        payload = response.json()
                        if not isinstance(payload, dict):
                            raise ValueError("Invalid LibreTranslate response.")
                        translated = payload.get("translatedText")
                        if isinstance(translated, str) and len(batch) == 1:
                            translated = [translated]
                        if (
                            not isinstance(translated, list)
                            or len(translated) != len(batch)
                            or not all(
                                isinstance(value, str) and value.strip()
                                for value in translated
                            )
                        ):
                            raise ValueError("Invalid LibreTranslate response.")
                        for source, value in zip(batch, translated, strict=True):
                            normalized = _normalize_text(value)
                            if len(normalized) > max(len(source) * 5, 2_000):
                                raise ValueError("Translation output is unexpectedly long.")
                            results[source] = TranslationField(
                                text=normalized,
                                status="translated",
                            )
                        self._record_success()
                    except httpx.TimeoutException:
                        error_code = "translation_timeout"
                        self._record_failure(error_code)
                        results.update(_failed_translations(batch, error_code))
                    except httpx.ConnectError:
                        error_code = "translation_upstream_unavailable"
                        self._record_failure(error_code)
                        results.update(_failed_translations(batch, error_code))
                    except httpx.HTTPStatusError as error:
                        if error.response.status_code == 429:
                            error_code = "translation_rate_limited"
                        elif error.response.status_code >= 500:
                            error_code = "translation_upstream_unavailable"
                        else:
                            error_code = "translation_rejected"
                        self._record_failure(error_code)
                        results.update(_failed_translations(batch, error_code))
                    except (httpx.HTTPError, ValueError, TypeError):
                        error_code = "translation_invalid_response"
                        self._record_failure(error_code)
                        results.update(_failed_translations(batch, error_code))
                    if self._circuit_open_until > monotonic():
                        remaining = texts[offset + len(batch) :]
                        results.update(
                            _failed_translations(
                                remaining,
                                "translation_temporarily_unavailable",
                            )
                        )
                        break
            return results

    @staticmethod
    def _failed_event_result(
        event: MonitorEvent,
        error_code: str,
    ) -> dict[str, object]:
        qualified_fields = _event_source_fields(event, 0)
        translations = {
            field_name: (
                TranslationField(text=source_text, status="identity")
                if not _needs_chinese_translation(_normalize_text(source_text))
                else TranslationField(
                    text=source_text,
                    status="unavailable",
                    error_code=error_code,
                )
            )
            for field_name, source_text in qualified_fields.items()
        }
        return NewsTranslationService._event_result(
            event,
            qualified_fields,
            translations,
        )

    def _record_success(self) -> None:
        self._failure_count = 0
        self._circuit_open_until = 0.0
        self._last_error_code = None
        self._last_success_at = datetime.now(UTC)

    def _record_failure(self, error_code: str) -> None:
        self._failure_count += 1
        self._last_error_code = error_code
        if self._failure_count >= 3:
            self._circuit_open_until = monotonic() + 30

    def _cache_key(self, text: str) -> str:
        return SQLiteCache.make_key(
            _CACHE_NAMESPACE,
            contract=self.contract_version,
            engine=_TRANSLATION_ENGINE,
            source="en",
            target="zh",
            text=text,
        )

    @staticmethod
    def _event_result(
        event: MonitorEvent,
        qualified_fields: dict[str, str],
        translations: dict[str, TranslationField],
    ) -> dict[str, object]:
        by_name = {
            qualified_name.split(":", 1)[1]: translations[qualified_name]
            for qualified_name in qualified_fields
        }
        statuses = [translation.status for translation in by_name.values()]
        translated_count = statuses.count("translated")
        unavailable_count = sum(
            status in {"unavailable", "too_long"} for status in statuses
        )
        if unavailable_count == 0 and translated_count > 0:
            status = "translated"
        elif unavailable_count == 0:
            status = "identity"
        elif translated_count > 0 or statuses.count("identity") > 0:
            status = "partial"
        else:
            status = "unavailable"
        impact_reasons = [
            by_name[f"impact_reason_{index}"].text
            for index in range(len(event.impact_assets))
        ]
        signature_payload = {
            "title": event.title,
            "content": event.content,
            "analysis": event.analysis,
            "impact_reasons": [
                impact.reason for impact in event.impact_assets
            ],
        }
        return {
            "source": event.source,
            "source_id": event.source_id,
            "source_fingerprint": hashlib.sha256(
                json.dumps(
                    signature_payload,
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode()
            ).hexdigest(),
            "status": status,
            "title_zh": by_name["title"].text,
            "content_zh": by_name["content"].text,
            "analysis_zh": (
                by_name["analysis"].text
                if "analysis" in by_name
                else None
            ),
            "impact_reasons_zh": impact_reasons,
            "fields": {
                name: translation.model_dump()
                for name, translation in by_name.items()
            },
            "translated_fields": translated_count,
            "cached_fields": sum(
                translation.cache_hit for translation in by_name.values()
            ),
            "unavailable_fields": unavailable_count,
        }


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    normalized = _CONTROL_CHARACTERS.sub(" ", normalized)
    return normalized.strip()


def _needs_chinese_translation(value: str) -> bool:
    if not value:
        return False
    latin_count = len(_LATIN_CHARACTERS.findall(value))
    han_count = len(_HAN_CHARACTERS.findall(value))
    return latin_count >= 4 and (han_count == 0 or latin_count > han_count * 2)


def _event_source_fields(
    event: MonitorEvent,
    event_index: int,
) -> dict[str, str]:
    fields = {
        "title": event.title,
        "content": event.content,
    }
    if event.analysis:
        fields["analysis"] = event.analysis
    for impact_index, impact in enumerate(event.impact_assets):
        fields[f"impact_reason_{impact_index}"] = impact.reason
    return {
        f"{event_index}:{field_name}": value
        for field_name, value in fields.items()
    }


def _failed_translations(
    texts: list[str],
    error_code: str,
) -> dict[str, TranslationField]:
    return {
        text: TranslationField(
            text=text,
            status="unavailable",
            error_code=error_code,
        )
        for text in texts
    }
