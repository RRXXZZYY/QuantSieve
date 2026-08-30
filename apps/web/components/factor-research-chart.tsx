"use client";

import * as echarts from "echarts";
import { useEffect, useMemo, useRef } from "react";

import {
  buildFactorIcSeries,
  buildFactorQuantileBars,
} from "@/lib/factor-research";
import type { FactorHorizonDiagnostics } from "@/lib/types";

const CHART_TEXT = "#8799a1";
const CHART_LINE = "#29414d";
const CHART_GRID = "rgba(87, 111, 121, .16)";
const CHART_TOOLTIP = {
  backgroundColor: "#111c23",
  borderColor: CHART_LINE,
  textStyle: { color: "#e8f0f2" },
};

export function FactorResearchChart({
  horizon,
}: {
  horizon: FactorHorizonDiagnostics;
}) {
  const icElement = useRef<HTMLDivElement>(null);
  const quantileElement = useRef<HTMLDivElement>(null);
  const icSeries = useMemo(() => buildFactorIcSeries(horizon), [horizon]);
  const quantiles = useMemo(() => buildFactorQuantileBars(horizon), [horizon]);

  useEffect(() => {
    if (!icElement.current || icSeries.every((series) => series.data.length === 0)) {
      return;
    }
    const chart = echarts.init(icElement.current, undefined, { renderer: "canvas" });
    chart.setOption({
      animationDuration: 450,
      backgroundColor: "transparent",
      grid: { left: 10, right: 18, top: 34, bottom: 14, containLabel: true },
      tooltip: { ...CHART_TOOLTIP, trigger: "axis" },
      legend: {
        top: 0,
        right: 10,
        textStyle: { color: CHART_TEXT },
      },
      xAxis: {
        type: "time",
        boundaryGap: false,
        axisLine: { lineStyle: { color: CHART_LINE } },
        axisLabel: { color: CHART_TEXT, hideOverlap: true },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        min: -1,
        max: 1,
        axisLabel: { color: CHART_TEXT },
        splitLine: { lineStyle: { color: CHART_GRID } },
      },
      series: icSeries.map((series) => ({
        type: "line",
        name: series.name,
        data: series.data,
        showSymbol: false,
        connectNulls: false,
        smooth: 0.08,
        lineStyle: { color: series.color, width: 1.8 },
        markLine: {
          silent: true,
          symbol: "none",
          label: { show: false },
          lineStyle: { color: "rgba(143, 166, 255, .42)", type: "dashed" },
          data: [{ yAxis: 0 }],
        },
      })),
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(icElement.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [icSeries]);

  useEffect(() => {
    if (!quantileElement.current || quantiles.values.length === 0) return;
    const chart = echarts.init(quantileElement.current, undefined, {
      renderer: "canvas",
    });
    chart.setOption({
      animationDuration: 450,
      backgroundColor: "transparent",
      grid: { left: 10, right: 18, top: 18, bottom: 8, containLabel: true },
      tooltip: {
        ...CHART_TOOLTIP,
        trigger: "axis",
        axisPointer: { type: "shadow" },
        valueFormatter: (value: number) => `${(value * 100).toFixed(3)}%`,
      },
      xAxis: {
        type: "category",
        data: quantiles.categories,
        axisLine: { lineStyle: { color: CHART_LINE } },
        axisLabel: { color: CHART_TEXT },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        axisLabel: {
          color: CHART_TEXT,
          formatter: (value: number) => `${(value * 100).toFixed(1)}%`,
        },
        splitLine: { lineStyle: { color: CHART_GRID } },
      },
      series: [
        {
          type: "bar",
          name: "分位平均前瞻收益",
          data: quantiles.values.map((value, index) => ({
            value,
            itemStyle: {
              color:
                index === 0
                  ? "#df7d78"
                  : index === quantiles.values.length - 1
                    ? "#55d6be"
                    : "#7892a0",
              borderRadius: [4, 4, 0, 0],
            },
          })),
          barMaxWidth: 52,
          markLine: {
            silent: true,
            symbol: "none",
            label: { show: false },
            lineStyle: { color: "rgba(143, 166, 255, .42)", type: "dashed" },
            data: [{ yAxis: 0 }],
          },
        },
      ],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(quantileElement.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [quantiles]);

  return (
    <div className="factor-chart-grid">
      <section className="factor-chart-panel">
        <div className="factor-chart-heading">
          <div>
            <span className="eyebrow">IC SERIES</span>
            <h3>逐期相关性</h3>
          </div>
          <small>Rank IC 与 Pearson IC，零轴仅用于识别方向</small>
        </div>
        <div
          aria-label={`${horizon.diagnostics.label_name} 的 IC 时序图`}
          className="factor-chart-canvas"
          ref={icElement}
          role="img"
        />
      </section>
      <section className="factor-chart-panel">
        <div className="factor-chart-heading">
          <div>
            <span className="eyebrow">QUANTILES</span>
            <h3>分位收益梯度</h3>
          </div>
          <small>等权、毛收益；未计费用，也不是可交易组合</small>
        </div>
        <div
          aria-label={`${horizon.diagnostics.label_name} 的分位收益柱状图`}
          className="factor-chart-canvas"
          ref={quantileElement}
          role="img"
        />
      </section>
    </div>
  );
}
