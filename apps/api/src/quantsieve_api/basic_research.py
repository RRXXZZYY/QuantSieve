from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
from quantsieve_providers import DataEnvelope, find_popular_instrument_mentions

from .providers import ProviderRouter, RequestedProvider
from .schemas import ChatRequest, ChatResponse

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


async def build_basic_research(
    providers: ProviderRouter,
    body: ChatRequest,
    event_handler: EventHandler | None = None,
) -> ChatResponse:
    """Build a question-aware, no-key research brief from independently sourced modules."""
    question = body.messages[-1].content.strip()
    symbol, provider_name, name = _resolve_instrument(body, question)
    adapter = providers.resolve(symbol, provider_name)
    optional_loaders: dict[str, Awaitable[DataEnvelope]] = {}
    if adapter.name == "yfinance":
        optional_loaders = {
            "get_fundamentals": adapter.fundamentals(symbol),
            "get_news": adapter.news(symbol, 8),
        }
    elif adapter.name == "akshare":
        optional_loaders = {
            "get_fundamentals": adapter.fundamentals(symbol),
            "get_news": adapter.news(symbol, 8),
            "get_capital_flow": adapter.capital_flow(symbol),
        }
    loader_names = ["get_quote", "get_history", *optional_loaders]
    if event_handler:
        for loader_name in loader_names:
            await event_handler({"type": "tool_start", "name": loader_name})
    loader_results = await asyncio.gather(
        _with_timeout(adapter.quote(symbol), seconds=20),
        _with_timeout(
            adapter.history(symbol, date.today() - timedelta(days=730), date.today()),
            seconds=45,
        ),
        *(_with_timeout(loader, seconds=20) for loader in optional_loaders.values()),
        return_exceptions=True,
    )
    results = dict(zip(loader_names, loader_results, strict=True))
    history_result = results["get_history"]
    if isinstance(history_result, BaseException):
        raise RuntimeError(f"{symbol} 历史行情读取失败：{type(history_result).__name__}")
    assert isinstance(history_result, DataEnvelope)
    if event_handler:
        for loader_name, result in results.items():
            await event_handler(
                {
                    "type": "tool_done",
                    "name": loader_name,
                    "ok": not isinstance(result, BaseException),
                }
            )
        await event_handler({"type": "composing"})

    frame = history_result.to_frame().sort_index()
    if frame.empty or "close" not in frame:
        raise ValueError(f"{symbol} 没有可用于基础研究的历史行情。")
    close = pd.to_numeric(frame["close"], errors="coerce").dropna()
    if close.empty:
        raise ValueError(f"{symbol} 的历史行情没有有效收盘价。")
    latest = float(close.iloc[-1])
    previous = float(close.iloc[-2]) if len(close) > 1 else latest
    daily_change = latest / previous - 1 if previous else 0.0
    annual_periods = 365 if adapter.name == "binance" else 252
    one_year = close.iloc[-min(len(close), annual_periods + 1) :]
    high = float(one_year.max())
    low = float(one_year.min())
    range_position = (latest - low) / (high - low) if high > low else 0.5
    returns = close.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    volatility = float(returns.std(ddof=0) * (annual_periods**0.5))
    equity = close / float(close.iloc[0])
    drawdown = equity / equity.cummax() - 1
    max_drawdown = float(drawdown.min())
    current_drawdown = latest / float(close.cummax().iloc[-1]) - 1
    return_20 = _period_return(close, 20)
    return_60 = _period_return(close, 60)
    return_year = _period_return(close, annual_periods)
    moving_averages = {
        period: float(close.iloc[-period:].mean()) if len(close) >= period else None
        for period in (20, 60, 200)
    }
    trend_label = _trend_label(latest, moving_averages)
    as_of = _date_value(close.index[-1])
    is_reference_series = bool(history_result.metadata.get("reference_series"))
    observation_label = "官方参考值" if is_reference_series else "收盘价"
    sample_label = (
        f"{len(close)} 个官方日频参考观测"
        if is_reference_series
        else f"{len(close)} 根真实日 K 线"
    )
    reference_notice = (
        "• 该序列不是可成交报价；源仅提供日频参考/收盘值，不能据此推断盘中开高低，"
        "策略成交需另接券商或交易所行情验证。\n"
        if is_reference_series
        else ""
    )
    modules = _module_summaries(results)
    completed_modules = [module["label"] for module in modules if module["status"] == "ready"]
    unavailable_modules = [
        module["label"] for module in modules if module["status"] != "ready"
    ]
    module_lines = "\n".join(
        f"• {module['label']}：{module['detail']}" for module in modules
    )
    content = (
        f"{name}（{symbol}）免 Key 研究报告\n"
        f"研究问题：{question}\n\n"
        "价格与趋势\n"
        f"• 最新可用{observation_label} {_number(latest)}（{as_of}），最近一个数据周期"
        f" {_percent(daily_change)}。\n"
        f"• 20 / 60 / 近一年收益分别为 {_percent(return_20)}、"
        f"{_percent(return_60)}、{_percent(return_year)}；当前判定为“{trend_label}”。\n"
        f"• 近一年区间 {_number(low)} — {_number(high)}，现价位于区间"
        f" {_percent(range_position)} 位置。\n\n"
        "风险画像\n"
        f"• 两年样本最大回撤 {_percent(max_drawdown)}，当前距样本高点"
        f" {_percent(current_drawdown)}，年化波动率 {_percent(volatility)}。\n"
        f"• 本次使用 {sample_label}；趋势和风险均由同一组可追溯数据计算。\n"
        f"{reference_notice}\n"
        "补充证据模块\n"
        f"{module_lines}\n\n"
        f"本轮已完成：{'、'.join(completed_modules) or '价格与风险'}。"
        f"{'暂不可用：' + '、'.join(unavailable_modules) + '。' if unavailable_modules else ''}\n"
        "这是一份确定性证据报告：它会回答数据能回答的部分，不会在缺少模型时假装完成"
        "主观推理。历史表现不代表未来收益，也不构成投资建议。"
    )
    envelopes = [
        result
        for result in results.values()
        if isinstance(result, DataEnvelope)
    ]
    citations = _unique_citations(
        [
            item.model_dump(mode="json")
            for envelope in envelopes
            for item in envelope.citations
        ]
    )
    return ChatResponse(
        content=content,
        citations=citations,
        tool_calls=[*loader_names, "question_aware_research_snapshot"],
        grounded=True,
        artifacts=[
            {
                "artifact_type": "research_snapshot",
                "symbol": symbol,
                "name": name,
                "question": question,
                "as_of": as_of,
                "bars": len(close),
                "price": latest,
                "reference_series": is_reference_series,
                "returns": {
                    "one_period": daily_change,
                    "twenty_period": return_20,
                    "sixty_period": return_60,
                    "one_year": return_year,
                },
                "risk": {
                    "annualized_volatility": volatility,
                    "max_drawdown": max_drawdown,
                    "current_drawdown": current_drawdown,
                },
                "trend": {
                    "label": trend_label,
                    "ma20": moving_averages[20],
                    "ma60": moving_averages[60],
                    "ma200": moving_averages[200],
                },
                "range": {
                    "low": low,
                    "high": high,
                    "position": range_position,
                },
                "modules": modules,
            }
        ],
    )


