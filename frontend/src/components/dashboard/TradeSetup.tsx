import { Panel } from "../ui/Panel";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { fmtPrice } from "../../lib/format";
import type { SignalResponse } from "../../lib/types";
import type { FetchState } from "../../lib/usePolling";

export function TradeSetup({ signal }: { signal: FetchState<SignalResponse> }) {
  const s = signal.data?.signal;
  const hasSetup = s && s.direction !== "NO_SIGNAL" && s.entry != null;

  return (
    <Panel title="Trade Setup">
      {signal.loading && !signal.data ? (
        <LoadingState />
      ) : signal.error && !signal.data ? (
        <ErrorState title="UNAVAILABLE" detail={signal.error} />
      ) : !hasSetup ? (
        <EmptyState label="No actionable setup — NO_SIGNAL from backend." />
      ) : (
        <div className="grid grid-cols-2 gap-2">
          <Field label="Entry" value={fmtPrice(s.entry)} />
          <Field label="Risk : Reward" value={s.risk_reward != null ? `1 : ${s.risk_reward.toFixed(2)}` : "—"} />
          <Field label="Stop Loss" value={fmtPrice(s.stop_loss)} tone="neg" />
          <Field label="Take Profit" value={fmtPrice(s.take_profit)} tone="pos" />
          <Field label="Strategy" value={s.strategy.replace(/_/g, " ")} />
          <Field label="Signal Score" value={s.score != null ? `${s.score} / 10` : "—"} />
        </div>
      )}
    </Panel>
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
