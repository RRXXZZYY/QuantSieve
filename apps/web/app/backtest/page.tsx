"use client";

import { FormEvent, useEffect, useState } from "react";

import { BacktestChart } from "@/components/backtest-chart";
import { SourceBadge } from "@/components/source-badge";
import { SymbolPicker } from "@/components/symbol-picker";
import { apiFetch } from "@/lib/api";
import {
  backtestRunReceiptHasExpired,
  buildBacktestExperimentSaveRequest,
} from "@/lib/backtest-experiments";
import {
  elapsedCalendarDays,
  preferredCalendarPreset,
  requestedCalendarDays,
} from "@/lib/date-range";
import {
  formatAnnualizedReturn,
  formatCompact,
  formatDate,
  formatPercent,
} from "@/lib/format";
import { instrumentLabel, resolveInstrument } from "@/lib/instruments";
import {
  DEFAULT_MAX_CASH_STREAK_BARS,
  RESEARCH_MINIMUM_PROFITABLE_FOLD_RATIO,
  profileCashStreakBars,
  validateResearchContract,
} from "@/lib/research-contract";
import {
  INTEGER_PARAMETER_KEYS,
  manualParameterValidationError,
} from "@/lib/strategy-parameters";
import type {
  BarInterval,
  BacktestPayload,
  CrossMarketExperimentRecord,
  DiscoveryHorizonRequirements,
  ExperimentCreatePayload,
  ExperimentRecord,
  Instrument,
  MarketHistoryCapabilities,
  OptimizationObjective,
  OptimizationResult,
  PaperTrack,
  Strategy,
  StrategyComparisonPayload,
  StrategyDiscoveryCandidate,
  StrategyDiscoveryPayload,
  StrategyRobustnessPayload,
} from "@/lib/types";

const INTERVALS: Array<{ id: BarInterval; label: string; detail: string }> = [
  { id: "15m", label: "15 分钟", detail: "短线 / 日内" },
  { id: "1h", label: "1 小时", detail: "日内 / 波段" },
  { id: "4h", label: "4 小时", detail: "波段趋势" },
  { id: "1d", label: "日线", detail: "中长期" },
  { id: "1wk", label: "周线", detail: "长期配置" },
];

// Keep this in step with the execution assumptions shown beside the manual
// backtest controls. This is a one-way, fully-invested position-change bound,
// not a forecast of the strategy's realised costs.
const ASSUMED_ONE_WAY_TRANSACTION_FRICTION = 0.0005;
const DEFAULT_ANNUAL_FRICTION_BUDGET = 0.2;
const DEFAULT_FRICTION_BUDGET_TRADE_CAP = Math.floor(
  DEFAULT_ANNUAL_FRICTION_BUDGET / ASSUMED_ONE_WAY_TRANSACTION_FRICTION,
);

const DEFAULT_MAX_TRADES_PER_YEAR: Record<BarInterval, number> = {
  // Short intervals must not silently admit a strategy whose full-position
  // turnover could consume most of a year's return in assumed friction.
  "15m": DEFAULT_FRICTION_BUDGET_TRADE_CAP,
  "1h": DEFAULT_FRICTION_BUDGET_TRADE_CAP,
  "4h": 360,
  "1d": 120,
  "1wk": 24,
};

const DEFAULT_DISCOVERY_HORIZON_REQUIREMENTS: DiscoveryHorizonRequirements["intervals"] = {
  "15m": { full_sample_days: 90, holdout_days: 18 },
  "1h": { full_sample_days: 180, holdout_days: 36 },
  "4h": { full_sample_days: 365, holdout_days: 73 },
  "1d": { full_sample_days: 365, holdout_days: 73 },
  "1wk": { full_sample_days: 730, holdout_days: 146 },
};

type ResearchProfile = {
  id: "balanced" | "return" | "risk_adjusted" | "drawdown";
  label: string;
  detail: string;
  objective: OptimizationObjective;
  minimumTradesPerYear: number;
  maximumTradesPerYear: number;
  minimumExposure: number;
  minimumAnnualizedReturn: number;
  maximumDrawdown: number;
  dailyMaximumCashStreakBars: number;
  minimumTimingPositiveFoldRatio: number;
};

type InitializationStatus = "idle" | "loading" | "ready" | "error";

// These profiles make the research contract explicit before any parameter
// search starts. They are constraints, not promises of a profitable strategy.
const RESEARCH_PROFILES: readonly ResearchProfile[] = [
  {
    id: "balanced",
    label: "收益 / 回撤平衡",
    detail: "默认：寻找可参与、回撤不超过 30% 的候选。",
    objective: "balanced",
    minimumTradesPerYear: 2,
    maximumTradesPerYear: 120,
    minimumExposure: 20,
    minimumAnnualizedReturn: 0,
    maximumDrawdown: 30,
    dailyMaximumCashStreakBars: 120,
    minimumTimingPositiveFoldRatio: 50,
  },
  {
    id: "return",
    label: "收益优先",
    detail: "接受更大历史波动，优先检验相对收益是否成立。",
    objective: "total_return",
    minimumTradesPerYear: 2,
    maximumTradesPerYear: 120,
    minimumExposure: 50,
    minimumAnnualizedReturn: 0,
    maximumDrawdown: 60,
    dailyMaximumCashStreakBars: 180,
    minimumTimingPositiveFoldRatio: 50,
  },
  {
    id: "risk_adjusted",
    label: "风险调整收益",
    detail: "优先夏普比率，同时保留参与度与基准比较门槛。",
    objective: "sharpe_ratio",
    minimumTradesPerYear: 2,
    maximumTradesPerYear: 90,
    minimumExposure: 25,
    minimumAnnualizedReturn: 0,
    maximumDrawdown: 35,
    dailyMaximumCashStreakBars: 120,
    minimumTimingPositiveFoldRatio: 50,
  },
  {
    id: "drawdown",
    label: "回撤控制",
    detail: "将历史最大回撤压到 20%；无解时会如实提示。",
    objective: "drawdown_control",
    minimumTradesPerYear: 1,
    maximumTradesPerYear: 60,
    minimumExposure: 20,
    minimumAnnualizedReturn: 0,
    maximumDrawdown: 20,
    dailyMaximumCashStreakBars: 90,
    minimumTimingPositiveFoldRatio: 50,
  },
];

const PARAMETER_LABELS: Record<string, string> = {
  fast: "快速周期",
  slow: "慢速周期",
  signal: "信号周期",
  period: "计算周期",
  rsi_period: "RSI 周期",
  lookback: "回看周期",
  threshold: "动量阈值",
  oversold: "超卖线",
  overbought: "超买线",
  exit_rsi: "RSI 离场线",
  deviations: "标准差倍数",
  entry_z: "入场 Z 值",
  exit_z: "离场 Z 值",
  entry_period: "突破周期",
  exit_period: "退出周期",
  multiplier: "成交量倍数",
  regime: "长期趋势周期",
  atr_period: "ATR 波动周期",
  atr_multiplier: "ATR 距离倍数",
  trend_period: "趋势周期",
  volatility_period: "波动率周期",
  minimum_allocation: "最低风险仓位",
  maximum_allocation: "最高风险仓位",
  adjustment_band: "仓位调整带",
  defensive_exposure: "弱势期核心仓位",
  allocation: "恒定资金仓位",
};

const PARAMETER_HINTS: Record<string, string> = {
  fast: "更短会更快响应，也更容易产生噪声交易",
  slow: "定义较慢的趋势背景；通常应大于快速周期",
  signal: "MACD 信号平滑周期",
  period: "指标计算使用的历史 K 线数量",
  rsi_period: "RSI 计算周期；更短更敏感",
  lookback: "用于比较价格或通道的历史 K 线数量",
  threshold: "触发趋势或动量信号所需的最小幅度",
  oversold: "RSI 低于该值才允许逆转入场",
  overbought: "RSI 高于该值触发离场或反向条件",
  exit_rsi: "持仓后 RSI 回到该值即离场，不是收益目标",
  deviations: "布林带宽度；更大代表信号更少",
  entry_z: "偏离均值达到该标准分后才入场",
  exit_z: "回归到该标准分时离场；通常低于入场 Z 值",
  entry_period: "突破前高/低所观察的 K 线数量",
  exit_period: "退出通道的 K 线数量；通常短于突破周期",
  multiplier: "成交量相对均值的放大倍数",
  regime: "长期趋势过滤的 EMA 周期；更长更保守",
  atr_period: "ATR 波动率的计算周期",
  atr_multiplier: "以 ATR 设定退出距离；更大代表更宽的风险空间",
  trend_period: "高于这条长期 EMA 才允许持有风险资产",
  volatility_period: "近期波动与慢速波动基准的比较窗口；仓位仅使用已完成 K 线调整",
  minimum_allocation: "趋势有效时的最低仓位；不代表止损或收益目标",
  maximum_allocation: "趋势有效时的最高仓位；上限为 100%，策略不使用杠杆",
  adjustment_band: "只有目标仓位与当前仓位相差达到此幅度才再平衡；趋势失效仍会退出",
  defensive_exposure: "弱势市场仍保留的资金比例",
  allocation: "从第一根 K 线起持续配置的资金比例",
};

type ParameterProvenanceKind =
  | "template_default"
  | "manual"
  | "optimized_unverified"
  | "provisional_holdout"
  | "validated_holdout"
  | "experiment_snapshot"
  | "passive_baseline";

type ParameterEvidenceScope = {
  symbol: string;
  provider: Instrument["provider"];
  interval: BarInterval;
  start: string;
  end: string;
  objective: OptimizationObjective;
  minimumTradesPerYear: number;
  maximumTradesPerYear: number;
  minimumExposure: number;
  minimumAnnualizedReturn: number;
  maximumDrawdown: number;
  maximumCashStreakBars: number;
  minimumTimingPositiveFoldRatio: number;
};

type ParameterProvenance = {
  kind: ParameterProvenanceKind;
  scope?: ParameterEvidenceScope;
};

const PARAMETER_PROVENANCE_COPY: Record<
  ParameterProvenanceKind,
  { label: string; detail: string }
> = {
  template_default: {
    label: "模板默认值 · 未寻优",
    detail: "这是可复现的策略起点，不是针对当前标的和周期的最优参数。",
  },
  manual: {
    label: "手动参数 · 未验证",
    detail: "你已修改参数；请运行新的独立研究，不把旧结果当作验证。",
  },
  optimized_unverified: {
    label: "已寻优 · 留出未通过",
    detail: "参数来自开发期寻优，但没有通过最终留出验证，不能作为可用策略。",
  },
  provisional_holdout: {
    label: "参数已冻结 · 前向补证",
    detail: "最终留出交易样本不足；不得继续调参，只能用之后新增 K 线积累证据。",
  },
  validated_holdout: {
    label: "最终留出通过 · 历史证据",
    detail: "参数通过了当前实验的样本外门槛，仍不代表未来最优或自动交易建议。",
  },
  experiment_snapshot: {
    label: "已载入实验快照",
    detail: "这是已保存的历史配置；需以当前数据重新运行，才会形成新的证据。",
  },
  passive_baseline: {
    label: "被动基准参数",
    detail: "买入持有不需要寻优参数；它用于检验主动策略是否真的创造价值。",
  },
};

const DEFAULT_INSTRUMENT: Instrument = {
  symbol: "600519",
  name: "贵州茅台",
  market: "CN",
  exchange: "上海证券交易所",
  currency: "CNY",
  provider: "akshare",
  asset_type: "equity",
};

function isoDate(value: Date): string {
  return value.toISOString().slice(0, 10);
}

function periodStart(years: number): string {
  const value = new Date();
  value.setFullYear(value.getFullYear() - years);
  return isoDate(value);
}

function daysAgo(days: number): string {
  const value = new Date();
  value.setDate(value.getDate() - days);
  return isoDate(value);
}

function maximumFrequencyFrictionCeiling(tradesPerYear: number): string {
  return formatPercent(
    Math.max(0, Number.isFinite(tradesPerYear) ? tradesPerYear : 0) *
      ASSUMED_ONE_WAY_TRANSACTION_FRICTION,
  );
}

function discoveryHorizonStatus(
  interval: BarInterval,
  days: number,
  requirements: DiscoveryHorizonRequirements["intervals"],
) {
  const requirement = requirements[interval];
  return {
    fullSampleDays: requirement.full_sample_days,
    holdoutDays: requirement.holdout_days,
    validationEligible: days >= requirement.full_sample_days,
  };
}

function preferredDiscoveryPreset(
  interval: BarInterval,
  instrument: Instrument | null,
  requirements: DiscoveryHorizonRequirements["intervals"],
) {
  const options = presets(interval, instrument);
  const minimumDays = requirements[interval].full_sample_days;
  // Leave a small calendar buffer: providers often omit the still-forming bar
  // or the current session, so an exact boundary may fail server-side review.
  const preferred = preferredCalendarPreset(options, minimumDays);
  if (!preferred) {
    throw new Error(`No backtest date presets are configured for ${interval}.`);
  }
  return preferred;
}

function presets(
  interval: BarInterval,
  instrument: Instrument | null = null,
): Array<{ id: string; label: string; days: number }> {
  if (
    ["15m", "1h", "4h"].includes(interval) &&
    instrument?.provider === "yfinance"
  ) {
    return [
      { id: "7D", label: "7 天", days: 7 },
      { id: "30D", label: "30 天", days: 30 },
      // Yahoo's limit counts both boundary dates. A 59-day lookback therefore
      // requests the full supported 60-calendar-day window.
      { id: "60D", label: "60 日上限", days: 59 },
    ];
  }
  if (interval === "15m" && instrument?.provider === "binance") {
    return [
      { id: "7D", label: "7 天", days: 7 },
      { id: "30D", label: "30 天", days: 30 },
      { id: "120D", label: "120 天", days: 120 },
    ];
  }
  if (
    ["15m", "1h", "4h"].includes(interval) &&
    instrument?.provider === "akshare"
  ) {
    return [
      { id: "7D", label: "7 天", days: 7 },
      { id: "30D", label: "30 天", days: 30 },
      { id: "90D", label: "90 天", days: 90 },
    ];
  }
  if (interval === "15m") {
    return [
      { id: "7D", label: "7 天", days: 7 },
      { id: "30D", label: "30 天", days: 30 },
      { id: "90D", label: "90 天", days: 90 },
    ];
  }
  if (interval === "1h") {
    return [
      { id: "30D", label: "30 天", days: 30 },
      { id: "180D", label: "6 个月", days: 180 },
      { id: "1Y", label: "1 年", days: 365 },
    ];
  }
  if (interval === "4h") {
    return [
      { id: "90D", label: "90 天", days: 90 },
      { id: "1Y", label: "1 年", days: 365 },
      { id: "3Y", label: "3 年", days: 1_095 },
    ];
  }
  return [
    { id: "1Y", label: "1 年", days: 365 },
    { id: "3Y", label: "3 年", days: 1_095 },
    { id: "5Y", label: "5 年", days: 1_825 },
  ];
}

function supportedIntervals(instrument: Instrument | null): BarInterval[] {
  if (instrument?.provider === "futures" || instrument?.provider === "macro") {
    return ["1d", "1wk"];
  }
  return ["15m", "1h", "4h", "1d", "1wk"];
}

function barsAsTime(bars: number, interval: BarInterval): string {
  const count = bars.toLocaleString("zh-CN");
  if (interval === "1d") return `约 ${count} 个交易日`;
  if (interval === "1wk") return `约 ${count} 个交易周`;
  const intervalLabel = INTERVALS.find((item) => item.id === interval)?.label ?? interval;
  return `约 ${count} 根 ${intervalLabel} K 线，不等于连续自然时间`;
}

function annualizedTradeFrequency(
  tradesPerYear: number,
  durationYears: number,
): string {
  const value = `${tradesPerYear.toFixed(1)} 笔/年`;
  return durationYears < 0.95 ? `${value}（短样本年化）` : value;
}

function annualizationBasis(
  annualPeriods: number,
  interval: BarInterval,
): string {
  const intervalLabel =
    INTERVALS.find((item) => item.id === interval)?.label ?? interval;
  return `年化收益与仓位变动频率按样本首尾的真实日历跨度折算；波动率与夏普按每年 ${annualPeriods.toLocaleString("zh-CN")} 根 ${intervalLabel} K 线计算。`;
}

function holdoutNextAction(
  code: OptimizationResult["validation_code"],
  forwardEligible: boolean,
): string {
  if (code === "sample_insufficient") {
    return forwardEligible
      ? "停止调参并冻结当前参数；用激活后的新 K 线继续积累，达到证据门槛前仍不称为验证通过。"
      : "最终留出不足 5 个已闭合独立决策周期，不启动跟踪；应在下一次预先登记的新实验中延长区间或改用更短周期，不能反复读取当前留出集挑结果。";
  }
  if (code === "negative_return") {
    return "淘汰当前候选；不要在同一留出集上继续调参。需要更换策略假设时，应作为一组全新的预先登记实验。";
  }
  if (code === "benchmark_capture_failed" || code === "weak_market_lag") {
    return "当前主动策略没有证明相对价值；保留买入持有作为基准，不用降低门槛把落后候选包装成冠军。";
  }
  if (code === "exposure_matched_lag") {
    return "淘汰当前择时候选；以策略平均持仓率作为首根开盘初始投入、之后固定份额持有的被动基准反而更好，不能把长期空仓造成的低回撤当成策略价值。";
  }
  if (code === "return_target_failed") {
    return "收益目标未守住，当前候选应淘汰；若目标本身需要调整，应先确定新目标，再开始新的独立实验。";
  }
  if (code === "drawdown_limit_failed") {
    return "风险预算已经突破，不能进入前向使用；先更换风险控制假设，再用新的未见样本验证。";
  }
  if (
    code === "frequency_too_low" ||
    code === "exposure_too_low" ||
    code === "cash_streak_too_long"
  ) {
    return "候选参与度不足；不要只为增加交易而降低门槛，可在下一组新实验中预先选择更短 K 线或更合适的策略类别。";
  }
  if (code === "frequency_too_high" || code === "cost_stress_failed") {
    return "交易摩擦风险不可接受；淘汰当前候选，下一组实验应先限制频率或采用更低换手的策略结构。";
  }
  return code === "passed"
    ? "可以保存实验并开始纸面验证；仍需用激活后数据持续检查退化。"
    : "保留当前证据，不根据同一留出结果继续追逐参数；下一次修改应作为独立实验。";
}

function provenanceForOptimization(
  optimization: OptimizationResult,
): ParameterProvenanceKind {
  if (optimization.validation_passed) return "validated_holdout";
  if (optimization.forward_observation_eligible) return "provisional_holdout";
  return "optimized_unverified";
}

