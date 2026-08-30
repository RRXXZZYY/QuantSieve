"use client";

import * as echarts from "echarts";
import { useEffect, useRef } from "react";

type Series = {
  name: string;
  data: Array<[string, number]>;
  color?: string;
};

export function LineChart({ series, height = 320 }: { series: Series[]; height?: number }) {
  const element = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!element.current) return;
    const chart = echarts.init(element.current, undefined, { renderer: "canvas" });
    chart.setOption({
      animationDuration: 550,
      backgroundColor: "transparent",
      grid: { left: 10, right: 18, top: 28, bottom: 18, containLabel: true },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#111c23",
        borderColor: "#29414d",
        textStyle: { color: "#e8f0f2" },
      },
      legend: {
        top: 0,
        right: 12,
        textStyle: { color: "#8799a1" },
      },
      xAxis: {
        type: "time",
        boundaryGap: false,
        axisLine: { lineStyle: { color: "#29414d" } },
        axisLabel: { color: "#71858e" },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        scale: true,
        axisLabel: { color: "#71858e" },
        splitLine: { lineStyle: { color: "rgba(87, 111, 121, .16)" } },
      },
      series: series.map((item, index) => ({
        type: "line",
        name: item.name,
        data: item.data,
        showSymbol: false,
        smooth: 0.15,
        lineStyle: { width: 2, color: item.color ?? (index ? "#e7b96b" : "#55d6be") },
        areaStyle:
          index === 0
            ? {
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                  { offset: 0, color: "rgba(85, 214, 190, .24)" },
                  { offset: 1, color: "rgba(85, 214, 190, 0)" },
                ]),
              }
            : undefined,
      })),
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(element.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [series]);

  return <div ref={element} style={{ height, width: "100%" }} />;
}
