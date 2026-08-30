import asyncio
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest
import quantsieve_providers.binance as binance_module
from quantsieve_providers import (
    BinanceProvider,
    BinanceSettlementDailyBarEvidence,
    BinanceSpotTradingRules,
    SQLiteCache,
)


@pytest.fixture(autouse=True)
def reference_stream(monkeypatch):
    state: dict[str, object] = {
        "payload": None,
        "error": None,
    }

    class MockReferenceStream:
        def __init__(self, uri: str) -> None:
            self.uri = uri

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

        async def recv(self):
            error = state["error"]
            if isinstance(error, BaseException):
                raise error
            payload = state["payload"]
            if payload is None:
                stream_name = self.uri.rsplit("/", 1)[-1]
                symbol = stream_name.split("@", 1)[0].upper()
                payload = {
                    "e": "referencePrice",
                    "s": symbol,
                    "r": "95499.50000000",
                    "t": int(datetime.now(UTC).timestamp() * 1_000),
                }
            if isinstance(payload, (str, bytes)):
                return payload
            return json.dumps(payload)

    def connect(uri: str, **kwargs):
        state["uri"] = uri
        state["kwargs"] = kwargs
        return MockReferenceStream(uri)

    monkeypatch.setattr(binance_module, "websocket_connect", connect)
    return state


def paper_rules(
    *,
    symbol: str = "BTCUSDT",
    average_minutes: int = 5,
) -> BinanceSpotTradingRules:
    return BinanceSpotTradingRules(
        symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        status="TRADING",
        spot_trading_allowed=True,
        order_types=("LIMIT", "MARKET"),
        lot_step_size=Decimal("0.00001"),
        lot_min_quantity=Decimal("0.00001"),
        lot_max_quantity=Decimal("100"),
        market_step_size=Decimal("0.00001"),
        market_min_quantity=Decimal("0.00001"),
        market_max_quantity=Decimal("100"),
        min_notional=Decimal("5"),
        min_notional_applies_to_market=True,
        max_notional=Decimal("1000000"),
        max_notional_applies_to_market=True,
        notional_average_price_minutes=average_minutes,
        verified_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_binance_search_history_and_quote(monkeypatch, tmp_path) -> None:
    requested_intervals: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/exchangeInfo"):
            payload = {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "TRADING",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "isSpotTradingAllowed": True,
                    },
                    {
                        "symbol": "BTCEUR",
                        "status": "TRADING",
                        "baseAsset": "BTC",
                        "quoteAsset": "EUR",
                        "isSpotTradingAllowed": True,
                    },
                ]
            }
        elif request.url.path.endswith("/klines"):
            requested_intervals.append(request.url.params["interval"])
            payload = [
                [
                    1_735_689_600_000,
                    "93000.0",
                    "95000.0",
                    "92000.0",
                    "94000.000000000000001",
                    "100.5",
                    1_735_775_999_999,
                    "9447000.0",
                    1000,
                ],
                [
                    1_735_776_000_000,
                    "94000.0",
                    "96000.0",
                    "93500.0",
                    "95500.0",
                    "110.0",
                    1_735_862_399_999,
                    "10450000.0",
                    1100,
                ],
            ]
        else:
            payload = {
                "openPrice": "93000.0",
                "highPrice": "96000.0",
                "lowPrice": "92000.0",
                "lastPrice": "95500.0",
                "volume": "210.5",
                "quoteVolume": "19900000.0",
                "priceChange": "2500.0",
                "priceChangePercent": "2.688",
                "bidPrice": "95499.0",
                "askPrice": "95500.0",
                "count": 2100,
            }
        return httpx.Response(200, request=request, json=payload)

    transport = httpx.MockTransport(respond)

    class MockClient(httpx.Client):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "Client", MockClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    matches = await provider.search("比特币", limit=5)
    slash_pair_matches = await provider.search("BTC/USDT", limit=5)
    named_pair_matches = await provider.search("bitcoin usdt", limit=5)
    quote_matches = await provider.search("USDT", limit=5)
    history = await provider.history("BTC", date(2025, 1, 1), date(2025, 1, 2))
    hourly = await provider.history_interval(
        "BTC",
        date(2025, 1, 1),
        date(2025, 1, 2),
        interval="1h",
    )
    quote = await provider.quote("BTCUSDT")

    assert matches[0].symbol == "BTCUSDT"
    assert slash_pair_matches[0].symbol == "BTCUSDT"
    assert named_pair_matches[0].symbol == "BTCUSDT"
    assert quote_matches[0].symbol == "BTCUSDT"
    assert matches[0].market == "CRYPTO"
    assert [row["close"] for row in history.rows] == [94000.0, 95500.0]
    assert [row["exact_close"] for row in history.rows] == [
        "94000.000000000000001",
        "95500",
    ]
    assert history.rows[0]["exact_volume"] == "100.5"
    assert {row["symbol"] for row in history.rows} == {"BTCUSDT"}
    assert {row["interval"] for row in history.rows} == {"1d"}
    assert {row["interval"] for row in hourly.rows} == {"1h"}
    assert hourly.metadata["interval"] == "1h"
    assert requested_intervals == ["1d", "1h"]
    assert quote.rows[0]["price_change_percent"] == 2.688
    assert quote.citations[0].source == "Binance Spot"


