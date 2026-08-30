"use client";

import { useMemo, useState } from "react";

import { apiFetch } from "@/lib/api";
import type {
  PortfolioPaperObservation,
  PortfolioPaperObservationPhase,
  PortfolioPaperPreflight,
  PortfolioPaperPublicStatus,
} from "@/lib/types";

const PHASE_COPY: Record<
  PortfolioPaperObservationPhase,
  { label: string; detail: string; tone: "ready" | "watch" | "done" | "alert" }
> = {
  awaiting_opening: {
    label: "等待模拟开盘",
    detail: "已冻结目标；尚未形成模拟建仓批次。",
    tone: "ready",
  },
  opening_window_expired: {
    label: "开盘窗口已过",
    detail: "这次一会话观察没有形成可验证的模拟建仓。",
    tone: "alert",
  },
  awaiting_close_valuation: {
    label: "等待收盘估值",
    detail: "模拟开仓已记录；只接受完成并额外确认过的日线。",
    tone: "watch",
  },
  valued: {
    label: "已完成估值",
    detail: "这是收盘估值，不代表平仓成交或真实账户收益。",
    tone: "done",
  },
};

const METHOD_COPY: Record<PortfolioPaperObservation["method"], string> = {
  initial_equal_hold: "起点等权",
  periodic_equal: "定期等权",
  periodic_inverse_volatility: "逆波动风险配置",
};

function percent(value: number): string {
  return `${(value * 100).toFixed(2)}%`;
}

function money(value: number): string {
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 2,
    minimumFractionDigits: 2,
  }).format(value);
}

