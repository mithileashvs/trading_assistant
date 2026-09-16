import { useState } from "react";
import { api } from "../../lib/api";
import { Panel } from "../ui/Panel";
import { StatusBadge } from "../ui/StatusBadge";
import type { KillSwitchStatus } from "../../lib/types";

export function KillSwitchControl({
  status,
  onChanged,
}: {
  status: KillSwitchStatus | null;
  onChanged: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = status?.active ?? false;

  async function activate() {
    setBusy(true);
    setError(null);
    try {
      await api.activateKillSwitch("Activated via Safety Center.", "dashboard_user");
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  }

  async function deactivate() {
    setBusy(true);
    setError(null);
    try {
      await api.deactivateKillSwitch("dashboard_user");
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title="Kill Switch"
      className={active ? "border-[var(--color-neg)]/50" : undefined}
      right={<StatusBadge tone={active ? "neg" : "pos"} size="md">{active ? "ACTIVE" : "OFF"}</StatusBadge>}
    >
      <div className="flex flex-col gap-3">
        <p className="text-[11px] leading-snug text-[var(--color-text-secondary)]">
          {active
            ? "New automated entries are blocked. This state is enforced by the backend, not this page."
            : "System is armed. Activating this stops new automated entries immediately at the backend."}
        </p>
        {status?.reason && active && (
          <div className="rounded border border-[var(--color-neg)]/30 bg-[var(--color-neg-dim)]/15 px-2.5 py-2 text-[10.5px] text-[var(--color-text-secondary)]">
            <span className="font-semibold text-[var(--color-neg)]">Reason: </span>
            {status.reason}
            {status.activated_by && <span className="text-[var(--color-text-muted)]"> — by {status.activated_by}</span>}
          </div>
        )}
        {error && (
          <div className="rounded border border-[var(--color-neg)]/30 bg-[var(--color-neg-dim)]/15 px-2.5 py-2 text-[10.5px] font-semibold text-[var(--color-neg)]">
            {error}
          </div>
        )}

        {active ? (
          <button
            disabled={busy}
            onClick={deactivate}
            className="rounded border border-[var(--color-pos)]/40 bg-[var(--color-pos-dim)]/20 px-3 py-2 text-[11px] font-bold uppercase tracking-wide text-[var(--color-pos)] hover:bg-[var(--color-pos-dim)]/30 disabled:opacity-50"
          >
            {busy ? "Deactivating…" : "Deactivate Kill Switch"}
          </button>
        ) : confirming ? (
          <div className="flex flex-col gap-2 rounded border border-[var(--color-neg)]/40 bg-[var(--color-neg-dim)]/15 px-3 py-2.5">
            <span className="text-[11px] font-semibold text-[var(--color-neg)]">
              Activate kill switch? This will prevent new automated entries.
            </span>
            <div className="flex gap-2">
              <button disabled={busy} onClick={activate} className="rounded bg-[var(--color-neg)] px-3 py-1.5 text-[10.5px] font-bold text-white disabled:opacity-50">
                {busy ? "Activating…" : "Confirm — Activate"}
              </button>
              <button disabled={busy} onClick={() => setConfirming(false)} className="text-[10.5px] text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)]">
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <button
            onClick={() => setConfirming(true)}
            className="rounded bg-[var(--color-neg)] px-3 py-2 text-[11px] font-bold uppercase tracking-wide text-white hover:opacity-90"
          >
            Activate Kill Switch
          </button>
        )}
      </div>
    </Panel>
  );
}
