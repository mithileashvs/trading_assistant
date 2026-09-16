import { Panel } from "../ui/Panel";
import { StatusBadge } from "../ui/StatusBadge";
import { LoadingState, ErrorState } from "../ui/States";
import type { TimeframesResponse, TimeframeSnapshot } from "../../lib/types";
import type { FetchState } from "../../lib/usePolling";

const TF_ORDER: Array<{ key: "H4" | "H1" | "M15"; label: string }> = [
  { key: "H4", label: "H4" },
  { key: "H1", label: "H1" },
  { key: "M15", label: "M15" },
];

export function TimeframeAnalysis({ timeframes }: { timeframes: FetchState<TimeframesResponse> }) {
  const tfs = timeframes.data?.timeframes;
  const directions = tfs ? TF_ORDER.map((t) => tfs[t.key]?.trend_direction).filter(Boolean) : [];
  const allAvailable = tfs ? TF_ORDER.every((t) => tfs[t.key]?.available) : false;
  const conflicted =
    allAvailable && new Set(directions.filter((d) => d !== "NEUTRAL")).size > 1;

  return (
    <Panel
      title="Multi-Timeframe Analysis"
      right={
        allAvailable ? (
          <StatusBadge tone={conflicted ? "warn" : "pos"}>
            {conflicted ? "Timeframe Conflict" : "Confluent"}
          </StatusBadge>
        ) : undefined
      }
    >
      {timeframes.loading && !timeframes.data ? (
        <LoadingState label="Loading timeframe data…" />
      ) : timeframes.error && !timeframes.data ? (
        <ErrorState title="TIMEFRAME DATA UNAVAILABLE" detail={timeframes.error} />
      ) : (
        <div className="grid grid-cols-3 gap-2">
          {TF_ORDER.map((tf) => (
            <TimeframeCell key={tf.key} label={tf.label} snap={tfs?.[tf.key]} />
          ))}
        </div>
      )}
      {conflicted && (
        <div className="mt-2.5 rounded border border-[var(--color-warn)]/30 bg-[var(--color-warn-dim)]/20 px-2.5 py-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--color-warn)]">
          Timeframe conflict — trading may be blocked by the backend's own gating logic.
        </div>
      )}
    </Panel>
  );
}

function TimeframeCell({ label, snap }: { label: string; snap?: TimeframeSnapshot }) {
  if (!snap || !snap.available) {
    return (
      <div className="flex flex-col gap-1 rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-2.5 py-2">
        <span className="text-[10px] font-bold text-[var(--color-text-muted)]">{label}</span>
        <span className="text-[10.5px] text-[var(--color-text-muted)]">UNAVAILABLE</span>
      </div>
    );
  }
  const dirTone = snap.trend_direction === "BULLISH" ? "pos" : snap.trend_direction === "BEARISH" ? "neg" : "neutral";
  return (
    <div className="flex flex-col gap-1.5 rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-2.5 py-2">
      <span className="text-[10px] font-bold text-[var(--color-text-secondary)]">{label}</span>
      <StatusBadge tone={dirTone} size="sm" className="w-fit">
        {snap.regime}
      </StatusBadge>
      <span className="font-num text-[12px] font-semibold text-[var(--color-text-primary)]">
        {snap.confidence != null ? `${(snap.confidence * 100).toFixed(0)}%` : "—"}
      </span>
      <span className="text-[9.5px] text-[var(--color-text-muted)]">
        RSI {snap.rsi?.toFixed(1) ?? "—"} · ADX {snap.adx?.toFixed(1) ?? "—"}
      </span>
    </div>
  );
}