@pytest.mark.parametrize(
    ("raw", "allow_zero", "canonical", "projection"),
    [
        ("0.00000001", False, "0.00000001", 0.00000001),
        ("93000.00000000", False, "93000", 93000.0),
        ("123456789.12345678", False, "123456789.12345678", 123456789.12345678),
        ("0.00000000", True, "0", 0.0),
        ("000123.4500", False, "123.45", 123.45),
        ("100", False, "100", 100.0),
    ],
)
def test_binance_kline_decimal_preserves_exact_canonical_evidence(
    raw: str,
    allow_zero: bool,
    canonical: str,
    projection: float,
) -> None:
    assert binance_module._canonical_kline_decimal(
        raw,
        field="close",
        allow_zero=allow_zero,
    ) == (canonical, projection)


def test_binance_public_request_uses_a_bounded_global_deadline(monkeypatch) -> None:
    attempts: list[float] = []
    clock = iter((0.0, 0.0, 7.0, 7.0, 14.0, 14.0, 21.0, 21.0))

    class UnavailableClient:
        def __init__(self, *, timeout: float, trust_env: bool) -> None:
            del trust_env
            attempts.append(timeout)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def get(self, *args, **kwargs):
            del args, kwargs
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(binance_module, "monotonic", lambda: next(clock))
    monkeypatch.setattr(binance_module.httpx, "Client", UnavailableClient)

    with pytest.raises(RuntimeError, match="public market data is unavailable"):
        BinanceProvider._request_json("/api/v3/klines")

    assert attempts == [6.0, 6.0, 4.0, 4.0]


@pytest.mark.parametrize(
    ("raw", "allow_zero"),
    [
        ("-1e-9999", True),
        ("1e-9999", False),
        ("1e9999", False),
        ("+1", False),
        ("-0", True),
        ("-1", False),
        (".5", False),
        ("1.", False),
        ("１２３.４５", False),
        ("١٢٣.٤٥", False),
        ("1_0", False),
        ("NaN", False),
        ("Infinity", False),
        ("", False),
        (" 1", False),
        ("1 ", False),
        (True, False),
    ],
)
def test_binance_kline_decimal_rejects_non_fixed_point_syntax(
    raw: object,
    allow_zero: bool,
) -> None:
    with pytest.raises(RuntimeError, match="K-line"):
        binance_module._canonical_kline_decimal(
            raw,
            field="volume" if allow_zero else "close",
            allow_zero=allow_zero,
        )


@pytest.mark.asyncio
async def test_binance_history_excludes_unfinalized_cached_bar(
    monkeypatch,
    tmp_path,
) -> None:
    now = datetime.now(UTC)
    today = now.date()
    finalized_open = datetime.combine(today - timedelta(days=1), time.min, tzinfo=UTC)
    live_open = datetime.combine(today, time.min, tzinfo=UTC)

    def milliseconds(value: datetime) -> int:
        return int(value.timestamp() * 1_000)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/time"):
            return httpx.Response(
                200,
                request=request,
                json={"serverTime": int(datetime.now(UTC).timestamp() * 1_000)},
            )
        payload = [
            [
                milliseconds(finalized_open),
                "100",
                "110",
                "90",
                "105",
                "10",
                milliseconds(now - timedelta(minutes=3)),
                "1000",
                12,
            ],
            [
                milliseconds(live_open),
                "105",
                "115",
                "100",
                "112",
                "8",
                milliseconds(now + timedelta(hours=1)),
                "900",
                9,
            ],
        ]
        return httpx.Response(200, request=request, json=payload)

    transport = httpx.MockTransport(respond)

    class MockClient(httpx.Client):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "Client", MockClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    history = await provider.history(
        "BTCUSDT",
        today - timedelta(days=1),
        today,
    )

    assert len(history.rows) == 1
    assert history.rows[0]["finalized"] is True
    assert history.rows[0]["symbol"] == "BTCUSDT"
    assert history.rows[0]["interval"] == "1d"
    assert history.rows[0]["close_time"]
    assert history.metadata["finalized_bars_only"] is True
    assert history.metadata["finalization_lag_seconds"] == 120


