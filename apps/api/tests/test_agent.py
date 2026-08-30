from __future__ import annotations

import json
from typing import Any, ClassVar

import httpx
import pytest
from quantsieve_api.agent import ResearchAgent
from quantsieve_api.schemas import ChatMessage


class FakeTools:
    definitions: ClassVar[list[dict[str, Any]]] = [
        {"type": "function", "function": {"name": "fixture"}}
    ]

    def __init__(self, results: dict[str, dict[str, Any]]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return self.results[name]


def completion(message: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"message": message}]}


@pytest.mark.asyncio
async def test_agent_resolves_name_then_fetches_sourced_quote() -> None:
    replies = iter(
        [
            completion(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "search-1",
                            "type": "function",
                            "function": {
                                "name": "search_instruments",
                                "arguments": json.dumps({"query": "贵州茅台"}),
                            },
                        }
                    ],
                }
            ),
            completion(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "quote-1",
                            "type": "function",
                            "function": {
                                "name": "get_quote",
                                "arguments": json.dumps({"symbol": "600519"}),
                            },
                        }
                    ],
                }
            ),
            completion({"role": "assistant", "content": "最新收盘价为 123.45。"}),
        ]
    )
    request_messages: list[list[dict[str, Any]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_messages.append(body["messages"])
        return httpx.Response(200, request=request, json=next(replies))

    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    tools = FakeTools(
        {
            "search_instruments": {
                "instruments": [{"symbol": "600519", "name": "贵州茅台"}],
                "source": "fixture catalog",
            },
            "get_quote": {
                "symbol": "600519",
                "kind": "quote",
                "rows": [{"date": "2026-07-24", "close": 123.45}],
                "citations": [{"source": "fixture", "retrieved_at": "2026-07-26"}],
            },
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        agent = ResearchAgent(
            tools,  # type: ignore[arg-type]
            "test-key",
            base_url="https://llm.example/v1",
            model="test-model",
            client=client,
            event_handler=emit,
        )
        result = await agent.respond([ChatMessage(role="user", content="贵州茅台现在多少钱？")])

    assert [name for name, _arguments in tools.calls] == [
        "search_instruments",
        "get_quote",
    ]
    assert result.content == "最新收盘价为 123.45。"
    assert result.citations[0]["source"] == "fixture"
    assert result.grounded is True
    assert [event["type"] for event in events] == [
        "thinking",
        "tool_start",
        "tool_done",
        "thinking",
        "tool_start",
        "tool_done",
        "thinking",
        "composing",
    ]
    assert any(message["role"] == "tool" for message in request_messages[-1])


@pytest.mark.asyncio
async def test_agent_preserves_custom_backtest_artifact_but_compacts_llm_context() -> None:
    replies = iter(
        [
            completion(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "backtest-1",
                            "type": "function",
                            "function": {
                                "name": "run_custom_backtest",
                                "arguments": json.dumps(
                                    {
                                        "symbol": "AAPL",
                                        "strategy_name": "Trend",
                                        "code": (
                                            "def generate_signals(data):\n"
                                            "    return (data['close'] > 0).astype(float)\n"
                                        ),
                                    }
                                ),
                            },
                        }
                    ],
                }
            ),
            completion({"role": "assistant", "content": "年化收益为 12.5%。"}),
        ]
    )
    request_bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        return httpx.Response(200, request=request, json=next(replies))

    artifact = {
        "artifact_type": "backtest",
        "symbol": "AAPL",
        "strategy_code": "def generate_signals(data):\n    return data['close'] > 0\n",
        "ohlcv": [{"date": "2026-07-24", "close": 200.0}],
        "result": {
            "metrics": {"annualized_return_pct": 12.5},
            "equity": [{"date": "2026-07-24", "equity": 1.0}],
        },
        "citations": [{"source": "fixture"}],
    }
    tools = FakeTools({"run_custom_backtest": artifact})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        agent = ResearchAgent(
            tools,  # type: ignore[arg-type]
            "test-key",
            base_url="https://llm.example/v1",
            model="test-model",
            client=client,
        )
        result = await agent.respond([ChatMessage(role="user", content="写一个趋势策略")])

    assert result.artifacts == [artifact]
    assert result.grounded is True
    tool_message = next(
        message
        for message in request_bodies[-1]["messages"]
        if message["role"] == "tool"
    )
    compacted = json.loads(tool_message["content"])
    assert "ohlcv" not in compacted
    assert "strategy_code" not in compacted
    assert compacted["result"] == {"metrics": {"annualized_return_pct": 12.5}}
