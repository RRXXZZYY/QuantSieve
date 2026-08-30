import { describe, expect, it } from "vitest";

import {
  buildFactorIcSeries,
  buildFactorQuantileBars,
  buildFactorResearchRequest,
  factorAvailabilitySourceLabel,
  factorCapabilityEntries,
  factorDecimal,
  factorDroppedReasonEntries,
  factorNumericInputBasisLabel,
  factorRunReceiptHasExpired,
  parseForwardHorizons,
} from "./factor-research";
import type {
  FactorHorizonDiagnostics,
  Instrument,
} from "./types";

const US_ASSETS: Instrument[] = ["SPY", "QQQ", "IWM"].map((symbol) => ({
  symbol,
  name: symbol,
  market: "ETF",
  exchange: "NYSE Arca",
  currency: "USD",
  provider: "yfinance",
  asset_type: "etf",
}));

const HORIZON: FactorHorizonDiagnostics = {
  panel_id: "a".repeat(64),
  diagnostics: {
    schema_version: 1,
    diagnostics_id: "b".repeat(64),
    panel_id: "a".repeat(64),
    feature_name: "momentum.20",
    label_name: "next-open-to-close.5-bars",
    quantile_count: 3,
    period_count: 2,
    observation_count: 6,
    coverage_rate: "1",
    pearson_ic_mean: "0.15",
    pearson_ic_volatility: "0.05",
    pearson_ic_information_ratio: "3",
    rank_ic_mean: "0.2",
    rank_ic_volatility: "0.1",
    rank_ic_information_ratio: "2",
    quantile_mean_returns: ["-0.02", "0.01", "0.04"],
    long_short_mean_return: "0.06",
    long_short_volatility: "0.02",
    long_short_information_ratio: "3",
    average_turnover: "0.4",
    quantile_monotonicity: "0.95",
    periods: [
      {
        period_at: "2026-01-01T00:00:00Z",
        eligible_count: 3,
        sample_count: 3,
        coverage_rate: "1",
        pearson_ic: "0.4",
        rank_ic: "0.5",
        quantile_returns: ["-0.01", "0.01", "0.03"],
        long_short_return: "0.04",
        long_short_turnover: null,
      },
      {
        period_at: "2026-01-02T00:00:00Z",
        eligible_count: 3,
        sample_count: 3,
        coverage_rate: "1",
        pearson_ic: "-0.1",
        rank_ic: "-0.2",
        quantile_returns: ["-0.03", "0.01", "0.05"],
        long_short_return: "0.08",
        long_short_turnover: "0.4",
      },
    ],
  },
  dropped_dates: [],
};

describe("factor research requests", () => {
  it("normalizes the universe and matches the API contract", () => {
    expect(
      buildFactorResearchRequest({
        instruments: US_ASSETS,
        factorId: "momentum",
        lookbackBars: 20,
        start: "2024-01-01",
        end: "2026-01-01",
        interval: "1d",
        forwardHorizons: "20, 1, 5",
        quantileCount: 3,
      }),
    ).toEqual({
      instruments: [
        { symbol: "SPY", provider: "yfinance" },
        { symbol: "QQQ", provider: "yfinance" },
        { symbol: "IWM", provider: "yfinance" },
      ],
      factor_id: "momentum",
      lookback: 20,
      horizons: [1, 5, 20],
      quantiles: 3,
      interval: "1d",
      start: "2024-01-01",
      end: "2026-01-01",
      finalized_bars_only: true,
    });
  });

  it("rejects duplicate and undersized universes but allows mixed providers", () => {
    expect(() =>
      buildFactorResearchRequest({
        instruments: [US_ASSETS[0], US_ASSETS[0], US_ASSETS[1]],
        factorId: "momentum",
        lookbackBars: 20,
        start: "2024-01-01",
        end: "2026-01-01",
        interval: "1d",
        forwardHorizons: [1],
        quantileCount: 3,
      }),
    ).toThrow("不能重复");

    const crypto = {
      ...US_ASSETS[2],
      symbol: "BTCUSDT",
      provider: "binance" as const,
      market: "CRYPTO" as const,
    };
    expect(
      buildFactorResearchRequest({
        instruments: [US_ASSETS[0], US_ASSETS[1], crypto],
        factorId: "momentum",
        lookbackBars: 20,
        start: "2024-01-01",
        end: "2026-01-01",
        interval: "1d",
        forwardHorizons: [1],
        quantileCount: 3,
      }).instruments,
    ).toEqual([
      { symbol: "SPY", provider: "yfinance" },
      { symbol: "QQQ", provider: "yfinance" },
      { symbol: "BTCUSDT", provider: "binance" },
    ]);

    expect(() =>
      buildFactorResearchRequest({
        instruments: US_ASSETS.slice(0, 2),
        factorId: "momentum",
        lookbackBars: 20,
        start: "2024-01-01",
        end: "2026-01-01",
        interval: "1d",
        forwardHorizons: [1],
        quantileCount: 2,
      }),
    ).toThrow("3–20");
  });

  it("parses unique bounded horizons", () => {
    expect(parseForwardHorizons("5， 1 20")).toEqual([1, 5, 20]);
    expect(parseForwardHorizons("1, 5, 20, 63")).toEqual([1, 5, 20, 63]);
    expect(() => parseForwardHorizons("1, 2, 3, 4, 5")).toThrow("1–4");
    expect(() => parseForwardHorizons("1,1")).toThrow("不能重复");
    expect(() => parseForwardHorizons("0,5")).toThrow("1–63");
  });
});

