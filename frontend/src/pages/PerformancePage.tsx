import { api } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { Panel } from "../components/ui/Panel";
import { MetricCard } from "../components/ui/MetricCard";
import { ErrorState, LoadingState } from "../components/ui/States";
import { fmtPct, fmtSigned } from "../lib/format";

export function PerformancePage() {
  const perf = usePolling(api.performance, 10000);

  if (perf.loading && !perf.data) return <LoadingState label="Loading performance…" />;
  if (perf.error && !perf.data) return <ErrorState title="PERFORMANCE DATA UNAVAILABLE" detail={perf.error} />;

  const p = perf.data!;
  const noTrades = p.number_of_trades === 0;

  return (
    <div className="flex flex-col gap-3">
      {noTrades && (
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-panel-alt)] px-4 py-3 text-[11px] text-[var(--color-text-secondary)]">
          {p.note ?? "No closed trades yet — metrics will populate as trades close."}
        </div>
      )}

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
        <MetricCard label="Net Profit" value={`$${fmtSigned(p.net_profit)}`} tone={p.net_profit >= 0 ? "pos" : "neg"} />
        <MetricCard label="Win Rate" value={fmtPct(p.win_rate * 100)} />
        <MetricCard label="Profit Factor" value={p.profit_factor != null ? p.profit_factor.toFixed(2) : "—"} />
        <MetricCard label="Expectancy" value={`$${fmtSigned(p.expectancy)}`} tone={p.expectancy >= 0 ? "pos" : "neg"} />
        <MetricCard label="Max Drawdown" value={`$${p.max_drawdown_abs.toFixed(2)}`} tone="neg" />
        <MetricCard label="Max DD %" value={fmtPct(p.max_drawdown_pct * 100)} tone="neg" />
        <MetricCard label="Trades" value={p.number_of_trades} />
        <MetricCard label="Balance" value={p.current_balance != null ? `$${p.current_balance.toFixed(2)}` : "—"} />
      </div>

      <Panel title="Equity Curve">
        {!p.equity_curve || p.equity_curve.length < 2 ? (
          <div className="py-8 text-center text-[11px] text-[var(--color-text-muted)]">Not enough closed trades to plot an equity curve yet.</div>
        ) : (
          <EquitySparkline points={p.equity_curve.map((e) => e.equity)} />
        )}
      </Panel>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <Panel title="By Strategy">
          {p.net_pnl_by_strategy && Object.keys(p.net_pnl_by_strategy).length > 0 ? (
            <div className="flex flex-col gap-1.5">
              {Object.entries(p.net_pnl_by_strategy).map(([name, pnl]) => (
                <BreakdownRow key={name} label={name.replace(/_/g, " ")} pnl={pnl} />
              ))}
            </div>
          ) : (
            <div className="py-4 text-center text-[11px] text-[var(--color-text-muted)]">No closed trades yet.</div>
          )}
        </Panel>

        <Panel title="By Regime">
          {p.net_pnl_by_regime && Object.keys(p.net_pnl_by_regime).length > 0 ? (
            <div className="flex flex-col gap-1.5">
              {Object.entries(p.net_pnl_by_regime).map(([name, pnl]) => (
                <BreakdownRow key={name} label={name.replace(/_/g, " ")} pnl={pnl} />
              ))}
            </div>
          ) : (
            <div className="py-4 text-center text-[11px] text-[var(--color-text-muted)]">No closed trades yet.</div>
          )}
        </Panel>
      </div>
    </div>
  );
}

function BreakdownRow({ label, pnl }: { label: string; pnl: number }) {
  return (
    <div className="flex items-center justify-between rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-2.5 py-1.5">
      <span className="text-[10.5px] text-[var(--color-text-secondary)]">{label}</span>
      <span className={`font-num text-[12px] font-semibold ${pnl >= 0 ? "text-[var(--color-pos)]" : "text-[var(--color-neg)]"}`}>
        {fmtSigned(pnl)}
      </span>
    </div>
  );
}

function EquitySparkline({ points }: { points: number[] }) {
  const w = 800;
  const h = 160;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const stepX = w / (points.length - 1);
  const path = points.map((v, i) => `${i === 0 ? "M" : "L"} ${(i * stepX).toFixed(1)} ${(h - ((v - min) / range) * h).toFixed(1)}`).join(" ");
  const up = points[points.length - 1] >= points[0];

  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full" style={{ height: 160 }} preserveAspectRatio="none">
      <path d={path} fill="none" stroke={up ? "var(--color-pos)" : "var(--color-neg)"} strokeWidth="2" />
    </svg>
  );
}
