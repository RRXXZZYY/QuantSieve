import { describe, expect, it } from "vitest";

import {
  buildPortfolioExperimentFromRunPayload,
  buildPortfolioExperimentPayload,
  buildPortfolioRequest,
  defaultPortfolioExperimentName,
  hydratePortfolioExperiment,
  portfolioDefaultDates,
  portfolioExperimentToPayload,
  portfolioMethodCopy,
  portfolioRequestFingerprint,
  summarizePortfolioExperiment,
} from "./portfolio-experiments";
import type {
  Instrument,
  PortfolioBacktestPayload,
  PortfolioExperimentRecord,
  PortfolioMethod,
  PortfolioResult,
} from "./types";

const ASSETS: Instrument[] = [
  {
    symbol: "BTCUSDT",
    name: "Bitcoin / USDT",
    market: "CRYPTO",
    exchange: "Binance Spot",
    currency: "USDT",
    provider: "binance",
    asset_type: "crypto",
  },
  {
    symbol: "NDX",
    name: "NASDAQ-100",
    market: "INDEX",
    exchange: "Nasdaq",
    currency: "USD",
    provider: "macro",
    asset_type: "index",
  },
  {
    symbol: "CL",
    name: "NYMEX 原油",
    market: "FUTURES",
    exchange: "NYMEX",
    currency: "USD",
    provider: "futures",
    asset_type: "commodity_future",
  },
  {
    symbol: "GC",
    name: "COMEX 黄金",
    market: "FUTURES",
    exchange: "COMEX",
    currency: "USD",
    provider: "futures",
    asset_type: "commodity_future",
  },
];

function request() {
  return buildPortfolioRequest({
    assets: ASSETS,
    start: "2021-07-26",
    end: "2026-07-26",
    volatilityLookback: 60,
    rebalanceBars: 21,
    maximumWeightPercent: 40,
  });
}

function portfolioResult(
  method: PortfolioMethod,
  totalReturn: number,
  maxDrawdown: number,
  sharpeRatio: number,
): PortfolioResult {
  const weights = Object.fromEntries(
    ASSETS.map((asset) => [asset.symbol, 1 / ASSETS.length]),
  );
  return {
    method,
    metrics: {
      bars: 1_000,
      duration_years: 4,
      total_return: totalReturn,
      annualized_return: 0.11,
      annualized_return_capped: false,
      annualized_return_cap: 1_000_000,
      annualized_volatility: 0.18,
      sharpe_ratio: sharpeRatio,
      max_drawdown: maxDrawdown,
      rebalances: 48,
      turnover_ratio: 1.2,
      transaction_cost_ratio: 0.003,
    },
    equity: [
      { date: "2021-07-26", equity: 100_000, return: 0 },
      { date: "2026-07-26", equity: 100_000 * (1 + totalReturn), return: 0.01 },
    ],
    allocations: [
      {
        date: "2021-07-26",
        weights,
        turnover: 1,
        cost: 100,
      },
    ],
    last_rebalance_target_weights: weights,
    ending_realized_weights: {
      ...weights,
      BTCUSDT: 0.28,
      NDX: 0.22,
    },
    latest_weights: weights,
    config: {
      initial_cash: 100_000,
      fee_rate: 0.0003,
      slippage_rate: 0.0002,
      annual_periods: 252,
      bar_interval: "1d",
      signal_delay_bars: 1,
    },
  };
}

