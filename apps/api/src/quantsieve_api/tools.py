from __future__ import annotations

from datetime import date
from typing import Any, cast

from quantsieve_engine import (
    STRATEGIES,
    BacktestConfig,
    SandboxExecutor,
    get_strategy,
    run_backtest,
)
from quantsieve_providers import MarketFilter

from .providers import ProviderRouter


class ResearchTools:
    def __init__(self, providers: ProviderRouter) -> None:
        self.providers = providers

    @property
    def definitions(self) -> list[dict[str, Any]]:
        symbol = {
            "type": "string",
            "description": (
                "A-share code, US ticker, Binance spot pair, or global-futures code; "
                "for example 600519, AAPL, BTCUSDT, or CL"
            ),
        }
        return [
            self._definition(
                "search_instruments",
                (
                    "Resolve a name or partial symbol across A-shares, US stocks, "
                    "Binance spot markets, and global commodity futures."
                ),
                {
                    "query": {
                        "type": "string",
                        "description": (
                            "Name or symbol, for example 宁德时代, Apple, 比特币, BTCUSDT, or 原油."
                        ),
                    },
                    "market": {
                        "type": "string",
                        "enum": ["all", "CN", "US", "CRYPTO", "FUTURES"],
                    },
                },
                optional={"market"},
            ),
            self._definition(
                "get_quote", "Get the latest sourced market quote.", {"symbol": symbol}
            ),
            self._definition(
                "get_fundamentals", "Get sourced financial statement data.", {"symbol": symbol}
            ),
            self._definition(
                "get_capital_flow", "Get sourced capital-flow data.", {"symbol": symbol}
            ),
            self._definition(
                "get_news",
                "Get recent sourced company news.",
                {
                    "symbol": symbol,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
            ),
            self._definition(
                "run_backtest",
                "Run a named strategy on sourced OHLCV history.",
                {
                    "symbol": symbol,
                    "strategy_id": {
                        "type": "string",
                        "enum": sorted(STRATEGIES),
                    },
                    "start": {"type": "string", "format": "date"},
                    "end": {"type": "string", "format": "date"},
                },
            ),
            self._definition(
                "run_custom_backtest",
                (
                    "Run model-authored Python signal code in the constrained strategy sandbox. "
                    "Use this when the user describes a strategy not covered by a named template."
                ),
                {
                    "symbol": symbol,
                    "strategy_name": {
                        "type": "string",
                        "description": "Short human-readable strategy name.",
                    },
                    "code": {
                        "type": "string",
                        "description": (
                            "Python code defining generate_signals(data). It must return a pandas "
                            "Series of 0/1 long-or-cash signals and may import only pandas, numpy, "
                            "or math. Data columns are open, high, low, close, volume."
                        ),
                    },
                    "start": {"type": "string", "format": "date"},
                    "end": {"type": "string", "format": "date"},
                },
            ),
            self._definition(
                "list_strategies",
                "List available audited strategy templates.",
                {},
            ),
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "search_instruments":
            market = str(arguments.get("market", "all"))
            if market not in {"all", "CN", "US", "CRYPTO", "FUTURES"}:
                market = "all"
            market_filter = cast(MarketFilter, market)
            instruments = await self.providers.search(
                str(arguments["query"]),
                market=market_filter,
                limit=10,
            )
            return {
                "instruments": [instrument.model_dump() for instrument in instruments],
                "source": "QuantSieve instrument catalog with provider fallback",
            }
        if name == "list_strategies":
            return {
                "strategies": [definition.model_dump() for definition in STRATEGIES.values()],
                "source": "QuantSieve audited strategy registry",
            }
        symbol = str(arguments["symbol"]).strip().upper()
        provider = self.providers.resolve(symbol)
        if name == "get_quote":
            return (await provider.quote(symbol)).model_dump(mode="json")
        if name == "get_fundamentals":
            return (await provider.fundamentals(symbol)).model_dump(mode="json")
        if name == "get_capital_flow":
            return (await provider.capital_flow(symbol)).model_dump(mode="json")
        if name == "get_news":
            limit = max(1, min(int(arguments.get("limit", 10)), 20))
            return (await provider.news(symbol, limit)).model_dump(mode="json")
        if name in {"run_backtest", "run_custom_backtest"}:
            start = date.fromisoformat(arguments["start"]) if arguments.get("start") else None
            end = date.fromisoformat(arguments["end"]) if arguments.get("end") else None
            history = await provider.history(symbol, start, end)
            data = history.to_frame()
            strategy_code: str | None = None
            if name == "run_custom_backtest":
                strategy_code = str(arguments["code"])
                signals = SandboxExecutor().execute(strategy_code, data)
                strategy_payload = {
                    "id": "custom",
                    "name": str(arguments["strategy_name"]),
                    "description": "Model-authored strategy executed in the QuantSieve sandbox.",
                    "parameters": {},
                    "category": "custom",
                }
            else:
                definition, strategy = get_strategy(str(arguments["strategy_id"]))
                signals = strategy(data, definition.parameters)
                strategy_payload = definition.model_dump()
            result = run_backtest(
                data,
                signals,
                BacktestConfig(annual_periods=365 if provider.name == "binance" else 252),
            )
            return {
                "artifact_type": "backtest",
                "symbol": symbol,
                "strategy": strategy_payload,
                "strategy_code": strategy_code,
                "ohlcv": history.rows,
                "result": result.model_dump(mode="json"),
                "citations": [item.model_dump(mode="json") for item in history.citations],
            }
        raise KeyError(f"Unknown tool: {name}")

    @staticmethod
    def _definition(
        name: str,
        description: str,
        properties: dict[str, Any],
        *,
        optional: set[str] | None = None,
    ) -> dict[str, Any]:
        optional = optional or {"limit", "start", "end"}
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": [key for key in properties if key not in optional],
                    "additionalProperties": False,
                },
            },
        }
