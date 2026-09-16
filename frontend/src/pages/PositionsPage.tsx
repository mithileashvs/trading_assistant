import { useState } from "react";
import { api } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { Panel } from "../components/ui/Panel";
import { StatusBadge } from "../components/ui/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../components/ui/States";
import { fmtPrice, fmtSigned } from "../lib/format";
import type { CloseResult, Position } from "../lib/types";

export function PositionsPage() {
  const positions = usePolling(api.positions, 4000);
  const [selected, setSelected] = useState<Position | null>(null);

  if (positions.loading && !positions.data) return <LoadingState label="Loading positions…" />;
  if (positions.error && !positions.data) return <ErrorState title="POSITIONS UNAVAILABLE" detail={positions.error} />;

  const rows = positions.data?.positions ?? [];
  const activeSelected = selected ? rows.find((r) => r.ticket === selected.ticket) ?? null : null;

  return (
    <div className="flex flex-col gap-3">
      <Panel title={`Open Positions (${rows.length})`} noPadding>
        {rows.length === 0 ? (
          <div className="p-3">
            <EmptyState label="No open positions." />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[11px]">
              <thead>
                <tr className="border-b border-[var(--color-border-soft)] text-[9.5px] uppercase tracking-wider text-[var(--color-text-muted)]">
                  <Th>Ticket</Th>
                  <Th>Direction</Th>
                  <Th>Entry</Th>
                  <Th>SL</Th>
                  <Th>TP</Th>
                  <Th>Volume</Th>
                  <Th>R Multiple</Th>
                  <Th>P&amp;L (ticks)</Th>
                  <Th>Strategy</Th>
                  <Th>Opened</Th>
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr
                    key={p.ticket}
                    onClick={() => setSelected(p)}
                    className={`cursor-pointer border-b border-[var(--color-border-soft)] transition-colors hover:bg-white/[0.03] ${
                      selected?.ticket === p.ticket ? "bg-[var(--color-gold-dim)]/10" : ""
                    }`}
                  >
                    <Td className="font-num">{p.ticket}</Td>
                    <Td>
                      <StatusBadge tone={p.direction === "BUY" ? "pos" : "neg"}>{p.direction}</StatusBadge>
                    </Td>
                    <Td className="font-num">{fmtPrice(p.entry_price)}</Td>
                    <Td className="font-num text-[var(--color-neg)]">{fmtPrice(p.stop_loss)}</Td>
                    <Td className="font-num text-[var(--color-pos)]">{fmtPrice(p.take_profit)}</Td>
                    <Td className="font-num">{p.volume.toFixed(2)}</Td>
                    <Td className={`font-num ${p.r_multiple != null && p.r_multiple >= 0 ? "text-[var(--color-pos)]" : "text-[var(--color-neg)]"}`}>
                      {p.r_multiple != null ? `${p.r_multiple.toFixed(2)}R` : "—"}
                    </Td>
                    <Td className={`font-num ${p.unrealized_pnl_ticks != null && p.unrealized_pnl_ticks >= 0 ? "text-[var(--color-pos)]" : "text-[var(--color-neg)]"}`}>
                      {p.unrealized_pnl_ticks != null ? fmtSigned(p.unrealized_pnl_ticks, 1) : "—"}
                    </Td>
                    <Td>{p.strategy?.replace(/_/g, " ") || "—"}</Td>
                    <Td className="font-num text-[var(--color-text-muted)]">{new Date(p.open_time).toLocaleString()}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {activeSelected && (
        <PositionDetail position={activeSelected} onClosed={() => positions.refresh()} onDismiss={() => setSelected(null)} />
      )}
    </div>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-3 py-2 font-semibold">{children}</th>;
}
function Td({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <td className={`px-3 py-2 ${className}`}>{children}</td>;
}

function PositionDetail({
  position,
  onClosed,
  onDismiss,
}: {
  position: Position;
  onClosed: () => void;
  onDismiss: () => void;
}) {
  const [pendingAction, setPendingAction] = useState<"close" | "partial" | null>(null);
  const [partialVolume, setPartialVolume] = useState(Math.max(position.volume / 2, 0.01).toFixed(2));
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CloseResult | null>(null);
  const [polling, setPolling] = useState(false);

  async function pollForOutcome(commandId: number) {
    setPolling(true);
    for (let attempt = 0; attempt < 20; attempt++) {
      await new Promise((res) => setTimeout(res, 1500));
      try {
        const status = await api.positionCommandStatus(commandId);
        if (status.status === "DONE" || status.status === "FAILED") {
          const outcome = status.result ?? { queued: false, success: status.status === "DONE", order_id: null, price: null, volume: null, retcode: null, comment: null };
          setResult(outcome);
          if (outcome.success) onClosed();
          setPolling(false);
          return;
        }
      } catch {
        // transient poll failure — keep trying until the attempt budget runs out
      }
    }
    setResult({
      queued: true, success: null, order_id: null, price: null, volume: null, retcode: null,
      comment: "Still pending after 30s — no trading-loop process appears to be running to pick this up. Check that scripts/run_paper_trading.py (or equivalent) is running.",
    });
    setPolling(false);
  }

  async function confirmClose() {
    setBusy(true);
    setResult(null);
    try {
      const r = await api.closePosition(position.ticket, "MANUAL_DASHBOARD_CLOSE");
      setResult(r);
      if (r.queued && r.command_id != null) {
        void pollForOutcome(r.command_id);
      } else if (r.success) {
        onClosed();
      }
    } catch (e) {
      setResult({ queued: false, success: false, order_id: null, price: null, volume: null, retcode: null, comment: e instanceof Error ? e.message : "Request failed" });
    } finally {
      setBusy(false);
      setPendingAction(null);
    }
  }

  async function confirmPartial() {
    const vol = Number(partialVolume);
    if (!(vol > 0)) return;
    setBusy(true);
    setResult(null);
    try {
      const r = await api.closePositionPartial(position.ticket, vol);
      setResult(r);
      if (r.queued && r.command_id != null) {
        void pollForOutcome(r.command_id);
      } else if (r.success) {
        onClosed();
      }
    } catch (e) {
      setResult({ queued: false, success: false, order_id: null, price: null, volume: null, retcode: null, comment: e instanceof Error ? e.message : "Request failed" });
    } finally {
      setBusy(false);
      setPendingAction(null);
    }
  }

  return (
    <Panel
      title={`Position Details — #${position.ticket}`}
      right={
        <button onClick={onDismiss} className="text-[10px] uppercase tracking-wide text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)]">
          Close panel
        </button>
      }
    >
      <div className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Field label="Entry Price" value={fmtPrice(position.entry_price)} />
          <Field label="Stop Loss" value={fmtPrice(position.stop_loss)} tone="neg" />
          <Field label="Take Profit" value={fmtPrice(position.take_profit)} tone="pos" />
          <Field label="Volume" value={position.volume.toFixed(2)} />
          <Field label="Strategy" value={position.strategy?.replace(/_/g, " ") || "—"} />
          <Field label="Regime" value={position.regime || "—"} />
          <Field label="R Multiple" value={position.r_multiple != null ? `${position.r_multiple.toFixed(2)}R` : "—"} />
          <Field label="Opened" value={new Date(position.open_time).toLocaleString()} />
        </div>

        {result && (
          <div
            className={`rounded border px-3 py-2 text-[11px] font-semibold ${
              result.success === true
                ? "border-[var(--color-pos)]/30 bg-[var(--color-pos-dim)]/20 text-[var(--color-pos)]"
                : result.success === false
                  ? "border-[var(--color-neg)]/30 bg-[var(--color-neg-dim)]/20 text-[var(--color-neg)]"
                  : "border-[var(--color-warn)]/30 bg-[var(--color-warn-dim)]/20 text-[var(--color-warn)]"
            }`}
          >
            {result.success === true
              ? `Closed: ${result.comment}`
              : result.success === false
                ? `Rejected: ${result.comment ?? "Unknown error"}`
                : polling
                  ? "Queued — waiting for the trading loop to process this request…"
                  : result.comment}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2 border-t border-[var(--color-border-soft)] pt-3">
          {pendingAction === null ? (
            <>
              <button
                disabled={busy}
                onClick={() => setPendingAction("partial")}
                className="rounded border border-[var(--color-border)] bg-[var(--color-panel-alt)] px-3 py-1.5 text-[11px] font-semibold text-[var(--color-text-primary)] hover:bg-white/5 disabled:opacity-50"
              >
                Partial Close
              </button>
              <button
                disabled={busy}
                onClick={() => setPendingAction("close")}
                className="rounded border border-[var(--color-neg)]/40 bg-[var(--color-neg-dim)]/20 px-3 py-1.5 text-[11px] font-semibold text-[var(--color-neg)] hover:bg-[var(--color-neg-dim)]/30 disabled:opacity-50"
              >
                Close Position
              </button>
            </>
          ) : pendingAction === "close" ? (
            <div className="flex items-center gap-2 rounded border border-[var(--color-warn)]/30 bg-[var(--color-warn-dim)]/15 px-3 py-2">
              <span className="text-[11px] font-semibold text-[var(--color-warn)]">
                Close #{position.ticket} at market? This calls the backend execution engine.
              </span>
              <button disabled={busy} onClick={confirmClose} className="rounded bg-[var(--color-neg)] px-2.5 py-1 text-[10.5px] font-bold text-white disabled:opacity-50">
                {busy ? "Closing…" : "Confirm Close"}
              </button>
              <button disabled={busy} onClick={() => setPendingAction(null)} className="text-[10.5px] text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)]">
                Cancel
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-2 rounded border border-[var(--color-warn)]/30 bg-[var(--color-warn-dim)]/15 px-3 py-2">
              <span className="text-[11px] font-semibold text-[var(--color-warn)]">Partial close volume:</span>
              <input
                type="number"
                step="0.01"
                min="0.01"
                max={position.volume}
                value={partialVolume}
                onChange={(e) => setPartialVolume(e.target.value)}
                className="w-20 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-1.5 py-1 text-[11px] font-num text-[var(--color-text-primary)]"
              />
              <button disabled={busy} onClick={confirmPartial} className="rounded bg-[var(--color-gold)] px-2.5 py-1 text-[10.5px] font-bold text-black disabled:opacity-50">
                {busy ? "Submitting…" : "Confirm Partial"}
              </button>
              <button disabled={busy} onClick={() => setPendingAction(null)} className="text-[10.5px] text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)]">
                Cancel
              </button>
            </div>
          )}
        </div>
        <p className="text-[10px] leading-snug text-[var(--color-text-muted)]">
          In LIVE mode this calls the backend's ExecutionEngine directly (broker-authoritative, same as the
          trading loop's own path). In PAPER mode this request is durably queued for the running trading-loop
          process — the one that actually owns this paper position — to execute on its next cycle; this panel
          polls for the real outcome rather than assuming success. If no trading-loop process is running, the
          request stays PENDING and that is shown honestly rather than hidden.
        </p>
      </div>
    </Panel>
  );
}

function Field({ label, value, tone }: { label: string; value: string; tone?: "pos" | "neg" }) {
  const color = tone === "pos" ? "text-[var(--color-pos)]" : tone === "neg" ? "text-[var(--color-neg)]" : "text-[var(--color-text-primary)]";
  return (
    <div className="flex flex-col gap-0.5 rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-2.5 py-1.5">
      <span className="text-[9.5px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">{label}</span>
      <span className={`font-num text-[13px] font-semibold ${color}`}>{value}</span>
    </div>
  );
}
