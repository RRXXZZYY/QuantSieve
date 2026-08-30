import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest
from quantsieve_monitor import EventStore, MonitorEvent
from quantsieve_monitor.models import EventKind, MarketImpact


def make_event() -> MonitorEvent:
    occurred = datetime(2026, 1, 1, tzinfo=UTC)
    return MonitorEvent.delayed(
        source="test",
        source_id="one",
        profile_id="person",
        profile_name="Person",
        kind=EventKind.SOCIAL,
        title="A public statement",
        content="Source content",
        url="https://example.com/event",
        occurred_at=occurred,
    )


def test_store_enforces_visibility_delay(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    event = make_event()
    store.upsert([event])

    assert store.list_visible(now=event.occurred_at + timedelta(minutes=29)) == []
    assert store.list_visible(now=event.occurred_at + timedelta(minutes=31)) == [event]


def test_store_deduplicates_source_id(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    event = make_event()
    store.upsert([event, event])

    assert len(store.list_visible(now=event.available_at + timedelta(seconds=1))) == 1


def test_store_samples_each_visible_source_without_future_rows(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    base = make_event()
    visible_at = base.available_at + timedelta(hours=1)
    events = [
        base.model_copy(
            update={
                "source": source,
                "source_id": f"{source}-{index}",
                "occurred_at": base.occurred_at + timedelta(minutes=index),
                "available_at": base.available_at + timedelta(minutes=index),
            }
        )
        for source in ("official", "headlines")
        for index in range(4)
    ]
    future = base.model_copy(
        update={
            "source": "future",
            "source_id": "future-1",
            "occurred_at": visible_at + timedelta(hours=1),
            "available_at": visible_at + timedelta(hours=1),
        }
    )
    store.upsert([*events, future])

    sampled = store.list_visible_source_sample(limit_per_source=2, now=visible_at)

    assert [(event.source, event.source_id) for event in sampled] == [
        ("headlines", "headlines-3"),
        ("headlines", "headlines-2"),
        ("official", "official-3"),
        ("official", "official-2"),
    ]


def test_store_reads_visible_exact_keys_in_request_order(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    base = make_event()
    visible_at = base.available_at + timedelta(hours=1)
    first = base.model_copy(update={"source_id": "first"})
    second = base.model_copy(update={"source_id": "second"})
    future = base.model_copy(
        update={
            "source_id": "future",
            "available_at": visible_at + timedelta(hours=1),
        }
    )
    store.upsert([first, second, future])

    events = store.get_visible_by_keys(
        [
            (second.source, second.source_id),
            (first.source, first.source_id),
            (future.source, future.source_id),
            ("missing", "unknown"),
            (second.source, second.source_id),
        ],
        now=visible_at,
    )

    assert [(event.source, event.source_id) for event in events] == [
        (second.source, second.source_id),
        (first.source, first.source_id),
    ]


def test_store_normalizes_offsets_before_visibility_comparison(tmp_path) -> None:
    path = tmp_path / "events.db"
    store = EventStore(path)
    eastern = timezone(timedelta(hours=-4))
    event = make_event().model_copy(
        update={
            "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
            "available_at": datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
        }
    )
    store.upsert([event])
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE monitor_events SET occurred_at = ?, available_at = ?",
            (
                datetime(2025, 12, 31, 20, 0, tzinfo=eastern).isoformat(),
                datetime(2025, 12, 31, 20, 30, tzinfo=eastern).isoformat(),
            ),
        )
        connection.execute(
            "DELETE FROM monitor_store_meta WHERE key = 'timestamp_storage'"
        )

    reloaded = EventStore(path)

    assert reloaded.list_visible(now=datetime(2026, 1, 1, tzinfo=UTC)) == []
    visible = reloaded.list_visible(now=datetime(2026, 1, 1, 0, 31, tzinfo=UTC))
    assert len(visible) == 1
    assert visible[0].available_at == datetime(2026, 1, 1, 0, 30, tzinfo=UTC)


def test_monitor_event_rejects_naive_timestamps() -> None:
    event = make_event()

    with pytest.raises(ValueError, match="timezone"):
        MonitorEvent(
            **event.model_dump(
                exclude={"occurred_at", "available_at"},
            ),
            occurred_at=datetime(2026, 1, 1),
            available_at=datetime(2026, 1, 1, 0, 30),
        )


def test_store_timestamp_migration_identifies_corrupt_row(tmp_path) -> None:
    path = tmp_path / "events.db"
    store = EventStore(path)
    store.upsert([make_event()])
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE monitor_events SET available_at = 'not-a-date'")
        connection.execute(
            "DELETE FROM monitor_store_meta WHERE key = 'timestamp_storage'"
        )

    with pytest.raises(
        ValueError,
        match=r"rowid=1, column=available_at",
    ):
        EventStore(path)


def test_store_source_sample_rejects_zero_limit(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")

    with pytest.raises(ValueError, match="at least 1"):
        store.list_visible_source_sample(limit_per_source=0)


def test_store_reclassifies_historical_gdelt_person_mentions(tmp_path) -> None:
    path = tmp_path / "events.db"
    store = EventStore(path)
    event = make_event().model_copy(
        update={
            "source": "gdelt-headlines",
            "profile_id": "elon-musk",
            "profile_name": "Elon Musk",
        }
    )
    store.upsert([event])

    reloaded = EventStore(path)
    visible = reloaded.list_visible(now=event.available_at + timedelta(seconds=1))

    assert visible[0].profile_id == "global-headline-watch"
    assert visible[0].profile_name == "全球新闻标题"


def test_store_removes_historical_gdelt_syndication_duplicates(tmp_path) -> None:
    path = tmp_path / "events.db"
    store = EventStore(path)
    first = make_event().model_copy(
        update={
            "source": "gdelt-headlines",
            "source_id": "publisher-a",
            "title": "Oil supply risk rises",
        }
    )
    second = first.model_copy(
        update={
            "source_id": "publisher-b",
            "title": "Oil supply risk rises!",
        }
    )
    store.upsert([first, second])

    visible_before_restart = store.list_visible(
        now=first.available_at + timedelta(seconds=1)
    )
    assert len(visible_before_restart) == 1
    assert visible_before_restart[0].source_id == "publisher-b"

    reloaded = EventStore(path)
    visible = reloaded.list_visible(now=first.available_at + timedelta(seconds=1))

    assert len(visible) == 1
    assert visible[0].source_id == "publisher-b"


def test_store_removes_gdelt_publisher_suffix_duplicates(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    first = make_event().model_copy(
        update={
            "source": "gdelt-headlines",
            "source_id": "wire",
            "title": "US pauses attacks on Iran for a second day",
        }
    )
    syndicated = first.model_copy(
        update={
            "source_id": "publisher",
            "title": "US pauses attacks on Iran for a second day - Daily Sitka Sentinel",
        }
    )

    store.upsert([first, syndicated])

    visible = store.list_visible(now=first.available_at + timedelta(seconds=1))
    assert len(visible) == 1
    assert visible[0].source_id == "publisher"


def test_store_removes_historical_gdelt_noise(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    event = make_event().model_copy(
        update={
            "source": "gdelt-headlines",
            "title": "Man discovers Civil War-era cannonballs on his property",
        }
    )

    store.upsert([event])

    assert store.list_visible(now=event.available_at + timedelta(seconds=1)) == []


def test_store_removes_civil_war_veterans_cemetery_noise(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    event = make_event().model_copy(
        update={
            "source": "gdelt-headlines",
            "title": "Memorial Park Cemetery marks graves of Civil War veterans",
        }
    )

    store.upsert([event])

    assert store.list_visible(now=event.available_at + timedelta(seconds=1)) == []


def test_store_removes_gdelt_entertainment_war_noise(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    event = make_event().model_copy(
        update={
            "source": "gdelt-headlines",
            "title": "God of War release date confirmed for a physical disk",
        }
    )

    store.upsert([event])

    assert store.list_visible(now=event.available_at + timedelta(seconds=1)) == []


def test_store_demotes_stale_speculative_gdelt_rules_but_keeps_direct_action(tmp_path) -> None:
    path = tmp_path / "events.db"
    store = EventStore(path)
    speculative = make_event().model_copy(
        update={
            "source": "gdelt-headlines",
            "source_id": "factory",
            "title": "Ukraine intends to build drone and missile factory",
            "content": "Headline-only aggregated signal.",
            "analysis": "旧的高优先级规则结论",
            "market_relevance": "high",
            "analysis_method": "rules",
        }
    )
    direct_action = speculative.model_copy(
        update={
            "source_id": "attack",
            "title": "Russia fires missiles at Ukraine",
            "analysis": "直接冲突规则结论",
        }
    )
    no_chain = speculative.model_copy(
        update={
            "source_id": "oil-commentary",
            "title": "Crude oil prices extend losses on peace talks",
            "analysis": "无资产传导链却被标高优先级",
        }
    )
    store.upsert([speculative, direct_action, no_chain])

    visible = {
        event.source_id: event
        for event in store.list_visible(now=speculative.available_at + timedelta(seconds=1))
    }

    assert visible["factory"].market_relevance == "low"
    assert visible["factory"].impact_assets == []
    assert visible["oil-commentary"].market_relevance == "low"
    assert visible["oil-commentary"].impact_assets == []
    assert visible["attack"].market_relevance == "high"


def test_store_reclassifies_stale_rule_based_deescalation_headline(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    event = make_event().model_copy(
        update={
            "source": "gdelt-headlines",
            "source_id": "paused-attacks",
            "kind": EventKind.GEOPOLITICAL,
            "title": "Oil prices settle lower as US pauses attacks on Iran",
            "content": "Headline-level aggregation only.",
            "analysis": "旧的冲突升级规则结论",
            "market_relevance": "high",
            "impact_assets": [
                MarketImpact(asset="黄金", direction="up", reason="旧规则")
            ],
            "analysis_method": "rules",
        }
    )

    store.upsert([event])
    visible = store.list_visible(now=event.available_at + timedelta(seconds=1))[0]

    assert visible.market_relevance == "medium"
    assert {
        (impact.asset, impact.direction) for impact in visible.impact_assets
    } == {("原油", "down"), ("黄金", "down"), ("全球股票", "up")}
