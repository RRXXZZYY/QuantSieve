"use client";

import { useEffect, useMemo, useState } from "react";

import { LineChart } from "@/components/chart";
import {
  PortfolioExperimentVault,
  PortfolioSavePanel,
  PortfolioSnapshotBanner,
} from "@/components/portfolio-experiment-vault";
import { PortfolioForwardObservation } from "@/components/portfolio-forward-observation";
import { SymbolPicker } from "@/components/symbol-picker";
import { apiFetch } from "@/lib/api";
import {
  buildPortfolioExperimentFromRunPayload,
  buildPortfolioRequest,
  defaultPortfolioExperimentName,
  hydratePortfolioExperiment,
  portfolioDefaultDates,
  portfolioExperimentToPayload,
  portfolioMethodCopy,
  portfolioRequestFingerprint,
  summarizePortfolioExperiment,
  type PortfolioBacktestRequestSnapshot,
  type PortfolioMethod,
  type PortfolioRunSnapshot,
} from "@/lib/portfolio-experiments";
import type {
  Instrument,
  PortfolioBacktestPayload,
  PortfolioExperimentRecord,
  PortfolioExperimentSummary,
  PortfolioMetrics,
  PortfolioPaperObservation,
  PortfolioPaperPublicStatus,
} from "@/lib/types";

const DEFAULT_ASSETS: Instrument[] = [
  {
    symbol: "BTCUSDT",
    name: "Bitcoin / USDT",
    market: "CRYPTO",
    exchange: "Binance Spot",
    currency: "USDT",
    provider: "binance",
    asset_type: "crypto",
  },
  {
    symbol: "NDX",
    name: "NASDAQ-100",
    market: "INDEX",
    exchange: "Nasdaq",
    currency: "USD",
    provider: "macro",
    asset_type: "index",
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
];

type PortfolioPreset = {
  id: "cross-market" | "crypto-majors" | "us-risk-and-defence";
  label: string;
  detail: string;
  assets: Instrument[];
  lookback: number;
  rebalanceBars: number;
  maximumWeight: number;
};

const PORTFOLIO_PRESETS: readonly PortfolioPreset[] = [
  {
    id: "cross-market",
    label: "跨市场 · 默认",
    detail: "加密 / 美股指数 / 原油 / 黄金",
    assets: DEFAULT_ASSETS,
    lookback: 60,
    rebalanceBars: 21,
    maximumWeight: 40,
  },
  {
    id: "crypto-majors",
    label: "加密主流现货",
    detail: "BTC / ETH / BNB · 精确 USDT 计价",
    assets: [
      {
        symbol: "BTCUSDT",
        name: "Bitcoin / USDT",
        market: "CRYPTO",
        exchange: "Binance Spot",
        currency: "USDT",
        provider: "binance",
        asset_type: "crypto",
      },
      {
        symbol: "ETHUSDT",
        name: "Ethereum / USDT",
        market: "CRYPTO",
        exchange: "Binance Spot",
        currency: "USDT",
        provider: "binance",
        asset_type: "crypto",
      },
      {
        symbol: "BNBUSDT",
        name: "BNB / USDT",
        market: "CRYPTO",
        exchange: "Binance Spot",
        currency: "USDT",
        provider: "binance",
        asset_type: "crypto",
      },
    ],
    lookback: 60,
    rebalanceBars: 21,
    maximumWeight: 40,
  },
  {
    id: "us-risk-and-defence",
    label: "美元权益与避险",
    detail: "SPY / QQQ / GLD · 同币种 ETF",
    assets: [
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
        symbol: "GLD",
        name: "SPDR Gold Shares",
        market: "ETF",
        exchange: "NYSE Arca",
        currency: "USD",
        provider: "yfinance",
        asset_type: "etf",
      },
    ],
    lookback: 60,
    rebalanceBars: 21,
    maximumWeight: 40,
  },
];

const METHOD_COLORS: Array<[PortfolioMethod, string]> = [
  ["periodic_inverse_volatility", "#55d6be"],
  ["periodic_equal", "#e7b96b"],
  ["initial_equal_hold", "#8fa6ff"],
];

function percent(value: number) {
  return `${(value * 100).toFixed(2)}%`;
}

function dateOnly(value: string) {
  return value.slice(0, 10);
}

function receiptHasExpired(value?: string): boolean {
  if (!value) return false;
  const expiresAt = Date.parse(value);
  return Number.isFinite(expiresAt) && expiresAt <= Date.now();
}

