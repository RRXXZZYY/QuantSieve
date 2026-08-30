import type {
  BarInterval,
  FactorHorizonDiagnostics,
  FactorId,
  FactorResearchAvailabilitySource,
  FactorResearchPayload,
  FactorResearchProviderCapabilities,
  FactorResearchRequest,
  Instrument,
} from "./types";

export const FACTOR_OPTIONS: ReadonlyArray<{
  id: FactorId;
  name: string;
  description: string;
}> = [
  {
    id: "momentum",
    name: "动量",
    description: "比较当前收盘价与回看窗口起点，检验近期强势是否延续。",
  },
  {
    id: "reversal",
    name: "短期反转",
    description: "对近期收益取反，检验短期超涨超跌后的均值回归。",
  },
  {
    id: "low_volatility",
    name: "低波动",
    description: "用历史收益波动率的相反数排序，检验低波动特征。",
  },
  {
    id: "volume_surprise",
    name: "成交量异动",
    description: "比较当前成交量与历史基准，检验放量后的前瞻差异。",
  },
] as const;

export type FactorIcSeries = {
  name: string;
  color: string;
  data: Array<[string, number]>;
};

export type FactorQuantileBars = {
  categories: string[];
  values: number[];
};

export type FactorCapabilityDisplay = {
  key: keyof FactorResearchProviderCapabilities;
  label: string;
  value: string;
  rawValue: string;
};

const FACTOR_CAPABILITY_LABELS: ReadonlyArray<
  [keyof FactorResearchProviderCapabilities, string]
> = [
  ["finalized_bars_only", "仅使用完结 K 线"],
  ["bar_finalization_policy", "完结规则"],
  ["bar_finalization_verified", "完结规则已验证"],
  ["exchange_clock_verified", "交易所时钟已验证"],
  ["reference_series", "参考序列"],
  ["tradable_quote", "可交易报价"],
  ["execution_ready", "可用于执行"],
  ["ohlc_derived_from_close", "OHLC 由收盘值派生"],
  ["fallback", "使用回退数据源"],
  ["price_basis", "价格口径"],
  ["repair_applied", "经过数据修复"],
  ["repaired_rows", "修复行数"],
  ["execution_note", "执行限制"],
] as const;

const FACTOR_AVAILABILITY_LABELS: Record<
  FactorResearchAvailabilitySource,
  string
> = {
  row_available_at: "逐行 available_at",
  row_finalized_at: "逐行 finalized_at",
  provider_policy_next_utc_day_estimate: "来源规则估算：次日 UTC",
};

function normalizedSymbol(value: string): string {
  return value.trim().toLocaleUpperCase();
}

function validIsoDate(value: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value));
}

export function parseForwardHorizons(value: string | number[]): number[] {
  const candidates = Array.isArray(value)
    ? value
    : value
        .split(/[,，\s]+/)
        .map((item) => item.trim())
        .filter(Boolean)
        .map(Number);
  if (
    candidates.length === 0 ||
    candidates.length > 4 ||
    candidates.some(
      (item) => !Number.isInteger(item) || item < 1 || item > 63,
    )
  ) {
    throw new RangeError("前瞻周期需要填写 1–4 个、范围为 1–63 的整数。");
  }
  const unique = [...new Set(candidates)].sort((left, right) => left - right);
  if (unique.length !== candidates.length) {
    throw new RangeError("前瞻周期不能重复。");
  }
  return unique;
}

