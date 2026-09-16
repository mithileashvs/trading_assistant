import { api } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { Panel } from "../components/ui/Panel";
import { MetricCard } from "../components/ui/MetricCard";
import { StatusBadge } from "../components/ui/StatusBadge";
import { ErrorState, LoadingState } from "../components/ui/States";
import { fmtInt, fmtPrice, fmtSigned } from "../lib/format";

const CHECK_LABELS: Record<string, string> = {
  kill_switch_ok: "Kill Switch",
  mt5_connected: "MT5 Connection",
  broker_trade_allowed: "Broker Trade Permissions",
  market_data_fresh: "Market Data Freshness",
  min_equity_ok: "Minimum Equity",
  daily_loss_ok: "Daily Loss Limit",
  weekly_loss_ok: "Weekly Loss Limit",
  max_trades_per_day_ok: "Max Trades / Day",
  max_open_positions_ok: "Max Open Positions",
  max_consecutive_losses_ok: "Consecutive Losses",
  max_spread_ok: "Max Spread",
};

export function RiskPage() {
  const risk = usePolling(api.risk, 4000);

  if (risk.loading && !risk.data) return <LoadingState label="Loading risk state…" />;
  if (risk.error && !risk.data) return <ErrorState title="RISK DATA UNAVAILABLE" detail={risk.error} />;

  const r = risk.data!;
  const dailyProgress = Math.min(100, (r.daily_loss_pct / r.limits.max_daily_loss_pct) * 100);
  const weeklyProgress = Math.min(100, (r.weekly_loss_pct / r.limits.max_weekly_loss_pct) * 100);

  return (
    <div className="flex flex-col gap-3">
      {!r.checks_passed && (
        <div className="rounded border border-[var(--color-neg)]/40 bg-[var(--color-neg-dim)]/20 px-4 py-3">
          <span className="text-[12px] font-bold uppercase tracking-wide text-[var(--color-neg)]">
            New entries blocked
          </span>
          <ul className="mt-1 flex flex-col gap-0.5">
            {r.reasons.map((reason, i) => (
              <li key={i} className="text-[11px] text-[var(--color-text-secondary)]">· {reason}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <MetricCard label="Account Balance" value={`$${fmtPrice(r.account_balance)}`} />
        <MetricCard label="Equity" value={`$${fmtPrice(r.equity)}`} />
        <MetricCard label="Free Margin" value={`$${fmtPrice(r.margin_free)}`} />
        <MetricCard label="Margin Used" value={`${(((r.equity - r.margin_free) / r.equity) * 100).toFixed(1)}%`} />
        <MetricCard label="Daily P&L" value={`$${fmtSigned(r.daily_pnl)}`} tone={r.daily_pnl >= 0 ? "pos" : "neg"} />
        <MetricCard label="Weekly P&L" value={`$${fmtSigned(r.weekly_pnl)}`} tone={r.weekly_pnl >= 0 ? "pos" : "neg"} />
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <Panel title="Risk Settings">
          <div className="grid grid-cols-2 gap-2">
            <Setting label="Risk per Trade" value={`${r.limits.risk_per_trade_pct}%`} />
            <Setting label="Max Daily Loss" value={`${r.limits.max_daily_loss_pct}%`} />
            <Setting label="Max Weekly Loss" value={`${r.limits.max_weekly_loss_pct}%`} />
            <Setting label="Max Open Positions" value={fmtInt(r.limits.max_open_positions)} />
            <Setting label="Max Trades / Day" value={fmtInt(r.limits.max_trades_per_day)} />
            <Setting label="Max Consecutive Losses" value={fmtInt(r.limits.max_consecutive_losses)} />
            <Setting label="Max Spread (pts)" value={fmtInt(r.limits.max_spread_points)} />
            <Setting label="Max Slippage (pts)" value={fmtInt(r.limits.max_slippage_points)} />
            <Setting label="Min Equity" value={r.limits.min_account_equity > 0 ? `$${fmtPrice(r.limits.min_account_equity)}` : "No minimum"} />
          </div>
          <p className="mt-2.5 text-[10px] leading-snug text-[var(--color-text-muted)]">
            These values are read from the backend's risk configuration. Changing them requires a backend
            configuration change and validation — this dashboard cannot edit them directly.
          </p>
        </Panel>

        <Panel title="Daily Loss Progress">
          <ProgressRing pct={dailyProgress} label={`${r.daily_loss_pct.toFixed(2)}%`} sub={`of ${r.limits.max_daily_loss_pct}% max`} danger={dailyProgress >= 100} />
          <div className="mt-3 flex items-center justify-between text-[11px]">
            <span className="text-[var(--color-text-muted)]">Used</span>
            <span className="font-num font-semibold text-[var(--color-text-primary)]">${Math.abs(Math.min(0, r.daily_pnl)).toFixed(2)}</span>
          </div>
          <div className="flex items-center justify-between text-[11px]">
            <span className="text-[var(--color-text-muted)]">Status</span>
            <StatusBadge tone={r.checks.daily_loss_ok ? "pos" : "neg"}>{r.checks.daily_loss_ok ? "NORMAL" : "LOCK ACTIVE"}</StatusBadge>
          </div>
        </Panel>

        <Panel title="Weekly Loss Progress">
          <ProgressRing pct={weeklyProgress} label={`${r.weekly_loss_pct.toFixed(2)}%`} sub={`of ${r.limits.max_weekly_loss_pct}% max`} danger={weeklyProgress >= 100} />
          <div className="mt-3 flex items-center justify-between text-[11px]">
            <span className="text-[var(--color-text-muted)]">Used</span>
            <span className="font-num font-semibold text-[var(--color-text-primary)]">${Math.abs(Math.min(0, r.weekly_pnl)).toFixed(2)}</span>
          </div>
          <div className="flex items-center justify-between text-[11px]">
            <span className="text-[var(--color-text-muted)]">Status</span>
            <StatusBadge tone={r.checks.weekly_loss_ok ? "pos" : "neg"}>{r.checks.weekly_loss_ok ? "NORMAL" : "LOCK ACTIVE"}</StatusBadge>
          </div>
        </Panel>
      </div>

      <Panel title="Risk Limits Status">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
          {Object.entries(r.checks).map(([key, passed]) => (
            <div key={key} className="flex items-center justify-between rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-2.5 py-1.5">
              <span className="text-[10.5px] text-[var(--color-text-secondary)]">{CHECK_LABELS[key] ?? key}</span>
              <StatusBadge tone={passed ? "pos" : "neg"}>{passed ? "OK" : "BLOCKED"}</StatusBadge>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="Total Exposure">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <MetricCard label="Open Positions" value={`${r.open_positions} / ${r.limits.max_open_positions}`} />
          <MetricCard label="Trades Today" value={`${r.trades_today} / ${r.limits.max_trades_per_day}`} />
          <MetricCard label="Consecutive Losses" value={`${r.consecutive_losses} / ${r.limits.max_consecutive_losses}`} tone={r.consecutive_losses > 0 ? "warn" : "neutral"} />
          <MetricCard label="Current Spread" value={`${r.current_spread_points.toFixed(1)} pts`} tone={r.checks.max_spread_ok ? "neutral" : "neg"} />
        </div>
      </Panel>
    </div>
  );
}

function Setting({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5 rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-2.5 py-1.5">
      <span className="text-[9.5px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">{label}</span>
      <span className="font-num text-[13px] font-semibold text-[var(--color-text-primary)]">{value}</span>
    </div>
  );
}

function ProgressRing({ pct, label, sub, danger }: { pct: number; label: string; sub: string; danger: boolean }) {
  const clamped = Math.max(0, Math.min(100, pct));
  const radius = 42;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (clamped / 100) * circumference;
  const color = danger ? "var(--color-neg)" : clamped > 70 ? "var(--color-warn)" : "var(--color-pos)";

  return (
    <div className="flex flex-col items-center gap-1">
      <svg width="110" height="110" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r={radius} fill="none" stroke="var(--color-border-soft)" strokeWidth="8" />
        <circle
          cx="50"
          cy="50"
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth="8"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          transform="rotate(-90 50 50)"
        />
        <text x="50" y="48" textAnchor="middle" className="font-num" fontSize="15" fontWeight="700" fill="var(--color-text-primary)">
          {label}
        </text>
        <text x="50" y="64" textAnchor="middle" fontSize="7" fill="var(--color-text-muted)">
          {sub}
        </text>
      </svg>
    </div>
  );
}
