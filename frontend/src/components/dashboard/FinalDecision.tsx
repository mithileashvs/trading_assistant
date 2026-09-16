import { Panel } from "../ui/Panel";
import { StatusBadge } from "../ui/StatusBadge";
import { ErrorState, LoadingState } from "../ui/States";
import type { SignalResponse } from "../../lib/types";
import type { FetchState } from "../../lib/usePolling";

const CHECK_LABELS: Record<string, string> = {
  spread_ok: "Spread",
  news_ok: "News Filter",
  daily_loss_limit_ok: "Daily Loss Limit",
  weekly_loss_limit_ok: "Weekly Loss Limit",
  position_limit_ok: "Open Position Limit",
  market_data_fresh: "Market Data Freshness",
  max_spread_ok: "Max Spread",
  daily_loss_ok: "Daily Loss",
  weekly_loss_ok: "Weekly Loss",
  max_open_positions_ok: "Max Open Positions",
  max_trades_per_day_ok: "Max Trades / Day",
  consecutive_losses_ok: "Consecutive Losses",
  min_equity_ok: "Minimum Equity",
  mt5_connected: "MT5 Connection",
  broker_trade_allowed: "Broker Trade Allowed",
};

export function FinalDecision({ signal }: { signal: FetchState<SignalResponse> }) {
  const v = signal.data?.validation;
  const s = signal.data?.signal;

  return (
    <Panel title="Final Decision" className="h-full">
      {signal.loading && !signal.data ? (
        <LoadingState />
      ) : signal.error && !signal.data ? (
        <ErrorState title="DECISION PIPELINE UNAVAILABLE" detail={signal.error} />
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">Signal</span>
            <StatusBadge tone={s?.direction === "BUY" ? "pos" : s?.direction === "SELL" ? "neg" : "neutral"}>
              {s?.direction ?? "—"}
            </StatusBadge>
          </div>

          <div className="flex flex-col gap-1.5 border-t border-[var(--color-border-soft)] pt-2.5">
            <span className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">
              Risk &amp; Safety Checks
            </span>
            {v ? (
              Object.entries(v.guard_checks).length > 0 ? (
                <div className="flex flex-col gap-1">
                  {Object.entries(v.guard_checks).map(([key, passed]) => (
                    <CheckRow key={key} label={CHECK_LABELS[key] ?? key} passed={passed} />
                  ))}
                  <CheckRow label="News Filter" passed={v.news_ok} note={v.news_state !== "CLEAR" ? v.news_state.toLowerCase() : undefined} />
                </div>
              ) : (
                <span className="text-[11px] text-[var(--color-text-muted)]">No guard checks returned.</span>
              )
            ) : (
              <span className="text-[11px] text-[var(--color-text-muted)]">—</span>
            )}
          </div>

          <div className="flex flex-col gap-2 rounded border-2 border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-3">
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-bold uppercase tracking-wider text-[var(--color-text-secondary)]">
                Final
              </span>
              <StatusBadge tone={v?.approved ? "pos" : "neg"} size="md">
                {v?.approved ? "APPROVED" : "NO TRADE"}
              </StatusBadge>
            </div>
            {v && !v.approved && v.rejection_reasons.length > 0 && (
              <div className="flex flex-col gap-1">
                <span className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">
                  Reason
                </span>
                <ul className="flex flex-col gap-0.5">
                  {v.rejection_reasons.map((r, i) => (
                    <li key={i} className="text-[10.5px] leading-snug text-[var(--color-neg)]">
                      · {r}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          <p className="text-[10px] leading-snug text-[var(--color-text-muted)]">
            All risk and safety authority lives in the backend. This panel only reflects the backend's own decision —
            it cannot approve, reject, or execute anything itself.
          </p>
        </div>
      )}
    </Panel>
  );
}

function CheckRow({ label, passed, note }: { label: string; passed: boolean; note?: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-[11px] text-[var(--color-text-secondary)]">{label}</span>
      <div className="flex items-center gap-1.5">
        {note && <span className="text-[9.5px] text-[var(--color-text-muted)]">{note}</span>}
        <StatusBadge tone={passed ? "pos" : "neg"}>{passed ? "PASS" : "FAIL"}</StatusBadge>
      </div>
    </div>
  );
}
