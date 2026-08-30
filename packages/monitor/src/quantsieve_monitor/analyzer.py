from __future__ import annotations

import json
import re
from collections.abc import Iterable

import httpx

from .models import (
    EventKind,
    MarketAnalysis,
    MarketImpact,
    MarketRelevance,
    MonitorEvent,
)


class RuleBasedMarketAnalyzer:
    """Deterministic fallback that never presents itself as an AI model."""

    async def analyze(self, event: MonitorEvent) -> MarketAnalysis:
        text = f"{event.profile_name} {event.title} {event.content}".lower()
        if _is_ceremonial_white_house_notice(event, text):
            return MarketAnalysis(
                relevance="low",
                summary=(
                    "这是白宫公开的礼仪、纪念或志愿活动公告；原文没有可核验的政策、"
                    "贸易、监管或供给动作，因此仅作为背景保留，不生成资产影响。"
                ),
                impacts=[],
                method="rules",
            )
        if _is_historical_headline_reference(event, text):
            return MarketAnalysis(
                relevance="low",
                summary=(
                    "这是新闻聚合中的历史、纪念或文化语境标题；虽然包含战争等关键词，"
                    "但原文没有识别到当前冲突、制裁、供应中断或其他可核验的市场传导动作，"
                    "因此仅作背景保留，不生成资产影响。"
                ),
                impacts=[],
                method="rules",
            )
        if deescalation := _confirmed_geopolitical_deescalation_analysis(event, text):
            return deescalation
        if _is_speculative_gdelt_headline(event, text):
            return MarketAnalysis(
                relevance="low",
                summary=(
                    "这是标题级新闻聚合，提及冲突或军工但没有识别到可核验的直接行动、"
                    "制裁或供应中断；保留原始链接等待核验，不自动生成资产影响。"
                ),
                impacts=[],
                method="rules",
            )
        if _is_informational_official_notice(event, text):
            return MarketAnalysis(
                relevance="low",
                summary=(
                    "这是官方机构的会议、简报或活动通知；当前原文未包含利率决定、"
                    "政策措辞、预测修订或其他可核验的市场传导信息，因此仅作为背景保留，"
                    "不生成资产影响。"
                ),
                impacts=[],
                method="rules",
            )
        if _is_uncontextualized_official_data_release(event, text):
            return MarketAnalysis(
                relevance="low",
                summary=(
                    "这是官方统计指标的发布标题，但原文没有提供数值、预期差或政策动作；"
                    "保留链接供核验，不把机构名称本身推演成跨资产交易信号。"
                ),
                impacts=[],
                method="rules",
            )
        if _is_low_severity_disaster_alert(text):
            return MarketAnalysis(
                relevance="low",
                summary=(
                    "这是官方发布的低等级灾害提示，但标题中没有识别到港口、能源、"
                    "供应链或重大受灾规模等市场传导链；保留作背景信息，不生成资产影响。"
                ),
                impacts=[],
                method="rules",
            )
        impacts = _deduplicate_impacts(
            impact
            for keywords, candidates in _IMPACT_RULES
            if any(_matches_keyword(text, keyword) for keyword in keywords)
            for impact in candidates
        )
        keyword_hits = sum(
            1
            for keyword in _MARKET_KEYWORDS
            if _matches_keyword(text, keyword)
        )
        relevance: MarketRelevance
        if not impacts and keyword_hits and event.source == "gdelt-headlines":
            # A title can mention a market (for example oil prices) without
            # providing an action or transmission chain.  It remains visible
            # for context, but must not be ranked as a high-priority signal.
            relevance = "low"
        elif event.kind is EventKind.GEOPOLITICAL and impacts:
            relevance = "high" if len(impacts) >= 3 else "medium"
        elif keyword_hits >= 3 or len(impacts) >= 3:
            relevance = "high"
        elif keyword_hits >= 1 or impacts:
            relevance = "medium"
        else:
            relevance = "low" if event.kind is EventKind.SOCIAL else "unrelated"

        if impacts:
            names = "、".join(impact.asset for impact in impacts[:4])
            summary = (
                f"规则识别到该事件可能通过政策、供给或风险偏好影响 {names}；"
                "方向是情景推演，需要结合后续事实和实时价格验证。"
            )
        elif event.kind is EventKind.SOCIAL:
            summary = (
                "规则引擎暂未识别出明确的市场传导链，保留原文供核验；"
                "这不等于该消息一定没有市场影响。"
            )
        else:
            summary = "当前事件未匹配到明确的可交易资产传导链。"
        return MarketAnalysis(
            relevance=relevance,
            summary=summary,
            impacts=impacts,
            method="rules",
        )


