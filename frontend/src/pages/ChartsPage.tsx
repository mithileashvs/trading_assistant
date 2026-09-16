import { useState } from "react";
import { api } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { Panel } from "../components/ui/Panel";
import { ErrorState, LoadingState } from "../components/ui/States";
import { CandleChart } from "../components/charts/CandleChart";
import { OscillatorChart } from "../components/charts/OscillatorChart";
import { fmtAgo, fmtTimeUtc } from "../lib/format";

const TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4", "D1"] as const;
type Tf = (typeof TIMEFRAMES)[number];

const STALE_AFTER_MS = 120_000;

export function ChartsPage() {
  const [tf, setTf] = useState<Tf>("H1");
  const [showEma, setShowEma] = useState(true);
  const [showBollinger, setShowBollinger] = useState(false);

  const candles = usePolling(() => api.candles(tf, 300), 15000);

  const data = candles.data;
  const ageMs = candles.lastUpdated ? Date.now() - candles.lastUpdated : null;
  const isStale = ageMs !== null && ageMs > STALE_AFTER_MS;
  const times = data?.candles.map((c) => c.time) ?? [];

  return (
    <div className="flex flex-col gap-3">
      <Panel
        title={data ? `${data.symbol} — ${data.timeframe}` : "XAU/USD Chart"}
        right={
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1.5 text-[10.5px] text-[var(--color-text-secondary)]">
              <input type="checkbox" checked={showEma} onChange={(e) => setShowEma(e.target.checked)} />
              EMA 20/50/200
            </label>
            <label className="flex items-center gap-1.5 text-[10.5px] text-[var(--color-text-secondary)]">
              <input type="checkbox" checked={showBollinger} onChange={(e) => setShowBollinger(e.target.checked)} />
              Bollinger
            </label>
            <div className="flex overflow-hidden rounded border border-[var(--color-border)]">
              {TIMEFRAMES.map((t) => (
                <button
                  key={t}
                  onClick={() => setTf(t)}
                  className={`px-2.5 py-1 text-[10.5px] font-semibold transition-colors ${
                    t === tf
                      ? "bg-[var(--color-gold-dim)]/30 text-[var(--color-gold-bright)]"
                      : "bg-[var(--color-panel-alt)] text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]"
                  }`}
                >
                  {t}
                </button>
              ))}
            </div>
          </div>
        }
      >
        {candles.loading && !data ? (
          <LoadingState label="Loading chart data…" />
        ) : candles.error && !data ? (
          <ErrorState title="DATA UNAVAILABLE" detail={candles.error} />
        ) : (
          <div className="flex flex-col gap-1">
            {(candles.error || isStale) && (
              <div className="mb-1 rounded border border-[var(--color-warn)]/30 bg-[var(--color-warn-dim)]/20 px-2.5 py-1.5 text-[10.5px] font-semibold uppercase tracking-wide text-[var(--color-warn)]">
                {candles.error ? `Data stale — ${candles.error}` : "Data stale — trading may be blocked"}
              </div>
            )}
            {data && <CandleChart data={data.candles} overlays={data.overlays} showEma={showEma} showBollinger={showBollinger} />}
            <div className="flex items-center justify-between pt-1 text-[10px] text-[var(--color-text-muted)]">
              <span>Last updated: {fmtTimeUtc(candles.lastUpdated ? new Date(candles.lastUpdated).toISOString() : null)}</span>
              <span className="font-num">{fmtAgo(candles.lastUpdated)}</span>
            </div>
          </div>
        )}
      </Panel>

      {data && (
        <>
          <Panel title="RSI (14)">
            <OscillatorChart
              times={times}
              lines={[{ values: data.oscillators.rsi_14, color: "#c9a227" }]}
              refLines={[30, 70]}
            />
          </Panel>
          <Panel title="MACD (12, 26, 9)">
            <div className="flex items-center gap-3 pb-1.5 text-[10px] text-[var(--color-text-muted)]">
              <Legend color="#60a5fa" label="MACD" />
              <Legend color="#e0bb3d" label="Signal" />
              <Legend color="#5b6478" label="Histogram" />
            </div>
            <OscillatorChart
              times={times}
              lines={[
                { values: data.oscillators.macd_histogram, color: "#5b6478", kind: "histogram" },
                { values: data.oscillators.macd, color: "#60a5fa" },
                { values: data.oscillators.macd_signal, color: "#e0bb3d" },
              ]}
            />
          </Panel>
          <Panel title="ADX (14)">
            <OscillatorChart times={times} lines={[{ values: data.oscillators.adx_14, color: "#22c55e" }]} refLines={[20]} />
          </Panel>
        </>
      )}
    </div>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1">
      <span className="inline-block h-1.5 w-3 rounded-sm" style={{ backgroundColor: color }} />
      {label}
    </span>
  );
}
