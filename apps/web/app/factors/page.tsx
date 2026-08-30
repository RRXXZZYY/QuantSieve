"use client";

import { FormEvent, useMemo, useState } from "react";

import { FactorResearchChart } from "@/components/factor-research-chart";
import { SymbolPicker } from "@/components/symbol-picker";
import { apiFetch } from "@/lib/api";
import { parseFactorResearchPayload } from "@/lib/factor-research-contract";
import {
  FACTOR_OPTIONS,
  buildFactorResearchRequest,
  factorAvailabilitySourceLabel,
  factorCapabilityEntries,
  factorDecimal,
  factorDroppedReasonEntries,
  factorNumericInputBasisLabel,
  factorRunReceiptHasExpired,
  findFactorHorizon,
} from "@/lib/factor-research";
import { instrumentLabel } from "@/lib/instruments";
import type {
  FactorHorizonDiagnostics,
  FactorId,
  FactorResearchPayload,
  FactorResearchRequest,
  Instrument,
} from "@/lib/types";

const US_ETF_PRESET: Instrument[] = [
  {
    symbol: "SPY",
    name: "SPDR S&P 500 ETF Trust",
    market: "ETF",
    exchange: "NYSE Arca",
    currency: "USD",
    provider: "yfinance",
    asset_type: "etf",
  },
  {
    symbol: "QQQ",
    name: "Invesco QQQ Trust",
    market: "ETF",
    exchange: "Nasdaq",
    currency: "USD",
    provider: "yfinance",
    asset_type: "etf",
  },
  {
    symbol: "IWM",
    name: "iShares Russell 2000 ETF",
    market: "ETF",
    exchange: "NYSE Arca",
    currency: "USD",
    provider: "yfinance",
    asset_type: "etf",
  },
  {
    symbol: "EFA",
    name: "iShares MSCI EAFE ETF",
    market: "ETF",
    exchange: "NYSE Arca",
    currency: "USD",
    provider: "yfinance",
    asset_type: "etf",
  },
  {
    symbol: "EEM",
    name: "iShares MSCI Emerging Markets ETF",
    market: "ETF",
    exchange: "NYSE Arca",
    currency: "USD",
    provider: "yfinance",
    asset_type: "etf",
  },
  {
    symbol: "GLD",
    name: "SPDR Gold Shares",
    market: "ETF",
    exchange: "NYSE Arca",
    currency: "USD",
    provider: "yfinance",
    asset_type: "etf",
  },
];

const BINANCE_PRESET: Instrument[] = [
  ["BTCUSDT", "Bitcoin / USDT"],
  ["ETHUSDT", "Ethereum / USDT"],
  ["BNBUSDT", "BNB / USDT"],
  ["SOLUSDT", "Solana / USDT"],
  ["XRPUSDT", "XRP / USDT"],
  ["ADAUSDT", "Cardano / USDT"],
].map(([symbol, name]) => ({
  symbol,
  name,
  market: "CRYPTO",
  exchange: "Binance Spot",
  currency: "USDT",
  provider: "binance",
  asset_type: "crypto",
}));

const CROSS_MARKET_PRESET: Instrument[] = [
  {
    symbol: "BTCUSDT",
    name: "Bitcoin / USDT",
    market: "CRYPTO",
    exchange: "Binance Spot",
    currency: "USDT",
    provider: "binance",
    asset_type: "spot",
  },
  {
    symbol: "NDX",
    name: "NASDAQ 100 Index",
    market: "INDEX",
    exchange: "Nasdaq",
    currency: "USD",
    provider: "macro",
    asset_type: "index_reference",
  },
  {
    symbol: "CL",
    name: "NYMEX 原油",
    market: "FUTURES",
    exchange: "NYMEX",
    currency: "USD",
    provider: "futures",
    asset_type: "commodity_future",
  },
  {
    symbol: "GC",
    name: "COMEX 黄金",
    market: "FUTURES",
    exchange: "COMEX",
    currency: "USD",
    provider: "futures",
    asset_type: "commodity_future",
  },
  {
    symbol: "SPY",
    name: "SPDR S&P 500 ETF Trust",
    market: "ETF",
    exchange: "NYSE Arca",
    currency: "USD",
    provider: "yfinance",
    asset_type: "etf",
  },
];

function universeMatches(left: Instrument[], right: Instrument[]): boolean {
  if (left.length !== right.length) return false;
  const identities = new Set(
    left.map(
      (instrument) =>
        `${instrument.provider}:${instrument.symbol.toLocaleUpperCase()}`,
    ),
  );
  return right.every((instrument) =>
    identities.has(
      `${instrument.provider}:${instrument.symbol.toLocaleUpperCase()}`,
    ),
  );
}

