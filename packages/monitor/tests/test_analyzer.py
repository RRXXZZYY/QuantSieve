from datetime import UTC, datetime

import pytest
from quantsieve_monitor.analyzer import RuleBasedMarketAnalyzer
from quantsieve_monitor.models import EventKind, MonitorEvent


@pytest.mark.asyncio
async def test_rule_analyzer_maps_war_event_to_commodities() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="fixture",
        source_id="war",
        profile_id="geo",
        profile_name="Global watch",
        kind=EventKind.GEOPOLITICAL,
        title="Missile attack disrupts Red Sea shipping",
        content="The conflict may block a key shipping route.",
        url="https://example.com/war",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.method == "rules"
    assert analysis.relevance == "high"
    assert {"原油", "黄金"}.issubset({impact.asset for impact in analysis.impacts})


@pytest.mark.asyncio
async def test_rule_analyzer_does_not_treat_warning_as_war() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="fixture",
        source_id="warning",
        profile_id="geo",
        profile_name="Global watch",
        kind=EventKind.GEOPOLITICAL,
        title="UN warning on civil rights",
        content="Officials warned about restrictions on elections.",
        url="https://example.com/warning",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.relevance == "unrelated"
    assert analysis.impacts == []


@pytest.mark.asyncio
async def test_rule_analyzer_demotes_gdelt_home_invasion_crime() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="gdelt-headlines",
        source_id="local-crime",
        profile_id="global-headline-watch",
        profile_name="全球新闻标题",
        kind=EventKind.GEOPOLITICAL,
        title="Mafia figure convicted in home invasion killing",
        content="Headline-level aggregation only.",
        url="https://example.com/local-crime",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.relevance == "low"
    assert analysis.impacts == []


@pytest.mark.asyncio
async def test_rule_analyzer_demotes_privacy_invasion_and_conditional_blockade() -> None:
    occurred_at = datetime.now(UTC)
    analyzer = RuleBasedMarketAnalyzer()
    for source_id, title in (
        ("privacy", "Celebrity sues hackers for malicious invasion’ of her privacy"),
        ("conditional", "Naval blockade would mean widened war"),
    ):
        event = MonitorEvent(
            source="gdelt-headlines",
            source_id=source_id,
            profile_id="global-headline-watch",
            profile_name="全球新闻标题",
            kind=EventKind.GEOPOLITICAL,
            title=title,
            content="Headline-level aggregation only.",
            url=f"https://example.com/{source_id}",
            occurred_at=occurred_at,
            available_at=occurred_at,
        )
        analysis = await analyzer.analyze(event)
        assert analysis.relevance == "low"
        assert analysis.impacts == []


@pytest.mark.asyncio
async def test_rule_analyzer_does_not_rank_unmapped_market_title_high() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="gdelt-headlines",
        source_id="oil-commentary",
        profile_id="global-headline-watch",
        profile_name="全球新闻标题",
        kind=EventKind.GEOPOLITICAL,
        title="Crude oil prices extend losses on peace talks",
        content="Headline-level aggregation only.",
        url="https://example.com/oil-commentary",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.relevance == "low"
    assert analysis.impacts == []


@pytest.mark.asyncio
async def test_rule_analyzer_reverses_risk_premium_for_completed_deescalation() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="gdelt-headlines",
        source_id="paused-attacks",
        profile_id="global-headline-watch",
        profile_name="全球新闻标题",
        kind=EventKind.GEOPOLITICAL,
        title="Oil prices settle lower as US pauses attacks on Iran",
        content="Headline-level aggregation only; open the original report for context.",
        url="https://example.com/paused-attacks",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.relevance == "medium"
    directions = {impact.asset: impact.direction for impact in analysis.impacts}
    assert directions == {"原油": "down", "黄金": "down", "全球股票": "up"}
    assert "原始报道" in analysis.summary


@pytest.mark.asyncio
async def test_rule_analyzer_keeps_green_disaster_alert_low_priority() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="official-intel",
        source_id="green-flood",
        profile_id="disaster-watch",
        profile_name="Global disaster watch",
        kind=EventKind.GEOPOLITICAL,
        title="Green flood alert in Panama",
        content="Population affected is 0.",
        url="https://example.com/green-flood",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.relevance == "low"
    assert analysis.impacts == []


@pytest.mark.asyncio
async def test_rule_analyzer_keeps_official_conference_notice_as_background() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="official-intel",
        source_id="boj-conference",
        profile_id="boj",
        profile_name="Bank of Japan",
        kind=EventKind.GEOPOLITICAL,
        title="2026 BOJ-IMES Conference",
        content="Newsletter invitation and event registration details.",
        url="https://example.com/boj-conference",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.relevance == "low"
    assert analysis.impacts == []


