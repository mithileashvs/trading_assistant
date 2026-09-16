import { MetricCard } from "../ui/MetricCard";
import { fmtPrice, fmtSigned } from "../../lib/format";
import type { AccountInfo, PositionsResponse, SystemStatus } from "../../lib/types";
import type { FetchState } from "../../lib/usePolling";

export function AccountStrip({
  account,
  positions,
  system,
}: {
  account: FetchState<AccountInfo>;
  positions: FetchState<PositionsResponse>;
  system: SystemStatus | null;
}) {
  const a = account.data;
  const critical = system ? !system.market_data_fresh || !system.mt5_connected : false;

  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
      <MetricCard label="Account Balance" value={a ? `$${fmtPrice(a.balance)}` : "—"} />
      <MetricCard
        label="Equity"
        value={a ? `$${fmtPrice(a.equity)}` : "—"}
        sub={a ? `${fmtSigned(a.equity - a.balance)} unrealized` : undefined}
        tone={a && a.equity - a.balance < 0 ? "neg" : a && a.equity - a.balance > 0 ? "pos" : "neutral"}
      />
      <MetricCard label="Open Positions" value={positions.data ? positions.data.positions.length : "—"} />
      <MetricCard
        label="Daily P&L"
        value={a ? `$${fmtSigned(a.daily_pnl)}` : "—"}
        tone={a ? (a.daily_pnl > 0 ? "pos" : a.daily_pnl < 0 ? "neg" : "neutral") : "neutral"}
      />
      <MetricCard label="Margin Free" value={a ? `$${fmtPrice(a.margin_free)}` : "—"} />
      <MetricCard
        label="System Health"
        value={!system ? "—" : critical ? "DEGRADED" : "100%"}
        tone={!system ? "neutral" : critical ? "neg" : "pos"}
      />
    </div>
  );
}
