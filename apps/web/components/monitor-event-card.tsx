"use client";

import { useId } from "react";

import { formatDate } from "@/lib/format";
import type { MonitorTranslationEntry } from "@/lib/monitor-translation";
import type { MonitorEvent } from "@/lib/types";

type TranslationDisplayMode = "original" | "zh";

export function MonitorEventCard({
  event,
  translationEntry,
  displayMode,
  translationDisabled,
  onTranslate,
  onDisplayMode,
}: {
  event: MonitorEvent;
  translationEntry?: MonitorTranslationEntry;
  displayMode: TranslationDisplayMode;
  translationDisabled: boolean;
  onTranslate: () => void;
  onDisplayMode: (mode: TranslationDisplayMode) => void;
}) {
  const translationStatusId = useId();
  const translation =
    translationEntry?.status === "ready" ? translationEntry.value : undefined;
  const showChinese = Boolean(
    translation &&
      translation.status !== "unavailable" &&
      translation.status !== "not_found" &&
      displayMode === "zh",
  );
  const title =
    showChinese && translation?.title_zh ? translation.title_zh : event.title;
  const content =
    showChinese && translation?.content_zh ? translation.content_zh : event.content;
  const analysis =
    showChinese && translation?.analysis_zh
      ? translation.analysis_zh
      : event.analysis;

  return (
    <article
      aria-busy={translationEntry?.status === "loading"}
      className="event-card"
    >
      <div className="event-rail">
        <span className={event.kind} />
      </div>
      <div className="event-content">
        <div className="event-meta">
          <span>{event.profile_name}</span>
          <time>{formatDate(event.occurred_at)}</time>
          <span className={`source-tier ${sourceTier(event.source)}`}>
            {sourceLabel(event.source)}
          </span>
          <span className={`relevance ${event.market_relevance}`}>
            {relevanceLabel(event.market_relevance)}
          </span>
          <span className="tag">{kindLabel(event.kind)}</span>
        </div>
        <div className="event-title-row">
          <h2 lang={showChinese ? "zh-CN" : undefined}>{title}</h2>
          <div aria-live="polite" className="translation-control-area">
            <TranslationControl
              describedBy={
                translationEntry?.status === "error"
                  ? translationStatusId
                  : undefined
              }
              disabled={translationDisabled}
              displayMode={displayMode}
              entry={translationEntry}
              onDisplayMode={onDisplayMode}
              onTranslate={onTranslate}
            />
            {translationEntry?.status === "error" && (
              <span
                className="translation-error"
                id={translationStatusId}
                role="alert"
              >
                {translationEntry.message}
              </span>
            )}
          </div>
        </div>
        <p lang={showChinese ? "zh-CN" : undefined}>{content}</p>
        {translation && showChinese && translation.status !== "identity" && (
          <p className="translation-disclaimer" role="status">
            {translation.status === "partial"
              ? "部分机器翻译 · 未译字段保留原文 · 原始来源为准"
              : translation.cached_fields > 0
                ? "NAS 离线机器翻译 · 已使用缓存 · 原始来源为准"
                : "NAS 离线机器翻译 · 原始来源为准"}
          </p>
        )}
        {analysis && (
          <blockquote lang={showChinese ? "zh-CN" : undefined}>
            <span>
              {event.analysis_method === "ai" ? "AI IMPACT ANALYSIS" : "规则影响分析"}
            </span>
            {analysis}
          </blockquote>
        )}
        {event.impact_assets.length > 0 && (
          <div className="impact-grid">
            {event.impact_assets.map((impact, index) => (
              <div key={`${impact.asset}-${impact.direction}`}>
                <span>{impact.asset}</span>
                <strong className={impact.direction}>
                  {directionLabel(impact.direction)}
                </strong>
                <p lang={showChinese ? "zh-CN" : undefined}>
                  {showChinese && translation?.impact_reasons_zh[index]
                    ? translation.impact_reasons_zh[index]
                    : impact.reason}
                </p>
              </div>
            ))}
          </div>
        )}
        <div className="event-footer">
          <div>
            {event.tags.map((tag) => (
              <span className="micro-tag" key={tag}>
                {tag}
              </span>
            ))}
          </div>
          {event.url && (
            <a href={event.url} rel="noreferrer" target="_blank">
              {event.kind === "social" ? "查看 X 原文 ↗" : "查看原始来源 ↗"}
            </a>
          )}
        </div>
      </div>
    </article>
  );
}