class OpenAICompatibleAnalyzer:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4.1-mini",
        client: httpx.AsyncClient | None = None,
        fallback: RuleBasedMarketAnalyzer | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = client
        self.fallback = fallback or RuleBasedMarketAnalyzer()

    async def analyze(self, event: MonitorEvent) -> MarketAnalysis:
        client = self._client or httpx.AsyncClient(timeout=60)
        try:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "temperature": 0.1,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You analyze public posts and geopolitical events for market "
                                "research. Return JSON only with keys relevance, summary, impacts. "
                                "relevance must be critical, high, medium, low, or unrelated. "
                                "summary must be concise Chinese and separate facts from "
                                "scenarios. "
                                "impacts is an array of objects with asset, direction, reason; "
                                "direction must be up, down, volatile, or uncertain. Use only the "
                                "provided event. Never invent prices, quotes, dates, or positions. "
                                "Explain uncertainty. This is not investment advice."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Source type: {event.kind.value}\n"
                                f"Profile: {event.profile_name}\n"
                                f"Title: {event.title}\n"
                                f"Event: {event.content}"
                            ),
                        },
                    ],
                },
            )
            response.raise_for_status()
            payload = response.json()
            content = str(payload["choices"][0]["message"]["content"]).strip()
            parsed = _parse_json_object(content)
            return MarketAnalysis.model_validate(
                {
                    "relevance": parsed.get("relevance", "unrated"),
                    "summary": str(parsed.get("summary", "")).strip(),
                    "impacts": parsed.get("impacts", []),
                    "method": "ai",
                }
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return await self.fallback.analyze(event)
        finally:
            if self._client is None:
                await client.aclose()


def _parse_json_object(content: str) -> dict[str, object]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("Analyzer response must be a JSON object.")
    return parsed


def _deduplicate_impacts(impacts: Iterable[MarketImpact]) -> list[MarketImpact]:
    unique: dict[tuple[str, str], MarketImpact] = {}
    for impact in impacts:
        unique.setdefault((impact.asset, impact.direction), impact)
    return list(unique.values())[:8]


def _impact(asset: str, direction: str, reason: str) -> MarketImpact:
    return MarketImpact(asset=asset, direction=direction, reason=reason)  # type: ignore[arg-type]


def _matches_keyword(text: str, keyword: str) -> bool:
    if keyword.isascii():
        return re.search(
            rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])",
            text,
        ) is not None
    return keyword in text


def _is_low_severity_disaster_alert(text: str) -> bool:
    """Keep green/zero-impact notices visible without fabricating market transmission."""

    is_disaster = any(
        _matches_keyword(text, keyword)
        for keyword in (
            "earthquake",
            "cyclone",
            "hurricane",
            "flood",
            "tsunami",
            "地震",
            "气旋",
            "飓风",
            "洪水",
            "海啸",
        )
    )
    is_low_severity = any(
        phrase in text
        for phrase in (
            "green flood alert",
            "green notification",
            "population affected is 0",
            "population affected: 0",
            "绿色洪水预警",
            "绿色预警",
            "受影响人口为 0",
        )
    )
    has_market_transmission = any(
        _matches_keyword(text, keyword)
        for keyword in (
            "port",
            "shipping",
            "pipeline",
            "refinery",
            "mine",
            "production",
            "supply",
            "power outage",
            "crop",
            "港口",
            "航运",
            "管道",
            "炼厂",
            "矿山",
            "产量",
            "供应",
            "停电",
            "农作物",
        )
    )
    return is_disaster and is_low_severity and not has_market_transmission


