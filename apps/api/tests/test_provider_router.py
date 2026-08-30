import pytest
from quantsieve_api.providers import ProviderRouter
from quantsieve_providers import BinanceProvider, Instrument


def test_provider_router_resolves_all_supported_markets(tmp_path) -> None:
    router = ProviderRouter(str(tmp_path / "cache.db"))

    assert router.resolve("600519").name == "akshare"
    assert router.resolve("AAPL").name == "yfinance"
    assert router.resolve("BTCUSDT").name == "binance"
    assert router.resolve("BTC").name == "binance"
    assert router.resolve("CL").name == "futures"
    assert router.resolve("NDX").name == "macro"
    assert router.resolve("^IXIC").name == "macro"
    assert router.resolve("EUR/USD").name == "macro"
    assert isinstance(router.binance_provider, BinanceProvider)


def test_provider_router_binance_accessor_fails_closed_when_replaced(
    tmp_path,
) -> None:
    router = ProviderRouter(str(tmp_path / "cache.db"))
    router.providers["binance"] = router.providers["yfinance"]

    with pytest.raises(RuntimeError, match="invalid type"):
        _ = router.binance_provider


@pytest.mark.asyncio
async def test_provider_router_keeps_alias_matches(monkeypatch, tmp_path) -> None:
    router = ProviderRouter(str(tmp_path / "cache.db"))

    async def no_matches(_query: str, _limit: int = 10):
        return []

    for provider in router.providers.values():
        monkeypatch.setattr(provider, "search", no_matches)

    results = await router.search("比特币")

    assert results[0].symbol == "BTCUSDT"


@pytest.mark.asyncio
async def test_provider_router_keeps_full_futures_directory_after_featured_match(
    monkeypatch, tmp_path
) -> None:
    router = ProviderRouter(str(tmp_path / "cache.db"))
    calls: list[str] = []

    async def futures_directory(query: str, _limit: int = 10) -> list[Instrument]:
        calls.append(query)
        return [
            Instrument(
                symbol="S",
                name="CBOT 大豆",
                market="FUTURES",
                exchange="CBOT",
                currency="USD",
                provider="futures",
                asset_type="commodity_future",
            )
        ]

    monkeypatch.setattr(router.providers["futures"], "search", futures_directory)

    results = await router.search("S", market="FUTURES")

    assert calls == ["S"]
    assert results[0].symbol == "S"
