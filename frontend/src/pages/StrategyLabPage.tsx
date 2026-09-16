import { useState } from "react";
import { api } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { Panel } from "../components/ui/Panel";
import { StatusBadge } from "../components/ui/StatusBadge";
import { MetricCard } from "../components/ui/MetricCard";
import { ErrorState, LoadingState } from "../components/ui/States";
import { fmtPct, fmtSigned } from "../lib/format";
import type { BacktestResponse } from "../lib/types";

export function StrategyLabPage() {
  const strategies = usePolling(api.strategies, 15000);
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<BacktestResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function runResearch(name: string) {
    setSelected(name);
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.runBacktest({ bars: 2000, warmup_bars: 1000, step: 4, strategy: name });
      setResult(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Research run failed");
    } finally {
      setBusy(false);
    }
  }

  if (strategies.loading && !strategies.data) return <LoadingState label="Loading strategy registry…" />;
  if (strategies.error && !strategies.data) return <ErrorState title="STRATEGY REGISTRY UNAVAILABLE" detail={strategies.error} />;

  return (
    <div className="flex flex-col gap-3">
      <Panel title="Strategies">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          {strategies.data!.strategies.map((s) => (
            <div key={s.name} className="flex flex-col gap-2 rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-3 py-2.5">
              <div className="flex items-center justify-between">
                <span className="text-[12px] font-semibold text-[var(--color-text-primary)]">{s.name.replace(/_/g, " ")}</span>
                <StatusBadge tone={s.enabled ? "pos" : "neutral"}>{s.enabled ? "ENABLED" : "DISABLED"}</StatusBadge>
              </div>
              <span className="text-[10px] text-[var(--color-text-muted)]">
                Lifecycle: not tracked by the backend — no RESEARCH/VALIDATION/LIVE state machine is implemented.
              </span>
              <button
                onClick={() => runResearch(s.name)}
                disabled={busy}
                className="rounded border border-[var(--color-gold)]/40 bg-[var(--color-gold-dim)]/15 px-2.5 py-1.5 text-[10.5px] font-semibold text-[var(--color-gold-bright)] hover:bg-[var(--color-gold-dim)]/25 disabled:opacity-50"
              >
                {busy && selected === s.name ? "Running…" : "Run Research Backtest"}
              </button>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="Regime → Strategy Eligibility">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries(strategies.data!.regime_strategy_map).map(([regime, names]) => (
            <div key={regime} className="flex flex-col gap-1.5 rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-3 py-2">
              <span className="text-[10.5px] font-semibold text-[var(--color-text-secondary)]">{regime}</span>
              <div className="flex flex-wrap gap-1">
                {names.length === 0 ? (
                  <span className="text-[10px] text-[var(--color-text-muted)]">No strategies eligible — NO TRADE</span>
                ) : (
                  names.map((n) => (
                    <StatusBadge key={n} tone="gold">{n.replace(/_/g, " ")}</StatusBadge>
                  ))
                )}
              </div>
            </div>
          ))}
        </div>
      </Panel>

      {busy && <LoadingState label={`Running research backtest for ${selected}…`} />}
      {error && <ErrorState title="RESEARCH RUN FAILED" detail={error} />}

      {result && !busy && (
        <Panel title={`Research Results — ${selected?.replace(/_/g, " ")}`}>
          <div className="mb-2 rounded border border-[var(--color-gold)]/30 bg-[var(--color-gold-dim)]/10 px-3 py-2 text-[10.5px] font-semibold uppercase tracking-wide text-[var(--color-gold-bright)]">
            Out-of-sample walk-forward and Monte Carlo modules are not implemented in this backend — this is a
            single historical backtest run scoped to this strategy only.
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <MetricCard label="Trades" value={result.trade_count} />
            <MetricCard label="Net Profit" value={`$${fmtSigned(result.net_profit)}`} tone={result.net_profit >= 0 ? "pos" : "neg"} />
            <MetricCard label="Win Rate" value={fmtPct(Number(result.metrics.win_rate ?? 0) * 100)} />
            <MetricCard label="Max Drawdown" value={result.metrics.max_drawdown_pct != null ? fmtPct(Number(result.metrics.max_drawdown_pct) * 100) : "—"} tone="neg" />
          </div>
        </Panel>
      )}
    </div>
  );
}
