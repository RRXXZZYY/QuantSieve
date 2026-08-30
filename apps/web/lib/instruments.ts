import { apiFetch } from "./api";
import type { Instrument } from "./types";

export type MarketFilter =
  | "all"
  | "CN"
  | "US"
  | "ETF"
  | "INDEX"
  | "FOREX"
  | "CRYPTO"
  | "FUTURES";

export async function searchInstruments(
  query: string,
  market: MarketFilter = "all",
  limit = 10,
  signal?: AbortSignal,
): Promise<Instrument[]> {
  const parameters = new URLSearchParams({
    q: query.trim(),
    market,
    limit: String(limit),
  });
  return apiFetch<Instrument[]>(`/api/v1/symbols/search?${parameters}`, { signal });
}

export async function resolveInstrument(
  query: string,
  market: MarketFilter = "all",
): Promise<Instrument | null> {
  const results = await searchInstruments(query, market, 10);
  return exactInstrumentMatch(query, results);
}

function normalizedSymbol(value: string): string {
  return value.trim().toLocaleUpperCase().replace(/[\s/_-]+/g, "");
}

export function exactInstrumentMatch(
  query: string,
  results: Instrument[],
): Instrument | null {
  const normalized = query.trim().toLocaleLowerCase();
  const symbol = normalizedSymbol(query);
  return (
    results.find(
      (instrument) =>
        normalizedSymbol(instrument.symbol) === symbol ||
        instrument.name.trim().toLocaleLowerCase() === normalized,
    ) ?? null
  );
}

export function instrumentLabel(instrument: Instrument): string {
  return `${instrument.name} · ${instrument.symbol}`;
}

export function marketLabel(market: Instrument["market"] | MarketFilter): string {
  return {
    all: "全部",
    CN: "A股",
    US: "美股",
    ETF: "ETF",
    INDEX: "指数",
    FOREX: "外汇",
    CRYPTO: "加密",
    FUTURES: "期货",
  }[market];
}