function TranslationControl({
  entry,
  displayMode,
  describedBy,
  disabled,
  onTranslate,
  onDisplayMode,
}: {
  entry?: MonitorTranslationEntry;
  displayMode: TranslationDisplayMode;
  describedBy?: string;
  disabled: boolean;
  onTranslate: () => void;
  onDisplayMode: (mode: TranslationDisplayMode) => void;
}) {
  if (entry?.status === "loading") {
    return (
      <button className="translation-action loading" disabled type="button">
        <span aria-hidden="true" />
        翻译中…
      </button>
    );
  }
  if (entry?.status === "ready" && entry.value.status === "identity") {
    return <span className="translation-identity">内容已是中文</span>;
  }
  if (
    entry?.status === "ready" &&
    !["unavailable", "not_found"].includes(entry.value.status)
  ) {
    return (
      <div
        aria-label="原文和中文译文切换"
        className="translation-toggle"
        role="group"
      >
        <button
          aria-pressed={displayMode === "original"}
          className={displayMode === "original" ? "active" : ""}
          onClick={() => onDisplayMode("original")}
          type="button"
        >
          原文
        </button>
        <button
          aria-pressed={displayMode === "zh"}
          className={displayMode === "zh" ? "active" : ""}
          onClick={() => onDisplayMode("zh")}
          type="button"
        >
          中文
        </button>
      </div>
    );
  }
  return (
    <button
      className={`translation-action ${entry?.status === "error" ? "retry" : ""}`}
      aria-describedby={describedBy}
      disabled={disabled}
      onClick={onTranslate}
      title={entry?.status === "error" ? entry.message : undefined}
      type="button"
    >
      {entry?.status === "error" ? "翻译失败 · 重试" : "译成中文"}
    </button>
  );
}

function kindLabel(kind: MonitorEvent["kind"]): string {
  return {
    social: "X POST",
    news: "NEWS / POLICY",
    geopolitical: "GEO EVENT",
    filing: "SEC FILING",
    market: "MARKET EVIDENCE",
  }[kind];
}

function relevanceLabel(relevance: MonitorEvent["market_relevance"]): string {
  return {
    critical: "极高相关",
    high: "高相关",
    medium: "中等相关",
    low: "低相关",
    unrelated: "未见相关",
    unrated: "待分析",
  }[relevance];
}

function directionLabel(direction: MonitorEvent["impact_assets"][number]["direction"]): string {
  return {
    up: "偏上行",
    down: "偏下行",
    volatile: "波动放大",
    uncertain: "方向不确定",
  }[direction];
}

function sourceLabel(source: string): string {
  const labels: Record<string, string> = {
    "x-api": "X 官方",
    "official-intel": "官方机构",
    "ofac-actions": "OFAC 官方",
    "hkma-press": "HKMA 官方",
    "sec-edgar": "SEC 官方",
    "un-news": "联合国",
    "gdelt-headlines": "新闻聚合 · 标题级需核验",
    "linked-market-data": "真实行情",
    "public-rss": "公开镜像",
    "recorded-demo": "演示快照",
  };
  return labels[source] || source;
}

function sourceTier(source: string): "primary" | "aggregate" | "evidence" {
  if (
    ["x-api", "official-intel", "ofac-actions", "hkma-press", "sec-edgar", "un-news"].includes(
      source,
    )
  ) {
    return "primary";
  }
  return source === "linked-market-data" ? "evidence" : "aggregate";
}
