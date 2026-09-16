import { api } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { Panel } from "../components/ui/Panel";
import { StatusBadge } from "../components/ui/StatusBadge";
import { ErrorState, LoadingState } from "../components/ui/States";

export function NewsPage() {
  const news = usePolling(api.news, 10000);

  if (news.loading && !news.data) return <LoadingState label="Checking news filter…" />;
  if (news.error && !news.data) return <ErrorState title="NEWS DATA UNAVAILABLE" detail={news.error} />;

  const n = news.data!;
  // CLEAR is the only state that permits a new trade. UNKNOWN and
  // UNAVAILABLE both mean "we don't actually know" and must never be
  // shown as safe — only BLOCKED gets the "active blackout" (neg)
  // treatment; the other two non-CLEAR states render as neutral so
  // they're not confused with "definitely clear" but are still
  // visually distinct from an active event blackout.
  const tone = n.state === "CLEAR" ? "pos" : n.state === "BLOCKED" ? "neg" : "neutral";
  const label = n.state;

  return (
    <div className="flex flex-col gap-3">
      <Panel title="News Safety" right={<StatusBadge tone={tone} size="md">{label}</StatusBadge>}>
        <div className="flex flex-col gap-2">
          <p className="text-[11px] leading-snug text-[var(--color-text-secondary)]">
            {n.reason ?? "No further detail provided by the backend."}
          </p>
          {!n.permits_new_trade && (
            <div className="rounded border border-[var(--color-warn)]/30 bg-[var(--color-warn-dim)]/20 px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--color-warn)]">
              {n.state === "UNAVAILABLE"
                ? "No news-data provider is configured — this reflects the backend's actual, honest state rather than a fabricated economic calendar. New trades are blocked."
                : n.state === "UNKNOWN"
                ? "The configured calendar cannot confidently speak to this specific time. New trades are blocked."
                : "An active high-impact news blackout window is in effect. New trades are blocked."}
            </div>
          )}
        </div>
      </Panel>

      <Panel title="Economic Calendar">
        <p className="text-[11px] text-[var(--color-text-muted)]">
          No news-data provider is wired into the backend, so there is no real calendar data to show here. This
          panel will populate once a real provider (e.g. ForexFactory, an economic-calendar API) is integrated on
          the backend — it intentionally does not display invented events, times, or impact levels.
        </p>
      </Panel>
    </div>
  );
}
