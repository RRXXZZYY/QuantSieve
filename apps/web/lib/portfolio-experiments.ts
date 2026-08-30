import type {
  Instrument,
  PortfolioBacktestPayload,
  PortfolioBacktestRequest,
  PortfolioExperimentAsset,
  PortfolioExperimentCreatePayload,
  PortfolioExperimentFromRunPayload,
  PortfolioExperimentRecord,
  PortfolioExperimentSummary,
  PortfolioMethod as PortfolioMethodType,
} from "./types";

export type PortfolioBacktestRequestSnapshot = PortfolioBacktestRequest;
export type PortfolioMethod = PortfolioMethodType;

export type PortfolioRunSnapshot = {
  request: PortfolioBacktestRequestSnapshot;
  assets: Instrument[];
  payload: PortfolioBacktestPayload;
};

export type HydratedPortfolioExperiment = {
  request: PortfolioBacktestRequestSnapshot;
  assets: Instrument[];
  start: string;
  end: string;
  volatilityLookback: number;
  rebalanceBars: number;
  maximumWeightPercent: number;
};

export type PortfolioRequestDraft = {
  assets: readonly Instrument[];
  start: string;
  end: string;
  volatilityLookback: number;
  rebalanceBars: number;
  maximumWeightPercent: number;
};

type PortfolioRuleAssumptions = Pick<
  PortfolioBacktestPayload["assumptions"],
  "volatility_lookback" | "rebalance_bars" | "maximum_asset_weight"
>;

const METHOD_NAMES: Record<PortfolioMethod, string> = {
  initial_equal_hold: "起点等权 · 不再平衡",
  periodic_equal: "定期等权",
  periodic_inverse_volatility: "逆波动风险配置",
};

