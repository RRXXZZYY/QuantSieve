import { describe, expect, it } from "vitest";

import { exactInstrumentMatch } from "./instruments";
import type { Instrument } from "./types";

const APPLE: Instrument = {
  symbol: "AAPL",
  name: "Apple",
  market: "US",
  exchange: "NASDAQ",
  currency: "USD",
  provider: "yfinance",
  asset_type: "equity",
};

const BITCOIN: Instrument = {
  symbol: "BTCUSDT",
  name: "Bitcoin / Tether",
  market: "CRYPTO",
  exchange: "Binance",
  currency: "USDT",
  provider: "binance",
  asset_type: "crypto",
};

describe("exactInstrumentMatch", () => {
  it("accepts an exact symbol or exact display name", () => {
    expect(exactInstrumentMatch("aapl", [APPLE])).toBe(APPLE);
    expect(exactInstrumentMatch("Apple", [APPLE])).toBe(APPLE);
  });

  it("accepts common separators in a crypto pair", () => {
    expect(exactInstrumentMatch("BTC/USDT", [BITCOIN])).toBe(BITCOIN);
  });

  it("does not silently choose the first fuzzy search result", () => {
    expect(exactInstrumentMatch("apple stock", [APPLE])).toBeNull();
    expect(exactInstrumentMatch("bitcoin", [BITCOIN, APPLE])).toBeNull();
  });
});
