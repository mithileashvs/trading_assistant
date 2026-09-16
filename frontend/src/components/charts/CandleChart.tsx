import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { CandlesResponse } from "../../lib/types";

const OVERLAY_COLORS = {
  ema_20: "#e0bb3d",
  ema_50: "#60a5fa",
  ema_200: "#a78bfa",
  bb_upper: "#5b6478",
  bb_lower: "#5b6478",
} as const;

export function CandleChart({
  data,
  overlays,
  showEma = true,
  showBollinger = false,
  height = 360,
}: {
  data: CandlesResponse["candles"];
  overlays: CandlesResponse["overlays"];
  showEma?: boolean;
  showBollinger?: boolean;
  height?: number;
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
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#161c29" },
        horzLines: { color: "#161c29" },
      },
      rightPriceScale: { borderColor: "#1c2331" },
      timeScale: { borderColor: "#1c2331", timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
      height,
      autoSize: true,
    });
    chartRef.current = chart;

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderVisible: false,
      wickUpColor: "#22c55e",
      wickDownColor: "#ef4444",
      priceScaleId: "right",
    });

    const times = data.map((c) => (new Date(c.time).getTime() / 1000) as UTCTimestamp);
    candleSeries.setData(
      data.map((c, i) => ({ time: times[i], open: c.open, high: c.high, low: c.low, close: c.close })),
    );

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
      color: "#5b6478",
    });
    chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
    volumeSeries.setData(
      data.map((c, i) => ({
        time: times[i],
        value: c.volume,
        color: c.close >= c.open ? "rgba(34,197,94,0.4)" : "rgba(239,68,68,0.4)",
      })),
    );

    function addOverlay(key: keyof typeof OVERLAY_COLORS, values: (number | null)[]) {
      const series = chart.addSeries(LineSeries, {
        color: OVERLAY_COLORS[key],
        lineWidth: 1,
        priceScaleId: "right",
        lastValueVisible: false,
        priceLineVisible: false,
      });
      series.setData(
        values
          .map((v, i) => (v == null ? null : { time: times[i], value: v }))
          .filter((v): v is { time: UTCTimestamp; value: number } => v !== null),
      );
    }

    if (showEma) {
      addOverlay("ema_20", overlays.ema_20);
      addOverlay("ema_50", overlays.ema_50);
      addOverlay("ema_200", overlays.ema_200);
    }
    if (showBollinger) {
      addOverlay("bb_upper", overlays.bb_upper);
      addOverlay("bb_lower", overlays.bb_lower);
    }

    chart.timeScale().fitContent();

    const resizeObserver = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, overlays, showEma, showBollinger, height]);

  return <div ref={containerRef} style={{ height }} className="w-full" />;
}