def _is_informational_official_notice(event: MonitorEvent, text: str) -> bool:
    """Avoid turning an event invitation into a rate or asset signal.

    Official feeds routinely publish conference, newsletter and calendar notices.
    A central-bank name alone is not evidence of a policy action, so reserve the
    generic central-bank impact rule for notices with explicit decision content.
    """

    if event.source != "official-intel":
        return False
    is_notice = any(
        _matches_keyword(text, phrase)
        for phrase in (
            "conference",
            "newsletter",
            "calendar",
            "seminar",
            "webinar",
            "event registration",
            "save the date",
            "会议",
            "通讯",
            "简报",
            "日程",
            "研讨会",
            "网络研讨会",
        )
    )
    has_decision_content = any(
        _matches_keyword(text, phrase)
        for phrase in (
            "rate decision",
            "rate cut",
            "rate hike",
            "policy statement",
            "monetary policy report",
            "meeting minutes",
            "inflation forecast",
            "economic outlook",
            "interest rate",
            "利率决议",
            "降息",
            "加息",
            "政策声明",
            "货币政策报告",
            "会议纪要",
            "通胀预测",
            "经济展望",
        )
    )
    return is_notice and not has_decision_content


def _is_ceremonial_white_house_notice(event: MonitorEvent, text: str) -> bool:
    """Keep White House ceremonial posts from triggering history-keyword rules.

    A memorial message can legitimately mention a war, but a historical
    commemoration is not evidence of an active conflict or policy action.
    Require both a narrow ceremonial cue and the absence of a substantive
    economic or executive-action cue, so actual orders and trade statements
    remain available for normal analysis.
    """

    if event.source != "official-intel" or event.profile_id != "donald-trump":
        return False
    ceremonial_cues = (
        "veterans armistice day",
        "memorial day",
        "anniversary of the liberation",
        "space exploration day",
        "christmas volunteer",
        "performer applications",
        "ceremonial",
        "纪念日",
        "阵亡将士",
        "志愿者",
        "节日",
    )
    substantive_cues = (
        "executive order",
        "proclamation on tariffs",
        "tariff",
        "trade agreement",
        "sanction",
        "regulation",
        "national security",
        "economic policy",
        "executive action",
        "行政令",
        "关税",
        "贸易协定",
        "制裁",
        "监管",
        "国家安全",
        "经济政策",
    )
    return any(_matches_keyword(text, cue) for cue in ceremonial_cues) and not any(
        _matches_keyword(text, cue) for cue in substantive_cues
    )


def _is_historical_headline_reference(event: MonitorEvent, text: str) -> bool:
    """Suppress historical-war keyword matches from the headline-only feed.

    GDELT only provides a headline and publisher link.  A cemetery, museum or
    Civil War commemoration therefore cannot support a current geopolitical
    transmission claim, even when it includes ``war`` or ``veterans``.
    Keep this guard scoped to that low-context aggregation source so an
    official statement about an active conflict is still analyzed normally.
    """

    if event.source != "gdelt-headlines":
        return False
    historical_cues = (
        "civil war",
        "cemetery",
        "memorial park",
        "historical society",
        "history museum",
        "veterans memorial",
        "战争纪念",
        "历史博物馆",
        "退伍军人公墓",
    )
    current_action_cues = (
        "missile",
        "airstrike",
        "invasion",
        "sanction",
        "blockade",
        "troops",
        "pipeline",
        "shipping",
        "导弹",
        "空袭",
        "入侵",
        "制裁",
        "封锁",
        "部队",
        "航运",
    )
    return any(_matches_keyword(text, cue) for cue in historical_cues) and not any(
        _matches_keyword(text, cue) for cue in current_action_cues
    )


