import { api } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { useSystemStatus } from "../lib/SystemStatusContext";
import { Panel } from "../components/ui/Panel";
import { StatusBadge } from "../components/ui/StatusBadge";
import { ErrorState, LoadingState } from "../components/ui/States";

export function SettingsPage() {
  const system = useSystemStatus();
  const risk = usePolling(api.risk, 10000);
  const news = usePolling(api.news, 10000);

  const sys = system.data;
  const isLive = sys?.trading_mode === "LIVE" && !sys.mt5_use_mock;

  return (
    <div className="flex flex-col gap-3">
      <Panel
        title="Trading Mode"
        right={sys && (
          <StatusBadge tone={isLive ? "neg" : "neutral"} size="md">
            {sys.trading_mode}
          </StatusBadge>
        )}
        className={isLive ? "border-[var(--color-neg)]/50" : undefined}
      >
        {system.loading && !sys ? (
          <LoadingState />
        ) : system.error && !sys ? (
          <ErrorState title="UNAVAILABLE" detail={system.error} />
        ) : (
          <div className="flex flex-col gap-2">
            <div className={`rounded border px-3 py-2.5 text-[11px] font-semibold ${isLive ? "border-[var(--color-neg)]/40 bg-[var(--color-neg-dim)]/20 text-[var(--color-neg)]" : "border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] text-[var(--color-text-secondary)]"}`}>
              {isLive
                ? "LIVE mode — real orders may be sent to a real broker. Every action on this dashboard is treated with extra caution in this mode."
                : `Currently running in ${sys?.trading_mode ?? "—"} mode${sys?.mt5_use_mock ? " with a MOCK MT5 client" : ""}. No real orders can be sent.`}
            </div>
            <p className="text-[10.5px] leading-snug text-[var(--color-text-muted)]">
              Trading mode is fixed by the backend's own configuration (environment / settings file) and cannot be
              changed from this dashboard. This is intentional: switching PAPER → LIVE is exactly the kind of
              change that must never happen silently from a UI.
            </p>
          </div>
        )}
      </Panel>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <Panel title="System">
          {sys && (
            <div className="grid grid-cols-2 gap-2">
              <SettingRow label="Symbol" value={sys.symbol} />
              <SettingRow label="MT5 Mode" value={sys.mt5_use_mock ? "MOCK" : "REAL"} />
              <SettingRow label="MT5 Connected" value={sys.mt5_connected ? "Yes" : "No"} />
              <SettingRow label="Broker Trade Allowed" value={sys.broker_trade_allowed == null ? "—" : sys.broker_trade_allowed ? "Yes" : "No"} />
              <SettingRow label="Market Data Fresh" value={sys.market_data_fresh ? "Yes" : "No"} />
              <SettingRow label="Kill Switch" value={sys.kill_switch_active ? "ACTIVE" : "Off"} />
            </div>
          )}
        </Panel>

        <Panel title="News">
          {news.loading && !news.data ? (
            <LoadingState />
          ) : news.error && !news.data ? (
            <ErrorState title="UNAVAILABLE" detail={news.error} />
          ) : (
            <div className="flex flex-col gap-2">
              <SettingRow label="Provider Status" value={news.data!.state !== "UNAVAILABLE" ? "Connected" : "Not configured"} />
              <SettingRow label="Current State" value={news.data!.state} />
              <p className="text-[10.5px] leading-snug text-[var(--color-text-muted)]">{news.data!.reason}</p>
            </div>
          )}
        </Panel>
      </div>

      <Panel title="Risk">
        {risk.loading && !risk.data ? (
          <LoadingState />
        ) : risk.error && !risk.data ? (
          <ErrorState title="UNAVAILABLE" detail={risk.error} />
        ) : (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <SettingRow label="Risk per Trade" value={`${risk.data!.limits.risk_per_trade_pct}%`} />
            <SettingRow label="Max Daily Loss" value={`${risk.data!.limits.max_daily_loss_pct}%`} />
            <SettingRow label="Max Weekly Loss" value={`${risk.data!.limits.max_weekly_loss_pct}%`} />
            <SettingRow label="Max Open Positions" value={String(risk.data!.limits.max_open_positions)} />
            <SettingRow label="Max Trades / Day" value={String(risk.data!.limits.max_trades_per_day)} />
            <SettingRow label="Max Consecutive Losses" value={String(risk.data!.limits.max_consecutive_losses)} />
            <SettingRow label="Max Spread (pts)" value={String(risk.data!.limits.max_spread_points)} />
            <SettingRow label="Max Slippage (pts)" value={String(risk.data!.limits.max_slippage_points)} />
          </div>
        )}
        <p className="mt-2.5 text-[10px] leading-snug text-[var(--color-text-muted)]">
          Risk limits are backend configuration. Changing them requires a backend configuration change and its own
          validation — this dashboard shows the live values but cannot edit them.
        </p>
      </Panel>

      <Panel title="Appearance">
        <SettingRow label="Theme" value="Dark (fixed)" />
      </Panel>
    </div>
  );
}

function SettingRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5 rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-2.5 py-1.5">
      <span className="text-[9.5px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">{label}</span>
      <span className="font-num text-[12px] font-semibold text-[var(--color-text-primary)]">{value}</span>
    </div>
  );
}
