import { describe, expect, it } from "vitest";

import { buildBacktestMarkers } from "./backtest-markers";

const candles = [
  { date: "2026-07-27T09:30:00Z", open: 100 },
  { date: "2026-07-28T09:30:00Z", open: 110 },
];

describe("buildBacktestMarkers", () => {
  it("uses modeled trade prices instead of candle prices", () => {
    const result = buildBacktestMarkers({
      candles,
      intraday: true,
      trades: [
        {
          closed: true,
          entry_date: "2026-07-27T09:30:00Z",
          entry_price: 100.15,
          exit_date: "2026-07-28T09:30:00Z",
          exit_price: 109.835,
        },
      ],
    });

    expect(result).toEqual({
      entries: [{ coord: ["2026-07-27 09:30", 100.15], value: "买" }],
      exits: [{ coord: ["2026-07-28 09:30", 109.835], value: "卖" }],
    });
  });

  it("falls back to the corresponding candle open when a modeled price is absent", () => {
    const result = buildBacktestMarkers({
      candles,
      intraday: false,
      trades: [
        {
          closed: true,
          entry_date: "2026-07-27T09:30:00Z",
          exit_date: "2026-07-28T09:30:00Z",
        },
      ],
    });

    expect(result).toEqual({
      entries: [{ coord: ["2026-07-27", 100], value: "买" }],
      exits: [{ coord: ["2026-07-28", 110], value: "卖" }],
    });
  });

  it("does not draw a synthetic exit for an open trade and deduplicates lot markers", () => {
    const result = buildBacktestMarkers({
      candles,
      intraday: true,
      trades: [
        {
          closed: false,
          entry_date: "2026-07-27T09:30:00Z",
          entry_price: 100.15,
          exit_date: "2026-07-28T09:30:00Z",
          exit_price: 111,
        },
        {
          closed: false,
          entry_date: "2026-07-27T09:30:00Z",
          entry_price: 100.15,
        },
      ],
    });

    expect(result.entries).toEqual([
      { coord: ["2026-07-27 09:30", 100.15], value: "买" },
    ]);
    expect(result.exits).toEqual([]);
  });
});
