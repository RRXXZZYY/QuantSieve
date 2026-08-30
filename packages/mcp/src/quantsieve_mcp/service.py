from __future__ import annotations

import asyncio
import re
from datetime import date
from typing import Any

from quantsieve_providers import (
    AKShareProvider,
    BinanceProvider,
    DataProvider,
    FuturesProvider,
    MarketFilter,
    SQLiteCache,
    YFinanceProvider,
    rank_instruments,
    search_popular_instruments,
)


class MarketDataService:
    def __init__(
        self,
        *,
        cache_path: str = "data/mcp-cache.db",
        providers: dict[str, DataProvider] | None = None,
    ) -> None:
        if providers is not None:
            self.providers = providers
        else:
            cache = SQLiteCache(cache_path)
            self.providers = {
                "akshare": AKShareProvider(cache),
                "yfinance": YFinanceProvider(cache),
                "binance": BinanceProvider(cache),
                "futures": FuturesProvider(cache),
            }

    def resolve(self, symbol: str) -> DataProvider:
        normalized = symbol.strip().upper()
        compact = normalized.replace("/", "").replace("-", "")
        crypto_bases = {
            "BTC",
            "ETH",
            "BNB",
            "SOL",
            "XRP",
            "DOGE",
            "ADA",
            "AVAX",
            "LINK",
            "DOT",
            "TRX",
            "LTC",
            "SUI",
            "TON",
        }
        crypto_quotes = ("USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB")
        futures_symbols = {
            "FEF",
            "FCPO",
            "RSS3",
            "RS",
            "CT",
            "NID",
            "PBD",
            "SND",
            "ZSD",
            "AHD",
            "CAD",
            "S",
            "W",
            "C",
            "BO",
            "SM",
            "TRB",
            "HG",
            "NG",
            "CL",
            "SI",
            "GC",
            "LHC",
            "OIL",
            "XAU",
            "XAG",
            "XPT",
            "XPD",
            "EUA",
        }
        if normalized.isdigit():
            provider = "akshare"
        elif normalized in crypto_bases or any(
            compact.endswith(quote) and len(compact) > len(quote) for quote in crypto_quotes
        ):
            provider = "binance"
        elif normalized in futures_symbols:
            provider = "futures"
        else:
            provider = "yfinance"
        return self.providers.get(provider, self.providers["yfinance"])

    async def search(
        self,
        query: str = "",
        market: MarketFilter = "all",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 20))
        normalized = query.strip()
        builtins = search_popular_instruments(normalized, market=market, limit=limit)
        if not normalized or builtins:
            return [instrument.model_dump(mode="json") for instrument in builtins]

        if market == "CN":
            provider_names = ["akshare"]
        elif market == "US":
            provider_names = ["yfinance"]
        elif market == "CRYPTO":
            provider_names = ["binance"]
        elif market == "FUTURES":
            provider_names = ["futures"]
        else:
            provider_names = ["akshare", "yfinance", "binance", "futures"]
        provider_names = [name for name in provider_names if name in self.providers]
        results = await asyncio.gather(
            *[
                asyncio.wait_for(
                    self.providers[name].search(normalized, limit * 2),
                    timeout=5,
                )
                for name in provider_names
            ],
            return_exceptions=True,
        )
        instruments = list(builtins)
        for result in results:
            if isinstance(result, list):
                instruments.extend(result)
        ranked = rank_instruments(
            instruments,
            normalized,
            limit=limit,
            include_unmatched=True,
        )
        return [instrument.model_dump(mode="json") for instrument in ranked]

    async def normalize_symbol(self, value: str) -> str:
        candidate = value.strip()
        if candidate.upper() in {
            "BTC",
            "ETH",
            "BNB",
            "SOL",
            "XRP",
            "DOGE",
            "ADA",
            "AVAX",
            "LINK",
            "DOT",
            "TRX",
            "LTC",
            "SUI",
            "TON",
        }:
            return f"{candidate.upper()}USDT"
        is_explicit_ticker = bool(
            candidate.isdigit()
            or (
                candidate == candidate.upper()
                and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", candidate)
            )
        )
        if is_explicit_ticker:
            return candidate.upper()
        matches = await self.search(candidate, limit=1)
        if not matches:
            raise ValueError(
                f"No instrument matched {value!r}; call search_market_instruments first."
            )
        return str(matches[0]["symbol"])

    async def history(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        resolved_symbol = await self.normalize_symbol(symbol)
        envelope = await self.resolve(resolved_symbol).history(
            resolved_symbol,
            date.fromisoformat(start) if start else None,
            date.fromisoformat(end) if end else None,
        )
        return envelope.model_dump(mode="json")

    async def quote(self, symbol: str) -> dict[str, Any]:
        resolved_symbol = await self.normalize_symbol(symbol)
        return (
            await self.resolve(resolved_symbol).quote(resolved_symbol)
        ).model_dump(mode="json")

    async def fundamentals(self, symbol: str) -> dict[str, Any]:
        resolved_symbol = await self.normalize_symbol(symbol)
        return (
            await self.resolve(resolved_symbol).fundamentals(resolved_symbol)
        ).model_dump(mode="json")

    async def capital_flow(self, symbol: str) -> dict[str, Any]:
        resolved_symbol = await self.normalize_symbol(symbol)
        return (
            await self.resolve(resolved_symbol).capital_flow(resolved_symbol)
        ).model_dump(mode="json")

    async def news(self, symbol: str, limit: int = 10) -> dict[str, Any]:
        resolved_symbol = await self.normalize_symbol(symbol)
        return (
            await self.resolve(resolved_symbol).news(
                resolved_symbol, max(1, min(limit, 20))
            )
        ).model_dump(mode="json")