@pytest.mark.asyncio
async def test_binance_paper_execution_evidence_is_typed_and_uncached(
    monkeypatch,
    tmp_path,
) -> None:
    calls = {
        "exchangeInfo": 0,
        "bookTicker": 0,
        "time": 0,
    }

    def respond(request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        calls[operation] += 1
        if operation == "exchangeInfo":
            payload = {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "TRADING",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "isSpotTradingAllowed": True,
                        "orderTypes": ["LIMIT", "MARKET"],
                        "filters": [
                            {
                                "filterType": "LOT_SIZE",
                                "minQty": "0.00001",
                                "maxQty": "100.00000000",
                                "stepSize": "0.00001",
                            },
                            {
                                "filterType": "MARKET_LOT_SIZE",
                                "minQty": "0.00000",
                                "maxQty": "0.00000000",
                                "stepSize": "0.00000",
                            },
                            {
                                "filterType": "MIN_NOTIONAL",
                                "minNotional": "10.00000000",
                                "applyToMarket": True,
                                "avgPriceMins": 5,
                            },
                            {
                                "filterType": "NOTIONAL",
                                "minNotional": "5.00000000",
                                "applyMinToMarket": True,
                                "maxNotional": "1000000.00000000",
                                "applyMaxToMarket": True,
                                "avgPriceMins": 5,
                            },
                        ],
                    }
                ]
            }
        elif operation == "bookTicker":
            payload = {
                "symbol": "BTCUSDT",
                "bidPrice": "95499.0",
                "askPrice": "95500.0",
                "bidQty": "1.25000000",
                "askQty": "0.75000000",
            }
        else:
            payload = {"serverTime": int(datetime.now(UTC).timestamp() * 1_000)}
        return httpx.Response(200, request=request, json=payload)

    transport = httpx.MockTransport(respond)

    class MockClient(httpx.Client):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "Client", MockClient)
    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    rules = await provider.trading_rules("BTC")
    first_quote = await provider.execution_quote("BTCUSDT", rules=rules)
    second_quote = await provider.execution_quote("BTCUSDT", rules=rules)

    assert rules.symbol == "BTCUSDT"
    assert rules.quote_asset == "USDT"
    assert rules.status == "TRADING"
    assert rules.spot_trading_allowed is True
    assert rules.market_step_size == rules.lot_step_size
    assert rules.market_min_quantity == rules.lot_min_quantity
    assert rules.market_max_quantity == rules.lot_max_quantity
    assert rules.lot_max_quantity == Decimal("100.00000000")
    assert rules.min_notional == Decimal("10.00000000")
    assert rules.min_notional_applies_to_market is True
    assert rules.max_notional == Decimal("1000000.00000000")
    assert rules.max_notional_applies_to_market is True
    assert rules.notional_average_price_minutes == 5
    assert first_quote.bid_price == Decimal("95499.0")
    assert first_quote.ask_price == Decimal("95500.0")
    assert first_quote.bid_quantity == Decimal("1.25000000")
    assert first_quote.ask_quantity == Decimal("0.75000000")
    assert first_quote.notional_reference_price == Decimal("95499.50000000")
    assert first_quote.notional_reference_kind == "exchange_reference"
    assert first_quote.notional_reference_window_minutes is None
    assert first_quote.exchange_reference_available is True
    assert (
        first_quote.exchange_reference_at
        == first_quote.notional_reference_at
    )
    assert (
        first_quote.exchange_reference_observed_at
        == first_quote.notional_reference_observed_at
    )
    inconsistent = first_quote.model_dump(mode="python")
    inconsistent["exchange_reference_observed_at"] += timedelta(milliseconds=1)
    with pytest.raises(ValueError, match="retain its raw timestamps"):
        type(first_quote).model_validate(inconsistent)
    unavailable = first_quote.model_dump(mode="python")
    unavailable["exchange_reference_available"] = False
    with pytest.raises(ValueError, match="requires a fallback price"):
        type(first_quote).model_validate(unavailable)
    assert first_quote.cache_used is False
    assert second_quote.cache_used is False
    assert calls == {
        "exchangeInfo": 1,
        "bookTicker": 2,
        "time": 2,
    }


@pytest.mark.asyncio
async def test_binance_live_history_rejects_exchange_clock_drift(
    monkeypatch,
    tmp_path,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/time")
        server_time = datetime.now(UTC) + timedelta(minutes=1)
        return httpx.Response(
            200,
            request=request,
            json={"serverTime": int(server_time.timestamp() * 1_000)},
        )

    transport = httpx.MockTransport(respond)

    class MockClient(httpx.Client):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "Client", MockClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))
    today = datetime.now(UTC).date()

    with pytest.raises(RuntimeError, match="clocks differ"):
        await provider.history("BTCUSDT", today - timedelta(days=1), today)


