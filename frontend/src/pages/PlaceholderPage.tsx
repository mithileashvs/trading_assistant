export function PlaceholderPage({ title, stage }: { title: string; stage: string }) {
  return (
    <div className="flex h-[70vh] flex-col items-center justify-center gap-2 rounded-md border border-dashed border-[var(--color-border)] text-center">
      <span className="text-[13px] font-semibold text-[var(--color-text-secondary)]">{title}</span>
      <span className="text-[11px] text-[var(--color-text-muted)]">{stage} — not yet built in this pass.</span>
    </div>
  );
}
