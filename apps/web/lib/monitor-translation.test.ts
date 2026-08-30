import { describe, expect, it } from "vitest";

import {
  chunkEvents,
  currentTranslationEntry,
  eventKey,
  eventReferenceKey,
  eventTranslationFingerprint,
  isActiveTranslationRequest,
  translationRequestItems,
  untranslatedEvents,
} from "./monitor-translation";
import type { MonitorTranslationEntry } from "./monitor-translation";
import type { MonitorEvent } from "./types";

function monitorEvent(overrides: Partial<MonitorEvent> = {}): MonitorEvent {
  return {
    source: "official-intel",
    source_id: "one",
    profile_id: "central-bank-watch",
    profile_name: "Global central banks",
    kind: "news",
    title: "Federal Reserve holds rates steady",
    content: "Inflation risks remain elevated.",
    url: "https://example.com/one",
    occurred_at: "2026-07-28T00:00:00Z",
    available_at: "2026-07-28T00:00:00Z",
    analysis: "Market volatility may increase.",
    market_relevance: "high",
    impact_assets: [
      {
        asset: "USD",
        direction: "volatile",
        reason: "Rate expectations affect the currency.",
      },
    ],
    analysis_method: "rules",
    tags: ["Federal Reserve"],
    ...overrides,
  };
}

describe("monitor translation state", () => {
  it("invalidates a ready translation when any translated source field changes", () => {
    const event = monitorEvent();
    const fingerprint = eventTranslationFingerprint(event);
    const translations: Record<string, MonitorTranslationEntry> = {
      [eventKey(event)]: {
        status: "ready",
        fingerprint,
        value: {
          source: event.source,
          source_id: event.source_id,
          status: "translated",
          title_zh: "美联储维持利率不变",
          content_zh: "通胀风险仍然较高。",
          impact_reasons_zh: ["利率预期会影响美元。"],
          fields: {},
          translated_fields: 3,
          cached_fields: 0,
          unavailable_fields: 0,
        },
      },
    };

    expect(currentTranslationEntry(event, translations)?.status).toBe("ready");
    expect(
      currentTranslationEntry(
        monitorEvent({
          impact_assets: [
            {
              asset: "USD",
              direction: "volatile",
              reason: "Updated evidence.",
            },
          ],
        }),
        translations,
      ),
    ).toBeUndefined();
  });

  it("chunks only unique current events and skips fresh ready translations", () => {
    const first = monitorEvent();
    const duplicate = monitorEvent();
    const second = monitorEvent({ source_id: "two" });
    const translations: Record<string, MonitorTranslationEntry> = {
      [eventKey(first)]: {
        status: "ready",
        fingerprint: eventTranslationFingerprint(first),
        value: {
          source: first.source,
          source_id: first.source_id,
          status: "identity",
          title_zh: first.title,
          content_zh: first.content,
          impact_reasons_zh: [],
          fields: {},
          translated_fields: 0,
          cached_fields: 0,
          unavailable_fields: 0,
        },
      },
    };

    expect(translationRequestItems([first, duplicate, second])).toEqual([
      { source: "official-intel", source_id: "one" },
      { source: "official-intel", source_id: "two" },
    ]);
    expect(untranslatedEvents([first, second], translations)).toEqual([second]);
    expect(chunkEvents([first, second], 1)).toEqual([[first], [second]]);
  });

  it("rejects an invalid frontend chunk size", () => {
    expect(() => chunkEvents([monitorEvent()], 0)).toThrow("正整数");
  });

  it("keeps composite event identities collision-safe", () => {
    expect(eventReferenceKey("a-b", "c")).not.toBe(
      eventReferenceKey("a", "b-c"),
    );
  });

  it("rejects a late response from an older translation request", () => {
    const fingerprint = eventTranslationFingerprint(monitorEvent());
    const loading: MonitorTranslationEntry = {
      status: "loading",
      fingerprint,
      requestId: 12,
    };

    expect(isActiveTranslationRequest(loading, fingerprint, 12)).toBe(true);
    expect(isActiveTranslationRequest(loading, fingerprint, 11)).toBe(false);
    expect(isActiveTranslationRequest(loading, "updated content", 12)).toBe(false);
  });
});
