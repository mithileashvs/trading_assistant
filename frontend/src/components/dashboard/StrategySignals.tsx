import { Panel } from "../ui/Panel";
import { StatusBadge, type Tone } from "../ui/StatusBadge";
import { ErrorState, LoadingState } from "../ui/States";
import type { TimeframesResponse } from "../../lib/types";
import type { FetchState } from "../../lib/usePolling";

export function StrategySignals({ timeframes }: { timeframes: FetchState<TimeframesResponse> }) {
  const h4 = timeframes.data?.timeframes.H4;

  const cards: Array<{ name: string; value: string; interpretation: string; tone: Tone }> = h4 && h4.available
    ? [
        {
          name: "RSI (14)",
          value: h4.rsi != null ? h4.rsi.toFixed(1) : "—",
          interpretation: h4.rsi == null ? "—" : h4.rsi > 70 ? "Overbought" : h4.rsi < 30 ? "Oversold" : "Neutral",
          tone: h4.rsi == null ? "neutral" : h4.rsi > 70 ? "neg" : h4.rsi < 30 ? "pos" : "neutral",
        },
        {
          name: "MACD",
          value: h4.macd != null ? h4.macd.toFixed(3) : "—",
          interpretation:
            h4.macd_histogram == null ? "—" : h4.macd_histogram > 0 ? "Bullish momentum" : "Bearish momentum",
          tone: h4.macd_histogram == null ? "neutral" : h4.macd_histogram > 0 ? "pos" : "neg",
        },
        {
          name: "ADX (14)",
          value: h4.adx != null ? h4.adx.toFixed(1) : "—",
          interpretation: h4.adx == null ? "—" : h4.adx >= 25 ? "Trending" : "Weak / Ranging",
          tone: h4.adx == null ? "neutral" : h4.adx >= 25 ? "pos" : "neutral",
        },
        {
          name: "Bollinger Width",
          value: h4.bb_width_pct != null ? `${h4.bb_width_pct.toFixed(2)}%` : "—",
          interpretation: h4.bb_width_pct == null ? "—" : h4.bb_width_pct < 1 ? "Squeeze" : "Expanded",
          tone: "neutral" as const,
        },
        {
          name: "ATR%",
          value: h4.atr_pct != null ? `${h4.atr_pct.toFixed(2)}%` : "—",
          interpretation: "Volatility (H4)",
          tone: "neutral" as const,
        },
        {
          name: "Structure",
          value: h4.structure ?? "—",
          interpretation: `Regime: ${h4.regime ?? "—"}`,
          tone: "neutral" as const,
        },
      ]
    : [];

  return (
    <Panel title="Strategy Signals (H4)">
      {timeframes.loading && !timeframes.data ? (
        <LoadingState />
      ) : timeframes.error && !timeframes.data ? (
        <ErrorState title="UNAVAILABLE" detail={timeframes.error} />
      ) : (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
          {cards.map((c) => (
            <div key={c.name} className="flex flex-col gap-1 rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-2.5 py-2">
              <span className="text-[9.5px] font-semibold uppercase tracking-wider text-[var(--color-text-muted)]">{c.name}</span>
              <span className="font-num text-[14px] font-bold text-[var(--color-text-primary)]">{c.value}</span>
              <StatusBadge tone={c.tone} size="sm" className="w-fit">
                {c.interpretation}
              </StatusBadge>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