export function buildFactorResearchRequest({
  instruments,
  factorId,
  lookbackBars,
  start,
  end,
  interval,
  forwardHorizons,
  quantileCount,
}: {
  instruments: Instrument[];
  factorId: FactorId;
  lookbackBars: number;
  start: string;
  end: string;
  interval: BarInterval;
  forwardHorizons: string | number[];
  quantileCount: number;
}): FactorResearchRequest {
  if (instruments.length < 3 || instruments.length > 20) {
    throw new RangeError("因子研究需要 3–20 个标的。");
  }
  if (!Number.isInteger(quantileCount) || quantileCount < 2 || quantileCount > 10) {
    throw new RangeError("分位数组数需要是 2–10 的整数。");
  }
  if (instruments.length < quantileCount) {
    throw new RangeError("标的数量不能少于分位数组数。");
  }
  if (!Number.isInteger(lookbackBars) || lookbackBars < 5 || lookbackBars > 252) {
    throw new RangeError("回看窗口需要是 5–252 根 K 线。");
  }
  if (!validIsoDate(start) || !validIsoDate(end) || start >= end) {
    throw new RangeError("开始日期必须早于结束日期。");
  }

  if (interval !== "1d") {
    throw new RangeError("当前因子研究只接受已完结日线。");
  }
  const identities = instruments.map(
    (instrument) => `${instrument.provider}:${normalizedSymbol(instrument.symbol)}`,
  );
  if (new Set(identities).size !== identities.length) {
    throw new RangeError("因子研究标的不能重复。");
  }

  return {
    instruments: instruments.map((instrument) => ({
      symbol: normalizedSymbol(instrument.symbol),
      provider: instrument.provider,
    })),
    factor_id: factorId,
    lookback: lookbackBars,
    horizons: parseForwardHorizons(forwardHorizons),
    quantiles: quantileCount,
    interval: "1d",
    start,
    end,
    finalized_bars_only: true,
  };
}

export function factorDecimal(value: string | null | undefined): number | null {
  if (typeof value !== "string" || !value.trim()) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

export function factorAvailabilitySourceLabel(
  source: FactorResearchAvailabilitySource,
): string {
  return FACTOR_AVAILABILITY_LABELS[source];
}

export function factorNumericInputBasisLabel(
  basis: "exact_provider_decimal" | "provider_numeric_projection",
): string {
  return basis === "exact_provider_decimal"
    ? "来源精确十进制"
    : "来源数值投影";
}

export function factorCapabilityEntries(
  capabilities: FactorResearchProviderCapabilities,
): FactorCapabilityDisplay[] {
  return FACTOR_CAPABILITY_LABELS.flatMap(([key, label]) => {
    const rawValue = capabilities[key];
    if (rawValue === undefined) return [];
    return [
      {
        key,
        label,
        value:
          typeof rawValue === "boolean"
            ? rawValue
              ? "是"
              : "否"
            : String(rawValue),
        rawValue: String(rawValue),
      },
    ];
  });
}

export function factorDroppedReasonEntries(
  reasons: Record<string, number> | undefined,
): Array<[string, number]> {
  if (!reasons) return [];
  return Object.entries(reasons).sort(([left], [right]) =>
    left.localeCompare(right),
  );
}

export function findFactorHorizon(
  payload: FactorResearchPayload,
  horizonBars: number,
): FactorHorizonDiagnostics | null {
  return payload.diagnostics_by_horizon[String(horizonBars)] ?? null;
}

export function buildFactorIcSeries(
  horizon: FactorHorizonDiagnostics | null,
): FactorIcSeries[] {
  if (!horizon) return [];
  const rankIc: Array<[string, number]> = [];
  const pearsonIc: Array<[string, number]> = [];
  for (const period of horizon.diagnostics.periods) {
    const rank = factorDecimal(period.rank_ic);
    const pearson = factorDecimal(period.pearson_ic);
    if (rank !== null) rankIc.push([period.period_at, rank]);
    if (pearson !== null) pearsonIc.push([period.period_at, pearson]);
  }
  return [
    { name: "Rank IC", color: "#55d6be", data: rankIc },
    { name: "Pearson IC", color: "#e7b96b", data: pearsonIc },
  ];
}

export function buildFactorQuantileBars(
  horizon: FactorHorizonDiagnostics | null,
): FactorQuantileBars {
  if (!horizon) return { categories: [], values: [] };
  const values = horizon.diagnostics.quantile_mean_returns
    .map(factorDecimal)
    .filter((value): value is number => value !== null);
  return {
    categories: values.map((_, index) => `Q${index + 1}`),
    values,
  };
}

export function factorRunReceiptHasExpired(
  expiresAt: string | undefined,
  now = Date.now(),
): boolean {
  if (!expiresAt) return false;
  const expiresAtMs = Date.parse(expiresAt);
  return Number.isFinite(expiresAtMs) && expiresAtMs <= now;
}
