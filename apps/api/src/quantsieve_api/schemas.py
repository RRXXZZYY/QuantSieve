from __future__ import annotations

import json
from datetime import date, datetime
from math import ceil, isclose, isfinite
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from quantsieve_engine import (
    BacktestConfig,
    BacktestMetrics,
    HoldoutValidationCode,
    OptimizationObjective,
    PortfolioBacktestResult,
    PortfolioMethod,
    RunManifest,
)
from quantsieve_providers import BarInterval

RequestedProvider = Literal[
    "auto", "akshare", "yfinance", "binance", "futures", "macro"
]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


class BacktestRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    provider: RequestedProvider = "auto"
    strategy_id: str
    start: date | None = None
    end: date | None = None
    interval: BarInterval = "1d"
    parameters: dict[str, float] = Field(default_factory=dict)
    config: BacktestConfig | None = None


class OptimizeBacktestRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    provider: RequestedProvider = "auto"
    strategy_id: str
    objective: OptimizationObjective = "balanced"
    start: date | None = None
    end: date | None = None
    interval: BarInterval = "1d"
    train_ratio: float = Field(default=0.8, ge=0.65, le=0.85)
    minimum_trades: int | None = Field(default=None, ge=0, le=500)
    minimum_trades_per_year: int = Field(default=2, ge=1, le=100)
    maximum_trades_per_year: int | None = Field(default=None, ge=1, le=10_000)
    minimum_exposure: float = Field(default=0.2, ge=0, le=1)
    minimum_annualized_return: float | None = Field(default=None, ge=-1, le=10)
    maximum_drawdown: float | None = Field(default=None, gt=0, le=1)
    maximum_cash_streak_ratio: float = Field(default=0.4, gt=0, le=1)
    maximum_cash_streak_bars: int | None = Field(default=None, ge=1, le=100_000)
    minimum_profitable_fold_ratio: float = Field(default=0.5, ge=0, le=1)
    minimum_timing_positive_fold_ratio: float = Field(default=0.5, ge=0, le=1)
    walk_forward_windows: int = Field(default=3, ge=2, le=5)
    config: BacktestConfig | None = None


class StrategyRobustnessMarketRequest(BaseModel):
    """One independently audited market in a cross-market strategy study."""

    symbol: str = Field(min_length=1, max_length=20)
    provider: RequestedProvider = "auto"


class StrategyRobustnessRequest(BaseModel):
    """Run the same strategy family against a small, user-selected market basket.

    Parameters are optimized independently for every market.  This is deliberate:
    the endpoint evaluates whether the strategy *family* has evidence beyond one
    instrument; it must never be presented as evidence for a universal parameter
    set.
    """

    strategy_id: str
    markets: list[StrategyRobustnessMarketRequest] = Field(min_length=2, max_length=4)
    objective: OptimizationObjective = "balanced"
    start: date | None = None
    end: date | None = None
    interval: BarInterval = "1d"
    train_ratio: float = Field(default=0.8, ge=0.65, le=0.85)
    minimum_trades: int | None = Field(default=None, ge=0, le=500)
    minimum_trades_per_year: int = Field(default=2, ge=1, le=100)
    maximum_trades_per_year: int | None = Field(default=None, ge=1, le=10_000)
    minimum_exposure: float = Field(default=0.2, ge=0, le=1)
    minimum_annualized_return: float | None = Field(default=None, ge=-1, le=10)
    maximum_drawdown: float | None = Field(default=None, gt=0, le=1)
    maximum_cash_streak_ratio: float = Field(default=0.4, gt=0, le=1)
    maximum_cash_streak_bars: int | None = Field(default=None, ge=1, le=100_000)
    minimum_profitable_fold_ratio: float = Field(default=0.5, ge=0, le=1)
    minimum_timing_positive_fold_ratio: float = Field(default=0.5, ge=0, le=1)
    walk_forward_windows: int = Field(default=3, ge=2, le=5)
    config: BacktestConfig | None = None

    @model_validator(mode="after")
    def validate_cross_market_study(self) -> StrategyRobustnessRequest:
        symbols = [market.symbol.upper() for market in self.markets]
        if len(symbols) != len(set(symbols)):
            raise ValueError("跨市场验证的标的不能重复。")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("开始日期必须早于结束日期。")
        return self


class DiscoverBacktestRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    provider: RequestedProvider = "auto"
    objective: OptimizationObjective = "balanced"
    start: date | None = None
    end: date | None = None
    interval: BarInterval = "1d"
    train_ratio: float = Field(default=0.8, ge=0.65, le=0.85)
    shortlist_size: int = Field(default=3, ge=1, le=4)
    minimum_trades: int | None = Field(default=None, ge=0, le=500)
    minimum_trades_per_year: int = Field(default=2, ge=1, le=100)
    maximum_trades_per_year: int | None = Field(default=None, ge=1, le=10_000)
    minimum_exposure: float = Field(default=0.2, ge=0, le=1)
    minimum_annualized_return: float | None = Field(default=None, ge=-1, le=10)
    maximum_drawdown: float | None = Field(default=None, gt=0, le=1)
    maximum_cash_streak_ratio: float = Field(default=0.4, gt=0, le=1)
    maximum_cash_streak_bars: int | None = Field(default=None, ge=1, le=100_000)
    minimum_profitable_fold_ratio: float = Field(default=0.5, ge=0, le=1)
    minimum_timing_positive_fold_ratio: float = Field(default=0.5, ge=0, le=1)
    walk_forward_windows: int = Field(default=3, ge=2, le=5)
    config: BacktestConfig | None = None


class CompareBacktestRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    provider: RequestedProvider = "auto"
    start: date | None = None
    end: date | None = None
    interval: BarInterval = "1d"
    config: BacktestConfig | None = None


class PortfolioAssetRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    provider: RequestedProvider = "auto"
    currency: str = Field(min_length=1, max_length=20)


class PortfolioBacktestRequest(BaseModel):
    assets: list[PortfolioAssetRequest] = Field(min_length=2, max_length=6)
    start: date
    end: date
    volatility_lookback: int = Field(default=60, ge=20, le=252)
    rebalance_bars: int = Field(default=21, ge=5, le=63)
    maximum_asset_weight: float = Field(default=0.4, gt=0, le=1)
    config: BacktestConfig | None = None

    @model_validator(mode="after")
    def validate_portfolio(self) -> PortfolioBacktestRequest:
        if self.start >= self.end:
            raise ValueError("组合开始日期必须早于结束日期。")
        symbols = [asset.symbol.upper() for asset in self.assets]
        if len(set(symbols)) != len(symbols):
            raise ValueError("组合标的不能重复。")
        currencies = {
            "USD" if asset.currency.upper() in {"USD", "USDT", "USDC"} else asset.currency.upper()
            for asset in self.assets
        }
        if len(currencies) != 1:
            raise ValueError("组合暂不做汇率换算，请只选择同一结算币种的资产。")
        if self.maximum_asset_weight * len(self.assets) < 1:
            raise ValueError("单资产权重上限过低，无法让组合权重合计为 100%。")
        return self


class PortfolioBacktestRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    run_request: PortfolioBacktestRequest
    result_payload: dict[str, Any]
    created_at: datetime
    expires_at: datetime


class CustomBacktestRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    provider: RequestedProvider = "auto"
    name: str = Field(default="自定义策略", min_length=1, max_length=80)
    code: str = Field(min_length=20, max_length=20_000)
    start: date | None = None
    end: date | None = None
    interval: BarInterval = "1d"
    config: BacktestConfig | None = None


class ExperimentInstrument(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=120)
    market: str = Field(min_length=1, max_length=20)
    exchange: str = Field(default="", max_length=120)
    currency: str = Field(default="", max_length=20)
    provider: RequestedProvider
    asset_type: str = Field(default="", max_length=40)


