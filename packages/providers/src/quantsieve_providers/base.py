from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from .models import BarInterval, DataEnvelope, Instrument


class DataProvider(ABC):
    name: str

    @abstractmethod
    async def history(
        self, symbol: str, start: date | None = None, end: date | None = None
    ) -> DataEnvelope:
        """Return adjusted OHLCV history with source metadata."""

    async def history_interval(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        *,
        interval: BarInterval = "1d",
    ) -> DataEnvelope:
        """Return OHLCV at a requested bar interval when the provider supports it."""
        if interval != "1d":
            raise ValueError(f"{self.name} does not support {interval} K-lines.")
        return await self.history(symbol, start, end)

    @abstractmethod
    async def quote(self, symbol: str) -> DataEnvelope:
        """Return the latest available quote."""

    @abstractmethod
    async def fundamentals(self, symbol: str) -> DataEnvelope:
        """Return company financial statements or key fundamentals."""

    @abstractmethod
    async def capital_flow(self, symbol: str) -> DataEnvelope:
        """Return capital-flow data where the market exposes it."""

    @abstractmethod
    async def news(self, symbol: str, limit: int = 20) -> DataEnvelope:
        """Return recent public news."""

    async def search(self, query: str, limit: int = 10) -> list[Instrument]:
        """Return matching instruments when the upstream source exposes a symbol directory."""
        return []
