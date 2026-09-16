import type { ReactNode } from "react";

export function MetricCard({
  label,
  value,
  sub,
  tone = "neutral",
  valueClassName = "",
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "pos" | "neg" | "warn" | "neutral" | "gold";
  valueClassName?: string;
}) {
  const toneColor =
    tone === "pos"
      ? "text-[var(--color-pos)]"
      : tone === "neg"
        ? "text-[var(--color-neg)]"
        : tone === "warn"
          ? "text-[var(--color-warn)]"
          : tone === "gold"
            ? "text-[var(--color-gold-bright)]"
            : "text-[var(--color-text-primary)]";

  return (
    <div className="flex flex-col gap-1 rounded border border-[var(--color-border-soft)] bg-[var(--color-panel-alt)] px-3 py-2.5">
      <span className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-muted)]">{label}</span>
      <span className={`font-num text-lg font-semibold leading-none ${toneColor} ${valueClassName}`}>{value}</span>
      {sub && <span className="text-[10.5px] text-[var(--color-text-muted)]">{sub}</span>}
    </div>
  );
}
