const MILLISECONDS_PER_DAY = 86_400_000;

function calendarRange(start: string, end: string): [number, number] | null {
  const startMs = Date.parse(`${start}T00:00:00Z`);
  const endMs = Date.parse(`${end}T00:00:00Z`);
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) {
    return null;
  }
  return [startMs, endMs];
}

/**
 * Elapsed whole calendar days between two dates.
 *
 * Use this for research-horizon checks: 2026-01-01 through 2026-01-02 spans
 * one day even though the provider request contains two calendar dates.
 */
export function elapsedCalendarDays(start: string, end: string): number {
  const range = calendarRange(start, end);
  if (!range) return 0;
  return Math.floor((range[1] - range[0]) / MILLISECONDS_PER_DAY);
}

/**
 * Number of inclusive calendar dates requested from a market-data provider.
 *
 * Provider history limits are expressed this way, so a same-day request uses
 * one day and an elapsed 60-day range uses 61 calendar dates.
 */
export function requestedCalendarDays(start: string, end: string): number {
  const range = calendarRange(start, end);
  if (!range) return 0;
  return Math.floor((range[1] - range[0]) / MILLISECONDS_PER_DAY) + 1;
}

/**
 * Pick the shortest preset that clears the research horizon. If a provider's
 * hard history cap makes that impossible, use its longest available preset so
 * the UI offers the strongest possible evidence rather than an arbitrary
 * middle range.
 */
export function preferredCalendarPreset<T extends { days: number }>(
  options: readonly T[],
  minimumDays: number,
): T | null {
  return (
    options.find((item) => item.days > minimumDays) ??
    options[options.length - 1] ??
    null
  );
}
