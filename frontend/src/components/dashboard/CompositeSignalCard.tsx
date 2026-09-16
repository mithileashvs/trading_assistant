import { Panel } from "../ui/Panel";
import { StatusBadge } from "../ui/StatusBadge";
import { LoadingState, ErrorState } from "../ui/States";
import type { SignalResponse } from "../../lib/types";
import type { FetchState } from "../../lib/usePolling";

export function CompositeSignalCard({ signal }: { signal: FetchState<SignalResponse> }) {
  const dir = signal.data?.signal.direction;
  const tone = dir === "BUY" ? "pos" : dir === "SELL" ? "neg" : "neutral";

  return (
    <Panel title="Composite Signal — Market Bias">
      {signal.loading && !signal.data ? (
        <LoadingState label="Computing signal…" />
      ) : signal.error && !signal.data ? (
        <ErrorState title="SIGNAL UNAVAILABLE" detail={signal.error} />
      ) : (
        <div className="flex flex-col gap-2.5">
          <div className="flex items-center justify-between">
            <StatusBadge tone={tone} size="md" className="px-3 py-1.5 text-[14px]">
              {dir ?? "—"}
            </StatusBadge>
            <span className="text-[11px] text-[var(--color-text-secondary)]">
              {signal.data?.signal.strategy?.replace(/_/g, " ") ?? "—"}
            </span>
          </div>
          <div className="flex items-center justify-between text-[11px]">
            <span className="text-[var(--color-text-muted)]">Score</span>
            <span className="font-num font-semibold text-[var(--color-text-primary)]">
              {signal.data?.signal.score ?? "—"} / 10{" "}
              <span className="text-[var(--color-text-muted)]">({signal.data?.signal.score_label ?? "—"})</span>
            </span>
          </div>
          <div className="flex items-center justify-between text-[11px]">
            <span className="text-[var(--color-text-muted)]">Confidence</span>
            <span className="font-num font-semibold text-[var(--color-text-primary)]">
              {signal.data?.signal.confidence != null ? `${(signal.data.signal.confidence * 100).toFixed(0)}%` : "—"}
            </span>
          </div>
          <p className="border-t border-[var(--color-border-soft)] pt-2 text-[10.5px] leading-snug text-[var(--color-text-muted)]">
            This reflects the backend's current computed signal only. It is not a trade execution or an
            authorization — see Final Decision below.
          </p>
        </div>
      )}
    </Panel>
  );
}
