import { describe, expect, it } from "vitest";

import {
  backtestRunReceiptHasExpired,
  buildBacktestExperimentSaveRequest,
} from "./backtest-experiments";
import type { ExperimentCreatePayload } from "./types";

const RUN_ID = "0123456789abcdef0123456789abcdef";
const NOW = Date.parse("2026-07-29T12:00:00Z");
const LEGACY_PAYLOAD = {
  name: "旧版实验",
} as ExperimentCreatePayload;

describe("backtest experiment save requests", () => {
  it("uses only client-authored labels with a server-issued run receipt", () => {
    const request = buildBacktestExperimentSaveRequest({
      run: {
        run_id: RUN_ID,
        run_expires_at: "2026-07-29T12:10:00Z",
      },
      name: "  BTC 趋势验证  ",
      notes: "  留出期通过  ",
      instrumentName: "  Bitcoin / USDT  ",
      legacyPayload: LEGACY_PAYLOAD,
      now: NOW,
    });

    expect(request).toEqual({
      provenance: "server_verified",
      path: "/api/v1/experiments/from-run",
      body: {
        name: "BTC 趋势验证",
        notes: "留出期通过",
        run_id: RUN_ID,
        instrument_name: "Bitcoin / USDT",
      },
    });
    expect(request.body).not.toHaveProperty("metrics");
    expect(request.body).not.toHaveProperty("run_manifest");
  });

  it("uses the legacy endpoint only when run_id is completely absent", () => {
    const request = buildBacktestExperimentSaveRequest({
      run: {},
      name: "旧版实验",
      notes: null,
      instrumentName: "测试标的",
      legacyPayload: LEGACY_PAYLOAD,
      now: NOW,
    });

    expect(request).toEqual({
      provenance: "legacy_unverified",
      path: "/api/v1/experiments",
      body: LEGACY_PAYLOAD,
    });
  });

  it("rejects an expired receipt instead of silently downgrading to legacy", () => {
    expect(() =>
      buildBacktestExperimentSaveRequest({
        run: {
          run_id: RUN_ID,
          run_expires_at: "2026-07-29T11:59:59Z",
        },
        name: "不能降级",
        notes: null,
        instrumentName: "测试标的",
        legacyPayload: LEGACY_PAYLOAD,
        now: NOW,
      }),
    ).toThrow("服务端运行回执已过期，请重新运行后再保存。");
  });

  it("treats a present but malformed run_id as an error, not a legacy result", () => {
    expect(() =>
      buildBacktestExperimentSaveRequest({
        run: { run_id: "" },
        name: "不能降级",
        notes: null,
        instrumentName: "测试标的",
        legacyPayload: LEGACY_PAYLOAD,
      }),
    ).toThrow("服务端运行回执无效，请重新运行后再保存。");
  });

  it("requires a frozen legacy snapshot when no receipt exists", () => {
    expect(() =>
      buildBacktestExperimentSaveRequest({
        run: {},
        name: "旧版实验",
        notes: null,
        instrumentName: "测试标的",
        legacyPayload: null,
      }),
    ).toThrow("旧版结果缺少可保存的实验快照，请重新运行后再保存。");
  });

  it("reports expiry only for valid timestamps at or before the current time", () => {
    expect(backtestRunReceiptHasExpired(undefined, NOW)).toBe(false);
    expect(backtestRunReceiptHasExpired("not-a-date", NOW)).toBe(false);
    expect(backtestRunReceiptHasExpired("2026-07-29T12:00:00Z", NOW)).toBe(true);
    expect(backtestRunReceiptHasExpired("2026-07-29T12:00:01Z", NOW)).toBe(false);
  });
});
