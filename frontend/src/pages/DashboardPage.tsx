import { api } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { useSystemStatus } from "../lib/SystemStatusContext";
import { AccountStrip } from "../components/dashboard/AccountStrip";
import { MarketCard } from "../components/dashboard/MarketCard";
import { CompositeSignalCard } from "../components/dashboard/CompositeSignalCard";
import { TimeframeAnalysis } from "../components/dashboard/TimeframeAnalysis";
import { TradeSetup } from "../components/dashboard/TradeSetup";
import { PositionSizer } from "../components/dashboard/PositionSizer";
import { StrategySignals } from "../components/dashboard/StrategySignals";
import { FinalDecision } from "../components/dashboard/FinalDecision";
import { EventFeed } from "../components/dashboard/EventFeed";

export function DashboardPage() {
  const system = useSystemStatus();
  const account = usePolling(api.account, 5000);
  const market = usePolling(api.market, 3000);
  const timeframes = usePolling(api.timeframes, 5000);
  const signal = usePolling(api.signal, 5000);
  const positions = usePolling(api.positions, 5000);

  return (
    <div className="flex flex-col gap-3">
      <AccountStrip account={account} positions={positions} system={system.data} />

      <div className="grid grid-cols-1 gap-3 xl:grid-cols-3">
        <div className="flex flex-col gap-3 xl:col-span-2">
          <MarketCard market={market} system={system.data} />
          <TimeframeAnalysis timeframes={timeframes} />
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <TradeSetup signal={signal} />
            <PositionSizer signal={signal} account={account} />
          </div>
          <StrategySignals timeframes={timeframes} />
        </div>

        <div className="flex flex-col gap-3">
          <CompositeSignalCard signal={signal} />
          <FinalDecision signal={signal} />
          <EventFeed />
        </div>
      </div>
    </div>
  );
}
