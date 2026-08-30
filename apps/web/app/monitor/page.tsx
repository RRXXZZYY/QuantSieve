"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { MonitorEventCard } from "@/components/monitor-event-card";
import { readLlmSettings } from "@/components/settings-panel";
import { apiFetch } from "@/lib/api";
import { formatDate } from "@/lib/format";
import {
  chunkEvents,
  currentTranslationEntry,
  eventKey,
  eventReferenceKey,
  eventTranslationFingerprint,
  isActiveTranslationRequest,
  translationRequestItems,
  untranslatedEvents,
  type MonitorTranslationEntry,
} from "@/lib/monitor-translation";
import type {
  MonitorEvent,
  MonitorProfile,
  MonitorRefreshResult,
  MonitorStatus,
  MonitorTranslationResponse,
} from "@/lib/types";

type KindFilter =
  | "signals"
  | "social"
  | "news"
  | "geopolitical"
  | "market"
  | "filing";

const KIND_FILTERS: Array<{ id: KindFilter; label: string }> = [
  { id: "signals", label: "关键事件" },
  { id: "social", label: "X 原文" },
  { id: "news", label: "新闻 / 政策" },
  { id: "geopolitical", label: "战争 / 地缘" },
  { id: "market", label: "行情佐证" },
  { id: "filing", label: "SEC 申报" },
];