class ExperimentStrategy(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(default="", max_length=80)
    parameters: dict[str, float] = Field(default_factory=dict)


class ExperimentComparison(BaseModel):
    excess_return: float
    excess_annualized_return: float
    drawdown_improvement: float
    beats_benchmark: bool
    positive_return: bool


class ExperimentTimingComparison(BaseModel):
    target_exposure: float = Field(ge=0, le=1)
    excess_return: float
    beats_exposure_matched: bool


class ExperimentValidation(BaseModel):
    objective: OptimizationObjective
    split_date: str
    validation_passed: bool
    validation_code: HoldoutValidationCode = "unclassified"
    validation_reason: str = Field(max_length=1_000)
    forward_observation_eligible: bool = False
    development_metrics: BacktestMetrics
    validation_metrics: BacktestMetrics
    validation_benchmark_metrics: BacktestMetrics
    validation_exposure_matched_benchmark_metrics: BacktestMetrics | None = None

    @model_validator(mode="after")
    def validate_forward_observation_tier(self) -> ExperimentValidation:
        if (
            self.validation_passed
            and self.validation_metrics.closed_trades < 10
        ):
            raise ValueError(
                "最终留出少于 10 个已闭合独立决策周期，不能标记为验证通过。"
            )
        if (
            (self.validation_passed or self.forward_observation_eligible)
            and self.validation_exposure_matched_benchmark_metrics is not None
            and self.validation_metrics.total_return
            <= self.validation_exposure_matched_benchmark_metrics.total_return
        ):
            raise ValueError(
                "候选未跑赢相同起点投入比例的固定份额被动基准，不能进入前向跟踪。"
            )
        if self.forward_observation_eligible and (
            self.validation_passed
            or self.validation_code != "sample_insufficient"
            or not 5 <= self.validation_metrics.closed_trades < 10
        ):
            raise ValueError(
                "前向观察资格只适用于最终留出 5–9 笔、且仅因样本不足未通过的候选。"
            )
        return self


class ExperimentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    notes: str | None = Field(default=None, max_length=1_000)
    instrument: ExperimentInstrument
    strategy: ExperimentStrategy
    interval: BarInterval
    start: date
    end: date
    optimized: bool = False
    run_request: dict[str, Any] = Field(default_factory=dict)
    engine_config: dict[str, Any] = Field(default_factory=dict)
    data_metadata: dict[str, Any] = Field(default_factory=dict)
    metrics: BacktestMetrics
    benchmark_metrics: BacktestMetrics
    exposure_matched_benchmark_metrics: BacktestMetrics | None = None
    comparison: ExperimentComparison
    timing_comparison: ExperimentTimingComparison | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    validation: ExperimentValidation | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


class ExperimentFromRunCreate(BaseModel):
    """Client-authored labels for archiving a server-issued backtest run."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    notes: str | None = Field(default=None, max_length=1_000)
    run_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    instrument_name: str | None = Field(default=None, min_length=1, max_length=120)


class ExperimentRecord(ExperimentCreate):
    id: str
    source_run_id: str | None = Field(
        default=None,
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
    )
    provenance_status: Literal["legacy_unverified", "server_verified"] = (
        "legacy_unverified"
    )
    run_manifest: RunManifest | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("run_manifest", mode="before")
    @classmethod
    def parse_persisted_run_manifest(cls, value: object) -> object:
        if isinstance(value, dict):
            return RunManifest.model_validate_json(
                json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            )
        return value


class CrossMarketBacktestRunRecord(BaseModel):
    """Short-lived, server-issued receipt for a cross-market study."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    run_request: StrategyRobustnessRequest
    result_payload: dict[str, Any]
    created_at: datetime
    expires_at: datetime


class CrossMarketExperimentFromRunCreate(BaseModel):
    """The only client-authored fields when archiving a server-issued study."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    notes: str | None = Field(default=None, max_length=1_000)
    run_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")


class CrossMarketExperimentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    source_run_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    schema_version: Literal[1]
    kind: Literal["cross_market"]
    name: str = Field(min_length=1, max_length=100)
    notes: str | None = Field(default=None, max_length=1_000)
    strategy_id: str = Field(min_length=1, max_length=80)
    symbols: list[str] = Field(min_length=2, max_length=4)
    interval: BarInterval
    objective: OptimizationObjective
    decision_status: Literal[
        "validated_across_markets",
        "mixed_evidence",
        "rejected_across_markets",
        "incomplete",
    ]
    validated_markets: int = Field(ge=0)
    rejected_markets: int = Field(ge=0)
    unavailable_markets: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class CrossMarketExperimentCreate(BaseModel):
    """Immutable archival form of a server-issued cross-market study."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    kind: Literal["cross_market"] = "cross_market"
    name: str = Field(min_length=1, max_length=100)
    notes: str | None = Field(default=None, max_length=1_000)
    strategy: ExperimentStrategy
    interval: BarInterval
    objective: OptimizationObjective
    parameter_policy: Literal["independently_optimized_per_market"]
    parameter_policy_note: str = Field(min_length=1, max_length=1_000)
    run_request: StrategyRobustnessRequest
    markets: list[dict[str, Any]] = Field(min_length=2, max_length=4)
    summary: dict[str, int]
    research_decision: dict[str, str]

    @model_validator(mode="after")
    def validate_study_snapshot(self) -> CrossMarketExperimentCreate:
        request_symbols = [market.symbol.upper() for market in self.run_request.markets]
        market_symbols = [str(market.get("symbol", "")).upper() for market in self.markets]
        if market_symbols != request_symbols:
            raise ValueError("跨市场实验的市场快照必须与运行回执的标的顺序完全一致。")
        if (
            len(set(market_symbols)) != len(market_symbols)
            or any(not symbol for symbol in market_symbols)
        ):
            raise ValueError("跨市场实验的市场快照包含无效或重复标的。")
        if self.summary.get("requested") != len(self.markets):
            raise ValueError("跨市场实验汇总的请求市场数量必须与市场快照一致。")
        return self


class CrossMarketExperimentRecord(CrossMarketExperimentCreate):
    id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    source_run_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    created_at: datetime
    updated_at: datetime


PORTFOLIO_METHODS = frozenset(
    {
        "initial_equal_hold",
        "periodic_equal",
        "periodic_inverse_volatility",
    }
)


def _portfolio_currency_group(currency: str) -> str:
    normalized = currency.strip().upper()
    return "USD" if normalized in {"USD", "USDT", "USDC"} else normalized


class PortfolioExperimentAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=20)
    requested_symbol: str | None = Field(default=None, min_length=1, max_length=20)
    name: str = Field(default="", max_length=120)
    market: str = Field(default="", max_length=20)
    exchange: str = Field(default="", max_length=120)
    currency: str = Field(min_length=1, max_length=20)
    provider: RequestedProvider
    asset_type: str = Field(default="", max_length=40)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol", "requested_symbol", "currency")
    @classmethod
    def normalize_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("资产代码和结算币种不能为空。")
        return normalized


