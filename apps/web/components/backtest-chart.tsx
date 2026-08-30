"use client";

import * as echarts from "echarts";
import { useEffect, useMemo, useRef } from "react";

import { buildBacktestMarkers } from "@/lib/backtest-markers";
import type { BacktestPayload } from "@/lib/types";

type Candle = {
  close: number;
  date: string;
  high: number;
  low: number;
  open: number;
};

function numeric(value: string | number | null | undefined): number {
  return typeof value === "number" ? value : Number(value ?? 0);
}

function axisDate(value: string, intraday: boolean): string {
  return intraday ? value.replace("T", " ").slice(0, 16) : value.slice(0, 10);
}

export function BacktestChart({
  payload,
  height = 520,
}: {
  payload: BacktestPayload;
  height?: number;
}) {
  const element = useRef<HTMLDivElement>(null);
  const candles = useMemo<Candle[]>(
    () =>
      payload.ohlcv
        .map((row) => ({
          date: String(row.date ?? ""),
          open: numeric(row.open),
          close: numeric(row.close),
          low: numeric(row.low),
          high: numeric(row.high),
        }))
        .filter(
          (row) =>
            row.date &&
            [row.open, row.close, row.low, row.high].every((value) => Number.isFinite(value)),
        ),
    [payload.ohlcv],
  );

  useEffect(() => {
    if (!element.current || !candles.length) return;
    const chart = echarts.init(element.current, undefined, { renderer: "canvas" });
    const intraday = ["15m", "1h", "4h"].includes(payload.interval);
    const { entries, exits } = buildBacktestMarkers({
      candles,
      trades: payload.result.trades,
      intraday,
    });

    chart.setOption({
      animationDuration: 450,
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      backgroundColor: "transparent",
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], start: 45, end: 100 },
        {
          type: "slider",
          xAxisIndex: [0, 1],
          bottom: 2,
          height: 18,
          borderColor: "#21343d",
          fillerColor: "rgba(85, 214, 190, .12)",
          handleStyle: { color: "#55d6be" },
          textStyle: { color: "#71858e" },
        },
      ],
      grid: [
        { left: 58, right: 20, top: 34, height: "50%" },
        { left: 58, right: 20, top: "67%", height: "19%" },
      ],
      legend: {
        right: 18,
        top: 0,
        data: ["K 线", "策略净值", "买入持有"],
        textStyle: { color: "#8799a1" },
      },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        backgroundColor: "#111c23",
        borderColor: "#29414d",
        textStyle: { color: "#e8f0f2" },
      },
      xAxis: [
        {
          type: "category",
          data: candles.map((candle) => axisDate(candle.date, intraday)),
          boundaryGap: true,
          axisLine: { lineStyle: { color: "#29414d" } },
          axisLabel: { color: "#71858e", hideOverlap: true },
          splitLine: { show: false },
        },
        {
          type: "category",
          gridIndex: 1,
          data: payload.result.equity.map((point) => axisDate(point.date, intraday)),
          boundaryGap: false,
          axisLine: { lineStyle: { color: "#29414d" } },
          axisLabel: { show: false },
          splitLine: { show: false },
        },
      ],
      yAxis: [
        {
          scale: true,
          axisLabel: { color: "#71858e" },
          splitLine: { lineStyle: { color: "rgba(87, 111, 121, .13)" } },
        },
        {
          scale: true,
          gridIndex: 1,
          axisLabel: { color: "#71858e" },
          splitLine: { lineStyle: { color: "rgba(87, 111, 121, .13)" } },
        },
      ],
      series: [
        {
          name: "K 线",
          type: "candlestick",
          data: candles.map((candle) => [
            candle.open,
            candle.close,
            candle.low,
            candle.high,
          ]),
          itemStyle: {
            color: "#e27a76",
            color0: "#55d6be",
            borderColor: "#e27a76",
            borderColor0: "#55d6be",
          },
          markPoint: {
            symbolSize: 34,
            data: [
              ...entries.map((point) => ({
                ...point,
                itemStyle: { color: "#55d6be" },
                label: { color: "#06211c", fontSize: 9 },
                symbol: "pin",
              })),
              ...exits.map((point) => ({
                ...point,
                itemStyle: { color: "#e8b767" },
                label: { color: "#261802", fontSize: 9 },
                symbol: "pin",
              })),
            ],
          },
        },
        {
          name: "策略净值",
          type: "line",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: payload.result.equity.map((point) => point.equity),
          showSymbol: false,
          smooth: 0.12,
          lineStyle: { color: "#e8b767", width: 2 },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: "rgba(232, 183, 103, .22)" },
              { offset: 1, color: "rgba(232, 183, 103, 0)" },
            ]),
          },
        },
        {
          name: "买入持有",
          type: "line",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: payload.benchmark.result.equity.map((point) => point.equity),
          showSymbol: false,
          smooth: 0.08,
          lineStyle: { color: "#71858e", type: "dashed", width: 1.5 },
        },
      ],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(element.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [candles, payload]);

  return <div className="backtest-chart" ref={element} style={{ height }} />;
}
