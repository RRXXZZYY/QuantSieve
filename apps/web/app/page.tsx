"use client";

import { FormEvent, useEffect, useState } from "react";

import { BacktestChart } from "@/components/backtest-chart";
import { SettingsPanel, readLlmSettings } from "@/components/settings-panel";
import { SourceBadge } from "@/components/source-badge";
import { SymbolPicker } from "@/components/symbol-picker";
import { apiFetch, apiStream } from "@/lib/api";
import { formatAnnualizedReturn, formatPercent } from "@/lib/format";
import { marketLabel } from "@/lib/instruments";
import type {
  BacktestArtifact,
  Citation,
  Demo,
  Instrument,
  ResearchArtifact,
  ResearchSnapshotArtifact,
} from "@/lib/types";

type ConversationItem = {
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  artifacts?: ResearchArtifact[];
};

type ChatResponsePayload = {
  content: string;
  citations: Citation[];
  grounded: boolean;
  artifacts: ResearchArtifact[];
};

const TOOL_PROGRESS: Record<string, string> = {
  search_instruments: "正在识别公司与交易代码…",
  get_quote: "正在读取最新行情…",
  get_fundamentals: "正在读取财务数据…",
  get_capital_flow: "正在读取资金流…",
  get_news: "正在检索公司新闻…",
  run_backtest: "正在运行模板回测…",
  run_custom_backtest: "正在沙箱中执行生成的策略…",
  list_strategies: "正在读取策略注册表…",
};

