import { useEffect, useRef } from "react";
import {
  ColorType,
  createChart,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type UTCTimestamp,
} from "lightweight-charts";

export interface OscillatorLine {
  values: (number | null)[];
  color: string;
  kind?: "line" | "histogram";
}

export function OscillatorChart({
  times,
  lines,
  height = 110,
  refLines = [],
}: {
  times: string[];
  lines: OscillatorLine[];
  height?: number;
  refLines?: number[];
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#97a1b3",
        fontFamily: "JetBrains Mono, monospace",
        fontSize: 10,
      },
      grid: { vertLines: { color: "#161c29" }, horzLines: { color: "#161c29" } },
      rightPriceScale: { borderColor: "#1c2331" },
      timeScale: { borderColor: "#1c2331", timeVisible: true, secondsVisible: false, visible: true },
      height,
      autoSize: true,
    });
    chartRef.current = chart;

    const tsArr = times.map((t) => (new Date(t).getTime() / 1000) as UTCTimestamp);

    for (const line of lines) {
      if (line.kind === "histogram") {
        const s = chart.addSeries(HistogramSeries, { color: line.color, priceLineVisible: false, lastValueVisible: false });
        s.setData(
          line.values
            .map((v, i) => (v == null ? null : { time: tsArr[i], value: v, color: v >= 0 ? "rgba(34,197,94,0.5)" : "rgba(239,68,68,0.5)" }))
            .filter((v): v is { time: UTCTimestamp; value: number; color: string } => v !== null),
        );
      } else {
        const s = chart.addSeries(LineSeries, { color: line.color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
        s.setData(
          line.values
            .map((v, i) => (v == null ? null : { time: tsArr[i], value: v }))
            .filter((v): v is { time: UTCTimestamp; value: number } => v !== null),
        );
      }
    }

    for (const ref of refLines) {
      const s = chart.addSeries(LineSeries, { color: "#3a4356", lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false });
      s.setData(tsArr.map((t) => ({ time: t, value: ref })));
    }

    chart.timeScale().fitContent();

    const resizeObserver = new ResizeObserver(() => {
      if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth });
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [times, lines, height, refLines]);

  return <div ref={containerRef} style={{ height }} className="w-full" />;
}
