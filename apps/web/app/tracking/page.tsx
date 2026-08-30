"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { SourceBadge } from "@/components/source-badge";
import { apiFetch } from "@/lib/api";
import { formatPercent } from "@/lib/format";
import type {
  PaperSchedulerStatus,
  PaperSignalSnapshot,
  PaperTrack,
} from "@/lib/types";

const SIGNAL_LABELS = {
  pending_entry: "待下一根 K 线开盘买入",
  pending_exit: "待下一根 K 线开盘退出",
  holding: "持仓中",
  cash: "空仓等待",
};
const FORWARD_EVIDENCE_TARGET = 30;
const HEALTH_STATUS_LABELS = {
  baseline: "仅基线",
  collecting: "证据积累",
  healthy: "未触发预警",
  watch: "需关注",
  review: "建议复核",
};
const DRAWDOWN_SOURCE_LABELS = {
  saved_constraint: "保存的回撤硬约束",
  holdout_reference: "留出期回撤参考线",
  default_reference: "默认复核参考线",
};

function localTime(value: string): string {
  return new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatMoney(value: number, currency: string): string {
  try {
    return new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency,
      currencyDisplay: "narrowSymbol",
      maximumFractionDigits: 2,
    }).format(value);
  } catch {
    return `${value.toLocaleString("zh-CN", {
      maximumFractionDigits: 2,
    })} ${currency}`;
  }
}

function signalTone(snapshot: PaperSignalSnapshot): string {
  if (snapshot.signal_state.status === "pending_entry") return "entry";
  if (snapshot.signal_state.status === "pending_exit") return "exit";
  if (snapshot.signal_state.status === "holding") return "holding";
  return "cash";
}

function ForwardEquityChart({
  snapshots,
}: {
  snapshots: PaperSignalSnapshot[];
}) {
  const latestByBar = new Map<string, PaperSignalSnapshot>();
  for (const snapshot of snapshots) {
    if (snapshot.forward && !latestByBar.has(snapshot.data_as_of)) {
      latestByBar.set(snapshot.data_as_of, snapshot);
    }
  }
  const points = [...latestByBar.values()]
    .reverse()
    .map((snapshot) => ({
      strategy: snapshot.forward?.total_return ?? 0,
      benchmark: snapshot.forward?.benchmark_total_return ?? 0,
    }));
  if (points.length === 0) return null;

  const width = 420;
  const height = 86;
  const padding = 7;
  const values = points.flatMap((point) => [point.strategy, point.benchmark, 0]);
  let minimum = Math.min(...values);
  let maximum = Math.max(...values);
  if (maximum - minimum < 0.002) {
    minimum -= 0.001;
    maximum += 0.001;
  }
  const x = (index: number) =>
    points.length === 1
      ? width / 2
      : padding + (index / (points.length - 1)) * (width - padding * 2);
  const y = (value: number) =>
    padding +
    ((maximum - value) / (maximum - minimum)) * (height - padding * 2);
  const line = (key: "strategy" | "benchmark") =>
    points.map((point, index) => `${x(index)},${y(point[key])}`).join(" ");
  const latestIndex = points.length - 1;

  return (
    <div className="paper-ledger-chart">
      <div>
        <span>前向净值路径</span>
        <small>最近 {points.length} 个去重 K 线快照</small>
      </div>
      <svg
        aria-label="策略与买入持有的激活后纸面收益路径"
        role="img"
        viewBox={`0 0 ${width} ${height}`}
      >
        <line
          className="paper-ledger-zero"
          x1={padding}
          x2={width - padding}
          y1={y(0)}
          y2={y(0)}
        />
        <polyline
          className="paper-ledger-benchmark-line"
          points={line("benchmark")}
        />
        <polyline className="paper-ledger-strategy-line" points={line("strategy")} />
        <circle
          className="paper-ledger-benchmark-dot"
          cx={x(latestIndex)}
          cy={y(points[latestIndex].benchmark)}
          r="2.6"
        />
        <circle
          className="paper-ledger-strategy-dot"
          cx={x(latestIndex)}
          cy={y(points[latestIndex].strategy)}
          r="2.8"
        />
      </svg>
      <div className="paper-ledger-legend">
        <span className="strategy">策略</span>
        <span className="benchmark">买入持有</span>
        <small>虚线为 0%</small>
      </div>
    </div>
  );
}