def _has_direct_gdelt_action(text: str) -> bool:
    direct_action_patterns = (
        r"\b(?:missile|drone)\s+(?:attack|strike|strikes?)\b",
        r"\b(?:fires?|launched?|strikes?|hit)\s+(?:\w+\s+){0,2}(?:missiles?|drones?)\b",
        r"\bairstrikes?\b",
        r"\binvasion\b",
        r"\bsanctions?\s+(?:are\s+)?(?:imposed|target|hit)\b",
        r"\b(?:shipping|pipeline|port)\s+(?:is\s+)?(?:disrupted|blocked|closed)\b",
        r"\bblockade\b",
        r"导弹(?:袭击|攻击|击中)",
        r"(?:发射|击落).{0,8}导弹",
        r"空袭",
        r"入侵",
        r"制裁(?:措施)?(?:生效|实施|升级)",
        r"(?:航运|港口|管道).{0,8}(?:中断|封锁|关闭)",
        r"封锁",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in direct_action_patterns)


def _is_confirmed_geopolitical_deescalation(event: MonitorEvent, text: str) -> bool:
    """Recognize completed de-escalation actions without treating talks as facts.

    The normal conflict mapping is intentionally conservative and assumes that
    a direct attack can add risk premium.  Applying it unchanged to a headline
    that says attacks *were paused* inverts the event's stated direction.  We
    therefore require an already-completed action; a proposal, a negotiation,
    or conditional language remains outside this rule.
    """
    if event.kind is not EventKind.GEOPOLITICAL:
        return False
    if re.search(
        r"\b(?:would|could|may|might|if|plans?\s+to|seeks?\s+to|talks?\s+to)\b",
        text,
    ):
        return False
    completed_action_patterns = (
        r"\b(?:ceasefire|truce)\s+(?:has\s+)?(?:taken effect|begins?|began|is in effect)\b",
        r"\b(?:reached|announced)\s+(?:a\s+)?(?:ceasefire|truce)\b",
        r"\b(?:pauses?|paused|halts?|halted|suspends?|suspended)\s+(?:its\s+)?"
        r"(?:attacks?|strikes?|military operations)\b",
        r"\btensions?\s+(?:ease|eased|are easing)\b",
        r"(?:停火|休战)(?:已经|已|正式)?(?:生效|达成)",
        r"(?:暂停|停止).{0,12}(?:袭击|空袭|打击|军事行动)",
        r"局势(?:已经|已|明显)?缓和",
    )
    return any(
        re.search(pattern, text, flags=re.IGNORECASE)
        for pattern in completed_action_patterns
    )


def _confirmed_geopolitical_deescalation_analysis(
    event: MonitorEvent,
    text: str,
) -> MarketAnalysis | None:
    """Return the auditable risk-premium reversal interpretation when eligible."""
    if not _is_confirmed_geopolitical_deescalation(event, text):
        return None
    # A headline aggregation is useful for speed but is not a primary-source
    # confirmation. Keep a factual de-escalation visible without ranking it
    # as a definitive trading signal.
    return MarketAnalysis(
        relevance="medium" if event.source == "gdelt-headlines" else "high",
        summary=(
            "事件标题明确描述了停火生效、攻击暂停或紧张局势已经缓和；"
            "若后续原文核验属实，地缘风险溢价可能回落。标题级来源仍需打开原始报道确认，"
            "不把这一情景推演当作价格预测。"
        ),
        impacts=[
            _impact("原油", "down", "供应与航运中断风险若缓和，能源风险溢价可能回落。"),
            _impact("黄金", "down", "避险需求若降温，黄金的地缘风险溢价可能回落。"),
            _impact("全球股票", "up", "风险溢价若下降，风险资产情绪可能改善。"),
        ],
        method="rules",
    )


