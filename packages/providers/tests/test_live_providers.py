from datetime import date, timedelta

import pytest
from quantsieve_providers import (
    AKShareProvider,
    BinanceProvider,
    FuturesProvider,
    SECEdgarProvider,
    SQLiteCache,
    YFinanceProvider,
)


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["300750", "600519"])
async def test_live_a_share_history(tmp_path, symbol: str) -> None:
    provider = AKShareProvider(SQLiteCache(tmp_path / "cache.db"))
    result = await provider.history(
        symbol,
        date.today() - timedelta(days=30),
        date.today(),
    )

    assert result.rows
    assert result.citations[0].source == "AKShare"


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_us_history(tmp_path) -> None:
    provider = YFinanceProvider(SQLiteCache(tmp_path / "cache.db"))
    result = await provider.history(
        "AAPL",
        date.today() - timedelta(days=30),
        date.today(),
    )

    assert result.rows
    assert result.citations[0].source in {
        "Yahoo Finance",
        "Tencent Finance",
        "Eastmoney",
    }


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_binance_history(tmp_path) -> None:
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))
    result = await provider.history(
        "BTCUSDT",
        date.today() - timedelta(days=7),
        date.today(),
    )

    assert result.rows
    assert result.citations[0].source == "Binance Spot"


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_global_futures_history(tmp_path) -> None:
    provider = FuturesProvider(SQLiteCache(tmp_path / "cache.db"))
    result = await provider.history(
        "CL",
        date.today() - timedelta(days=14),
        date.today(),
    )

    assert result.rows
    assert result.citations[0].source == "AKShare / Sina Finance"


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_berkshire_13f(tmp_path) -> None:
    provider = SECEdgarProvider(
        "QuantSieve integration-test contact@example.com",
        SQLiteCache(tmp_path / "cache.db"),
    )
    result = await provider.latest_13f("1067983", "Berkshire Hathaway")

    assert result.rows
    assert result.metadata["accession_number"]
    assert result.citations[0].source == "SEC EDGAR"
