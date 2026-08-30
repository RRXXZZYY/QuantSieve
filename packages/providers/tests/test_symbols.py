from quantsieve_providers import (
    Instrument,
    find_popular_instrument_mentions,
    rank_instruments,
    search_popular_instruments,
)


def test_searches_popular_instruments_by_chinese_name() -> None:
    results = search_popular_instruments("宁德时代")

    assert results[0].symbol == "300750"
    assert results[0].provider == "akshare"


def test_searches_us_instruments_by_ticker_and_chinese_alias() -> None:
    assert search_popular_instruments("AAPL")[0].name == "Apple Inc."
    assert search_popular_instruments("特斯拉")[0].symbol == "TSLA"


def test_filters_popular_instruments_by_market() -> None:
    results = search_popular_instruments("", market="US", limit=5)

    assert len(results) == 5
    assert all(instrument.market == "US" for instrument in results)


def test_searches_crypto_and_global_futures() -> None:
    assert search_popular_instruments("比特币")[0].symbol == "BTCUSDT"
    assert search_popular_instruments("原油")[0].provider == "futures"
    assert search_popular_instruments("Gold")[0].symbol == "GC"
    assert search_popular_instruments("玉米")[0].symbol == "C"
    assert search_popular_instruments("铁矿石")[0].symbol == "FEF"


def test_searches_etfs_indices_and_forex_by_name_or_alias() -> None:
    assert search_popular_instruments("标普500 ETF")[0].symbol == "SPY"
    assert search_popular_instruments("纳斯达克综合指数")[0].symbol == "IXIC"
    assert search_popular_instruments("EUR/USD")[0].symbol == "EURUSD"
    assert search_popular_instruments("美元兑人民币")[0].symbol == "USDCNY"


def test_default_catalog_represents_all_markets() -> None:
    results = search_popular_instruments("", limit=8)

    assert {instrument.market for instrument in results} == {
        "CN",
        "US",
        "ETF",
        "INDEX",
        "FOREX",
        "CRYPTO",
        "FUTURES",
    }


def test_crypto_ranking_prefers_usdt_pairs() -> None:
    instruments = [
        Instrument(
            symbol="PEPEBRL",
            name="PEPE / BRL",
            market="CRYPTO",
            exchange="Binance Spot",
            currency="BRL",
            provider="binance",
            asset_type="spot",
        ),
        Instrument(
            symbol="PEPEUSDT",
            name="PEPE / USDT",
            market="CRYPTO",
            exchange="Binance Spot",
            currency="USDT",
            provider="binance",
            asset_type="spot",
        ),
    ]

    ranked = rank_instruments(instruments, "PEPE", limit=2)

    assert ranked[0].symbol == "PEPEUSDT"


def test_natural_language_mentions_resolve_without_a_picker() -> None:
    assert find_popular_instrument_mentions("帮我分析一下苹果最近的趋势")[0].symbol == "AAPL"
    assert find_popular_instrument_mentions("BTCUSDT 现在风险大吗？")[0].symbol == "BTCUSDT"
    assert [
        item.symbol for item in find_popular_instrument_mentions("比较一下特斯拉和英伟达")
    ] == ["NVDA", "TSLA"]


def test_short_futures_symbols_do_not_match_inside_words() -> None:
    assert find_popular_instrument_mentions("Show me a basic market study") == []