class PortfolioExperimentDisplayAsset(BaseModel):
    """Client-owned labels for a server-issued portfolio run."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=20)
    requested_symbol: str | None = Field(default=None, min_length=1, max_length=20)
    name: str = Field(default="", max_length=120)
    market: str = Field(default="", max_length=20)
    exchange: str = Field(default="", max_length=120)
    asset_type: str = Field(default="", max_length=40)

    @field_validator("symbol", "requested_symbol")
    @classmethod
    def normalize_symbol(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("资产代码不能为空。")
        return normalized


class PortfolioExperimentFromRunCreate(BaseModel):
    """Only user-authored presentation fields; calculations come from the run receipt."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    notes: str | None = Field(default=None, max_length=1_000)
    focus_method: PortfolioMethod
    assets: list[PortfolioExperimentDisplayAsset] = Field(
        min_length=2,
        max_length=6,
    )
    run_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")

    @model_validator(mode="after")
    def validate_assets(self) -> PortfolioExperimentFromRunCreate:
        requested_symbols = [
            asset.requested_symbol or asset.symbol for asset in self.assets
        ]
        if len(set(requested_symbols)) != len(requested_symbols):
            raise ValueError("组合展示资产不能重复。")
        return self


class PortfolioExperimentAssumptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volatility_lookback: int = Field(ge=20, le=252)
    rebalance_bars: int = Field(ge=5, le=63)
    maximum_asset_weight: float = Field(gt=0, le=1)
    fee_rate: float = Field(ge=0, le=0.1)
    slippage_rate: float = Field(ge=0, le=0.1)
    cash_return: float = 0
    execution: Literal[
        "next_open",
        "next_common_session_open_proxy",
    ] = "next_common_session_open_proxy"

    @model_validator(mode="after")
    def validate_cash_assumption(self) -> PortfolioExperimentAssumptions:
        if not isclose(self.cash_return, 0, abs_tol=1e-12):
            raise ValueError("当前组合引擎未建模现金收益，现金收益假设必须为 0。")
        return self


class PortfolioExperimentDataQuality(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actual_start: str | None = Field(default=None, max_length=80)
    actual_end: str | None = Field(default=None, max_length=80)
    annual_periods: int = Field(gt=0)
    source_bars: dict[str, int]
    alignment: Literal["common_daily_session_labels"]
    valuation_limit: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_source_bars(self) -> PortfolioExperimentDataQuality:
        if not self.source_bars or any(value <= 0 for value in self.source_bars.values()):
            raise ValueError("数据质量记录中的各资产源 K 线数量必须为正数。")
        if (
            self.actual_start is not None
            and self.actual_end is not None
            and self.actual_start > self.actual_end
        ):
            raise ValueError("数据质量记录的实际开始时间不能晚于结束时间。")
        return self


class PortfolioEvidenceChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    all_segments_evaluable: bool
    all_segments_drawdown_strictly_improved: bool
    majority_segments_sharpe_improved: bool
    full_sample_sharpe_improved: bool
    full_sample_positive_return: bool
    full_sample_invested: bool
    minimum_segment_bars: int = Field(ge=10)


class PortfolioResearchDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_evidence_passed: bool
    drawdown_improved_segments: int = Field(ge=0)
    sharpe_improved_segments: int = Field(ge=0)
    evaluable_segments: int = Field(ge=0)
    total_segments: int = Field(ge=1, le=4)
    evidence_checks: PortfolioEvidenceChecks
    title: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1_000)


class PortfolioExperimentSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1, le=4)
    start: str = Field(min_length=1, max_length=80)
    end: str = Field(min_length=1, max_length=80)
    results: dict[PortfolioMethod, PortfolioBacktestResult]

    @model_validator(mode="after")
    def validate_segment(self) -> PortfolioExperimentSegment:
        if self.start > self.end:
            raise ValueError("组合分段开始时间不能晚于结束时间。")
        if set(self.results) != PORTFOLIO_METHODS:
            raise ValueError("每个组合分段必须精确包含三种配置方法。")
        for method, result in self.results.items():
            if result.method != method:
                raise ValueError("组合分段结果中的 method 必须与结果键一致。")
        bars = {result.metrics.bars for result in self.results.values()}
        if len(bars) != 1:
            raise ValueError("同一组合分段的三种方法必须使用相同数量的共同 K 线。")
        return self


class PortfolioExperimentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    kind: Literal["portfolio"] = "portfolio"
    name: str = Field(min_length=1, max_length=100)
    notes: str | None = Field(default=None, max_length=1_000)
    assets: list[PortfolioExperimentAsset] = Field(min_length=2, max_length=6)
    interval: Literal["1d"] = "1d"
    start: date
    end: date
    focus_method: PortfolioMethod
    run_request: PortfolioBacktestRequest
    data_quality: PortfolioExperimentDataQuality
    assumptions: PortfolioExperimentAssumptions
    common_bars: int = Field(ge=1)
    results: dict[PortfolioMethod, PortfolioBacktestResult]
    segments: list[PortfolioExperimentSegment] = Field(min_length=1, max_length=4)
    research_decision: PortfolioResearchDecision
    citations: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    calculation_version: str = Field(default="", max_length=80)

    @field_validator("assets", mode="before")
    @classmethod
    def validate_asset_count(cls, value: Any) -> Any:
        if not isinstance(value, list) or not 2 <= len(value) <= 6:
            raise ValueError("组合实验必须包含 2–6 个资产。")
        return value

    @field_validator("segments", mode="before")
    @classmethod
    def validate_segment_count(cls, value: Any) -> Any:
        if not isinstance(value, list) or not 1 <= len(value) <= 4:
            raise ValueError("组合实验必须包含 1–4 个研究分段。")
        return value

    @staticmethod
    def _validate_weights(
        result: PortfolioBacktestResult,
        symbols: set[str],
        *,
        context: str,
    ) -> None:
        weight_snapshots = {
            "最新权重": result.latest_weights,
            "最后再平衡目标权重": result.last_rebalance_target_weights,
            "期末实际权重": result.ending_realized_weights,
        }
        for label, snapshot in weight_snapshots.items():
            raw_keys = list(snapshot)
            normalized_keys = {str(key).strip().upper() for key in raw_keys}
            if (
                len(normalized_keys) != len(raw_keys)
                or normalized_keys != symbols
                or set(raw_keys) != symbols
            ):
                raise ValueError(f"{context}的{label}必须与资产集合完全一致。")
            weights = [float(value) for value in snapshot.values()]
            if (
                not all(
                    isfinite(value) and -1e-9 <= value <= 1 + 1e-9
                    for value in weights
                )
                or not isclose(sum(weights), 1, rel_tol=0, abs_tol=1e-6)
            ):
                raise ValueError(f"{context}的{label}必须为有限非负数且合计约为 1。")
        if any(
            not isclose(
                result.latest_weights[symbol],
                result.ending_realized_weights[symbol],
                rel_tol=0,
                abs_tol=1e-12,
            )
            for symbol in result.latest_weights
        ):
            raise ValueError(f"{context}的最近实际权重必须与期末实际权重一致。")
        for allocation in result.allocations:
            allocation_weights = allocation.get("weights")
            if not isinstance(allocation_weights, dict):
                continue
            allocation_keys = {
                str(key).strip().upper() for key in allocation_weights
            }
            if (
                len(allocation_keys) != len(allocation_weights)
                or allocation_keys != symbols
            ):
                raise ValueError(f"{context}的历史调仓权重必须与资产集合完全一致。")
            values = [float(value) for value in allocation_weights.values()]
            if (
                not all(
                    isfinite(value) and -1e-9 <= value <= 1 + 1e-9
                    for value in values
                )
                or not isclose(sum(values), 1, rel_tol=0, abs_tol=1e-6)
            ):
                raise ValueError(f"{context}的历史调仓权重必须合计约为 1。")

    @model_validator(mode="after")
    def validate_snapshot(self) -> PortfolioExperimentCreate:
        if self.start >= self.end:
            raise ValueError("组合实验开始日期必须早于结束日期。")

        symbols = [asset.symbol for asset in self.assets]
        symbol_set = set(symbols)
        if len(symbol_set) != len(symbols):
            raise ValueError("组合实验资产代码不能重复，大小写视为同一代码。")
        currency_groups = {
            _portfolio_currency_group(asset.currency) for asset in self.assets
        }
        if len(currency_groups) != 1:
            raise ValueError("组合实验暂不做汇率换算，只能保存同一结算币种组的资产。")

        requested_assets = {
            asset.symbol.strip().upper(): asset for asset in self.run_request.assets
        }
        top_assets_by_requested_symbol = {
            asset.requested_symbol or asset.symbol: asset for asset in self.assets
        }
        if len(top_assets_by_requested_symbol) != len(self.assets):
            raise ValueError("组合实验请求代码不能重复，大小写视为同一代码。")
        if set(requested_assets) != set(top_assets_by_requested_symbol):
            raise ValueError("run_request 的资产集合必须与组合实验顶层资产一致。")
        for symbol, requested in requested_assets.items():
            actual = top_assets_by_requested_symbol[symbol]
            if (
                requested.provider != "auto"
                and requested.provider != actual.provider
            ):
                raise ValueError("run_request 的资产数据源必须与顶层资产一致。")
            if requested.currency.strip().upper() != actual.currency:
                raise ValueError("run_request 的资产结算币种必须与顶层资产一致。")

        if self.run_request.start != self.start or self.run_request.end != self.end:
            raise ValueError("run_request 的开始和结束日期必须与组合实验一致。")
        if (
            self.run_request.volatility_lookback
            != self.assumptions.volatility_lookback
            or self.run_request.rebalance_bars != self.assumptions.rebalance_bars
            or not isclose(
                self.run_request.maximum_asset_weight,
                self.assumptions.maximum_asset_weight,
                rel_tol=0,
                abs_tol=1e-12,
            )
        ):
            raise ValueError("run_request 的配置参数必须与组合实验假设一致。")

        annual_periods = (
            365
            if all(asset.provider == "binance" for asset in self.assets)
            else 252
        )
        expected_config = (self.run_request.config or BacktestConfig()).model_copy(
            update={
                "annual_periods": annual_periods,
                "bar_interval": "1d",
                "signal_delay_bars": 0,
            }
        )
        if (
            not isclose(
                expected_config.fee_rate,
                self.assumptions.fee_rate,
                rel_tol=0,
                abs_tol=1e-12,
            )
            or not isclose(
                expected_config.slippage_rate,
                self.assumptions.slippage_rate,
                rel_tol=0,
                abs_tol=1e-12,
            )
        ):
            raise ValueError("run_request 的费用与滑点必须与组合实验假设一致。")
        if self.data_quality.annual_periods != annual_periods:
            raise ValueError("数据质量记录的年化周期必须与实际资产市场一致。")
        source_symbols = {
            str(symbol).strip().upper() for symbol in self.data_quality.source_bars
        }
        if source_symbols != symbol_set:
            raise ValueError("数据质量记录的资产集合必须与组合实验一致。")

        if set(self.results) != PORTFOLIO_METHODS:
            raise ValueError("组合实验必须精确包含三种配置方法。")
        if self.focus_method not in self.results:
            raise ValueError("组合实验关注方法必须存在于结果中。")
        for method, result in self.results.items():
            if result.method != method:
                raise ValueError("组合实验结果中的 method 必须与结果键一致。")
            if result.metrics.bars != self.common_bars:
                raise ValueError("三种配置方法必须与 common_bars 使用相同共同 K 线。")
            if result.equity and len(result.equity) != self.common_bars:
                raise ValueError("组合净值序列长度必须与 common_bars 一致。")
            if result.config != expected_config:
                raise ValueError("组合结果的实际引擎配置必须与 run_request 一致。")
            self._validate_weights(result, symbol_set, context=f"{method} 完整结果")

        indices = [segment.index for segment in self.segments]
        if indices != list(range(1, len(self.segments) + 1)):
            raise ValueError("组合分段编号必须从 1 开始连续排列。")
        for segment in self.segments:
            for method, result in segment.results.items():
                if result.config != expected_config:
                    raise ValueError("组合分段的引擎配置必须与 run_request 一致。")
                self._validate_weights(
                    result,
                    symbol_set,
                    context=f"分段 {segment.index} 的 {method} 结果",
                )

        total_segments = len(self.segments)
        decision = self.research_decision
        if decision.total_segments != total_segments:
            raise ValueError("研究决策中的分段总数必须与实际分段数量一致。")
        minimum_segment_bars = max(10, self.assumptions.rebalance_bars)
        evaluable_segments = sum(
            segment.results[
                "periodic_inverse_volatility"
            ].metrics.bars
            >= minimum_segment_bars
            and segment.results[
                "periodic_inverse_volatility"
            ].metrics.rebalances
            >= 2
            and segment.results[
                "periodic_inverse_volatility"
            ].metrics.annualized_volatility
            > 1e-8
            and isclose(
                sum(
                    segment.results[
                        "periodic_inverse_volatility"
                    ].latest_weights.values()
                ),
                1,
                rel_tol=0,
                abs_tol=1e-6,
            )
            for segment in self.segments
        )
        drawdown_improved = sum(
            segment.results["periodic_inverse_volatility"].metrics.max_drawdown
            > segment.results["periodic_equal"].metrics.max_drawdown + 1e-6
            for segment in self.segments
        )
        sharpe_improved = sum(
            segment.results["periodic_inverse_volatility"].metrics.sharpe_ratio
            > segment.results["periodic_equal"].metrics.sharpe_ratio + 1e-3
            for segment in self.segments
        )
        if decision.evaluable_segments != evaluable_segments:
            raise ValueError("研究决策中的可评估分段数与实际结果不一致。")
        if decision.drawdown_improved_segments != drawdown_improved:
            raise ValueError("研究决策中的回撤改善分段数与实际结果不一致。")
        if decision.sharpe_improved_segments != sharpe_improved:
            raise ValueError("研究决策中的夏普改善分段数与实际结果不一致。")
        inverse = self.results["periodic_inverse_volatility"]
        periodic_equal = self.results["periodic_equal"]
        full_sample_invested = (
            inverse.metrics.rebalances >= 2
            and inverse.metrics.annualized_volatility > 1e-8
            and isclose(
                sum(inverse.latest_weights.values()),
                1,
                rel_tol=0,
                abs_tol=1e-6,
            )
        )
        expected_checks = {
            "all_segments_evaluable": evaluable_segments == total_segments,
            "all_segments_drawdown_strictly_improved": (
                drawdown_improved == total_segments
            ),
            "majority_segments_sharpe_improved": (
                sharpe_improved >= ceil(total_segments / 2)
            ),
            "full_sample_sharpe_improved": (
                inverse.metrics.sharpe_ratio
                > periodic_equal.metrics.sharpe_ratio + 1e-3
            ),
            "full_sample_positive_return": inverse.metrics.total_return > 0,
            "full_sample_invested": full_sample_invested,
            "minimum_segment_bars": minimum_segment_bars,
        }
        if decision.evidence_checks.model_dump() != expected_checks:
            raise ValueError("研究决策中的证据检查明细与实际结果不一致。")
        expected_risk_evidence = (
            bool(self.segments)
            and evaluable_segments == total_segments
            and drawdown_improved == total_segments
            and sharpe_improved >= ceil(total_segments / 2)
            and inverse.metrics.sharpe_ratio
            > periodic_equal.metrics.sharpe_ratio + 1e-3
            and inverse.metrics.total_return > 0
            and full_sample_invested
        )
        if decision.risk_evidence_passed != expected_risk_evidence:
            raise ValueError("研究决策的风险证据结论与实际结果不一致。")
        return self