@pytest.mark.asyncio
async def test_binance_execution_quote_rejects_mismatched_payload_symbol(
    monkeypatch,
    tmp_path,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/bookTicker"):
            payload = {
                "symbol": "ETHUSDT",
                "bidPrice": "100",
                "askPrice": "101",
                "bidQty": "1",
                "askQty": "1",
            }
        else:
            payload = {"serverTime": int(datetime.now(UTC).timestamp() * 1_000)}
        return httpx.Response(200, request=request, json=payload)

    transport = httpx.MockTransport(respond)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    with pytest.raises(RuntimeError, match="identity does not match"):
        await provider.execution_quote("BTCUSDT", rules=paper_rules())


@pytest.mark.asyncio
async def test_binance_execution_quote_revalidates_copied_rules(
    monkeypatch,
    tmp_path,
) -> None:
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Invalid rules must fail before requesting {request.url}.")

    transport = httpx.MockTransport(unexpected_request)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))
    forged = paper_rules().model_copy(
        update={"market_max_quantity": Decimal("-1")}
    )

    with pytest.raises(ValueError, match="greater than 0"):
        await provider.execution_quote("BTCUSDT", rules=forged)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_field", "invalid_value"),
    [
        ("isSpotTradingAllowed", "true"),
        ("applyMinToMarket", 1),
        ("applyMaxToMarket", None),
        ("avgPriceMins", True),
        ("maxQty", "NaN"),
    ],
)
async def test_binance_trading_rules_fail_closed_on_invalid_exchange_fields(
    monkeypatch,
    tmp_path,
    invalid_field,
    invalid_value,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        row = {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "isSpotTradingAllowed": True,
            "orderTypes": ["LIMIT", "MARKET"],
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.00001",
                    "maxQty": "100",
                    "stepSize": "0.00001",
                },
                {
                    "filterType": "MARKET_LOT_SIZE",
                    "minQty": "0",
                    "maxQty": "0",
                    "stepSize": "0",
                },
                {
                    "filterType": "NOTIONAL",
                    "minNotional": "5",
                    "applyMinToMarket": True,
                    "maxNotional": "1000000",
                    "applyMaxToMarket": True,
                    "avgPriceMins": 5,
                },
            ],
        }
        if invalid_field == "isSpotTradingAllowed":
            row[invalid_field] = invalid_value
        elif invalid_field == "maxQty":
            row["filters"][0][invalid_field] = invalid_value
        else:
            row["filters"][2][invalid_field] = invalid_value
        return httpx.Response(
            200,
            request=request,
            json={"symbols": [row]},
        )

    transport = httpx.MockTransport(respond)

    class MockClient(httpx.Client):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "Client", MockClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    with pytest.raises(RuntimeError, match=r"no valid|invalid"):
        await provider.trading_rules("BTCUSDT")