function localDateTime(value: string): string {
  return new Date(value).toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function currencyGroup(currency: string) {
  return ["USD", "USDT", "USDC"].includes(currency.toUpperCase())
    ? "USD"
    : currency.toUpperCase();
}

export default function PortfolioPage() {
  const [assets, setAssets] = useState<Instrument[]>(DEFAULT_ASSETS);
  const [pickerQuery, setPickerQuery] = useState("");
  const [pickerSelection, setPickerSelection] = useState<Instrument | null>(null);
  const [initialDates] = useState(() => portfolioDefaultDates());
  const [start, setStart] = useState(initialDates.start);
  const [end, setEnd] = useState(initialDates.end);
  const [lookback, setLookback] = useState(60);
  const [rebalanceBars, setRebalanceBars] = useState(21);
  const [maximumWeight, setMaximumWeight] = useState(40);
  const [latestRun, setLatestRun] = useState<PortfolioRunSnapshot | null>(null);
  const [viewedExperiment, setViewedExperiment] =
    useState<PortfolioExperimentRecord | null>(null);
  const [experimentSummaries, setExperimentSummaries] = useState<
    PortfolioExperimentSummary[]
  >([]);
  const [vaultOpen, setVaultOpen] = useState(false);
  const [vaultError, setVaultError] = useState("");
  const [portfolioObservations, setPortfolioObservations] = useState<
    PortfolioPaperObservation[]
  >([]);
  const [portfolioObservationStatus, setPortfolioObservationStatus] =
    useState<PortfolioPaperPublicStatus | null>(null);
  const [portfolioObservationsLoading, setPortfolioObservationsLoading] =
    useState(true);
  const [portfolioObservationsError, setPortfolioObservationsError] = useState("");
  const [operationIds, setOperationIds] = useState<string[]>(["list"]);
  const [notice, setNotice] = useState<{
    kind: "status" | "error";
    text: string;
  } | null>(null);
  const [showSaveEditor, setShowSaveEditor] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [saveNotes, setSaveNotes] = useState("");
  const [focusMethod, setFocusMethod] = useState<PortfolioMethod>(
    "periodic_inverse_volatility",
  );
  const [savedRunRecord, setSavedRunRecord] = useState<{
    run: PortfolioRunSnapshot;
    id: string;
  } | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void apiFetch<PortfolioExperimentSummary[]>(
      "/api/v1/experiments/portfolios?limit=100",
    )
      .then((items) => {
        if (!active) return;
        setExperimentSummaries(items);
        setVaultError("");
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setVaultError(
          reason instanceof Error ? reason.message : "组合档案读取失败",
        );
      })
      .finally(() => {
        if (!active) return;
        setOperationIds((ids) => ids.filter((id) => id !== "list"));
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    let inFlight = false;
    const loadObservations = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const [status, observations] = await Promise.all([
          apiFetch<PortfolioPaperPublicStatus>("/api/v1/portfolio-paper/status"),
          apiFetch<PortfolioPaperObservation[]>(
            "/api/v1/portfolio-paper/observations?limit=100",
          ),
        ]);
        if (!active) return;
        setPortfolioObservationStatus(status);
        setPortfolioObservations(observations);
        setPortfolioObservationsError("");
      } catch (reason: unknown) {
        if (!active) return;
        setPortfolioObservationsError(
          reason instanceof Error ? reason.message : "前向观察读取失败",
        );
      } finally {
        inFlight = false;
        if (active) setPortfolioObservationsLoading(false);
      }
    };

    void loadObservations();
    const interval = window.setInterval(() => void loadObservations(), 30_000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, []);

  const currentRequest = useMemo(
    () =>
      buildPortfolioRequest({
        assets,
        start,
        end,
        volatilityLookback: lookback,
        rebalanceBars,
        maximumWeightPercent: maximumWeight,
      }),
    [assets, end, lookback, maximumWeight, rebalanceBars, start],
  );
  const currentRequestFingerprint = useMemo(
    () => portfolioRequestFingerprint(currentRequest),
    [currentRequest],
  );
  const latestRequestFingerprint = latestRun
    ? portfolioRequestFingerprint(latestRun.request)
    : null;
  const resultIsStale =
    viewedExperiment === null &&
    latestRequestFingerprint !== null &&
    latestRequestFingerprint !== currentRequestFingerprint;
  const payload = viewedExperiment
    ? portfolioExperimentToPayload(viewedExperiment)
    : latestRun?.payload ?? null;
  const runBusy = operationIds.some((id) => id.startsWith("run:"));
  const latestRunSaved =
    latestRun !== null && savedRunRecord?.run === latestRun;
  const latestRunReceiptUnavailable =
    latestRun !== null &&
    (!latestRun.payload.run_id ||
      receiptHasExpired(latestRun.payload.run_expires_at));
  const portfolioExperimentNames = useMemo(
    () =>
      Object.fromEntries(
        experimentSummaries.map((summary) => [summary.id, summary.name]),
      ) as Record<string, string>,
    [experimentSummaries],
  );

  const chartSeries = useMemo(() => {
    if (!payload) return [];
    return METHOD_COLORS.map(([method, color]) => {
      const equity = payload.results[method].equity;
      const base = equity[0]?.equity || 100_000;
      return {
        name: portfolioMethodCopy(
          method,
          payload.assets.length,
          payload.assumptions,
        ).name,
        color,
        data: equity.map((row) => [row.date, (row.equity / base) * 100] as [string, number]),
      };
    });
  }, [payload]);

  function addAsset() {
    if (!pickerSelection || assets.length >= 6) return;
    if (assets.some((asset) => asset.symbol === pickerSelection.symbol)) {
      setError("这个标的已经在组合中。");
      return;
    }
    if (
      assets.some(
        (asset) =>
          currencyGroup(asset.currency) !== currencyGroup(pickerSelection.currency),
      )
    ) {
      setError(
        `当前组合按 ${assets[0]?.currency} 计价，暂不能直接加入 ${pickerSelection.currency} 资产；汇率换算完成前不生成混合币种结果。`,
      );
      return;
    }
    setAssets((items) => [...items, pickerSelection]);
    setPickerSelection(null);
    setPickerQuery("");
    setError("");
  }

  function beginOperation(operationId: string) {
    setOperationIds((ids) => [...new Set([...ids, operationId])]);
  }

  function endOperation(operationId: string) {
    setOperationIds((ids) => ids.filter((id) => id !== operationId));
  }

  function applyExperimentConfiguration(
    hydrated: ReturnType<typeof hydratePortfolioExperiment>,
  ) {
    setAssets(hydrated.assets);
    setStart(hydrated.start);
    setEnd(hydrated.end);
    setLookback(hydrated.volatilityLookback);
    setRebalanceBars(hydrated.rebalanceBars);
    setMaximumWeight(hydrated.maximumWeightPercent);
    setPickerQuery("");
    setPickerSelection(null);
    setError("");
  }

  function applyPreset(preset: PortfolioPreset) {
    setAssets(preset.assets.map((asset) => ({ ...asset })));
    setLookback(preset.lookback);
    setRebalanceBars(preset.rebalanceBars);
    setMaximumWeight(preset.maximumWeight);
    setPickerQuery("");
    setPickerSelection(null);
    setError("");
    setNotice({
      kind: "status",
      text: `已载入「${preset.label}」研究篮子；请运行后查看本次真实历史证据。`,
    });
  }

  async function runPortfolio(
    requestSnapshot: PortfolioBacktestRequestSnapshot,
    assetSnapshot: readonly Instrument[],
    operationId = "run:current",
  ) {
    const frozenRequest: PortfolioBacktestRequestSnapshot = {
      ...requestSnapshot,
      assets: requestSnapshot.assets.map((asset) => ({ ...asset })),
    };
    const frozenAssets = assetSnapshot.map((asset) => ({ ...asset }));
    if (frozenRequest.assets.length < 2) {
      setError("至少保留两个不同资产，才能研究分散效果。");
      return;
    }
    if (frozenRequest.maximum_asset_weight * frozenRequest.assets.length < 1) {
      setError("单资产上限过低，组合权重无法合计到 100%。");
      return;
    }
    beginOperation(operationId);
    setError("");
    setNotice(null);
    try {
      const result = await apiFetch<PortfolioBacktestPayload>(
        "/api/v1/backtests/portfolio",
        {
          method: "POST",
          body: JSON.stringify(frozenRequest),
        },
      );
      setLatestRun({
        request: frozenRequest,
        assets: frozenAssets,
        payload: result,
      });
      setViewedExperiment(null);
      setShowSaveEditor(false);
      setNotice({ kind: "status", text: "组合实验已完成，证据区已更新。" });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "组合实验失败");
    } finally {
      endOperation(operationId);
    }
  }

  async function readExperiment(
    id: string,
    operationId: string,
  ): Promise<PortfolioExperimentRecord | null> {
    beginOperation(operationId);
    setVaultError("");
    try {
      return await apiFetch<PortfolioExperimentRecord>(
        `/api/v1/experiments/portfolios/${encodeURIComponent(id)}`,
      );
    } catch (reason) {
      setVaultError(
        reason instanceof Error ? reason.message : "组合档案读取失败",
      );
      return null;
    } finally {
      endOperation(operationId);
    }
  }

  async function viewExperiment(summary: PortfolioExperimentSummary) {
    const record = await readExperiment(summary.id, `view:${summary.id}`);
    if (!record) return;
    setViewedExperiment(record);
    setVaultOpen(false);
    setNotice({
      kind: "status",
      text: `正在查看「${record.name}」的保存快照；没有重新运行回测。`,
    });
  }

  async function loadExperimentConfiguration(
    summary: PortfolioExperimentSummary,
  ) {
    const record = await readExperiment(summary.id, `load:${summary.id}`);
    if (!record) return;
    try {
      applyExperimentConfiguration(hydratePortfolioExperiment(record));
      setViewedExperiment(null);
      setVaultOpen(false);
      setNotice({
        kind: "status",
        text: `已载入「${record.name}」的配置。旧结果仍保留，重新运行前会标记为过期。`,
      });
    } catch (reason) {
      setVaultError(
        reason instanceof Error ? reason.message : "档案配置无法恢复",
      );
    }
  }

  async function deleteExperiment(summary: PortfolioExperimentSummary) {
    const operationId = `delete:${summary.id}`;
    beginOperation(operationId);
    setVaultError("");
    try {
      await apiFetch<void>(
        `/api/v1/experiments/portfolios/${encodeURIComponent(summary.id)}`,
        { method: "DELETE" },
      );
      setExperimentSummaries((items) =>
        items.filter((item) => item.id !== summary.id),
      );
      setViewedExperiment((record) =>
        record?.id === summary.id ? null : record,
      );
      setSavedRunRecord((record) =>
        record?.id === summary.id ? null : record,
      );
      setNotice({
        kind: "status",
        text: `已永久删除组合档案「${summary.name}」。`,
      });
    } catch (reason) {
      setVaultError(
        reason instanceof Error ? reason.message : "组合档案删除失败",
      );
    } finally {
      endOperation(operationId);
    }
  }

  function openSaveEditor() {
    if (!latestRun) return;
    if (latestRunSaved) {
      setVaultOpen(true);
      return;
    }
    setSaveName(defaultPortfolioExperimentName(latestRun.request));
    setSaveNotes("");
    setFocusMethod("periodic_inverse_volatility");
    setShowSaveEditor(true);
  }

  async function saveLatestRun() {
    if (!latestRun || latestRunSaved || !saveName.trim()) return;
    if (
      !latestRun.payload.run_id ||
      receiptHasExpired(latestRun.payload.run_expires_at)
    ) {
      setShowSaveEditor(false);
      setNotice({
        kind: "error",
        text: "服务端运行回执已过期或不可用，请重新运行后再保存。",
      });
      return;
    }
    const operationId = "save:latest";
    beginOperation(operationId);
    setVaultError("");
    setNotice(null);
    try {
      const body = buildPortfolioExperimentFromRunPayload({
        name: saveName,
        notes: saveNotes,
        focusMethod,
        run: latestRun,
      });
      const record = await apiFetch<PortfolioExperimentRecord>(
        "/api/v1/experiments/portfolios/from-run",
        {
          method: "POST",
          body: JSON.stringify(body),
        },
      );
      const summary = summarizePortfolioExperiment(record);
      setExperimentSummaries((items) => [
        summary,
        ...items.filter((item) => item.id !== summary.id),
      ]);
      setSavedRunRecord({ run: latestRun, id: record.id });
      setShowSaveEditor(false);
      setNotice({
        kind: "status",
        text: `组合实验「${record.name}」已保存到 NAS 档案。`,
      });
    } catch (reason) {
      setNotice({
        kind: "error",
        text: reason instanceof Error ? reason.message : "组合实验保存失败",
      });
    } finally {
      endOperation(operationId);
    }
  }

  async function rerunViewedExperiment(record: PortfolioExperimentRecord) {
    try {
      const hydrated = hydratePortfolioExperiment(record);
      applyExperimentConfiguration(hydrated);
      await runPortfolio(
        hydrated.request,
        hydrated.assets,
        `run:archive:${record.id}`,
      );
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "档案配置无法重新运行",
      );
    }
  }

  return (
    <div className="page portfolio-page">
      <header className="topbar">
        <div>
          <span className="eyebrow">MULTI-ASSET ALLOCATION LAB</span>
          <h1>组合实验</h1>
        </div>
        <div className="portfolio-top-actions">
          <div className="portfolio-safety">
            <i />
            研究组合 · 不连接账户
          </div>
          <button
            aria-controls="portfolio-vault"
            aria-expanded={vaultOpen}
            className="portfolio-vault-toggle"
            onClick={() => setVaultOpen((open) => !open)}
            type="button"
          >
            组合档案 <strong>{experimentSummaries.length}</strong>
          </button>
        </div>
      </header>

      <section className="portfolio-intro">
        <div>
          <span className="eyebrow">DIVERSIFICATION BEFORE COMPLEXITY</span>
          <h2>先研究怎么分配资金，再讨论更复杂的择时。</h2>
          <p>
            同时比较起点等权、定期等权和逆波动风险配置。所有动态权重只使用当时已经出现的数据，
            在下一根共同交易日开盘调整，并计入费用与滑点。
          </p>
        </div>
        <div className="portfolio-intro-facts">
          <span>2–6 个资产</span>
          <span>日线共同样本</span>
          <span>四段稳定性</span>
        </div>
      </section>

      {notice && (
        <div
          aria-live={notice.kind === "error" ? "assertive" : "polite"}
          className={`portfolio-notice ${notice.kind}`}
          role={notice.kind === "error" ? "alert" : "status"}
        >
          <span>{notice.text}</span>
          <button
            aria-label="关闭提示"
            onClick={() => setNotice(null)}
            type="button"
          >
            ×
          </button>
        </div>
      )}

      {vaultOpen && (
        <PortfolioExperimentVault
          error={vaultError}
          id="portfolio-vault"
          loading={operationIds.includes("list")}
          onDelete={(summary) => void deleteExperiment(summary)}
          onLoad={(summary) => void loadExperimentConfiguration(summary)}
          onView={(summary) => void viewExperiment(summary)}
          operationIds={operationIds}
          summaries={experimentSummaries}
        />
      )}

      <PortfolioForwardObservation
        error={portfolioObservationsError}
        experimentNames={portfolioExperimentNames}
        loading={portfolioObservationsLoading}
        observations={portfolioObservations}
        status={portfolioObservationStatus}
      />

      <div className="portfolio-layout">
        <aside className="portfolio-builder panel">
          <div className="panel-number">01 / UNIVERSE</div>
          <h2>定义资产篮子</h2>
          <div className="portfolio-assets">
            {assets.map((asset) => (
              <div className="portfolio-asset" key={`${asset.market}-${asset.symbol}`}>
                <div>
                  <strong>{asset.symbol}</strong>
                  <span>
                    {asset.name} · {asset.currency}
                  </span>
                </div>
                <button
                  aria-label={`移除 ${asset.symbol}`}
                  disabled={assets.length <= 2}
                  onClick={() => {
                    setAssets((items) =>
                      items.filter((item) => item.symbol !== asset.symbol),
                    );
                    setError("");
                  }}
                  type="button"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
          <div className="portfolio-presets" aria-label="研究篮子预设">
            <span>快速研究篮子</span>
            <div>
              {PORTFOLIO_PRESETS.map((preset) => {
                const selected =
                  assets.length === preset.assets.length &&
                  assets.every(
                    (asset, index) => asset.symbol === preset.assets[index]?.symbol,
                  );
                return (
                  <button
                    aria-pressed={selected}
                    className={selected ? "active" : ""}
                    key={preset.id}
                    onClick={() => applyPreset(preset)}
                    title={preset.detail}
                    type="button"
                  >
                    <strong>{preset.label}</strong>
                    <small>{preset.detail}</small>
                  </button>
                );
              })}
            </div>
          </div>
          <SymbolPicker
            label="添加资产"
            onQueryChange={setPickerQuery}
            onSelect={setPickerSelection}
            placeholder="名称或代码，如 黄金 / SPY / EURUSD"
            query={pickerQuery}
            selected={pickerSelection}
          />
          <button
            className="secondary-button full"
            disabled={!pickerSelection || assets.length >= 6}
            onClick={addAsset}
            type="button"
          >
            {assets.length >= 6 ? "最多 6 个资产" : "加入组合"}
          </button>
          <fieldset className="portfolio-settings">
            <legend>实验区间与规则</legend>
            <div className="date-grid">
              <label>
                开始
                <input
                  max={end}
                  onChange={(event) => {
                    setStart(event.target.value);
                    setError("");
                  }}
                  type="date"
                  value={start}
                />
              </label>
              <label>
                结束
                <input
                  min={start}
                  onChange={(event) => {
                    setEnd(event.target.value);
                    setError("");
                  }}
                  type="date"
                  value={end}
                />
              </label>
            </div>
            <label>
              波动率观察 K 线
              <input
                max="252"
                min="20"
                onChange={(event) => {
                  setLookback(Number(event.target.value));
                  setError("");
                }}
                step="1"
                type="number"
                value={lookback}
              />
            </label>
            <label>
              再平衡间隔 K 线
              <input
                max="63"
                min="5"
                onChange={(event) => {
                  setRebalanceBars(Number(event.target.value));
                  setError("");
                }}
                step="1"
                type="number"
                value={rebalanceBars}
              />
              <small>参考：21 根约等于一个交易月</small>
            </label>
            <label>
              单资产权重上限 %
              <input
                max="100"
                min={Math.ceil(100 / assets.length)}
                onChange={(event) => {
                  setMaximumWeight(Number(event.target.value));
                  setError("");
                }}
                step="1"
                type="number"
                value={maximumWeight}
              />
            </label>
          </fieldset>
          <button
            className="primary-button full"
            disabled={runBusy}
            onClick={() => void runPortfolio(currentRequest, assets)}
            type="button"
          >
            {runBusy ? "正在对齐真实行情并计算…" : "运行组合实验 ↗"}
          </button>
          {error && <p className="portfolio-error">{error}</p>}
        </aside>

        <section className="portfolio-evidence panel">
          <div className="panel-number">02 / EVIDENCE</div>
          {viewedExperiment && (
            <PortfolioSnapshotBanner
              hasCurrentResult={latestRun !== null}
              onBack={() => {
                setViewedExperiment(null);
                setNotice(null);
              }}
              onLoadAndRun={() =>
                void rerunViewedExperiment(viewedExperiment)
              }
              record={viewedExperiment}
              rerunning={operationIds.includes(
                `run:archive:${viewedExperiment.id}`,
              )}
            />
          )}
          {resultIsStale && latestRun && (
            <div
              aria-live="polite"
              className="portfolio-stale-result"
              role="status"
            >
              <div>
                <span>CONFIGURATION CHANGED</span>
                <strong>配置已修改，当前仍显示上一轮结果</strong>
                <p>
                  下方证据仍对应 {latestRun.assets.length} 个资产 ·{" "}
                  {latestRun.request.start} — {latestRun.request.end}。只有重新运行后，
                  才会用新配置替换这份结果。
                </p>
              </div>
              <button
                className="secondary-button"
                disabled={runBusy}
                onClick={() => void runPortfolio(currentRequest, assets)}
                type="button"
              >
                {runBusy ? "正在重新计算…" : "按新配置重新运行"}
              </button>
            </div>
          )}
          {!payload ? (
            <div className="portfolio-empty">
              <span>
                {String(assets.length).padStart(2, "0")} ASSETS · 03 RULES · 04
                WINDOWS
              </span>
              <h2>用同一组真实行情，比较收益与风险来自哪里。</h2>
              <p>
                组合功能不会帮你挑“历史最高收益”；它优先检查回撤是否跨窗口改善，
                并把换仓次数、资金周转和交易摩擦一起披露。
              </p>
            </div>
          ) : (
            <>
              {!viewedExperiment && latestRun && (
                <>
                  <div className="portfolio-result-toolbar">
                    <div>
                      <span className="eyebrow">CURRENT RUN · REPRODUCIBLE</span>
                      <strong>
                        {latestRun.request.start} — {latestRun.request.end} ·{" "}
                        {latestRun.assets.length} 个资产
                      </strong>
                      {latestRun.payload.run_expires_at && (
                        <small>
                          服务端回执有效至{" "}
                          <time
                            dateTime={latestRun.payload.run_expires_at}
                            suppressHydrationWarning
                          >
                            {localDateTime(latestRun.payload.run_expires_at)}
                          </time>
                          {latestRun.payload.calculation_version
                            ? ` · 计算版本 ${latestRun.payload.calculation_version}`
                            : ""}
                        </small>
                      )}
                    </div>
                    <button
                      className={latestRunSaved ? "saved" : ""}
                      disabled={
                        operationIds.includes("save:latest") || runBusy
                      }
                      onClick={() => {
                        if (latestRunReceiptUnavailable) {
                          void runPortfolio(
                            currentRequest,
                            assets,
                            "run:receipt-refresh",
                          );
                          return;
                        }
                        openSaveEditor();
                      }}
                      type="button"
                    >
                      {operationIds.includes("save:latest")
                        ? "正在保存…"
                        : latestRunSaved
                          ? "已保存 · 查看档案"
                          : latestRunReceiptUnavailable
                            ? "重新运行后保存"
                            : "保存本次实验"}
                    </button>
                  </div>
                  {showSaveEditor && !latestRunSaved && (
                    <PortfolioSavePanel
                      focusMethod={focusMethod}
                      name={saveName}
                      notes={saveNotes}
                      onCancel={() => setShowSaveEditor(false)}
                      onFocusMethodChange={setFocusMethod}
                      onNameChange={setSaveName}
                      onNotesChange={setSaveNotes}
                      onSave={() => void saveLatestRun()}
                      saving={operationIds.includes("save:latest")}
                    />
                  )}
                </>
              )}

              <div
                className={`portfolio-decision ${
                  payload.research_decision.risk_evidence_passed ? "passed" : "watch"
                }`}
              >
                <div>
                  <span className="eyebrow">RESEARCH DECISION</span>
                  <h2>{payload.research_decision.title}</h2>
                  <p>{payload.research_decision.reason}</p>
                </div>
                <div>
                  <strong>
                    {payload.research_decision.evaluable_segments} /{" "}
                    {payload.research_decision.total_segments}
                  </strong>
                  <span>可评估窗口</span>
                </div>
                <div>
                  <strong>
                    {payload.research_decision.drawdown_improved_segments} /{" "}
                    {payload.research_decision.total_segments}
                  </strong>
                  <span>严格回撤改善</span>
                </div>
                <div>
                  <strong>
                    {payload.research_decision.sharpe_improved_segments} /{" "}
                    {payload.research_decision.total_segments}
                  </strong>
                  <span>窗口夏普改善</span>
                </div>
              </div>

              <div className="portfolio-methods">
                {METHOD_COLORS.map(([method]) => (
                  <MethodCard
                    assetCount={payload.assets.length}
                    assumptions={payload.assumptions}
                    key={method}
                    highlighted={
                      method === "periodic_inverse_volatility" &&
                      payload.research_decision.risk_evidence_passed
                    }
                    metrics={payload.results[method].metrics}
                    method={method}
                  />
                ))}
              </div>

              <div className="portfolio-chart-card">
                <div>
                  <span className="eyebrow">NORMALIZED EQUITY</span>
                  <h3>同起点净值 · 100</h3>
                  <small>{payload.common_bars} 根共同交易日 K 线</small>
                </div>
                <LineChart height={340} series={chartSeries} />
              </div>

              <section className="portfolio-weights">
                <div>
                  <span className="eyebrow">TARGET VS. REALIZED ALLOCATION</span>
                  <h3>目标权重与期末实际权重</h3>
                  <p>
                    目标权重是最后一次再平衡时的指令；期末实际权重会随之后的价格变化自然漂移。
                    两者都不是价格方向预测，也不应被混称为“最新目标”。
                  </p>
                </div>
                <div className="portfolio-weight-snapshots">
                  <WeightSnapshot
                    eyebrow="LAST REBALANCE TARGET"
                    label="最后再平衡目标"
                    weights={
                      payload.results.periodic_inverse_volatility
                        .last_rebalance_target_weights
                    }
                  />
                  <WeightSnapshot
                    eyebrow="ENDING REALIZED"
                    label="期末实际 · 已含价格漂移"
                    realized
                    weights={
                      payload.results.periodic_inverse_volatility
                        .ending_realized_weights
                    }
                  />
                </div>
              </section>

              <section className="portfolio-segments">
                <div className="portfolio-section-heading">
                  <div>
                    <span className="eyebrow">REGIME WINDOWS</span>
                    <h3>四段风险改善是否重复出现</h3>
                  </div>
                  <p>每段重新从现金起步，不把前一段盈利带入下一段。</p>
                </div>
                <div className="portfolio-segment-grid">
                  {payload.segments.map((segment) => {
                    const equal = segment.results.periodic_equal.metrics;
                    const inverse =
                      segment.results.periodic_inverse_volatility.metrics;
                    return (
                      <article
                        className={
                          inverse.max_drawdown > equal.max_drawdown + 1e-6
                            ? "improved"
                            : "weaker"
                        }
                        key={segment.index}
                      >
                        <div>
                          <span>窗口 {segment.index}</span>
                          <small>
                            {dateOnly(segment.start)} — {dateOnly(segment.end)}
                          </small>
                        </div>
                        <strong>{percent(inverse.total_return)}</strong>
                        <p>
                          回撤 {percent(inverse.max_drawdown)} · 等权{" "}
                          {percent(equal.max_drawdown)}
                        </p>
                        <p>
                          夏普 {inverse.sharpe_ratio.toFixed(2)} · 等权{" "}
                          {equal.sharpe_ratio.toFixed(2)}
                        </p>
                      </article>
                    );
                  })}
                </div>
              </section>

              <div className="portfolio-sources">
                <span>
                  来源 {payload.citations.length} 条 · 费用{" "}
                  {percent(payload.assumptions.fee_rate)} · 滑点{" "}
                  {percent(payload.assumptions.slippage_rate)}
                </span>
                <div>
                  {payload.citations.map((citation) => (
                    <a
                      href={citation.url ?? undefined}
                      key={`${citation.source}-${citation.url}`}
                      rel="noreferrer"
                      target="_blank"
                    >
                      {citation.source} ↗
                    </a>
                  ))}
                </div>
              </div>
              <p className="disclaimer">
                组合历史回测不代表未来表现。共同交易日对齐会忽略 BTC
                的周末行情；指数值与期货连续行情也不是同一券商账户可直接成交的报价，
                实盘前必须换成具体可交易工具并重新验证。USD、USDT 与 USDC
                仅归为同一研究计价组，不代表 1:1 无风险；当前没有建模稳定币脱锚与兑换摩擦。
              </p>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

function MethodCard({
  method,
  metrics,
  highlighted,
  assetCount,
  assumptions,
}: {
  method: PortfolioMethod;
  metrics: PortfolioMetrics;
  highlighted: boolean;
  assetCount: number;
  assumptions: PortfolioBacktestPayload["assumptions"];
}) {
  const copy = portfolioMethodCopy(method, assetCount, assumptions);
  return (
    <article className={highlighted ? "portfolio-method highlighted" : "portfolio-method"}>
      <div>
        <span>{highlighted ? "RISK EVIDENCE" : "COMPARISON"}</span>
        <h3>{copy.name}</h3>
        <p>{copy.note}</p>
      </div>
      <dl>
        <div>
          <dt>累计收益</dt>
          <dd>{percent(metrics.total_return)}</dd>
        </div>
        <div>
          <dt>最大回撤</dt>
          <dd className="negative">{percent(metrics.max_drawdown)}</dd>
        </div>
        <div>
          <dt>夏普比率</dt>
          <dd>{metrics.sharpe_ratio.toFixed(2)}</dd>
        </div>
        <div>
          <dt>年化波动</dt>
          <dd>{percent(metrics.annualized_volatility)}</dd>
        </div>
        <div>
          <dt>再平衡</dt>
          <dd>{metrics.rebalances} 次</dd>
        </div>
        <div>
          <dt>交易摩擦</dt>
          <dd>{percent(metrics.transaction_cost_ratio)}</dd>
        </div>
      </dl>
    </article>
  );
}

function WeightSnapshot({
  eyebrow,
  label,
  weights,
  realized = false,
}: {
  eyebrow: string;
  label: string;
  weights: Record<string, number>;
  realized?: boolean;
}) {
  return (
    <section className={realized ? "portfolio-weight-group realized" : "portfolio-weight-group"}>
      <div>
        <span>{eyebrow}</span>
        <strong>{label}</strong>
      </div>
      <div>
        {Object.entries(weights).map(([symbol, weight]) => (
          <div className="portfolio-weight-row" key={symbol}>
            <span>{symbol}</span>
            <strong>{percent(weight)}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}
