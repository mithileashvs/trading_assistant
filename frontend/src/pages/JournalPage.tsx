import { useState } from "react";
import { api } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { Panel } from "../components/ui/Panel";
import { StatusBadge } from "../components/ui/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../components/ui/States";
import { fmtPrice } from "../lib/format";
import type { JournalEntry } from "../lib/types";

const FILTERS = [
  { key: "all", label: "All" },
  { key: "signals", label: "Signals" },
  { key: "trades", label: "Trades" },
] as const;
type FilterKey = (typeof FILTERS)[number]["key"];

export function JournalPage() {
  const [filter, setFilter] = useState<FilterKey>("all");
  const [selected, setSelected] = useState<JournalEntry | null>(null);
  const journal = usePolling(() => api.journal(filter, 200), 6000);

  return (
    <div className="flex flex-col gap-3">
      <Panel
        title="Journal / History"
        right={
          <div className="flex overflow-hidden rounded border border-[var(--color-border)]">
            {FILTERS.map((f) => (
              <button
                key={f.key}
                onClick={() => setFilter(f.key)}
                className={`px-2.5 py-1 text-[10.5px] font-semibold transition-colors ${
                  f.key === filter
                    ? "bg-[var(--color-gold-dim)]/30 text-[var(--color-gold-bright)]"
                    : "bg-[var(--color-panel-alt)] text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]"
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>
        }
        noPadding
      >
        {journal.loading && !journal.data ? (
          <div className="p-3"><LoadingState /></div>
        ) : journal.error && !journal.data ? (
          <div className="p-3"><ErrorState title="JOURNAL UNAVAILABLE" detail={journal.error} /></div>
        ) : (journal.data?.entries.length ?? 0) === 0 ? (
          <div className="p-3"><EmptyState label="No journal entries yet." /></div>
        ) : (
          <div className="max-h-[560px] overflow-auto">
            <table className="w-full text-left text-[11px]">
              <thead className="sticky top-0 bg-[var(--color-panel)]">
                <tr className="border-b border-[var(--color-border-soft)] text-[9.5px] uppercase tracking-wider text-[var(--color-text-muted)]">
                  <th className="px-3 py-2 font-semibold">Timestamp</th>
                  <th className="px-3 py-2 font-semibold">Type</th>
                  <th className="px-3 py-2 font-semibold">Strategy</th>
                  <th className="px-3 py-2 font-semibold">Symbol</th>
                  <th className="px-3 py-2 font-semibold">Decision</th>
                  <th className="px-3 py-2 font-semibold">Status</th>
                </tr>
              </thead>
              <tbody>
                {journal.data!.entries.map((e, i) => (
                  <tr
                    key={i}
                    onClick={() => setSelected(e)}
                    className={`cursor-pointer border-b border-[var(--color-border-soft)] hover:bg-white/[0.03] ${selected === e ? "bg-[var(--color-gold-dim)]/10" : ""}`}
                  >
                    <td className="px-3 py-1.5 font-num text-[var(--color-text-muted)]">{e.timestamp ? new Date(e.timestamp).toLocaleString() : "—"}</td>
                    <td className="px-3 py-1.5 uppercase text-[var(--color-text-secondary)]">{e.record_type}</td>
                    <td className="px-3 py-1.5">{e.strategy?.replace(/_/g, " ")}</td>
                    <td className="px-3 py-1.5">{e.symbol}</td>
                    <td className="px-3 py-1.5">
                      <StatusBadge tone={e.direction === "BUY" ? "pos" : e.direction === "SELL" ? "neg" : "neutral"}>{e.direction}</StatusBadge>
                    </td>
                    <td className="px-3 py-1.5">
                      <StatusBadge tone={["APPROVED", "WIN"].includes(e.status) ? "pos" : ["REJECTED", "LOSS"].includes(e.status) ? "neg" : "neutral"}>
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

      {selected && <EntryDetail entry={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

function EntryDetail({ entry, onClose }: { entry: JournalEntry; onClose: () => void }) {
  return (
    <Panel
      title={`Record Detail — ${entry.record_type.toUpperCase()}`}
      right={<button onClick={onClose} className="text-[10px] uppercase tracking-wide text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)]">Close</button>}
    >
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Field label="Timestamp" value={entry.timestamp ? new Date(entry.timestamp).toLocaleString() : "—"} />
        <Field label="Symbol" value={entry.symbol} />
        <Field label="Strategy" value={entry.strategy?.replace(/_/g, " ") ?? "—"} />
        <Field label="Regime" value={entry.regime ?? "—"} />
        <Field label="Score" value={entry.score != null ? `${entry.score}/10` : "—"} />
        <Field label="Entry" value={fmtPrice(entry.entry_price)} />
        <Field label="Stop Loss" value={fmtPrice(entry.stop_loss)} tone="neg" />
        <Field label="Take Profit" value={fmtPrice(entry.take_profit)} tone="pos" />
        {entry.record_type === "trade" && (
          <>
            <Field label="Exit Price" value={fmtPrice(entry.exit_price)} />
            <Field label="Lots" value={entry.lots != null ? entry.lots.toFixed(2) : "—"} />
            <Field label="PnL" value={entry.pnl != null ? `$${entry.pnl.toFixed(2)}` : "—"} tone={entry.pnl != null ? (entry.pnl >= 0 ? "pos" : "neg") : undefined} />
            <Field label="R Multiple" value={entry.r_multiple != null ? `${entry.r_multiple.toFixed(2)}R` : "—"} />
            <Field label="Exit Reason" value={entry.exit_reason ?? "—"} />
          </>
        )}
      </div>
      {entry.record_type === "signal" && entry.rejection_reasons && entry.rejection_reasons.length > 0 && (
        <div className="mt-3 rounded border border-[var(--color-neg)]/30 bg-[var(--color-neg-dim)]/15 px-3 py-2">
          <span className="text-[10px] font-bold uppercase tracking-wide text-[var(--color-neg)]">Why rejected</span>
          <ul className="mt-1 flex flex-col gap-0.5">
            {entry.rejection_reasons.map((r, i) => (
              <li key={i} className="text-[11px] text-[var(--color-text-secondary)]">· {r}</li>
            ))}
          </ul>
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