describe("factor research projections", () => {
  it("converts canonical decimals only at the display boundary", () => {
    expect(factorDecimal("-0.012500000000")).toBe(-0.0125);
    expect(factorDecimal(null)).toBeNull();
    expect(factorDecimal("NaN")).toBeNull();
    expect(factorDecimal("Infinity")).toBeNull();
  });

  it("maps diagnostic periods and quantiles into chart data", () => {
    expect(buildFactorIcSeries(HORIZON)).toEqual([
      {
        name: "Rank IC",
        color: "#55d6be",
        data: [
          ["2026-01-01T00:00:00Z", 0.5],
          ["2026-01-02T00:00:00Z", -0.2],
        ],
      },
      {
        name: "Pearson IC",
        color: "#e7b96b",
        data: [
          ["2026-01-01T00:00:00Z", 0.4],
          ["2026-01-02T00:00:00Z", -0.1],
        ],
      },
    ]);
    expect(buildFactorQuantileBars(HORIZON)).toEqual({
      categories: ["Q1", "Q2", "Q3"],
      values: [-0.02, 0.01, 0.04],
    });
  });

  it("detects an expired server receipt without treating bad timestamps as expired", () => {
    const now = Date.parse("2026-07-30T12:00:00Z");
    expect(factorRunReceiptHasExpired(undefined, now)).toBe(false);
    expect(factorRunReceiptHasExpired("bad-date", now)).toBe(false);
    expect(factorRunReceiptHasExpired("2026-07-30T12:00:00Z", now)).toBe(true);
    expect(factorRunReceiptHasExpired("2026-07-30T12:00:01Z", now)).toBe(false);
  });

  it("renders provider evidence without hiding false capability flags", () => {
    expect(
      factorCapabilityEntries({
        finalized_bars_only: true,
        bar_finalization_policy: "completed_interval_with_publication_lag",
        bar_finalization_verified: false,
        reference_series: true,
        tradable_quote: false,
        repaired_rows: 2,
      }),
    ).toEqual([
      {
        key: "finalized_bars_only",
        label: "仅使用完结 K 线",
        value: "是",
        rawValue: "true",
      },
      {
        key: "bar_finalization_policy",
        label: "完结规则",
        value: "completed_interval_with_publication_lag",
        rawValue: "completed_interval_with_publication_lag",
      },
      {
        key: "bar_finalization_verified",
        label: "完结规则已验证",
        value: "否",
        rawValue: "false",
      },
      {
        key: "reference_series",
        label: "参考序列",
        value: "是",
        rawValue: "true",
      },
      {
        key: "tradable_quote",
        label: "可交易报价",
        value: "否",
        rawValue: "false",
      },
      {
        key: "repaired_rows",
        label: "修复行数",
        value: "2",
        rawValue: "2",
      },
    ]);
    expect(
      factorAvailabilitySourceLabel(
        "provider_policy_next_utc_day_estimate",
      ),
    ).toBe("来源规则估算：次日 UTC");
    expect(factorNumericInputBasisLabel("exact_provider_decimal")).toBe(
      "来源精确十进制",
    );
    expect(factorNumericInputBasisLabel("provider_numeric_projection")).toBe(
      "来源数值投影",
    );
  });

  it("keeps dropped-period reasons deterministic for evidence display", () => {
    expect(
      factorDroppedReasonEntries({
        label_not_available: 5,
        insufficient_lookback: 20,
      }),
    ).toEqual([
      ["insufficient_lookback", 20],
      ["label_not_available", 5],
    ]);
    expect(factorDroppedReasonEntries(undefined)).toEqual([]);
  });
});
