import { describe, expect, it } from "vitest";

import {
  formatAnnualizedReturn,
  formatCompact,
  formatPercent,
} from "./format";

describe("financial formatting", () => {
  it("formats ratios as percentages", () => {
    expect(formatPercent(0.1234)).toContain("12.34");
  });

  it("formats compact values", () => {
    expect(formatCompact(1_000_000)).toMatch(/100万|1M/);
  });

  it("labels a saturated annualized return instead of presenting it as exact", () => {
    expect(
      formatAnnualizedReturn({
        annualized_return: 1_000_000,
        annualized_return_capped: true,
        annualized_return_cap: 1_000_000,
      }),
    ).toBe("≥ 100,000,000.00%（年化截断）");
    expect(
      formatAnnualizedReturn({
        annualized_return: 0.1234,
        annualized_return_capped: false,
        annualized_return_cap: 1_000_000,
      }),
    ).toBe("12.34%");
  });
});