def _resolve_instrument(
    body: ChatRequest,
    question: str,
) -> tuple[str, RequestedProvider, str]:
    if body.symbol:
        return (
            body.symbol.strip().upper(),
            body.provider,
            body.instrument_name or body.symbol.strip().upper(),
        )
    mentions = find_popular_instrument_mentions(question)
    if not mentions:
        raise ValueError(
            "免 Key 研究需要一个明确标的。可以直接写“分析苹果 / BTCUSDT / 原油”，"
            "也可以先在搜索框选择标的；开放式多标的研究需要配置 API Key。"
        )
    if len(mentions) > 1:
        choices = "、".join(f"{item.name}（{item.symbol}）" for item in mentions)
        raise ValueError(f"免 Key 模式一次研究一个标的；检测到 {choices}，请先选择其中一个。")
    instrument = mentions[0]
    return instrument.symbol, instrument.provider, instrument.name


async def _with_timeout(
    operation: Awaitable[DataEnvelope],
    *,
    seconds: float,
) -> DataEnvelope:
    return await asyncio.wait_for(operation, timeout=seconds)


def _period_return(close: pd.Series, periods: int) -> float:
    if len(close) < 2:
        return 0.0
    start = float(close.iloc[-min(len(close), periods + 1)])
    return float(close.iloc[-1]) / start - 1 if start else 0.0


def _trend_label(latest: float, averages: dict[int, float | None]) -> str:
    ma20, ma60, ma200 = averages[20], averages[60], averages[200]
    if ma200 is not None and latest > ma20 > ma60 > ma200:  # type: ignore[operator]
        return "长期与中期趋势共振向上"
    if ma200 is not None and latest < ma20 < ma60 < ma200:  # type: ignore[operator]
        return "长期与中期趋势共振向下"
    if ma60 is not None and latest > ma60:
        return "价格位于中期均线上方，但趋势尚未完全共振"
    if ma60 is not None:
        return "价格位于中期均线下方，需防范弱势延续"
    return "样本不足，暂不判定中长期趋势"