@pytest.mark.asyncio
async def test_binance_execution_quote_uses_exact_average_price_fallback(
    monkeypatch,
    tmp_path,
    reference_stream,
) -> None:
    calls: list[str] = []
    average_ms = int(
        (datetime.now(UTC) - timedelta(seconds=1)).timestamp() * 1_000
    )
    reference_stream["payload"] = {
        "e": "referencePrice",
        "s": "BTCUSDT",
        "r": None,
        "t": int(datetime.now(UTC).timestamp() * 1_000),
    }

    def respond(request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        calls.append(operation)
        now_ms = int(datetime.now(UTC).timestamp() * 1_000)
        if operation == "bookTicker":
            payload = {
                "symbol": "BTCUSDT",
                "bidPrice": "100.01000000",
                "askPrice": "100.02000000",
                "bidQty": "2.50000000",
                "askQty": "3.75000000",
            }
        elif operation == "avgPrice":
            payload = {
                "mins": 5,
                "price": "100.01500000",
                "closeTime": average_ms,
            }
        else:
            payload = {"serverTime": now_ms}
        return httpx.Response(200, request=request, json=payload)

    transport = httpx.MockTransport(respond)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    quote = await provider.execution_quote("BTCUSDT", rules=paper_rules())

    assert quote.notional_reference_kind == "average_price"
    assert quote.exchange_reference_available is False
    forged_available = quote.model_dump(mode="python")
    forged_available["exchange_reference_available"] = True
    with pytest.raises(ValueError, match="must be the chosen reference"):
        type(quote).model_validate(forged_available)
    assert quote.notional_reference_window_minutes == 5
    assert quote.notional_reference_price == Decimal("100.01500000")
    assert quote.bid_quantity == Decimal("2.50000000")
    assert quote.ask_quantity == Decimal("3.75000000")
    assert quote.observed_at <= quote.notional_reference_observed_at
    assert quote.notional_reference_at < quote.request_started_at
    assert calls == ["bookTicker", "avgPrice", "time"]


@pytest.mark.asyncio
async def test_binance_execution_quote_falls_back_on_null_stream_reference(
    monkeypatch,
    tmp_path,
    reference_stream,
) -> None:
    calls: list[str] = []
    client_instances = 0
    reference_stream["payload"] = {
        "e": "referencePrice",
        "s": "BTCUSDT",
        "r": None,
        "t": int(datetime.now(UTC).timestamp() * 1_000),
    }

    def respond(request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        calls.append(operation)
        now_ms = int(datetime.now(UTC).timestamp() * 1_000)
        assert request.url.host == "data-api.binance.vision"
        if operation == "bookTicker":
            status = 200
            payload = {
                "symbol": "BTCUSDT",
                "bidPrice": "100.01",
                "askPrice": "100.02",
                "bidQty": "2",
                "askQty": "3",
            }
        elif operation == "avgPrice":
            status = 200
            payload = {
                "mins": 5,
                "price": "100.015",
                "closeTime": now_ms,
            }
        else:
            status = 200
            payload = {"serverTime": now_ms}
        return httpx.Response(status, request=request, json=payload)

    transport = httpx.MockTransport(respond)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            nonlocal client_instances
            client_instances += 1
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    quote = await BinanceProvider(SQLiteCache(tmp_path / "cache.db")).execution_quote(
        "BTCUSDT",
        rules=paper_rules(),
    )

    assert quote.notional_reference_kind == "average_price"
    assert quote.exchange_reference_available is False
    assert quote.notional_reference_price == Decimal("100.015")
    assert calls == ["bookTicker", "avgPrice", "time"]
    assert client_instances == 1
    assert reference_stream["uri"] == (
        "wss://data-stream.binance.vision/ws/btcusdt@referencePrice"
    )
    stream_options = reference_stream["kwargs"]
    assert stream_options["proxy"] is None
    assert stream_options["close_timeout"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "expected_message"),
    [
        ("e", "trade", "wrong reference-price event type"),
        ("s", "ETHUSDT", "stream identity does not match"),
    ],
)
async def test_binance_execution_quote_rejects_wrong_reference_stream_event(
    monkeypatch,
    tmp_path,
    reference_stream,
    field,
    value,
    expected_message,
) -> None:
    payload = {
        "e": "referencePrice",
        "s": "BTCUSDT",
        "r": "100.5",
        "t": int(datetime.now(UTC).timestamp() * 1_000),
    }
    payload[field] = value
    reference_stream["payload"] = payload

    class ForbiddenClient:
        def __init__(self, **kwargs) -> None:
            raise AssertionError("Invalid stream evidence must fail before REST.")

    monkeypatch.setattr(httpx, "Client", ForbiddenClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    with pytest.raises(RuntimeError, match=expected_message):
        await provider.execution_quote("BTCUSDT", rules=paper_rules())


@pytest.mark.asyncio
async def test_binance_execution_quote_fails_closed_on_reference_stream_timeout(
    monkeypatch,
    tmp_path,
    reference_stream,
) -> None:
    reference_stream["error"] = TimeoutError("Reference stream timed out.")

    class ForbiddenClient:
        def __init__(self, **kwargs) -> None:
            raise AssertionError("Timed-out stream evidence must fail before REST.")

    monkeypatch.setattr(httpx, "Client", ForbiddenClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    with pytest.raises(RuntimeError, match="four-second collection deadline"):
        await provider.execution_quote("BTCUSDT", rules=paper_rules())


@pytest.mark.asyncio
async def test_binance_execution_quote_uses_last_price_for_zero_minute_rule(
    monkeypatch,
    tmp_path,
    reference_stream,
) -> None:
    calls: list[str] = []
    exchange_time = datetime.now(UTC)
    trade_ms = int((exchange_time - timedelta(seconds=1)).timestamp() * 1_000)
    exchange_ms = int(exchange_time.timestamp() * 1_000)
    reference_stream["payload"] = {
        "e": "referencePrice",
        "s": "BTCUSDT",
        "r": None,
        "t": exchange_ms,
    }

    def respond(request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        calls.append(operation)
        if operation == "bookTicker":
            assert request.url.host == "data-api.binance.vision"
            return httpx.Response(
                200,
                request=request,
                json={
                    "symbol": "BTCUSDT",
                    "bidPrice": "100.01",
                    "askPrice": "100.02",
                    "bidQty": "2",
                    "askQty": "3",
                },
            )
        if operation == "trades":
            assert request.url.host == "data-api.binance.vision"
            assert request.url.params["limit"] == "1"
            return httpx.Response(
                200,
                request=request,
                json=[
                    {
                        "id": 123,
                        "price": "100.015",
                        "time": trade_ms,
                    }
                ],
            )
        assert request.url.host == "data-api.binance.vision"
        return httpx.Response(
            200,
            request=request,
            json={"serverTime": exchange_ms},
        )

    transport = httpx.MockTransport(respond)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    quote = await BinanceProvider(SQLiteCache(tmp_path / "cache.db")).execution_quote(
        "BTCUSDT",
        rules=paper_rules(average_minutes=0),
    )

    assert quote.notional_reference_kind == "last_price"
    assert quote.exchange_reference_available is False
    assert quote.notional_reference_window_minutes == 0
    assert quote.notional_reference_price == Decimal("100.015")
    assert quote.notional_reference_at == datetime.fromtimestamp(
        trade_ms / 1_000,
        tz=UTC,
    )
    assert quote.notional_reference_at < quote.exchange_server_time
    assert quote.notional_reference_at < quote.request_started_at
    assert calls == ["bookTicker", "trades", "time"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trades_payload", "expected_message"),
    [
        ([], "unique last trade"),
        ([{"id": True, "price": "100", "time": 1}], "last-trade id"),
        ([{"id": 1, "price": "NaN", "time": 1}], "last trade price"),
        ([{"id": 1, "price": "100", "time": None}], "last trade time"),
    ],
)
async def test_binance_execution_quote_strictly_validates_last_trade(
    monkeypatch,
    tmp_path,
    reference_stream,
    trades_payload,
    expected_message,
) -> None:
    reference_stream["payload"] = {
        "e": "referencePrice",
        "s": "BTCUSDT",
        "r": None,
        "t": int(datetime.now(UTC).timestamp() * 1_000),
    }

    def respond(request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        if operation == "bookTicker":
            payload = {
                "symbol": "BTCUSDT",
                "bidPrice": "100",
                "askPrice": "101",
                "bidQty": "2",
                "askQty": "3",
            }
            return httpx.Response(200, request=request, json=payload)
        return httpx.Response(200, request=request, json=trades_payload)

    transport = httpx.MockTransport(respond)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    with pytest.raises(RuntimeError, match=expected_message):
        await provider.execution_quote(
            "BTCUSDT",
            rules=paper_rules(average_minutes=0),
        )


@pytest.mark.asyncio
async def test_binance_execution_quote_enforces_total_bundle_deadline(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[str] = []
    clock_values = iter((0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 4.1))

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            200,
            request=request,
            json={
                "symbol": "BTCUSDT",
                "bidPrice": "100",
                "askPrice": "101",
                "bidQty": "2",
                "askQty": "3",
            },
        )

    transport = httpx.MockTransport(respond)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    monkeypatch.setattr(binance_module, "monotonic", lambda: next(clock_values))
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    with pytest.raises(RuntimeError, match="four-second collection deadline"):
        await provider.execution_quote("BTCUSDT", rules=paper_rules())

    assert calls == ["bookTicker"]


@pytest.mark.asyncio
async def test_binance_execution_quote_cancellation_closes_active_rest_request(
    monkeypatch,
    tmp_path,
) -> None:
    request_started = asyncio.Event()
    request_cancelled = asyncio.Event()
    client_exited = asyncio.Event()
    active_requests = 0

    class BlockingAsyncClient:
        def __init__(self, **kwargs) -> None:
            del kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            client_exited.set()
            return False

        async def get(self, path, *, params, timeout):
            nonlocal active_requests
            del path, params, timeout
            active_requests += 1
            request_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                request_cancelled.set()
                raise
            finally:
                active_requests -= 1

    monkeypatch.setattr(httpx, "AsyncClient", BlockingAsyncClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))
    task = asyncio.create_task(
        provider.execution_quote("BTCUSDT", rules=paper_rules())
    )

    await asyncio.wait_for(request_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert request_cancelled.is_set()
    assert client_exited.is_set()
    assert active_requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("book_field", "book_value", "expected_message"),
    [
        ("bidQty", None, "book ticker bidQty"),
        ("askPrice", "NaN", "book ticker askPrice"),
    ],
)
async def test_binance_execution_quote_rejects_incomplete_or_nonfinite_book(
    monkeypatch,
    tmp_path,
    book_field,
    book_value,
    expected_message,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        payload = {
            "symbol": "BTCUSDT",
            "bidPrice": "100",
            "askPrice": "101",
            "bidQty": "2",
            "askQty": "3",
        }
        payload[book_field] = book_value
        return httpx.Response(200, request=request, json=payload)

    transport = httpx.MockTransport(respond)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    with pytest.raises(RuntimeError, match=expected_message):
        await provider.execution_quote("BTCUSDT", rules=paper_rules())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reference_field", "reference_value", "expected_message"),
    [
        ("r", "NaN", "reference price stream r"),
        ("t", None, "reference price stream t"),
    ],
)
async def test_binance_execution_quote_rejects_invalid_reference_stream_payload(
    monkeypatch,
    tmp_path,
    reference_stream,
    reference_field,
    reference_value,
    expected_message,
) -> None:
    payload = {
        "e": "referencePrice",
        "s": "BTCUSDT",
        "r": "100.5",
        "t": int(datetime.now(UTC).timestamp() * 1_000),
    }
    payload[reference_field] = reference_value
    reference_stream["payload"] = payload

    class ForbiddenClient:
        def __init__(self, **kwargs) -> None:
            raise AssertionError("Invalid stream evidence must fail before REST.")

    monkeypatch.setattr(httpx, "Client", ForbiddenClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    with pytest.raises(RuntimeError, match=expected_message):
        await provider.execution_quote("BTCUSDT", rules=paper_rules())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("average_field", "average_value", "expected_message"),
    [
        ("mins", 4, "window does not match"),
        ("price", "NaN", "average price price"),
        ("closeTime", None, "average price closeTime"),
        ("symbol", "ETHUSDT", "identity does not match"),
    ],
)
async def test_binance_execution_quote_rejects_invalid_average_fallback(
    monkeypatch,
    tmp_path,
    reference_stream,
    average_field,
    average_value,
    expected_message,
) -> None:
    reference_stream["payload"] = {
        "e": "referencePrice",
        "s": "BTCUSDT",
        "r": None,
        "t": int(datetime.now(UTC).timestamp() * 1_000),
    }

    def respond(request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        now_ms = int(datetime.now(UTC).timestamp() * 1_000)
        if operation == "bookTicker":
            payload = {
                "symbol": "BTCUSDT",
                "bidPrice": "100",
                "askPrice": "101",
                "bidQty": "2",
                "askQty": "3",
            }
        else:
            payload = {
                "mins": 5,
                "price": "100.5",
                "closeTime": now_ms,
            }
            payload[average_field] = average_value
        return httpx.Response(200, request=request, json=payload)

    transport = httpx.MockTransport(respond)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    with pytest.raises(RuntimeError, match=expected_message):
        await provider.execution_quote("BTCUSDT", rules=paper_rules())


@pytest.mark.asyncio
async def test_binance_default_history_window_does_not_use_host_local_date(
    monkeypatch,
    tmp_path,
) -> None:
    now = datetime.now(UTC)
    open_time = datetime.combine(
        now.date() - timedelta(days=2),
        time.min,
        tzinfo=UTC,
    )
    close_time = open_time + timedelta(days=1) - timedelta(milliseconds=1)

    class ForbiddenLocalDate:
        @staticmethod
        def today():
            raise AssertionError("Host-local date must not define Binance UTC windows.")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/time"):
            payload = {"serverTime": int(datetime.now(UTC).timestamp() * 1_000)}
        else:
            payload = [
                [
                    int(open_time.timestamp() * 1_000),
                    "100",
                    "110",
                    "90",
                    "105",
                    "10",
                    int(close_time.timestamp() * 1_000),
                    "1000",
                    12,
                ]
            ]
        return httpx.Response(200, request=request, json=payload)

    transport = httpx.MockTransport(respond)

    class MockClient(httpx.Client):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(binance_module, "date", ForbiddenLocalDate)
    monkeypatch.setattr(httpx, "Client", MockClient)
    history = await BinanceProvider(SQLiteCache(tmp_path / "cache.db")).history(
        "BTCUSDT"
    )

    assert history.rows[0]["exchange_clock_verified"] is True


def settlement_kline(
    session: date,
    *,
    exact_close: str = "105.000000000000001",
) -> list[object]:
    open_time = datetime.combine(session, time.min, tzinfo=UTC)
    close_time = open_time + timedelta(days=1) - timedelta(milliseconds=1)
    return [
        int(open_time.timestamp() * 1_000),
        "100.00000000",
        "110.00000000",
        "90.00000000",
        exact_close,
        "10.50000000",
        int(close_time.timestamp() * 1_000),
        "1000",
        12,
    ]


@pytest.mark.asyncio
async def test_binance_settlement_bar_is_typed_native_async_and_uncached(
    monkeypatch,
    tmp_path,
) -> None:
    session = datetime.now(UTC).date() - timedelta(days=2)
    calls: list[tuple[str, dict[str, str]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        calls.append((operation, dict(request.url.params)))
        payload: object
        if operation == "time":
            payload = {
                "serverTime": int(datetime.now(UTC).timestamp() * 1_000)
            }
        else:
            payload = [settlement_kline(session)]
        return httpx.Response(200, request=request, json=payload)

    transport = httpx.MockTransport(respond)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    class ForbiddenSyncClient:
        def __init__(self, **kwargs) -> None:
            del kwargs
            raise AssertionError("Settlement evidence must be native async.")

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    monkeypatch.setattr(httpx, "Client", ForbiddenSyncClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    def forbidden_cache(*args, **kwargs):
        del args, kwargs
        raise AssertionError("Settlement evidence must bypass SQLite cache.")

    monkeypatch.setattr(provider.cache, "get", forbidden_cache)
    monkeypatch.setattr(provider.cache, "set", forbidden_cache)
    first = await provider.settlement_daily_bar(
        "BTC",
        session=session,
        deadline=binance_module.monotonic() + 5,
    )
    second = await provider.settlement_daily_bar(
        "BTCUSDT",
        session=session,
        deadline=binance_module.monotonic() + 5,
    )

    assert isinstance(first, BinanceSettlementDailyBarEvidence)
    assert first.symbol == second.symbol
    assert first.session == second.session
    assert first.exact_close == second.exact_close
    assert first.provider == "binance"
    assert first.venue == "Binance Spot"
    assert first.symbol == "BTCUSDT"
    assert first.session == datetime.combine(
        session,
        time.min,
        tzinfo=UTC,
    ).isoformat()
    assert first.cache_used is False
    assert first.exchange_clock_verified is True
    assert first.finalization_lag_seconds == 120
    assert first.exact_close == "105.000000000000001"
    assert first.close == 105.0
    assert [operation for operation, _params in calls] == [
        "time",
        "klines",
        "time",
        "klines",
    ]
    kline_params = calls[1][1]
    assert kline_params["symbol"] == "BTCUSDT"
    assert kline_params["interval"] == "1d"
    assert kline_params["limit"] == "2"
    assert int(kline_params["startTime"]) == settlement_kline(session)[0]
    assert int(kline_params["endTime"]) == settlement_kline(session)[6]


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["wrong_open", "wrong_close", "duplicate"])
async def test_binance_settlement_bar_rejects_wrong_session_or_duplicates(
    monkeypatch,
    tmp_path,
    attack,
) -> None:
    session = datetime.now(UTC).date() - timedelta(days=2)
    row = settlement_kline(session)
    if attack == "wrong_open":
        row[0] = int(row[0]) + 86_400_000
    elif attack == "wrong_close":
        row[6] = int(row[6]) + 1
    payload = [row, list(row)] if attack == "duplicate" else [row]

    def respond(request: httpx.Request) -> httpx.Response:
        response_payload: object = (
            {"serverTime": int(datetime.now(UTC).timestamp() * 1_000)}
            if request.url.path.endswith("/time")
            else payload
        )
        return httpx.Response(200, request=request, json=response_payload)

    transport = httpx.MockTransport(respond)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    with pytest.raises(RuntimeError, match=r"session|unique"):
        await provider.settlement_daily_bar(
            "BTCUSDT",
            session=session,
            deadline=binance_module.monotonic() + 5,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["clock_drift", "not_finalized"])
async def test_binance_settlement_bar_requires_verified_mature_exchange_clock(
    monkeypatch,
    tmp_path,
    attack,
) -> None:
    session = (
        datetime.now(UTC).date()
        if attack == "not_finalized"
        else datetime.now(UTC).date() - timedelta(days=2)
    )

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/time"):
            server_time = datetime.now(UTC)
            if attack == "clock_drift":
                server_time += timedelta(seconds=6)
            payload: object = {
                "serverTime": int(server_time.timestamp() * 1_000)
            }
        else:
            payload = [settlement_kline(session)]
        return httpx.Response(200, request=request, json=payload)

    transport = httpx.MockTransport(respond)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    with pytest.raises(RuntimeError, match=r"clocks differ|finalization"):
        await provider.settlement_daily_bar(
            "BTCUSDT",
            session=session,
            deadline=binance_module.monotonic() + 5,
        )


@pytest.mark.asyncio
async def test_binance_settlement_bundle_uses_one_absolute_deadline(
    monkeypatch,
    tmp_path,
) -> None:
    session = datetime.now(UTC).date() - timedelta(days=2)
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            200,
            request=request,
            json={"serverTime": int(datetime.now(UTC).timestamp() * 1_000)},
        )

    transport = httpx.MockTransport(respond)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    clock_values = iter((0.0, 0.1, 0.2, 20.1))
    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    monkeypatch.setattr(
        binance_module,
        "monotonic",
        lambda: next(clock_values),
    )
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))

    with pytest.raises(RuntimeError, match="shared deadline"):
        await provider.settlement_daily_bar(
            "BTCUSDT",
            session=session,
            deadline=20.0,
        )

    assert calls == ["time"]


@pytest.mark.asyncio
async def test_binance_settlement_cancellation_closes_active_request(
    monkeypatch,
    tmp_path,
) -> None:
    request_started = asyncio.Event()
    request_cancelled = asyncio.Event()
    client_exited = asyncio.Event()
    active_requests = 0

    class BlockingAsyncClient:
        def __init__(self, **kwargs) -> None:
            del kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            client_exited.set()
            return False

        async def get(self, path, *, params, timeout):
            del path, params, timeout
            nonlocal active_requests
            active_requests += 1
            request_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                request_cancelled.set()
                raise
            finally:
                active_requests -= 1

    monkeypatch.setattr(httpx, "AsyncClient", BlockingAsyncClient)
    provider = BinanceProvider(SQLiteCache(tmp_path / "cache.db"))
    current_task = asyncio.current_task()
    baseline_tasks = {
        task
        for task in asyncio.all_tasks()
        if task is not current_task
    }
    task = asyncio.create_task(
        provider.settlement_daily_bar(
            "BTCUSDT",
            session=datetime.now(UTC).date() - timedelta(days=2),
            deadline=binance_module.monotonic() + 20,
        )
    )

    await asyncio.wait_for(request_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert request_cancelled.is_set()
    assert client_exited.is_set()
    assert active_requests == 0
    await asyncio.sleep(0)
    remaining_tasks = {
        task
        for task in asyncio.all_tasks()
        if task is not current_task
    }
    assert remaining_tasks <= baseline_tasks
