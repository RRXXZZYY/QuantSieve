export type BacktestMarker = {
  coord: [string, number];
  value: "买" | "卖";
};

type MarkerCandle = {
  date: string;
  open: number;
};

type MarkerResult = {
  entries: BacktestMarker[];
  exits: BacktestMarker[];
};

function dateKey(value: string): string {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? String(timestamp) : value;
}

function axisDate(value: string, intraday: boolean): string {
  return intraday ? value.replace("T", " ").slice(0, 16) : value.slice(0, 10);
}

function positiveNumber(value: unknown): number | null {
  const numeric = typeof value === "number" ? value : Number(value);
  return Number.isFinite(numeric) && numeric > 0 ? numeric : null;
}

function isOpenTrade(value: unknown): boolean {
  return value === false || value === 0 || value === "false";
}

export function buildBacktestMarkers({
  candles,
  trades,
  intraday,
}: {
  candles: readonly MarkerCandle[];
  trades: readonly Record<string, unknown>[];
  intraday: boolean;
}): MarkerResult {
  const candleByDate = new Map(candles.map((candle) => [dateKey(candle.date), candle]));
  const entries: BacktestMarker[] = [];
  const exits: BacktestMarker[] = [];
  const seen = new Set<string>();

  function append(
    destination: BacktestMarker[],
    side: "entry" | "exit",
    dateValue: unknown,
    modeledPrice: unknown,
  ) {
    if (typeof dateValue !== "string" || !dateValue.trim()) return;
    const key = dateKey(dateValue);
    const candle = candleByDate.get(key);
    if (!candle) return;
    const price = positiveNumber(modeledPrice) ?? positiveNumber(candle.open);
    if (price === null) return;
    const identity = `${side}:${key}:${price}`;
    if (seen.has(identity)) return;
    seen.add(identity);
    destination.push({
      coord: [axisDate(candle.date, intraday), price],
      value: side === "entry" ? "买" : "卖",
    });
  }

  for (const trade of trades) {
    append(entries, "entry", trade.entry_date, trade.entry_price);
    if (!isOpenTrade(trade.closed)) {
      append(exits, "exit", trade.exit_date, trade.exit_price);
    }
  }

  return { entries, exits };
}