def _module_summaries(
    results: dict[str, DataEnvelope | BaseException],
) -> list[dict[str, str]]:
    labels = {
        "get_quote": "最新行情",
        "get_history": "历史行情",
        "get_fundamentals": "财务数据",
        "get_news": "近期新闻",
        "get_capital_flow": "资金流",
    }
    modules: list[dict[str, str]] = []
    for key, result in results.items():
        label = labels[key]
        if isinstance(result, BaseException):
            modules.append(
                {
                    "id": key,
                    "label": label,
                    "status": "unavailable",
                    "detail": f"上游未返回（{type(result).__name__}），本报告未据此推断。",
                }
            )
            continue
        if key in {"get_quote", "get_history"}:
            detail = f"已读取 {len(result.rows)} 条真实数据。"
        elif key == "get_fundamentals":
            detail = _fundamentals_detail(result.rows)
        elif key == "get_news":
            detail = _news_detail(result.rows)
        else:
            detail = _capital_flow_detail(result.rows)
        modules.append(
            {
                "id": key,
                "label": label,
                "status": "ready" if result.rows else "unavailable",
                "detail": detail,
            }
        )
    return modules


def _fundamentals_detail(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "当前数据源没有返回可比财务记录，本报告未补写估值或利润数字。"
    facts = _row_facts(
        rows[0],
        (
            "营业总收入",
            "营业收入",
            "净利润",
            "基本每股收益",
            "total revenue",
            "operating income",
            "net income",
            "basic eps",
        ),
    )
    return (
        f"已读取 {len(rows)} 个报告期；最新记录：{'；'.join(facts)}。"
        if facts
        else f"已读取 {len(rows)} 个报告期，但字段口径需要在原始来源中逐项核对。"
    )


def _news_detail(rows: list[dict[str, Any]]) -> str:
    titles: list[str] = []
    for row in rows:
        nested = row.get("content")
        nested_title = nested.get("title") if isinstance(nested, dict) else None
        title = next(
            (
                row.get(key)
                for key in ("新闻标题", "标题", "title")
                if row.get(key)
            ),
            nested_title,
        )
        if title:
            titles.append(str(title).strip())
        if len(titles) == 3:
            break
    if not titles:
        return "当前数据源没有返回可核验新闻标题，不以旧消息填充。"
    return f"读取 {len(rows)} 条，最新标题包括：{'；'.join(titles)}。"


def _capital_flow_detail(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "当前市场或数据源没有可比资金流数据。"
    facts = _row_facts(
        rows[-1],
        (
            "日期",
            "收盘价",
            "涨跌幅",
            "主力净流入-净额",
            "超大单净流入-净额",
            "大单净流入-净额",
        ),
    )
    return f"最新记录：{'；'.join(facts)}。" if facts else f"已读取 {len(rows)} 条资金流记录。"


def _row_facts(row: dict[str, Any], preferred_keys: tuple[str, ...]) -> list[str]:
    facts: list[str] = []
    lowered = {str(key).casefold(): (str(key), value) for key, value in row.items()}
    for preferred in preferred_keys:
        match = next(
            (
                pair
                for lowered_key, pair in lowered.items()
                if preferred.casefold() in lowered_key
            ),
            None,
        )
        if match is None:
            continue
        key, value = match
        if value is None or (isinstance(value, float) and not np.isfinite(value)):
            continue
        facts.append(f"{key}={_compact_value(value)}")
        if len(facts) == 4:
            break
    return facts


def _compact_value(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if abs(numeric) >= 100_000_000:
            return f"{numeric / 100_000_000:.2f} 亿"
        if abs(numeric) >= 10_000:
            return f"{numeric / 10_000:.2f} 万"
        return _number(numeric)
    return str(value)[:80]


def _unique_citations(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[object, object]] = set()
    unique: list[dict[str, Any]] = []
    for citation in citations:
        key = (citation.get("source"), citation.get("url"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(citation)
    return unique


def _number(value: float) -> str:
    if abs(value) >= 1_000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _percent(value: float) -> str:
    return f"{value * 100:+.2f}%"


def _date_value(value: object) -> str:
    date_method = getattr(value, "date", None)
    if callable(date_method):
        date_value = date_method()
        if hasattr(date_value, "isoformat"):
            return str(date_value.isoformat())
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
