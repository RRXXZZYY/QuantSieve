from datetime import timedelta

from quantsieve_providers import SQLiteCache


def test_cache_round_trip_and_expiry(tmp_path) -> None:
    cache = SQLiteCache(tmp_path / "cache.db")
    cache.set("fresh", {"answer": 42}, timedelta(minutes=1))
    cache.set("expired", {"answer": 0}, timedelta(seconds=-1))

    assert cache.get("fresh") == {"answer": 42}
    assert cache.get("expired") is None


def test_cache_batch_round_trip_deduplicates_and_drops_expired_values(
    tmp_path,
) -> None:
    cache = SQLiteCache(tmp_path / "cache.db")
    cache.set_many(
        {
            "one": {"translation": "一"},
            "two": {"translation": "二"},
        },
        timedelta(minutes=1),
    )
    cache.set("expired", {"translation": "旧"}, timedelta(seconds=-1))

    assert cache.get_many(["two", "one", "two", "missing", "expired"]) == {
        "one": {"translation": "一"},
        "two": {"translation": "二"},
    }
    assert cache.get("expired") is None
