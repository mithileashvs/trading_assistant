import { useEffect, useRef, useState } from "react";

export interface LiveEvent {
  ts: string;
  level: string;
  logger?: string;
  message: string;
  event_type?: string;
  data?: Record<string, unknown>;
}

export type WsStatus = "connecting" | "open" | "closed";

const MAX_EVENTS = 200;

/**
 * Connects to the backend's /ws/events WebSocket, which tails the real
 * structured event log every pipeline stage already writes to
 * (app/logging_config.py::log_event). This never fabricates events —
 * an empty feed means no trading-loop process has logged anything in
 * this environment, and that's shown honestly rather than papered over.
 */
export function useEventFeed(): { events: LiveEvent[]; status: WsStatus } {
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [status, setStatus] = useState<WsStatus>("connecting");
  const retryRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout>;

    function connect() {
      if (cancelled) return;
      setStatus("connecting");
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${protocol}//${window.location.host}/ws/events`;
      ws = new WebSocket(url);

      ws.onopen = () => {
        if (cancelled) return;
        retryRef.current = 0;
        setStatus("open");
      };

      ws.onmessage = (msg) => {
        if (cancelled) return;
        try {
          const event = JSON.parse(msg.data) as LiveEvent;
          setEvents((prev) => [...prev.slice(-(MAX_EVENTS - 1)), event]);
        } catch {
          // Non-JSON payload — ignore rather than crash the feed.
        }
      };

      ws.onclose = () => {
        if (cancelled) return;
        setStatus("closed");
        const delay = Math.min(1000 * 2 ** retryRef.current, 15000);
        retryRef.current += 1;
        retryTimer = setTimeout(connect, delay);
      };

      ws.onerror = () => {
        ws?.close();
      };
    }

    connect();

    return () => {
      cancelled = true;
      clearTimeout(retryTimer);
      ws?.close();
    };
  }, []);

  return { events, status };
}
