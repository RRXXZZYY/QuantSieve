from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from ..agent import ResearchAgent
from ..basic_research import build_basic_research
from ..config import Settings
from ..demos import load_demos
from ..schemas import ChatRequest, ChatResponse
from ..tools import ResearchTools

router = APIRouter(tags=["chat"])


@router.get("/demos")
async def demos() -> list[dict[str, Any]]:
    return load_demos()


@router.post("/chat")
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    return await _run_chat(request, body)


async def _run_chat(
    request: Request,
    body: ChatRequest,
    event_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> ChatResponse:
    demo = _demo_response(body)
    if demo:
        if event_handler:
            await event_handler({"type": "demo", "name": body.demo_id})
        return demo
    settings: Settings = request.app.state.settings
    api_key = body.api_key.get_secret_value() if body.api_key else settings.llm_api_key
    if not api_key:
        try:
            return await build_basic_research(
                request.app.state.providers,
                body,
                event_handler,
            )
        except (RuntimeError, ValueError, LookupError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    agent = ResearchAgent(
        ResearchTools(request.app.state.providers),
        api_key,
        base_url=body.base_url or settings.llm_base_url,
        model=body.model or settings.llm_model,
        event_handler=event_handler,
    )
    try:
        return await agent.respond(body.messages)
    except httpx_error_types() as exc:
        raise HTTPException(status_code=502, detail=f"LLM provider error: {exc}") from exc


@router.post("/chat/stream")
async def stream_chat(request: Request, body: ChatRequest) -> EventSourceResponse:
    async def events() -> Any:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def emit(event: dict[str, Any]) -> None:
            await queue.put(event)

        async def produce() -> None:
            try:
                await emit({"type": "started"})
                response = await _run_chat(request, body, emit)
                await emit({"type": "response", "response": response})
            except HTTPException as exc:
                await emit({"type": "error", "detail": exc.detail})
            except Exception as exc:
                await emit({"type": "error", "detail": str(exc)})
            finally:
                await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=8)
                except TimeoutError:
                    yield {
                        "event": "status",
                        "data": json.dumps({"type": "heartbeat"}, ensure_ascii=False),
                    }
                    continue
                if event is None:
                    break
                event_type = str(event["type"])
                if event_type == "response":
                    response = event["response"]
                    assert isinstance(response, ChatResponse)
                    for offset in range(0, len(response.content), 24):
                        yield {
                            "event": "token",
                            "data": json.dumps(
                                {"text": response.content[offset : offset + 24]},
                                ensure_ascii=False,
                            ),
                        }
                    yield {"event": "done", "data": response.model_dump_json()}
                elif event_type == "error":
                    yield {
                        "event": "error",
                        "data": json.dumps({"detail": event["detail"]}, ensure_ascii=False),
                    }
                else:
                    yield {
                        "event": "status",
                        "data": json.dumps(event, ensure_ascii=False),
                    }
        finally:
            await producer

    return EventSourceResponse(events())


def _demo_response(body: ChatRequest) -> ChatResponse | None:
    if not body.demo_id:
        return None
    demo = next((item for item in load_demos() if item["id"] == body.demo_id), None)
    if not demo:
        raise HTTPException(status_code=404, detail="Unknown demo.")
    return ChatResponse(
        content=demo["answer"],
        citations=demo["citations"],
        tool_calls=["recorded-demo"],
        grounded=True,
    )


def httpx_error_types() -> tuple[type[Exception], ...]:
    import httpx

    return (httpx.HTTPError, RuntimeError, ValueError, KeyError)
