from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from .models import Instrument

MarketFilter = Literal[
    "all", "CN", "US", "ETF", "INDEX", "FOREX", "CRYPTO", "FUTURES"
]


@dataclass(frozen=True)
class CatalogEntry:
    instrument: Instrument
    aliases: tuple[str, ...] = ()


POPULAR_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        Instrument(
            symbol="300750",
            name="宁德时代",
            market="CN",
            exchange="深圳证券交易所",
            currency="CNY",
            provider="akshare",
        ),
        ("CATL", "宁德"),
    ),
    CatalogEntry(
        Instrument(
            symbol="600519",
            name="贵州茅台",
            market="CN",
            exchange="上海证券交易所",
            currency="CNY",
            provider="akshare",
        ),
        ("茅台", "Kweichow Moutai"),
    ),
    CatalogEntry(
        Instrument(
            symbol="002594",
            name="比亚迪",
            market="CN",
            exchange="深圳证券交易所",
            currency="CNY",
            provider="akshare",
        ),
        ("BYD",),
    ),
    CatalogEntry(
        Instrument(
            symbol="000858",
            name="五粮液",
            market="CN",
            exchange="深圳证券交易所",
            currency="CNY",
            provider="akshare",
        )
    ),
    CatalogEntry(
        Instrument(
            symbol="601318",
            name="中国平安",
            market="CN",
            exchange="上海证券交易所",
            currency="CNY",
            provider="akshare",
        ),
        ("平安", "Ping An"),
    ),
    CatalogEntry(
        Instrument(
            symbol="600036",
            name="招商银行",
            market="CN",
            exchange="上海证券交易所",
            currency="CNY",
            provider="akshare",
        ),
        ("招行", "CMB"),
    ),
    CatalogEntry(
        Instrument(
            symbol="000001",
            name="平安银行",
            market="CN",
            exchange="深圳证券交易所",
            currency="CNY",
            provider="akshare",
        )
    ),
    CatalogEntry(
        Instrument(
            symbol="601899",
            name="紫金矿业",
            market="CN",
            exchange="上海证券交易所",
            currency="CNY",
            provider="akshare",
        )
    ),
    CatalogEntry(
        Instrument(
            symbol="600900",
            name="长江电力",
            market="CN",
            exchange="上海证券交易所",
            currency="CNY",
            provider="akshare",
        )
    ),
    CatalogEntry(
        Instrument(
            symbol="300059",
            name="东方财富",
            market="CN",
            exchange="深圳证券交易所",
            currency="CNY",
            provider="akshare",
        ),
        ("东财",),
    ),
    CatalogEntry(
        Instrument(
            symbol="600276",
            name="恒瑞医药",
            market="CN",
            exchange="上海证券交易所",
            currency="CNY",
            provider="akshare",
        )
    ),
    CatalogEntry(
        Instrument(
            symbol="000333",
            name="美的集团",
            market="CN",
            exchange="深圳证券交易所",
            currency="CNY",
            provider="akshare",
        ),
        ("美的", "Midea"),
    ),
    CatalogEntry(
        Instrument(
            symbol="002415",
            name="海康威视",
            market="CN",
            exchange="深圳证券交易所",
            currency="CNY",
            provider="akshare",
        ),
        ("海康", "Hikvision"),
    ),
    CatalogEntry(
        Instrument(
            symbol="688981",
            name="中芯国际",
            market="CN",
            exchange="上海证券交易所",
            currency="CNY",
            provider="akshare",
        ),
        ("SMIC",),
    ),
    CatalogEntry(
        Instrument(
            symbol="AAPL",
            name="Apple Inc.",
            market="US",
            exchange="NASDAQ",
            currency="USD",
            provider="yfinance",
        ),
        ("Apple", "苹果"),
    ),
    CatalogEntry(
        Instrument(
            symbol="MSFT",
            name="Microsoft Corporation",
            market="US",
            exchange="NASDAQ",
            currency="USD",
            provider="yfinance",
        ),
        ("Microsoft", "微软"),
    ),
    CatalogEntry(
        Instrument(
            symbol="NVDA",
            name="NVIDIA Corporation",
            market="US",
            exchange="NASDAQ",
            currency="USD",
            provider="yfinance",
        ),
        ("NVIDIA", "英伟达"),
    ),
    CatalogEntry(
        Instrument(
            symbol="AMZN",
            name="Amazon.com, Inc.",
            market="US",
            exchange="NASDAQ",
            currency="USD",
            provider="yfinance",
        ),
        ("Amazon", "亚马逊"),
    ),
    CatalogEntry(
        Instrument(
            symbol="GOOGL",
            name="Alphabet Inc.",
            market="US",
            exchange="NASDAQ",
            currency="USD",
            provider="yfinance",
        ),
        ("Google", "谷歌", "Alphabet"),
    ),
    CatalogEntry(
        Instrument(
            symbol="META",
            name="Meta Platforms, Inc.",
            market="US",
            exchange="NASDAQ",
            currency="USD",
            provider="yfinance",
        ),
        ("Facebook", "脸书", "Meta"),
    ),
    CatalogEntry(
        Instrument(
            symbol="TSLA",
            name="Tesla, Inc.",
            market="US",
            exchange="NASDAQ",
            currency="USD",
            provider="yfinance",
        ),
        ("Tesla", "特斯拉"),
    ),
    CatalogEntry(
        Instrument(
            symbol="BRK-B",
            name="Berkshire Hathaway Inc.",
            market="US",
            exchange="NYSE",
            currency="USD",
            provider="yfinance",
        ),
        ("Berkshire", "伯克希尔", "巴菲特"),
    ),
    CatalogEntry(
        Instrument(
            symbol="JPM",
            name="JPMorgan Chase & Co.",
            market="US",
            exchange="NYSE",
            currency="USD",
            provider="yfinance",
        ),
        ("JPMorgan", "摩根大通"),
    ),
    CatalogEntry(
        Instrument(
            symbol="AMD",
            name="Advanced Micro Devices, Inc.",
            market="US",
            exchange="NASDAQ",
            currency="USD",
            provider="yfinance",
        ),
        ("AMD", "超威半导体"),
    ),
    CatalogEntry(
        Instrument(
            symbol="BABA",
            name="Alibaba Group Holding Limited",
            market="US",
            exchange="NYSE",
            currency="USD",
            provider="yfinance",
        ),
        ("Alibaba", "阿里巴巴", "阿里"),
    ),
    CatalogEntry(
        Instrument(
            symbol="PDD",
            name="PDD Holdings Inc.",
            market="US",
            exchange="NASDAQ",
            currency="USD",
            provider="yfinance",
        ),
        ("Pinduoduo", "拼多多"),
    ),
    CatalogEntry(
        Instrument(
            symbol="SPY",
            name="SPDR S&P 500 ETF",
            market="ETF",
            exchange="NYSE Arca",
            currency="USD",
            provider="yfinance",
            asset_type="etf",
        ),
        ("标普500 ETF", "S&P 500 ETF"),
    ),
    CatalogEntry(
        Instrument(
            symbol="QQQ",
            name="Invesco QQQ Trust",
            market="ETF",
            exchange="NASDAQ",
            currency="USD",
            provider="yfinance",
            asset_type="etf",
        ),
        ("纳斯达克100 ETF", "Nasdaq 100 ETF"),
    ),
    CatalogEntry(
        Instrument(
            symbol="DIA",
            name="SPDR Dow Jones ETF",
            market="ETF",
            exchange="NYSE Arca",
            currency="USD",
            provider="yfinance",
            asset_type="etf",
        ),
        ("道琼斯 ETF", "Dow Jones ETF"),
    ),
    CatalogEntry(
        Instrument(
            symbol="IWM",
            name="iShares Russell 2000 ETF",
            market="ETF",
            exchange="NYSE Arca",
            currency="USD",
            provider="yfinance",
            asset_type="etf",
        ),
        ("罗素2000 ETF", "Russell 2000 ETF"),
    ),
    CatalogEntry(
        Instrument(
            symbol="VTI",
            name="Vanguard Total Stock Market ETF",
            market="ETF",
            exchange="NYSE Arca",
            currency="USD",
            provider="yfinance",
            asset_type="etf",
        ),
        ("美国全市场 ETF", "Total US Market"),
    ),
    CatalogEntry(
        Instrument(
            symbol="VOO",
            name="Vanguard S&P 500 ETF",
            market="ETF",
            exchange="NYSE Arca",
            currency="USD",
            provider="yfinance",
            asset_type="etf",
        ),
        ("先锋标普500", "Vanguard S&P 500"),
    ),
    CatalogEntry(
        Instrument(
            symbol="GLD",
            name="SPDR Gold Shares",
            market="ETF",
            exchange="NYSE Arca",
            currency="USD",
            provider="yfinance",
            asset_type="etf",
        ),
        ("黄金 ETF", "Gold ETF"),
    ),
    CatalogEntry(
        Instrument(
            symbol="TLT",
            name="iShares 20+ Year Treasury Bond ETF",
            market="ETF",
            exchange="NASDAQ",
            currency="USD",
            provider="yfinance",
            asset_type="etf",
        ),
        ("美国长期国债 ETF", "Treasury ETF"),
    ),
    CatalogEntry(
        Instrument(
            symbol="USO",
            name="United States Oil Fund",
            market="ETF",
            exchange="NYSE Arca",
            currency="USD",
            provider="yfinance",
            asset_type="etf",
        ),
        ("原油 ETF", "Oil ETF"),
    ),
    CatalogEntry(
        Instrument(
            symbol="IXIC",
            name="NASDAQ Composite Index",
            market="INDEX",
            exchange="Nasdaq",
            currency="USD",
            provider="macro",
            asset_type="index_reference",
        ),
        ("纳斯达克综合指数", "NASDAQ Composite", "^IXIC"),
    ),
    CatalogEntry(
        Instrument(
            symbol="NDX",
            name="NASDAQ 100 Index",
            market="INDEX",
            exchange="Nasdaq",
            currency="USD",
            provider="macro",
            asset_type="index_reference",
        ),
        ("纳斯达克100", "NASDAQ 100", "^NDX"),
    ),
    CatalogEntry(
        Instrument(
            symbol="VIX",
            name="CBOE Volatility Index",
            market="INDEX",
            exchange="Cboe Global Indices",
            currency="INDEX",
            provider="macro",
            asset_type="index_reference",
        ),
        ("恐慌指数", "波动率指数", "^VIX"),
    ),
    CatalogEntry(
        Instrument(
            symbol="EURUSD",
            name="Euro / US Dollar",
            market="FOREX",
            exchange="ECB daily reference rate",
            currency="USD",
            provider="macro",
            asset_type="forex_reference",
        ),
        ("EUR/USD", "欧元美元", "欧元兑美元"),
    ),
    CatalogEntry(
        Instrument(
            symbol="USDJPY",
            name="US Dollar / Japanese Yen",
            market="FOREX",
            exchange="ECB daily reference rate",
            currency="JPY",
            provider="macro",
            asset_type="forex_reference",
        ),
        ("USD/JPY", "美元日元", "美元兑日元"),
    ),
    CatalogEntry(
        Instrument(
            symbol="GBPUSD",
            name="British Pound / US Dollar",
            market="FOREX",
            exchange="ECB daily reference rate",
            currency="USD",
            provider="macro",
            asset_type="forex_reference",
        ),
        ("GBP/USD", "英镑美元", "英镑兑美元"),
    ),
    CatalogEntry(
        Instrument(
            symbol="AUDUSD",
            name="Australian Dollar / US Dollar",
            market="FOREX",
            exchange="ECB daily reference rate",
            currency="USD",
            provider="macro",
            asset_type="forex_reference",
        ),
        ("AUD/USD", "澳元美元", "澳元兑美元"),
    ),
    CatalogEntry(
        Instrument(
            symbol="USDCAD",
            name="US Dollar / Canadian Dollar",
            market="FOREX",
            exchange="ECB daily reference rate",
            currency="CAD",
            provider="macro",
            asset_type="forex_reference",
        ),
        ("USD/CAD", "美元加元", "美元兑加元"),
    ),
    CatalogEntry(
        Instrument(
            symbol="USDCHF",
            name="US Dollar / Swiss Franc",
            market="FOREX",
            exchange="ECB daily reference rate",
            currency="CHF",
            provider="macro",
            asset_type="forex_reference",
        ),
        ("USD/CHF", "美元瑞郎", "美元兑瑞郎"),
    ),
    CatalogEntry(
        Instrument(
            symbol="NZDUSD",
            name="New Zealand Dollar / US Dollar",
            market="FOREX",
            exchange="ECB daily reference rate",
            currency="USD",
            provider="macro",
            asset_type="forex_reference",
        ),
        ("NZD/USD", "纽元美元", "纽元兑美元"),
    ),
    CatalogEntry(
        Instrument(
            symbol="USDCNY",
            name="US Dollar / Chinese Yuan",
            market="FOREX",
            exchange="ECB daily reference rate",
            currency="CNY",
            provider="macro",
            asset_type="forex_reference",
        ),
        ("USD/CNY", "美元人民币", "美元兑人民币"),
    ),
    CatalogEntry(
        Instrument(
            symbol="BTCUSDT",
            name="Bitcoin / USDT",
            market="CRYPTO",
            exchange="Binance Spot",
            currency="USDT",
            provider="binance",
            asset_type="spot",
        ),
        ("BTC", "Bitcoin", "比特币"),
    ),
    CatalogEntry(
        Instrument(
            symbol="ETHUSDT",
            name="Ethereum / USDT",
            market="CRYPTO",
            exchange="Binance Spot",
            currency="USDT",
            provider="binance",
            asset_type="spot",
        ),
        ("ETH", "Ethereum", "Ether", "以太坊"),
    ),
    CatalogEntry(
        Instrument(
            symbol="BNBUSDT",
            name="BNB / USDT",
            market="CRYPTO",
            exchange="Binance Spot",
            currency="USDT",
            provider="binance",
            asset_type="spot",
        ),
        ("BNB", "币安币"),
    ),
    CatalogEntry(
        Instrument(
            symbol="SOLUSDT",
            name="Solana / USDT",
            market="CRYPTO",
            exchange="Binance Spot",
            currency="USDT",
            provider="binance",
            asset_type="spot",
        ),
        ("SOL", "Solana", "索拉纳"),
    ),
    CatalogEntry(
        Instrument(
            symbol="XRPUSDT",
            name="XRP / USDT",
            market="CRYPTO",
            exchange="Binance Spot",
            currency="USDT",
            provider="binance",
            asset_type="spot",
        ),
        ("XRP", "Ripple", "瑞波"),
    ),
    CatalogEntry(
        Instrument(
            symbol="DOGEUSDT",
            name="Dogecoin / USDT",
            market="CRYPTO",
            exchange="Binance Spot",
            currency="USDT",
            provider="binance",
            asset_type="spot",
        ),
        ("DOGE", "Dogecoin", "狗狗币"),
    ),
    CatalogEntry(
        Instrument(
            symbol="CL",
            name="NYMEX 原油",
            market="FUTURES",
            exchange="NYMEX",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("WTI", "Crude Oil", "美原油", "纽约原油", "石油"),
    ),
    CatalogEntry(
        Instrument(
            symbol="OIL",
            name="布伦特原油",
            market="FUTURES",
            exchange="ICE",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("Brent", "Brent Crude", "布油", "原油", "石油"),
    ),
    CatalogEntry(
        Instrument(
            symbol="GC",
            name="COMEX 黄金",
            market="FUTURES",
            exchange="COMEX",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("Gold", "黄金期货", "美黄金"),
    ),
    CatalogEntry(
        Instrument(
            symbol="SI",
            name="COMEX 白银",
            market="FUTURES",
            exchange="COMEX",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("Silver", "白银期货", "美白银"),
    ),
    CatalogEntry(
        Instrument(
            symbol="NG",
            name="NYMEX 天然气",
            market="FUTURES",
            exchange="NYMEX",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("Natural Gas", "天然气期货"),
    ),
    CatalogEntry(
        Instrument(
            symbol="HG",
            name="COMEX 铜",
            market="FUTURES",
            exchange="COMEX",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("Copper", "铜期货", "美铜"),
    ),
    CatalogEntry(
        Instrument(
            symbol="S",
            name="CBOT 大豆",
            market="FUTURES",
            exchange="CBOT",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("Soybean", "Soybeans", "大豆期货", "美豆", "黄豆"),
    ),
    CatalogEntry(
        Instrument(
            symbol="W",
            name="CBOT 小麦",
            market="FUTURES",
            exchange="CBOT",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("Wheat", "小麦期货", "美小麦"),
    ),
    CatalogEntry(
        Instrument(
            symbol="C",
            name="CBOT 玉米",
            market="FUTURES",
            exchange="CBOT",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("Corn", "玉米期货", "美玉米"),
    ),
    CatalogEntry(
        Instrument(
            symbol="CT",
            name="NYBOT 棉花",
            market="FUTURES",
            exchange="NYBOT",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("Cotton", "棉花期货", "美棉"),
    ),
    CatalogEntry(
        Instrument(
            symbol="LHC",
            name="CME 瘦肉猪",
            market="FUTURES",
            exchange="CME",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("Lean Hog", "Lean Hogs", "瘦肉猪期货"),
    ),
    CatalogEntry(
        Instrument(
            symbol="FEF",
            name="新加坡铁矿石",
            market="FUTURES",
            exchange="SGX",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("Iron Ore", "铁矿石期货"),
    ),
    CatalogEntry(
        Instrument(
            symbol="XAU",
            name="伦敦金",
            market="FUTURES",
            exchange="London Spot",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("London Gold", "现货黄金"),
    ),
    CatalogEntry(
        Instrument(
            symbol="XAG",
            name="伦敦银",
            market="FUTURES",
            exchange="London Spot",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("London Silver", "现货白银"),
    ),
    CatalogEntry(
        Instrument(
            symbol="XPT",
            name="伦敦铂金",
            market="FUTURES",
            exchange="London Spot",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("London Platinum", "现货铂金"),
    ),
    CatalogEntry(
        Instrument(
            symbol="XPD",
            name="伦敦钯金",
            market="FUTURES",
            exchange="London Spot",
            currency="USD",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("London Palladium", "现货钯金"),
    ),
    CatalogEntry(
        Instrument(
            symbol="EUA",
            name="欧洲碳排放",
            market="FUTURES",
            exchange="ICE",
            currency="EUR",
            provider="futures",
            asset_type="commodity_future",
        ),
        ("EU Carbon", "Carbon Allowance", "碳排放期货", "欧盟碳"),
    ),
)


def normalize_search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"[\s._/\\-]+", "", normalized)


def instrument_score(instrument: Instrument, query: str, aliases: tuple[str, ...] = ()) -> int:
    needle = normalize_search_text(query)
    if not needle:
        return 0
    symbol = normalize_search_text(instrument.symbol)
    name = normalize_search_text(instrument.name)
    alias_values = [normalize_search_text(alias) for alias in aliases]
    if needle == symbol:
        return 0
    if needle == name or needle in alias_values:
        return 1
    if symbol.startswith(needle):
        return 2
    if name.startswith(needle) or any(alias.startswith(needle) for alias in alias_values):
        return 3
    if needle in symbol:
        return 4
    if needle in name or any(needle in alias for alias in alias_values):
        return 5
    return 100


def search_popular_instruments(
    query: str = "",
    *,
    market: MarketFilter = "all",
    limit: int = 10,
) -> list[Instrument]:
    if not query and market == "all":
        groups = {
            market_name: [
                entry.instrument
                for entry in POPULAR_CATALOG
                if entry.instrument.market == market_name
            ]
            for market_name in (
                "CN",
                "US",
                "ETF",
                "INDEX",
                "FOREX",
                "CRYPTO",
                "FUTURES",
            )
        }
        featured: list[Instrument] = []
        index = 0
        while len(featured) < limit and any(index < len(group) for group in groups.values()):
            for group in groups.values():
                if index < len(group):
                    featured.append(group[index])
                    if len(featured) == limit:
                        break
            index += 1
        return featured
    matches = [
        (instrument_score(entry.instrument, query, entry.aliases), index, entry.instrument)
        for index, entry in enumerate(POPULAR_CATALOG)
        if market == "all" or entry.instrument.market == market
    ]
    if query:
        matches = [match for match in matches if match[0] < 100]
    matches.sort(key=lambda item: (item[0], item[1]))
    return [instrument for _, _, instrument in matches[:limit]]


def find_popular_instrument_mentions(
    text: str,
    *,
    market: MarketFilter = "all",
    limit: int = 5,
) -> list[Instrument]:
    """Resolve catalog instruments mentioned inside a longer natural-language question."""
    normalized_text = unicodedata.normalize("NFKC", text).casefold()
    matches: list[tuple[int, int, Instrument]] = []
    for index, entry in enumerate(POPULAR_CATALOG):
        if market != "all" and entry.instrument.market != market:
            continue
        candidates = (
            entry.instrument.symbol,
            entry.instrument.name,
            *entry.aliases,
        )
        matched_lengths = [
            len(candidate)
            for candidate in candidates
            if _contains_mention(normalized_text, candidate)
        ]
        if matched_lengths:
            matches.append((-max(matched_lengths), index, entry.instrument))
    matches.sort(key=lambda item: (item[0], item[1]))
    return [instrument for _, _, instrument in matches[:limit]]


def rank_instruments(
    instruments: list[Instrument],
    query: str,
    *,
    limit: int,
    include_unmatched: bool = False,
) -> list[Instrument]:
    unique: dict[tuple[str, str], Instrument] = {}
    for instrument in instruments:
        unique.setdefault((instrument.market, instrument.symbol), instrument)
    ranked = sorted(
        unique.values(),
        key=lambda instrument: (
            instrument_score(instrument, query),
            (
                {"USDT": 0, "USDC": 1, "FDUSD": 2, "BTC": 3}.get(
                    instrument.currency,
                    4,
                )
                if instrument.market == "CRYPTO"
                else 0
            ),
            instrument.market,
            instrument.symbol,
        ),
    )
    if include_unmatched:
        return ranked[:limit]
    return [
        instrument for instrument in ranked if instrument_score(instrument, query) < 100
    ][:limit]


def _contains_mention(normalized_text: str, candidate: str) -> bool:
    normalized_candidate = unicodedata.normalize("NFKC", candidate).casefold().strip()
    if not normalized_candidate:
        return False
    if normalized_candidate.isascii():
        return (
            re.search(
                rf"(?<![a-z0-9]){re.escape(normalized_candidate)}(?![a-z0-9])",
                normalized_text,
            )
            is not None
        )
    return normalized_candidate in normalized_text
