export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-6 text-[11px] text-[var(--color-text-muted)]">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-[var(--color-border)] border-t-[var(--color-gold)]" />
      {label}
    </div>
  );
}

export function ErrorState({ title = "UNAVAILABLE", detail }: { title?: string; detail?: string }) {
  return (
    <div className="flex flex-col gap-1 rounded border border-[var(--color-neg)]/30 bg-[var(--color-neg-dim)]/20 px-3 py-2.5">
      <span className="text-[11px] font-bold uppercase tracking-wide text-[var(--color-neg)]">{title}</span>
      {detail && <span className="text-[11px] text-[var(--color-text-secondary)]">{detail}</span>}
    </div>
  );
}

export function EmptyState({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center py-6 text-[11px] text-[var(--color-text-muted)]">{label}</div>
  );
}