function isoDate(value: Date): string {
  return value.toISOString().slice(0, 10);
}

function yearsAgo(years: number): string {
  const value = new Date();
  value.setUTCFullYear(value.getUTCFullYear() - years);
  return isoDate(value);
}

function percent(value: string | null | undefined, digits = 2): string {
  const numeric = factorDecimal(value);
  return numeric === null ? "—" : `${(numeric * 100).toFixed(digits)}%`;
}

function decimal(value: string | null | undefined, digits = 3): string {
  const numeric = factorDecimal(value);
  return numeric === null ? "—" : numeric.toFixed(digits);
}

function compactId(value: string | undefined): string {
  if (!value) return "—";
  return value.length > 18 ? `${value.slice(0, 9)}…${value.slice(-7)}` : value;
}

function localDateTime(value: string | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function requestFingerprint(request: FactorResearchRequest | null): string | null {
  return request ? JSON.stringify(request) : null;
}

function latestScoreValue(value: string): string {
  const numeric = factorDecimal(value);
  if (numeric === null) return "—";
  if (Math.abs(numeric) >= 1000 || (Math.abs(numeric) > 0 && Math.abs(numeric) < 0.001)) {
    return numeric.toExponential(3);
  }
  return numeric.toFixed(4);
}

function metricExplanation(horizon: FactorHorizonDiagnostics): string {
  const rankIc = factorDecimal(horizon.diagnostics.rank_ic_mean);
  if (rankIc === null) return "当前样本无法形成稳定的 Rank IC 判断。";
  const direction =
    rankIc > 0.05
      ? "样本内呈正向排序关系"
      : rankIc < -0.05
        ? "样本内呈反向排序关系"
        : "样本内排序关系较弱";
  return `${direction}；这只是固定事后标的池中的历史诊断，不能据此称为最佳因子或未来收益承诺。`;
}

export default function FactorsPage() {
  const [instruments, setInstruments] = useState<Instrument[]>(US_ETF_PRESET);
  const [pickerQuery, setPickerQuery] = useState("");
  const [pickerSelection, setPickerSelection] = useState<Instrument | null>(null);
  const [factorId, setFactorId] = useState<FactorId>("momentum");
  const [lookbackBars, setLookbackBars] = useState(20);
  const [forwardHorizons, setForwardHorizons] = useState("1, 5, 20");
  const [quantileCount, setQuantileCount] = useState(3);
  const [start, setStart] = useState(() => yearsAgo(3));
  const [end, setEnd] = useState(() => isoDate(new Date()));
  const [result, setResult] = useState<FactorResearchPayload | null>(null);
  const [resultRequest, setResultRequest] = useState<FactorResearchRequest | null>(
    null,
  );
  const [selectedHorizon, setSelectedHorizon] = useState(5);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");

  const firstInstrument = instruments[0] ?? null;
  const interval = "1d" as const;

  const currentRequest = useMemo(() => {
    try {
      return buildFactorResearchRequest({
        instruments,
        factorId,
        lookbackBars,
        start,
        end,
        interval,
        forwardHorizons,
        quantileCount,
      });
    } catch {
      return null;
    }
  }, [
    end,
    factorId,
    forwardHorizons,
    instruments,
    interval,
    lookbackBars,
    quantileCount,
    start,
  ]);

  const resultIsStale =
    result !== null &&
    requestFingerprint(currentRequest) !== requestFingerprint(resultRequest);
  const resultHorizons = result
    ? Object.keys(result.diagnostics_by_horizon)
        .map(Number)
        .filter(Number.isFinite)
        .sort((left, right) => left - right)
    : [];
  const activeHorizon =
    result === null ? null : findFactorHorizon(result, selectedHorizon);
  const activeLatestScores =
    result?.latest_scores[String(selectedHorizon)] ?? null;
  const providerCount = new Set(
    instruments.map((instrument) => instrument.provider),
  ).size;
  const mixedProviders = providerCount > 1;
  const receiptExpired =
    result !== null &&
    factorRunReceiptHasExpired(result.run_expires_at);
  const activePeriods = activeHorizon?.diagnostics.periods ?? [];
  const activeStart = activePeriods[0]?.period_at;
  const activeEnd = activePeriods.at(-1)?.period_at;
  const activeEligiblePairs = activePeriods.reduce(
    (total, period) => total + period.eligible_count,
    0,
  );
  const activeEvaluatedPeriods =
    result?.coverage.evaluated_periods_by_horizon[String(selectedHorizon)] ?? 0;
  const activeDecisionCoverage =
    result?.coverage.decision_date_coverage_by_horizon[
      String(selectedHorizon)
    ];
  const activeDroppedReasons = factorDroppedReasonEntries(
    result?.coverage.dropped_reason_counts_by_horizon[
      String(selectedHorizon)
    ],
  );
  const activeTimeSeries =
    result?.time_series[String(selectedHorizon)] ?? [];
  const activeLatestSeriesRow = activeTimeSeries.at(-1) ?? null;
  const resultProviderCount = new Set(
    result?.dataset_snapshots.map((item) => item.provider) ?? [],
  ).size;
  const datasetEvidenceByAsset = new Map(
    result?.dataset_snapshots.map((item) => [
      `${item.provider}:${item.symbol}`,
      item,
    ]) ?? [],
  );
  const datasetEvidenceCount = new Set(
    result?.dataset_snapshots.map((item) => item.evidence_id) ?? [],
  ).size;

  function applyPreset(next: Instrument[]) {
    setInstruments(next.map((instrument) => ({ ...instrument })));
    setQuantileCount((current) => Math.min(current, next.length));
    setPickerQuery("");
    setPickerSelection(null);
    setError("");
  }

  function addInstrument() {
    if (!pickerSelection) return;
    if (instruments.length >= 20) {
      setError("首版单次研究最多支持 20 个标的。");
      return;
    }
    if (
      instruments.some(
        (instrument) =>
          instrument.provider === pickerSelection.provider &&
          instrument.symbol.toLocaleUpperCase() ===
            pickerSelection.symbol.toLocaleUpperCase(),
      )
    ) {
      setError("这个标的已经在研究池中。");
      return;
    }
    setInstruments((items) => [...items, pickerSelection]);
    setPickerSelection(null);
    setPickerQuery("");
    setError("");
  }

  async function runResearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    let request: FactorResearchRequest;
    try {
      request = buildFactorResearchRequest({
        instruments,
        factorId,
        lookbackBars,
        start,
        end,
        interval,
        forwardHorizons,
        quantileCount,
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "研究配置无效");
      return;
    }
    setRunning(true);
    try {
      const responseBody = await apiFetch<unknown>(
        "/api/v1/factors/research",
        {
          method: "POST",
          body: JSON.stringify(request),
        },
      );
      const payload = parseFactorResearchPayload(responseBody);
      if (
        payload.recipe?.factor_id !== request.factor_id ||
        !payload.diagnostics_by_horizon ||
        Object.keys(payload.diagnostics_by_horizon).length === 0
      ) {
        throw new Error("因子研究响应缺少可核验诊断。");
      }
      setResult(payload);
      setResultRequest(request);
      setSelectedHorizon(
        payload.diagnostics_by_horizon["5"]
          ? 5
          : Number(Object.keys(payload.diagnostics_by_horizon)[0]),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "因子研究运行失败");
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="factor-page">
      <header className="factor-hero">
        <div>
          <span className="eyebrow">FACTOR LAB · DIAGNOSTIC ONLY</span>
          <h1>因子研究</h1>
          <p>
            在固定标的池里检查价量特征与未来收益的横截面关系。可组合多个数据源，但结果只取精确共有 UTC
            日标签；用于筛除脆弱想法，不用于寻找“保证盈利”的最佳因子。
          </p>
        </div>
        <div className="factor-hero-status">
          <span className="status-dot" />
          <div>
            <strong>时间边界优先</strong>
            <small>特征截止 d-1；建模可用后再等一根共有日 K 线</small>
          </div>
        </div>
      </header>

      <section className="factor-boundary-banner" aria-label="因子研究边界">
        <div>
          <strong>固定事后样本</strong>
          <span>当前用户选择不是历史指数成分，PIT universe 未验证，存在幸存者偏差。</span>
        </div>
        <div>
          <strong>建模可用时间 + 一根延迟</strong>
          <span>按逐行时间或来源规则估算可用时刻，随后统一等待一根精确共有日 bar。</span>
        </div>
        <div>
          <strong>仅作诊断</strong>
          <span>共有日交集与分位毛收益都不是可交易组合，不含费用、冲击和真实成交约束。</span>
        </div>
      </section>

      <div className="factor-workspace">
        <form className="factor-controls" onSubmit={runResearch}>
          <div className="factor-control-heading">
            <div>
              <span className="eyebrow">RESEARCH CONTRACT</span>
              <h2>研究配置</h2>
            </div>
            <span className="factor-asset-count">{instruments.length}/20</span>
          </div>

          <fieldset className="factor-fieldset">
            <legend>推荐同源预设</legend>
            <div className="factor-preset-grid">
              <button
                className={universeMatches(instruments, US_ETF_PRESET) ? "active" : ""}
                onClick={() => applyPreset(US_ETF_PRESET)}
                type="button"
              >
                <strong>US ETF 横截面</strong>
                <small>SPY / QQQ / IWM / EFA / EEM / GLD</small>
              </button>
              <button
                className={
                  universeMatches(instruments, BINANCE_PRESET) ? "active" : ""
                }
                onClick={() => applyPreset(BINANCE_PRESET)}
                type="button"
              >
                <strong>Binance 主流现货</strong>
                <small>BTC / ETH / BNB / SOL / XRP / ADA</small>
              </button>
              <button
                className={
                  universeMatches(instruments, CROSS_MARKET_PRESET) ? "active" : ""
                }
                onClick={() => applyPreset(CROSS_MARKET_PRESET)}
                type="button"
              >
                <strong>跨市场共同日诊断</strong>
                <small>BTCUSDT / NDX / CL / GC / SPY · 参考序列</small>
              </button>
            </div>
          </fieldset>

          <fieldset className="factor-fieldset">
            <legend>研究标的池</legend>
            <SymbolPicker
              label="搜索并添加标的"
              onQueryChange={setPickerQuery}
              onSelect={setPickerSelection}
              placeholder="名称、代码或交易对"
              query={pickerQuery}
              selected={pickerSelection}
            />
            <button
              className="secondary-button full"
              disabled={!pickerSelection || instruments.length >= 20}
              onClick={addInstrument}
              type="button"
            >
              添加到固定标的池
            </button>
            <div className="factor-universe-chips">
              {instruments.map((instrument) => (
                <span
                  className="factor-universe-chip"
                  key={`${instrument.provider}:${instrument.symbol}`}
                  title={instrumentLabel(instrument)}
                >
                  <span>
                    <strong>{instrument.symbol}</strong>
                    <small>{instrument.name}</small>
                  </span>
                  <button
                    aria-label={`移除 ${instrument.name}`}
                    onClick={() => {
                      const next = instruments.filter(
                        (item) =>
                          !(
                            item.provider === instrument.provider &&
                            item.symbol === instrument.symbol
                          ),
                      );
                      setInstruments(next);
                      setQuantileCount((current) =>
                        Math.min(current, Math.max(2, next.length)),
                      );
                    }}
                    type="button"
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
            <p className="factor-field-hint">
              {firstInstrument
                ? `${providerCount} 个数据源；至少保留 3 个标的，且数量不能少于分位数组数。`
                : "请先添加 3–20 个标的。"}
            </p>
            {mixedProviders && (
              <p className="factor-field-hint error" role="status">
                混合数据源将只保留精确共有 UTC 日标签；周末与各市场休市日会被排除，结论不能解释为可同步交易的组合。
              </p>
            )}
          </fieldset>

          <fieldset className="factor-fieldset">
            <legend>因子定义</legend>
            <div className="factor-option-grid">
              {FACTOR_OPTIONS.map((option) => (
                <button
                  aria-pressed={factorId === option.id}
                  className={factorId === option.id ? "active" : ""}
                  key={option.id}
                  onClick={() => setFactorId(option.id)}
                  type="button"
                >
                  <strong>{option.name}</strong>
                  <small>{option.description}</small>
                </button>
              ))}
            </div>
            <label className="factor-input-label">
              <span>回看窗口</span>
              <input
                max={252}
                min={5}
                onChange={(event) => setLookbackBars(Number(event.target.value))}
                step={1}
                type="number"
                value={lookbackBars}
              />
              <small>根 K 线；不是自动寻优参数</small>
            </label>
          </fieldset>

          <fieldset className="factor-fieldset">
            <legend>周期与前瞻标签</legend>
            <div className="factor-interval-grid">
              <button
                aria-pressed="true"
                className="active"
                type="button"
              >
                日线
              </button>
            </div>
            <p className="factor-field-hint">
              当前服务端只接受提供方明确标记为 finalized_bars_only=true
              的日线；任何未认证来源都会返回 422 原因，不会降级生成结果。
            </p>
            <div className="factor-date-grid">
              <label className="factor-input-label">
                <span>开始</span>
                <input
                  max={end}
                  onChange={(event) => setStart(event.target.value)}
                  type="date"
                  value={start}
                />
              </label>
              <label className="factor-input-label">
                <span>结束</span>
                <input
                  max={isoDate(new Date())}
                  min={start}
                  onChange={(event) => setEnd(event.target.value)}
                  type="date"
                  value={end}
                />
              </label>
            </div>
            <label className="factor-input-label">
              <span>前瞻周期</span>
              <input
                onChange={(event) => setForwardHorizons(event.target.value)}
                placeholder="1, 5, 20"
                type="text"
                value={forwardHorizons}
              />
              <small>最多 4 个，用逗号分隔；单位为 K 线根数</small>
            </label>
            <label className="factor-input-label">
              <span>分位数组数</span>
              <select
                onChange={(event) => setQuantileCount(Number(event.target.value))}
                value={quantileCount}
              >
                {Array.from(
                  { length: Math.max(1, Math.min(9, instruments.length - 1)) },
                  (_, index) => index + 2,
                ).map((count) => (
                  <option key={count} value={count}>
                    {count} 组
                  </option>
                ))}
              </select>
            </label>
          </fieldset>

          {error && (
            <div className="error-banner" role="alert">
              {error}
            </div>
          )}
          <button
            className="primary-button factor-run-button"
            disabled={running}
            type="submit"
          >
            {running ? "正在构建点时边界与诊断…" : "运行因子诊断 ↗"}
          </button>
          <p className="factor-submit-note">
            运行会生成服务端回执和内容哈希；不会连接账户、下单或自动选择“冠军因子”。
          </p>
        </form>

        <main className="factor-results">
          {!result ? (
            <section className="factor-empty-state">
              <span className="eyebrow">WAITING FOR EVIDENCE</span>
              <h2>先定义一个可证伪的因子问题</h2>
              <p>
                选择一个固定标的池、价量因子和前瞻周期。平台会展示决策日覆盖、逐期
                IC、分位收益和时间边界；也可混合来源并按共有日标签严格对齐，弱证据会原样保留。
              </p>
              <div className="factor-empty-flow" aria-label="因子研究流程">
                <span>固定 universe</span>
                <i>→</i>
                <span>历史特征</span>
                <i>→</i>
                <span>可用后等待一根共有 bar</span>
                <i>→</i>
                <span>横截面诊断</span>
              </div>
            </section>
          ) : (
            <>
              {resultIsStale && (
                <div className="factor-stale-banner" role="status">
                  当前配置已改变；下方仍是上一次服务端运行的冻结结果。重新运行前不会把它解释为当前配置。
                </div>
              )}

              <section className="factor-result-heading">
                <div>
                  <span className="eyebrow">SERVER-EVIDENCED RESULT</span>
                  <h2>
                    {FACTOR_OPTIONS.find(
                      (item) => item.id === result.recipe.factor_id,
                    )?.name ?? result.recipe.factor_id}
                  </h2>
                  <p>
                    {result.recipe.interval} · 回看 {result.recipe.lookback} 根 ·{" "}
                    {result.coverage.per_asset.length}/
                    {result.coverage.requested_assets} 个完成证据快照的标的
                  </p>
                </div>
                <span
                  className={`factor-receipt-pill ${
                    result.run_id && !receiptExpired ? "verified" : "incomplete"
                  }`}
                >
                  {result.run_id
                    ? receiptExpired
                      ? "回执已过期"
                      : "服务端回执"
                    : "无运行回执"}
                </span>
              </section>

              <section className="factor-validity-grid">
                <div className="warning">
                  <small>UNIVERSE</small>
                  <strong>固定事后标的池</strong>
                  <span>
                    {result.limitations.point_in_time_universe
                      ? "服务端声明具备点时成分证据"
                      : "没有历史成分股证据，不排除幸存者偏差"}
                  </span>
                </div>
                <div className="estimated">
                  <small>AVAILABLE AT / PIT</small>
                  <strong>
                    {result.limitations.point_in_time_validation_passed
                      ? "PIT 验证已通过"
                      : "PIT 未验证"}
                  </strong>
                  <span>
                    数据可用性口径：{result.limitations.source_availability}
                  </span>
                </div>
                <div className="diagnostic">
                  <small>USE</small>
                  <strong>非交易诊断</strong>
                  <span>
                    {result.limitations.tradable_conclusion
                      ? "服务端标记为可交易结论"
                      : "服务端明确标记 tradable_conclusion=false"}
                  </span>
                </div>
              </section>

              <section className="factor-provenance">
                <div className="factor-section-heading">
                  <div>
                    <span className="eyebrow">ANTI-LOOKAHEAD TIMELINE</span>
                    <h3>防前视时间链</h3>
                  </div>
                  <small>
                    建模时刻来自来源证据或保守估算，不代表已通过完整 PIT 验证
                  </small>
                </div>
                <dl className="factor-provenance-grid">
                  <div>
                    <dt>特征信息截止</dt>
                    <dd>
                      <strong>{result.recipe.feature_information_cutoff}</strong>
                      <small>{result.recipe.formula}</small>
                    </dd>
                  </div>
                  <div>
                    <dt>建模可用时刻</dt>
                    <dd>
                      <strong>
                        {localDateTime(
                          activeLatestScores?.period_at ??
                            activeLatestSeriesRow?.period_at,
                        )}
                      </strong>
                      <small>{result.recipe.decision_time}</small>
                    </dd>
                  </div>
                  <div>
                    <dt>延迟入场</dt>
                    <dd>
                      <strong>
                        {localDateTime(
                          activeLatestScores?.entry_at ??
                            activeLatestSeriesRow?.entry_at,
                        )}
                      </strong>
                      <small>{result.recipe.execution_delay}</small>
                    </dd>
                  </div>
                  <div>
                    <dt>前瞻标签可用</dt>
                    <dd>
                      <strong>
                        {localDateTime(activeLatestSeriesRow?.label_available_at)}
                      </strong>
                      <small>
                        {result.recipe.label_formula} ·{" "}
                        {result.recipe.label_available_at}
                      </small>
                    </dd>
                  </div>
                </dl>
              </section>

              <section className="factor-coverage-grid">
                <article>
                  <span>标的覆盖</span>
                  <strong>
                    {result.coverage.per_asset.length}/
                    {result.coverage.requested_assets}
                  </strong>
                  <small>{resultProviderCount} 个数据源，失败会使整次运行中止</small>
                </article>
                <article>
                  <span>{selectedHorizon} 根前瞻有效时期</span>
                  <strong>{activeEvaluatedPeriods}</strong>
                  <small>
                    决策日覆盖 {percent(activeDecisionCoverage)} ·{" "}
                    {activeDroppedReasons.length > 0
                      ? activeDroppedReasons
                          .map(([reason, count]) => `${reason} × ${count}`)
                          .join(" · ")
                      : "无剔除原因"}
                  </small>
                </article>
                <article>
                  <span>精确共有日 / 来源日期并集</span>
                  <strong>
                    {result.coverage.common_days}/
                    {result.coverage.source_distinct_days}
                  </strong>
                  <small>
                    共有日保留率{" "}
                    {percent(result.coverage.alignment_day_retention_rate)} ·
                    不前向填充
                  </small>
                </article>
                <article>
                  <span>当前诊断区间</span>
                  <strong>
                    {activeStart?.slice(0, 10) ?? "—"} →{" "}
                    {activeEnd?.slice(0, 10) ?? "—"}
                  </strong>
                  <small>
                    {result.coverage.requested_decision_days} 个候选决策日 ·{" "}
                    {result.coverage.candidate_observation_pairs.toLocaleString(
                      "zh-CN",
                    )}{" "}
                    个候选资产配对
                  </small>
                </article>
              </section>

              <section className="factor-horizon-section">
                <div className="factor-section-heading">
                  <div>
                    <span className="eyebrow">FORWARD HORIZON</span>
                    <h3>选择前瞻周期</h3>
                  </div>
                  <div className="factor-horizon-tabs" role="tablist">
                    {resultHorizons.map((horizon) => (
                      <button
                        aria-selected={selectedHorizon === horizon}
                        className={
                          selectedHorizon === horizon ? "active" : ""
                        }
                        key={horizon}
                        onClick={() => setSelectedHorizon(horizon)}
                        role="tab"
                        type="button"
                      >
                        {horizon} 根
                      </button>
                    ))}
                  </div>
                </div>
              </section>

              {activeHorizon && (
                <>
                  <section className="factor-metric-grid">
                    <article>
                      <span>Rank IC 均值</span>
                      <strong>
                        {decimal(activeHorizon.diagnostics.rank_ic_mean)}
                      </strong>
                      <small>只描述样本内排序相关性</small>
                    </article>
                    <article>
                      <span>Rank IC IR</span>
                      <strong>
                        {decimal(
                          activeHorizon.diagnostics
                            .rank_ic_information_ratio,
                        )}
                      </strong>
                      <small>均值 ÷ 逐期样本波动</small>
                    </article>
                    <article>
                      <span>Top − Bottom 均值</span>
                      <strong>
                        {percent(
                          activeHorizon.diagnostics.long_short_mean_return,
                          3,
                        )}
                      </strong>
                      <small>等权毛收益，不含成本</small>
                    </article>
                    <article>
                      <span>平均换手</span>
                      <strong>
                        {percent(activeHorizon.diagnostics.average_turnover)}
                      </strong>
                      <small>双边权重变化的一半</small>
                    </article>
                    <article>
                      <span>分位单调性</span>
                      <strong>
                        {decimal(
                          activeHorizon.diagnostics.quantile_monotonicity,
                        )}
                      </strong>
                      <small>接近 1 仅表示样本内梯度更顺</small>
                    </article>
                    <article>
                      <span>截面资产配对率</span>
                      <strong>
                        {percent(activeHorizon.diagnostics.coverage_rate)}
                      </strong>
                      <small>
                        {activeHorizon.diagnostics.observation_count}/
                        {activeEligiblePairs} 个有效/候选资产配对；不是决策日覆盖率
                      </small>
                    </article>
                  </section>
                  <p className="factor-interpretation">
                    {metricExplanation(activeHorizon)}
                  </p>
                  <FactorResearchChart horizon={activeHorizon} />
                </>
              )}

              <section className="factor-ranking-section">
                <div className="factor-section-heading">
                  <div>
                    <span className="eyebrow">LATEST EVALUABLE SNAPSHOT</span>
                    <h3>最新可评估截面排名</h3>
                  </div>
                  <small>
                    {activeLatestScores
                      ? `已取得完整前瞻标签；建模可用 ${localDateTime(activeLatestScores.period_at)}`
                      : "当前周期没有分数快照"}
                  </small>
                </div>
                {activeLatestScores && activeLatestScores.scores.length > 0 ? (
                  <div className="factor-ranking-table-wrap">
                    <table className="factor-ranking-table">
                      <thead>
                        <tr>
                           <th>排名</th>
                           <th>标的</th>
                           <th>来源</th>
                           <th>原始值</th>
                           <th>建模可用时刻</th>
                           <th>等待一根 bar 后入场</th>
                        </tr>
                      </thead>
                      <tbody>
                        {activeLatestScores.scores.map((score, index) => (
                            <tr key={`${score.provider}:${score.symbol}`}>
                              <td>
                                <strong>#{index + 1}</strong>
                              </td>
                              <td>{score.symbol}</td>
                              <td>{score.provider}</td>
                              <td>{latestScoreValue(score.factor_value)}</td>
                              <td>{localDateTime(activeLatestScores.period_at)}</td>
                              <td>{localDateTime(activeLatestScores.entry_at)}</td>
                            </tr>
                          ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="factor-field-hint">
                    本次响应没有独立的最新排名快照。
                  </p>
                )}
              </section>

              <section className="factor-ranking-section">
                <div className="factor-section-heading">
                  <div>
                    <span className="eyebrow">DATASET EVIDENCE</span>
                    <h3>逐资产数据能力与证据绑定</h3>
                  </div>
                  <small>
                    {datasetEvidenceCount}/{result.dataset_snapshots.length} 个唯一
                    evidence_id；它绑定快照、请求边界、元数据与引用
                  </small>
                </div>
                <div className="factor-ranking-table-wrap">
                  <table className="factor-ranking-table factor-evidence-table">
                    <thead>
                      <tr>
                        <th>标的 / 来源</th>
                        <th>来源行 → 共有日</th>
                        <th>数值口径</th>
                        <th>可用时刻证据</th>
                        <th>数据源能力限制</th>
                        <th>snapshot_id</th>
                        <th>evidence_id</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.coverage.per_asset.map((asset) => {
                        const datasetEvidence = datasetEvidenceByAsset.get(
                          `${asset.provider}:${asset.symbol}`,
                        );
                        const capabilityEntries = factorCapabilityEntries(
                          asset.capabilities,
                        );
                        return (
                          <tr key={`${asset.provider}:${asset.symbol}`}>
                            <td>
                              <strong>{asset.symbol}</strong>
                              <div>{asset.provider}</div>
                            </td>
                            <td>
                              {asset.source_rows} → {asset.aligned_rows}
                              <div>
                                对齐保留 {percent(asset.aligned_row_ratio)}
                              </div>
                            </td>
                            <td>
                              {factorNumericInputBasisLabel(
                                asset.numeric_input_basis,
                              )}
                              <div>
                                <code>{asset.numeric_input_basis}</code>
                              </div>
                            </td>
                            <td>
                              {asset.availability_sources.map((source) => (
                                <div key={source} title={source}>
                                  {factorAvailabilitySourceLabel(source)}
                                </div>
                              ))}
                            </td>
                            <td>
                              {capabilityEntries.length > 0
                                ? capabilityEntries.map((capability) => (
                                    <div
                                      key={capability.key}
                                      title={`${capability.key}=${capability.rawValue}`}
                                    >
                                      {capability.label}：{capability.value}
                                    </div>
                                  ))
                                : "数据源未声明能力字段"}
                            </td>
                            <td>
                              <code title={asset.snapshot_id}>
                                {compactId(asset.snapshot_id)}
                              </code>
                            </td>
                            <td>
                              <code
                                title={
                                  datasetEvidence?.evidence_id ??
                                  asset.evidence_id
                                }
                              >
                                {compactId(
                                  datasetEvidence?.evidence_id ??
                                    asset.evidence_id,
                                )}
                              </code>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="factor-provenance">
                <div className="factor-section-heading">
                  <div>
                    <span className="eyebrow">RUN PROVENANCE</span>
                    <h3>服务端运行证据</h3>
                  </div>
                  <span
                    className={`factor-manifest-state ${
                      result.run_manifest?.reproducibility_status ?? "incomplete"
                    }`}
                  >
                    {result.run_manifest?.reproducibility_status === "complete"
                      ? "REPRO COMPLETE"
                      : "REPRO INCOMPLETE"}
                  </span>
                </div>
                <dl className="factor-provenance-grid">
                  <div>
                    <dt>研究截点</dt>
                    <dd>
                      <strong>{localDateTime(result.run_as_of_at)}</strong>
                      <small>特征依赖与标签证据不得晚于此 UTC 截点</small>
                    </dd>
                  </div>
                  <div>
                    <dt>运行回执</dt>
                    <dd>
                      <code title={result.run_id}>
                        {compactId(result.run_id)}
                      </code>
                      <small>
                        {result.run_id
                          ? receiptExpired
                            ? "归档窗口已过期；证据仍显示，但不能据此执行保存"
                            : `有效至 ${localDateTime(result.run_expires_at)}`
                          : "响应未附带服务端回执"}
                      </small>
                    </dd>
                  </div>
                  <div>
                    <dt>运行清单</dt>
                    <dd>
                      <code title={result.run_manifest?.manifest_id}>
                        {compactId(result.run_manifest?.manifest_id)}
                      </code>
                      <small>{result.run_manifest?.engine_version ?? "未提供"}</small>
                    </dd>
                  </div>
                  <div>
                    <dt>数据快照</dt>
                    <dd>
                      <strong>
                        {result.run_manifest?.datasets.length ??
                          result.dataset_snapshots.length}
                      </strong>
                      <small>
                        {result.run_manifest
                          ? `${result.run_manifest.datasets.filter((item) => item.quality_status === "passed").length} 个通过快照质量检查`
                          : "未提供快照清单"}
                      </small>
                    </dd>
                  </div>
                  <div>
                    <dt>复现边界</dt>
                    <dd>
                      <strong>
                        {result.run_manifest?.missing_requirements.length ?? 0}
                      </strong>
                      <small>
                        {result.run_manifest?.missing_requirements.join(" · ") ||
                          "清单未报告复现缺失项；不代表 PIT 已验证"}
                      </small>
                    </dd>
                  </div>
                </dl>
              </section>

              <section className="factor-evidence-footer">
                <div>
                  <span className="eyebrow">SOURCES</span>
                  <h3>来源与限制</h3>
                </div>
                <div className="factor-source-grid">
                  <div>
                    <strong>数据来源</strong>
                    {result.citations.length > 0 ? (
                      <ul>
                        {result.citations.map((citation, index) => (
                          <li key={`${citation.source}:${citation.url}:${index}`}>
                            {citation.url ? (
                              <a
                                href={citation.url}
                                rel="noreferrer"
                                target="_blank"
                              >
                                {citation.source} ↗
                              </a>
                            ) : (
                              citation.source
                            )}
                            {citation.note && <small>{citation.note}</small>}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p>本次响应未附带引用。</p>
                    )}
                  </div>
                  <div>
                    <strong>研究限制</strong>
                    <ul>
                      {[
                        ...new Set([
                          "固定事后标的池不等于点时成分股 universe。",
                          `point_in_time_validation_passed=${String(result.limitations.point_in_time_validation_passed)}；来源完结规则与逐行时间本身不等于完整 PIT 数据认证。`,
                          `特征建模可用后统一等待一根共有日 bar；诊断标签为 ${result.recipe.label_formula}。`,
                          "分位收益未扣除费用、滑点、冲击与融资成本。",
                          "多日标签可能重叠；当前 IC/IR 没有 HAC、序列相关或多重检验校正，只能视为描述性诊断。",
                          "若完全相同的因子值跨越分位边界，服务端会失败关闭，不会按股票代码任意拆组。",
                          result.limitations.note,
                          ...(resultProviderCount > 1
                            ? [
                                "混合市场仅按精确共有 UTC 日标签取交集，周末与休市日会排除；这不是可同步成交的组合。",
                                "CL、GC 等 futures 数据是公开参考序列，不代表某个可连续交易的具体合约。",
                              ]
                            : []),
                        ]),
                      ].map((limitation) => (
                        <li key={limitation}>{limitation}</li>
                      ))}
                    </ul>
                  </div>
                </div>
              </section>
            </>
          )}
        </main>
      </div>
    </div>
  );
}
