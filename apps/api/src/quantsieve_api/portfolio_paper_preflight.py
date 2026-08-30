from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator
from quantsieve_providers import BinanceSpotTradingRules, Instrument

from .portfolio_paper_contracts import assess_portfolio_paper_eligibility
from .schemas import PortfolioExperimentRecord

_RULES_TIMEOUT_SECONDS = 8.0


class PortfolioPaperPreflightRulesProvider(Protocol):
    """The public, read-only exchangeInfo capability needed for a review."""

    async def trading_rules(self, symbol: str) -> BinanceSpotTradingRules: ...


class PortfolioPaperPreflightAsset(BaseModel):
    """A deliberately redacted readiness result for one saved portfolio asset."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    symbol: str = Field(min_length=1, max_length=20)
    status: Literal[
        "eligible_for_internal_review",
        "research_only",
        "verification_unavailable",
    ]
    reasons: tuple[str, ...] = Field(min_length=1, max_length=6)
    rules_verified_at: datetime | None = None

    @field_validator("rules_verified_at")
    @classmethod
    def normalize_rules_verified_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Rules verification time must be timezone-aware.")
        return value.astimezone(UTC)


class PortfolioPaperPreflightResponse(BaseModel):
    """Read-only readiness review; it never creates a paper observation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    availability: Literal["internal_only"] = "internal_only"
    scope: Literal["one_session_modeled_observation"] = (
        "one_session_modeled_observation"
    )
    activation_available: Literal[False] = False
    review_status: Literal[
        "ready_for_internal_review",
        "verification_incomplete",
        "not_in_scope",
    ]
    portfolio_experiment_id: str = Field(min_length=1, max_length=128)
    reviewed_at: datetime
    assets: tuple[PortfolioPaperPreflightAsset, ...] = Field(min_length=2, max_length=6)
    next_step: str = Field(min_length=1, max_length=500)

    @field_validator("reviewed_at")
    @classmethod
    def normalize_reviewed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Review time must be timezone-aware.")
        return value.astimezone(UTC)


async def review_portfolio_paper_readiness(
    experiment: PortfolioExperimentRecord,
    *,
    provider: PortfolioPaperPreflightRulesProvider,
    now: datetime | None = None,
) -> PortfolioPaperPreflightResponse:
    """Review saved evidence against the narrow execution-paper scope.

    The review performs only public exchange-rule reads. It does not quote prices,
    create a track, write SQLite state, start a scheduler, or permit activation.
    A future activation must independently repeat its own freshness checks.
    """

    reviewed_at = _aware_utc(now or datetime.now(UTC))
    assets = tuple(experiment.assets)
    static_results: dict[int, PortfolioPaperPreflightAsset] = {}
    candidates: list[tuple[int, Instrument]] = []

    for index, asset in enumerate(assets):
        symbol = asset.symbol.strip().upper()
        if asset.provider != "binance":
            static_results[index] = PortfolioPaperPreflightAsset(
                symbol=symbol,
                status="research_only",
                reasons=(
                    "该归档实验的服务端行情来源不是 Binance Spot；"
                    "当前前向观察只审查 Binance Spot 现货。",
                ),
            )
            continue
        if asset.currency.strip().upper() != "USDT":
            static_results[index] = PortfolioPaperPreflightAsset(
                symbol=symbol,
                status="research_only",
                reasons=(
                    "当前前向观察只接受精确 USDT 结算的同一 Binance Spot 现货篮子；"
                    "不合并 USD 或 USDC。",
                ),
            )
            continue
        candidates.append(
            (
                index,
                Instrument(
                    symbol=symbol,
                    name=asset.name or symbol,
                    market="CRYPTO",
                    exchange="Binance Spot",
                    currency="USDT",
                    provider="binance",
                    asset_type="spot",
                ),
            )
        )

    verified = await _review_candidates(candidates, provider=provider, now=reviewed_at)
    results = tuple(
        static_results[index] if index in static_results else verified[index]
        for index in range(len(assets))
    )
    statuses = {result.status for result in results}
    if statuses == {"eligible_for_internal_review"}:
        review_status: Literal[
            "ready_for_internal_review", "verification_incomplete", "not_in_scope"
        ] = "ready_for_internal_review"
        next_step = (
            "交易规则复核通过，可进入内部人工审查；此结果不会创建观察、不会启动调度，也不能代替未来激活时的重新校验。"
        )
    elif "verification_unavailable" in statuses:
        review_status = "verification_incomplete"
        next_step = (
            "至少一个 Binance 交易规则暂时无法复核。请稍后再次检查；"
            "在所有资产复核通过前，不应创建组合观察。"
        )
    else:
        review_status = "not_in_scope"
        next_step = (
            "该组合超出当前窄范围：仅支持同一 Binance Spot、USDT 结算、"
            "2 至 6 个现货资产的内部单会话模型观察。"
        )
    return PortfolioPaperPreflightResponse(
        review_status=review_status,
        portfolio_experiment_id=experiment.id,
        reviewed_at=reviewed_at,
        assets=results,
        next_step=next_step,
    )


async def _review_candidates(
    candidates: Sequence[tuple[int, Instrument]],
    *,
    provider: PortfolioPaperPreflightRulesProvider,
    now: datetime,
) -> dict[int, PortfolioPaperPreflightAsset]:
    if not candidates:
        return {}

    async def review_one(
        index: int,
        instrument: Instrument,
    ) -> tuple[int, PortfolioPaperPreflightAsset]:
        try:
            rules = await asyncio.wait_for(
                provider.trading_rules(instrument.symbol),
                timeout=_RULES_TIMEOUT_SECONDS,
            )
            assessment = assess_portfolio_paper_eligibility(
                instrument,
                rules=rules,
                now=now,
            )
        except Exception:
            return (
                index,
                PortfolioPaperPreflightAsset(
                    symbol=instrument.symbol,
                    status="verification_unavailable",
                    reasons=(
                        "未能在受限时间内从 Binance 复核该标的的公开交易规则；"
                        "本次检查不使用缓存结果代替复核。",
                    ),
                ),
            )
        if assessment.status == "eligible":
            return (
                index,
                PortfolioPaperPreflightAsset(
                    symbol=instrument.symbol,
                    status="eligible_for_internal_review",
                    reasons=assessment.reasons,
                    rules_verified_at=rules.verified_at,
                ),
            )
        return (
            index,
            PortfolioPaperPreflightAsset(
                symbol=instrument.symbol,
                status="research_only",
                reasons=assessment.reasons,
                rules_verified_at=rules.verified_at,
            ),
        )

    reviewed = await asyncio.gather(
        *(review_one(index, instrument) for index, instrument in candidates)
    )
    return dict(reviewed)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Review time must be timezone-aware.")
    return value.astimezone(UTC)