export default function TrackingPage() {
  const [tracks, setTracks] = useState<PaperTrack[]>([]);
  const [scheduler, setScheduler] = useState<PaperSchedulerStatus | null>(null);
  const [operation, setOperation] = useState("");
  const [error, setError] = useState("");
  const [notificationPermission, setNotificationPermission] =
    useState<NotificationPermission>("default");
  const latestSnapshotIds = useRef<Map<string, string>>(new Map());
  const initialized = useRef(false);

  useEffect(() => {
    let disposed = false;
    async function load() {
      try {
        const [nextTracks, nextScheduler] = await Promise.all([
          apiFetch<PaperTrack[]>("/api/v1/paper-tracks"),
          apiFetch<PaperSchedulerStatus>("/api/v1/paper-tracks/scheduler"),
        ]);
        if (disposed) return;
        if ("Notification" in window) {
          setNotificationPermission(Notification.permission);
        }
        if (
          initialized.current &&
          "Notification" in window &&
          Notification.permission === "granted"
        ) {
          for (const track of nextTracks) {
            const latest = track.snapshots[0];
            const previousId = latestSnapshotIds.current.get(track.id);
            if (
              latest &&
              previousId &&
              previousId !== latest.id &&
              ["pending_entry", "pending_exit"].includes(
                latest.signal_state.status,
              )
            ) {
              new Notification(`QuantSieve · ${track.experiment.name}`, {
                body: `${SIGNAL_LABELS[latest.signal_state.status]} · ${track.experiment.instrument.symbol} · 数据 ${latest.data_as_of.slice(0, 16)}`,
              });
            }
          }
        }
        latestSnapshotIds.current = new Map(
          nextTracks
            .filter((track) => track.snapshots[0])
            .map((track) => [track.id, track.snapshots[0].id]),
        );
        initialized.current = true;
        setTracks(nextTracks);
        setScheduler(nextScheduler);
      } catch (reason) {
        if (!disposed) {
          setError(
            reason instanceof Error ? reason.message : "读取纸面跟踪失败",
          );
        }
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, []);

  async function enableNotifications() {
    if (!("Notification" in window)) return;
    const permission = await Notification.requestPermission();
    setNotificationPermission(permission);
  }

  async function refresh(track: PaperTrack) {
    setOperation(`refresh:${track.id}`);
    setError("");
    try {
      const updated = await apiFetch<PaperTrack>(
        `/api/v1/paper-tracks/${track.id}/refresh`,
        { method: "POST" },
      );
      setTracks((items) =>
        items.map((item) => (item.id === updated.id ? updated : item)),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "刷新信号失败");
    } finally {
      setOperation("");
    }
  }

  async function toggleStatus(track: PaperTrack) {
    setOperation(`status:${track.id}`);
    setError("");
    try {
      const updated = await apiFetch<PaperTrack>(
        `/api/v1/paper-tracks/${track.id}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            status: track.status === "active" ? "paused" : "active",
          }),
        },
      );
      setTracks((items) =>
        items.map((item) => (item.id === updated.id ? updated : item)),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "更新状态失败");
    } finally {
      setOperation("");
    }
  }

  async function remove(track: PaperTrack) {
    if (!window.confirm(`移除纸面跟踪「${track.experiment.name}」？`)) return;
    setOperation(`delete:${track.id}`);
    setError("");
    try {
      await apiFetch<void>(`/api/v1/paper-tracks/${track.id}`, {
        method: "DELETE",
      });
      setTracks((items) => items.filter((item) => item.id !== track.id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "移除纸面跟踪失败");
    } finally {
      setOperation("");
    }
  }

  const activeCount = tracks.filter((track) => track.status === "active").length;
  const snapshotCount = tracks.reduce(
    (total, track) => total + track.snapshot_count,
    0,
  );
  const pendingCount = tracks.filter((track) =>
    ["pending_entry", "pending_exit"].includes(
      track.snapshots[0]?.signal_state.status ?? "",
    ),
  ).length;

  return (
    <div className="page">
      <header className="topbar">
        <div>
          <span className="eyebrow">Forward signal watch</span>
          <h1>纸面跟踪</h1>
        </div>
        <div className="tracking-top-actions">
          <button
            className="secondary-button"
            disabled={
              typeof window !== "undefined" &&
              (!("Notification" in window) ||
                notificationPermission === "denied")
            }
            onClick={() => void enableNotifications()}
            type="button"
          >
            {notificationPermission === "granted"
              ? "信号提醒已开启"
              : notificationPermission === "denied"
                ? "提醒已被浏览器拒绝"
                : "开启信号变化提醒"}
          </button>
          <Link className="secondary-button" href="/backtest">
            从实验档案添加 ↗
          </Link>
        </div>
      </header>

      <section className="tracking-safety">
        <div>
          <span className="eyebrow">NO BROKER · NO ORDERS · FORWARD ONLY</span>
          <h2>逐根计算今后的纸面净值，不倒填激活前收益。</h2>
          <p>
            首个快照只建立零收益基线；此后按保存的开盘成交、费用、滑点与信号延迟计入新
            K 线。激活前的历史回测不会冒充纸面业绩，当前版本也不保存券商凭据或下单。
          </p>
        </div>
        <div className="tracking-summary">
          <div>
            <span>活跃策略</span>
            <strong>{activeCount}</strong>
          </div>
          <div>
            <span>信号快照</span>
            <strong>{snapshotCount}</strong>
          </div>
          <div>
            <span>待执行变化</span>
            <strong>{pendingCount}</strong>
          </div>
          <div>
            <span>NAS 后台刷新</span>
            <strong className="status">
              {scheduler?.running ? "运行中" : "未运行"}
            </strong>
          </div>
        </div>
      </section>

      {scheduler?.last_error && (
        <p className="error-banner">后台刷新：{scheduler.last_error}</p>
      )}
      {error && <p className="error-banner">{error}</p>}

      {tracks.length === 0 ? (
        <section className="tracking-empty">
          <span>◎</span>
          <h2>还没有纸面跟踪策略</h2>
          <p>
            先在回测工作台保存一组实验，确认基准、分段稳定性和留出验证，再从实验档案点击“纸面跟踪”。
          </p>
          <Link className="primary-button" href="/backtest">
            打开回测工作台
          </Link>
        </section>
      ) : (
        <div className="tracking-grid">
          {tracks.map((track) => {
            const latest = track.snapshots[0];
            const forward = latest?.forward ?? null;
            const forwardHealth = track.forward_health ?? null;
            const cycleDefinition =
              forwardHealth?.cycle_kind === "satellite_over_core"
                ? "卫星仓位升至防御核心之上，再回到核心"
                : "从空仓进入持仓，再完整回到空仓";
            const recentExecution =
              track.snapshots.find((snapshot) => snapshot.forward?.execution)
                ?.forward?.execution ?? null;
            const busy = operation.endsWith(track.id);
            const isReference = track.experiment.instrument.provider === "macro";
            const forwardBars = Math.max(track.observed_bar_count - 1, 0);
            const evidenceProgress = Math.min(
              forwardBars / FORWARD_EVIDENCE_TARGET,
              1,
            );
            const evidenceLabel =
              forwardBars === 0
                ? "仅建立基线"
                : forwardBars < 10
                  ? "早期观察"
                  : forwardBars < FORWARD_EVIDENCE_TARGET
                    ? "证据积累中"
                    : "完成首批观察";
            return (
              <article className="tracking-card" key={track.id}>
                <div className="tracking-card-heading">
                  <div>
                    <div className="tracking-badges">
                      <span
                        className={`track-status ${
                          track.last_error ? "error" : track.status
                        }`}
                      >
                        {track.last_error
                          ? "需处理"
                          : track.status === "active"
                            ? "跟踪中"
                            : "已暂停"}
                      </span>
                      <span>{track.experiment.interval}</span>
                      <span>{track.experiment.instrument.market}</span>
                    </div>
                    <h2>{track.experiment.name}</h2>
                    <p>
                      {track.experiment.instrument.name} ·{" "}
                      {track.experiment.instrument.symbol} ·{" "}
                      {track.experiment.strategy.name}
                    </p>
                  </div>
                  <span className="track-created">
                    激活 {new Date(track.created_at).toLocaleDateString("zh-CN")}
                  </span>
                </div>
                {track.experiment.validation?.forward_observation_eligible &&
                  !track.experiment.validation.validation_passed && (
                    <div className="provisional-track-notice" role="note">
                      <div>
                        <span>FROZEN PROVISIONAL CANDIDATE</span>
                        <strong>未通过最终验证 · 仅前向积累</strong>
                      </div>
                      <p>
                        最终留出期只有{" "}
                        {
                          track.experiment.validation.validation_metrics
                            .closed_trades
                        }{" "}
                        个已闭合独立决策周期。参数已经冻结，不再读取同一留出集调参；只有激活后新增
                        K 线和已闭合独立决策周期可以补充证据。
                      </p>
                    </div>
                  )}
                {isReference && (
                  <div className="reference-data-notice" role="note">
                    <strong>参考序列跟踪</strong>
                    <span>
                      此处跟踪的是下一次官方日频参考值，不是券商实时价，也不会产生真实委托。
                    </span>
                  </div>
                )}
                {track.last_error && (
                  <div className="tracking-refresh-alert" role="alert">
                    <div>
                      <span>FORWARD TRACKING INTERRUPTED</span>
                      <strong>这条跟踪没有连续更新</strong>
                    </div>
                    <p>{track.last_error}</p>
                    <small>
                      当前展示的是上次成功快照，不代表最新信号。处理数据窗口或上游问题后，请点击“刷新信号”重新确认。
                    </small>
                  </div>
                )}

                <section className="forward-evidence-progress">
                  <div>
                    <span className="eyebrow">FORWARD EVIDENCE</span>
                    <h3>激活后前向观察</h3>
                    <p>
                      {forwardBars === 0
                        ? "当前只有首个基线快照，尚未观察到新的 K 线。"
                        : `已经按时间顺序记录 ${forwardBars} 根激活后 K 线；继续等待真实信号变化。`}
                    </p>
                  </div>
                  <div className="forward-evidence-meter">
                    <div>
                      <strong>
                        {forwardBars} / {FORWARD_EVIDENCE_TARGET}
                      </strong>
                      <span>{evidenceLabel}</span>
                    </div>
                    <div
                      aria-label={`前向 K 线观察进度 ${forwardBars} / ${FORWARD_EVIDENCE_TARGET}`}
                      aria-valuemax={FORWARD_EVIDENCE_TARGET}
                      aria-valuemin={0}
                      aria-valuenow={Math.min(
                        forwardBars,
                        FORWARD_EVIDENCE_TARGET,
                      )}
                      className="forward-evidence-bar"
                      role="progressbar"
                    >
                      <i style={{ width: `${evidenceProgress * 100}%` }} />
                    </div>
                    <small>
                      30 根只是首批观察里程碑，不代表交易样本已经充分或策略已经有效。
                    </small>
                  </div>
                </section>

                {forward && forwardHealth && (
                  <section
                    className={`forward-health ${forwardHealth.status}`}
                  >
                    <div className="forward-health-heading">
                      <div>
                        <span className="eyebrow">FORWARD HEALTH CHECK</span>
                        <h3>{forwardHealth.title}</h3>
                        <p>{forwardHealth.summary}</p>
                      </div>
                      <strong>{HEALTH_STATUS_LABELS[forwardHealth.status]}</strong>
                    </div>

                    <div className="forward-health-evidence">
                      <div>
                        <span>已观察 K 线</span>
                        <strong>
                          {forwardHealth.evidence_bars} /{" "}
                          {forwardHealth.minimum_evidence_bars}
                        </strong>
                        <small>首批评估门槛</small>
                      </div>
                      <div>
                        <span>已闭合独立决策周期</span>
                        <strong>
                          {forwardHealth.round_trips} /{" "}
                          {forwardHealth.minimum_round_trips}
                        </strong>
                        <small>{cycleDefinition}，才算一个样本</small>
                      </div>
                      <div>
                        <span>前向回撤 / 复核线</span>
                        <strong>
                          {formatPercent(Math.abs(forward.max_drawdown))} /{" "}
                          {formatPercent(forwardHealth.drawdown_limit)}
                        </strong>
                        <small>
                          {
                            DRAWDOWN_SOURCE_LABELS[
                              forwardHealth.drawdown_limit_source
                            ]
                          }
                        </small>
                      </div>
                      <div>
                        <span>留出期等比参考</span>
                        <strong>
                          {forwardHealth.expected_return_reference == null
                            ? "暂无"
                            : formatPercent(
                                forwardHealth.expected_return_reference,
                              )}
                        </strong>
                        <small>仅作同期尺度参照，不是预测</small>
                      </div>
                    </div>

                    <div className="forward-health-trades">
                      {forwardHealth.round_trips > 0 && (
                        <>
                        <div>
                          <span>独立周期胜率</span>
                          <strong>
                            {forwardHealth.win_rate == null
                              ? "暂无"
                              : formatPercent(forwardHealth.win_rate)}
                          </strong>
                        </div>
                        <div>
                          <span>Wilson 95% 区间</span>
                          <strong>
                            {forwardHealth.win_rate_confidence_low == null ||
                            forwardHealth.win_rate_confidence_high == null
                              ? "暂无"
                              : `${formatPercent(
                                  forwardHealth.win_rate_confidence_low,
                                )}–${formatPercent(
                                  forwardHealth.win_rate_confidence_high,
                                )}`}
                          </strong>
                        </div>
                        <div>
                          <span>单周期期望</span>
                          <strong>
                            {forwardHealth.expectancy == null
                              ? "暂无"
                              : formatPercent(forwardHealth.expectancy)}
                          </strong>
                        </div>
                        <div>
                          <span>独立周期利润因子</span>
                          <strong>
                            {forwardHealth.profit_factor == null
                              ? "暂无"
                              : forwardHealth.profit_factor >= 999
                                ? "仅有盈利样本"
                                : forwardHealth.profit_factor.toFixed(2)}
                          </strong>
                        </div>
                        </>
                      )}
                      <div>
                        <span>年化仓位变动 · 持仓率</span>
                        <strong>
                          {forwardHealth.trades_per_year == null
                            ? "暂无"
                            : `${forwardHealth.trades_per_year.toFixed(1)} 次/年`}
                          {" · "}
                          {forwardHealth.exposure_ratio == null
                            ? "暂无"
                            : formatPercent(forwardHealth.exposure_ratio)}
                        </strong>
                      </div>
                    </div>

                    <ul className="forward-health-reasons">
                      {forwardHealth.reasons.map((reason) => (
                        <li key={reason}>{reason}</li>
                      ))}
                    </ul>
                    <p className="forward-health-disclosure">
                      健康状态只做研究分层，不会自动下单或改变跟踪状态；小样本阶段不会因为短期盈亏直接判定策略有效或失效。
                    </p>
                  </section>
                )}

                {forward && (
                  <section className="paper-ledger">
                    <div className="paper-ledger-heading">
                      <div>
                        <span className="eyebrow">FORWARD PAPER LEDGER</span>
                        <h3>激活后独立净值</h3>
                        <p>
                          {forward.bars === 0
                            ? "基线已经建立；等待第一根激活后新 K 线。"
                            : `${forward.bars} 根未见 K 线已按顺序计入纸面账本。`}
                        </p>
                      </div>
                      <span className="paper-ledger-mode">
                        规则模拟 · 非券商成交
                      </span>
                    </div>

                    <div className="paper-ledger-performance">
                      <div>
                        <span>策略纸面收益</span>
                        <strong
                          className={
                            forward.total_return >= 0 ? "positive" : "negative"
                          }
                        >
                          {formatPercent(forward.total_return)}
                        </strong>
                      </div>
                      <div>
                        <span>同期买入持有</span>
                        <strong
                          className={
                            forward.benchmark_total_return >= 0
                              ? "positive"
                              : "negative"
                          }
                        >
                          {formatPercent(forward.benchmark_total_return)}
                        </strong>
                      </div>
                      <div>
                        <span>前向超额收益</span>
                        <strong
                          className={
                            forward.excess_return >= 0 ? "positive" : "negative"
                          }
                        >
                          {formatPercent(forward.excess_return)}
                        </strong>
                      </div>
                      <div>
                        <span>前向最大回撤</span>
                        <strong
                          className={
                            forward.max_drawdown < 0 ? "negative" : undefined
                          }
                        >
                          {formatPercent(forward.max_drawdown)}
                        </strong>
                      </div>
                    </div>

                    <ForwardEquityChart snapshots={track.snapshots} />

                    <div className="paper-ledger-state">
                      <div>
                        <span>当前纸面净值</span>
                        <strong>
                          {formatMoney(
                            forward.equity,
                            track.experiment.instrument.currency,
                          )}
                        </strong>
                      </div>
                      <div>
                        <span>当前纸面仓位</span>
                        <strong>{forward.position > 0 ? "持有" : "空仓"}</strong>
                      </div>
                      <div>
                        <span>下一次开盘目标</span>
                        <strong>
                          {(forward.pending_targets[0] ?? 0) > 0
                            ? "持有"
                            : "空仓"}
                        </strong>
                      </div>
                      <div>
                        <span>仓位变动事件 / 已闭合周期</span>
                        <strong>
                          {forward.orders} /{" "}
                          {forward.schema_version === 1
                            ? 0
                            : forward.round_trips}
                        </strong>
                      </div>
                    </div>

                    {recentExecution && (
                      <div className="paper-ledger-execution">
                        <span>最近模拟成交</span>
                        <strong>
                          {recentExecution.side === "buy" ? "买入" : "卖出"} ·{" "}
                          {recentExecution.executed_at.slice(0, 10)} 开盘 ·
                          计摩擦成交价{" "}
                          {recentExecution.modeled_fill_price.toLocaleString(
                            "zh-CN",
                            { maximumFractionDigits: 4 },
                          )}
                        </strong>
                        <small>
                          本次费用与滑点合计{" "}
                          {formatPercent(recentExecution.friction_rate)}，折算账本扣减{" "}
                          {formatMoney(
                            recentExecution.friction_amount,
                            track.experiment.instrument.currency,
                          )}
                        </small>
                      </div>
                    )}

                    {forward.calculation_origin === "migration" && (
                      <p className="paper-ledger-migration">
                        此跟踪创建于前向净值功能上线前；净值从首次兼容刷新时重新建立基线，早期信号快照仍保留但不倒推收益。
                      </p>
                    )}
                    {(forward.schema_version === 1 ||
                      forward.quality_calculation_origin ===
                        "cycle_semantics_migration") && (
                      <p className="paper-ledger-migration">
                        旧版按资金批次计算的胜率与往返样本已排除，不会混入新证据；新口径只统计完整独立决策周期。
                        {forward.quality_sample_status ===
                          "awaiting_baseline_reset" &&
                          ` 当前先等待仓位回到${
                            forward.cycle_kind === "satellite_over_core"
                              ? "防御核心"
                              : "空仓"
                          }，再开始记录下一周期。`}
                      </p>
                    )}
                    <p className="paper-ledger-disclosure">
                      仅使用激活后实际新增的 K
                      线，并按实验保存的开盘成交、费用、滑点和信号延迟逐根复算；不连接账户、不下单，也不等同于真实可成交收益。
                      仓位变动事件用于交易频率；部分加减仓不会拆成多个胜率样本。
                    </p>
                  </section>
                )}

                {latest ? (
                  <>
                    <section className={`tracking-signal ${signalTone(latest)}`}>
                      <div>
                        <span className="eyebrow">LATEST STRATEGY SIGNAL</span>
                        <h3>{SIGNAL_LABELS[latest.signal_state.status]}</h3>
                        <p>
                          数据截至 {latest.data_as_of.replace("T", " ").slice(0, 16)} ·
                          {isReference ? "最新参考值" : "最新价"}{" "}
                          {latest.latest_price.toLocaleString("zh-CN")} ·{" "}
                          {latest.signal_state.bars_in_signal_state} 根 K 线未改变
                        </p>
                      </div>
                      <div className="tracking-position">
                        <span>滚动原始信号</span>
                        <strong>
                          {latest.signal_state.requested_signal > 0 ? "持有" : "空仓"}
                        </strong>
                        <span>滚动模型仓位</span>
                        <strong>
                          {latest.signal_state.executed_position > 0 ? "持有" : "空仓"}
                        </strong>
                      </div>
                    </section>
                    <p className="tracking-signal-note">
                      此处是策略在完整滚动历史窗口中的最新诊断；真正从激活时刻开始的纸面仓位与净值以上方独立账本为准。
                    </p>

                    <div className="tracking-evidence">
                      <div>
                        <span>滚动窗口历史收益</span>
                        <strong>{formatPercent(latest.metrics.total_return)}</strong>
                      </div>
                      <div>
                        <span>同期买入持有</span>
                        <strong>
                          {formatPercent(latest.benchmark_metrics.total_return)}
                        </strong>
                      </div>
                      <div>
                        <span>最大回撤</span>
                        <strong>{formatPercent(latest.metrics.max_drawdown)}</strong>
                      </div>
                      <div>
                        <span>盈利分段</span>
                        <strong>
                          {formatPercent(
                            latest.diagnostics.profitable_segment_ratio,
                          )}
                        </strong>
                      </div>
                    </div>
                    <p className="tracking-evidence-note">
                      上述仍是“截至本次检查的滚动历史窗口”，不是激活后的纸面收益。
                    </p>

                    <div className="tracking-history">
                      <div className="tracking-history-heading">
                        <h3>前向信号快照</h3>
                        <span>{track.snapshots.length} 次</span>
                      </div>
                      {track.snapshots.slice(0, 6).map((snapshot) => (
                        <div className="tracking-history-row" key={snapshot.id}>
                          <time>{localTime(snapshot.checked_at)}</time>
                          <span className={signalTone(snapshot)}>
                            {SIGNAL_LABELS[snapshot.signal_state.status]}
                          </span>
                          <span>数据 {snapshot.data_as_of.slice(0, 10)}</span>
                          <b>{snapshot.latest_price.toLocaleString("zh-CN")}</b>
                        </div>
                      ))}
                    </div>
                    <div className="source-row tracking-sources">
                      {latest.citations.map((citation, index) => (
                        <SourceBadge
                          citation={citation}
                          key={`${citation.source}-${index}`}
                        />
                      ))}
                    </div>
                  </>
                ) : (
                  <div className="tracking-no-snapshot">
                    <strong>尚未生成信号快照</strong>
                    <span>{track.last_error || "点击刷新以读取最新真实行情。"}</span>
                  </div>
                )}

                <div className="tracking-actions">
                  <button
                    className="primary-button"
                    disabled={busy}
                    onClick={() => void refresh(track)}
                    type="button"
                  >
                    {operation === `refresh:${track.id}` ? "正在刷新…" : "刷新信号"}
                  </button>
                  <button
                    className="secondary-button"
                    disabled={busy}
                    onClick={() => void toggleStatus(track)}
                    type="button"
                  >
                    {track.status === "active" ? "暂停" : "恢复"}
                  </button>
                  <button
                    className="tracking-remove"
                    disabled={busy}
                    onClick={() => void remove(track)}
                    type="button"
                  >
                    移除
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