class PortfolioExperimentRecord(PortfolioExperimentCreate):
    id: str
    source_run_id: str | None = None
    created_at: datetime
    updated_at: datetime


class PortfolioExperimentSummary(BaseModel):
    schema_version: Literal[1] = 1
    kind: Literal["portfolio"] = "portfolio"
    id: str
    source_run_id: str | None = None
    name: str
    notes: str | None = None
    symbols: list[str]
    asset_count: int = Field(ge=2, le=6)
    interval: Literal["1d"] = "1d"
    start: date
    end: date
    focus_method: PortfolioMethod
    common_bars: int = Field(ge=1)
    total_return: float
    max_drawdown: float
    sharpe_ratio: float
    risk_evidence_passed: bool
    created_at: datetime
    updated_at: datetime


class PaperForwardExecution(BaseModel):
    side: Literal["buy", "sell"]
    executed_at: str
    raw_open_price: float = Field(gt=0)
    modeled_fill_price: float = Field(gt=0)
    position_before: float = Field(ge=0, le=1)
    position_after: float = Field(ge=0, le=1)
    turnover: float = Field(gt=0, le=1)
    friction_rate: float = Field(ge=0, le=0.2)
    friction_amount: float = Field(ge=0)


class PaperForwardLot(BaseModel):
    entered_at: str
    entry_price: float = Field(gt=0)
    position_size: float = Field(gt=0, le=1)


