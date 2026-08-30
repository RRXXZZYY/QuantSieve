import { describe, expect, it } from "vitest";

import { describeApiError } from "./api";

describe("describeApiError", () => {
  it("keeps a server-authored business error intact", () => {
    expect(
      describeApiError({ detail: "策略发现至少需要 125 根 K 线。" }, "fallback"),
    ).toBe("策略发现至少需要 125 根 K 线。");
  });

  it("turns an upstream outage into a retryable Chinese explanation", () => {
    expect(
      describeApiError(
        { detail: "AKShare could not load history_interval" },
        "fallback",
        502,
      ),
    ).toBe(
      "数据源或研究引擎暂不可用，请稍后重试。原因：AKShare could not load history_interval",
    );
  });

  it("renders FastAPI validation details without object coercion", () => {
    expect(
      describeApiError(
        {
          detail: [
            { loc: ["body", "start"], msg: "Field required" },
            { loc: ["body", "interval"], msg: "Input should be '1d'" },
          ],
        },
        "fallback",
      ),
    ).toBe("请求参数有误：开始日期：Field required；K 线周期：Input should be '1d'");
  });

  it("falls back when an error response is not usable", () => {
    expect(describeApiError({ detail: [] }, "请求失败")).toBe("请求失败");
  });
});