export default function ResearchPage() {
  const [demos, setDemos] = useState<Demo[]>([]);
  const [selectedDemo, setSelectedDemo] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [instrumentQuery, setInstrumentQuery] = useState("");
  const [selectedInstrument, setSelectedInstrument] = useState<Instrument | null>(null);
  const [conversation, setConversation] = useState<ConversationItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [progress, setProgress] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    apiFetch<Demo[]>("/api/v1/demos").then(setDemos).catch(() => setDemos([]));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const question =
      input.trim() ||
      (selectedInstrument
        ? `请对${selectedInstrument.name}做一份基础研究，概括最新行情、近一年趋势与风险。`
        : "");
    if (!question || loading) return;
    const demo = demos.find((item) => item.id === selectedDemo && item.prompt === question);
    const settings = readLlmSettings();
    const groundedQuestion = selectedInstrument
      ? `${question}\n\n用户已选择研究标的：${selectedInstrument.name}（${
          selectedInstrument.symbol
        }，${marketLabel(selectedInstrument.market)}，${selectedInstrument.exchange}）。`
      : question;
    const requestMessages = [...conversation, { role: "user" as const, content: question }];
    setConversation([
      ...requestMessages,
      { role: "assistant", content: "", citations: [], artifacts: [] },
    ]);
    setInput("");
    setLoading(true);
    setProgress("正在建立研究任务…");
    setError("");
    try {
      await apiStream(
        "/api/v1/chat/stream",
        {
          messages: requestMessages.map(
            ({ role, content }, index, messages) => ({
              role,
              content:
                index === messages.length - 1 && role === "user" ? groundedQuestion : content,
            }),
          ),
          demo_id: demo?.id,
          api_key: demo ? undefined : settings.apiKey || undefined,
          base_url: settings.baseUrl,
          model: settings.model,
          symbol: selectedInstrument?.symbol,
          provider: selectedInstrument?.provider,
          instrument_name: selectedInstrument?.name,
        },
        ({ event, data }) => {
          if (event === "status") {
            const status = data as { type?: string; name?: string };
            if (status.type === "tool_start" && status.name) {
              setProgress(TOOL_PROGRESS[status.name] ?? `正在调用 ${status.name}…`);
            } else if (status.type === "tool_done") {
              setProgress("数据已返回，正在核对来源…");
            } else if (status.type === "composing") {
              setProgress("正在基于证据组织答案…");
            } else if (status.type === "demo") {
              setProgress("正在载入已标记的演示快照…");
            }
          } else if (event === "token") {
            const token = data as { text?: string };
            if (!token.text) return;
            setConversation((items) => {
              const next = [...items];
              const last = next.at(-1);
              if (last?.role === "assistant") {
                next[next.length - 1] = { ...last, content: last.content + token.text };
              }
              return next;
            });
          } else if (event === "done") {
            const response = data as ChatResponsePayload;
            setConversation((items) => {
              const next = [...items];
              next[next.length - 1] = {
                role: "assistant",
                content: response.content,
                citations: response.citations,
                artifacts: response.artifacts,
              };
              return next;
            });
          } else if (event === "error") {
            const payload = data as { detail?: string };
            throw new Error(payload.detail ?? "流式请求失败");
          }
        },
      );
      setSelectedDemo(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "请求失败");
      setConversation((items) => {
        const last = items.at(-1);
        return last?.role === "assistant" && !last.content ? items.slice(0, -1) : items;
      });
    } finally {
      setLoading(false);
      setProgress("");
    }
  }

  function chooseDemo(demo: Demo) {
    setSelectedDemo(demo.id);
    setInput(demo.prompt);
  }

  const quickQuestions = selectedInstrument
    ? selectedInstrument.market === "CN" || selectedInstrument.market === "US"
      ? [
          `分析${selectedInstrument.name}的中长期趋势和主要风险`,
          `检查${selectedInstrument.name}最近财务数据与新闻是否有重要变化`,
          `给${selectedInstrument.name}做一份不夸大结论的基础研究`,
        ]
      : [
          `分析${selectedInstrument.name}的中长期趋势和主要风险`,
          `检查${selectedInstrument.name}近期波动、回撤和区间位置`,
          `给${selectedInstrument.name}做一份不夸大结论的基础研究`,
        ]
    : ["分析苹果最近的趋势和风险", "研究 BTCUSDT 近一年风险", "分析宁德时代财务与资金流"];

  return (
    <div className="page research-page">
      <header className="topbar">
        <div>
          <span className="eyebrow">Research desk</span>
          <h1>先证据，后观点。</h1>
        </div>
        <div className="topbar-actions">
          <span className="market-status">
            <i /> 数据源按需连接
          </span>
          <SettingsPanel />
        </div>
      </header>

      <section className="research-hero">
        <div>
          <span className="hero-kicker">GROUNDED RESEARCH</span>
          <h2>
            把模糊问题，
            <br />
            变成<span>可核查的答案。</span>
          </h2>
          <p>QuantSieve 先调用真实数据与回测工具，再让模型解释。每个关键结论都带来源。</p>
        </div>
        <div className="trust-stack">
          <div>
            <strong>01</strong>
            <span>工具取数</span>
          </div>
          <div>
            <strong>02</strong>
            <span>量化计算</span>
          </div>
          <div>
            <strong>03</strong>
            <span>谨慎解读</span>
          </div>
        </div>
      </section>

      {conversation.length > 0 && (
        <section className="conversation" aria-live="polite">
          {conversation.map((message, index) => (
            <article className={`message ${message.role}`} key={`${message.role}-${index}`}>
              <span className="message-label">{message.role === "user" ? "YOU" : "ALPHA"}</span>
              <div>
                <p>{message.content}</p>
                {message.citations && (
                  <div className="source-row">
                    {message.citations.map((citation, citationIndex) => (
                      <SourceBadge
                        citation={citation}
                        key={`${citation.source}-${citationIndex}`}
                      />
                    ))}
                  </div>
                )}
                {message.artifacts?.map((artifact, artifactIndex) =>
                  artifact.artifact_type === "research_snapshot" ? (
                    <ResearchSnapshotCard
                      artifact={artifact}
                      key={`snapshot-${artifact.symbol}-${artifactIndex}`}
                    />
                  ) : (
                    <ResearchBacktestCard
                      artifact={artifact}
                      key={`backtest-${artifact.symbol}-${artifactIndex}`}
                    />
                  ),
                )}
              </div>
            </article>
          ))}
          {loading && <div className="thinking">{progress || "正在检索证据并组织答案…"}</div>}
        </section>
      )}

      <form className="research-composer" onSubmit={submit}>
        <div className="composer-label">
          <span>向 QuantSieve 提问</span>
          <small>
            {selectedDemo ? "预录演示 · 无需 Key" : "基础研究免 Key · 深度问答可选 BYOK"}
          </small>
        </div>
        <SymbolPicker
          label="研究标的（可选）"
          onQueryChange={setInstrumentQuery}
          onSelect={setSelectedInstrument}
          query={instrumentQuery}
          selected={selectedInstrument}
        />
        {selectedInstrument?.provider === "macro" && (
          <div className="reference-data-notice" role="note">
            <strong>官方日频参考序列</strong>
            <span>
              指数来自 Nasdaq / Cboe，外汇来自 ECB；指数本身不可直接交易，外汇参考汇率
              也不是券商可成交 OHLC 报价。
            </span>
          </div>
        )}
        <div className="research-quick-prompts" aria-label="研究问题建议">
          {quickQuestions.map((question) => (
            <button
              disabled={loading}
              key={question}
              onClick={() => {
                setInput(question);
                setSelectedDemo(null);
              }}
              type="button"
            >
              {question}
            </button>
          ))}
        </div>
        <textarea
          aria-label="研究问题"
          onChange={(event) => {
            setInput(event.target.value);
            if (event.target.value !== demos.find((item) => item.id === selectedDemo)?.prompt) {
              setSelectedDemo(null);
            }
          }}
          placeholder="例如：分析宁德时代最新财报，所有数字注明来源…"
          rows={3}
          value={input}
        />
        <div className="composer-footer">
          <span>模型不会被允许凭空补充数字</span>
          <button
            className="primary-button"
            disabled={loading || (!input.trim() && !selectedInstrument)}
            type="submit"
          >
            开始研究 <b>↗</b>
          </button>
        </div>
      </form>
      {error && <p className="error-banner">{error}</p>}

      <section className="demo-section">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Zero-config demos</span>
            <h2>先看看完整工作流</h2>
          </div>
          <span className="muted">录制快照均明确标记，不冒充实时数据</span>
        </div>
        <div className="demo-grid">
          {demos.map((demo, index) => (
            <button className="demo-card" key={demo.id} onClick={() => chooseDemo(demo)}>
              <span className="demo-index">0{index + 1}</span>
              <strong>{demo.title}</strong>
              <p>{demo.prompt}</p>
              <span className="demo-action">载入演示 →</span>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}

function ResearchBacktestCard({ artifact }: { artifact: BacktestArtifact }) {
  const metrics = artifact.result.metrics;
  return (
    <section className="research-artifact">
      <div className="research-artifact-heading">
        <div>
          <span className="eyebrow">SANDBOX BACKTEST · {artifact.symbol}</span>
          <h3>{artifact.strategy.name}</h3>
        </div>
        <span>{artifact.result.equity.length} 根 K 线</span>
      </div>
      <div className="artifact-metrics">
        <div>
          <span>累计收益</span>
          <strong>{formatPercent(metrics.total_return)}</strong>
        </div>
        <div>
          <span>年化收益</span>
          <strong>{formatAnnualizedReturn(metrics)}</strong>
        </div>
        <div>
          <span>夏普</span>
          <strong>{metrics.sharpe_ratio.toFixed(2)}</strong>
        </div>
        <div>
          <span>最大回撤</span>
          <strong className="negative">{formatPercent(metrics.max_drawdown)}</strong>
        </div>
      </div>
      <BacktestChart height={430} payload={artifact} />
      {artifact.strategy_code && (
        <details className="strategy-code">
          <summary>查看沙箱执行的策略代码</summary>
          <pre>{artifact.strategy_code}</pre>
        </details>
      )}
    </section>
  );
}

function ResearchSnapshotCard({ artifact }: { artifact: ResearchSnapshotArtifact }) {
  const movingAverages = [
    ["MA20", artifact.trend.ma20],
    ["MA60", artifact.trend.ma60],
    ["MA200", artifact.trend.ma200],
  ] as const;
  return (
    <section className="research-snapshot">
      <div className="research-snapshot-heading">
        <div>
          <span className="eyebrow">NO-KEY EVIDENCE BRIEF · {artifact.as_of}</span>
          <h3>
            {artifact.name} <small>{artifact.symbol}</small>
          </h3>
          <p>{artifact.trend.label}</p>
        </div>
        <div>
          <span>{artifact.reference_series ? "最新参考值" : "最新收盘"}</span>
          <strong>{formatResearchNumber(artifact.price)}</strong>
          <small>
            {artifact.bars} {artifact.reference_series ? "个日频观测" : "根日 K 线"}
          </small>
        </div>
      </div>
      <div className="snapshot-metrics">
        <SnapshotMetric label="20 周期" value={formatPercent(artifact.returns.twenty_period)} />
        <SnapshotMetric label="60 周期" value={formatPercent(artifact.returns.sixty_period)} />
        <SnapshotMetric label="近一年" value={formatPercent(artifact.returns.one_year)} />
        <SnapshotMetric
          label="年化波动"
          value={formatPercent(artifact.risk.annualized_volatility)}
        />
        <SnapshotMetric
          label="最大回撤"
          negative
          value={formatPercent(artifact.risk.max_drawdown)}
        />
        <SnapshotMetric
          label="区间位置"
          value={formatPercent(artifact.range.position)}
        />
      </div>
      <div className="snapshot-trend-strip">
        {movingAverages.map(([label, value]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value == null ? "样本不足" : formatResearchNumber(value)}</strong>
          </div>
        ))}
        <div>
          <span>近一年范围</span>
          <strong>
            {formatResearchNumber(artifact.range.low)} —{" "}
            {formatResearchNumber(artifact.range.high)}
          </strong>
        </div>
      </div>
      <div className="snapshot-modules">
        {artifact.modules.map((module) => (
          <div className={module.status} key={module.id}>
            <span>{module.label}</span>
            <strong>{module.status === "ready" ? "已核验" : "未返回"}</strong>
            <p>{module.detail}</p>
          </div>
        ))}
      </div>
      <p className="snapshot-boundary">
        这是确定性数据报告；未返回的模块不会由模型补写。需要开放式推理时再连接 BYOK。
      </p>
    </section>
  );
}

function SnapshotMetric({
  label,
  value,
  negative = false,
}: {
  label: string;
  value: string;
  negative?: boolean;
}) {
  return (
    <div>
      <span>{label}</span>
      <strong className={negative ? "negative" : ""}>{value}</strong>
    </div>
  );
}

function formatResearchNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: Math.abs(value) >= 1 ? 2 : 8,
  }).format(value);
}
