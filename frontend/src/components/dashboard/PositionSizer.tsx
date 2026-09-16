import { Panel } from "../ui/Panel";
import { ErrorState, LoadingState } from "../ui/States";
import { fmtPrice } from "../../lib/format";
import type { AccountInfo, SignalResponse } from "../../lib/types";
import type { FetchState } from "../../lib/usePolling";

export function PositionSizer({
  signal,
  account,
}: {
  signal: FetchState<SignalResponse>;
  account: FetchState<AccountInfo>;
}) {
  const v = signal.data?.validation;
  const unavailable = !v || (v.entry != null && v.lots === 0 && !v.approved);

  return (
    <Panel title="Position Sizer">
      {signal.loading && !signal.data ? (
        <LoadingState />
      ) : signal.error && !signal.data ? (
        <ErrorState title="RISK CALCULATION UNAVAILABLE" detail="NO TRADE — backend signal endpoint unreachable." />
      ) : (
        <div className="grid grid-cols-2 gap-2">
          <Field label="Account Balance" value={account.data ? `$${fmtPrice(account.data.balance)}` : "—"} />
          <Field label="Risk %" value={v ? `${v.risk_percent.toFixed(2)}%` : "—"} />
          <Field
            label="Risk Amount"
            value={account.data && v ? `$${((account.data.equity * v.risk_percent) / 100).toFixed(2)}` : "—"}
          />
          <Field
            label="SL Distance"
            value={v && v.entry != null && v.stop_loss != null ? Math.abs(v.entry - v.stop_loss).toFixed(2) : "—"}
          />
          <Field
            label="Recommended Volume"
            value={v && v.lots > 0 ? `${v.lots.toFixed(2)} lots` : unavailable ? "—" : v ? `${v.lots.toFixed(2)} lots` : "—"}
            className="col-span-2"
            emphasize={!!v && v.lots > 0}
          />
          {v && v.lots === 0 && v.entry != null && (
            <div className="col-span-2 rounded border border-[var(--color-warn)]/30 bg-[var(--color-warn-dim)]/20 px-2.5 py-2 text-[10.5px] font-semibold uppercase tracking-wide text-[var(--color-warn)]">
              Risk calculation unavailable — no trade
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

function Field({
  label,
  value,
  className = "",
  emphasize = false,
}: {
  label: string;
  value: string;
  className?: string;
  emphasize?: boolean;
}) {
  return (
    <div className={`flex flex-col gap-0.5 rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-2.5 py-1.5 ${className}`}>
      <span className="text-[9.5px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">{label}</span>
      <span className={`font-num text-[13px] font-semibold ${emphasize ? "text-[var(--color-gold-bright)]" : "text-[var(--color-text-primary)]"}`}>
        {value}
      </span>
    </div>
  );
}
