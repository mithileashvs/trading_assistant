import { api } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { Panel } from "../components/ui/Panel";
import { StatusBadge } from "../components/ui/StatusBadge";
import { ErrorState, LoadingState } from "../components/ui/States";
import { fmtPrice } from "../lib/format";
import type { Signal } from "../lib/types";

const BREAKDOWN_LABELS: Record<string, string> = {
  h4_trend_alignment: "H4 alignment",
  h1_trend_alignment: "H1 alignment",
  m15_momentum: "M15 momentum",
  entry_confirmation: "Entry confirmation",
  setup_quality: "Setup quality",
  volatility_suitable: "Volatility suitable",
  market_context: "Market context",
};

export function SignalsPage() {
  const signal = usePolling(api.signal, 5000);

  if (signal.loading && !signal.data) {
    return <LoadingState label="Computing signal…" />;
  }
  if (signal.error && !signal.data) {
    return <ErrorState title="SIGNAL DATA UNAVAILABLE" detail={signal.error} />;
  }

  const data = signal.data!;
  const s = data.signal;
  const v = data.validation;
  const e = data.explanation;
  const dirTone = s.direction === "BUY" ? "pos" : s.direction === "SELL" ? "neg" : "neutral";

  return (
    <div className="flex flex-col gap-3">
      <Panel
        title="Current Signal"
        right={<StatusBadge tone={v.approved ? "pos" : "neg"}>{v.approved ? "APPROVED" : "NO TRADE"}</StatusBadge>}
      >
        <div className="flex flex-wrap items-center gap-4">
          <StatusBadge tone={dirTone} size="md" className="px-3 py-1.5 text-[15px]">
            {s.direction}
          </StatusBadge>
          <InfoBit label="Strategy" value={s.strategy.replace(/_/g, " ")} />
          <InfoBit label="Regime" value={v.regime} />
          <InfoBit label="Score" value={s.score != null ? `${s.score} / 10` : "—"} />
          <InfoBit label="Confidence" value={s.confidence != null ? `${(s.confidence * 100).toFixed(0)}%` : "—"} />
          <InfoBit label="Score Label" value={s.score_label ?? "—"} />
        </div>
      </Panel>

      {s.score_breakdown && (
        <Panel title="Scoring Breakdown">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
            {Object.entries(s.score_breakdown).map(([key, points]) => (
              <div key={key} className="flex items-center justify-between rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-2.5 py-1.5">
                <span className="text-[10.5px] text-[var(--color-text-secondary)]">{BREAKDOWN_LABELS[key] ?? key}</span>
                <span className={`font-num text-[12px] font-bold ${points > 0 ? "text-[var(--color-pos)]" : "text-[var(--color-text-muted)]"}`}>
                  +{points}
                </span>
              </div>
            ))}
          </div>
        </Panel>
      )}

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <Panel title="Setup">
          <div className="grid grid-cols-2 gap-2">
            <Field label="Entry" value={fmtPrice(s.entry)} />
            <Field label="Risk : Reward" value={s.risk_reward != null ? `1 : ${s.risk_reward.toFixed(2)}` : "—"} />
            <Field label="Stop Loss" value={fmtPrice(s.stop_loss)} tone="neg" />
            <Field label="Take Profit" value={fmtPrice(s.take_profit)} tone="pos" />
            <Field label="Risk %" value={`${v.risk_percent.toFixed(2)}%`} />
            <Field label="Lots" value={v.lots > 0 ? v.lots.toFixed(2) : "—"} />
          </div>
        </Panel>

        <Panel title="Why?">
          <div className="flex flex-col gap-2.5 text-[11px] leading-snug text-[var(--color-text-secondary)]">
            <Explain label="Why?" text={e.why} />
            <Explain label="Why now?" text={e.why_now} />
            <Explain label="Why this strategy?" text={e.why_this_strategy} />
            <Explain label="Why this entry?" text={e.why_this_entry} />
            <Explain label="Why this stop?" text={e.why_this_stop} />
            <Explain label="Why this target?" text={e.why_this_target} />
            <Explain label="How much risk?" text={e.how_much_risk} />
            <Explain label="What would invalidate it?" text={e.what_would_invalidate_it} />
          </div>
        </Panel>
      </div>

      <Panel title="Other Strategies">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          {data.all_signals.map((other, i) => (
            <StrategyCard key={i} signal={other} isBest={other.strategy === s.strategy && other.direction === s.direction} />
          ))}
        </div>
      </Panel>
    </div>
  );
}

function InfoBit({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[9.5px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">{label}</span>
      <span className="font-num text-[12px] font-semibold text-[var(--color-text-primary)]">{value}</span>
    </div>
  );
}

function Field({ label, value, tone }: { label: string; value: string; tone?: "pos" | "neg" }) {
  const color = tone === "pos" ? "text-[var(--color-pos)]" : tone === "neg" ? "text-[var(--color-neg)]" : "text-[var(--color-text-primary)]";
  return (
    <div className="flex flex-col gap-0.5 rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-2.5 py-1.5">
      <span className="text-[9.5px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">{label}</span>
      <span className={`font-num text-[13px] font-semibold ${color}`}>{value}</span>
    </div>
  );
}

function Explain({ label, text }: { label: string; text: string }) {
  if (!text) return null;
  return (
    <div>
      <span className="block text-[10px] font-bold uppercase tracking-wide text-[var(--color-gold-bright)]">{label}</span>
      <span>{text}</span>
    </div>
  );
}

function StrategyCard({ signal, isBest }: { signal: Signal; isBest: boolean }) {
  const dirTone = signal.direction === "BUY" ? "pos" : signal.direction === "SELL" ? "neg" : "neutral";
  return (
    <div className={`flex flex-col gap-1.5 rounded border px-3 py-2.5 ${isBest ? "border-[var(--color-gold)]/50 bg-[var(--color-gold-dim)]/10" : "border-[var(--color-border-soft)] bg-[var(--color-panel-alt)]"}`}>
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold text-[var(--color-text-primary)]">{signal.strategy.replace(/_/g, " ")}</span>
        {isBest && <StatusBadge tone="gold">Selected</StatusBadge>}
      </div>
      <StatusBadge tone={dirTone} className="w-fit">
        {signal.direction}
      </StatusBadge>
      <div className="flex items-center justify-between text-[10.5px] text-[var(--color-text-muted)]">
        <span>Score: {signal.score != null ? `${signal.score}/10` : "—"}</span>
        <span>{signal.score_label ?? "—"}</span>
      </div>
    </div>
  );
}