function runPayload(): PortfolioBacktestPayload {
  const results = {
    initial_equal_hold: portfolioResult(
      "initial_equal_hold",
      0.35,
      -0.32,
      0.7,
    ),
    periodic_equal: portfolioResult("periodic_equal", 0.4, -0.28, 0.8),
    periodic_inverse_volatility: portfolioResult(
      "periodic_inverse_volatility",
      0.46,
      -0.22,
      0.95,
    ),
  };
  return {
    run_id: "0123456789abcdef0123456789abcdef",
    run_expires_at: "2026-07-26T12:10:00Z",
    calculation_version: "test-v1",
    assets: ASSETS.map((asset, index) => ({
      symbol: index === 0 ? "BTC-USDT" : asset.symbol,
      requested_symbol: asset.symbol,
      provider: asset.provider,
      currency: asset.currency,
      metadata: { adapter: asset.provider, index },
    })),
    start: "2021-07-26",
    end: "2026-07-26",
    common_bars: 1_000,
    data_quality: {
      actual_start: "2021-07-26",
      actual_end: "2026-07-25",
      annual_periods: 252,
      source_bars: Object.fromEntries(
        ASSETS.map((asset) => [asset.symbol, 1_050]),
      ),
      alignment: "common_daily_session_labels",
      valuation_limit: "USD valuation group; no FX conversion.",
    },
    assumptions: {
      volatility_lookback: 60,
      rebalance_bars: 21,
      maximum_asset_weight: 0.4,
      fee_rate: 0.0003,
      slippage_rate: 0.0002,
      cash_return: 0,
      execution: "next_common_session_open_proxy",
    },
    results,
    segments: [
      {
        index: 1,
        start: "2021-07-26",
        end: "2022-10-26",
        results,
      },
    ],
    research_decision: {
      risk_evidence_passed: true,
      drawdown_improved_segments: 1,
      sharpe_improved_segments: 1,
      evaluable_segments: 1,
      total_segments: 1,
      evidence_checks: {
        all_segments_evaluable: true,
        all_segments_drawdown_strictly_improved: true,
        majority_segments_sharpe_improved: true,
        full_sample_sharpe_improved: true,
        full_sample_positive_return: true,
        full_sample_invested: true,
        minimum_segment_bars: 63,
      },
      title: "风险证据通过",
      reason: "严格回撤改善且夏普更高。",
    },
    citations: [{ source: "fixture", url: "https://example.com/data" }],
  };
}

function experimentRecord(): PortfolioExperimentRecord {
  const payload = buildPortfolioExperimentPayload({
    name: "全球多资产验证",
    notes: "固定证据快照",
    focusMethod: "periodic_inverse_volatility",
    run: { request: request(), assets: ASSETS, payload: runPayload() },
    calculationVersion: "test-v1",
  });
  return {
    ...payload,
    id: "portfolio-fixture",
    source_run_id: "0123456789abcdef0123456789abcdef",
    created_at: "2026-07-26T12:00:00Z",
    updated_at: "2026-07-26T12:00:00Z",
  };
}

