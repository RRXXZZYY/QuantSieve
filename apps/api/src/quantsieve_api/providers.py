from __future__ import annotations

import asyncio
from typing import Literal

from quantsieve_providers import (
    AKShareProvider,
    BinanceProvider,
    DataProvider,
    FuturesProvider,
    Instrument,
    MacroProvider,
    MarketFilter,
    SQLiteCache,
    YFinanceProvider,
    rank_instruments,
    search_popular_instruments,
)

RequestedProvider = Literal[
    "auto", "akshare", "yfinance", "binance", "futures", "macro"
]
CRYPTO_QUOTES = ("USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB")
CRYPTO_BASES = {
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
FUTURES_SYMBOLS = {
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


class ProviderRouter:
    def __init__(self, cache_path: str) -> None:
        cache = SQLiteCache(cache_path)
        self.providers: dict[str, DataProvider] = {
            "akshare": AKShareProvider(cache),
            "yfinance": YFinanceProvider(cache),
            "binance": BinanceProvider(cache),
            "futures": FuturesProvider(cache),
            "macro": MacroProvider(cache),
        }

    @property
    def binance_provider(self) -> BinanceProvider:
        """Return the execution-capable Binance provider or fail closed."""

        provider = self.providers.get("binance")
        if not isinstance(provider, BinanceProvider):
            raise RuntimeError(
                "The Binance provider binding is missing or has an invalid type."
            )
        return provider

    def resolve(
        self,
        symbol: str,
        requested: RequestedProvider = "auto",
    ) -> DataProvider:
        if requested != "auto":
            return self.providers[requested]
        normalized = symbol.strip().upper()
        compact = normalized.replace("/", "").replace("-", "")
        if MacroProvider.supports(normalized):
            provider_name = "macro"
        elif normalized.isdigit():
            provider_name = "akshare"
        elif normalized in CRYPTO_BASES or any(
            compact.endswith(quote) and len(compact) > len(quote) for quote in CRYPTO_QUOTES
        ):
            provider_name = "binance"
        elif normalized in FUTURES_SYMBOLS:
            provider_name = "futures"
        else:
            provider_name = "yfinance"
        return self.providers[provider_name]

    async def search(
        self,
        query: str = "",
        *,
        market: MarketFilter = "all",
        limit: int = 10,
    ) -> list[Instrument]:
        normalized = query.strip()
        builtins = search_popular_instruments(normalized, market=market, limit=limit)
        # The crypto and futures pickers are directories, rather than a short
        # closed watchlist.  A fuzzy featured hit (for example ``SI`` for a
        # search for ``S``) must not hide the upstream directory's exact
        # contract or Binance pair.  Other markets keep the fast featured
        # path, because their broad public searches are comparatively slow.
        directory_markets = {"CRYPTO", "FUTURES"}
        if not normalized or (builtins and market not in directory_markets):
            return builtins

        provider_names: list[str]
        if market == "CN":
            provider_names = ["akshare"]
        elif market == "US":
            provider_names = ["yfinance"]
        elif market == "CRYPTO":
            provider_names = ["binance"]
        elif market == "FUTURES":
            provider_names = ["futures"]
        elif market in {"INDEX", "FOREX"}:
            provider_names = ["macro"]
        elif market == "ETF":
            provider_names = ["yfinance"]
        else:
            provider_names = [
                "akshare",
                "yfinance",
                "binance",
                "futures",
                "macro",
            ]

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
        return rank_instruments(
            instruments,
            normalized,
            limit=limit,
            include_unmatched=True,
        )
