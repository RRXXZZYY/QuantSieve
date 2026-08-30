import type { MonitorEvent, MonitorEventTranslation } from "./types";

export type MonitorTranslationEntry =
  | {
      status: "loading";
      fingerprint: string;
      requestId: number;
    }
  | {
      status: "ready";
      fingerprint: string;
      value: MonitorEventTranslation;
    }
  | {
      status: "error";
      fingerprint: string;
      message: string;
    };

export function eventReferenceKey(source: string, sourceId: string): string {
  return JSON.stringify([source, sourceId]);
}

export function eventKey(event: MonitorEvent): string {
  return eventReferenceKey(event.source, event.source_id);
}

export function eventTranslationFingerprint(event: MonitorEvent): string {
  return JSON.stringify({
    title: event.title,
    content: event.content,
    analysis: event.analysis ?? null,
    impactReasons: event.impact_assets.map((impact) => impact.reason),
  });
}

export function currentTranslationEntry(
  event: MonitorEvent,
  translations: Record<string, MonitorTranslationEntry>,
): MonitorTranslationEntry | undefined {
  const entry = translations[eventKey(event)];
  return entry?.fingerprint === eventTranslationFingerprint(event) ? entry : undefined;
}

export function isActiveTranslationRequest(
  entry: MonitorTranslationEntry | undefined,
  fingerprint: string,
  requestId: number,
): boolean {
  return (
    entry?.status === "loading" &&
    entry.requestId === requestId &&
    entry.fingerprint === fingerprint
  );
}

export function translationRequestItems(events: MonitorEvent[]) {
  const seen = new Set<string>();
  return events.flatMap((event) => {
    const key = eventKey(event);
    if (seen.has(key)) return [];
    seen.add(key);
    return [{ source: event.source, source_id: event.source_id }];
  });
}

export function chunkEvents(events: MonitorEvent[], size = 6): MonitorEvent[][] {
  if (!Number.isInteger(size) || size < 1) {
    throw new Error("翻译批次大小必须是正整数。");
  }
  const chunks: MonitorEvent[][] = [];
  for (let index = 0; index < events.length; index += size) {
    chunks.push(events.slice(index, index + size));
  }
  return chunks;
}

export function untranslatedEvents(
  events: MonitorEvent[],
  translations: Record<string, MonitorTranslationEntry>,
): MonitorEvent[] {
  return events.filter((event) => {
    const entry = currentTranslationEntry(event, translations);
    return entry?.status !== "ready";
  });
}
