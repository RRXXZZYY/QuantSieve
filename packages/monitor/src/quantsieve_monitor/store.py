from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from .analyzer import (
    _confirmed_geopolitical_deescalation_analysis,
    _has_direct_gdelt_action,
    _is_speculative_gdelt_headline,
)
from .models import EventKind, MonitorEvent
from .sources import _headline_identity

_GDELT_KNOWN_NOISE_PHRASES = (
    "civil war-era",
    "civil war era",
    "civil war veterans",
    "memorial park cemetery",
    "war veterans cemetery",
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
_TIMESTAMP_STORAGE_VERSION = "utc-v1"


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Monitor event timestamps must include a timezone.")
    return value.astimezone(UTC).isoformat()


class EventStore:
    def __init__(self, path: str | Path = "data/quantsieve.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS monitor_events (
                    source TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    profile_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    url TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    analysis TEXT,
                    market_relevance TEXT NOT NULL DEFAULT 'unrated',
                    impact_assets TEXT NOT NULL DEFAULT '[]',
                    analysis_method TEXT,
                    tags TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(source, source_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS monitor_store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(monitor_events)").fetchall()
            }
            if "market_relevance" not in columns:
                connection.execute(
                    "ALTER TABLE monitor_events ADD COLUMN "
                    "market_relevance TEXT NOT NULL DEFAULT 'unrated'"
                )
            if "impact_assets" not in columns:
                connection.execute(
                    "ALTER TABLE monitor_events ADD COLUMN "
                    "impact_assets TEXT NOT NULL DEFAULT '[]'"
                )
            if "analysis_method" not in columns:
                connection.execute(
                    "ALTER TABLE monitor_events ADD COLUMN analysis_method TEXT"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_available "
                "ON monitor_events(available_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_source_occurred "
                "ON monitor_events(source, occurred_at DESC)"
            )
            timestamp_version = connection.execute(
                "SELECT value FROM monitor_store_meta WHERE key = 'timestamp_storage'"
            ).fetchone()
            if (
                timestamp_version is None
                or str(timestamp_version["value"]) != _TIMESTAMP_STORAGE_VERSION
            ):
                self._normalize_stored_timestamps(connection)
                connection.execute(
                    "INSERT INTO monitor_store_meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("timestamp_storage", _TIMESTAMP_STORAGE_VERSION),
                )
            # Historical GDELT rows were once grouped under people mentioned in
            # their headlines. They are third-party news references, not those
            # people's statements, so correct them whenever the store opens.
            connection.execute(
                """
                UPDATE monitor_events
                SET profile_id = 'global-headline-watch',
                    profile_name = '全球新闻标题'
                WHERE source = 'gdelt-headlines'
                """
            )
            self._deduplicate_gdelt_headlines(connection)
            self._remove_known_gdelt_noise(connection)
            self._demote_cached_speculative_gdelt_headlines(connection)
            self._reclassify_cached_confirmed_deescalations(connection)

    @staticmethod
    def _normalize_stored_timestamps(connection: sqlite3.Connection) -> None:
        """Migrate legacy offset or naive timestamps to canonical UTC text."""

        rows = connection.execute(
            "SELECT rowid, occurred_at, available_at, created_at FROM monitor_events"
        ).fetchall()
        updates: list[tuple[str, str, str, int]] = []
        for row in rows:
            values: list[str] = []
            changed = False
            for column in ("occurred_at", "available_at", "created_at"):
                original = str(row[column])
                try:
                    parsed = datetime.fromisoformat(original.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError(
                        "Invalid stored monitor timestamp at "
                        f"rowid={int(row['rowid'])}, column={column}."
                    ) from exc
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    parsed = parsed.replace(tzinfo=UTC)
                normalized = _utc_iso(parsed)
                values.append(normalized)
                changed = changed or normalized != original
            if changed:
                updates.append(
                    (values[0], values[1], values[2], int(row["rowid"]))
                )
        if updates:
            connection.executemany(
                "UPDATE monitor_events "
                "SET occurred_at = ?, available_at = ?, created_at = ? "
                "WHERE rowid = ?",
                updates,
            )

    @staticmethod
    def _deduplicate_gdelt_headlines(connection: sqlite3.Connection) -> None:
        """Keep one stored row for each syndicated GDELT headline."""

        rows = connection.execute(
            """
            SELECT rowid, title
            FROM monitor_events
            WHERE source = 'gdelt-headlines'
            ORDER BY occurred_at DESC, rowid DESC
            """
        ).fetchall()
        seen_titles: set[str] = set()
        duplicate_row_ids: list[int] = []
        for row in rows:
            title_key = _headline_identity(str(row["title"]))
            if title_key in seen_titles:
                duplicate_row_ids.append(int(row["rowid"]))
            else:
                seen_titles.add(title_key)
        if duplicate_row_ids:
            placeholders = ", ".join("?" for _ in duplicate_row_ids)
            connection.execute(
                f"DELETE FROM monitor_events WHERE rowid IN ({placeholders})",
                duplicate_row_ids,
            )

    @staticmethod
    def _remove_known_gdelt_noise(connection: sqlite3.Connection) -> None:
        """Remove clear historical or entertainment GDELT false positives only."""

        rows = connection.execute(
            "SELECT rowid, title FROM monitor_events WHERE source = 'gdelt-headlines'"
        ).fetchall()
        row_ids = [
            int(row["rowid"])
            for row in rows
            if any(
                phrase in str(row["title"]).casefold()
                for phrase in _GDELT_KNOWN_NOISE_PHRASES
            )
        ]
        if row_ids:
            placeholders = ", ".join("?" for _ in row_ids)
            connection.execute(
                f"DELETE FROM monitor_events WHERE rowid IN ({placeholders})",
                row_ids,
            )

    @staticmethod
    def _demote_cached_speculative_gdelt_headlines(connection: sqlite3.Connection) -> None:
        """Apply safe deterministic rule upgrades to stale headline cards.

        A source may stop returning an old title before the next refresh, so
        ``MonitorService`` cannot reanalyse that row.  Reclassify only rows
        whose existing analysis is rule-based; a user-provided/BYOK analysis
        remains untouched.  The card stays visible with its source link, but
        no longer claims a concrete cross-asset transmission without a direct
        current action in the title.
        """

        rows = connection.execute(
            """
            SELECT rowid, source, source_id, profile_id, profile_name, kind,
                   title, content, url, occurred_at, available_at, tags, created_at,
                   market_relevance, impact_assets
            FROM monitor_events
            WHERE source = 'gdelt-headlines'
              AND (analysis_method IS NULL OR analysis_method = 'rules')
            """
        ).fetchall()
        row_ids: list[int] = []
        for row in rows:
            event = MonitorEvent(
                source=str(row["source"]),
                source_id=str(row["source_id"]),
                profile_id=str(row["profile_id"]),
                profile_name=str(row["profile_name"]),
                kind=EventKind(str(row["kind"])),
                title=str(row["title"]),
                content=str(row["content"]),
                url=str(row["url"]),
                occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
                available_at=datetime.fromisoformat(str(row["available_at"])),
                tags=json.loads(str(row["tags"])),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            text = f"{event.profile_name} {event.title} {event.content}".lower()
            no_transmission_chain = (
                str(row["market_relevance"]) in {"high", "medium"}
                and json.loads(str(row["impact_assets"])) == []
            )
            if _is_speculative_gdelt_headline(event, text) or (
                no_transmission_chain and not _has_direct_gdelt_action(text)
            ):
                row_ids.append(int(row["rowid"]))
        if row_ids:
            placeholders = ", ".join("?" for _ in row_ids)
            connection.execute(
                f"""
                UPDATE monitor_events
                SET analysis = ?, market_relevance = 'low', impact_assets = '[]',
                    analysis_method = 'rules'
                WHERE rowid IN ({placeholders})
                """,
                [
                    (
                        "这是标题级新闻聚合，提及冲突或军工但没有识别到可核验的直接行动、"
                        "制裁或供应中断；保留原始链接等待核验，不自动生成资产影响。"
                    ),
                    *row_ids,
                ],
            )

    @staticmethod
    def _reclassify_cached_confirmed_deescalations(
        connection: sqlite3.Connection,
    ) -> None:
        """Correct stale rule-only conflict cards when a later rule adds context.

        RSS feeds are short-lived, so an already stored headline may no longer
        arrive in a future refresh. This narrow migration updates only
        deterministic GDELT analyses with an explicit completed de-escalation
        action; it never overwrites a BYOK/AI interpretation.
        """
        rows = connection.execute(
            """
            SELECT rowid, source, source_id, profile_id, profile_name, kind,
                   title, content, url, occurred_at, available_at, tags, created_at
            FROM monitor_events
            WHERE source = 'gdelt-headlines'
              AND (analysis_method IS NULL OR analysis_method = 'rules')
            """
        ).fetchall()
        updates: list[tuple[str, str, str, str, int]] = []
        for row in rows:
            event = MonitorEvent(
                source=str(row["source"]),
                source_id=str(row["source_id"]),
                profile_id=str(row["profile_id"]),
                profile_name=str(row["profile_name"]),
                kind=EventKind(str(row["kind"])),
                title=str(row["title"]),
                content=str(row["content"]),
                url=str(row["url"]),
                occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
                available_at=datetime.fromisoformat(str(row["available_at"])),
                tags=json.loads(str(row["tags"])),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            text = f"{event.profile_name} {event.title} {event.content}".lower()
            analysis = _confirmed_geopolitical_deescalation_analysis(event, text)
            if analysis is not None:
                updates.append(
                    (
                        analysis.summary,
                        analysis.relevance,
                        json.dumps(
                            [impact.model_dump() for impact in analysis.impacts],
                            ensure_ascii=False,
                        ),
                        analysis.method,
                        int(row["rowid"]),
                    )
                )
        if updates:
            connection.executemany(
                """
                UPDATE monitor_events
                SET analysis = ?, market_relevance = ?, impact_assets = ?, analysis_method = ?
                WHERE rowid = ?
                """,
                updates,
            )

    def upsert(self, events: list[MonitorEvent]) -> int:
        if not events:
            return 0
        values = [
            (
                event.source,
                event.source_id,
                event.profile_id,
                event.profile_name,
                event.kind.value,
                event.title,
                event.content,
                event.url,
                _utc_iso(event.occurred_at),
                _utc_iso(event.available_at),
                event.analysis,
                event.market_relevance,
                json.dumps(
                    [impact.model_dump() for impact in event.impact_assets],
                    ensure_ascii=False,
                ),
                event.analysis_method,
                json.dumps(event.tags, ensure_ascii=False),
                _utc_iso(event.created_at),
            )
            for event in events
        ]
        with self._lock, self._connect() as connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO monitor_events (
                    source, source_id, profile_id, profile_name, kind, title, content,
                    url, occurred_at, available_at, analysis, market_relevance,
                    impact_assets, analysis_method, tags, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, source_id) DO UPDATE SET
                    profile_id = excluded.profile_id,
                    profile_name = excluded.profile_name,
                    kind = excluded.kind,
                    title = excluded.title,
                    content = excluded.content,
                    url = excluded.url,
                    analysis = COALESCE(excluded.analysis, monitor_events.analysis),
                    market_relevance = CASE
                        WHEN excluded.analysis IS NOT NULL THEN excluded.market_relevance
                        ELSE monitor_events.market_relevance
                    END,
                    impact_assets = CASE
                        WHEN excluded.analysis IS NOT NULL THEN excluded.impact_assets
                        ELSE monitor_events.impact_assets
                    END,
                    analysis_method = COALESCE(
                        excluded.analysis_method,
                        monitor_events.analysis_method
                    ),
                    tags = excluded.tags
                """,
                values,
            )
            self._deduplicate_gdelt_headlines(connection)
            self._remove_known_gdelt_noise(connection)
            self._demote_cached_speculative_gdelt_headlines(connection)
            self._reclassify_cached_confirmed_deescalations(connection)
            return connection.total_changes - before

    def list_visible(
        self,
        *,
        limit: int = 50,
        profile_id: str | None = None,
        now: datetime | None = None,
    ) -> list[MonitorEvent]:
        visible_at = _utc_iso(now or datetime.now(UTC))
        query = "SELECT * FROM monitor_events WHERE available_at <= ?"
        parameters: list[object] = [visible_at]
        if profile_id:
            query += " AND profile_id = ?"
            parameters.append(profile_id)
        query += " ORDER BY occurred_at DESC, source, source_id LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._row_to_event(row) for row in rows]

    def list_visible_source_sample(
        self,
        *,
        limit_per_source: int = 12,
        now: datetime | None = None,
    ) -> list[MonitorEvent]:
        """Return a bounded recent sample from every stored source.

        A single high-volume feed can otherwise fill the global recency window
        before the API gets a chance to curate source-diverse evidence.
        """

        if limit_per_source < 1:
            raise ValueError("limit_per_source must be at least 1.")
        visible_at = _utc_iso(now or datetime.now(UTC))
        with self._connect() as connection:
            sources = [
                str(row["source"])
                for row in connection.execute(
                    "SELECT DISTINCT source FROM monitor_events "
                    "WHERE available_at <= ? ORDER BY source",
                    (visible_at,),
                ).fetchall()
            ]
            rows = [
                row
                for source in sources
                for row in connection.execute(
                    "SELECT * FROM monitor_events "
                    "WHERE available_at <= ? AND source = ? "
                    "ORDER BY occurred_at DESC, source_id LIMIT ?",
                    (visible_at, source, limit_per_source),
                ).fetchall()
            ]
        return [self._row_to_event(row) for row in rows]

    def contains(self, source: str, source_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM monitor_events WHERE source = ? AND source_id = ? LIMIT 1",
                (source, source_id),
            ).fetchone()
        return row is not None

    def get_visible_by_keys(
        self,
        keys: list[tuple[str, str]],
        *,
        now: datetime | None = None,
    ) -> list[MonitorEvent]:
        """Return visible events for exact source identities in request order."""

        unique_keys = list(dict.fromkeys(keys))
        if not unique_keys:
            return []
        visible_at = _utc_iso(now or datetime.now(UTC))
        placeholders = ", ".join("(?, ?)" for _ in unique_keys)
        parameters: list[object] = [
            visible_at,
            *(value for key in unique_keys for value in key),
        ]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM monitor_events WHERE available_at <= ? "
                f"AND (source, source_id) IN ({placeholders})",
                parameters,
            ).fetchall()
        events = {
            (str(row["source"]), str(row["source_id"])): self._row_to_event(row)
            for row in rows
        }
        return [events[key] for key in unique_keys if key in events]

    def rule_analyzed_keys(
        self,
        events: list[MonitorEvent],
    ) -> set[tuple[str, str]]:
        """Return stored events whose deterministic analysis may be safely refreshed."""

        keys = list(dict.fromkeys((event.source, event.source_id) for event in events))
        if not keys:
            return set()
        placeholders = ", ".join("(?, ?)" for _ in keys)
        parameters = [value for key in keys for value in key]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT source, source_id FROM monitor_events "
                "WHERE analysis_method = 'rules' "
                f"AND (source, source_id) IN ({placeholders})",
                parameters,
            ).fetchall()
        return {(str(row["source"]), str(row["source_id"])) for row in rows}

    def update_analysis(self, source: str, source_id: str, analysis: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE monitor_events SET analysis = ? WHERE source = ? AND source_id = ?",
                (analysis, source, source_id),
            )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> MonitorEvent:
        return MonitorEvent(
            source=row["source"],
            source_id=row["source_id"],
            profile_id=row["profile_id"],
            profile_name=row["profile_name"],
            kind=EventKind(row["kind"]),
            title=row["title"],
            content=row["content"],
            url=row["url"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            available_at=datetime.fromisoformat(row["available_at"]),
            analysis=row["analysis"],
            market_relevance=row["market_relevance"],
            impact_assets=json.loads(row["impact_assets"]),
            analysis_method=row["analysis_method"],
            tags=json.loads(row["tags"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
