from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .grounding import enforce_grounded_numbers
from .schemas import ChatMessage, ChatResponse
from .tools import ResearchTools

SYSTEM_PROMPT = """You are QuantSieve, a cautious investment-research assistant.
Always call tools before making any factual or numeric market claim. Every number in the final
answer must appear in tool output. Cite source names and retrieval/as-of timestamps near claims.
If a data source is unavailable, say so plainly. Separate facts from interpretation. Never give
personalized investment advice or promise returns. Resolve company names with search_instruments
instead of guessing tickers. When a user asks for a strategy that is not an existing template,
write a generate_signals(data) function and call run_custom_backtest so the sandbox actually
executes it. Reply in the user's language."""


class ResearchAgent:
    def __init__(
        self,
        tools: ResearchTools,
        api_key: str,
        *,
        base_url: str,
        model: str,
        client: httpx.AsyncClient | None = None,
        event_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.tools = tools
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = client
        self._event_handler = event_handler

    async def respond(self, messages: list[ChatMessage]) -> ChatResponse:
        conversation: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *[message.model_dump() for message in messages],
        ]
        payloads: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        called: list[str] = []
        citations: list[dict[str, Any]] = []
        for round_index in range(4):
            await self._emit("thinking", round=round_index + 1)
            message = await self._completion(conversation)
            conversation.append(message)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                await self._emit("composing")
                content = str(message.get("content") or "")
                grounded_content, grounded = enforce_grounded_numbers(content, payloads)
                return ChatResponse(
                    content=grounded_content,
                    citations=citations,
                    tool_calls=called,
                    grounded=grounded,
                    artifacts=artifacts,
                )
            for tool_call in tool_calls:
                name = str(tool_call["function"]["name"])
                arguments = json.loads(tool_call["function"].get("arguments") or "{}")
                await self._emit("tool_start", name=name)
                result = await self.tools.call(name, arguments)
                payloads.append(result)
                if result.get("artifact_type"):
                    artifacts.append(result)
                called.append(name)
                citations.extend(list(result.get("citations", [])))
                await self._emit("tool_done", name=name)
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": json.dumps(
                            self._llm_tool_payload(result),
                            ensure_ascii=False,
                            default=str,
                        ),
                    }
                )
        raise RuntimeError("The agent exceeded the tool-call limit.")

    async def _emit(self, event_type: str, **payload: Any) -> None:
        if self._event_handler:
            await self._event_handler({"type": event_type, **payload})

    @staticmethod
    def _llm_tool_payload(result: dict[str, Any]) -> dict[str, Any]:
        if result.get("artifact_type") != "backtest":
            return result
        backtest = dict(result)
        backtest.pop("ohlcv", None)
        backtest.pop("strategy_code", None)
        result_payload = dict(backtest.get("result") or {})
        backtest["result"] = {"metrics": result_payload.get("metrics", {})}
        return backtest

    async def _completion(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=90)
        try:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "temperature": 0.1,
                    "messages": messages,
                    "tools": self.tools.definitions,
                    "tool_choice": "auto",
                },
            )
            response.raise_for_status()
            return dict(response.json()["choices"][0]["message"])
        finally:
            if self._client is None:
                await client.aclose()
