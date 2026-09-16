import { api } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { useSystemStatus } from "../lib/SystemStatusContext";
import { Panel } from "../components/ui/Panel";
import { StatusBadge, type Tone } from "../components/ui/StatusBadge";
import { ErrorState, LoadingState } from "../components/ui/States";
import { KillSwitchControl } from "../components/safety/KillSwitchControl";

const CHECK_LABELS: Record<string, string> = {
  kill_switch_ok: "Kill Switch",
  mt5_connected: "MT5 Connection",
  broker_trade_allowed: "Broker Permissions",
  market_data_fresh: "Data Freshness",
  min_equity_ok: "Account State (Equity)",
  daily_loss_ok: "Risk Engine — Daily Loss",
  weekly_loss_ok: "Risk Engine — Weekly Loss",
  max_trades_per_day_ok: "Risk Engine — Trade Frequency",
  max_open_positions_ok: "Risk Engine — Position Count",
  max_consecutive_losses_ok: "Risk Engine — Consecutive Losses",
  max_spread_ok: "Safety Gate — Spread",
};

export function SafetyPage() {
  const risk = usePolling(api.risk, 4000);
  const signal = usePolling(api.signal, 5000);
  const system = useSystemStatus();

  if (risk.loading && !risk.data) return <LoadingState label="Loading safety state…" />;
  if (risk.error && !risk.data) return <ErrorState title="SAFETY DATA UNAVAILABLE" detail={risk.error} />;

  const r = risk.data!;
  // Fail-safe default: if the signal endpoint hasn't loaded yet, treat
  // news as UNAVAILABLE/not-ok rather than defaulting to "clear" — an
  // unknown state must never render as safe.
  const newsState = signal.data?.validation.news_state ?? "UNAVAILABLE";
  const newsOk = signal.data?.validation.news_ok ?? false;
  const dataIntegrityOk = system.data?.market_data_fresh ?? false;
  const allCriticalOk = r.checks_passed && dataIntegrityOk && newsOk;

  const checklist: Array<{ label: string; tone: Tone; text: string }> = [
    { label: "Data Integrity", tone: dataIntegrityOk ? "pos" : "neg", text: dataIntegrityOk ? "PASS" : "BLOCKED" },
    ...Object.entries(r.checks).map(([key, passed]) => ({
      label: CHECK_LABELS[key] ?? key,
      tone: (passed ? "pos" : "neg") as Tone,
      text: passed ? "PASS" : "BLOCKED",
    })),
    {
      label: "News State",
      // CLEAR is the only state that passes. BLOCKED is an active
      // blackout (neg); UNAVAILABLE/UNKNOWN are "we don't know" — still
      // blocking, shown as neutral so it reads as "not confirmed safe"
      // rather than being confused with an active blackout, but never
      // as "pos".
      tone: newsState === "CLEAR" ? "pos" : newsState === "BLOCKED" ? "neg" : "neutral",
      text: newsState,
    },
  ];

  return (
    <div className="flex flex-col gap-3">
      <div
        className={`flex items-center justify-between rounded-md border px-4 py-3 ${
          allCriticalOk
            ? "border-[var(--color-pos)]/40 bg-[var(--color-pos-dim)]/15"
            : "border-[var(--color-neg)]/40 bg-[var(--color-neg-dim)]/15"
        }`}
      >
        <div className="flex items-center gap-3">
          <span className={`h-2.5 w-2.5 rounded-full ${allCriticalOk ? "bg-[var(--color-pos)] pulse-dot" : "bg-[var(--color-neg)]"}`} />
          <span className="text-[13px] font-bold uppercase tracking-wide text-[var(--color-text-primary)]">
            System {allCriticalOk ? "Safe" : "Unsafe"}
          </span>
          <span className="text-[10.5px] text-[var(--color-text-muted)]">
            {allCriticalOk ? "All safety checks passed." : "One or more critical checks failed."}
          </span>
        </div>
        <StatusBadge tone={allCriticalOk ? "pos" : "neg"} size="md">
          Trading {allCriticalOk ? "Enabled" : "Blocked"}
        </StatusBadge>
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <Panel title="Safety Checks" className="lg:col-span-2">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {checklist.map((c) => (
              <div key={c.label} className="flex items-center justify-between rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-2.5 py-1.5">
                <span className="text-[10.5px] text-[var(--color-text-secondary)]">{c.label}</span>
                <StatusBadge tone={c.tone}>{c.text}</StatusBadge>
              </div>
            ))}
          </div>
        </Panel>

        <KillSwitchControl status={r.kill_switch_status} onChanged={() => risk.refresh()} />
      </div>

      <Panel title="Structural Safeguards">
        <p className="mb-2 text-[10.5px] leading-snug text-[var(--color-text-muted)]">
          These are enforced in the backend's code path itself, not measured as a live health check, so they're
          described here rather than shown as a PASS/BLOCKED badge — a badge would imply a runtime signal that
          doesn't actually exist.
        </p>
        <ul className="flex flex-col gap-1.5 text-[11px] text-[var(--color-text-secondary)]">
          <li>· Duplicate-order protection — the same client order ID can never be submitted twice.</li>
          <li>· The deterministic backend (risk engine → safety gate → execution) is the sole trading authority; this dashboard cannot bypass it.</li>
          <li>· PAPER/LIVE mode is fixed by backend configuration; this dashboard cannot switch it silently.</li>
        </ul>
      </Panel>
    </div>
  );
}