function localIsoDate(value: Date): string {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function compactPercent(value: number): string {
  const formatted = (value * 100).toFixed(2).replace(/\.?0+$/, "");
  return `${formatted}%`;
}

export function portfolioDefaultDates(referenceDate = new Date()): {
  start: string;
  end: string;
} {
  if (Number.isNaN(referenceDate.getTime())) {
    throw new RangeError("A valid local date is required.");
  }
  const startYear = referenceDate.getFullYear() - 5;
  const month = referenceDate.getMonth();
  const lastDayOfStartMonth = new Date(startYear, month + 1, 0).getDate();
  const startDay = Math.min(referenceDate.getDate(), lastDayOfStartMonth);
  const startDate = new Date(startYear, month, startDay);
  return {
    start: localIsoDate(startDate),
    end: localIsoDate(referenceDate),
  };
}

export function buildPortfolioRequest({
  assets,
  start,
  end,
  volatilityLookback,
  rebalanceBars,
  maximumWeightPercent,
}: PortfolioRequestDraft): PortfolioBacktestRequestSnapshot {
  return {
    assets: assets.map((asset) => ({
      symbol: asset.symbol,
      provider: asset.provider,
      currency: asset.currency,
    })),
    start,
    end,
    volatility_lookback: volatilityLookback,
    rebalance_bars: rebalanceBars,
    maximum_asset_weight: maximumWeightPercent / 100,
  };
}

export function portfolioRequestFingerprint(
  request: PortfolioBacktestRequestSnapshot,
): string {
  return JSON.stringify({
    assets: request.assets.map((asset) => [
      asset.symbol.trim().toUpperCase(),
      asset.provider,
      asset.currency.trim().toUpperCase(),
    ]),
    start: request.start,
    end: request.end,
    volatility_lookback: request.volatility_lookback,
    rebalance_bars: request.rebalance_bars,
    maximum_asset_weight: request.maximum_asset_weight,
  });
}

export function defaultPortfolioExperimentName(
  request: PortfolioBacktestRequestSnapshot,
): string {
  const symbols = request.assets.map((asset) => asset.symbol.trim().toUpperCase());
  const universe =
    symbols.length === 0
      ? "未命名组合"
      : symbols.length <= 3
        ? symbols.join(" / ")
        : `${symbols[0]} 等 ${symbols.length} 资产`;
  const startYear = request.start.slice(0, 4);
  const endYear = request.end.slice(0, 4);
  const period =
    startYear && endYear
      ? startYear === endYear
        ? startYear
        : `${startYear}–${endYear}`
      : "自定义区间";
  return `${universe} · 逆波动配置 · ${period}`;
}

export function portfolioMethodCopy(
  method: PortfolioMethod,
  assetCount: number,
  assumptions: PortfolioRuleAssumptions,
): { name: string; note: string } {
  const normalizedAssetCount = Math.max(1, Math.trunc(assetCount));
  const equalWeight = compactPercent(1 / normalizedAssetCount);
  const notes: Record<PortfolioMethod, string> = {
    initial_equal_hold: `只在起点为 ${normalizedAssetCount} 个资产各投 ${equalWeight}，之后让资产自然漂移。`,
    periodic_equal: `每 ${assumptions.rebalance_bars} 个共同交易日恢复等权，每个资产目标权重 ${equalWeight}。`,
    periodic_inverse_volatility: `只用前一日以前 ${assumptions.volatility_lookback} 根 K 线，低波动资产获得更高权重，单资产不超过 ${compactPercent(
      assumptions.maximum_asset_weight,
    )}。`,
  };
  return {
    name: METHOD_NAMES[method],
    note: notes[method],
  };
}

function clonePortfolioRequest(
  request: PortfolioBacktestRequestSnapshot,
): PortfolioBacktestRequestSnapshot {
  return {
    ...request,
    assets: request.assets.map((asset) => ({ ...asset })),
    ...(request.config
      ? {
          config: { ...request.config },
        }
      : {}),
  };
}

function requestedSymbol(value: string): string {
  return value.trim().toUpperCase();
}

export function buildPortfolioExperimentPayload({
  name,
  notes,
  focusMethod,
  run,
  calculationVersion,
}: {
  name: string;
  notes?: string | null;
  focusMethod: PortfolioMethod;
  run: PortfolioRunSnapshot;
  calculationVersion?: string;
}): PortfolioExperimentCreatePayload {
  const assets: PortfolioExperimentAsset[] = run.payload.assets.map(
    (canonical, index) => {
      const requested = run.request.assets[index];
      const instrument = run.assets[index];
      if (!requested || !instrument) {
        throw new RangeError("组合结果、请求和资产快照必须保持相同顺序与数量。");
      }
      return {
        symbol: canonical.symbol.trim().toUpperCase(),
        requested_symbol: requestedSymbol(
          canonical.requested_symbol ?? requested.symbol,
        ),
        name: instrument.name,
        market: instrument.market,
        exchange: instrument.exchange,
        currency: canonical.currency.trim().toUpperCase(),
        provider: canonical.provider as PortfolioExperimentAsset["provider"],
        asset_type: instrument.asset_type,
        metadata: { ...canonical.metadata },
      };
    },
  );
  if (
    assets.length !== run.assets.length ||
    assets.length !== run.request.assets.length
  ) {
    throw new RangeError("组合结果、请求和资产快照必须保持相同顺序与数量。");
  }
  return {
    schema_version: 1,
    kind: "portfolio",
    name: name.trim(),
    notes: notes?.trim() || null,
    assets,
    interval: "1d",
    start: run.request.start,
    end: run.request.end,
    focus_method: focusMethod,
    run_request: clonePortfolioRequest(run.request),
    data_quality: { ...run.payload.data_quality },
    assumptions: { ...run.payload.assumptions },
    common_bars: run.payload.common_bars,
    results: run.payload.results,
    segments: run.payload.segments,
    research_decision: run.payload.research_decision,
    citations: run.payload.citations,
    calculation_version:
      calculationVersion ?? run.payload.calculation_version ?? "",
  };
}

export function buildPortfolioExperimentFromRunPayload({
  name,
  notes,
  focusMethod,
  run,
}: {
  name: string;
  notes?: string | null;
  focusMethod: PortfolioMethod;
  run: PortfolioRunSnapshot;
}): PortfolioExperimentFromRunPayload {
  if (!run.payload.run_id) {
    throw new RangeError("本次结果没有可验证的服务端运行回执，请重新运行后再保存。");
  }
  const assets = run.payload.assets.map((canonical, index) => {
    const requested = run.request.assets[index];
    const instrument = run.assets[index];
    if (!requested || !instrument) {
      throw new RangeError("组合结果、请求和资产快照必须保持相同顺序与数量。");
    }
    return {
      symbol: canonical.symbol.trim().toUpperCase(),
      requested_symbol: requestedSymbol(
        canonical.requested_symbol ?? requested.symbol,
      ),
      name: instrument.name,
      market: instrument.market,
      exchange: instrument.exchange,
      asset_type: instrument.asset_type,
    };
  });
  if (
    assets.length !== run.assets.length ||
    assets.length !== run.request.assets.length
  ) {
    throw new RangeError("组合结果、请求和资产快照必须保持相同顺序与数量。");
  }
  return {
    run_id: run.payload.run_id,
    name: name.trim(),
    notes: notes?.trim() || null,
    focus_method: focusMethod,
    assets,
  };
}

export function hydratePortfolioExperiment(
  record: PortfolioExperimentRecord,
): HydratedPortfolioExperiment {
  const topAssetsByRequestedSymbol = new Map(
    record.assets.map((asset) => [
      requestedSymbol(asset.requested_symbol ?? asset.symbol),
      asset,
    ]),
  );
  const assets = record.run_request.assets.map((requested) => {
    const topAsset = topAssetsByRequestedSymbol.get(
      requestedSymbol(requested.symbol),
    );
    if (!topAsset) {
      throw new RangeError("档案中的请求资产与保存资产无法对应。");
    }
    return {
      symbol: requested.symbol,
      name: topAsset.name || topAsset.symbol,
      market: topAsset.market,
      exchange: topAsset.exchange,
      currency: requested.currency,
      provider:
        requested.provider === "auto" ? topAsset.provider : requested.provider,
      asset_type: topAsset.asset_type,
    };
  });
  return {
    request: clonePortfolioRequest(record.run_request),
    assets,
    start: record.run_request.start,
    end: record.run_request.end,
    volatilityLookback: record.run_request.volatility_lookback,
    rebalanceBars: record.run_request.rebalance_bars,
    maximumWeightPercent: record.run_request.maximum_asset_weight * 100,
  };
}

export function portfolioExperimentToPayload(
  record: PortfolioExperimentRecord,
): PortfolioBacktestPayload {
  return {
    calculation_version: record.calculation_version,
    assets: record.assets.map((asset) => ({
      symbol: asset.symbol,
      requested_symbol: asset.requested_symbol,
      provider: asset.provider,
      currency: asset.currency,
      metadata: asset.metadata,
    })),
    start: record.start,
    end: record.end,
    common_bars: record.common_bars,
    data_quality: record.data_quality,
    assumptions: record.assumptions,
    results: record.results,
    segments: record.segments,
    research_decision: record.research_decision,
    citations: record.citations,
  };
}

export function summarizePortfolioExperiment(
  record: PortfolioExperimentRecord,
): PortfolioExperimentSummary {
  const metrics = record.results[record.focus_method].metrics;
  return {
    schema_version: 1,
    kind: "portfolio",
    id: record.id,
    source_run_id: record.source_run_id,
    name: record.name,
    notes: record.notes,
    symbols: record.assets.map((asset) => asset.symbol),
    asset_count: record.assets.length,
    interval: "1d",
    start: record.start,
    end: record.end,
    focus_method: record.focus_method,
    common_bars: record.common_bars,
    total_return: metrics.total_return,
    max_drawdown: metrics.max_drawdown,
    sharpe_ratio: metrics.sharpe_ratio,
    risk_evidence_passed: record.research_decision.risk_evidence_passed,
    created_at: record.created_at,
    updated_at: record.updated_at,
  };
}
