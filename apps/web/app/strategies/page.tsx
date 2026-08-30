"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import type { Strategy } from "@/lib/types";

export default function StrategiesPage() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  useEffect(() => {
    apiFetch<Strategy[]>("/api/v1/strategies").then(setStrategies).catch(() => setStrategies([]));
  }, []);

  return (
    <div className="page">
      <header className="topbar">
        <div>
          <span className="eyebrow">Strategy registry</span>
          <h1>策略模板库</h1>
        </div>
        <Link className="secondary-button" href="/backtest">
          打开回测台 ↗
        </Link>
      </header>
      <section className="library-intro">
        <span>{strategies.length || "—"}</span>
        <div>
          <h2>少而精，且能解释。</h2>
          <p>
            每个模板都返回明确的 0–100% 资金仓位，统一通过无前视偏差的回测内核。它们是研究起点，不是收益承诺。
          </p>
        </div>
      </section>
      <div className="strategy-grid">
        {strategies.map((strategy, index) => (
          <article className="strategy-card" key={strategy.id}>
            <div className="strategy-card-top">
              <span className="strategy-number">{String(index + 1).padStart(2, "0")}</span>
              <span className="tag">{strategy.category}</span>
            </div>
            <h2>{strategy.name}</h2>
            <p>{strategy.description}</p>
            <div className="strategy-fit">
              <p>
                <span>适合</span>
                {strategy.best_for || "需要结合市场状态验证"}
              </p>
              <p>
                <span>风险</span>
                {strategy.risk_note || "历史表现不代表未来"}
              </p>
              <p>
                <span>周期</span>
                {strategy.recommended_intervals.join(" · ") || "按数据验证"}
              </p>
              <p>
                <span>预热</span>
                {strategy.warmup_bars > 0
                  ? `${strategy.warmup_bars} 根 K 线，不计入所选区间收益`
                  : "无需指标预热"}
              </p>
            </div>
            <div className="strategy-parameters">
              {Object.entries(strategy.parameters).map(([key, value]) => (
                <span key={key}>
                  {key} <b>{value}</b>
                </span>
              ))}
              {Object.keys(strategy.parameters).length === 0 && <span>无参数</span>}
            </div>
            <Link href={`/backtest?strategy=${strategy.id}`}>运行这个策略 →</Link>
          </article>
        ))}
      </div>
    </div>
  );
}