def _is_speculative_gdelt_headline(event: MonitorEvent, text: str) -> bool:
    """Do not turn low-context GDELT references into a conflict signal.

    A headline about a proposed factory can contain both ``drone`` and
    ``missile`` without recording a current attack or supply disruption.  The
    aggregation source does not supply enough context to infer a commodity or
    risk-premium transmission in that case.  Direct, current actions remain
    available to the normal rules below.
    """

    if event.source != "gdelt-headlines":
        return False
    # "Home invasion" and similar local-crime wording is unrelated to a
    # sovereign invasion.  GDELT supplies only a headline, so there is no
    # safe basis to infer geopolitical risk from that lexical overlap.
    if any(
        phrase in text
        for phrase in (
            "home invasion",
            "home-invasion",
            "mafia figure",
            "club owner killing",
        )
    ):
        return True
    if re.search(r"\binvasion[’']?\s+of\s+(?:\w+\s+){0,2}privacy\b", text):
        return True
    mentions_conflict = any(
        _matches_keyword(text, phrase)
        for phrase in (
            "war",
            "missile",
            "drone",
            "attack",
            "ukraine",
            "军工",
            "战争",
            "导弹",
            "无人机",
            "袭击",
            "乌克兰",
        )
    )
    if mentions_conflict and re.search(
        r"\b(?:would|could|may|might|intends?\s+to|plans?\s+to|proposes?\s+to)\b",
        text,
    ):
        return True
    return mentions_conflict and not _has_direct_gdelt_action(text)


def _is_uncontextualized_official_data_release(event: MonitorEvent, text: str) -> bool:
    """Keep a bare central-bank statistics headline out of the signal queue.

    A price-index release can matter after its value and surprise are known,
    but a feed title alone is not evidence of a rate-path change.  This is
    deliberately narrow: decisions, forecasts and releases with an explicit
    policy action continue through the normal central-bank rule.
    """

    if event.source != "official-intel" or event.profile_id != "central-bank-watch":
        return False
    data_release_cues = (
        "services producer price index",
        "corporate goods price index",
        "producer price index",
        "price index (",
    )
    policy_or_context_cues = (
        "rate decision",
        "rate cut",
        "rate hike",
        "policy statement",
        "monetary policy",
        "inflation forecast",
        "economic outlook",
        "interest rate",
        "利率决议",
        "降息",
        "加息",
        "政策声明",
        "货币政策",
        "通胀预测",
        "经济展望",
    )
    return any(_matches_keyword(text, cue) for cue in data_release_cues) and not any(
        _matches_keyword(text, cue) for cue in policy_or_context_cues
    )


_MARKET_KEYWORDS = (
    "stock",
    "market",
    "tariff",
    "sanction",
    "interest rate",
    "fed",
    "ecb",
    "pboc",
    "boj",
    "central bank",
    "inflation",
    "bitcoin",
    "crypto",
    "tesla",
    "oil",
    "gas",
    "gold",
    "opec",
    "lng",
    "cyberattack",
    "earthquake",
    "flood",
    "hurricane",
    "war",
    "missile",
    "attack",
    "blockade",
    "trade",
    "股市",
    "关税",
    "制裁",
    "利率",
    "比特币",
    "原油",
    "黄金",
    "央行",
    "欧央行",
    "日本央行",
    "欧佩克",
    "天然气",
    "网络攻击",
    "地震",
    "洪水",
    "飓风",
    "战争",
)