function timestamp(value: string | null): string {
  if (!value) return "未发生";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return "时间不可用";
  return parsed.toLocaleString("zh-CN", {
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
}

function weights(weights: Record<string, number>): string {
  return Object.entries(weights)
    .map(([symbol, value]) => `${symbol} ${percent(value)}`)
    .join(" · ");
}

export function PortfolioForwardObservation({
  observations,
  status,
  loading,
  error,
  experimentNames,
}: {
  observations: readonly PortfolioPaperObservation[];
  status: PortfolioPaperPublicStatus | null;
  loading: boolean;
  error: string;
  experimentNames: Readonly<Record<string, string>>;
}) {
  const experimentOptions = useMemo(
    () => Object.entries(experimentNames).sort((left, right) => left[1].localeCompare(right[1], "zh-CN")),
    [experimentNames],
  );
  const [preflightExperimentId, setPreflightExperimentId] = useState("");
  const [preflight, setPreflight] = useState<PortfolioPaperPreflight | null>(null);
  const [preflightError, setPreflightError] = useState("");
  const [preflightLoading, setPreflightLoading] = useState(false);
  const selectedPreflightExperimentId = experimentNames[preflightExperimentId]
    ? preflightExperimentId
    : (experimentOptions[0]?.[0] ?? "");
  const openingReady = Boolean(status?.opening.enabled && status.opening.running);
  const settlementReady = Boolean(
    status?.settlement.enabled && status.settlement.running,
  );

  async function reviewPreflight() {
    if (!selectedPreflightExperimentId || preflightLoading) return;
    setPreflightLoading(true);
    setPreflightError("");
    try {
      const result = await apiFetch<PortfolioPaperPreflight>(
        `/api/v1/portfolio-paper/preflight/${encodeURIComponent(selectedPreflightExperimentId)}`,
      );
      setPreflight(result);
    } catch (reason) {
      setPreflight(null);
      setPreflightError(
        reason instanceof Error ? reason.message : "准备度检查暂时不可用",
      );
    } finally {
      setPreflightLoading(false);
    }
  }

  return (
    <section
      aria-busy={loading}
      aria-live="polite"
      className="portfolio-observations"
    >
      <div className="portfolio-observations-heading">
        <div>
          <span className="eyebrow">ONE-SESSION MODELED OBSERVATION</span>
          <h2>组合前向观察</h2>
          <p>
            仅展示已持久化的模拟开盘与收盘估值。不会连接券商、不会下单；收盘估值也不是平仓成交。
          </p>
        </div>
        <div className="portfolio-observation-readiness" aria-label="观察服务状态">
          <span className={openingReady ? "ready" : ""}>
            模拟开盘 {openingReady ? "运行中" : "未启用"}
          </span>
          <span className={settlementReady ? "ready" : ""}>
            收盘估值 {settlementReady ? "运行中" : "未启用"}
          </span>
        </div>
      </div>

      <div className="portfolio-observation-preflight">
        <div>
          <strong>组合观察准备度</strong>
          <p>
            只读取已保存实验和 Binance 公开交易规则，帮助确认范围；不会创建观察、启动调度、连接账户或下单。
          </p>
        </div>
        {experimentOptions.length > 0 ? (
          <div className="portfolio-observation-preflight-controls">
            <label>
              <span>已保存实验</span>
              <select
                aria-label="选择要检查的组合实验"
                disabled={preflightLoading}
                onChange={(event) => {
                  setPreflightExperimentId(event.target.value);
                  setPreflight(null);
                  setPreflightError("");
                }}
                value={selectedPreflightExperimentId}
              >
                {experimentOptions.map(([id, name]) => (
                  <option key={id} value={id}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="secondary-button"
              disabled={preflightLoading || !selectedPreflightExperimentId}
              onClick={() => void reviewPreflight()}
              type="button"
            >
              {preflightLoading ? "正在复核…" : "检查准备度"}
            </button>
          </div>
        ) : (
          <span className="portfolio-observation-preflight-muted">
            先保存一个组合实验，才能对其服务端回执进行范围复核。
          </span>
        )}
      </div>

      {preflightError && (
        <p className="portfolio-observation-error" role="alert">
          准备度检查暂时不可用：{preflightError}
        </p>
      )}

      {preflight && (
        <div
          className={`portfolio-observation-preflight-result ${preflight.review_status}`}
          role="status"
        >
          <div>
            <strong>
              {preflight.review_status === "ready_for_internal_review"
                ? "可进入内部人工审查"
                : preflight.review_status === "verification_incomplete"
                  ? "交易规则复核未完成"
                  : "当前不在观察范围内"}
            </strong>
            <p>{preflight.next_step}</p>
          </div>
          <ul>
            {preflight.assets.map((asset) => (
              <li key={asset.symbol}>
                <span className={`portfolio-observation-preflight-status ${asset.status}`}>
                  {asset.status === "eligible_for_internal_review"
                    ? "已通过规则复核"
                    : asset.status === "verification_unavailable"
                      ? "暂无法复核"
                      : "仅研究"}
                </span>
                <strong>{asset.symbol}</strong>
                <small>{asset.reasons.join("；")}</small>
              </li>
            ))}
          </ul>
        </div>
      )}

      {error ? (
        <p className="portfolio-observation-error" role="alert">
          前向观察暂时不可读取：{error}
        </p>
      ) : loading && observations.length === 0 ? (
        <div className="portfolio-observation-empty" role="status">
          正在读取 NAS 中的前向观察记录…
        </div>
      ) : observations.length === 0 ? (
        <div className="portfolio-observation-empty">
          <strong>尚无一会话模拟观察</strong>
          <span>
            这里不会自动把历史回测变成交易。安全的激活登记尚未开放，因此不会显示误导性的“开始跟踪”按钮。
          </span>
        </div>
      ) : (
        <div className="portfolio-observation-grid">
          {observations.map((observation) => {
            const phase = PHASE_COPY[observation.phase];
            const valuation = observation.valuation;
            const experimentName = experimentNames[observation.portfolio_experiment_id];
            return (
              <article
                className={`portfolio-observation-card ${phase.tone}`}
                key={observation.id}
              >
                <div className="portfolio-observation-card-top">
                  <span className={`portfolio-observation-phase ${phase.tone}`}>
                    {phase.label}
                  </span>
                  {observation.attention_required && <span>需要复核</span>}
                </div>
                <h3>{experimentName ?? "已冻结组合实验"}</h3>
                <p>{phase.detail}</p>
                <div className="portfolio-observation-symbols">
                  {observation.symbols.map((symbol) => (
                    <span key={symbol}>{symbol}</span>
                  ))}
                </div>
                <dl className="portfolio-observation-facts">
                  <div>
                    <dt>配置方法</dt>
                    <dd>{METHOD_COPY[observation.method]}</dd>
                  </div>
                  <div>
                    <dt>信息日</dt>
                    <dd>{observation.information_session.slice(0, 10)}</dd>
                  </div>
                  <div>
                    <dt>模拟开盘</dt>
                    <dd>{timestamp(observation.opening_at)}</dd>
                  </div>
                  <div>
                    <dt>收盘估值</dt>
                    <dd>{timestamp(observation.valuation_at)}</dd>
                  </div>
                </dl>
                <div className="portfolio-observation-weights">
                  <span>冻结目标权重</span>
                  <strong>{weights(observation.target_weights)}</strong>
                </div>
                {valuation && (
                  <dl className="portfolio-observation-valuation">
                    <div>
                      <dt>模拟估值收益</dt>
                      <dd className={valuation.total_return >= 0 ? "positive" : "negative"}>
                        {percent(valuation.total_return)}
                      </dd>
                    </div>
                    <div>
                      <dt>最大回撤</dt>
                      <dd className="negative">{percent(valuation.max_drawdown)}</dd>
                    </div>
                    <div>
                      <dt>估值权益</dt>
                      <dd>{money(valuation.equity)} USDT</dd>
                    </div>
                    <div>
                      <dt>模型总成本</dt>
                      <dd>{money(valuation.total_cost)} USDT</dd>
                    </div>
                  </dl>
                )}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
