import { useEffect, useState } from "react";
import { useSystemStatus } from "../../lib/SystemStatusContext";
import { StatusBadge } from "../ui/StatusBadge";

export function TopBar({ onMenuClick }: { onMenuClick?: () => void } = {}) {
  const { data: sys, error } = useSystemStatus();
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const utcTime = now.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZone: "UTC",
  });

  const connected = !!sys && !error;

  return (
    <header className="flex h-12 shrink-0 items-center gap-2 border-b border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 sm:gap-4 sm:px-4">
      <button
        aria-label="Toggle navigation menu"
        onClick={onMenuClick}
        className="flex h-7 w-7 shrink-0 items-center justify-center rounded text-[var(--color-text-secondary)] hover:bg-white/5 lg:hidden"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <path d="M4 6h16M4 12h16M4 18h16" />
        </svg>
      </button>

      <div className="flex items-center gap-1.5">
        <span className="text-[13px] font-extrabold tracking-wide text-[var(--color-gold-bright)]">GOLDSIGNAL</span>
      </div>

      <div className="hidden flex-1 items-center sm:flex">
        <div className="flex w-full max-w-sm items-center gap-2 rounded border border-[var(--color-border)] bg-[var(--color-panel-alt)] px-2.5 py-1.5">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-[var(--color-text-muted)]">
            <circle cx="11" cy="11" r="7" />
            <path d="m21 21-4.3-4.3" />
          </svg>
          <label htmlFor="command-search" className="sr-only">Search or run a command</label>
          <input
            id="command-search"
            placeholder="Search / command…"
            className="w-full bg-transparent text-[12px] text-[var(--color-text-primary)] placeholder:text-[var(--color-text-muted)] focus:outline-none"
          />
        </div>
      </div>
      <div className="flex-1 sm:hidden" />

      <div className="flex items-center gap-1.5 sm:gap-2.5">
        <StatusBadge tone={sys?.trading_mode === "LIVE" ? "neg" : "neutral"} size="md">
          {sys?.trading_mode ?? "…"}
        </StatusBadge>
        <StatusBadge tone={!sys ? "neutral" : sys.mt5_use_mock ? "neutral" : sys.mt5_connected ? "pos" : "neg"} size="md" className="hidden sm:inline-flex">
          MT5 {!sys ? "…" : sys.mt5_use_mock ? "MOCK" : sys.mt5_connected ? "CONNECTED" : "DISCONNECTED"}
        </StatusBadge>

        <div className="hidden items-center gap-1.5 font-num text-[11.5px] text-[var(--color-text-secondary)] md:flex">
          <span>{utcTime}</span>
          <span className="text-[var(--color-text-muted)]">UTC</span>
        </div>

        <div className="flex items-center gap-1.5" title={error ?? "Connected to backend"} role="status" aria-live="polite">
          <span
            className={`h-2 w-2 rounded-full ${connected ? "bg-[var(--color-pos)] pulse-dot" : "bg-[var(--color-neg)]"}`}
          />
          <span className="hidden text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-muted)] md:inline">
            {connected ? "Live" : "Disconnected"}
          </span>
        </div>
      </div>
    </header>
  );
}
