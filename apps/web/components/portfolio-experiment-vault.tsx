"use client";

import { useMemo, useState } from "react";

import type {
  PortfolioExperimentRecord,
  PortfolioExperimentSummary,
  PortfolioMethod,
} from "@/lib/types";

const METHOD_LABELS: Record<PortfolioMethod, string> = {
  initial_equal_hold: "起点等权",
  periodic_equal: "定期等权",
  periodic_inverse_volatility: "逆波动风险配置",
};

function percent(value: number): string {
  return `${(value * 100).toFixed(2)}%`;
}

function savedAt(value: string): string {
  return new Date(value).toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function PortfolioExperimentVault({
  id,
  summaries,
  loading,
  operationIds,
  error,
  onView,
  onLoad,
  onDelete,
}: {
  id: string;
  summaries: PortfolioExperimentSummary[];
  loading: boolean;
  operationIds: readonly string[];
  error: string;
  onView: (summary: PortfolioExperimentSummary) => void;
  onLoad: (summary: PortfolioExperimentSummary) => void;
  onDelete: (summary: PortfolioExperimentSummary) => void;
}) {
  const [query, setQuery] = useState("");
  const [deleteConfirmationId, setDeleteConfirmationId] = useState<string | null>(
    null,
  );
  const filtered = useMemo(() => {
    const search = query.trim().toLowerCase();
    if (!search) return summaries;
    return summaries.filter((summary) =>
      [
        summary.name,
        summary.notes ?? "",
        METHOD_LABELS[summary.focus_method],
        ...summary.symbols,
      ].some((value) => value.toLowerCase().includes(search)),
    );
  }, [query, summaries]);

  return (
    <section
      aria-busy={loading}
      className="portfolio-vault"
      id={id}
      onKeyDown={(event) => {
        if (event.key === "Escape" && deleteConfirmationId) {
          event.stopPropagation();
          setDeleteConfirmationId(null);
        }
      }}
    >
      <div className="portfolio-vault-heading">
        <div>
          <span className="eyebrow">PORTFOLIO VAULT · NAS PERSISTED</span>
          <h2>组合实验档案</h2>
          <p>
            列表只加载轻量摘要；查看时才读取完整净值与权重快照。USD、USDT 与
            USDC 当前仅归为同一研究计价组，不代表 1:1 无风险，也尚未建模稳定币脱锚和兑换摩擦。
          </p>
        </div>
        <label>
          搜索组合实验
          <input
            onChange={(event) => setQuery(event.target.value)}
            placeholder="名称 / 资产代码 / 配置方法"
            value={query}
          />
        </label>
      </div>

      {error && (
        <p className="portfolio-vault-error" role="alert">
          {error}
        </p>
      )}

      {loading && summaries.length === 0 ? (
        <div className="portfolio-vault-empty" role="status">
          正在读取 NAS 中的组合档案…
        </div>
      ) : filtered.length === 0 ? (
        <div className="portfolio-vault-empty">
          <strong>{summaries.length === 0 ? "还没有组合档案" : "没有匹配的组合档案"}</strong>
          <span>
            {summaries.length === 0
              ? "运行一次组合实验后，可在证据区保存完整研究快照。"
              : "换一个名称、资产代码或配置方法再试。"}
          </span>
        </div>
      ) : (
        <div className="portfolio-vault-grid">
          {filtered.map((summary) => {
            const viewOperation = `view:${summary.id}`;
            const loadOperation = `load:${summary.id}`;
            const deleteOperation = `delete:${summary.id}`;
            const viewing = operationIds.includes(viewOperation);
            const loadingConfiguration = operationIds.includes(loadOperation);
            const deleting = operationIds.includes(deleteOperation);
            const itemBusy = viewing || loadingConfiguration || deleting;
            const serverVerified = Boolean(summary.source_run_id);
            return (
              <article className="portfolio-vault-card" key={summary.id}>
                <div className="portfolio-vault-card-heading">
                  <span
                    className={
                      !serverVerified
                        ? "portfolio-evidence-badge legacy"
                        : summary.risk_evidence_passed
                        ? "portfolio-evidence-badge passed"
                        : "portfolio-evidence-badge watch"
                    }
                  >
                    {!serverVerified
                      ? "旧版客户端快照 · 未验证回执"
                      : summary.risk_evidence_passed
                        ? "服务端回执 · 风险证据通过"
                        : "服务端回执 · 尚待复核"}
                  </span>
                  <time dateTime={summary.created_at}>
                    {savedAt(summary.created_at)}
                  </time>
                </div>
                <h3>{summary.name}</h3>
                <p>
                  {METHOD_LABELS[summary.focus_method]} · {summary.start} —{" "}
                  {summary.end} · {summary.common_bars} 根共同 K 线
                </p>
                <div className="portfolio-vault-symbols" aria-label="组合资产">
                  {summary.symbols.slice(0, 5).map((symbol) => (
                    <span key={symbol}>{symbol}</span>
                  ))}
                  {summary.symbols.length > 5 && (
                    <span>+{summary.symbols.length - 5}</span>
                  )}
                </div>
                <dl className="portfolio-vault-metrics">
                  <div>
                    <dt>累计收益</dt>
                    <dd className={summary.total_return >= 0 ? "positive" : "negative"}>
                      {percent(summary.total_return)}
                    </dd>
                  </div>
                  <div>
                    <dt>最大回撤</dt>
                    <dd className="negative">{percent(summary.max_drawdown)}</dd>
                  </div>
                  <div>
                    <dt>夏普比率</dt>
                    <dd>{summary.sharpe_ratio.toFixed(2)}</dd>
                  </div>
                </dl>
                {summary.notes && <small>{summary.notes}</small>}

                {deleteConfirmationId === summary.id ? (
                  <div
                    aria-label={`确认删除 ${summary.name}`}
                    className="portfolio-delete-confirmation"
                    role="group"
                  >
                    <p>永久删除这份 NAS 档案？历史快照将无法恢复。</p>
                    <div>
                      <button
                        autoFocus
                        disabled={deleting}
                        onClick={() => setDeleteConfirmationId(null)}
                        type="button"
                      >
                        取消
                      </button>
                      <button
                        className="danger"
                        disabled={deleting}
                        onClick={() => {
                          onDelete(summary);
                          setDeleteConfirmationId(null);
                        }}
                        type="button"
                      >
                        {deleting ? "正在删除…" : "永久删除"}
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="portfolio-vault-card-actions">
                    <button
                      disabled={itemBusy}
                      onClick={() => onView(summary)}
                      type="button"
                    >
                      {viewing ? "正在读取…" : "查看快照"}
                    </button>
                    <button
                      disabled={itemBusy}
                      onClick={() => onLoad(summary)}
                      type="button"
                    >
                      {loadingConfiguration ? "正在载入…" : "载入配置"}
                    </button>
                    <button
                      aria-label={`删除 ${summary.name}`}
                      className="danger"
                      disabled={itemBusy}
                      onClick={() => setDeleteConfirmationId(summary.id)}
                      type="button"
                    >
                      删除
                    </button>
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

export function PortfolioSavePanel({
  name,
  notes,
  focusMethod,
  saving,
  onNameChange,
  onNotesChange,
  onFocusMethodChange,
  onCancel,
  onSave,
}: {
  name: string;
  notes: string;
  focusMethod: PortfolioMethod;
  saving: boolean;
  onNameChange: (value: string) => void;
  onNotesChange: (value: string) => void;
  onFocusMethodChange: (value: PortfolioMethod) => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  return (
    <section className="portfolio-save-panel">
      <div>
        <span className="eyebrow">SERVER-VERIFIED RUN RECEIPT</span>
        <h3>保存本次组合实验</h3>
        <p>
          服务端会用本次运行回执验证并保存冻结请求、完整结果、数据质量、两类权重和分段证据；
          浏览器不能改写收益或风险数字。以后查看快照也不会重新请求行情。
        </p>
      </div>
      <div className="portfolio-save-fields">
        <label>
          实验名称
          <input
            autoFocus
            maxLength={100}
            onChange={(event) => onNameChange(event.target.value)}
            value={name}
          />
        </label>
        <label>
          备注（可选）
          <textarea
            maxLength={1000}
            onChange={(event) => onNotesChange(event.target.value)}
            placeholder="例如：观察稳定币计价风险，等待不同市场环境复核"
            rows={2}
            value={notes}
          />
        </label>
        <label>
          档案关注方法
          <select
            onChange={(event) =>
              onFocusMethodChange(event.target.value as PortfolioMethod)
            }
            value={focusMethod}
          >
            {(Object.keys(METHOD_LABELS) as PortfolioMethod[]).map((method) => (
              <option key={method} value={method}>
                {METHOD_LABELS[method]}
              </option>
            ))}
          </select>
        </label>
        <div>
          <button
            className="secondary-button"
            disabled={saving}
            onClick={onCancel}
            type="button"
          >
            取消
          </button>
          <button
            className="primary-button"
            disabled={saving || !name.trim()}
            onClick={onSave}
            type="button"
          >
            {saving ? "正在保存…" : "确认保存"}
          </button>
        </div>
      </div>
    </section>
  );
}

export function PortfolioSnapshotBanner({
  record,
  hasCurrentResult,
  rerunning,
  onBack,
  onLoadAndRun,
}: {
  record: PortfolioExperimentRecord;
  hasCurrentResult: boolean;
  rerunning: boolean;
  onBack: () => void;
  onLoadAndRun: () => void;
}) {
  return (
    <section
      aria-live="polite"
      className={
        record.source_run_id
          ? "portfolio-snapshot-banner"
          : "portfolio-snapshot-banner legacy"
      }
      role="status"
    >
      <div>
        <span>
          {record.source_run_id
            ? "SERVER-VERIFIED SNAPSHOT · NOT RECALCULATED"
            : "LEGACY CLIENT SNAPSHOT · RECEIPT NOT VERIFIED"}
        </span>
        <strong>历史快照 · 未重新计算</strong>
        <p>
          「{record.name}」保存于 {savedAt(record.created_at)}。当前净值、权重和证据均来自保存时结果，
          不代表现在重新运行会得到相同数字。
          {record.source_run_id
            ? " 该档案由服务端运行回执生成。"
            : " 这是旧版客户端快照，未经过服务端运行回执验证。"}
          {record.calculation_version
            ? ` 计算版本 ${record.calculation_version}。`
            : ""}
        </p>
      </div>
      <div>
        <button
          className="primary-button"
          disabled={rerunning}
          onClick={onLoadAndRun}
          type="button"
        >
          {rerunning ? "正在重新计算…" : "载入并重新计算"}
        </button>
        <button className="secondary-button" onClick={onBack} type="button">
          {hasCurrentResult ? "回到当前结果" : "回到工作台"}
        </button>
      </div>
    </section>
  );
}
