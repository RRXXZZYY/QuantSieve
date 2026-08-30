from datetime import UTC, date, datetime

import pytest
from quantsieve_mcp.service import MarketDataService
from quantsieve_providers import Citation, DataEnvelope, Instrument
from quantsieve_providers.base import DataProvider


class StubProvider(DataProvider):
    name = "stub"

    def __init__(self) -> None:
        self.last_symbol: str | None = None

    def envelope(self, symbol: str, kind: str) -> DataEnvelope:
        return DataEnvelope(
            symbol=symbol,
            kind=kind,
            rows=[{"value": 1}],
            citations=[Citation(source="fixture", as_of=datetime(2026, 1, 1, tzinfo=UTC))],
        )

    async def history(
        self, symbol: str, start: date | None = None, end: date | None = None
    ) -> DataEnvelope:
        return self.envelope(symbol, "history")

    async def quote(self, symbol: str) -> DataEnvelope:
        self.last_symbol = symbol
        return self.envelope(symbol, "quote")

    async def fundamentals(self, symbol: str) -> DataEnvelope:
        return self.envelope(symbol, "fundamentals")

    async def capital_flow(self, symbol: str) -> DataEnvelope:
        return self.envelope(symbol, "capital_flow")

    async def news(self, symbol: str, limit: int = 20) -> DataEnvelope:
        return self.envelope(symbol, "news")

    async def search(self, query: str, limit: int = 10) -> list[Instrument]:
        if query.casefold() == "fixture company":
            return [
                Instrument(
                    symbol="FIX",
                    name="Fixture Company",
                    market="US",
                    exchange="TEST",
                    currency="USD",
                    provider="yfinance",
                )
            ]
        return []


@pytest.mark.asyncio
async def test_service_preserves_citations() -> None:
    stub = StubProvider()
    service = MarketDataService(providers={"akshare": stub, "yfinance": stub})

    payload = await service.quote("600519")

    assert payload["rows"] == [{"value": 1}]
    assert payload["citations"][0]["source"] == "fixture"


@pytest.mark.asyncio
async def test_service_searches_and_resolves_company_name() -> None:
    stub = StubProvider()
    service = MarketDataService(providers={"akshare": stub, "yfinance": stub})

    matches = await service.search("Fixture Company")
    payload = await service.quote("Fixture Company")

    assert matches[0]["symbol"] == "FIX"
    assert payload["symbol"] == "FIX"
    assert stub.last_symbol == "FIX"
