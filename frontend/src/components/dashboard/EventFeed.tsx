import { useEffect, useRef } from "react";
import { Panel } from "../ui/Panel";
import { StatusBadge, type Tone } from "../ui/StatusBadge";
import { useEventFeed } from "../../lib/useEventFeed";

const EVENT_TONE: Record<string, Tone> = {
  SIGNAL_GENERATED: "neutral",
  SIGNAL_REJECTED: "neg",
  RISK_CHECK: "neutral",
  ORDER_SUBMITTED: "warn",
  ORDER_FILLED: "pos",
  ORDER_REJECTED: "neg",
  POSITION_UPDATED: "neutral",
  POSITION_CLOSED: "neutral",
  KILL_SWITCH: "neg",
  SYSTEM_ERROR: "neg",
  MT5_CONNECT: "pos",
  MT5_DISCONNECT: "neg",
  SYMBOL_DISCOVERY: "neutral",
  MARKET_DATA: "neutral",
  REGIME_CHANGE: "gold",
};

export function EventFeed() {
  const { events, status } = useEventFeed();
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events.length]);

  return (
    <Panel
      title="Live Event Feed"
      right={
        <div className="flex items-center gap-1.5">
          <span className={`h-1.5 w-1.5 rounded-full ${status === "open" ? "bg-[var(--color-pos)] pulse-dot" : status === "connecting" ? "bg-[var(--color-warn)]" : "bg-[var(--color-neg)]"}`} />
          <span className="text-[9.5px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">
            {status === "open" ? "Live" : status === "connecting" ? "Connecting…" : "Disconnected"}
          </span>
        </div>
      }
      noPadding
    >
      <div ref={scrollRef} className="max-h-[260px] overflow-y-auto px-3 py-2">
        {events.length === 0 ? (
          <div className="py-6 text-center text-[11px] text-[var(--color-text-muted)]">
            {status === "open"
              ? "No events logged yet — this feed reflects the backend's real event log, not simulated activity."
              : "Waiting to connect to the backend's live event stream…"}
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            {events.map((e, i) => (
              <div key={i} className="flash-update flex items-start gap-2 rounded px-1.5 py-1 text-[10.5px]">
                <span className="shrink-0 font-num text-[var(--color-text-muted)]">
                  {e.ts ? new Date(e.ts).toLocaleTimeString() : "—"}
                </span>
                {e.event_type && (
                  <StatusBadge tone={EVENT_TONE[e.event_type] ?? "neutral"} className="shrink-0">
                    {e.event_type}
                  </StatusBadge>
                )}
                <span className="text-[var(--color-text-secondary)]">{e.message}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </Panel>
  );
}