class PaperForwardState(BaseModel):
    # Missing version means a legacy payload. New states set version 2
    # explicitly so an old compact snapshot can never be mistaken for
    # independent-cycle evidence.
    schema_version: Literal[1, 2] = 1
    calculation_origin: Literal["activation", "migration"] = "activation"
    started_at: str
    last_bar_at: str
    bars: int = Field(default=0, ge=0)
    initial_equity: float = Field(gt=0)
    equity: float = Field(gt=0)
    total_return: float
    peak_equity: float = Field(gt=0)
    max_drawdown: float = Field(le=0)
    benchmark_equity: float = Field(gt=0)
    benchmark_total_return: float
    benchmark_peak_equity: float = Field(gt=0)
    benchmark_max_drawdown: float = Field(le=0)
    excess_return: float
    position: float = Field(ge=0, le=1)
    benchmark_position: float = Field(ge=0, le=1)
    execution_delay_bars: int = Field(ge=1, le=5)
    pending_targets: list[float] = Field(min_length=1, max_length=5)
    cycle_kind: Literal["flat_to_flat", "satellite_over_core"] = "flat_to_flat"
    cycle_return_semantics: Literal[
        "compounded_strategy_return",
        "compounded_relative_to_core",
    ] = "compounded_strategy_return"
    cycle_baseline_position: float = Field(default=0, ge=0, le=1)
    pending_cycle_baseline_targets: list[float] = Field(
        default_factory=list,
        max_length=5,
    )
    quality_calculation_origin: Literal[
        "activation",
        "cycle_semantics_migration",
        "legacy_lot_statistics",
    ] = "legacy_lot_statistics"
    quality_started_at: str | None = None
    quality_bars: int = Field(default=0, ge=0)
    quality_sample_status: Literal[
        "tracking",
        "awaiting_baseline_reset",
        "legacy_pending_migration",
    ] = "legacy_pending_migration"
    quality_reset_reason: Literal[
        "none",
        "legacy_lot_statistics_excluded",
    ] = "legacy_lot_statistics_excluded"
    open_cycle_started_at: str | None = None
    open_cycle_strategy_growth: float = Field(default=1, gt=0)
    open_cycle_baseline_growth: float = Field(default=1, gt=0)
    open_cycle_holding_bars: int = Field(default=0, ge=0)
    orders: int = Field(default=0, ge=0)
    # These names remain stable for stored/API compatibility. In schema v2
    # they describe closed independent decision cycles, never matched lots.
    round_trips: int = Field(default=0, ge=0)
    total_friction_paid: float = Field(default=0, ge=0)
    benchmark_friction_paid: float = Field(default=0, ge=0)
    open_trade_entry_price: float | None = Field(default=None, gt=0)
    open_lots: list[PaperForwardLot] = Field(default_factory=list, max_length=1_000)
    last_closed_trade_return: float | None = None
    winning_round_trips: int = Field(default=0, ge=0)
    losing_round_trips: int = Field(default=0, ge=0)
    closed_trade_return_sum: float = 0
    closed_trade_gain_sum: float = Field(default=0, ge=0)
    closed_trade_loss_sum: float = Field(default=0, ge=0)
    exposed_bars: int = Field(default=0, ge=0)
    exposure_sum: float | None = Field(default=None, ge=0)
    cash_streak_bars: int = Field(default=0, ge=0)
    max_cash_streak_bars: int = Field(default=0, ge=0)
    last_bar_return: float = 0
    benchmark_last_bar_return: float = 0
    execution: PaperForwardExecution | None = None


