from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import quantsieve_api.routers.portfolio_paper as portfolio_paper_router_module
from fastapi import FastAPI
from fastapi.testclient import TestClient
from quantsieve_api.portfolio_paper_preflight import (
    review_portfolio_paper_readiness,
)
from quantsieve_api.routers.portfolio_paper import router
from quantsieve_providers import BinanceSpotTradingRules

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
EXPERIMENT_ID = "a" * 32


def _rules(symbol: str) -> BinanceSpotTradingRules:
    base_asset = symbol.removesuffix("USDT")
    return BinanceSpotTradingRules(
        symbol=symbol,
        base_asset=base_asset,
        quote_asset="USDT",
        status="TRADING",
        spot_trading_allowed=True,
        order_types=("LIMIT", "MARKET"),
        lot_step_size=Decimal("0.001"),
        lot_min_quantity=Decimal("0.001"),
        lot_max_quantity=Decimal("100000"),
        market_step_size=Decimal("0.001"),
        market_min_quantity=Decimal("0.001"),
        market_max_quantity=Decimal("100000"),
        min_notional=Decimal("5"),
        min_notional_applies_to_market=True,
        max_notional=None,
        max_notional_applies_to_market=False,
        notional_average_price_minutes=5,
        verified_at=NOW,
    )


def _experiment(*assets: tuple[str, str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        id=EXPERIMENT_ID,
        assets=tuple(
            SimpleNamespace(
                symbol=symbol,
                name=symbol,
                provider=provider,
                currency=currency,
            )
            for symbol, provider, currency in assets
        ),
    )


class FakeRulesProvider:
    def __init__(self, rules: dict[str, BinanceSpotTradingRules]) -> None:
        self.rules = rules
        self.calls: list[str] = []
        self.failures: dict[str, Exception] = {}

    async def trading_rules(self, symbol: str) -> BinanceSpotTradingRules:
        self.calls.append(symbol)
        if symbol in self.failures:
            raise self.failures[symbol]
        return self.rules[symbol]


class FakeExperimentStore:
    def __init__(self, experiment: SimpleNamespace | None) -> None:
        self.experiment = experiment
        self.requested_ids: list[str] = []

    def get_portfolio(self, experiment_id: str) -> SimpleNamespace | None:
        self.requested_ids.append(experiment_id)
        return self.experiment if experiment_id == EXPERIMENT_ID else None


@pytest.mark.asyncio
async def test_readiness_reviews_live_rules_without_creating_any_track() -> None:
    experiment = _experiment(
        ("BTCUSDT", "binance", "USDT"),
        ("ETHUSDT", "binance", "USDT"),
    )
    provider = FakeRulesProvider(
        {"BTCUSDT": _rules("BTCUSDT"), "ETHUSDT": _rules("ETHUSDT")}
    )

    result = await review_portfolio_paper_readiness(
        experiment,
        provider=provider,
        now=NOW,
    )

    assert result.model_dump(mode="json") == {
        "availability": "internal_only",
        "scope": "one_session_modeled_observation",
        "activation_available": False,
        "review_status": "ready_for_internal_review",
        "portfolio_experiment_id": EXPERIMENT_ID,
        "reviewed_at": "2026-07-27T12:00:00Z",
        "assets": [
            {
                "symbol": "BTCUSDT",
                "status": "eligible_for_internal_review",
                "reasons": [
                    "已验证为同一 Binance Spot 场所、24/7 UTC 日历和精确 USDT 结算。"
                ],
                "rules_verified_at": "2026-07-27T12:00:00Z",
            },
            {
                "symbol": "ETHUSDT",
                "status": "eligible_for_internal_review",
                "reasons": [
                    "已验证为同一 Binance Spot 场所、24/7 UTC 日历和精确 USDT 结算。"
                ],
                "rules_verified_at": "2026-07-27T12:00:00Z",
            },
        ],
        "next_step": (
            "交易规则复核通过，可进入内部人工审查；此结果不会创建观察、不会启动调度，"
            "也不能代替未来激活时的重新校验。"
        ),
    }
    assert provider.calls == ["BTCUSDT", "ETHUSDT"]


@pytest.mark.asyncio
async def test_readiness_keeps_mixed_or_non_usdt_assets_in_research_scope() -> None:
    experiment = _experiment(
        ("BTCUSDT", "binance", "USDT"),
        ("AAPL", "yfinance", "USD"),
        ("ETHUSDC", "binance", "USDC"),
    )
    provider = FakeRulesProvider({"BTCUSDT": _rules("BTCUSDT")})

    result = await review_portfolio_paper_readiness(
        experiment,
        provider=provider,
        now=NOW,
    )

    assert result.review_status == "not_in_scope"
    assert [asset.status for asset in result.assets] == [
        "eligible_for_internal_review",
        "research_only",
        "research_only",
    ]
    assert provider.calls == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_readiness_redacts_upstream_rule_failures() -> None:
    experiment = _experiment(
        ("BTCUSDT", "binance", "USDT"),
        ("ETHUSDT", "binance", "USDT"),
    )
    provider = FakeRulesProvider({"BTCUSDT": _rules("BTCUSDT")})
    provider.failures["ETHUSDT"] = RuntimeError("private upstream token leaked")

    result = await review_portfolio_paper_readiness(
        experiment,
        provider=provider,
        now=NOW,
    )

    assert result.review_status == "verification_incomplete"
    unavailable = result.assets[1]
    assert unavailable.status == "verification_unavailable"
    assert unavailable.rules_verified_at is None
    assert "private" not in " ".join(unavailable.reasons)


def test_preflight_api_is_read_only_and_uses_only_the_saved_experiment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = _experiment(
        ("BTCUSDT", "binance", "USDT"),
        ("ETHUSDT", "binance", "USDT"),
    )
    store = FakeExperimentStore(experiment)
    provider = FakeRulesProvider(
        {"BTCUSDT": _rules("BTCUSDT"), "ETHUSDT": _rules("ETHUSDT")}
    )
    monkeypatch.setattr(portfolio_paper_router_module, "_utc_now", lambda: NOW)
    app = FastAPI()
    app.state.experiment_store = store
    app.state.providers = SimpleNamespace(binance_provider=provider)
    app.include_router(router, prefix="/api/v1")

    with TestClient(app) as client:
        response = client.get(f"/api/v1/portfolio-paper/preflight/{EXPERIMENT_ID}")
        missing = client.get(f"/api/v1/portfolio-paper/preflight/{'b' * 32}")
        schema = client.get("/openapi.json").json()

    assert response.status_code == 200
    body = response.json()
    assert body["activation_available"] is False
    assert body["review_status"] == "ready_for_internal_review"
    assert {"quotes", "scheduler", "track_id", "activation"}.isdisjoint(body)
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Portfolio experiment not found."}
    assert set(schema["paths"]["/api/v1/portfolio-paper/preflight/{experiment_id}"]) == {
        "get"
    }
    assert store.requested_ids == [EXPERIMENT_ID, "b" * 32]
