import { Panel } from "../ui/Panel";
import { StatusBadge } from "../ui/StatusBadge";
import { LoadingState, ErrorState } from "../ui/States";
import { fmtPrice, fmtAgo, fmtTimeUtc } from "../../lib/format";
import type { MarketSnapshot, SystemStatus } from "../../lib/types";
import type { FetchState } from "../../lib/usePolling";

export function MarketCard({
  market,
  system,
}: {
  market: FetchState<MarketSnapshot>;
  system: SystemStatus | null;
}) {
  const modeLabel = system
    ? system.mt5_use_mock
      ? "MOCK"
      : system.trading_mode === "LIVE"
        ? "LIVE"
        : system.trading_mode
    : "…";

  return (
    <Panel
      title="XAU/USD — Gold Spot"
      right={
        <div className="flex items-center gap-2">
          <StatusBadge tone={system?.trading_mode === "LIVE" && !system.mt5_use_mock ? "neg" : "neutral"} dot={!market.error}>
            {modeLabel}
          </StatusBadge>
        </div>
      }
    >
      {market.loading && !market.data ? (
        <LoadingState label="Loading market data…" />
      ) : market.error && !market.data ? (
        <ErrorState title="MARKET DATA UNAVAILABLE" detail={market.error} />
      ) : (
        <div className="flex flex-col gap-3">
          {market.error && (
            <ErrorState title="DATA STALE — LAST KNOWN VALUES SHOWN" detail={market.error} />
          )}
          <div className="flex items-baseline gap-3">
            <span className="font-num text-3xl font-bold text-[var(--color-text-primary)]">
              ${fmtPrice(market.data?.bid)}
            </span>
            <StatusBadge tone="pos" dot={!market.error}>
              {market.error ? "STALE" : "LIVE"}
            </StatusBadge>
          </div>

          <div className="grid grid-cols-4 gap-2">
            <Stat label="Bid" value={fmtPrice(market.data?.bid)} />
            <Stat label="Ask" value={fmtPrice(market.data?.ask)} />
            <Stat label="Spread" value={market.data ? market.data.spread.toFixed(2) : "—"} />
            <Stat label="ATR%" value={market.data?.atr_pct != null ? `${market.data.atr_pct.toFixed(2)}%` : "—"} />
          </div>

          <div className="flex items-center justify-between border-t border-[var(--color-border-soft)] pt-2 text-[10.5px] text-[var(--color-text-muted)]">
            <span>Last updated: {fmtTimeUtc(market.lastUpdated ? new Date(market.lastUpdated).toISOString() : null)}</span>
            <span className="font-num">{fmtAgo(market.lastUpdated)}</span>
          </div>
        </div>
      )}
    </Panel>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5 rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-2 py-1.5">
      <span className="text-[9.5px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">{label}</span>
      <span className="font-num text-[13px] font-semibold text-[var(--color-text-primary)]">{value}</span>
    </div>
  );
}
