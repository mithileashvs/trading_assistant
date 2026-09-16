import { api } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { Panel } from "../components/ui/Panel";
import { StatusBadge, type Tone } from "../components/ui/StatusBadge";
import { ErrorState, LoadingState } from "../components/ui/States";
import { fmtPrice } from "../lib/format";

export function ExecutionPage() {
  const signal = usePolling(api.signal, 5000);
  const journal = usePolling(() => api.journal("all", 15), 8000);

  if (signal.loading && !signal.data) return <LoadingState label="Loading execution pipeline…" />;
  if (signal.error && !signal.data) return <ErrorState title="EXECUTION PIPELINE UNAVAILABLE" detail={signal.error} />;

  const v = signal.data!.validation;
  const s = signal.data!.signal;
  const guardsAllPass = Object.values(v.guard_checks).every(Boolean);
  // v.news_ok already IS the correct CLEAR-only gate (see
  // TradeValidator.validate); news_state is only used here to pick the
  // right display label (BLOCKED vs UNAVAILABLE vs UNKNOWN) for the
  // non-passing cases.
  const newsStage: Tone = v.news_ok ? "pos" : v.news_state === "BLOCKED" ? "neg" : "neutral";
  const riskStage: Tone = guardsAllPass ? "pos" : "neg";
  const finalStage: Tone = v.approved ? "pos" : "neg";

  return (
    <div className="flex flex-col gap-3">
      <Panel title="Execution Pipeline">
        <div className="flex flex-col items-stretch gap-0">
          <Stage label="Signal" tone={s.direction === "NO_SIGNAL" ? "neutral" : s.direction === "BUY" ? "pos" : "neg"} value={s.direction} />
          <Arrow />
          <Stage label="Risk Engine" tone={riskStage} value={riskStage === "pos" ? "APPROVED" : "REJECTED"} />
          <Arrow />
          <Stage label="Safety Gate (News)" tone={newsStage} value={v.news_ok ? "APPROVED" : v.news_state} />
          <Arrow />
          <Stage label="Final" tone={finalStage} value={v.approved ? "APPROVED" : "NO TRADE"} emphasize />
          {v.approved && (
            <>
              <Arrow />
              <div className="rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-4 py-2.5 text-center text-[11px] text-[var(--color-text-secondary)]">
                An approved signal is eligible for submission by the trading loop / execution engine. This
                dashboard does not itself submit orders — see the Positions page for confirmed fills.
              </div>
            </>
          )}
        </div>

        {!v.approved && v.rejection_reasons.length > 0 && (
          <div className="mt-3 rounded border border-[var(--color-neg)]/30 bg-[var(--color-neg-dim)]/15 px-3 py-2">
            <span className="text-[10px] font-bold uppercase tracking-wide text-[var(--color-neg)]">Rejection reasons</span>
            <ul className="mt-1 flex flex-col gap-0.5">
              {v.rejection_reasons.map((r, i) => (
                <li key={i} className="text-[11px] text-[var(--color-text-secondary)]">· {r}</li>
              ))}
            </ul>
          </div>
        )}
      </Panel>

      <Panel title="Recent Execution Activity" noPadding>
        {journal.loading && !journal.data ? (
          <div className="p-3"><LoadingState /></div>
        ) : journal.error && !journal.data ? (
          <div className="p-3"><ErrorState title="UNAVAILABLE" detail={journal.error} /></div>
        ) : (journal.data?.entries.length ?? 0) === 0 ? (
          <div className="p-3 text-[11px] text-[var(--color-text-muted)]">No recent signal or trade activity recorded yet.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[11px]">
              <thead>
                <tr className="border-b border-[var(--color-border-soft)] text-[9.5px] uppercase tracking-wider text-[var(--color-text-muted)]">
                  <th className="px-3 py-2 font-semibold">Time</th>
                  <th className="px-3 py-2 font-semibold">Type</th>
                  <th className="px-3 py-2 font-semibold">Direction</th>
                  <th className="px-3 py-2 font-semibold">Strategy</th>
                  <th className="px-3 py-2 font-semibold">Entry</th>
                  <th className="px-3 py-2 font-semibold">Status</th>
                </tr>
              </thead>
              <tbody>
                {journal.data!.entries.map((e, i) => (
                  <tr key={i} className="border-b border-[var(--color-border-soft)]">
                    <td className="px-3 py-1.5 font-num text-[var(--color-text-muted)]">{e.timestamp ? new Date(e.timestamp).toLocaleString() : "—"}</td>
                    <td className="px-3 py-1.5 uppercase text-[var(--color-text-secondary)]">{e.record_type}</td>
                    <td className="px-3 py-1.5">
                      <StatusBadge tone={e.direction === "BUY" ? "pos" : e.direction === "SELL" ? "neg" : "neutral"}>{e.direction}</StatusBadge>
                    </td>
                    <td className="px-3 py-1.5 text-[var(--color-text-secondary)]">{e.strategy?.replace(/_/g, " ")}</td>
                    <td className="px-3 py-1.5 font-num">{fmtPrice(e.entry_price)}</td>
                    <td className="px-3 py-1.5">
                      <StatusBadge tone={e.status === "APPROVED" || e.status === "WIN" ? "pos" : e.status === "REJECTED" || e.status === "LOSS" ? "neg" : "neutral"}>
                        {e.status}
                      </StatusBadge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

function Stage({ label, tone, value, emphasize }: { label: string; tone: Tone; value: string; emphasize?: boolean }) {
  return (
    <div
      className={`flex items-center justify-between rounded border px-4 py-2.5 ${
        emphasize ? "border-2 border-[var(--color-border)] bg-[var(--color-bg-elevated)]" : "border-[var(--color-border-soft)] bg-[var(--color-panel-alt)]"
      }`}
    >
      <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--color-text-secondary)]">{label}</span>
      <StatusBadge tone={tone} size="md">{value}</StatusBadge>
    </div>
  );
}

function Arrow() {
  return (
    <div className="flex justify-center py-0.5">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-[var(--color-text-muted)]">
        <path d="M12 4v14M6 13l6 6 6-6" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  );
}
