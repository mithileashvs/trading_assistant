import { NavLink } from "react-router-dom";
import { useSystemStatus } from "../../lib/SystemStatusContext";
import { StatusBadge } from "../ui/StatusBadge";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", icon: DashboardIcon },
  { to: "/charts", label: "Charts", icon: ChartsIcon },
  { to: "/signals", label: "Signals", icon: SignalsIcon },
  { to: "/positions", label: "Positions", icon: PositionsIcon },
  { to: "/risk", label: "Risk", icon: RiskIcon },
  { to: "/safety", label: "Safety", icon: SafetyIcon },
  { to: "/news", label: "News", icon: NewsIcon },
  { to: "/execution", label: "Execution", icon: ExecutionIcon },
  { to: "/backtest", label: "Backtest", icon: BacktestIcon },
  { to: "/strategy-lab", label: "Strategy Lab", icon: LabIcon },
  { to: "/journal", label: "Journal", icon: JournalIcon },
  { to: "/history", label: "History", icon: HistoryIcon },
  { to: "/performance", label: "Performance", icon: PerformanceIcon },
  { to: "/settings", label: "Settings", icon: SettingsIcon },
];

export function Sidebar({ onNavigate }: { onNavigate?: () => void } = {}) {
  const { data: sys } = useSystemStatus();

  return (
    <aside className="flex h-full w-[196px] shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-bg-elevated)]">
      <div className="flex items-center gap-2 border-b border-[var(--color-border)] px-4 py-4">
        <svg width="20" height="20" viewBox="0 0 32 32" className="shrink-0">
          <rect width="32" height="32" rx="6" fill="#0a0d14" stroke="#1c2331" />
          <path
            d="M8 20 L13 12 L18 17 L24 8"
            stroke="#c9a227"
            strokeWidth="2.2"
            fill="none"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <span className="text-[13px] font-extrabold tracking-[0.12em] text-[var(--color-text-primary)]">
          GOLD<span className="text-[var(--color-gold-bright)]">SIGNAL</span>
        </span>
      </div>

      <nav aria-label="Primary" className="flex-1 overflow-y-auto px-2 py-3">
        <ul className="flex flex-col gap-0.5">
          {NAV_ITEMS.map((item) => (
            <li key={item.to}>
              <NavLink
                to={item.to}
                end={item.to === "/"}
                onClick={onNavigate}
                className={({ isActive }) =>
                  `flex items-center gap-2.5 rounded px-2.5 py-1.5 text-[12px] font-medium transition-colors ${
                    isActive
                      ? "bg-[var(--color-gold-dim)]/20 text-[var(--color-gold-bright)] border-l-2 border-[var(--color-gold)] -ml-0.5 pl-[9px]"
                      : "text-[var(--color-text-secondary)] hover:bg-white/[0.04] hover:text-[var(--color-text-primary)]"
                  }`
                }
              >
                <item.icon className="h-[15px] w-[15px] shrink-0" />
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>

      <div className="flex flex-col gap-2 border-t border-[var(--color-border)] px-3 py-3">
        <StatusRow label="System">
          <StatusBadge tone={sys ? "pos" : "neutral"} dot>
            {sys ? "ONLINE" : "—"}
          </StatusBadge>
        </StatusRow>
        <StatusRow label="Mode">
          <StatusBadge tone={sys?.trading_mode === "LIVE" ? "neg" : sys?.trading_mode === "PAPER" ? "neutral" : "warn"}>
            {sys?.trading_mode ?? "—"}
          </StatusBadge>
        </StatusRow>
        <StatusRow label="MT5">
          <StatusBadge tone={!sys ? "neutral" : sys.mt5_connected ? (sys.mt5_use_mock ? "neutral" : "pos") : "neg"}>
            {!sys ? "—" : sys.mt5_use_mock ? "MOCK" : sys.mt5_connected ? "CONNECTED" : "DISCONNECTED"}
          </StatusBadge>
        </StatusRow>
        <StatusRow label="Kill Switch">
          <StatusBadge tone={sys?.kill_switch_active ? "neg" : "pos"}>
            {sys ? (sys.kill_switch_active ? "ACTIVE" : "OFF") : "—"}
          </StatusBadge>
        </StatusRow>
      </div>
    </aside>
  );
}

function StatusRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">{label}</span>
      {children}
    </div>
  );
}

/* Minimal inline icon set — kept as simple stroked SVGs so no icon
   dependency is required and the visual language stays consistent. */
function IconBase({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" className={className}>
      {children}
    </svg>
  );
}
function DashboardIcon(p: { className?: string }) { return <IconBase {...p}><rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/></IconBase>; }
function ChartsIcon(p: { className?: string }) { return <IconBase {...p}><path d="M4 19V9M10 19V5M16 19v-7M22 19H2"/></IconBase>; }
function SignalsIcon(p: { className?: string }) { return <IconBase {...p}><path d="M4 12a8 8 0 0 1 16 0M7 12a5 5 0 0 1 10 0M12 12v9"/></IconBase>; }
function PositionsIcon(p: { className?: string }) { return <IconBase {...p}><rect x="3" y="4" width="18" height="16" rx="1"/><path d="M3 9h18M8 4v16"/></IconBase>; }
function RiskIcon(p: { className?: string }) { return <IconBase {...p}><path d="M12 2 3 7v6c0 5 4 8 9 9 5-1 9-4 9-9V7z"/><path d="M12 8v5"/><circle cx="12" cy="16" r="0.4" fill="currentColor"/></IconBase>; }
function SafetyIcon(p: { className?: string }) { return <IconBase {...p}><path d="M12 2 3 6v6c0 5 4 9 9 10 5-1 9-5 9-10V6z"/><path d="m9 12 2 2 4-4"/></IconBase>; }
function NewsIcon(p: { className?: string }) { return <IconBase {...p}><rect x="3" y="4" width="18" height="16" rx="1"/><path d="M7 8h10M7 12h10M7 16h6"/></IconBase>; }
function ExecutionIcon(p: { className?: string }) { return <IconBase {...p}><path d="M12 3v6M9 6l3 3 3-3M5 13h14M12 21v-6M9 18l3-3 3 3"/></IconBase>; }
function BacktestIcon(p: { className?: string }) { return <IconBase {...p}><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></IconBase>; }
function LabIcon(p: { className?: string }) { return <IconBase {...p}><path d="M9 2v6L4 20a1 1 0 0 0 1 2h14a1 1 0 0 0 1-2L15 8V2M9 2h6"/></IconBase>; }
function JournalIcon(p: { className?: string }) { return <IconBase {...p}><path d="M6 2h12v20H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Z"/><path d="M9 7h6M9 11h6"/></IconBase>; }
function HistoryIcon(p: { className?: string }) { return <IconBase {...p}><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 3v5h5M12 7v5l4 2"/></IconBase>; }
function PerformanceIcon(p: { className?: string }) { return <IconBase {...p}><path d="M3 3v18h18"/><path d="m7 15 4-6 4 3 5-8"/></IconBase>; }
function SettingsIcon(p: { className?: string }) { return <IconBase {...p}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21a2 2 0 1 1-4 0v-.2a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3a2 2 0 1 1 0-4h.2a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.6V3a2 2 0 1 1 4 0v.2a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.6 1H21a2 2 0 1 1 0 4h-.2a1.7 1.7 0 0 0-1.5 1Z"/></IconBase>; }