export default function MonitorPage() {
  const [events, setEvents] = useState<MonitorEvent[]>([]);
  const [profiles, setProfiles] = useState<MonitorProfile[]>([]);
  const [status, setStatus] = useState<MonitorStatus | null>(null);
  const [activeProfile, setActiveProfile] = useState("");
  const [kindFilter, setKindFilter] = useState<KindFilter>("signals");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [notice, setNotice] = useState("");
  const [displayLimit, setDisplayLimit] = useState(18);
  const [notificationsEnabled, setNotificationsEnabled] = useState(false);
  const [translations, setTranslations] = useState<
    Record<string, MonitorTranslationEntry>
  >({});
  const [translationModes, setTranslationModes] = useState<
    Record<string, "original" | "zh">
  >({});
  const [translationProgress, setTranslationProgress] = useState<{
    completed: number;
    total: number;
  } | null>(null);
  const [translatingBatch, setTranslatingBatch] = useState(false);
  const [translationNotice, setTranslationNotice] = useState("");
  const knownEvents = useRef<Set<string>>(new Set());
  const translationRequestSequence = useRef(0);
  const currentEventFingerprints = useRef<Map<string, string>>(new Map());

  const load = useCallback(async (profileId = activeProfile, quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const parameters = new URLSearchParams({ limit: "100" });
      if (profileId) parameters.set("profile_id", profileId);
      const items = await apiFetch<MonitorEvent[]>(
        `/api/v1/monitor/feed?${parameters.toString()}`,
      );
      if (
        quiet &&
        notificationsEnabled &&
        typeof Notification !== "undefined" &&
        Notification.permission === "granted"
      ) {
        items
          .filter(
            (event) =>
              !knownEvents.current.has(eventKey(event)) &&
              ["critical", "high"].includes(event.market_relevance),
          )
          .slice(0, 3)
          .forEach((event) => {
            new Notification(`${event.profile_name} · 市场相关 ${event.market_relevance}`, {
              body: event.analysis || event.title,
              tag: eventKey(event),
            });
          });
      }
      knownEvents.current = new Set(items.map(eventKey));
      currentEventFingerprints.current = new Map(
        items.map((event) => [
          eventKey(event),
          eventTranslationFingerprint(event),
        ]),
      );
      setEvents(items);
      setError("");
    } catch (reason) {
      if (!quiet) setError(reason instanceof Error ? reason.message : "加载失败");
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [activeProfile, notificationsEnabled]);

  async function refresh() {
    setRefreshing(true);
    setError("");
    setNotice("");
    const settings = readLlmSettings();
    try {
      const result = await apiFetch<MonitorRefreshResult>("/api/v1/monitor/refresh", {
        method: "POST",
        body: JSON.stringify({
          api_key: settings.apiKey || undefined,
          base_url: settings.baseUrl,
          model: settings.model,
        }),
      });
      const unavailable = Object.entries(result.source_status)
        .filter(([, source]) => source.attempted > 0 && source.succeeded === 0)
        .map(([source]) => source);
      const degradedNote =
        unavailable.length > 0
          ? ` 未连接：${unavailable.join("、")}；页面不会用行情冒充人物原文。`
          : "";
      setNotice(
        `本轮读取 ${result.fetched} 条事件；分析方式：${
          result.analysis_method === "ai" ? "AI 模型" : "可解释规则"
        }。${degradedNote}`,
      );
      const nextStatus = await apiFetch<MonitorStatus>("/api/v1/monitor/status");
      setStatus(nextStatus);
      await load(activeProfile);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "刷新公开信号失败");
    } finally {
      setRefreshing(false);
    }
  }

  async function enableNotifications() {
    if (typeof Notification === "undefined") {
      setError("当前浏览器不支持系统通知。");
      return;
    }
    const permission = await Notification.requestPermission();
    setNotificationsEnabled(permission === "granted");
    if (permission !== "granted") {
      setError("浏览器通知未获授权；站内事件流仍会自动刷新。");
    }
  }

  async function translateEvents(targetEvents: MonitorEvent[], batch = false) {
    const targets = untranslatedEvents(targetEvents, translations);
    if (targets.length === 0) {
      if (batch) {
        setTranslationNotice("当前显示的事件已经有可用中文译文。");
      }
      return;
    }
    const requestId = ++translationRequestSequence.current;
    const fingerprints = new Map(
      targets.map((event) => [eventKey(event), eventTranslationFingerprint(event)]),
    );
    if (batch) {
      setTranslationNotice("");
      setTranslationProgress({ completed: 0, total: targets.length });
      setTranslatingBatch(true);
    }
    setTranslations((current) => {
      const next = { ...current };
      targets.forEach((event) => {
        next[eventKey(event)] = {
          status: "loading",
          fingerprint: fingerprints.get(eventKey(event)) ?? "",
          requestId,
        };
      });
      return next;
    });

    let completed = 0;
    let succeeded = 0;
    let failed = 0;
    for (const chunk of chunkEvents(targets)) {
      try {
        const response = await apiFetch<MonitorTranslationResponse>(
          "/api/v1/monitor/translations",
          {
            method: "POST",
            body: JSON.stringify({
              items: translationRequestItems(chunk),
            }),
          },
        );
        const translatedByKey = new Map(
          response.items.map((item) => [
            eventReferenceKey(item.source, item.source_id),
            item,
          ]),
        );
        const chunkResults = chunk.map((event) => {
          const key = eventKey(event);
          const translated = translatedByKey.get(key);
          const fingerprint = fingerprints.get(key) ?? "";
          const stale =
            currentEventFingerprints.current.get(key) !== fingerprint;
          const ready = Boolean(
            translated &&
              !stale &&
              ["translated", "identity", "partial"].includes(translated.status),
          );
          return { event, key, translated, ready, stale };
        });
        const translatedKeys = chunkResults
          .filter((result) => result.ready)
          .map((result) => result.key);
        succeeded += translatedKeys.length;
        failed += chunkResults.length - translatedKeys.length;
        setTranslations((current) => {
          const next = { ...current };
          chunkResults.forEach((result) => {
            const fingerprint = fingerprints.get(result.key) ?? "";
            const active = current[result.key];
            if (!isActiveTranslationRequest(active, fingerprint, requestId)) {
              return;
            }
            if (result.ready && result.translated) {
              next[result.key] = {
                status: "ready",
                fingerprint,
                value: result.translated,
              };
            } else {
              next[result.key] = {
                status: "error",
                fingerprint,
                message: result.stale
                  ? "事件内容已更新，请对最新版本重新翻译。"
                  : translationErrorMessage(result.translated?.status),
              };
            }
          });
          return next;
        });
        if (translatedKeys.length > 0) {
          setTranslationModes((current) => {
            const next = { ...current };
            translatedKeys.forEach((key) => {
              next[key] = "zh";
            });
            return next;
          });
        }
      } catch (reason) {
        const message =
          reason instanceof Error ? reason.message : "翻译服务暂时不可用。";
        failed += chunk.length;
        setTranslations((current) => {
          const next = { ...current };
          chunk.forEach((event) => {
            const key = eventKey(event);
            const fingerprint = fingerprints.get(key) ?? "";
            const active = current[key];
            if (!isActiveTranslationRequest(active, fingerprint, requestId)) {
              return;
            }
            next[key] = {
              status: "error",
              fingerprint,
              message,
            };
          });
          return next;
        });
      }
      completed += chunk.length;
      if (batch) {
        setTranslationProgress({ completed, total: targets.length });
      }
    }
    if (batch) {
      setTranslationNotice(
        failed > 0
          ? `已完成 ${succeeded} 条，${failed} 条暂未翻译；原文完整保留，可单独重试。`
          : `当前 ${succeeded} 条已切换为中文；译文来自 NAS 离线模型，原文可随时切回。`,
      );
      setTranslationProgress(null);
      setTranslatingBatch(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      apiFetch<MonitorEvent[]>("/api/v1/monitor/feed?limit=100"),
      apiFetch<MonitorProfile[]>("/api/v1/monitor/profiles"),
      apiFetch<MonitorStatus>("/api/v1/monitor/status"),
    ])
      .then(([items, profileItems, monitorStatus]) => {
        if (!cancelled) {
          knownEvents.current = new Set(items.map(eventKey));
          currentEventFingerprints.current = new Map(
            items.map((event) => [
              eventKey(event),
              eventTranslationFingerprint(event),
            ]),
          );
          setEvents(items);
          setProfiles(profileItems);
          setStatus(monitorStatus);
        }
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "加载失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void load(undefined, true);
      void apiFetch<MonitorStatus>("/api/v1/monitor/status").then(setStatus).catch(() => null);
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const visibleEvents = events.filter((event) => {
    if (kindFilter === "signals") {
      return event.kind !== "market" && event.market_relevance !== "unrelated";
    }
    return event.kind === kindFilter;
  });
  const displayedEvents = visibleEvents.slice(0, displayLimit);
  const xStatus = status?.scheduler.sources["x-api"];
  const publicStatus = status?.scheduler.sources["public-rss"];
  const geoStatus = status?.scheduler.sources["un-news"];
  const officialStatus = status?.scheduler.sources["official-intel"];
  const gdeltStatus = status?.scheduler.sources["gdelt-headlines"];
  const marketStatus = status?.scheduler.sources["linked-market-data"];
  const secStatus = status?.scheduler.sources["sec-edgar"];
  const visibleTranslationLoading = displayedEvents.some(
    (event) => currentTranslationEntry(event, translations)?.status === "loading",
  );
  const remainingTranslations = untranslatedEvents(
    displayedEvents,
    translations,
  ).length;
  const translatedVisibleCount =
    displayedEvents.length - remainingTranslations;

  return (
    <div className="page">
      <header className="topbar">
        <div>
          <span className="eyebrow">Market intelligence</span>
          <h1>市场脉搏</h1>
        </div>
        <div className="topbar-actions">
          <button className="text-button" onClick={enableNotifications}>
            {notificationsEnabled ? "浏览器提醒已开启" : "开启关键提醒"}
          </button>
          <button className="text-button" disabled={loading} onClick={() => load()}>
            重新读取
          </button>
          <button className="secondary-button" disabled={refreshing} onClick={refresh}>
            {refreshing ? "正在抓取与分析…" : "立即刷新 ↗"}
          </button>
        </div>
      </header>

      <section className="monitor-intro">
        <div>
          <span className="live-ring" />
          <div>
            <strong>全球新闻 + 官方政策 + 地缘事件 + 市场传导</strong>
            <p>
              NAS 自动轮询免费公开源并按原始 ID 去重；聚合标题、官方原文和行情佐证分层展示。
            </p>
          </div>
        </div>
        <span className="muted">情景分析不是确定预测，也不构成投资建议</span>
      </section>

      <div className="monitor-health">
        <StatusCard
          detail={
            xStatus?.message ||
            (status?.x_configured
              ? "X 凭据已配置，等待轮询。"
              : "按当前选择暂不启用付费 X API；免费新闻只显示标题提及，不代表人物原文。")
          }
          label="X 官方原文"
          ok={Boolean(status?.x_configured && xStatus?.succeeded)}
        />
        <StatusCard
          detail={
            publicStatus?.message ||
            "等待连接免费人物公开 RSS；这是非官方聚合层，必须打开原始链接复核。"
          }
          label="人物公开镜像（非官方）"
          ok={Boolean(publicStatus?.succeeded)}
        />
        <StatusCard
          detail={officialStatus?.message || "等待连接央行、监管、能源和灾害官方源。"}
          label="免费官方情报"
          ok={Boolean(officialStatus?.succeeded)}
        />
        <StatusCard
          detail={gdeltStatus?.message || "等待连接全球公开新闻标题流。"}
          label="全球新闻聚合"
          ok={Boolean(gdeltStatus?.succeeded)}
        />
        <StatusCard
          detail={marketStatus?.message || "等待自动补充关联资产真实行情。"}
          label="真实行情佐证"
          ok={Boolean(marketStatus?.succeeded)}
        />
        <StatusCard
          detail={secStatus?.message || "等待轮询 SEC EDGAR 13F 官方申报。"}
          label="SEC 13F"
          ok={Boolean(secStatus?.succeeded)}
        />
        <StatusCard
          detail={geoStatus?.message || "联合国战争与冲突公开源等待首次轮询。"}
          label="战争 / 地缘事件"
          ok={Boolean(geoStatus?.succeeded)}
        />
        <StatusCard
          detail={
            status?.analysis_configured
              ? "NAS 已配置模型分析；失败时自动降级到规则。"
              : "当前使用可解释规则；配置服务端 LLM Key 后自动启用 AI。"
          }
          label="影响分析"
          ok={Boolean(status?.analysis_configured)}
        />
        <StatusCard
          detail={
            status?.translation?.enabled
              ? status.translation.availability === "degraded"
                ? "NAS 离线翻译服务暂时繁忙；已有缓存仍可使用，原文不会受影响。"
                : "英中模型仅在 NAS 内网运行；按需翻译当前列表并缓存译文。"
              : "中文翻译服务尚未启用；原始新闻仍可正常阅读。"
          }
          label="中文翻译"
          ok={Boolean(
            status?.translation?.enabled &&
              status.translation.availability !== "degraded",
          )}
        />
        <StatusCard
          detail={
            status?.scheduler.running
              ? `每 ${status.scheduler.poll_seconds} 秒轮询；上次 ${
                  status.scheduler.last_refresh_at
                    ? formatDate(status.scheduler.last_refresh_at)
                    : "等待中"
                }`
              : "后台轮询尚未启用。"
          }
          label="实时监控"
          ok={Boolean(status?.scheduler.running)}
        />
      </div>

      <div className="signal-kind-filters" aria-label="事件类型筛选">
        {KIND_FILTERS.map((item) => (
          <button
            className={kindFilter === item.id ? "active" : ""}
            key={item.id}
            onClick={() => {
              setKindFilter(item.id);
              setDisplayLimit(18);
            }}
          >
            {item.label} <span>{filterCount(events, item.id)}</span>
          </button>
        ))}
      </div>

      <div className="profile-filters" aria-label="监控对象筛选">
        <button
          className={activeProfile === "" ? "active" : ""}
          onClick={() => {
            setActiveProfile("");
            setDisplayLimit(18);
            void load("");
          }}
        >
          全部 <span>{profiles.length}</span>
        </button>
        {profiles
          .filter((profile) => profile.handle || profile.cik)
          .map((profile) => (
            <button
              className={activeProfile === profile.id ? "active" : ""}
              key={profile.id}
              onClick={() => {
                setActiveProfile(profile.id);
                setDisplayLimit(18);
                void load(profile.id);
              }}
            >
              {profile.display_name}
              <span>{profile.cik ? "13F" : `@${profile.handle}`}</span>
            </button>
          ))}
      </div>

      <section
        aria-busy={translatingBatch}
        aria-label="新闻中文翻译"
        className="monitor-translation-toolbar"
      >
        <div>
          <span>ZH</span>
          <div>
            <strong>当前新闻一键翻译</strong>
            <p>
              只处理当前显示的 {displayedEvents.length} 条；NAS 离线英中模型，
              不覆盖原文，也不会随 30 秒轮询自动消耗资源。
            </p>
          </div>
        </div>
        <div className="monitor-translation-actions">
          <small aria-live="polite">
            {translationProgress
              ? `正在翻译 ${translationProgress.completed} / ${translationProgress.total}`
              : translatedVisibleCount > 0
                ? `本页已有 ${translatedVisibleCount} 条中文`
                : "尚未翻译当前列表"}
          </small>
          <button
            className="translation-batch-button"
            disabled={
              loading ||
              displayedEvents.length === 0 ||
              remainingTranslations === 0 ||
              translatingBatch ||
              visibleTranslationLoading ||
              !status?.translation?.enabled
            }
            onClick={() => void translateEvents(displayedEvents, true)}
            type="button"
          >
            {translatingBatch
              ? `翻译中 ${translationProgress?.completed ?? 0} / ${
                  translationProgress?.total ?? displayedEvents.length
                }`
              : status === null
                ? "正在确认翻译服务…"
                : !status.translation?.enabled
                ? "中文翻译未启用"
                : remainingTranslations === 0
                  ? "当前列表已翻译"
                  : translatedVisibleCount > 0
                    ? `翻译新增 ${remainingTranslations} 条`
                    : `一键翻译当前 ${displayedEvents.length} 条`}
          </button>
        </div>
      </section>

      {notice && <p className="notice-banner">{notice}</p>}
      {translationNotice && (
        <p className="notice-banner" aria-live="polite">
          {translationNotice}
        </p>
      )}
      {error && <p className="error-banner">{error}</p>}
      <div className="timeline">
        {displayedEvents.map((event) => {
          const key = eventKey(event);
          return (
            <MonitorEventCard
              displayMode={translationModes[key] ?? "original"}
              event={event}
              key={key}
              onDisplayMode={(mode) =>
                setTranslationModes((current) => ({ ...current, [key]: mode }))
              }
              onTranslate={() => void translateEvents([event])}
              translationDisabled={
                translatingBatch || !status?.translation?.enabled
              }
              translationEntry={currentTranslationEntry(event, translations)}
            />
          );
        })}
        {!loading && visibleEvents.length === 0 && (
          <div className="monitor-empty">
            <span>这个筛选下还没有真实事件</span>
            <p>
              免费官方源、全球新闻和地缘事件会由 NAS 后台自动刷新；X 原文暂未启用时，
              新闻聚合标题仍会明确标注来源，不会伪装成人物原帖。
            </p>
          </div>
        )}
        {displayedEvents.length < visibleEvents.length && (
          <button
            className="timeline-load-more"
            onClick={() => setDisplayLimit((value) => value + 18)}
            type="button"
          >
            继续显示 18 条
            <span>
              已显示 {displayedEvents.length} / {visibleEvents.length}
            </span>
          </button>
        )}
      </div>
    </div>
  );
}

function StatusCard({
  label,
  detail,
  ok,
}: {
  label: string;
  detail: string;
  ok: boolean;
}) {
  return (
    <div className={ok ? "ok" : "degraded"}>
      <span>{label}</span>
      <strong>{ok ? "已连接" : "需注意"}</strong>
      <p>{detail}</p>
    </div>
  );
}

function filterCount(events: MonitorEvent[], filter: KindFilter): number {
  if (filter === "signals") {
    return events.filter(
      (event) => event.kind !== "market" && event.market_relevance !== "unrelated",
    ).length;
  }
  return events.filter((event) => event.kind === filter).length;
}

function translationErrorMessage(status?: string): string {
  if (status === "not_found") {
    return "这条事件已经更新或暂不可见，请重新读取后再试。";
  }
  return "NAS 翻译服务暂时不可用；原文已完整保留，请稍后重试。";
}
