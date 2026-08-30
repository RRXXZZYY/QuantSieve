import type { BarInterval } from "./types";

export const RESEARCH_MINIMUM_PROFITABLE_FOLD_RATIO = 0.5;

export const DEFAULT_MAX_CASH_STREAK_BARS: Record<BarInterval, number> = {
  "15m": 192,
  "1h": 168,
  "4h": 90,
  "1d": 120,
  "1wk": 26,
};

export function profileCashStreakBars(
  interval: BarInterval,
  dailyEquivalentBars: number,
): number {
  const ratio = dailyEquivalentBars / DEFAULT_MAX_CASH_STREAK_BARS["1d"];
  return Math.max(
    1,
    Math.round(DEFAULT_MAX_CASH_STREAK_BARS[interval] * ratio),
  );
}

export type ResearchContract = {
  minimumTradesPerYear: number;
  maximumTradesPerYear: number;
  minimumExposure: number;
  minimumAnnualizedReturn: number;
  maximumDrawdown: number;
  maximumCashStreakBars: number;
  minimumTimingPositiveFoldRatio: number;
};

const FINITE_FIELDS: ReadonlyArray<[keyof ResearchContract, string]> = [
  ["minimumTradesPerYear", "每年最低仓位变动次数"],
  ["maximumTradesPerYear", "每年最高仓位变动次数"],
  ["minimumExposure", "最低持仓率"],
  ["minimumAnnualizedReturn", "最低目标年化"],
  ["maximumDrawdown", "最大可接受回撤"],
  ["maximumCashStreakBars", "最多连续空仓 K 线"],
  ["minimumTimingPositiveFoldRatio", "最低择时有效窗口"],
];

export function validateResearchContract(contract: ResearchContract): string | null {
  for (const [field, label] of FINITE_FIELDS) {
    if (!Number.isFinite(contract[field])) return `${label}必须是有限数字。`;
  }
  if (
    !Number.isInteger(contract.minimumTradesPerYear) ||
    contract.minimumTradesPerYear <= 0 ||
    contract.minimumTradesPerYear > 100
  ) {
    return "每年最低仓位变动次数必须是 1–100 的整数。";
  }
  if (
    !Number.isInteger(contract.maximumTradesPerYear) ||
    contract.maximumTradesPerYear <= 0 ||
    contract.maximumTradesPerYear > 10_000
  ) {
    return "每年最高仓位变动次数必须是 1–10000 的整数。";
  }
  if (contract.maximumTradesPerYear < contract.minimumTradesPerYear) {
    return "每年最高仓位变动次数不能低于每年最低仓位变动次数。";
  }
  if (contract.minimumExposure < 0 || contract.minimumExposure > 100) {
    return "最低持仓率必须在 0%–100% 之间。";
  }
  if (
    contract.minimumTimingPositiveFoldRatio < 0 ||
    contract.minimumTimingPositiveFoldRatio > 100
  ) {
    return "最低择时有效窗口必须在 0%–100% 之间。";
  }
  if (contract.maximumDrawdown < 1 || contract.maximumDrawdown > 100) {
    return "最大可接受回撤必须在 1%–100% 之间。";
  }
  if (
    !Number.isInteger(contract.maximumCashStreakBars) ||
    contract.maximumCashStreakBars <= 0 ||
    contract.maximumCashStreakBars > 100_000
  ) {
    return "最多连续空仓 K 线必须是 1–100000 的整数。";
  }
  if (
    contract.minimumAnnualizedReturn < -100 ||
    contract.minimumAnnualizedReturn > 1000
  ) {
    return "最低目标年化必须在 -100%–1000% 之间。";
  }
  return null;
}
