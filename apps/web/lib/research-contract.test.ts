import { describe, expect, it } from "vitest";

import {
  DEFAULT_MAX_CASH_STREAK_BARS,
  RESEARCH_MINIMUM_PROFITABLE_FOLD_RATIO,
  profileCashStreakBars,
  type ResearchContract,
  validateResearchContract,
} from "./research-contract";

const validContract: ResearchContract = {
  minimumTradesPerYear: 2,
  maximumTradesPerYear: 120,
  minimumExposure: 20,
  minimumAnnualizedReturn: 0,
  maximumDrawdown: 30,
  maximumCashStreakBars: 120,
  minimumTimingPositiveFoldRatio: 50,
};

describe("validateResearchContract", () => {
  it("accepts the normal research contract and documented boundaries", () => {
    expect(validateResearchContract(validContract)).toBeNull();
    expect(
      validateResearchContract({
        ...validContract,
        minimumExposure: 0,
        minimumAnnualizedReturn: -100,
        maximumDrawdown: 1,
        minimumTimingPositiveFoldRatio: 100,
      }),
    ).toBeNull();
    expect(
      validateResearchContract({
        ...validContract,
        minimumExposure: 100,
        minimumAnnualizedReturn: 1000,
        maximumDrawdown: 100,
        minimumTimingPositiveFoldRatio: 0,
      }),
    ).toBeNull();
  });

  it.each([
    ["minimumTradesPerYear", "每年最低仓位变动次数"],
    ["maximumTradesPerYear", "每年最高仓位变动次数"],
    ["minimumExposure", "最低持仓率"],
    ["minimumAnnualizedReturn", "最低目标年化"],
    ["maximumDrawdown", "最大可接受回撤"],
    ["maximumCashStreakBars", "最多连续空仓 K 线"],
    ["minimumTimingPositiveFoldRatio", "最低择时有效窗口"],
  ] as const)("rejects a non-finite %s", (field, label) => {
    expect(
      validateResearchContract({ ...validContract, [field]: Number.NaN }),
    ).toBe(`${label}必须是有限数字。`);
  });

  it("requires positive integral trade and cash-streak counts", () => {
    expect(
      validateResearchContract({ ...validContract, minimumTradesPerYear: 1.5 }),
    ).toBe("每年最低仓位变动次数必须是 1–100 的整数。");
    expect(
      validateResearchContract({ ...validContract, maximumTradesPerYear: 0 }),
    ).toBe("每年最高仓位变动次数必须是 1–10000 的整数。");
    expect(
      validateResearchContract({ ...validContract, maximumCashStreakBars: 2.5 }),
    ).toBe("最多连续空仓 K 线必须是 1–100000 的整数。");
  });

  it("requires the maximum trade frequency to cover the minimum", () => {
    expect(
      validateResearchContract({
        ...validContract,
        minimumTradesPerYear: 10,
        maximumTradesPerYear: 9,
      }),
    ).toBe("每年最高仓位变动次数不能低于每年最低仓位变动次数。");
  });

  it("matches the server-owned count ceilings", () => {
    expect(
      validateResearchContract({ ...validContract, minimumTradesPerYear: 101 }),
    ).toBe("每年最低仓位变动次数必须是 1–100 的整数。");
    expect(
      validateResearchContract({
        ...validContract,
        maximumTradesPerYear: 10_001,
      }),
    ).toBe("每年最高仓位变动次数必须是 1–10000 的整数。");
    expect(
      validateResearchContract({
        ...validContract,
        maximumCashStreakBars: 100_001,
      }),
    ).toBe("最多连续空仓 K 线必须是 1–100000 的整数。");
  });

  it("enforces percentage and return boundaries", () => {
    expect(
      validateResearchContract({ ...validContract, minimumExposure: 101 }),
    ).toBe("最低持仓率必须在 0%–100% 之间。");
    expect(
      validateResearchContract({
        ...validContract,
        minimumTimingPositiveFoldRatio: -1,
      }),
    ).toBe("最低择时有效窗口必须在 0%–100% 之间。");
    expect(
      validateResearchContract({ ...validContract, maximumDrawdown: 0 }),
    ).toBe("最大可接受回撤必须在 1%–100% 之间。");
    expect(
      validateResearchContract({
        ...validContract,
        minimumAnnualizedReturn: 1001,
      }),
    ).toBe("最低目标年化必须在 -100%–1000% 之间。");
  });

  it("keeps the profitable-window discipline fixed at fifty percent", () => {
    expect(RESEARCH_MINIMUM_PROFITABLE_FOLD_RATIO).toBe(0.5);
  });

  it("converts a profile cash-streak budget for every K-line interval", () => {
    expect(profileCashStreakBars("15m", 120)).toBe(192);
    expect(profileCashStreakBars("1h", 120)).toBe(168);
    expect(profileCashStreakBars("4h", 120)).toBe(90);
    expect(profileCashStreakBars("1d", 120)).toBe(120);
    expect(profileCashStreakBars("1wk", 120)).toBe(26);
    expect(profileCashStreakBars("1wk", 180)).toBe(39);
    expect(profileCashStreakBars("4h", 90)).toBe(68);
    expect(DEFAULT_MAX_CASH_STREAK_BARS["1d"]).toBe(120);
  });
});