@pytest.mark.asyncio
async def test_rule_analyzer_keeps_official_rate_decision_actionable() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="official-intel",
        source_id="boj-rate",
        profile_id="boj",
        profile_name="Bank of Japan",
        kind=EventKind.GEOPOLITICAL,
        title="Monetary policy meeting rate decision",
        content="The Bank announced its interest rate decision.",
        url="https://example.com/boj-rate",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.relevance in {"medium", "high"}
    assert "礼仪" not in analysis.summary


@pytest.mark.asyncio
async def test_rule_analyzer_keeps_white_house_ceremonial_notice_as_background() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="official-intel",
        source_id="white-house-veterans",
        profile_id="donald-trump",
        profile_name="Donald Trump · 白宫官方政策",
        kind=EventKind.NEWS,
        title="Presidential Message on National Korean War Veterans Armistice Day",
        content="A White House ceremonial message recognizing veterans.",
        url="https://www.whitehouse.gov/example",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.relevance == "low"
    assert analysis.impacts == []


@pytest.mark.asyncio
async def test_rule_analyzer_keeps_white_house_trade_action_available() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="official-intel",
        source_id="white-house-trade",
        profile_id="donald-trump",
        profile_name="Donald Trump · 白宫官方政策",
        kind=EventKind.NEWS,
        title="Agreement on reciprocal trade",
        content="The White House announced a trade agreement with a partner country.",
        url="https://www.whitehouse.gov/example-trade",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.relevance in {"medium", "high"}
    assert "礼仪" not in analysis.summary


@pytest.mark.asyncio
async def test_rule_analyzer_keeps_historical_gdelt_war_headline_as_background() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="gdelt-headlines",
        source_id="civil-war-cemetery",
        profile_id="global-headline-watch",
        profile_name="全球新闻标题",
        kind=EventKind.GEOPOLITICAL,
        title="Memorial Park Cemetery marks graves of Civil War veterans",
        content="Headline-only aggregated signal; open the publisher article for context.",
        url="https://publisher.example/civil-war-cemetery",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.relevance == "low"
    assert analysis.impacts == []


@pytest.mark.asyncio
async def test_rule_analyzer_keeps_current_gdelt_conflict_available() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="gdelt-headlines",
        source_id="current-conflict",
        profile_id="global-headline-watch",
        profile_name="全球新闻标题",
        kind=EventKind.GEOPOLITICAL,
        title="Missile strikes disrupt shipping after conflict escalation",
        content="Headline-only aggregated signal; open the publisher article for context.",
        url="https://publisher.example/current-conflict",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.relevance == "high"
    assert analysis.impacts


@pytest.mark.asyncio
async def test_rule_analyzer_keeps_speculative_gdelt_factory_headline_as_background() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="gdelt-headlines",
        source_id="missile-factory",
        profile_id="global-headline-watch",
        profile_name="全球新闻标题",
        kind=EventKind.GEOPOLITICAL,
        title="Zelenskyy says Ukraine intends to build drone and missile factory",
        content="Headline-only aggregated signal; open the publisher article for context.",
        url="https://publisher.example/factory",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.relevance == "low"
    assert analysis.impacts == []


@pytest.mark.asyncio
async def test_rule_analyzer_keeps_bare_central_bank_price_index_as_background() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="official-intel",
        source_id="boj-ppi",
        profile_id="central-bank-watch",
        profile_name="全球央行与宏观政策",
        kind=EventKind.NEWS,
        title="Services Producer Price Index (June)",
        content="Services Producer Price Index (June)",
        url="https://boj.example/ppi",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.relevance == "low"
    assert analysis.impacts == []


@pytest.mark.asyncio
async def test_rule_analyzer_keeps_price_index_with_policy_context_available() -> None:
    occurred_at = datetime.now(UTC)
    event = MonitorEvent(
        source="official-intel",
        source_id="boj-policy-ppi",
        profile_id="central-bank-watch",
        profile_name="全球央行与宏观政策",
        kind=EventKind.NEWS,
        title="Producer Price Index and monetary policy outlook",
        content="The Bank published an inflation forecast alongside its policy statement.",
        url="https://boj.example/policy-ppi",
        occurred_at=occurred_at,
        available_at=occurred_at,
    )

    analysis = await RuleBasedMarketAnalyzer().analyze(event)

    assert analysis.relevance in {"medium", "high"}
    assert analysis.impacts
