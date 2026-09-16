import { useState } from "react";
import { api } from "../lib/api";
import { Panel } from "../components/ui/Panel";
import { MetricCard } from "../components/ui/MetricCard";
import { ErrorState, LoadingState } from "../components/ui/States";
import { StatusBadge } from "../components/ui/StatusBadge";
import { fmtPct, fmtSigned } from "../lib/format";
import type { BacktestResponse } from "../lib/types";

export function BacktestPage() {
  const [bars, setBars] = useState(2000);
  const [step, setStep] = useState(4);
  const [startingBalance, setStartingBalance] = useState(10000);
  const [strategy, setStrategy] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestResponse | null>(null);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.runBacktest({
        bars, step, starting_balance: startingBalance,
        strategy: strategy || undefined,
        warmup_bars: Math.min(1600, Math.max(300, bars - 200)),
      });
      setResult(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Backtest failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <Panel title="Backtest — Trend Pullback / Breakout / Mean Reversion">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-5">
          <NumField label="M15 Bars" value={bars} onChange={setBars} min={500} max={4000} step={100} />
          <NumField label="Step" value={step} onChange={setStep} min={1} max={96} step={1} />
          <NumField label="Starting Capital" value={startingBalance} onChange={setStartingBalance} min={1000} max={1000000} step={1000} />
          <div className="flex flex-col gap-1">
            <label className="text-[9.5px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">Strategy</label>
            <select
              value={strategy}
              onChange={(e) => setStrategy(e.target.value)}
              className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-1.5 text-[11px] text-[var(--color-text-primary)]"
            >
              <option value="">All strategies</option>
              <option value="TREND_PULLBACK">Trend Pullback</option>
              <option value="BREAKOUT">Breakout</option>
              <option value="MEAN_REVERSION">Mean Reversion</option>
            </select>
          </div>
          <div className="flex items-end">
            <button
              onClick={run}
              disabled={busy}
              className="w-full rounded bg-[var(--color-gold)] px-3 py-2 text-[11px] font-bold uppercase tracking-wide text-black hover:opacity-90 disabled:opacity-50"
            >
              {busy ? "Running…" : "Run Backtest"}
            </button>
          </div>
        </div>
        <p className="mt-2.5 text-[10px] leading-snug text-[var(--color-text-muted)]">
          Historical simulation only — this walks forward bar-by-bar with no look-ahead bias, using the exact same
          feature engine, regime detector, strategy selector, and trade validator as live/paper trading. It is not
          connected to live or paper execution.
        </p>
      </Panel>

      {busy && <LoadingState label="Running backtest — walking forward bar-by-bar…" />}
      {error && <ErrorState title="BACKTEST FAILED" detail={error} />}

      {result && !busy && (
        <>
          <div className="rounded border border-[var(--color-gold)]/30 bg-[var(--color-gold-dim)]/10 px-3 py-2 text-[10.5px] font-semibold uppercase tracking-wide text-[var(--color-gold-bright)]">
            Historical simulation · {result.bars_used} M15 bars · {result.strategy_filter ? result.strategy_filter.replace(/_/g, " ") : "all strategies"}
          </div>

          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
            <MetricCard label="Net Profit" value={`$${fmtSigned(result.net_profit)}`} tone={result.net_profit >= 0 ? "pos" : "neg"} />
            <MetricCard label="Trades" value={result.trade_count} />
            <MetricCard label="Win Rate" value={fmtPct(Number(result.metrics.win_rate ?? 0) * 100)} />
            <MetricCard label="Profit Factor" value={result.metrics.profit_factor != null ? Number(result.metrics.profit_factor).toFixed(2) : "—"} />
            <MetricCard label="Sharpe" value={result.metrics.sharpe != null ? Number(result.metrics.sharpe).toFixed(2) : "—"} />
            <MetricCard label="Sortino" value={result.metrics.sortino != null ? Number(result.metrics.sortino).toFixed(2) : "—"} />
            <MetricCard label="Max Drawdown" value={result.metrics.max_drawdown_pct != null ? fmtPct(Number(result.metrics.max_drawdown_pct) * 100) : "—"} tone="neg" />
            <MetricCard label="Ending Balance" value={`$${result.ending_balance.toFixed(2)}`} />
          </div>

          <Panel title="Equity Curve">
            {result.equity_curve.length < 2 ? (
              <div className="py-6 text-center text-[11px] text-[var(--color-text-muted)]">No trades were executed in this run.</div>
            ) : (
              <EquitySparkline points={result.equity_curve.map((e) => e.equity)} />
            )}
          </Panel>

          <Panel title={`Trades (${result.trades.length})`} noPadding>
            {result.trades.length === 0 ? (
              <div className="p-3 text-[11px] text-[var(--color-text-muted)]">No trades in this run.</div>
            ) : (
              <div className="max-h-[400px] overflow-auto">
                <table className="w-full text-left text-[11px]">
                  <thead className="sticky top-0 bg-[var(--color-panel)]">
                    <tr className="border-b border-[var(--color-border-soft)] text-[9.5px] uppercase tracking-wider text-[var(--color-text-muted)]">
                      <th className="px-3 py-2 font-semibold">Open</th>
                      <th className="px-3 py-2 font-semibold">Direction</th>
                      <th className="px-3 py-2 font-semibold">Strategy</th>
                      <th className="px-3 py-2 font-semibold">Entry</th>
                      <th className="px-3 py-2 font-semibold">Exit</th>
                      <th className="px-3 py-2 font-semibold">PnL</th>
                      <th className="px-3 py-2 font-semibold">R</th>
                      <th className="px-3 py-2 font-semibold">Exit Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.trades.map((t, i) => (
                      <tr key={i} className="border-b border-[var(--color-border-soft)]">
                        <td className="px-3 py-1.5 font-num text-[var(--color-text-muted)]">{new Date(t.open_time).toLocaleString()}</td>
                        <td className="px-3 py-1.5"><StatusBadge tone={t.direction === "BUY" ? "pos" : "neg"}>{t.direction}</StatusBadge></td>
                        <td className="px-3 py-1.5">{t.strategy.replace(/_/g, " ")}</td>
                        <td className="px-3 py-1.5 font-num">{t.entry_price.toFixed(2)}</td>
                        <td className="px-3 py-1.5 font-num">{t.exit_price.toFixed(2)}</td>
                        <td className={`px-3 py-1.5 font-num ${t.pnl >= 0 ? "text-[var(--color-pos)]" : "text-[var(--color-neg)]"}`}>{fmtSigned(t.pnl)}</td>
                        <td className="px-3 py-1.5 font-num">{t.r_multiple != null ? `${t.r_multiple.toFixed(2)}R` : "—"}</td>
                        <td className="px-3 py-1.5 text-[var(--color-text-muted)]">{t.exit_reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}

function NumField({ label, value, onChange, min, max, step }: { label: string; value: number; onChange: (v: number) => void; min: number; max: number; step: number }) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-[9.5px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">{label}</label>
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => onChange(Number(e.target.value))}
        className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-1.5 font-num text-[11px] text-[var(--color-text-primary)]"
      />
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