_IMPACT_RULES: tuple[tuple[tuple[str, ...], tuple[MarketImpact, ...]], ...] = (
    (
        ("tesla", "tsla", "robotaxi", "gigafactory"),
        (_impact("TSLA", "volatile", "人物消息可能改变对特斯拉增长或监管路径的预期。"),),
    ),
    (
        ("bitcoin", "btc", "crypto", "doge", "加密", "比特币"),
        (
            _impact("BTCUSDT", "volatile", "公开表态可能快速改变加密市场风险偏好。"),
            _impact("COIN", "volatile", "加密交易活跃度预期可能影响相关股票。"),
        ),
    ),
    (
        ("tariff", "trade war", "关税", "贸易战"),
        (
            _impact("全球股票", "down", "关税升级可能压缩利润并提高供应链成本。"),
            _impact("美元", "volatile", "贸易与通胀预期可能改变美元路径。"),
            _impact("黄金", "up", "政策不确定性通常会提高避险需求。"),
        ),
    ),
    (
        ("sanction", "制裁", "embargo", "禁运"),
        (
            _impact("原油", "up", "产油国或运输相关制裁可能收紧可交付供应。"),
            _impact("黄金", "up", "地缘风险和支付限制可能提升避险需求。"),
        ),
    ),
    (
        (
            "war",
            "missile",
            "missiles",
            "airstrike",
            "airstrikes",
            "attack",
            "attacks",
            "attacked",
            "invasion",
            "战争",
            "导弹",
            "空袭",
            "袭击",
        ),
        (
            _impact("黄金", "up", "武装冲突升级通常提高避险需求。"),
            _impact("原油", "volatile", "供应与运输受阻风险会放大能源价格波动。"),
            _impact("全球股票", "down", "风险溢价上升可能压制股票估值。"),
            _impact("国防军工", "up", "安全支出预期可能上升。"),
        ),
    ),
    (
        ("red sea", "strait of hormuz", "shipping", "blockade", "红海", "霍尔木兹", "封锁"),
        (
            _impact("原油", "up", "关键航道受阻可能抬高能源运输成本。"),
            _impact("航运", "volatile", "绕航和保险成本可能显著变化。"),
        ),
    ),
    (
        ("ukraine", "black sea", "grain", "wheat", "乌克兰", "黑海", "粮食", "小麦"),
        (
            _impact("小麦", "up", "黑海出口或农业生产中断可能收紧粮食供应。"),
            _impact("天然气", "volatile", "欧洲能源通道风险可能重新定价。"),
        ),
    ),
    (
        ("pipeline", "natural gas", "lng", "管道", "天然气"),
        (_impact("天然气", "up", "基础设施或供应中断可能收紧区域供给。"),),
    ),
    (
        (
            "opec",
            "oil production",
            "oil supply",
            "crude output",
            "欧佩克",
            "原油产量",
            "石油供应",
        ),
        (
            _impact("原油", "volatile", "产量政策与供给预期变化可能直接影响油价。"),
            _impact("能源股", "volatile", "油价路径可能改变能源企业盈利预期。"),
            _impact("通胀预期", "volatile", "能源成本会向运输与消费价格传导。"),
        ),
    ),
    (
        (
            "fed",
            "ecb",
            "pboc",
            "boj",
            "central bank",
            "interest rate",
            "rate cut",
            "rate hike",
            "美联储",
            "欧洲央行",
            "中国人民银行",
            "日本央行",
            "央行",
            "降息",
            "加息",
        ),
        (
            _impact("美股", "volatile", "利率路径会影响估值和融资成本。"),
            _impact("美元", "volatile", "利差预期可能改变美元方向。"),
            _impact("黄金", "volatile", "实际利率变化会影响无息资产吸引力。"),
        ),
    ),
    (
        ("cyberattack", "ransomware", "data breach", "hack", "网络攻击", "勒索", "数据泄露"),
        (
            _impact("科技股", "volatile", "重大网络事件可能带来停机、赔偿和监管成本。"),
            _impact("加密资产", "volatile", "交易所或基础设施安全事件会冲击流动性与信心。"),
        ),
    ),
    (
        (
            "earthquake",
            "cyclone",
            "hurricane",
            "flood",
            "tsunami",
            "地震",
            "气旋",
            "飓风",
            "洪水",
            "海啸",
        ),
        (
            _impact("大宗商品", "volatile", "重大灾害可能扰动生产、港口和运输网络。"),
            _impact("保险", "volatile", "灾损规模可能改变赔付和再保险预期。"),
        ),
    ),
    (
        (
            "semiconductor",
            "chip export",
            "export control",
            "nvidia",
            "半导体",
            "芯片出口",
            "出口管制",
            "英伟达",
        ),
        (
            _impact("半导体", "volatile", "出口限制或供应变化可能重塑销售与估值预期。"),
            _impact("科技股", "volatile", "芯片供应与政策变化会传导至人工智能产业链。"),
        ),
    ),
)
