from __future__ import annotations

from typing import Any, cast

from mcp.server.fastmcp import FastMCP
from quantsieve_providers import MarketFilter

from .service import MarketDataService

mcp = FastMCP(
    "QuantSieve Market Data",
    instructions=(
        "Use these tools for sourced A-share, US stock, Binance spot, "
        "and global commodity-futures data. "
        "Preserve every citations field in answers and never invent missing values."
    ),
)
service = MarketDataService()


@mcp.tool()
async def search_market_instruments(
    query: str = "",
    market: str = "all",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search all supported asset classes by name or symbol."""
    if market not in {"all", "CN", "US", "CRYPTO", "FUTURES"}:
        raise ValueError("market must be one of: all, CN, US, CRYPTO, FUTURES")
    typed_market = cast(MarketFilter, market)
    return await service.search(query, typed_market, limit)


@mcp.tool()
async def get_market_history(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Get sourced adjusted daily OHLCV. Dates use YYYY-MM-DD."""
    return await service.history(symbol, start, end)


@mcp.tool()
async def get_latest_quote(symbol: str) -> dict[str, Any]:
    """Get the latest sourced quote for any supported instrument."""
    return await service.quote(symbol)


@mcp.tool()
async def get_fundamentals(symbol: str) -> dict[str, Any]:
    """Get sourced public financial-statement data."""
    return await service.fundamentals(symbol)


@mcp.tool()
async def get_capital_flow(symbol: str) -> dict[str, Any]:
    """Get sourced A-share capital-flow data when supported."""
    return await service.capital_flow(symbol)


@mcp.tool()
async def get_company_news(symbol: str, limit: int = 10) -> dict[str, Any]:
    """Get up to twenty recent sourced company-news items."""
    return await service.news(symbol, limit)


def main() -> None:
    mcp.run(transport="stdio")
