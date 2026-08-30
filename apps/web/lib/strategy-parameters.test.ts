import { describe, expect, it } from "vitest";

import { manualParameterValidationError } from "./strategy-parameters";

describe("manualParameterValidationError", () => {
  it("accepts a valid MACD configuration", () => {
    expect(
      manualParameterValidationError("macd", { fast: 12, slow: 26, signal: 9 }),
    ).toBeNull();
  });

  it("explains a reversed trend window before submission", () => {
    expect(
      manualParameterValidationError("macd", { fast: 26, slow: 12, signal: 9 }),
    ).toBe("快速周期必须小于慢速周期。");
  });

  it("requires ordered RSI thresholds and whole-number periods", () => {
    expect(
      manualParameterValidationError("rsi", {
        period: 14,
        oversold: 70,
        overbought: 30,
      }),
    ).toBe("RSI 超卖线必须低于超买线，且两者都在 0–100 内。");
    expect(
      manualParameterValidationError("macd", { fast: 12.5, slow: 26, signal: 9 }),
    ).toBe("fast 必须是大于 0 的整数根数。");
  });

  it("keeps allocation bounds in their valid range", () => {
    expect(
      manualParameterValidationError("volatility-target-trend", {
        trend_period: 150,
        volatility_period: 20,
        minimum_allocation: 1,
        maximum_allocation: 0.75,
        adjustment_band: 0.15,
      }),
    ).toBe("最低/最高风险仓位必须按 0–100% 从小到大设置。");
  });
});