describe("portfolio experiment helpers", () => {
  it("uses the local calendar day and starts five years earlier", () => {
    expect(portfolioDefaultDates(new Date(2026, 6, 26, 23, 30))).toEqual({
      start: "2021-07-26",
      end: "2026-07-26",
    });
  });

  it("clamps a leap-day start to the final valid day of the target month", () => {
    expect(portfolioDefaultDates(new Date(2024, 1, 29, 12))).toEqual({
      start: "2019-02-28",
      end: "2024-02-29",
    });
  });

  it("builds a frozen-shape API request without retaining instrument objects", () => {
    const built = request();

    expect(built).toEqual({
      assets: [
        { symbol: "BTCUSDT", provider: "binance", currency: "USDT" },
        { symbol: "NDX", provider: "macro", currency: "USD" },
        { symbol: "CL", provider: "futures", currency: "USD" },
        { symbol: "GC", provider: "futures", currency: "USD" },
      ],
      start: "2021-07-26",
      end: "2026-07-26",
      volatility_lookback: 60,
      rebalance_bars: 21,
      maximum_asset_weight: 0.4,
    });
    expect(built.assets[0]).not.toBe(ASSETS[0]);
  });

  it("fingerprints equivalent request semantics and detects rule changes", () => {
    const built = request();
    const equivalent = {
      ...built,
      assets: built.assets.map((asset) => ({
        ...asset,
        symbol: asset.symbol.toLowerCase(),
        currency: asset.currency.toLowerCase(),
      })),
    };

    expect(portfolioRequestFingerprint(equivalent)).toBe(
      portfolioRequestFingerprint(built),
    );
    expect(
      portfolioRequestFingerprint({ ...built, rebalance_bars: 22 }),
    ).not.toBe(portfolioRequestFingerprint(built));
  });

  it("creates a compact default name from the exact request scope", () => {
    expect(defaultPortfolioExperimentName(request())).toBe(
      "BTCUSDT 等 4 资产 · 逆波动配置 · 2021–2026",
    );
  });

  it("describes each method from the actual asset count and assumptions", () => {
    const assumptions = {
      volatility_lookback: 45,
      rebalance_bars: 10,
      maximum_asset_weight: 0.5,
    };

    expect(
      portfolioMethodCopy("initial_equal_hold", 3, assumptions).note,
    ).toContain("3 个资产各投 33.33%");
    expect(portfolioMethodCopy("periodic_equal", 3, assumptions).note).toContain(
      "每 10 个共同交易日",
    );
    const inverse = portfolioMethodCopy(
      "periodic_inverse_volatility",
      3,
      assumptions,
    ).note;
    expect(inverse).toContain("45 根 K 线");
    expect(inverse).toContain("不超过 50%");
  });

  it("builds a canonical full snapshot while preserving requested symbols and display metadata", () => {
    const built = buildPortfolioExperimentPayload({
      name: "  全球多资产验证  ",
      notes: "  保留原始请求  ",
      focusMethod: "periodic_inverse_volatility",
      run: { request: request(), assets: ASSETS, payload: runPayload() },
    });

    expect(built.name).toBe("全球多资产验证");
    expect(built.notes).toBe("保留原始请求");
    expect(built.assets[0]).toMatchObject({
      symbol: "BTC-USDT",
      requested_symbol: "BTCUSDT",
      name: "Bitcoin / USDT",
      market: "CRYPTO",
      exchange: "Binance Spot",
      provider: "binance",
      currency: "USDT",
      metadata: { adapter: "binance", index: 0 },
    });
    expect(built.run_request).toEqual(request());
    expect(built.run_request).not.toBe(request());
  });

  it("builds the exact server-receipt save body without client-authored result fields", () => {
    const built = buildPortfolioExperimentFromRunPayload({
      name: "  回执验证  ",
      notes: "",
      focusMethod: "periodic_equal",
      run: { request: request(), assets: ASSETS, payload: runPayload() },
    });

    expect(built).toEqual({
      run_id: "0123456789abcdef0123456789abcdef",
      name: "回执验证",
      notes: null,
      focus_method: "periodic_equal",
      assets: [
        {
          symbol: "BTC-USDT",
          requested_symbol: "BTCUSDT",
          name: "Bitcoin / USDT",
          market: "CRYPTO",
          exchange: "Binance Spot",
          asset_type: "crypto",
        },
        ...ASSETS.slice(1).map((asset) => ({
          symbol: asset.symbol,
          requested_symbol: asset.symbol,
          name: asset.name,
          market: asset.market,
          exchange: asset.exchange,
          asset_type: asset.asset_type,
        })),
      ],
    });
    expect(built).not.toHaveProperty("results");
    expect(built.assets[0]).not.toHaveProperty("provider");
    expect(built.assets[0]).not.toHaveProperty("currency");
  });

  it("hydrates the original request and editable controls from a saved detail record", () => {
    const record = experimentRecord();
    const hydrated = hydratePortfolioExperiment(record);

    expect(hydrated.request).toEqual(record.run_request);
    expect(hydrated.request).not.toBe(record.run_request);
    expect(hydrated.assets.map((asset) => asset.symbol)).toEqual(
      ASSETS.map((asset) => asset.symbol),
    );
    expect(hydrated.assets[0]).toMatchObject({
      symbol: "BTCUSDT",
      name: "Bitcoin / USDT",
      provider: "binance",
    });
    expect(hydrated).toMatchObject({
      start: "2021-07-26",
      end: "2026-07-26",
      volatilityLookback: 60,
      rebalanceBars: 21,
      maximumWeightPercent: 40,
    });
  });

  it("converts a saved detail to a display payload without recalculating it", () => {
    const record = experimentRecord();
    const payload = portfolioExperimentToPayload(record);

    expect(payload.results).toBe(record.results);
    expect(payload.segments).toBe(record.segments);
    expect(payload.assets[0]).toEqual({
      symbol: "BTC-USDT",
      requested_symbol: "BTCUSDT",
      provider: "binance",
      currency: "USDT",
      metadata: { adapter: "binance", index: 0 },
    });
    expect(payload).not.toHaveProperty("run_id");
  });

  it("summarizes the selected focus method rather than a hard-coded strategy", () => {
    const record = {
      ...experimentRecord(),
      focus_method: "periodic_equal" as const,
    };
    const summary = summarizePortfolioExperiment(record);

    expect(summary.focus_method).toBe("periodic_equal");
    expect(summary.total_return).toBe(
      record.results.periodic_equal.metrics.total_return,
    );
    expect(summary.max_drawdown).toBe(
      record.results.periodic_equal.metrics.max_drawdown,
    );
    expect(summary.sharpe_ratio).toBe(
      record.results.periodic_equal.metrics.sharpe_ratio,
    );
    expect(summary.source_run_id).toBe(record.source_run_id);
  });
});