class PaperForwardHealth(BaseModel):
    status: Literal["baseline", "collecting", "healthy", "watch", "review"]
    title: str = Field(max_length=120)
    summary: str = Field(max_length=500)
    assessment_ready: bool
    evidence_bars: int = Field(ge=0)
    minimum_evidence_bars: int = Field(default=30, ge=1)
    round_trips: int = Field(ge=0)
    minimum_round_trips: int = Field(default=5, ge=1)
    cycle_kind: Literal["flat_to_flat", "satellite_over_core"]
    cycle_return_semantics: Literal[
        "compounded_strategy_return",
        "compounded_relative_to_core",
    ]
    quality_calculation_origin: Literal[
        "activation",
        "cycle_semantics_migration",
        "legacy_lot_statistics",
    ]
    quality_sample_status: Literal[
        "tracking",
        "awaiting_baseline_reset",
        "legacy_pending_migration",
    ]
    quality_started_at: str | None = None
    drawdown_limit: float = Field(gt=0, le=1)
    drawdown_limit_source: Literal[
        "saved_constraint",
        "holdout_reference",
        "default_reference",
    ]
    expected_return_reference: float | None = None
    trades_per_year: float | None = Field(default=None, ge=0)
    exposure_ratio: float | None = Field(default=None, ge=0, le=1)
    win_rate: float | None = Field(default=None, ge=0, le=1)
    win_rate_confidence_low: float | None = Field(default=None, ge=0, le=1)
    win_rate_confidence_high: float | None = Field(default=None, ge=0, le=1)
    expectancy: float | None = None
    profit_factor: float | None = Field(default=None, ge=0)
    warning_codes: list[str] = Field(default_factory=list, max_length=12)
    reasons: list[str] = Field(default_factory=list, max_length=12)


class PaperSignalSnapshot(BaseModel):
    id: str
    track_id: str
    checked_at: datetime
    data_as_of: str
    window_start: date
    window_end: date
    latest_price: float
    interval: BarInterval
    signal_state: dict[str, Any]
    metrics: BacktestMetrics
    benchmark_metrics: BacktestMetrics
    comparison: ExperimentComparison
    diagnostics: dict[str, Any]
    citations: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    forward: PaperForwardState | None = None


class PaperTrackCreate(BaseModel):
    experiment_id: str = Field(min_length=1, max_length=64)


class PaperTrackStatusUpdate(BaseModel):
    status: Literal["active", "paused"]


class PaperTrackRecord(BaseModel):
    id: str
    experiment: ExperimentRecord
    status: Literal["active", "paused"]
    created_at: datetime
    updated_at: datetime
    last_checked_at: datetime | None = None
    last_error: str | None = None
    snapshot_count: int = Field(default=0, ge=0)
    observed_bar_count: int = Field(default=0, ge=0)
    forward_health: PaperForwardHealth | None = None
    snapshots: list[PaperSignalSnapshot] = Field(default_factory=list)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=30)
    api_key: SecretStr | None = None
    base_url: str | None = None
    model: str | None = None
    demo_id: str | None = None
    symbol: str | None = Field(default=None, min_length=1, max_length=20)
    provider: RequestedProvider = "auto"
    instrument_name: str | None = Field(default=None, min_length=1, max_length=120)


class ChatResponse(BaseModel):
    content: str
    citations: list[dict[str, Any]]
    tool_calls: list[str]
    grounded: bool
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