export default function BacktestPage() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [strategyCatalogStatus, setStrategyCatalogStatus] =
    useState<InitializationStatus>("loading");
  const [strategyCatalogError, setStrategyCatalogError] = useState("");
  const [strategyCatalogAttempt, setStrategyCatalogAttempt] = useState(0);
  const [discoveryHorizonRequirements, setDiscoveryHorizonRequirements] = useState(
    DEFAULT_DISCOVERY_HORIZON_REQUIREMENTS,
  );
  const [marketCapabilities, setMarketCapabilities] =
    useState<MarketHistoryCapabilities | null>(null);
  const [marketCapabilitiesStatus, setMarketCapabilitiesStatus] =
    useState<InitializationStatus>("loading");
  const [marketCapabilitiesError, setMarketCapabilitiesError] = useState("");
  const [marketCapabilitiesAttempt, setMarketCapabilitiesAttempt] = useState(0);
  const [symbolQuery, setSymbolQuery] = useState(instrumentLabel(DEFAULT_INSTRUMENT));
  const [selectedInstrument, setSelectedInstrument] = useState<Instrument | null>(
    DEFAULT_INSTRUMENT,
  );
  const [resultInstrument, setResultInstrument] = useState<Instrument | null>(null);
  const [strategyId, setStrategyId] = useState("sma-cross");
  const [parameters, setParameters] = useState<Record<string, number>>({});
  const [parameterProvenance, setParameterProvenance] =
    useState<ParameterProvenance>({ kind: "template_default" });
  const [interval, setInterval] = useState<BarInterval>("1d");
  const [startDate, setStartDate] = useState(periodStart(3));
  const [endDate, setEndDate] = useState(isoDate(new Date()));
  const [period, setPeriod] = useState("3Y");
  const [objective, setObjective] = useState<OptimizationObjective>("balanced");
  const [minimumTradesPerYear, setMinimumTradesPerYear] = useState(2);
  const [maximumTradesPerYear, setMaximumTradesPerYear] = useState(120);
  const [minimumExposure, setMinimumExposure] = useState(20);
  const [minimumAnnualizedReturn, setMinimumAnnualizedReturn] = useState(0);
  const [maximumDrawdown, setMaximumDrawdown] = useState(30);
  const [maximumCashStreakBars, setMaximumCashStreakBars] = useState(120);
  const [minimumTimingPositiveFoldRatio, setMinimumTimingPositiveFoldRatio] =
    useState(50);
  const [optimization, setOptimization] = useState<OptimizationResult | null>(null);
  const [comparison, setComparison] = useState<StrategyComparisonPayload | null>(null);
  const [discovery, setDiscovery] = useState<StrategyDiscoveryPayload | null>(null);
  const [robustnessTargets, setRobustnessTargets] = useState<Instrument[]>([]);
  const [robustnessQuery, setRobustnessQuery] = useState("");
  const [robustness, setRobustness] = useState<StrategyRobustnessPayload | null>(null);
  const [crossMarketExperiments, setCrossMarketExperiments] = useState<
    CrossMarketExperimentRecord[]
  >([]);
  const [crossMarketArchiveName, setCrossMarketArchiveName] = useState("");
  const [crossMarketArchiveNotes, setCrossMarketArchiveNotes] = useState("");
  const [result, setResult] = useState<BacktestPayload | null>(null);
  const [experiments, setExperiments] = useState<ExperimentRecord[]>([]);
  const [showExperimentLibrary, setShowExperimentLibrary] = useState(false);
  const [showSaveEditor, setShowSaveEditor] = useState(false);
  const [experimentName, setExperimentName] = useState("");
  const [experimentNotes, setExperimentNotes] = useState("");
  const [experimentOperation, setExperimentOperation] = useState<
    "save" | "delete" | "track" | "cross-save" | "cross-delete" | null
  >(null);
  const [experimentNotice, setExperimentNotice] = useState("");
  const [forwardExperiment, setForwardExperiment] =
    useState<ExperimentRecord | null>(null);
  const [forwardTrack, setForwardTrack] = useState<PaperTrack | null>(null);
  const [forwardResult, setForwardResult] = useState<BacktestPayload | null>(null);
  const [operation, setOperation] = useState<
    "backtest" | "optimize" | "compare" | "discover" | "robustness" | null
  >(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;

    void (async () => {
      try {
        const items = await apiFetch<Strategy[]>("/api/v1/strategies");
        if (cancelled) return;
        if (items.length === 0) {
          throw new Error("服务端返回了空策略目录");
        }
        const requested = new URLSearchParams(window.location.search).get("strategy");
        const fallbackId = items.some((strategy) => strategy.id === "sma-cross")
          ? "sma-cross"
          : items[0].id;
        const initialId =
          requested && items.some((strategy) => strategy.id === requested)
            ? requested
            : fallbackId;
        setStrategies(items);
        setStrategyId(initialId);
        setParameters({
          ...(items.find((strategy) => strategy.id === initialId)?.parameters ?? {}),
        });
        setParameterProvenance({ kind: "template_default" });
        setStrategyCatalogStatus("ready");
      } catch (reason) {
        if (cancelled) return;
        setStrategies([]);
        setParameters({});
        setStrategyCatalogError(
          reason instanceof Error ? reason.message : "无法读取策略目录",
        );
        setStrategyCatalogStatus("error");
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [strategyCatalogAttempt]);

  useEffect(() => {
    apiFetch<ExperimentRecord[]>("/api/v1/experiments?limit=100")
      .then(setExperiments)
      .catch(() => setExperiments([]));
    apiFetch<CrossMarketExperimentRecord[]>("/api/v1/experiments/cross-market?limit=50")
      .then(setCrossMarketExperiments)
      .catch(() => setCrossMarketExperiments([]));
    apiFetch<DiscoveryHorizonRequirements>("/api/v1/backtests/discovery-requirements")
      .then((payload) => setDiscoveryHorizonRequirements(payload.intervals))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!selectedInstrument) return;
    let cancelled = false;
    const instrument = selectedInstrument;

    void (async () => {
      try {
        const payload = await apiFetch<MarketHistoryCapabilities>(
          `/api/v1/market/${encodeURIComponent(instrument.symbol)}/capabilities?provider=${instrument.provider}`,
        );
        if (cancelled) return;
        if (
          payload.provider !== instrument.provider ||
          payload.symbol.toUpperCase() !== instrument.symbol.toUpperCase() ||
          !INTERVALS.every(
            (item) =>
              typeof payload.intervals?.[item.id]?.supported === "boolean",
          )
        ) {
          throw new Error("行情能力响应不完整或与当前所选标的不一致");
        }
        setMarketCapabilities(payload);
        setMarketCapabilitiesStatus("ready");
      } catch (reason) {
        if (cancelled) return;
        setMarketCapabilities(null);
        setMarketCapabilitiesError(
          reason instanceof Error ? reason.message : "无法确认行情能力",
        );
        setMarketCapabilitiesStatus("error");
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [selectedInstrument, marketCapabilitiesAttempt]);

  const selectedStrategy = strategies.find((strategy) => strategy.id === strategyId);
  const parameterValidationError =
    selectedStrategy && Object.keys(parameters).length > 0
      ? manualParameterValidationError(strategyId, parameters)
      : null;
  const researchContractError = validateResearchContract({
    minimumTradesPerYear,
    maximumTradesPerYear,
    minimumExposure,
    minimumAnnualizedReturn,
    maximumDrawdown,
    maximumCashStreakBars,
    minimumTimingPositiveFoldRatio,
  });
  const activeMarketCapabilities =
    selectedInstrument &&
    marketCapabilitiesStatus === "ready" &&
    marketCapabilities?.provider === selectedInstrument.provider &&
    marketCapabilities.symbol.toUpperCase() === selectedInstrument.symbol.toUpperCase()
      ? marketCapabilities
      : null;
  const strategyCatalogReady =
    strategyCatalogStatus === "ready" && strategies.length > 0;
  const marketCapabilitiesConfirmed = activeMarketCapabilities !== null;
  const researchPrerequisitesReady =
    strategyCatalogReady && marketCapabilitiesConfirmed;
  const availableIntervals = activeMarketCapabilities
    ? INTERVALS.filter((item) => activeMarketCapabilities.intervals[item.id].supported).map(
        (item) => item.id,
      )
    : [];
  const selectedIntervalCapability = activeMarketCapabilities?.intervals[interval] ?? null;
  const plannedRangeDays = elapsedCalendarDays(startDate, endDate);
  const requestedRangeDays = requestedCalendarDays(startDate, endDate);
  const maximumHistoryDays = selectedIntervalCapability?.max_history_days ?? null;
  const historyWindowExceeded =
    maximumHistoryDays !== null && requestedRangeDays > maximumHistoryDays;
  const plannedHorizon = discoveryHorizonStatus(
    interval,
    plannedRangeDays,
    discoveryHorizonRequirements,
  );
  const currentForwardExperiment =
    forwardResult === result ? forwardExperiment : null;
  const currentForwardTrack = forwardResult === result ? forwardTrack : null;
  const forwardObservationMode =
    optimization?.validation_passed &&
    optimization.validation_metrics.closed_trades >= 10
      ? "validated"
      : optimization?.forward_observation_eligible
        ? "provisional"
        : null;
  const fixedShareBaseline =
    result?.data_metadata.execution_model === "fixed_shares";

  function currentParameterEvidenceScope(
    instrument: Instrument | null = selectedInstrument,
  ): ParameterEvidenceScope | null {
    if (!instrument) return null;
    return {
      symbol: instrument.symbol.toUpperCase(),
      provider: instrument.provider,
      interval,
      start: startDate,
      end: endDate,
      objective,
      minimumTradesPerYear,
      maximumTradesPerYear,
      minimumExposure,
      minimumAnnualizedReturn,
      maximumDrawdown,
      maximumCashStreakBars,
      minimumTimingPositiveFoldRatio,
    };
  }

  const parameterEvidenceIsCurrent =
    !parameterProvenance.scope ||
    JSON.stringify(parameterProvenance.scope) ===
      JSON.stringify(currentParameterEvidenceScope());
  const parameterProvenanceCopy = PARAMETER_PROVENANCE_COPY[parameterProvenance.kind];
  const activeResearchProfile = RESEARCH_PROFILES.find(
    (profile) =>
      profile.objective === objective &&
      profile.minimumTradesPerYear === minimumTradesPerYear &&
      profile.maximumTradesPerYear === maximumTradesPerYear &&
      profile.minimumExposure === minimumExposure &&
      profile.minimumAnnualizedReturn === minimumAnnualizedReturn &&
      profile.maximumDrawdown === maximumDrawdown &&
      profileCashStreakBars(interval, profile.dailyMaximumCashStreakBars) ===
        maximumCashStreakBars &&
      profile.minimumTimingPositiveFoldRatio === minimumTimingPositiveFoldRatio,
  )?.id;

  function applyResearchProfile(profile: ResearchProfile) {
    setObjective(profile.objective);
    setMinimumTradesPerYear(profile.minimumTradesPerYear);
    setMaximumTradesPerYear(profile.maximumTradesPerYear);
    setMinimumExposure(profile.minimumExposure);
    setMinimumAnnualizedReturn(profile.minimumAnnualizedReturn);
    setMaximumDrawdown(profile.maximumDrawdown);
    setMaximumCashStreakBars(
      profileCashStreakBars(interval, profile.dailyMaximumCashStreakBars),
    );
    setMinimumTimingPositiveFoldRatio(profile.minimumTimingPositiveFoldRatio);
    setDiscovery(null);
    setComparison(null);
    setRobustness(null);
    setOptimization(null);
    setResult(null);
  }

  function choosePeriod(id: string, days: number) {
    setStartDate(daysAgo(days));
    setEndDate(isoDate(new Date()));
    setPeriod(id);
    setDiscovery(null);
    setComparison(null);
    setRobustness(null);
    setResult(null);
    setOptimization(null);
  }

  function chooseInterval(nextInterval: BarInterval) {
    const nextPreset = preferredDiscoveryPreset(
      nextInterval,
      selectedInstrument,
      discoveryHorizonRequirements,
    );
    setInterval(nextInterval);
    choosePeriod(nextPreset.id, nextPreset.days);
    const activeProfile = RESEARCH_PROFILES.find(
      (profile) => profile.id === activeResearchProfile,
    );
    setMaximumCashStreakBars(
      activeProfile
        ? profileCashStreakBars(
            nextInterval,
            activeProfile.dailyMaximumCashStreakBars,
          )
        : DEFAULT_MAX_CASH_STREAK_BARS[nextInterval],
    );
    setMaximumTradesPerYear(
      activeProfile
        ? activeProfile.maximumTradesPerYear
        : DEFAULT_MAX_TRADES_PER_YEAR[nextInterval],
    );
    setOptimization(null);
    setDiscovery(null);
    setComparison(null);
    setRobustness(null);
    setResult(null);
  }

  function retryStrategyCatalog() {
    if (strategyCatalogStatus === "loading") return;
    setStrategyCatalogStatus("loading");
    setStrategyCatalogError("");
    setStrategies([]);
    setParameters({});
    setStrategyCatalogAttempt((attempt) => attempt + 1);
  }

  function retryMarketCapabilities() {
    if (!selectedInstrument || marketCapabilitiesStatus === "loading") return;
    setMarketCapabilitiesStatus("loading");
    setMarketCapabilitiesError("");
    setMarketCapabilities(null);
    setMarketCapabilitiesAttempt((attempt) => attempt + 1);
  }

  function ensureResearchPrerequisitesReady(): boolean {
    if (!strategyCatalogReady) {
      setError(
        strategyCatalogStatus === "error"
          ? "策略目录尚不可用，请先在策略模板区重试。"
          : "策略目录仍在加载，请稍候。",
      );
      return false;
    }
    if (!selectedInstrument) {
      setError("请先从统一市场搜索结果中选择标的，并等待行情能力确认。");
      return false;
    }
    if (!marketCapabilitiesConfirmed) {
      setError(
        marketCapabilitiesStatus === "error"
          ? "行情能力未知，请先在 K 线周期区重新确认。"
          : "正在确认所选标的的行情能力，请稍候。",
      );
      return false;
    }
    return true;
  }

  function ensureResearchContractValid(): boolean {
    if (!researchContractError) return true;
    setError(`研究合同无法运行：${researchContractError}`);
    return false;
  }

  async function assertHistoryWindow(instrument: Instrument) {
    const capabilities =
      marketCapabilities?.provider === instrument.provider &&
      marketCapabilities.symbol.toUpperCase() === instrument.symbol.toUpperCase()
        ? marketCapabilities
        : await apiFetch<MarketHistoryCapabilities>(
            `/api/v1/market/${encodeURIComponent(instrument.symbol)}/capabilities?provider=${instrument.provider}`,
          );
    const capability = capabilities.intervals[interval];
    if (!capability.supported) {
      throw new Error(
        `${instrument.name} 的数据源不支持当前 K 线周期，请切换到可用周期后再研究。`,
      );
    }
    const requestedDays = requestedCalendarDays(startDate, endDate);
    if (
      capability.max_history_days !== null &&
      requestedDays > capability.max_history_days
    ) {
      throw new Error(
        `${instrument.name} 的 ${INTERVALS.find((item) => item.id === interval)?.label ?? interval} 最多可回看 ${capability.max_history_days} 天；当前区间约 ${requestedDays} 天。请缩短区间或切换更大周期。`,
      );
    }
    return capabilities;
  }

  async function run(event: FormEvent) {
    event.preventDefault();
    if (parameterValidationError) {
      setError(`当前参数无法运行：${parameterValidationError}`);
      return;
    }
    await execute(false);
  }

  async function optimize() {
    await execute(true);
  }

  function addRobustnessTarget(instrument: Instrument | null) {
    if (!instrument) return;
    setRobustnessTargets((items) => {
      const key = `${instrument.provider}:${instrument.symbol.toUpperCase()}`;
      return items.some((item) => `${item.provider}:${item.symbol.toUpperCase()}` === key)
        ? items
        : [...items, instrument].slice(0, 3);
    });
    setRobustness(null);
  }

  function removeRobustnessTarget(instrument: Instrument) {
    setRobustnessTargets((items) =>
      items.filter(
        (item) =>
          `${item.provider}:${item.symbol.toUpperCase()}` !==
          `${instrument.provider}:${instrument.symbol.toUpperCase()}`,
      ),
    );
    setRobustness(null);
  }

  async function validateAcrossMarkets() {
    if (!ensureResearchPrerequisitesReady()) return;
    if (!ensureResearchContractValid()) return;
    setOperation("robustness");
    setError("");
    setExperimentNotice("");
    try {
      const primary = selectedInstrument ?? (await resolveInstrument(symbolQuery));
      if (!primary) {
        throw new Error("请先从统一市场搜索结果中选择当前标的，再添加 1–3 个不同市场的对照标的。");
      }
      const markets = [primary, ...robustnessTargets].filter(
        (instrument, index, items) =>
          items.findIndex(
            (candidate) =>
              candidate.symbol.toUpperCase() === instrument.symbol.toUpperCase(),
          ) === index,
      );
      if (markets.length < 2) {
        throw new Error("跨市场复现至少需要当前标的之外的 1 个不同标的。");
      }
      await Promise.all(markets.map((instrument) => assertHistoryWindow(instrument)));
      setSelectedInstrument(primary);
      setSymbolQuery(instrumentLabel(primary));
      const payload = await apiFetch<StrategyRobustnessPayload>(
        "/api/v1/backtests/robustness",
        {
          method: "POST",
          body: JSON.stringify({
            strategy_id: strategyId,
            markets: markets.map((instrument) => ({
              symbol: instrument.symbol,
              provider: instrument.provider,
            })),
            objective,
            interval,
            start: startDate,
            end: endDate,
            train_ratio: 0.8,
            walk_forward_windows: 3,
            minimum_trades_per_year: minimumTradesPerYear,
            maximum_trades_per_year: maximumTradesPerYear,
            minimum_exposure: minimumExposure / 100,
            minimum_annualized_return: minimumAnnualizedReturn / 100,
            maximum_drawdown: maximumDrawdown / 100,
            maximum_cash_streak_ratio: 1,
            maximum_cash_streak_bars: maximumCashStreakBars,
            minimum_profitable_fold_ratio:
              RESEARCH_MINIMUM_PROFITABLE_FOLD_RATIO,
            minimum_timing_positive_fold_ratio: minimumTimingPositiveFoldRatio / 100,
          }),
        },
      );
      setRobustness(payload);
      setCrossMarketArchiveName(
        `${payload.strategy.name} · ${markets.map((item) => item.symbol).join(" / ")}`,
      );
      setCrossMarketArchiveNotes("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "跨市场复现失败");
    } finally {
      setOperation(null);
    }
  }

  async function saveCrossMarketExperiment() {
    if (!robustness?.run_id || !crossMarketArchiveName.trim()) return;
    setExperimentOperation("cross-save");
    setExperimentNotice("");
    try {
      const created = await apiFetch<CrossMarketExperimentRecord>(
        "/api/v1/experiments/cross-market/from-run",
        {
          method: "POST",
          body: JSON.stringify({
            name: crossMarketArchiveName.trim(),
            notes: crossMarketArchiveNotes.trim() || null,
            run_id: robustness.run_id,
          }),
        },
      );
      setCrossMarketExperiments((items) => [
        created,
        ...items.filter((item) => item.id !== created.id),
      ]);
      setRobustness(created);
      setExperimentNotice("跨市场证据已按 NAS 服务端回执归档，可随时重新载入审计。")
    } catch (reason) {
      setExperimentNotice(reason instanceof Error ? reason.message : "跨市场证据归档失败");
    } finally {
      setExperimentOperation(null);
    }
  }

  async function deleteCrossMarketExperiment(experiment: CrossMarketExperimentRecord) {
    if (!window.confirm(`删除跨市场实验“${experiment.name}”？此操作不可撤销。`)) return;
    setExperimentOperation("cross-delete");
    try {
      await apiFetch<void>(`/api/v1/experiments/cross-market/${experiment.id}`, {
        method: "DELETE",
      });
      setCrossMarketExperiments((items) => items.filter((item) => item.id !== experiment.id));
      setExperimentNotice("跨市场实验记录已删除。");
    } catch (reason) {
      setExperimentNotice(reason instanceof Error ? reason.message : "删除跨市场实验失败");
    } finally {
      setExperimentOperation(null);
    }
  }

  function loadDiscoveryCandidate(
    candidate: StrategyDiscoveryCandidate,
    instrument: Instrument | null = selectedInstrument,
  ) {
    setStrategyId(candidate.strategy.id);
    setParameters({ ...candidate.strategy.parameters });
    setOptimization(candidate.backtest.optimization ?? null);
    setResult(candidate.backtest);
    setComparison(null);
    const scope = currentParameterEvidenceScope(instrument);
    setParameterProvenance({
      kind: candidate.backtest.optimization
        ? provenanceForOptimization(candidate.backtest.optimization)
        : "optimized_unverified",
      ...(scope ? { scope } : {}),
    });
  }

  async function discoverStrategies() {
    if (!ensureResearchPrerequisitesReady()) return;
    if (!ensureResearchContractValid()) return;
    setOperation("discover");
    setError("");
    setExperimentNotice("");
    setDiscovery(null);
    setComparison(null);
    try {
      const instrument = selectedInstrument ?? (await resolveInstrument(symbolQuery));
      if (!instrument) {
        throw new Error("没有找到这个标的，请从统一市场搜索结果中选择。");
      }
      await assertHistoryWindow(instrument);
      setSelectedInstrument(instrument);
      setSymbolQuery(instrumentLabel(instrument));
      const payload = await apiFetch<StrategyDiscoveryPayload>(
        "/api/v1/backtests/discover",
        {
          method: "POST",
          body: JSON.stringify({
            symbol: instrument.symbol,
            provider: instrument.provider,
            objective,
            interval,
            start: startDate,
            end: endDate,
            train_ratio: 0.8,
            shortlist_size: 3,
            walk_forward_windows: 3,
            minimum_trades_per_year: minimumTradesPerYear,
            maximum_trades_per_year: maximumTradesPerYear,
            minimum_exposure: minimumExposure / 100,
            minimum_annualized_return: minimumAnnualizedReturn / 100,
            maximum_drawdown: maximumDrawdown / 100,
            maximum_cash_streak_ratio: 1,
            maximum_cash_streak_bars: maximumCashStreakBars,
            minimum_profitable_fold_ratio:
              RESEARCH_MINIMUM_PROFITABLE_FOLD_RATIO,
            minimum_timing_positive_fold_ratio:
              minimumTimingPositiveFoldRatio / 100,
          }),
        },
      );
      setDiscovery(payload);
      setComparison(null);
      setResultInstrument(instrument);
      const chosen = payload.champion ?? payload.best_available;
      if (chosen) {
        loadDiscoveryCandidate(chosen, instrument);
      } else {
        setOptimization(null);
        setResult(null);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "策略发现失败");
    } finally {
      setOperation(null);
    }
  }

  async function compareStrategies() {
    if (!ensureResearchPrerequisitesReady()) return;
    setOperation("compare");
    setError("");
    setExperimentNotice("");
    setDiscovery(null);
    setResult(null);
    setOptimization(null);
    try {
      const instrument = selectedInstrument ?? (await resolveInstrument(symbolQuery));
      if (!instrument) {
        throw new Error("没有找到这个标的，请从统一市场搜索结果中选择。");
      }
      await assertHistoryWindow(instrument);
      setSelectedInstrument(instrument);
      setSymbolQuery(instrumentLabel(instrument));
      const payload = await apiFetch<StrategyComparisonPayload>(
        "/api/v1/backtests/compare",
        {
          method: "POST",
          body: JSON.stringify({
            symbol: instrument.symbol,
            provider: instrument.provider,
            interval,
            start: startDate,
            end: endDate,
          }),
        },
      );
      setDiscovery(null);
      setComparison(payload);
      setResultInstrument(instrument);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "策略比较失败");
    } finally {
      setOperation(null);
    }
  }

  async function execute(shouldOptimize: boolean) {
    if (!ensureResearchPrerequisitesReady()) return;
    if (shouldOptimize && !ensureResearchContractValid()) return;
    setOperation(shouldOptimize ? "optimize" : "backtest");
    setError("");
    setExperimentNotice("");
    setDiscovery(null);
    setComparison(null);
    try {
      const instrument = selectedInstrument ?? (await resolveInstrument(symbolQuery));
      if (!instrument) {
        throw new Error("没有找到这个标的，请从统一市场搜索结果中选择。");
      }
      await assertHistoryWindow(instrument);
      setSelectedInstrument(instrument);
      setSymbolQuery(instrumentLabel(instrument));
      const payload = await apiFetch<BacktestPayload>(
        shouldOptimize ? "/api/v1/backtests/optimize" : "/api/v1/backtests",
        {
          method: "POST",
          body: JSON.stringify({
            symbol: instrument.symbol,
            provider: instrument.provider,
            strategy_id: strategyId,
            interval,
            start: startDate,
            end: endDate,
            ...(shouldOptimize
              ? {
                  objective,
                  train_ratio: 0.8,
                  walk_forward_windows: 3,
                  minimum_trades_per_year: minimumTradesPerYear,
                  maximum_trades_per_year: maximumTradesPerYear,
                  minimum_exposure: minimumExposure / 100,
                  minimum_annualized_return: minimumAnnualizedReturn / 100,
                  maximum_drawdown: maximumDrawdown / 100,
                  maximum_cash_streak_ratio: 1,
                  maximum_cash_streak_bars: maximumCashStreakBars,
                  minimum_profitable_fold_ratio:
                    RESEARCH_MINIMUM_PROFITABLE_FOLD_RATIO,
                  minimum_timing_positive_fold_ratio:
                    minimumTimingPositiveFoldRatio / 100,
                }
              : { parameters }),
          }),
        },
      );
      setDiscovery(null);
      setComparison(null);
      setResult(payload);
      setOptimization(payload.optimization ?? null);
      if (payload.optimization) {
        setParameters(payload.optimization.selected_parameters);
        const scope = currentParameterEvidenceScope(instrument);
        setParameterProvenance({
          kind: provenanceForOptimization(payload.optimization),
          ...(scope ? { scope } : {}),
        });
      }
      setResultInstrument(instrument);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "回测失败");
    } finally {
      setOperation(null);
    }
  }

  function defaultExperimentName(): string {
    if (!result) return "";
    const instrumentName = resultInstrument?.name ?? result.symbol;
    const intervalLabel =
      INTERVALS.find((item) => item.id === result.interval)?.label ?? result.interval;
    return `${instrumentName} · ${result.strategy.name} · ${intervalLabel}`;
  }

  function openSaveEditor() {
    if (!result) return;
    setExperimentName(defaultExperimentName());
    setExperimentNotes("");
    setExperimentNotice("");
    setShowSaveEditor(true);
  }

  function buildExperimentPayload(
    name: string,
    notes: string | null,
  ): ExperimentCreatePayload | null {
    if (!result || !resultInstrument || !name.trim()) return null;
    const runRequest: Record<string, unknown> = {
      symbol: resultInstrument.symbol,
      provider: resultInstrument.provider,
      strategy_id: result.strategy.id,
      interval: result.interval,
      start: startDate,
      end: endDate,
      parameters: result.strategy.parameters,
      ...(optimization
        ? {
            objective,
            train_ratio: 0.8,
            walk_forward_windows: 3,
            minimum_trades_per_year: minimumTradesPerYear,
            maximum_trades_per_year: maximumTradesPerYear,
            minimum_exposure: minimumExposure / 100,
            minimum_annualized_return: minimumAnnualizedReturn / 100,
            maximum_drawdown: maximumDrawdown / 100,
            maximum_cash_streak_ratio: 1,
            maximum_cash_streak_bars: maximumCashStreakBars,
            minimum_profitable_fold_ratio:
              RESEARCH_MINIMUM_PROFITABLE_FOLD_RATIO,
            minimum_timing_positive_fold_ratio:
              minimumTimingPositiveFoldRatio / 100,
          }
        : {}),
    };
    return {
      name: name.trim(),
      notes,
      instrument: resultInstrument,
      strategy: {
        id: result.strategy.id,
        name: result.strategy.name,
        category: result.strategy.category,
        parameters: result.strategy.parameters,
      },
      interval: result.interval,
      start: startDate,
      end: endDate,
      optimized: optimization !== null,
      run_request: runRequest,
      engine_config: result.result.config,
      data_metadata: result.data_metadata,
      metrics: result.result.metrics,
      benchmark_metrics: result.benchmark.result.metrics,
      exposure_matched_benchmark_metrics:
        result.exposure_matched_benchmark.result.metrics,
      comparison: result.comparison,
      timing_comparison: result.timing_comparison,
      diagnostics: result.diagnostics,
      validation: optimization
        ? {
            objective: optimization.objective,
            split_date: optimization.split_date,
            validation_passed: optimization.validation_passed,
            validation_code: optimization.validation_code,
            validation_reason: optimization.validation_reason,
            forward_observation_eligible:
              optimization.forward_observation_eligible,
            development_metrics: optimization.train_metrics,
            validation_metrics: optimization.validation_metrics,
            validation_benchmark_metrics:
              optimization.validation_benchmark_metrics,
            validation_exposure_matched_benchmark_metrics:
              optimization.validation_exposure_matched_benchmark_metrics,
          }
        : null,
      citations: result.citations,
    };
  }

  function buildCurrentExperimentSaveRequest(
    name: string,
    notes: string | null,
  ) {
    if (!result || !resultInstrument || !name.trim()) return null;
    return buildBacktestExperimentSaveRequest({
      run: result,
      name,
      notes,
      instrumentName: resultInstrument.name,
      legacyPayload:
        result.run_id === undefined ? buildExperimentPayload(name, notes) : null,
    });
  }

  async function saveExperiment() {
    if (!result || !resultInstrument || !experimentName.trim()) return;
    setExperimentOperation("save");
    setExperimentNotice("");
    try {
      const request = buildCurrentExperimentSaveRequest(
        experimentName,
        experimentNotes.trim() || null,
      );
      if (!request) return;
      const created = await apiFetch<ExperimentRecord>(request.path, {
        method: "POST",
        body: JSON.stringify(request.body),
      });
      setExperiments((items) => [
        created,
        ...items.filter((item) => item.id !== created.id),
      ]);
      setShowSaveEditor(false);
      setShowExperimentLibrary(true);
      setExperimentNotice(
        request.provenance === "server_verified"
          ? "服务端已核验本次运行回执并保存到 NAS，可随时比较或载入同一配置。"
          : "旧版结果已兼容保存到 NAS，但它属于未经过服务端回执核验的历史快照。",
      );
    } catch (reason) {
      setExperimentNotice(reason instanceof Error ? reason.message : "保存实验失败");
    } finally {
      setExperimentOperation(null);
    }
  }

  async function saveAndTrackCurrent() {
    if (
      !result ||
      !resultInstrument ||
      !optimization ||
      !forwardObservationMode
    ) {
      return;
    }
    setExperimentOperation("track");
    setExperimentNotice("");
    let snapshot = currentForwardExperiment;
    try {
      if (!snapshot) {
        const quality = optimization.validation_trade_quality;
        const workflowLabel =
          forwardObservationMode === "validated"
            ? "一键前向验证"
            : "冻结参数前向观察；该候选仅因最终留出交易样本不足而未通过验证";
        const notes = quality
          ? `${workflowLabel}；最终留出 ${quality.closed_trades} 个已闭合独立决策周期，历史胜率 ${formatPercent(
              quality.win_rate,
            )}，Wilson 95% 区间 ${formatPercent(
              quality.win_rate_confidence_low,
            )}–${formatPercent(quality.win_rate_confidence_high)}。`
          : `${workflowLabel}；最终留出 ${optimization.validation_metrics.closed_trades} 个已闭合独立决策周期。`;
        const request = buildCurrentExperimentSaveRequest(
          defaultExperimentName(),
          notes,
        );
        if (!request) return;
        const created = await apiFetch<ExperimentRecord>(request.path, {
          method: "POST",
          body: JSON.stringify(request.body),
        });
        snapshot = created;
        setForwardResult(result);
        setForwardExperiment(created);
        setExperiments((items) => [
          created,
          ...items.filter((item) => item.id !== created.id),
        ]);
      }
      const track = await apiFetch<PaperTrack>("/api/v1/paper-tracks", {
        method: "POST",
        body: JSON.stringify({ experiment_id: snapshot.id }),
      });
      setForwardTrack(track);
      const provenancePrefix =
        snapshot.provenance_status === "server_verified"
          ? "服务端运行回执已核验；"
          : "该档案属于旧版未验证快照；";
      setExperimentNotice(
        forwardObservationMode === "validated"
          ? `${provenancePrefix}「${snapshot.name}」已保存并开始纸面跟踪；首个真实信号快照已生成，不会连接账户或下单。`
          : `${provenancePrefix}「${snapshot.name}」的参数已冻结并开始前向积累；它不是已验证冠军，只能由后续新增 K 线补充证据。`,
      );
    } catch (reason) {
      const message =
        reason instanceof Error ? reason.message : "启动纸面跟踪失败";
      if (snapshot && message.includes("已经在纸面跟踪中")) {
        try {
          const tracks = await apiFetch<PaperTrack[]>("/api/v1/paper-tracks");
          const existing = tracks.find(
            (item) => item.experiment.id === snapshot?.id,
          );
          if (existing) {
            setForwardResult(result);
            setForwardTrack(existing);
            setExperimentNotice(`「${snapshot.name}」已经在纸面跟踪中。`);
            return;
          }
        } catch {
          // Keep the original conflict message if the recovery lookup fails.
        }
      }
      setExperimentNotice(
        snapshot
          ? `实验快照已安全保存，但纸面跟踪未启动：${message}。可直接重试，不会重复保存。`
          : message,
      );
    } finally {
      setExperimentOperation(null);
    }
  }

  function loadExperiment(experiment: ExperimentRecord) {
    const instrumentChanged =
      !selectedInstrument ||
      selectedInstrument.provider !== experiment.instrument.provider ||
      selectedInstrument.symbol.toUpperCase() !==
        experiment.instrument.symbol.toUpperCase();
    if (instrumentChanged) {
      setMarketCapabilities(null);
      setMarketCapabilitiesError("");
      setMarketCapabilitiesStatus("loading");
    }
    setSelectedInstrument(experiment.instrument);
    setSymbolQuery(instrumentLabel(experiment.instrument));
    setStrategyId(experiment.strategy.id);
    setParameters({ ...experiment.strategy.parameters });
    setInterval(experiment.interval);
    setStartDate(experiment.start);
    setEndDate(experiment.end);
    setPeriod("custom");
    if (experiment.validation) setObjective(experiment.validation.objective);
    const request = experiment.run_request;
    setParameterProvenance({
      kind: experiment.validation
        ? experiment.validation.validation_passed
          ? "validated_holdout"
          : experiment.validation.forward_observation_eligible
            ? "provisional_holdout"
            : "optimized_unverified"
        : experiment.optimized
          ? "optimized_unverified"
          : "experiment_snapshot",
      scope: {
        symbol: experiment.instrument.symbol.toUpperCase(),
        provider: experiment.instrument.provider,
        interval: experiment.interval,
        start: experiment.start,
        end: experiment.end,
        objective: experiment.validation?.objective ?? objective,
        minimumTradesPerYear:
          typeof request.minimum_trades_per_year === "number"
            ? request.minimum_trades_per_year
            : minimumTradesPerYear,
        maximumTradesPerYear:
          typeof request.maximum_trades_per_year === "number"
            ? request.maximum_trades_per_year
            : maximumTradesPerYear,
        minimumExposure:
          typeof request.minimum_exposure === "number"
            ? request.minimum_exposure * 100
            : minimumExposure,
        minimumAnnualizedReturn:
          typeof request.minimum_annualized_return === "number"
            ? request.minimum_annualized_return * 100
            : minimumAnnualizedReturn,
        maximumDrawdown:
          typeof request.maximum_drawdown === "number"
            ? request.maximum_drawdown * 100
            : maximumDrawdown,
        maximumCashStreakBars:
          typeof request.maximum_cash_streak_bars === "number"
            ? request.maximum_cash_streak_bars
            : maximumCashStreakBars,
        minimumTimingPositiveFoldRatio:
          typeof request.minimum_timing_positive_fold_ratio === "number"
            ? request.minimum_timing_positive_fold_ratio * 100
            : minimumTimingPositiveFoldRatio,
      },
    });
    if (typeof request.minimum_trades_per_year === "number") {
      setMinimumTradesPerYear(request.minimum_trades_per_year);
    }
    if (typeof request.maximum_trades_per_year === "number") {
      setMaximumTradesPerYear(request.maximum_trades_per_year);
    }
    if (typeof request.minimum_exposure === "number") {
      setMinimumExposure(request.minimum_exposure * 100);
    }
    if (typeof request.minimum_annualized_return === "number") {
      setMinimumAnnualizedReturn(request.minimum_annualized_return * 100);
    }
    if (typeof request.maximum_drawdown === "number") {
      setMaximumDrawdown(request.maximum_drawdown * 100);
    }
    if (typeof request.maximum_cash_streak_bars === "number") {
      setMaximumCashStreakBars(request.maximum_cash_streak_bars);
    }
    if (typeof request.minimum_timing_positive_fold_ratio === "number") {
      setMinimumTimingPositiveFoldRatio(
        request.minimum_timing_positive_fold_ratio * 100,
      );
    }
    setDiscovery(null);
    setComparison(null);
    setResult(null);
    setOptimization(null);
    setShowExperimentLibrary(false);
    setExperimentNotice(`已载入「${experiment.name}」；点击运行即可用最新数据复现。`);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function deleteExperiment(experiment: ExperimentRecord) {
    if (!window.confirm(`删除实验「${experiment.name}」？此操作不可撤销。`)) return;
    setExperimentOperation("delete");
    try {
      await apiFetch<void>(`/api/v1/experiments/${experiment.id}`, {
        method: "DELETE",
      });
      setExperiments((items) => items.filter((item) => item.id !== experiment.id));
      setExperimentNotice("实验记录已删除。");
    } catch (reason) {
      setExperimentNotice(reason instanceof Error ? reason.message : "删除实验失败");
    } finally {
      setExperimentOperation(null);
    }
  }

  async function trackExperiment(experiment: ExperimentRecord) {
    if (
      !experiment.validation?.validation_passed &&
      !experiment.validation?.forward_observation_eligible
    ) {
      setExperimentNotice("该实验没有通过样本外验证，也不满足冻结参数前向观察条件。");
      return;
    }
    setExperimentOperation("track");
    setExperimentNotice("");
    try {
      await apiFetch("/api/v1/paper-tracks", {
        method: "POST",
        body: JSON.stringify({ experiment_id: experiment.id }),
      });
      setExperimentNotice(
        `「${experiment.name}」已加入纸面跟踪并生成首个真实信号快照；不会连接账户或下单。`,
      );
    } catch (reason) {
      setExperimentNotice(
        reason instanceof Error ? reason.message : "加入纸面跟踪失败",
      );
    } finally {
      setExperimentOperation(null);
    }
  }

  return (
    <div className="page">
      <header className="topbar">
        <div>
          <span className="eyebrow">Backtest lab</span>
          <h1>回测工作台</h1>
        </div>
        <div className="backtest-top-actions">
          <span className="market-status">
            <i /> 信号延后一根 K 线 · 开盘成交
          </span>
          <button
            className={showExperimentLibrary ? "experiment-toggle active" : "experiment-toggle"}
            onClick={() => setShowExperimentLibrary((value) => !value)}
            type="button"
          >
            实验档案 <b>{experiments.length}</b>
          </button>
        </div>
      </header>

      {experimentNotice && (
        <div className="experiment-notice">
          <span>{experimentNotice}</span>
          {currentForwardTrack && <a href="/tracking">查看纸面跟踪 →</a>}
        </div>
      )}
      {showExperimentLibrary && (
        <ExperimentLibrary
          busy={experimentOperation !== null}
          experiments={experiments}
          onDelete={(experiment) => void deleteExperiment(experiment)}
          onLoad={loadExperiment}
          onTrack={(experiment) => void trackExperiment(experiment)}
        />
      )}

      <div className="workbench-grid">
        <aside className="control-panel panel">
          <div className="panel-number">01 / INPUT</div>
          <h2>定义实验</h2>
          <p className="muted">选择数据标的与审计过的策略模板。费用与滑点已计入。</p>
          <form onSubmit={run}>
            <SymbolPicker
              label="股票 / ETF / 指数 / 外汇 / 加密 / 期货"
              onQueryChange={(value) => {
                setSymbolQuery(value);
                setDiscovery(null);
              }}
              onSelect={(instrument) => {
                setSelectedInstrument(instrument);
                setMarketCapabilities(null);
                setMarketCapabilitiesError("");
                setMarketCapabilitiesStatus(instrument ? "loading" : "idle");
                setDiscovery(null);
                setComparison(null);
                setRobustness(null);
                setResult(null);
                setOptimization(null);
                if (!supportedIntervals(instrument).includes(interval)) {
                  chooseInterval("1d");
                }
              }}
              query={symbolQuery}
              selected={selectedInstrument}
            />
            {selectedInstrument?.provider === "macro" && (
              <div className="reference-data-notice" role="note">
                <strong>官方参考序列 · 仅日线 / 周线</strong>
                <span>
                  指数使用 Nasdaq / Cboe 官方日线，外汇使用 ECB 每日参考汇率。指数本身
                  不可直接交易，外汇源不含可成交开高低；结果需用具体 ETF、期货或券商报价复核。
                </span>
              </div>
            )}
            <label>
              策略模板
              <select
                disabled={!strategyCatalogReady}
                onChange={(event) => {
                  const nextId = event.target.value;
                  setStrategyId(nextId);
                  setParameters({
                    ...(strategies.find((strategy) => strategy.id === nextId)?.parameters ?? {}),
                  });
                  setParameterProvenance({ kind: "template_default" });
                  setDiscovery(null);
                  setOptimization(null);
                  setRobustness(null);
                  setResult(null);
                }}
                value={strategyId}
              >
                {Array.from(
                  strategies.reduce<Map<string, Strategy[]>>((groups, strategy) => {
                    groups.set(strategy.category, [...(groups.get(strategy.category) ?? []), strategy]);
                    return groups;
                  }, new Map()),
                ).map(([category, items]) => (
                  <optgroup key={category} label={category}>
                    {items.map((strategy) => (
                      <option key={strategy.id} value={strategy.id}>
                        {strategy.name}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </label>
            {strategyCatalogStatus === "loading" && (
              <p className="notice-banner" role="status">
                正在加载策略目录；完成前不会启用回测、寻优或策略发现。
              </p>
            )}
            {strategyCatalogStatus === "error" && (
              <div className="error-banner" role="alert">
                <strong>策略目录加载失败：</strong>
                {strategyCatalogError || "无法读取策略目录"}
                。当前不会使用空目录或默认参数继续运行。
                <br />
                <button
                  className="secondary-button full"
                  onClick={retryStrategyCatalog}
                  type="button"
                >
                  重试策略目录
                </button>
              </div>
            )}
            {selectedStrategy && (
              <div className="strategy-guidance">
                <div>
                  <span>{selectedStrategy.category}</span>
                  <strong>{selectedStrategy.description}</strong>
                </div>
                <p>
                  <b>适合：</b>
                  {selectedStrategy.best_for || "需要结合标的与周期验证"}
                </p>
                <p>
                  <b>主要风险：</b>
                  {selectedStrategy.risk_note || "历史参数不保证未来有效"}
                </p>
                <p
                  className={
                    selectedStrategy.recommended_intervals.includes(interval)
                      ? "interval-guidance recommended"
                      : "interval-guidance fallback"
                  }
                >
                  <b>周期适配：</b>
                  {selectedStrategy.recommended_intervals.includes(interval)
                    ? `当前 ${INTERVALS.find((item) => item.id === interval)?.label ?? interval} K 线属于此模板的建议研究周期。`
                    : `当前 ${INTERVALS.find((item) => item.id === interval)?.label ?? interval} K 线不在建议周期（${selectedStrategy.recommended_intervals.join(" / ")}）；仍可手动研究，但自动发现会优先尝试更适配的模板。`}
                </p>
                <p>
                  <b>参数定位：</b>
                  当前数值只是可解释的模板起点，不是“最佳参数”。需要选参时，请使用“只优化当前模板”或“一键发现并验证策略”；两者都会把最终留出期排除在选参之外。
                </p>
                <p>
                  <b>指标预热：</b>
                  {selectedStrategy.warmup_bars > 0
                    ? `至少 ${selectedStrategy.warmup_bars} 根当前周期 K 线；系统会从开始日期之前单独获取。`
                    : "该模板不需要历史指标预热。"}
                </p>
              </div>
            )}
            <fieldset className="timeframe-controls">
              <legend>K 线周期 · 决定信号与交易频率</legend>
              {!selectedInstrument && (
                <p className="notice-banner" role="status">
                  请先从搜索结果中选择标的；选择后系统会确认可用 K 线周期与回看范围。
                </p>
              )}
              {selectedInstrument && marketCapabilitiesStatus === "loading" && (
                <p className="notice-banner" role="status">
                  正在确认 {selectedInstrument.name}（{selectedInstrument.symbol}
                  ）的行情周期与回看能力…
                </p>
              )}
              {selectedInstrument && marketCapabilitiesStatus === "error" && (
                <div className="error-banner" role="alert">
                  <strong>行情能力未知：</strong>
                  {marketCapabilitiesError || "无法确认所选标的的行情能力"}
                  。重新确认前不会启动研究，标的搜索仍可继续使用。
                  <br />
                  <button
                    className="secondary-button full"
                    onClick={retryMarketCapabilities}
                    type="button"
                  >
                    重新确认行情能力
                  </button>
                </div>
              )}
              <div className="timeframe-switcher">
                {INTERVALS.map((item) => {
                  const supported = availableIntervals.includes(item.id);
                  return (
                    <button
                      className={interval === item.id ? "active" : ""}
                      disabled={!supported}
                      key={item.id}
                      onClick={() => chooseInterval(item.id)}
                      title={supported ? item.detail : "当前数据源暂不支持"}
                      type="button"
                    >
                      <strong>{item.label}</strong>
                      <span>{item.detail}</span>
                    </button>
                  );
                })}
              </div>
              <p className="field-help">
                参数里的“周期”均指当前 K 线根数；例如日线 20 表示约 20
                个交易日，15 分钟线 20 表示约 5 小时。
              </p>
              {intervalStructureNote(selectedInstrument, interval) && (
                <p className="interval-structure-note">
                  {intervalStructureNote(selectedInstrument, interval)}
                </p>
              )}
              {selectedIntervalCapability && (
                <p
                  className={`history-window-guidance ${
                    historyWindowExceeded ? "exceeded" : "available"
                  }`}
                  role={historyWindowExceeded ? "status" : undefined}
                >
                  {maximumHistoryDays === null
                    ? "当前数据源未设置 QuantSieve 回看天数上限；服务端仍会按实际可返回历史复核。"
                    : historyWindowExceeded
                      ? `当前请求含 ${requestedRangeDays} 个日历日，超过该数据源 ${INTERVALS.find((item) => item.id === interval)?.label ?? interval} 最多 ${maximumHistoryDays} 个日历日的回看上限；请缩短区间或切换更大周期。`
                      : `当前数据源的 ${INTERVALS.find((item) => item.id === interval)?.label ?? interval} 最多可回看 ${maximumHistoryDays} 个日历日；本次请求 ${requestedRangeDays} 个。`}
                </p>
              )}
            </fieldset>
            <fieldset className="date-controls">
              <legend>回测区间</legend>
              <div className="period-switcher">
                {presets(interval, selectedInstrument).map((item) => {
                  const itemHorizon = discoveryHorizonStatus(
                    interval,
                    item.days,
                    discoveryHorizonRequirements,
                  );
                  return (
                    <button
                      className={`${period === item.id ? "active" : ""} ${
                        itemHorizon.validationEligible ? "" : "exploration"
                      }`}
                      key={item.id}
                      onClick={() => choosePeriod(item.id, item.days)}
                      title={
                        itemHorizon.validationEligible
                          ? "区间长度满足策略发现的最低研究覆盖度。"
                          : `仅探索：策略发现至少需要 ${itemHorizon.fullSampleDays} 天完整样本。`
                      }
                      type="button"
                    >
                      <span>{item.label}</span>
                      {!itemHorizon.validationEligible && <small>探索</small>}
                    </button>
                  );
                })}
              </div>
              <div className="date-grid">
                <label>
                  开始
                  <input
                    max={endDate}
                    onChange={(event) => {
                      setStartDate(event.target.value);
                      setPeriod("custom");
                      setDiscovery(null);
                      setComparison(null);
                      setResult(null);
                      setOptimization(null);
                    }}
                    type="date"
                    value={startDate}
                  />
                </label>
                <label>
                  结束
                  <input
                    max={isoDate(new Date())}
                    min={startDate}
                    onChange={(event) => {
                      setEndDate(event.target.value);
                      setPeriod("custom");
                      setDiscovery(null);
                      setComparison(null);
                      setResult(null);
                      setOptimization(null);
                    }}
                    type="date"
                    value={endDate}
                  />
                </label>
              </div>
              <div
                className={`discovery-range-readiness ${
                  plannedHorizon.validationEligible ? "ready" : "exploration"
                }`}
                role={plannedHorizon.validationEligible ? undefined : "status"}
              >
                <strong>
                  {plannedHorizon.validationEligible
                    ? "策略发现可进行最终验证"
                    : "当前区间仅用于探索"}
                </strong>
                <span>
                  完整样本计划 {plannedRangeDays} / {plannedHorizon.fullSampleDays} 天
                  · 最终留出至少 {plannedHorizon.holdoutDays} 天
                </span>
                <small>
                  {plannedHorizon.validationEligible
                    ? "服务端仍会根据实际返回 K 线的首尾时间复核覆盖度。"
                    : "可以比较策略和查看信号，但不会产生已验证冠军，也不能启动前向观察。"}
                </small>
              </div>
            </fieldset>
            <fieldset className="optimization-controls">
              <legend>策略发现与参数寻优</legend>
              <div className="research-profile-picker">
                <div className="research-profile-heading">
                  <span>先定义研究目标</span>
                  <small>预设会同步下方可调门槛；固定验证纪律单独列出。</small>
                </div>
                <div className="research-profile-options" role="group" aria-label="研究目标预设">
                  {RESEARCH_PROFILES.map((profile) => (
                    <button
                      aria-pressed={activeResearchProfile === profile.id}
                      className={activeResearchProfile === profile.id ? "active" : ""}
                      key={profile.id}
                      onClick={() => applyResearchProfile(profile)}
                      type="button"
                    >
                      <strong>{profile.label}</strong>
                      <small>{profile.detail}</small>
                    </button>
                  ))}
                </div>
                <p
                  className={`research-profile-status ${
                    activeResearchProfile ? "preset" : "custom"
                  }`}
                  role="status"
                >
                  {activeResearchProfile ? (
                    <>
                      当前研究合同：<strong>
                        {RESEARCH_PROFILES.find(
                          (profile) => profile.id === activeResearchProfile,
                        )?.label}
                      </strong>
                      预设。所有目标与约束仍可在下方核对。
                    </>
                  ) : (
                    <>
                      当前研究合同：<strong>自定义</strong>。它不等同于任一预设；
                      系统将按下方显示的完整目标、风险、频率与参与度约束筛选，不会悄悄套用预设。
                    </>
                  )}
                </p>
              </div>
              <label>
                优化目标
                <select
                  onChange={(event) =>
                    {
                      setObjective(event.target.value as OptimizationObjective);
                      setDiscovery(null);
                      setOptimization(null);
                      setResult(null);
                    }
                  }
                  value={objective}
                >
                  <option value="balanced">收益 / 回撤平衡</option>
                  <option value="total_return">最大化累计收益</option>
                  <option value="sharpe_ratio">最大化夏普比率</option>
                  <option value="drawdown_control">优先控制最大回撤</option>
                </select>
              </label>
              <p>
                收益目标与回撤预算是硬门槛；前 80% 做 3 段走步验证，最后 20%
                完全留出。每段以策略平均持仓率作为首根开盘的初始投入比例，随后让被动基准固定份额持有，避免长期空仓被误判为择时能力。
              </p>
              <div className="optimization-guardrails">
                <label className="target-guardrail">
                  最低目标年化 %
                  <input
                    max="1000"
                    min="-100"
                    onChange={(event) => {
                      setMinimumAnnualizedReturn(Number(event.target.value));
                      setDiscovery(null);
                      setOptimization(null);
                      setResult(null);
                    }}
                    step="1"
                    type="number"
                    value={minimumAnnualizedReturn}
                  />
                  <small>开发期与最终留出均需达到</small>
                </label>
                <label className="target-guardrail">
                  最大可接受回撤 %
                  <input
                    max="100"
                    min="1"
                    onChange={(event) => {
                      setMaximumDrawdown(Number(event.target.value));
                      setDiscovery(null);
                      setOptimization(null);
                      setResult(null);
                    }}
                    step="1"
                    type="number"
                    value={maximumDrawdown}
                  />
                  <small>按回撤绝对值限制</small>
                </label>
                <label>
                  每年最低仓位变动次数
                  <input
                    max="100"
                    min="1"
                    onChange={(event) => {
                      setMinimumTradesPerYear(Number(event.target.value));
                      setDiscovery(null);
                      setOptimization(null);
                      setResult(null);
                    }}
                    step="1"
                    type="number"
                    value={minimumTradesPerYear}
                  />
                </label>
                <label>
                  每年最高仓位变动次数
                  <input
                    max="10000"
                    min={minimumTradesPerYear}
                    onChange={(event) => {
                      setMaximumTradesPerYear(Number(event.target.value));
                      setDiscovery(null);
                      setOptimization(null);
                      setResult(null);
                    }}
                    step="1"
                    type="number"
                    value={maximumTradesPerYear}
                  />
                  <small>
                    仓位变动上限；按当前 0.03% 费用 + 0.02% 滑点、每次满仓变动估算，年度单边摩擦上限约
                    {maximumFrequencyFrictionCeiling(maximumTradesPerYear)}。切换到短周期时，默认上限按不超过 20% 年度理论摩擦预算推导；你仍可根据真实费率和仓位修改，发现流程还会单独做成本压力测试。
                  </small>
                </label>
                <label>
                  最低持仓率 %
                  <input
                    max="100"
                    min="0"
                    onChange={(event) => {
                      setMinimumExposure(Number(event.target.value));
                      setDiscovery(null);
                      setOptimization(null);
                      setResult(null);
                    }}
                    step="1"
                    type="number"
                    value={minimumExposure}
                  />
                </label>
                <label>
                  最多连续空仓 K 线
                  <input
                    max="100000"
                    min="1"
                    onChange={(event) => {
                      setMaximumCashStreakBars(Number(event.target.value));
                      setDiscovery(null);
                      setOptimization(null);
                      setResult(null);
                    }}
                    step="1"
                    type="number"
                    value={maximumCashStreakBars}
                  />
                  <small>{barsAsTime(maximumCashStreakBars, interval)}</small>
                </label>
                <label>
                  最低择时有效窗口 %
                  <input
                    max="100"
                    min="0"
                    onChange={(event) => {
                      setMinimumTimingPositiveFoldRatio(
                        Number(event.target.value),
                      );
                      setDiscovery(null);
                      setOptimization(null);
                      setResult(null);
                    }}
                    step="1"
                    type="number"
                    value={minimumTimingPositiveFoldRatio}
                  />
                  <small>3 段中至少过半跑赢初始投入匹配基准</small>
                </label>
              </div>
              <p className="research-profile-status preset" role="note">
                <strong>固定验证纪律（不可调）：</strong>3 段开发验证中至少{" "}
                {RESEARCH_MINIMUM_PROFITABLE_FOLD_RATIO * 100}% 的窗口必须盈利。
                此项不随研究预设或手动门槛调整，避免把只在少数区间获利的参数包装成稳定候选。
              </p>
              {researchContractError && (
                <p className="error-banner research-contract-error" role="alert">
                  研究合同无法运行：{researchContractError}
                </p>
              )}
              <button
                className="discovery-button full"
                disabled={
                  operation !== null ||
                  !symbolQuery.trim() ||
                  !researchPrerequisitesReady ||
                  Boolean(researchContractError)
                }
                onClick={() => void discoverStrategies()}
                type="button"
              >
                <span>
                  {operation === "discover"
                    ? `正在初筛 ${strategies.filter(
                        (item) =>
                          !["buy-hold", "constant-allocation"].includes(item.id),
                      ).length} 个模板并验证候选…`
                    : "一键发现并验证策略"}
                </span>
                <small>开发集初筛 · 走步验证 · 最终 20% 留出</small>
              </button>
              <button
                className="secondary-button full optimize-current-button"
                disabled={
                  operation !== null ||
                  !researchPrerequisitesReady ||
                  ["buy-hold", "constant-allocation"].includes(strategyId) ||
                  Boolean(researchContractError)
                }
                onClick={() => void optimize()}
                type="button"
              >
                {operation === "optimize" ? "正在搜索与验证…" : "只优化当前模板"}
              </button>
              <div className="cross-market-study">
                <div className="cross-market-study-heading">
                  <div>
                    <span className="eyebrow">CROSS-MARKET REPLICATION</span>
                    <strong>跨市场复现策略族</strong>
                  </div>
                  <small>2–4 个市场</small>
                </div>
                <p>
                  当前标的会自动纳入。再添加 1–3 个股票、加密或期货标的；每个市场独立选参、走步验证和最终留出，不能把结果理解为一组万能参数。
                </p>
                <div className="cross-market-targets" aria-label="已选跨市场标的">
                  <span className="cross-market-primary">
                    当前：{selectedInstrument?.symbol || symbolQuery || "待选择"}
                  </span>
                  {robustnessTargets.map((instrument) => (
                    <button
                      key={`${instrument.provider}-${instrument.symbol}`}
                      onClick={() => removeRobustnessTarget(instrument)}
                      title={`移除 ${instrument.name}`}
                      type="button"
                    >
                      {instrument.symbol} ×
                    </button>
                  ))}
                </div>
                {robustnessTargets.length < 3 && (
                  <SymbolPicker
                    label="添加不同市场的标的"
                    onQueryChange={setRobustnessQuery}
                    onSelect={addRobustnessTarget}
                    placeholder="搜索名称、代码或交易对，如 SPY / BTC/USDT / 原油"
                    query={robustnessQuery}
                    selected={null}
                  />
                )}
                <button
                  className="secondary-button full cross-market-button"
                  disabled={
                    operation !== null ||
                    !researchPrerequisitesReady ||
                    robustnessTargets.length < 1 ||
                    ["buy-hold", "constant-allocation"].includes(strategyId) ||
                    Boolean(researchContractError)
                  }
                  onClick={() => void validateAcrossMarkets()}
                  type="button"
                >
                  {operation === "robustness"
                    ? "正在逐市场复现与验证…"
                    : `复现当前策略于 ${robustnessTargets.length + 1} 个市场`}
                </button>
              </div>
            </fieldset>
            <fieldset className="parameter-controls">
              <legend>策略参数</legend>
              {strategyCatalogReady && (
                <div
                  className={`parameter-provenance ${parameterProvenance.kind} ${
                    parameterEvidenceIsCurrent ? "current" : "stale"
                  }`}
                  role={parameterEvidenceIsCurrent ? "note" : "status"}
                >
                  <strong>{parameterProvenanceCopy.label}</strong>
                  <span>
                    {parameterEvidenceIsCurrent
                      ? parameterProvenanceCopy.detail
                      : "参数源于一项历史验证，但当前标的、K 线周期、区间、目标或约束已变化；必须重新寻优和验证。"}
                  </span>
                </div>
              )}
              {!strategyCatalogReady ? (
                <p>策略目录确认后才会显示可运行参数。</p>
              ) : Object.entries(parameters).length === 0 ? (
                <p>该策略没有可调参数。</p>
              ) : (
                <div className="parameter-grid">
                  {Object.entries(parameters).map(([key, value]) => (
                    <label key={key}>
                      {PARAMETER_LABELS[key] ?? key}
                      <input
                        max={
                          ["allocation", "defensive_exposure"].includes(key)
                            ? 1
                            : undefined
                        }
                        min={
                          ["allocation", "defensive_exposure"].includes(key)
                            ? 0
                            : INTEGER_PARAMETER_KEYS.has(key)
                              ? 1
                            : undefined
                        }
                        onChange={(event) => {
                          setParameters((items) => ({
                            ...items,
                            [key]: Number(event.target.value),
                          }));
                          setParameterProvenance({ kind: "manual" });
                          setDiscovery(null);
                          setOptimization(null);
                          setResult(null);
                        }}
                        step={
                          ["allocation", "defensive_exposure"].includes(key)
                            ? 0.01
                            : INTEGER_PARAMETER_KEYS.has(key)
                              ? 1
                            : "any"
                        }
                        type="number"
                        value={value}
                      />
                      <small>
                        {["allocation", "defensive_exposure"].includes(key)
                          ? `${formatPercent(value)} · ${key}`
                          : PARAMETER_HINTS[key] ?? key}
                      </small>
                    </label>
                  ))}
                </div>
              )}
              {parameterValidationError && (
                <p className="parameter-validation-error" role="alert">
                  当前参数无法运行：{parameterValidationError}
                </p>
              )}
            </fieldset>
            <div className="execution-note">
              <span>{INTERVALS.find((item) => item.id === interval)?.label} K 线</span>
              <span>费用 0.03%</span>
              <span>滑点 0.02%</span>
              <span>下一根 K 线开盘执行</span>
            </div>
            <button
              className="secondary-button full compare-button"
              disabled={
                operation !== null ||
                !symbolQuery.trim() ||
                !researchPrerequisitesReady
              }
              onClick={() => void compareStrategies()}
              type="button"
            >
              {operation === "compare"
                ? `正在读取 ${strategies.length} 个模板的默认参数快照…`
                : "查看模板默认参数快照"}
            </button>
            <button
              className="primary-button full"
              disabled={
                operation !== null ||
                !symbolQuery.trim() ||
                !researchPrerequisitesReady ||
                Boolean(parameterValidationError)
              }
              type="submit"
            >
              {operation === "backtest" ? "正在取数与计算…" : "仅回测当前参数 ↗"}
            </button>
            <p className="field-help manual-parameter-note">
              此操作不会自动调参；用于复现、对照或验证你手动输入的参数。若希望系统按收益、回撤与交易频率自动筛选，请使用上方的寻优功能。
            </p>
          </form>
          {error && <p className="error-banner">{error}</p>}
        </aside>

        <section className="results-panel panel">
          <div className="panel-number">02 / EVIDENCE</div>
          {discovery && (
            <StrategyDiscovery
              guardrails={{
                minimumTradesPerYear,
                maximumTradesPerYear,
                minimumExposure,
                minimumAnnualizedReturn,
                maximumDrawdown,
                maximumCashStreakBars,
              }}
              onSelectBaseline={(backtest) => {
                setStrategyId(backtest.strategy.id);
                setParameters({ ...backtest.strategy.parameters });
                setParameterProvenance({ kind: "passive_baseline" });
                setOptimization(null);
                setResult(backtest);
                setComparison(null);
              }}
              onSelect={(candidate) => loadDiscoveryCandidate(candidate)}
              payload={discovery}
            />
          )}
          {comparison && (
            <StrategyLeaderboard
              onSelect={(strategy) => {
                setStrategyId(strategy.id);
                setParameters({ ...strategy.parameters });
                setParameterProvenance({ kind: "template_default" });
                setOptimization(null);
                setResult(null);
              }}
              payload={comparison}
            />
          )}
          {robustness && (
            <section
              className={`cross-market-result ${robustness.research_decision.status}`}
            >
              <div className="cross-market-result-heading">
                <div>
                  <span className="eyebrow">CROSS-MARKET EVIDENCE</span>
                  <h2>{robustness.research_decision.title}</h2>
                  <p>{robustness.research_decision.reason}</p>
                </div>
                <strong>
                  {robustness.summary.validated} / {robustness.summary.requested} 通过
                </strong>
              </div>
              <p className="cross-market-policy">{robustness.parameter_policy_note}</p>
              {robustness.run_id ? (
                <div className="cross-market-archive-controls">
                  <label>
                    归档名称
                    <input
                      maxLength={100}
                      onChange={(event) => setCrossMarketArchiveName(event.target.value)}
                      value={crossMarketArchiveName}
                    />
                  </label>
                  <label>
                    备注（可选）
                    <input
                      maxLength={1000}
                      onChange={(event) => setCrossMarketArchiveNotes(event.target.value)}
                      value={crossMarketArchiveNotes}
                    />
                  </label>
                  <button
                    className="secondary-button"
                    disabled={
                      experimentOperation !== null || !crossMarketArchiveName.trim()
                    }
                    onClick={() => void saveCrossMarketExperiment()}
                    type="button"
                  >
                    {experimentOperation === "cross-save"
                      ? "正在按回执归档…"
                      : "保存跨市场证据"}
                  </button>
                  <small>
                    回执有效至 {robustness.run_expires_at?.slice(0, 16).replace("T", " ")}
                  </small>
                </div>
              ) : (
                <p className="cross-market-archived-note">这是已归档的不可变证据快照。</p>
              )}
              <div className="cross-market-results-grid">
                {robustness.markets.map((market) => {
                  const evidence = market.evidence;
                  const optimization = evidence?.optimization;
                  const finalization = evidence
                    ? barFinalizationSummary(evidence.data_metadata)
                    : null;
                  return (
                    <article
                      className={
                        market.status === "unavailable"
                          ? "unavailable"
                          : market.validation_passed
                            ? "passed"
                            : market.forward_observation_eligible
                              ? "provisional"
                              : "rejected"
                      }
                      key={`${market.provider}-${market.symbol}`}
                    >
                      <div>
                        <span>{market.provider}</span>
                        <h3>{market.symbol}</h3>
                        <strong>
                          {market.status === "unavailable"
                            ? "数据不可用"
                            : market.validation_passed
                              ? "留出验证通过"
                              : market.forward_observation_eligible
                                ? "仅可冻结前向观察"
                                : "留出验证未通过"}
                        </strong>
                      </div>
                      {evidence && optimization ? (
                        <>
                          <div className="cross-market-data-evidence">
                            <span>
                              {evidence.data_metadata.fallback
                                ? "已使用数据回退来源"
                                : "主数据来源"}
                              {typeof evidence.data_metadata.bars === "number"
                                ? ` · ${formatCompact(evidence.data_metadata.bars)} 根 K 线`
                                : ""}
                            </span>
                            {finalization && <span>{finalization}</span>}
                            <div className="source-row">
                              {evidence.citations.map((citation, index) => (
                                <SourceBadge
                                  citation={citation}
                                  key={`${citation.source}-${index}`}
                                />
                              ))}
                            </div>
                          </div>
                          <dl>
                            <div>
                              <dt>留出收益</dt>
                              <dd>{formatPercent(optimization.validation_metrics.total_return)}</dd>
                            </div>
                            <div>
                              <dt>同持仓基准超额</dt>
                              <dd>
                                {formatPercent(optimization.validation_timing_excess_return)}
                              </dd>
                            </div>
                            <div>
                              <dt>留出回撤</dt>
                              <dd>{formatPercent(optimization.validation_metrics.max_drawdown)}</dd>
                            </div>
                            <div>
                              <dt>留出闭合周期</dt>
                              <dd>
                                {optimization.validation_metrics.closed_trades} / {robustness.final_holdout_min_closed_trades} 个
                              </dd>
                            </div>
                            <div>
                              <dt>成本压力</dt>
                              <dd>
                                {optimization.cost_stress_passed ? "通过" : "未通过"}
                              </dd>
                            </div>
                            <div>
                              <dt>已选参数</dt>
                              <dd className="parameters">
                                {Object.entries(optimization.selected_parameters)
                                  .map(([key, value]) => `${key}=${value}`)
                                  .join(" · ")}
                              </dd>
                            </div>
                            <div>
                              <dt>开发期验证</dt>
                              <dd>
                                {optimization.walk_forward_method ===
                                "expanding_window_reoptimization"
                                  ? `扩展窗口再寻优 · ${optimization.walk_forward_folds.length} 窗`
                                  : "历史快照未记录逐窗选参方法"}
                              </dd>
                            </div>
                          </dl>
                          <p>{market.validation_reason}</p>
                        </>
                      ) : (
                        <p>{market.validation_reason}</p>
                      )}
                    </article>
                  );
                })}
              </div>
              {crossMarketExperiments.length > 0 && (
                <div className="cross-market-archive-list">
                  <div>
                    <span className="eyebrow">SAVED CROSS-MARKET STUDIES</span>
                    <strong>已归档 {crossMarketExperiments.length} 组跨市场证据</strong>
                  </div>
                  {crossMarketExperiments.slice(0, 5).map((experiment) => (
                    <article key={experiment.id}>
                      <button
                        onClick={() => setRobustness(experiment)}
                        type="button"
                      >
                        <span>{experiment.strategy.name}</span>
                        <strong>{experiment.name}</strong>
                        <small>
                          {experiment.markets.map((market) => market.symbol).join(" / ")}
                        </small>
                      </button>
                      <button
                        aria-label={`删除 ${experiment.name}`}
                        className="cross-market-delete"
                        disabled={experimentOperation !== null}
                        onClick={() => void deleteCrossMarketExperiment(experiment)}
                        type="button"
                      >
                        删除
                      </button>
                    </article>
                  ))}
                </div>
              )}
            </section>
          )}
          {!result ? (
            comparison || discovery ? null : (
              <div className="empty-state">
                <span>α</span>
                <h2>等待第一组实验</h2>
                <p>先统一比较全部模板，再对候选策略做参数寻优与留出验证。</p>
              </div>
            )
          ) : (
            <>
              <div className="result-title">
                <div>
                  <span className="eyebrow">
                    {resultInstrument
                      ? `${resultInstrument.name} · ${resultInstrument.symbol}`
                      : result.symbol}{" "}
                    · {INTERVALS.find((item) => item.id === result.interval)?.label}
                  </span>
                  <h2>{result.strategy.name}</h2>
                </div>
                <div className="result-actions">
                  <div className="source-row">
                    {result.citations.map((citation, index) => (
                      <SourceBadge citation={citation} key={`${citation.source}-${index}`} />
                    ))}
                  </div>
                  <div className="result-action-buttons">
                    <button
                      className="save-experiment-button"
                      onClick={openSaveEditor}
                      type="button"
                    >
                      保存为实验
                    </button>
                    {forwardObservationMode &&
                      result.strategy.id !== "custom" &&
                      (currentForwardTrack ? (
                        <a className="forward-track-link" href="/tracking">
                          {forwardObservationMode === "validated"
                            ? "已开始纸面验证 ↗"
                            : "正在前向积累 ↗"}
                        </a>
                      ) : (
                        <button
                          className="forward-track-button"
                          disabled={experimentOperation !== null}
                          onClick={() => void saveAndTrackCurrent()}
                          type="button"
                        >
                          {experimentOperation === "track"
                            ? "正在建立…"
                            : currentForwardExperiment
                              ? forwardObservationMode === "validated"
                                ? "重试纸面跟踪"
                                : "重试前向观察"
                              : forwardObservationMode === "validated"
                                ? "一键纸面验证"
                                : "冻结参数 · 前向积累"}
                        </button>
                      ))}
                  </div>
                </div>
              </div>
              {showSaveEditor && (
                <section className="save-experiment-panel">
                  <div>
                    <span className="eyebrow">REPRODUCIBLE SNAPSHOT</span>
                    <h3>保存本次实验</h3>
                    <p>
                      同时保存参数、区间、数据源、成交假设、基准结果和样本外验证，
                      以后可以公平比较或载入配置重跑。
                    </p>
                  </div>
                  <div className="save-experiment-fields">
                    <label>
                      实验名称
                      <input
                        maxLength={100}
                        onChange={(event) => setExperimentName(event.target.value)}
                        value={experimentName}
                      />
                    </label>
                    <label>
                      备注（可选）
                      <input
                        maxLength={1000}
                        onChange={(event) => setExperimentNotes(event.target.value)}
                        placeholder="例如：趋势行情候选，等待震荡期复核"
                        value={experimentNotes}
                      />
                    </label>
                    <div>
                      <button
                        className="secondary-button"
                        onClick={() => setShowSaveEditor(false)}
                        type="button"
                      >
                        取消
                      </button>
                      <button
                        className="primary-button"
                        disabled={
                          experimentOperation !== null || !experimentName.trim()
                        }
                        onClick={() => void saveExperiment()}
                        type="button"
                      >
                        {experimentOperation === "save" ? "正在保存…" : "确认保存"}
                      </button>
                    </div>
                  </div>
                </section>
              )}
              <BarFinalization payload={result} />
              <RunProvenance payload={result} />
              <InstrumentScopeNotice metadata={result.data_metadata} />
              <OhlcBoundRepairNotice metadata={result.data_metadata} />
              <IndicatorWarmup payload={result} />
              <section
                className={`benchmark-verdict ${
                  ["buy-hold", "constant-allocation"].includes(
                    result.strategy.id,
                  ) ||
                  (result.comparison.positive_return &&
                    result.timing_comparison.beats_exposure_matched)
                    ? "passed"
                    : "failed"
                }`}
              >
                <div className="benchmark-verdict-copy">
                  <span className="eyebrow">INITIAL-ALLOCATION BENCHMARK</span>
                  <h3>
                    {result.strategy.id === "buy-hold"
                      ? "这是满仓被动基准，不评价择时"
                      : fixedShareBaseline
                        ? "这是风险预算固定份额配置，不评价择时"
                        : result.strategy.id === "constant-allocation"
                        ? "这是固定目标比例配置，不评价择时"
                      : result.comparison.positive_return &&
                          result.timing_comparison.beats_exposure_matched
                      ? "择时跑赢初始投入匹配的固定份额基准"
                      : "长期空仓没有证明择时价值"}
                  </h3>
                  <p>
                    {result.strategy.id === "buy-hold"
                      ? "该结果用于回答“如果从起点一直持有会怎样”，是所有主动策略必须面对的最低可解释对照。"
                      : fixedShareBaseline
                        ? "该结果只在第一根 K 线开盘按历史回撤预算投入，之后固定份额与剩余现金，不择时、不再平衡；实际持仓率会随价格漂移。"
                        : result.strategy.id === "constant-allocation"
                        ? "该结果维持固定目标资金比例，其余保留现金；这是理论配置对照，不代表已计入真实再平衡换手。"
                      : "除完整买入持有外，再把策略平均持仓率作为第一根 K 线开盘的初始投入比例，之后不再平衡，固定份额与剩余现金持有到底。基准的实际平均持仓会随价格漂移；只有跑赢它，低回撤才不只是因为钱长期留在现金里。"}
                  </p>
                </div>
                <div className="benchmark-stats">
                  <div>
                    <span>策略累计</span>
                    <strong>{formatPercent(result.result.metrics.total_return)}</strong>
                  </div>
                  <div>
                    <span>买入持有</span>
                    <strong>
                      {formatPercent(result.benchmark.result.metrics.total_return)}
                    </strong>
                  </div>
                  <div>
                    <span>固定份额被动</span>
                    <span>
                      初始投入{" "}
                      {formatPercent(
                        result.exposure_matched_benchmark.target_exposure,
                      )}{" "}
                      · 实际平均持仓{" "}
                      {formatPercent(
                        result.exposure_matched_benchmark.result.metrics
                          .exposure_ratio,
                      )}
                    </span>
                    <strong>
                      {formatPercent(
                        result.exposure_matched_benchmark.result.metrics
                          .total_return,
                      )}
                    </strong>
                  </div>
                  <div>
                    <span>择时超额</span>
                    <strong>
                      {formatPercent(result.timing_comparison.excess_return)}
                    </strong>
                  </div>
                  <div>
                    <span>回撤改善</span>
                    <strong>{formatPercent(result.comparison.drawdown_improvement)}</strong>
                  </div>
                  <div>
                    <span>完整基准超额</span>
                    <strong>{formatPercent(result.comparison.excess_return)}</strong>
                  </div>
                </div>
              </section>
              <StrategyDiagnostics optimization={optimization} payload={result} />
              {(() => {
                const passiveStrategy = ["buy-hold", "constant-allocation"].includes(
                  result.strategy.id,
                );
                const metrics = result.result.metrics;
                return (
              <div className="metrics-grid">
                <Metric
                  label="累计收益"
                  value={formatPercent(metrics.total_return)}
                />
                <Metric
                  label="年化收益"
                  value={formatAnnualizedReturn(metrics)}
                />
                <Metric label="夏普比率" value={metrics.sharpe_ratio.toFixed(2)} />
                <Metric
                  label="最大回撤"
                  negative
                  value={formatPercent(metrics.max_drawdown)}
                />
                <Metric
                  label="闭合决策周期胜率"
                  value={formatPercent(metrics.win_rate)}
                />
                <Metric
                  label={passiveStrategy ? "初始建仓动作" : "样本内执行变动"}
                  value={
                    passiveStrategy
                      ? `${formatCompact(metrics.trades)} 笔`
                      : formatCompact(metrics.trades)
                  }
                />
                <Metric
                  label={passiveStrategy ? "再平衡规则" : "年化仓位变动频率"}
                  value={
                    passiveStrategy
                      ? "无 · 全程持有"
                      : annualizedTradeFrequency(
                          metrics.trades_per_year,
                          metrics.duration_years,
                        )
                  }
                />
                <Metric
                  label="闭合周期平均持有"
                  value={`${metrics.average_holding_bars.toFixed(1)} 根 K 线`}
                />
                <Metric
                  label="持仓覆盖率"
                  value={formatPercent(metrics.exposure_ratio)}
                />
                  <Metric
                    label="最长连续空仓"
                    value={barsAsTime(metrics.max_cash_streak, result.interval)}
                  />
                <Metric
                  label="有效样本"
                  value={`${formatCompact(metrics.bars)} 根 K 线`}
                />
                <Metric
                  label="闭合周期盈亏比"
                  value={metrics.profit_factor.toFixed(2)}
                />
              </div>
                );
              })()}
              <p className="metric-frequency-note">
                {result.strategy.id === "buy-hold" ||
                result.strategy.id === "constant-allocation"
                  ? "被动基准只在起点配置一次，不把这次初始建仓年化为交易频率。"
                  : "年化频率按每次仓位变化推算；胜率、盈亏比和平均持有时长按独立决策周期统计：现金择时使用“空仓→持仓→空仓”，核心+卫星配置使用“加卫星仓位→回到核心仓位”，分批加减仓不会虚增样本。短于一年时只用于描述当前样本，不代表未来频率。"}{" "}
                {annualizationBasis(
                  result.result.config.annual_periods,
                  result.result.config.bar_interval,
                )}
              </p>
              {optimization && (
                <section
                  className={`optimization-result ${
                    optimization.validation_passed
                      ? "validation-pass"
                      : optimization.forward_observation_eligible
                        ? "validation-provisional"
                        : "validation-fail"
                  }`}
                >
                  <div>
                    <span className="eyebrow">REOPTIMIZED WINDOWS + FINAL HOLDOUT</span>
                    <h3>
                      {optimization.validation_passed
                        ? optimization.validation_trade_quality?.sample_quality ===
                          "insufficient"
                          ? "通过硬门槛，留出样本仍有限"
                          : "参数通过最终留出期验证"
                        : optimization.forward_observation_eligible
                          ? "未通过验证 · 可冻结参数前向积累"
                          : "参数未通过最终留出期验证"}
                    </h3>
                    <p>
                      共评估 {optimization.candidates_evaluated} 组参数，其中{" "}
                      {optimization.candidates_eligible} 组通过参与度和走步验证；最终留出期始于{" "}
                      {optimization.split_date.slice(0, 10)}。系统已采用
                      {" "}
                      {Object.entries(optimization.selected_parameters)
                        .map(([key, value]) => `${key}=${value}`)
                        .join(" · ")}
                    </p>
                    <p className="validation-reason">{optimization.validation_reason}</p>
                    <p className="validation-next-action">
                      <b>下一步</b>
                      {holdoutNextAction(
                        optimization.validation_code,
                        optimization.forward_observation_eligible,
                      )}
                    </p>
                  </div>
                  <div className="optimization-comparison">
                    <div>
                      <span>开发期（含 3 段走步验证）</span>
                      <strong>{formatPercent(optimization.train_metrics.total_return)}</strong>
                      <small>
                        夏普 {optimization.train_metrics.sharpe_ratio.toFixed(2)} · 回撤{" "}
                        {formatPercent(optimization.train_metrics.max_drawdown)} · 持仓{" "}
                        {formatPercent(optimization.train_metrics.exposure_ratio)}
                      </small>
                    </div>
                    <div>
                      <span>最终 20% 留出期（未参与选参）</span>
                      <strong>
                        {formatPercent(optimization.validation_metrics.total_return)}
                      </strong>
                      <small>
                        夏普 {optimization.validation_metrics.sharpe_ratio.toFixed(2)} · 回撤{" "}
                        {formatPercent(optimization.validation_metrics.max_drawdown)} · 交易{" "}
                        {optimization.validation_metrics.trades} 笔 · 同期持有{" "}
                        {formatPercent(
                          optimization.validation_benchmark_metrics.total_return,
                        )}{" "}
                        · 初始投入匹配被动{" "}
                        {formatPercent(
                          optimization
                            .validation_exposure_matched_benchmark_metrics
                            .total_return,
                        )}
                      </small>
                    </div>
                  </div>
                  {optimization.validation_trade_quality && (
                    <div
                      className={`holdout-confidence ${optimization.validation_trade_quality.sample_quality}`}
                    >
                      <div className="robustness-heading">
                        <div>
                          <span className="eyebrow">UNSEEN SAMPLE CONFIDENCE</span>
                          <h4>最终留出交易可信度</h4>
                        </div>
                        <strong>
                          {optimization.validation_trade_quality.sample_quality === "mature"
                            ? "样本较成熟"
                            : optimization.validation_trade_quality.sample_quality ===
                                "developing"
                              ? "样本积累中"
                              : "样本仍有限"}
                        </strong>
                      </div>
                      <div className="holdout-confidence-grid">
                        <div>
                          <span>已闭合独立决策周期</span>
                          <strong>
                            {optimization.validation_trade_quality.closed_trades} 个
                          </strong>
                          <small>至少 10 个才通过参与度门槛</small>
                        </div>
                        <div>
                          <span>胜率 · Wilson 95% 区间</span>
                          <strong>
                            {formatPercent(optimization.validation_trade_quality.win_rate)}
                          </strong>
                          <small>
                            {formatPercent(
                              optimization.validation_trade_quality
                                .win_rate_confidence_low,
                            )}{" "}
                            —{" "}
                            {formatPercent(
                              optimization.validation_trade_quality
                                .win_rate_confidence_high,
                            )}
                          </small>
                        </div>
                        <div>
                          <span>留出单笔期望</span>
                          <strong>
                            {formatPercent(
                              optimization.validation_trade_quality.expectancy,
                            )}
                          </strong>
                        </div>
                        <div>
                          <span>留出平均盈亏比</span>
                          <strong>
                            {optimization.validation_trade_quality.payoff_ratio === null
                              ? "样本不足"
                              : optimization.validation_trade_quality.payoff_ratio.toFixed(2)}
                          </strong>
                        </div>
                      </div>
                      <p>
                        这里只统计从未参与模板初筛、参数搜索和候选锁定的最终留出区间；
                        少于 30 笔仍属于有限统计证据，建议进入纸面跟踪继续积累。
                      </p>
                    </div>
                  )}
                  <div className="robustness-evidence">
                    <div className="robustness-heading">
                      <div>
                        <span className="eyebrow">FRICTION STRESS</span>
                        <h4>交易成本压力测试</h4>
                      </div>
                      <strong
                        className={
                          optimization.cost_stress_passed ? "passed" : "failed"
                        }
                      >
                        {optimization.cost_stress_passed ? "通过" : "未通过"}
                      </strong>
                    </div>
                    <div className="cost-stress-grid">
                      {optimization.cost_stress_tests.map((stress) => (
                        <div key={stress.multiplier}>
                          <span>{stress.multiplier.toFixed(0)}× 费用 + 滑点</span>
                            <strong className={stress.passed ? "" : "negative"}>
                              {formatPercent(stress.metrics.total_return)}
                            </strong>
                            <small>
                              总摩擦 {formatPercent(stress.fee_rate + stress.slippage_rate)} ·
                              初始投入基准超额 {formatPercent(stress.timing_excess_return)} ·
                              {stress.passed ? "仍有择时价值" : "未保留择时价值"}
                            </small>
                        </div>
                      ))}
                    </div>
                  </div>
                  <div className="parameter-cluster">
                    <div className="robustness-heading">
                      <div>
                        <span className="eyebrow">ROBUST PARAMETER CLUSTER</span>
                        <h4>前三组稳健参数</h4>
                      </div>
                      <p>来自开发区走步评分，不读取最终留出期。</p>
                    </div>
                    <div className="parameter-cluster-grid">
                      {optimization.top_candidates.slice(0, 3).map((candidate, index) => (
                        <div key={JSON.stringify(candidate.parameters)}>
                          <span>#{index + 1}</span>
                          <code>
                            {Object.entries(candidate.parameters)
                              .map(([key, value]) => `${key}=${value}`)
                              .join(" · ")}
                          </code>
                          <small>
                            开发收益 {formatPercent(candidate.metrics.total_return)} ·
                            盈利窗口 {formatPercent(candidate.profitable_fold_ratio)} ·
                            择时有效{" "}
                            {formatPercent(candidate.timing_positive_fold_ratio)}
                          </small>
                        </div>
                      ))}
                    </div>
                  </div>
                  <div className="walk-forward-grid">
                    {optimization.walk_forward_folds.map((fold, index) => (
                      <div key={fold.validation_start}>
                        <span>窗口 {index + 1}</span>
                        <strong
                          className={fold.timing_value_added ? "passed" : "negative"}
                        >
                          择时超额 {formatPercent(fold.timing_excess_return)}
                        </strong>
                        <small>
                          {fold.validation_start.slice(0, 10)} —{" "}
                          {fold.validation_end.slice(0, 10)} ·{" "}
                          {Object.entries(fold.selected_parameters)
                            .map(([key, value]) => `${key}=${value}`)
                            .join(" · ")} ·{" "}
                          策略 {formatPercent(fold.validation_metrics.total_return)} ·
                          同资金被动{" "}
                          {formatPercent(
                            fold.exposure_matched_benchmark_metrics.total_return,
                          )}{" "}
                          · {fold.validation_metrics.trades} 笔
                        </small>
                      </div>
                    ))}
                  </div>
                  <p className="walk-forward-note">
                    {optimization.walk_forward_execution_state_carried
                      ? "每个窗口只用其之前的训练数据重新寻优，随后冻结该窗参数验证。窗口只统计验证期指标；若边界前已持仓，会按同一下一根开盘规则承接仓位、隔夜收益与换手成本。基准也按同一边界计算。"
                      : "该历史结果未记录走步窗口的跨边界执行状态，不能据此判断仓位是否被承接。"}
                  </p>
                  <p className="optimization-warning">
                    硬约束：开发期至少 {optimization.minimum_trades} 笔交易、持仓率至少{" "}
                    {formatPercent(optimization.minimum_exposure)}
                    {optimization.minimum_annualized_return !== null &&
                    optimization.minimum_annualized_return !== undefined
                      ? `、目标年化至少 ${formatPercent(optimization.minimum_annualized_return)}`
                      : ""}
                    {optimization.maximum_drawdown
                      ? `、最大回撤不超过 ${formatPercent(optimization.maximum_drawdown)}`
                      : ""}
                    {optimization.maximum_trades_per_year
                      ? `、每年不超过 ${optimization.maximum_trades_per_year} 笔`
                      : ""}
                    {optimization.maximum_cash_streak_bars
                      ? `、连续空仓不超过 ${optimization.maximum_cash_streak_bars} 根 K 线`
                      : ""}
                    、最终留出至少 10 个已闭合独立决策周期、开发区至少{" "}
                    {formatPercent(optimization.minimum_profitable_fold_ratio)}{" "}
                    的验证窗口盈利，且至少{" "}
                    {formatPercent(
                      optimization.minimum_timing_positive_fold_ratio,
                    )}{" "}
                    的窗口跑赢初始投入匹配的固定份额被动基准。未通过留出期时，不应把参数当作“最佳策略”。
                  </p>
                </section>
              )}
              <div className="chart-card">
                <BacktestChart payload={result} />
              </div>
              {result.result.trades.length > 0 && (
                <div className="trade-log">
                  <div className="trade-log-heading">
                    <h3>调仓批次明细</h3>
                    <span>
                      最近 {Math.min(12, result.result.trades.length)} / 总计{" "}
                      {result.result.trades.length} 个资金归因批次 ·{" "}
                      {result.result.metrics.closed_trades} 个已闭合独立决策周期
                    </span>
                  </div>
                  <div className="trade-table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>入场</th>
                          <th>出场</th>
                          <th>入场价</th>
                          <th>出场价</th>
                          <th>批次仓位</th>
                          <th>持有</th>
                          <th>状态</th>
                          <th>收益</th>
                        </tr>
                      </thead>
                      <tbody>
                        {result.result.trades.slice(-12).reverse().map((trade, index) => (
                          <tr key={`${trade.entry_date}-${index}`}>
                            <td>
                              {String(trade.entry_date)
                                .replace("T", " ")
                                .slice(0, ["15m", "1h", "4h"].includes(result.interval) ? 16 : 10)}
                            </td>
                            <td>
                              {String(trade.exit_date)
                                .replace("T", " ")
                                .slice(0, ["15m", "1h", "4h"].includes(result.interval) ? 16 : 10)}
                            </td>
                            <td>{Number(trade.entry_price).toFixed(2)}</td>
                            <td>{Number(trade.exit_price).toFixed(2)}</td>
                            <td>
                              {formatPercent(Number(trade.position_size ?? 1))}
                            </td>
                            <td>{Number(trade.holding_bars)} 根</td>
                            <td>{trade.closed ? "已平仓" : "持仓中"}</td>
                            <td className={Number(trade.return) < 0 ? "negative" : "positive"}>
                              {formatPercent(Number(trade.return))}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
              <p className="disclaimer">
                历史回测不代表未来表现。结果使用收盘价生成信号、下一根 K
                线开盘成交，区分隔夜与日内收益，并计入双向费用与滑点。
              </p>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

function StrategyDiagnostics({
  payload,
  optimization,
}: {
  payload: BacktestPayload;
  optimization: OptimizationResult | null;
}) {
  const metrics = payload.result.metrics;
  const diagnostics = payload.diagnostics;
  const tradeQuality = diagnostics.trade_quality;
  const fixedShareBaseline =
    payload.data_metadata.execution_model === "fixed_shares";
  let level: "candidate" | "watch" | "reject" = "watch";
  let title = "观察：证据还不够稳定";
  let reason = "先扩展样本或更换市场状态复核，不要只根据总收益决定。";
  if (payload.strategy.id === "buy-hold") {
    title = "基准：完整承受标的收益与回撤";
    reason = "买入持有不做择时判断；它用于检验主动策略是否真的增加价值。";
  } else if (fixedShareBaseline) {
    title = "配置：以固定份额执行历史回撤预算";
    reason =
      "该方案只校准起点投入，建仓后不再平衡；实际持仓率随价格漂移，历史预算也可能被未来更极端的行情突破。";
  } else if (payload.strategy.id === "constant-allocation") {
    title = "理论配置：维持固定目标权重";
    reason =
      "该方案不做择时；当前结果用于固定权重研究，未把维持权重所需的真实再平衡换手计入执行成本。";
  } else if (!optimization) {
    title = "样本内观察：未做选参隔离";
    reason =
      "这里只描述当前参数在同一历史样本中的表现，没有开发期选参、走步验证或最终留出证据；不能据此认定为研究候选或淘汰策略。";
  } else if (optimization.forward_observation_eligible) {
    title = "前向观察：参数已冻结，等待新证据";
    reason =
                  "候选已通过收益、风险、参与度和成本检查，但最终留出少于 10 个已闭合独立决策周期；不能称为验证通过，只能用激活后数据继续积累。";
  } else if (!optimization.validation_passed) {
    level = "reject";
    title = "淘汰：最终留出期未通过";
    reason = "参数在未参与选参的数据上没有守住约束，不应进入纸面跟踪。";
  } else if (metrics.total_return <= 0) {
    level = "reject";
    title = "淘汰：完整样本仍为负收益";
    reason = "即使局部区间有效，当前配置也没有证明长期可用性。";
  } else if (!payload.timing_comparison.beats_exposure_matched) {
    level = "reject";
    title = "淘汰：未跑赢初始投入匹配的固定份额基准";
    reason =
      "以策略平均持仓率作为初始投入、之后固定份额持有的被动基准收益更高；当前低回撤主要来自长期留现金，不足以证明择时有效。";
  } else if (tradeQuality.sample_quality === "insufficient") {
    title = "观察：完整交易样本过少";
    reason = "少于 30 个已闭合独立决策周期时，胜率区间通常很宽，需要更长区间或更高频周期复核。";
  } else if (diagnostics.profitable_segment_ratio < 0.5) {
    title = "观察：盈利集中在少数区间";
    reason = "至少一半分段未盈利，策略对市场状态的依赖较强。";
  } else if (
    payload.comparison.beats_benchmark ||
    payload.comparison.drawdown_improvement >= 0.1
  ) {
    level = "candidate";
    title = "研究候选：择时具备相对价值";
    reason = payload.comparison.beats_benchmark
      ? "在完整样本跑赢买入持有和初始投入匹配的固定份额基准，且分段表现达到最低稳定性要求。"
      : "未跑赢满仓持有，但跑赢初始投入匹配的固定份额基准，并以更小回撤保留正收益。";
  }
  const signal = diagnostics.signal_state;
  const signalLabels = {
    pending_entry: "待下一根 K 线开盘买入",
    pending_exit: "待下一根 K 线开盘退出",
    holding: "持仓中",
    cash: "空仓等待",
  };
  const sampleQualityLabels = {
    insufficient: "样本不足",
    developing: "样本积累中",
    mature: "样本较成熟",
  };
  const isPassive = ["buy-hold", "constant-allocation"].includes(
    payload.strategy.id,
  );
  const isFractionalAllocation =
    (signal.requested_signal > 0 && signal.requested_signal < 1) ||
    (signal.executed_position > 0 && signal.executed_position < 1);

  return (
    <section className={`strategy-diagnostics ${level}`}>
      <div className="strategy-decision">
        <div>
          <span className="eyebrow">RESEARCH DECISION</span>
          <h3>{title}</h3>
          <p>{reason}</p>
        </div>
        <div className="decision-facts">
          {isPassive ? (
            <>
              <div>
                <span>资产仓位</span>
                <strong>{formatPercent(metrics.exposure_ratio)}</strong>
              </div>
              <div>
                <span>现金保留</span>
                <strong>{formatPercent(1 - metrics.exposure_ratio)}</strong>
              </div>
              <div>
                <span>历史最大回撤</span>
                <strong>{formatPercent(metrics.max_drawdown)}</strong>
              </div>
            </>
          ) : (
            <>
              <div>
                <span>盈利分段</span>
                <strong>{formatPercent(diagnostics.profitable_segment_ratio)}</strong>
              </div>
              <div>
                <span>跑赢基准分段</span>
                <strong>
                  {formatPercent(diagnostics.benchmark_beaten_segment_ratio)}
                </strong>
              </div>
              <div>
                <span>最差分段</span>
                <strong>{formatPercent(diagnostics.worst_segment_return)}</strong>
              </div>
            </>
          )}
        </div>
      </div>
      {!isPassive && (
        <div className={`trade-confidence ${tradeQuality.sample_quality}`}>
          <div className="trade-confidence-heading">
            <div>
              <span className="eyebrow">TRADE SAMPLE CONFIDENCE</span>
              <h4>交易统计可信度</h4>
            </div>
            <strong>{sampleQualityLabels[tradeQuality.sample_quality]}</strong>
          </div>
          <div className="trade-confidence-grid">
            <div>
              <span>已闭合独立决策周期</span>
              <strong>{tradeQuality.closed_trades} 个</strong>
            </div>
            <div>
              <span>胜率 · 95% 区间</span>
              <strong>{formatPercent(tradeQuality.win_rate)}</strong>
              <small>
                {formatPercent(tradeQuality.win_rate_confidence_low)} —{" "}
                {formatPercent(tradeQuality.win_rate_confidence_high)}
              </small>
            </div>
            <div>
              <span>单笔期望</span>
              <strong>{formatPercent(tradeQuality.expectancy)}</strong>
            </div>
            <div>
              <span>平均盈利 / 亏损</span>
              <strong>
                {formatPercent(tradeQuality.average_winner)} /{" "}
                {formatPercent(tradeQuality.average_loser)}
              </strong>
            </div>
            <div>
              <span>平均盈亏比</span>
              <strong>
                {tradeQuality.payoff_ratio === null
                  ? "样本不足"
                  : tradeQuality.payoff_ratio.toFixed(2)}
              </strong>
            </div>
          </div>
          <p>
            置信区间使用 Wilson 95% 区间；交易越少，区间越宽。它描述历史统计不确定性，
            不代表未来胜率保证。
          </p>
        </div>
      )}
      <div className="signal-state-card">
        <div>
          <span className="eyebrow">LATEST SIGNAL</span>
          <h4>{signalLabels[signal.status]}</h4>
          <p>
            最新 K 线 {signal.latest_bar_at.replace("T", " ").slice(0, 16)}；
            {isFractionalAllocation ? (
              <>
                目标仓位 {formatPercent(signal.requested_signal)}，已执行仓位{" "}
                {formatPercent(signal.executed_position)}。
              </>
            ) : (
              <>
                原始信号 {signal.requested_signal > 0 ? "持有" : "空仓"}，已执行仓位{" "}
                {signal.executed_position > 0 ? "持有" : "空仓"}。
              </>
            )}
          </p>
        </div>
        <div className="signal-state-meta">
          <span>
            信号自 {signal.last_signal_change_at.replace("T", " ").slice(0, 16)} 起持续{" "}
            <b>{signal.bars_in_signal_state}</b> 根 K 线
          </span>
          {signal.executed_position <= 0 && (
            <span>
              当前已连续空仓 <b>{signal.bars_in_executed_cash}</b> 根 K 线
            </span>
          )}
        </div>
      </div>
      <div className="segment-diagnostics">
        {diagnostics.segments.map((segment) => (
          <article
            className={
              segment.profitable && segment.beats_benchmark
                ? "strong"
                : segment.profitable
                  ? "mixed"
                  : "weak"
            }
            key={segment.index}
          >
            <div>
              <span>样本 {segment.index}</span>
              <small>
                {segment.start.slice(0, 10)} — {segment.end.slice(0, 10)}
              </small>
            </div>
            <strong>{formatPercent(segment.strategy_return)}</strong>
            <p>
              持有 {formatPercent(segment.benchmark_return)} · 超额{" "}
              {formatPercent(segment.excess_return)} · 回撤{" "}
              {formatPercent(segment.max_drawdown)}
            </p>
          </article>
        ))}
      </div>
      <p className="diagnostics-note">
        四段按时间顺序等分，用于研究分级并暴露“总收益很好，但只靠一小段行情”的情况；
        它不能替代最终留出期验证。“研究候选”仍不代表可直接实盘。
      </p>
    </section>
  );
}

function ExperimentLibrary({
  experiments,
  busy,
  onLoad,
  onDelete,
  onTrack,
}: {
  experiments: ExperimentRecord[];
  busy: boolean;
  onLoad: (experiment: ExperimentRecord) => void;
  onDelete: (experiment: ExperimentRecord) => void;
  onTrack: (experiment: ExperimentRecord) => void;
}) {
  const [query, setQuery] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const filtered = experiments.filter((experiment) => {
    const search = query.trim().toLowerCase();
    return (
      !search ||
      [
        experiment.name,
        experiment.notes ?? "",
        experiment.instrument.symbol,
        experiment.instrument.name,
        experiment.strategy.name,
      ].some((value) => value.toLowerCase().includes(search))
    );
  });
  const selected = selectedIds
    .map((id) => experiments.find((experiment) => experiment.id === id))
    .filter((experiment): experiment is ExperimentRecord => Boolean(experiment));
  const scopes = new Set(
    selected.map(
      (experiment) =>
        `${experiment.instrument.symbol}|${experiment.interval}|${experiment.start}|${experiment.end}`,
    ),
  );
  const sameScope = scopes.size <= 1;

  function toggleExperiment(id: string) {
    setSelectedIds((items) =>
      items.includes(id)
        ? items.filter((item) => item !== id)
        : items.length < 4
          ? [...items, id]
          : items,
    );
  }

  return (
    <section className="experiment-library">
      <div className="experiment-library-heading">
        <div>
          <span className="eyebrow">EXPERIMENT VAULT · NAS PERSISTED</span>
          <h2>策略实验档案</h2>
          <p>
            保存的是可复现的研究快照。勾选 2–4
            组对比；只有标的、周期和样本区间一致时，排名才可直接解释。
          </p>
        </div>
        <label>
          搜索实验
          <input
            onChange={(event) => setQuery(event.target.value)}
            placeholder="名称 / 标的 / 策略"
            value={query}
          />
        </label>
      </div>
      {filtered.length === 0 ? (
        <div className="experiment-library-empty">
          <strong>{experiments.length === 0 ? "还没有保存的实验" : "没有匹配的实验"}</strong>
          <span>完成一次回测或自动寻优后，使用结果右上角的“保存为实验”。</span>
        </div>
      ) : (
        <div className="experiment-card-grid">
          {filtered.map((experiment) => {
            const isSelected = selectedIds.includes(experiment.id);
            return (
              <article
                className={isSelected ? "experiment-card selected" : "experiment-card"}
                key={experiment.id}
              >
                <div className="experiment-card-heading">
                  <label className="experiment-checkbox">
                    <input
                      checked={isSelected}
                      disabled={!isSelected && selectedIds.length >= 4}
                      onChange={() => toggleExperiment(experiment.id)}
                      type="checkbox"
                    />
                    对比
                  </label>
                  <span>{new Date(experiment.created_at).toLocaleDateString("zh-CN")}</span>
                </div>
                <strong>{experiment.name}</strong>
                <p>
                  {experiment.instrument.name} · {experiment.instrument.symbol} ·{" "}
                  {INTERVALS.find((item) => item.id === experiment.interval)?.label} ·{" "}
                  {experiment.strategy.name}
                </p>
                <div className="experiment-card-metrics">
                  <div>
                    <span>收益</span>
                    <b className={experiment.metrics.total_return < 0 ? "negative" : "positive"}>
                      {formatPercent(experiment.metrics.total_return)}
                    </b>
                  </div>
                  <div>
                    <span>择时超额</span>
                    <b
                      className={
                        (experiment.timing_comparison?.excess_return ??
                          experiment.comparison.excess_return) < 0
                          ? "negative"
                          : "positive"
                      }
                    >
                      {formatPercent(
                        experiment.timing_comparison?.excess_return ??
                          experiment.comparison.excess_return,
                      )}
                    </b>
                  </div>
                  <div>
                    <span>回撤</span>
                    <b className="negative">
                      {formatPercent(experiment.metrics.max_drawdown)}
                    </b>
                  </div>
                  <div>
                    <span>夏普</span>
                    <b>{experiment.metrics.sharpe_ratio.toFixed(2)}</b>
                  </div>
                </div>
                <div
                  className={`experiment-validation ${
                    experiment.validation?.validation_passed
                      ? "passed"
                      : experiment.validation?.forward_observation_eligible
                        ? "provisional"
                        : "unverified"
                  }`}
                >
                  {experiment.validation
                    ? experiment.validation.validation_passed
                      ? "最终留出期通过"
                      : experiment.validation.forward_observation_eligible
                        ? "样本不足 · 参数已冻结"
                      : "最终留出期未通过"
                    : "未做样本外验证"}
                </div>
                {experiment.notes && <small>{experiment.notes}</small>}
                <div className="experiment-card-actions">
                  <button disabled={busy} onClick={() => onLoad(experiment)} type="button">
                    载入配置
                  </button>
                  <button
                    disabled={
                      busy ||
                      (!experiment.validation?.validation_passed &&
                        !experiment.validation?.forward_observation_eligible)
                    }
                    onClick={() => onTrack(experiment)}
                    type="button"
                  >
                    {experiment.validation?.validation_passed
                      ? "纸面跟踪"
                      : experiment.validation?.forward_observation_eligible
                        ? "前向积累"
                        : "不可跟踪"}
                  </button>
                  <button
                    className="danger"
                    disabled={busy}
                    onClick={() => onDelete(experiment)}
                    type="button"
                  >
                    删除
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}
      {selected.length > 1 && (
        <div className="experiment-comparison">
          <div className={sameScope ? "comparison-scope valid" : "comparison-scope warning"}>
            <strong>{sameScope ? "同口径比较" : "口径不同，仅供方向参考"}</strong>
            <span>
              {sameScope
                ? "标的、K 线周期和样本区间一致，可以比较收益、回撤与风险调整表现。"
                : "至少一组的标的、周期或样本区间不同，不能据此直接认定策略优劣。"}
            </span>
          </div>
          <div className="experiment-comparison-table">
            <table>
              <thead>
                <tr>
                  <th>实验</th>
                  <th>累计收益</th>
                  <th>买入持有</th>
                  <th>初始投入匹配被动</th>
                  <th>择时超额</th>
                  <th>最大回撤</th>
                  <th>夏普</th>
                  <th>每年交易</th>
                  <th>留出验证</th>
                </tr>
              </thead>
              <tbody>
                {selected.map((experiment) => (
                  <tr key={experiment.id}>
                    <td>
                      <strong>{experiment.name}</strong>
                      <small>
                        {experiment.instrument.symbol} · {experiment.interval} ·{" "}
                        {experiment.start} — {experiment.end}
                      </small>
                    </td>
                    <td>{formatPercent(experiment.metrics.total_return)}</td>
                    <td>{formatPercent(experiment.benchmark_metrics.total_return)}</td>
                    <td>
                      {experiment.exposure_matched_benchmark_metrics
                        ? formatPercent(
                            experiment.exposure_matched_benchmark_metrics
                              .total_return,
                          )
                        : "—"}
                    </td>
                    <td>
                      {experiment.timing_comparison
                        ? formatPercent(
                            experiment.timing_comparison.excess_return,
                          )
                        : "—"}
                    </td>
                    <td>{formatPercent(experiment.metrics.max_drawdown)}</td>
                    <td>{experiment.metrics.sharpe_ratio.toFixed(2)}</td>
                    <td>{experiment.metrics.trades_per_year.toFixed(1)}</td>
                    <td>
                      {experiment.validation
                        ? experiment.validation.validation_passed
                          ? "通过"
                          : experiment.validation.forward_observation_eligible
                            ? "样本不足"
                          : "未通过"
                        : "未验证"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}

function barFinalizationSummary(metadata: Record<string, unknown>): string {
  const finalizedOnly = metadata.finalized_bars_only === true;
  const verified = metadata.bar_finalization_verified === true;
  const lagSeconds = Number(metadata.bar_finalization_lag_seconds ?? 0);
  const policy =
    typeof metadata.bar_finalization_policy === "string"
      ? metadata.bar_finalization_policy
      : "";
  if (verified) {
    return `交易所时钟已确认${lagSeconds > 0 ? ` · 缓冲 ${lagSeconds} 秒` : ""}`;
  }
  if (!finalizedOnly) return "收盘状态未由来源确认";
  if (metadata.interval_semantics === "four_trading_hours_per_session") {
    return "A 股每交易日 4 个交易小时合成";
  }
  if (metadata.interval_semantics === "us_regular_session_4h_plus_close_segment") {
    return "美股 4 小时主段 + 收盘短段";
  }
  if (policy === "estimated_us_regular_session_close") return "美股常规时段后过滤";
  if (policy === "estimated_cn_regular_session_close") return "A 股常规时段后过滤";
  return `仅完整 K 线${lagSeconds > 0 ? ` · 缓冲 ${lagSeconds} 秒` : ""}`;
}

function intervalStructureNote(
  instrument: Instrument | null,
  interval: BarInterval,
): string | null {
  if (interval !== "4h" || !instrument) return null;
  if (instrument.provider === "akshare") {
    return "A 股 4 小时 = 每个交易日跨午休合成的 4 个交易小时（一日一根，年化 252 根）；不是两根自然时钟碎片。";
  }
  if (instrument.provider === "yfinance") {
    return "美股 4 小时 = 美东 09:30–13:30 主段 + 13:30–16:00 收盘短段（通常一日两根，年化 504 根）；不是 UTC 自然时钟聚合。";
  }
  return null;
}

const RUN_REQUIREMENT_LABELS = {
  source_revision: "缺少源码修订号",
  dependency_lock: "缺少依赖锁定摘要",
  dataset_quality: "数据质量尚未完全通过",
  indicator_warmup: "指标预热输入尚未纳入快照",
} as const;

function compactEvidenceId(value: string | undefined): string {
  if (!value) return "未提供";
  if (value.length <= 22) return value;
  return `${value.slice(0, 12)}…${value.slice(-6)}`;
}

function evidenceTimestamp(value: string | undefined): string {
  if (!value) return "未提供";
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return value;
  return new Date(timestamp).toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function RunProvenance({ payload }: { payload: BacktestPayload }) {
  const manifest = payload.run_manifest;
  const snapshot = payload.dataset_snapshot ?? manifest?.datasets[0];
  const hasReceipt = payload.run_id !== undefined;
  const legacy = !hasReceipt && !manifest && !snapshot;
  const complete = manifest?.reproducibility_status === "complete";
  const state = legacy ? "legacy" : complete ? "complete" : "incomplete";
  const receiptExpired =
    hasReceipt &&
    backtestRunReceiptHasExpired(payload.run_expires_at);
  const receiptDetail = !hasReceipt
    ? "旧版结果没有服务端运行回执"
    : receiptExpired
      ? `已于 ${evidenceTimestamp(payload.run_expires_at)} 过期；保存前请重新运行`
      : payload.run_expires_at
        ? `可归档至 ${evidenceTimestamp(payload.run_expires_at)}`
        : "有效期未随结果返回；保存时仍由服务端复核";
  const gapLabels = [
    ...(!hasReceipt ? ["无运行回执"] : []),
    ...(!snapshot ? ["无数据快照"] : []),
    ...(!manifest ? ["无运行清单"] : []),
    ...(manifest?.missing_requirements.map(
      (item) => RUN_REQUIREMENT_LABELS[item],
    ) ?? []),
  ];
  const title = legacy
    ? "旧版结果未附服务端证据"
    : complete
      ? "本次运行证据可完整追溯"
      : "本次运行已封存，仍有复现缺口";
  const detail = legacy
    ? "该结果只能通过兼容接口保存为未验证历史快照；重新运行后，平台会改用服务端回执归档。"
    : complete
      ? "请求、参数、成本模型、结果和实际使用的数据快照均已生成稳定摘要；这证明可追溯，不代表未来收益。"
      : "平台已保留现有证据，但不会把缺少源码、依赖、数据质量或预热输入的运行标记为完全可复现。";

  return (
    <section className={`run-provenance ${state}`} role="note">
      <div className="run-provenance-heading">
        <div>
          <span className="eyebrow">RUN PROVENANCE</span>
          <h3>{title}</h3>
          <p>{detail}</p>
        </div>
        <div className="run-provenance-status">
          <strong>
            {legacy ? "UNVERIFIED" : complete ? "COMPLETE" : "INCOMPLETE"}
          </strong>
          <span>
            {legacy
              ? "兼容旧档案"
              : receiptExpired
                ? "证据保留 · 归档回执已过期"
                : "服务端证据"}
          </span>
        </div>
      </div>
      <dl className="run-provenance-grid">
        <div>
          <dt>运行回执</dt>
          <dd>
            <code title={payload.run_id}>{compactEvidenceId(payload.run_id)}</code>
            <small>{receiptDetail}</small>
          </dd>
        </div>
        <div>
          <dt>运行清单</dt>
          <dd>
            <code title={manifest?.manifest_id}>
              {compactEvidenceId(manifest?.manifest_id)}
            </code>
            <small>
              {manifest
                ? `${manifest.engine_version} · ${manifest.execution_model}`
                : "未提供 manifest"}
            </small>
          </dd>
        </div>
        <div>
          <dt>数据快照</dt>
          <dd>
            <code title={snapshot?.snapshot_id}>
              {compactEvidenceId(snapshot?.snapshot_id)}
            </code>
            <small>
              {snapshot
                ? `${snapshot.provider} · ${snapshot.symbol} · ${snapshot.row_count} 根`
                : "未提供 dataset snapshot"}
            </small>
          </dd>
        </div>
        <div>
          <dt>数据边界</dt>
          <dd>
            <span>
              {snapshot
                ? `${evidenceTimestamp(snapshot.first_observation_at)} → ${evidenceTimestamp(
                    snapshot.last_observation_at,
                  )}`
                : "未提供"}
            </span>
            <small>
              {snapshot
                ? `${snapshot.finalized_only ? "仅已完成 K 线" : "含未确认 K 线"} · 质量 ${snapshot.quality_status}`
                : "无法核验样本边界"}
            </small>
          </dd>
        </div>
      </dl>
      {gapLabels.length > 0 && (
        <div className="run-provenance-gaps">
          <span>复现边界</span>
          <div>
            {gapLabels.map((label) => (
              <small key={label}>{label}</small>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function BarFinalization({ payload }: { payload: BacktestPayload }) {
  const metadata = payload.data_metadata;
  const finalizedOnly = metadata.finalized_bars_only === true;
  const verified = metadata.bar_finalization_verified === true;
  const lagSeconds = Number(metadata.bar_finalization_lag_seconds ?? 0);
  const policy =
    typeof metadata.bar_finalization_policy === "string"
      ? metadata.bar_finalization_policy
      : "";
  const sessionComposite =
    metadata.interval_semantics === "four_trading_hours_per_session";
  const usSessionComposite =
    metadata.interval_semantics === "us_regular_session_4h_plus_close_segment";
  const label = verified
    ? "交易所时钟已确认"
    : finalizedOnly
      ? "仅使用已完成 K 线"
      : "收盘状态未获来源确认";
  const detail = verified
    ? `按交易所服务器时间过滤，收盘后额外等待 ${lagSeconds || 0} 秒。`
    : finalizedOnly
      ? sessionComposite
        ? "A 股 4 小时指每交易日跨午休合成的 4 个交易小时；不是两根自然时钟的两小时碎片，并只在收市缓冲后进入研究。"
        : usSessionComposite
          ? "美股 4 小时按美东 09:30–13:30 主段和 13:30–16:00 收盘短段切分；不是 UTC 自然时钟聚合，并只在收市缓冲后进入研究。"
        : "公共行情源不提供逐根结算证明；已按交易时段或完整周期并留出发布缓冲，排除正在形成的 K 线。"
      : "当前数据源未提供足以确认每根 K 线已收盘的证据；结果只适合历史研究，不能当作实时交易触发。";
  const policyLabel = verified
    ? "可核验收盘"
    : policy === "estimated_us_regular_session_close"
      ? "美股常规时段"
      : policy === "estimated_cn_regular_session_close"
        ? sessionComposite
          ? "A 股交易时段合成"
          : usSessionComposite
            ? "美股交易时段切分"
            : "A 股常规时段"
        : policy === "completed_interval_with_publication_lag"
          ? "完整周期 + 发布缓冲"
          : "来源未声明";

  return (
    <section className={`bar-finalization ${verified ? "verified" : finalizedOnly ? "filtered" : "unknown"}`}>
      <div>
        <span className="eyebrow">BAR FINALIZATION</span>
        <h3>{label}</h3>
        <p>{detail}</p>
      </div>
      <div className="bar-finalization-status">
        <strong>{policyLabel}</strong>
        <span>{lagSeconds > 0 ? `发布缓冲 ${lagSeconds} 秒` : "未声明缓冲"}</span>
      </div>
    </section>
  );
}

function InstrumentScopeNotice({
  metadata,
}: {
  metadata: Record<string, unknown>;
}) {
  if (metadata.instrument_scope !== "foreign_futures_reference_series") {
    return null;
  }

  const sourceNote =
    typeof metadata.instrument_scope_note === "string"
      ? metadata.instrument_scope_note
      : "公开外盘期货价格序列仅用于研究。";

  return (
    <section className="instrument-scope-notice" role="note">
      <div>
        <span className="eyebrow">INSTRUMENT SCOPE</span>
        <h3>公开期货品种序列，不是可直接下单的合约</h3>
        <p>{sourceNote}</p>
      </div>
      <div className="instrument-scope-boundaries">
        <span>到期月：未识别</span>
        <span>换月：未建模</span>
        <span>保证金 / 汇兑：未建模</span>
      </div>
      <small>
        本页只检验该公开价格序列上的历史信号与假设摩擦；若要交易实际期货，请以券商合约、到期月、保证金、换月和真实成交规则重新复核。
      </small>
    </section>
  );
}

function OhlcBoundRepairNotice({
  metadata,
}: {
  metadata: Record<string, unknown>;
}) {
  const repairs = Number(metadata.ohlc_bound_repairs ?? 0);
  if (!Number.isFinite(repairs) || repairs <= 0) return null;

  const policy =
    typeof metadata.ohlc_bound_repair_policy === "string"
      ? metadata.ohlc_bound_repair_policy
      : "上游高低价未包住开收盘时，仅做最小边界扩展。";
  const firstDate =
    typeof metadata.ohlc_bound_repair_first_date === "string"
      ? formatDate(metadata.ohlc_bound_repair_first_date)
      : null;
  const lastDate =
    typeof metadata.ohlc_bound_repair_last_date === "string"
      ? formatDate(metadata.ohlc_bound_repair_last_date)
      : null;

  return (
    <section className="ohlc-bound-repair-notice" role="note">
      <div>
        <span className="eyebrow">SOURCE OHLC REPAIR</span>
        <h3>已披露地修正 {repairs} 根上游高低价边界</h3>
        <p>{policy}</p>
      </div>
      <div>
        <strong>开盘 / 收盘保持原始值</strong>
        {(firstDate || lastDate) && (
          <small>
            首末异常：{firstDate ?? "未记录"} — {lastDate ?? "未记录"}
          </small>
        )}
      </div>
      <small>
        这只修复 OHLC 的逻辑包络，不会替换开收盘或把数据问题藏起来；若你的策略依赖日内高低价触发，须回到更适合的可交易数据源复核。
      </small>
    </section>
  );
}

function IndicatorWarmup({ payload }: { payload: BacktestPayload }) {
  const metadata = payload.data_metadata;
  const required = Number(
    metadata.indicator_warmup_required_bars ?? payload.strategy.warmup_bars ?? 0,
  );
  const available = Number(metadata.indicator_warmup_available_bars ?? 0);
  const complete = metadata.indicator_warmup_complete === true;
  const error =
    typeof metadata.indicator_warmup_error === "string"
      ? metadata.indicator_warmup_error
      : "";
  if (required <= 0) return null;

  return (
    <section className={`indicator-warmup ${complete ? "complete" : "partial"}`}>
      <div>
        <span className="eyebrow">INDICATOR PRE-ROLL</span>
        <h3>{complete ? "指标预热已隔离" : "指标预热数据不足"}</h3>
        <p>
          开始日期之前的 K 线只用于初始化指标，不参与收益、回撤、仓位变动次数或买入持有基准。
          {!complete && " 缺失的预热部分会保持空仓，不使用未来数据补齐。"}
        </p>
      </div>
      <div className="indicator-warmup-count">
        <strong>{complete ? available : `${available} / ${required}`}</strong>
        <span>{complete ? "实际获取的区间外 K 线" : "已获取 / 最低需要"}</span>
        {complete && <small>当前参数最低需要 {required} 根</small>}
        {error && <small title={error}>上游预热数据未完整返回</small>}
      </div>
    </section>
  );
}

function StrategyDiscovery({
  payload,
  onSelect,
  onSelectBaseline,
  guardrails,
}: {
  payload: StrategyDiscoveryPayload;
  onSelect: (candidate: StrategyDiscoveryCandidate) => void;
  onSelectBaseline: (backtest: BacktestPayload) => void;
  guardrails: {
    minimumTradesPerYear: number;
    maximumTradesPerYear: number;
    minimumExposure: number;
    minimumAnnualizedReturn: number;
    maximumDrawdown: number;
    maximumCashStreakBars: number;
  };
}) {
  const researchHorizon = payload.data_metadata.research_horizon as
    | {
        status: "adequate" | "short_horizon";
        validation_eligible: boolean;
        full_sample_days: number;
        minimum_full_sample_days: number;
        holdout_days: number;
        minimum_holdout_days: number;
        reason: string;
      }
    | undefined;
  const championHoldoutQuality =
    payload.champion?.backtest.optimization?.validation_trade_quality;
  const statusCopy = {
    validated_candidate: {
      label:
        championHoldoutQuality?.sample_quality === "insufficient"
          ? "留出约束通过 · 样本有限"
          : "留出验证通过",
      title: `找到 ${payload.champion?.strategy.name ?? "可用候选"}`,
      detail:
        championHoldoutQuality?.sample_quality === "insufficient"
          ? "候选通过最终留出期的收益、风险和参与度门槛，但未见交易仍少于 30 笔，需要纸面跟踪继续积累证据。"
          : "候选在开发集完成初筛和走步验证后，又通过了未参与选择的最终留出区间。",
    },
    provisional_candidate: {
      label: "未验证 · 可前向积累",
      title: `冻结 ${payload.provisional?.strategy.name ?? "临时候选"} 的参数`,
      detail:
        "候选已守住收益、风险、参与度和成本门槛，但最终留出只有 5–9 笔完整交易。它不是冠军；继续调参会污染留出集，因此只允许冻结参数，用激活后新数据补证据。",
    },
    no_validated_candidate: {
      label: "本轮没有冠军",
        title:
          payload.research_decision.mode === "development_rejected"
            ? "开发验证未通过 · 最终留出未触碰"
            : payload.research_decision.mode === "passive_baseline"
              ? "主动候选未过关 · 基准优先"
      : "开发期排名靠前候选未通过最终验证",
        detail:
          payload.research_decision.mode === "development_rejected"
            ? "短名单未通过开发期嵌套验证；系统保留最终留出期，不将最不差的失败候选送去碰运气。"
            : "仍展示本轮表现最好的主动候选供诊断，但不会把它包装成可靠冠军。",
    },
    development_rejected: {
      label: "开发验证未通过",
      title: "短名单已评估，但最终留出未触碰",
      detail:
        "候选没有通过开发期嵌套验证。系统保留最终未见样本，不把失败者送去二次挑选。",
    },
    constraints_too_strict: {
      label: "没有可优化候选",
      title:
        payload.research_decision.mode === "passive_baseline"
          ? "主动参数无解 · 基准优先"
          : "当前约束下没有合格参数",
      detail:
        "系统不会为得到结果而自动降低门槛；可以预先修改约束并开启一组新实验。",
    },
  }[payload.status];
  const regimeLabels = {
    trending: "趋势型开发样本",
    range_bound: "震荡型开发样本",
    mixed: "混合型开发样本",
    insufficient: "状态样本不足",
  } as const;
  const directionLabels = {
    rising: "净上涨",
    falling: "净下跌",
    flat: "净持平",
  } as const;
  const regimeFitLabels = {
    aligned: "状态适配",
    neutral: "状态中性",
    counter_regime: "逆状态范式",
  } as const;
  const objectiveLabels: Record<OptimizationObjective, string> = {
    balanced: "收益 / 回撤平衡",
    total_return: "累计收益",
    sharpe_ratio: "夏普比率",
    drawdown_control: "回撤控制",
  };
  const familyLabels = {
    mean_reversion: "均值回归",
    risk_managed_allocation: "风险管理配置",
    trend_or_breakout: "趋势 / 突破",
  } as const;
  const constraintSummary = payload.selection_protocol.screening_constraints;

  return (
    <section className={`strategy-discovery ${payload.status}`}>
      <div className="discovery-heading">
        <div>
          <span className="eyebrow">DISCOVER · WALK FORWARD · HOLDOUT</span>
          <div className="discovery-title-line">
            <h2>{statusCopy.title}</h2>
            <span className="discovery-status">{statusCopy.label}</span>
          </div>
          <p>{statusCopy.detail}</p>
        </div>
        <div className="source-row">
          {payload.citations.map((citation, index) => (
            <SourceBadge citation={citation} key={`${citation.source}-${index}`} />
          ))}
        </div>
      </div>

      <div className="discovery-protocol">
        <div>
          <span>模板初筛</span>
          <strong>{payload.selection_protocol.templates_screened}</strong>
          <small>
            仅开发样本 · {payload.selection_protocol.screening_parameter_sets_evaluated}/
            {payload.selection_protocol.screening_parameter_grid_total} 组参数 · 组合覆盖{" "}
            {formatPercent(payload.selection_protocol.screening_grid_coverage_ratio)}
          </small>
        </div>
        <i>→</i>
          <div>
            <span>候选寻优</span>
            <strong>{payload.selection_protocol.shortlisted}</strong>
            <small>
              {objectiveLabels[payload.objective]} · 入选覆盖 {payload.selection_protocol.shortlist_strategy_families
                .map((family) => familyLabels[family])
                .join(" / ")}
            </small>
            <small>
              开发期通过基础约束：{payload.selection_protocol.feasible_strategy_families.length > 0
                ? payload.selection_protocol.feasible_strategy_families
                    .map((family) => familyLabels[family])
                    .join(" / ")
                : "无（不会为凑齐策略族放宽约束）"}
            </small>
          </div>
        <i>→</i>
        <div>
          <span>参数寻优</span>
          <strong>{payload.selection_protocol.optimized}</strong>
          <small>
            开发验证通过 {payload.selection_protocol.development_validated} · 有资格最终审计 {payload.selection_protocol.development_selection_eligible}
          </small>
        </div>
        <i>→</i>
        <div className="holdout">
          <span>最终留出</span>
          <strong>{payload.selection_protocol.holdout_evaluated}</strong>
          <small>
            仅锁定候选 · {formatPercent(payload.selection_protocol.final_holdout_ratio)}
          </small>
        </div>
        <time>{(payload.elapsed_ms / 1_000).toFixed(1)}s</time>
      </div>
      <p className="discovery-boundary-note">
          最终留出期不参与筛选；若开发期末已形成仓位，首根留出 K 线会按同一下一根开盘规则承接该仓位、隔夜收益与换手成本。跨切分持仓的开发期盈亏不会混入留出期交易质量统计。
          已验证候选还须在最终留出期拥有至少 {payload.selection_protocol.final_holdout_min_closed_trades} 个留出期内新开启并闭合的独立决策周期；现金择时按“空仓→持仓→空仓”，核心+卫星配置按“加卫星仓位→回到核心仓位”统计。{payload.selection_protocol.forward_observation_min_closed_trades}–{payload.selection_protocol.final_holdout_min_closed_trades - 1} 个只可冻结参数继续观察，不能称为验证通过。
      </p>

      {constraintSummary.constraint_failures.length > 0 && (
        <section className="discovery-constraint-impact">
          <div className="constraint-impact-heading">
            <div>
              <span className="eyebrow">SCREENING CONSTRAINT IMPACT</span>
              <h3>本轮硬约束筛选影响</h3>
              <p>
                {constraintSummary.candidates_evaluated} 组开发样本参数中，仅有{" "}
                {constraintSummary.candidates_passing_base_constraints} 组同时通过基础门槛。
              </p>
            </div>
            <small>
              单组参数可能同时触发多条约束，以下计数不相加；它只解释本轮筛选，不证明放宽某一项就会得到可用策略。
            </small>
          </div>
          <div className="constraint-impact-list">
            {constraintSummary.constraint_failures.map((failure) => (
              <div key={failure.reason}>
                <span>{failure.reason}</span>
                <strong>{failure.failed_candidates} 组</strong>
                <small>{formatPercent(failure.failure_ratio)} 参数组合受影响</small>
              </div>
            ))}
          </div>
          {constraintSummary.candidates_passing_base_constraints === 0 && (
            <p className="constraint-impact-next-step">
              当前没有参数同时通过开发期门槛。先基于投资目的选择上方“研究目标预设”或调整 K 线周期，再启动一项新的独立实验；不要根据这次结果在同一留出样本上反复追调参数。
            </p>
          )}
        </section>
      )}

      <section className="development-market-regime">
        <div>
          <span className="eyebrow">DEVELOPMENT MARKET REGIME</span>
          <h3>{regimeLabels[payload.development_market_regime.classification]}</h3>
          <p>{payload.development_market_regime.reason}</p>
        </div>
        <dl>
          <div>
            <dt>净方向</dt>
            <dd>{directionLabels[payload.development_market_regime.direction]}</dd>
          </div>
          <div>
            <dt>净收益</dt>
            <dd>{formatPercent(payload.development_market_regime.net_return)}</dd>
          </div>
          <div>
            <dt>路径效率</dt>
            <dd>{formatPercent(payload.development_market_regime.path_efficiency)}</dd>
          </div>
        </dl>
        <small>
          仅用开发样本 {payload.development_market_regime.sample_bars} 根 K 线；状态适配只作同质量模板的轻微排序，不读取最终留出期，也不保证收益。
        </small>
      </section>

      {researchHorizon && (
        <div
          className={`discovery-horizon ${researchHorizon.status}`}
          role={researchHorizon.validation_eligible ? undefined : "status"}
        >
          <div>
            <span>研究覆盖度</span>
            <strong>
              {researchHorizon.validation_eligible ? "可作最终验证" : "仅探索，不设冠军"}
            </strong>
          </div>
          <p>
            完整样本 {researchHorizon.full_sample_days.toFixed(0)} / {researchHorizon.minimum_full_sample_days} 天
            · 留出期 {researchHorizon.holdout_days.toFixed(0)} / {researchHorizon.minimum_holdout_days} 天
          </p>
          <small>{researchHorizon.reason}</small>
        </div>
      )}

      <details className="discovery-screening">
        <summary>
          查看开发样本两阶段预筛：首轮最多{" "}
          {payload.selection_protocol.screening_initial_parameter_limit_per_template} 组，
          自适应扩展至 {payload.selection_protocol.screening_parameter_limit_per_template} 组
        </summary>
          <p>
            首轮确定性覆盖默认值、中心、边界和每个参数轴的全部档位；第二轮围绕开发期可行区域细化，同时保留全局探索。
            采样规则不为某个市场或代码写特例，但细化区域来自当前标的开发样本，不能直接外推到其他标的。
            预筛只决定哪些模板进入完整寻优；最终参数仍须通过完整参数网格、走步验证和完全未参与初筛的最终留出期。
          </p>
          <div className="discovery-guardrail-context">
            <strong>本轮基础约束</strong>
            <small>
              年化仓位变动 {guardrails.minimumTradesPerYear}–{guardrails.maximumTradesPerYear} 笔 · 持仓 ≥ {guardrails.minimumExposure}% · 连续空仓上限：{barsAsTime(guardrails.maximumCashStreakBars, payload.interval)} · 年化收益 ≥ {guardrails.minimumAnnualizedReturn}% · 回撤 ≤ {guardrails.maximumDrawdown}%
            </small>
            <small>下方为每个短名单模板在开发样本中的真实数值；这里只解释拒绝原因，不会自动放宽任何约束。</small>
          </div>
          <div>
          {payload.shortlist.map((item) => (
            <article key={item.strategy.id}>
              <strong>{item.strategy.name}</strong>
              <code>
                {Object.entries(item.strategy.parameters)
                  .map(([key, value]) => `${key}=${value}`)
                  .join(" · ")}
              </code>
                <span>
                  {item.screening_candidates_passing_base_constraints}/
                  {item.screening_candidates_evaluated} 组通过基础约束 · 完整网格{" "}
                  {item.screening_grid_total} 组 · 组合覆盖{" "}
                  {formatPercent(item.screening_grid_coverage_ratio)}
                </span>
                <small>
                  入选参数来自
                  {item.screening_selected_stage === "coverage"
                    ? "首轮覆盖"
                    : "第二轮自适应细化"}
                  {item.screening_budget.full_grid_evaluated
                    ? " · 小网格已完整扫描"
                    : ` · 本轮预算封顶 ${item.screening_budget.maximum} 组`}
                </small>
                <small className="screening-metrics">
                  开发样本：{item.metrics.trades_per_year.toFixed(1)} 笔/年 · 持仓 {formatPercent(item.metrics.exposure_ratio)} · 最长空仓 {barsAsTime(item.metrics.max_cash_streak, payload.interval)} · 收益 {formatPercent(item.metrics.total_return)} · 回撤 {formatPercent(item.metrics.max_drawdown)}
                </small>
                <small>
                参数档位覆盖：
                {Object.entries(item.screening_parameter_coverage)
                  .map(
                    ([key, coverage]) =>
                      `${PARAMETER_LABELS[key] ?? key} ${coverage.sampled_levels}/${coverage.available_levels}${coverage.extrema_covered ? "（含两端）" : ""}`,
                  )
                  .join(" · ")}
              </small>
              <small className={`market-regime-fit ${item.market_regime_fit}`}>
                {regimeFitLabels[item.market_regime_fit]} · {item.market_regime_fit_reason}
              </small>
              <small
                className={`interval-fit ${
                  item.interval_recommended ? "recommended" : "fallback"
                }`}
              >
                {item.interval_recommended
                  ? `适配当前 ${INTERVALS.find((interval) => interval.id === payload.interval)?.label ?? payload.interval} K 线 · 优先进入短名单`
                  : `非建议 ${INTERVALS.find((interval) => interval.id === payload.interval)?.label ?? payload.interval} K 线 · 仅在适配模板不满足基础约束时作为备选`}
              </small>
              {item.screening_constraint_reasons.length > 0 && (
                <small>{item.screening_constraint_reasons.join("；")}</small>
              )}
            </article>
          ))}
        </div>
      </details>

      {(payload.research_decision.mode === "development_rejected" ||
        payload.research_decision.mode === "passive_baseline" ||
        payload.research_decision.mode === "no_actionable_strategy") && (
        <div
          className={`passive-research-decision ${payload.research_decision.mode}`}
        >
          <div>
            <span className="eyebrow">RESEARCH DECISION · SIMPLE FIRST</span>
            <h3>{payload.research_decision.title}</h3>
            <p>{payload.research_decision.reason}</p>
            <small>
              买入持有不需要调参，以下是同一完整样本的历史证据；它仍不保证未来收益，也不是自动下单建议。
            </small>
          </div>
          <div className="passive-decision-metrics">
            <div>
              <span>同期累计收益</span>
              <strong
                className={
                  payload.passive_baseline.backtest.result.metrics.total_return < 0
                    ? "negative"
                    : ""
                }
              >
                {formatPercent(
                  payload.passive_baseline.backtest.result.metrics.total_return,
                )}
              </strong>
            </div>
            <div>
              <span>年化收益</span>
              <strong>
                {formatAnnualizedReturn(
                  payload.passive_baseline.backtest.result.metrics,
                )}
              </strong>
            </div>
            <div>
              <span>最大回撤</span>
              <strong className="negative">
                {formatPercent(
                  payload.passive_baseline.backtest.result.metrics.max_drawdown,
                )}
              </strong>
            </div>
            <div>
              <span>夏普比率</span>
              <strong>
                {payload.passive_baseline.backtest.result.metrics.sharpe_ratio.toFixed(
                  2,
                )}
              </strong>
            </div>
          </div>
          <button
            onClick={() => onSelectBaseline(payload.passive_baseline.backtest)}
            type="button"
          >
            查看买入持有完整证据
          </button>
          <div className="risk-budgeted-passive">
            <div>
              <span className="eyebrow">HISTORICAL RISK-BUDGET CALIBRATION</span>
              <h4>
                开发期历史回撤预算{" "}
                {formatPercent(
                  payload.passive_baseline.risk_budgeted
                    .requested_max_drawdown,
                )}{" "}
                → 初始投入{" "}
                {formatPercent(
                  payload.passive_baseline.risk_budgeted.target_exposure,
                )}
              </h4>
              <p>
                系统只在开发期寻找满足回撤预算的最高初始投入比例；首根开盘一次建仓后固定份额与剩余现金，不再为了维持比例而调仓。
                实际持仓率会随价格漂移。这是初始资金校准，不是择时，也不保证未来回撤不会突破预算。
              </p>
              <small>
                校准区间 {formatDate(payload.passive_baseline.risk_budgeted.calibration_start)} 至{" "}
                {formatDate(payload.passive_baseline.risk_budgeted.calibration_end)} · 最终留出后
                {payload.passive_baseline.risk_budgeted.full_sample_budget_satisfied
                  ? " 仍在历史预算内"
                  : " 已突破历史预算"}
              </small>
            </div>
              <div className="risk-budgeted-metrics">
                <div>
                  <span>初始投入</span>
                <strong>
                  {formatPercent(
                    payload.passive_baseline.risk_budgeted.target_exposure,
                  )}
                </strong>
                </div>
                <div>
                  <span>实际平均持仓</span>
                  <strong>
                    {formatPercent(
                      payload.passive_baseline.risk_budgeted.backtest.result
                        .metrics.exposure_ratio,
                    )}
                </strong>
              </div>
              <div>
                <span>历史收益</span>
                <strong>
                  {formatPercent(
                    payload.passive_baseline.risk_budgeted.backtest.result
                      .metrics.total_return,
                  )}
                </strong>
              </div>
              <div>
                <span>历史最大回撤</span>
                <strong className="negative">
                  {formatPercent(
                    payload.passive_baseline.risk_budgeted.backtest.result
                      .metrics.max_drawdown,
                  )}
                </strong>
              </div>
            </div>
            <button
              onClick={() =>
                onSelectBaseline(
                  payload.passive_baseline.risk_budgeted.backtest,
                )
              }
              type="button"
            >
              查看风险预算配置
            </button>
          </div>
        </div>
      )}

      {payload.selection_trials.length > 0 && (
        <div className="selection-trials">
          <span>开发区锁定顺序</span>
          {payload.selection_trials.map((trial, index) => (
            <small className={index === 0 ? "selected" : ""} key={trial.strategy.id}>
              {index + 1}. {trial.strategy.name}
              {trial.development_selection_eligible
                ? " · 送入最终留出"
                : " · 开发验证失败，最终留出未读取"}
              {" · "}
              {trial.development_validation_passed
                ? "开发期内部验证通过"
                : trial.development_validation_reason}
              {!trial.development_validation_passed &&
                trial.development_selection_eligible &&
                ` · 仅以不少于 ${payload.selection_protocol.development_selection_min_closed_trades} 个内部已闭合独立决策周期获得最终审计资格，不代表开发验证通过`}
              {" · 内部留出 "}
              {formatPercent(trial.development_validation_metrics.total_return)}
              {" · 初始投入基准超额 "}
              {formatPercent(trial.development_validation_metrics.timing_excess_return)}
              {" · 闭合周期 "}
              {trial.development_validation_metrics.closed_trades}
              {" 个 · "}
              {trial.development_validation_metrics.trades_per_year.toFixed(1)}
              {" 笔/年 · "}
              {trial.development_validation_metrics.cost_stress_passed
                ? "成本压力通过"
                : "成本压力未通过"}
            </small>
          ))}
        </div>
      )}

      {payload.candidates.length > 0 && (
        <div className="discovery-candidates">
          {payload.candidates.map((candidate, index) => {
            const validation = candidate.backtest.optimization;
            const isChampion =
              candidate.validation_passed &&
              payload.champion?.strategy.id === candidate.strategy.id;
            const isProvisional = candidate.forward_observation_eligible;
            return (
              <article
                className={
                  candidate.validation_passed
                    ? "passed"
                    : isProvisional
                      ? "provisional"
                      : "failed"
                }
                key={candidate.strategy.id}
              >
                <div className="candidate-rank">
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <i>
                    {isChampion
                      ? "CHAMPION"
                      : isProvisional
                        ? "PROVISIONAL"
                        : candidate.validation_passed
                          ? "PASSED"
                          : "FAILED"}
                  </i>
                </div>
                <div className="candidate-name">
                  <h3>{candidate.strategy.name}</h3>
                  <p>{candidate.strategy.category}</p>
                </div>
                {validation && (
                  <div className="candidate-metrics">
                    <div>
                      <span>留出收益</span>
                      <strong
                        className={validation.validation_metrics.total_return < 0 ? "negative" : ""}
                      >
                        {formatPercent(validation.validation_metrics.total_return)}
                      </strong>
                    </div>
                    <div>
                      <span>留出基准</span>
                      <strong>
                        {formatPercent(validation.validation_benchmark_metrics.total_return)}
                      </strong>
                    </div>
                    <div>
                      <span>初始投入匹配被动</span>
                      <strong>
                        {formatPercent(
                          validation
                            .validation_exposure_matched_benchmark_metrics
                            .total_return,
                        )}
                      </strong>
                    </div>
                    <div>
                      <span>择时超额</span>
                      <strong
                        className={
                          validation.validation_timing_excess_return < 0
                            ? "negative"
                            : ""
                        }
                      >
                        {formatPercent(
                          validation.validation_timing_excess_return,
                        )}
                      </strong>
                    </div>
                    <div>
                      <span>最大回撤</span>
                      <strong className="negative">
                        {formatPercent(validation.validation_metrics.max_drawdown)}
                      </strong>
                    </div>
                    <div>
                      <span>每年交易</span>
                      <strong>{validation.validation_metrics.trades_per_year.toFixed(1)}</strong>
                    </div>
                  </div>
                )}
                <p className="candidate-reason">{candidate.validation_reason}</p>
                <p className="candidate-next-action">
                  <b>下一步</b>
                  {holdoutNextAction(
                    candidate.validation_code,
                    candidate.forward_observation_eligible,
                  )}
                </p>
                <button onClick={() => onSelect(candidate)} type="button">
                  查看完整证据
                </button>
              </article>
            );
          })}
        </div>
      )}

      {payload.failures.length > 0 && (
        <details className="discovery-failures">
          <summary>{payload.failures.length} 个候选未满足约束</summary>
          {payload.failures.map((failure) => (
            <p key={failure.strategy_id}>
              <strong>{failure.strategy_name}</strong>
              {failure.reason}
            </p>
          ))}
        </details>
      )}

      <p className="discovery-disclaimer">
        “冠军”只表示它在本次标的、周期、成本和约束下通过了未见数据验证，不代表未来最优，也不是交易建议。
      </p>
    </section>
  );
}

function StrategyLeaderboard({
  payload,
  onSelect,
}: {
  payload: StrategyComparisonPayload;
  onSelect: (strategy: Strategy) => void;
}) {
  const qualityLabels = {
    outperform: "默认快照跑赢基准",
    defensive: "默认快照回撤防守",
    baseline: "买入持有基准",
    lagging: "默认快照未证明择时",
    negative: "默认快照负收益",
  };
  return (
    <section className="strategy-leaderboard">
      <div className="leaderboard-heading">
        <div>
          <span className="eyebrow">SAME DATA · SAME COST · SAME EXECUTION</span>
          <h2>模板默认参数快照</h2>
          <p>
            {payload.bars} 根 K 线；买入持有同期收益{" "}
            {formatPercent(payload.benchmark.metrics.total_return)}。{payload.evaluation_note}
            每个模板还会与“以策略平均持仓率作为初始投入、之后固定份额持有”的被动配置比较，避免把长期空仓误判成回撤控制能力。
          </p>
        </div>
        <div className="source-row">
          {payload.citations.map((citation, index) => (
            <SourceBadge citation={citation} key={`${citation.source}-${index}`} />
          ))}
        </div>
      </div>
      <InstrumentScopeNotice metadata={payload.data_metadata} />
      <OhlcBoundRepairNotice metadata={payload.data_metadata} />
      <div className="leaderboard-table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>策略</th>
              <th>判定</th>
              <th>累计收益</th>
              <th>择时超额</th>
              <th>平均持仓</th>
              <th>最大回撤</th>
              <th>每年交易</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {payload.results.map((row, index) => (
              <tr key={row.strategy.id}>
                <td>{String(index + 1).padStart(2, "0")}</td>
                <td>
                  <strong>{row.strategy.name}</strong>
                  <small>{row.strategy.category}</small>
                </td>
                <td>
                  <span className={`quality-badge ${row.quality}`}>
                    {qualityLabels[row.quality]}
                  </span>
                </td>
                <td className={row.metrics.total_return < 0 ? "negative" : "positive"}>
                  {formatPercent(row.metrics.total_return)}
                </td>
                <td
                  className={
                    row.timing_comparison.excess_return < 0
                      ? "negative"
                      : "positive"
                  }
                >
                  {formatPercent(row.timing_comparison.excess_return)}
                </td>
                <td>{formatPercent(row.metrics.exposure_ratio)}</td>
                <td className="negative">{formatPercent(row.metrics.max_drawdown)}</td>
                <td>{row.metrics.trades_per_year.toFixed(1)}</td>
                <td>
                  <button onClick={() => onSelect(row.strategy)} type="button">
                    载入此默认参数
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="leaderboard-note">
        这不是推荐排行榜：默认参数不参与自动寻优，也没有经过唯一最终留出验证。若要寻找可研究候选，请使用“一键发现并验证策略”。
      </p>
    </section>
  );
}

function Metric({
  label,
  value,
  negative = false,
}: {
  label: string;
  value: string;
  negative?: boolean;
}) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong className={negative ? "negative" : ""}>{value}</strong>
    </div>
  );
}
