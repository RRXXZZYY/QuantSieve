import { describe, expect, it } from "vitest";

import {
  elapsedCalendarDays,
  preferredCalendarPreset,
  requestedCalendarDays,
} from "./date-range";

describe("calendar date ranges", () => {
  it("distinguishes elapsed research span from inclusive provider requests", () => {
    expect(elapsedCalendarDays("2026-06-01", "2026-07-30")).toBe(59);
    expect(requestedCalendarDays("2026-06-01", "2026-07-30")).toBe(60);
  });

  it("counts a same-day provider request as one calendar date", () => {
    expect(elapsedCalendarDays("2026-07-28", "2026-07-28")).toBe(0);
    expect(requestedCalendarDays("2026-07-28", "2026-07-28")).toBe(1);
  });

  it("rejects invalid or reversed ranges", () => {
    expect(elapsedCalendarDays("invalid", "2026-07-28")).toBe(0);
    expect(requestedCalendarDays("2026-07-29", "2026-07-28")).toBe(0);
  });

  it("uses the provider maximum when no preset clears the research horizon", () => {
    const options = [
      { id: "7D", days: 7 },
      { id: "30D", days: 30 },
      { id: "60D", days: 59 },
    ];

    expect(preferredCalendarPreset(options, 365)?.id).toBe("60D");
    expect(preferredCalendarPreset(options, 20)?.id).toBe("30D");
    expect(preferredCalendarPreset([], 20)).toBeNull();
  });
});
