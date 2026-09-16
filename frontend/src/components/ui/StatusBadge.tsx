import type { ReactNode } from "react";

export type Tone = "pos" | "neg" | "warn" | "neutral" | "gold";

const toneClasses: Record<Tone, string> = {
  pos: "bg-[var(--color-pos-dim)]/40 text-[var(--color-pos)] border-[var(--color-pos)]/30",
  neg: "bg-[var(--color-neg-dim)]/40 text-[var(--color-neg)] border-[var(--color-neg)]/30",
  warn: "bg-[var(--color-warn-dim)]/40 text-[var(--color-warn)] border-[var(--color-warn)]/30",
  neutral: "bg-white/5 text-[var(--color-text-secondary)] border-[var(--color-border)]",
  gold: "bg-[var(--color-gold-dim)]/25 text-[var(--color-gold-bright)] border-[var(--color-gold)]/40",
};

export function StatusBadge({
  children,
  tone = "neutral",
  dot = false,
  size = "sm",
  className = "",
}: {
  children: ReactNode;
  tone?: Tone;
  dot?: boolean;
  size?: "sm" | "md";
  className?: string;
}) {
  const sizeClasses = size === "sm" ? "px-1.5 py-0.5 text-[10px]" : "px-2.5 py-1 text-[11px]";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded border font-semibold uppercase tracking-wide font-num ${toneClasses[tone]} ${sizeClasses} ${className}`}
    >
      {dot && <span className={`h-1.5 w-1.5 rounded-full bg-current ${tone === "pos" ? "pulse-dot" : ""}`} />}
      {children}
    </span>
  );
}

/** Maps common PASS/BLOCKED/APPROVED/REJECTED/UNKNOWN vocab to a tone consistently. */
export function toneForStatus(status: string): Tone {
  const s = status.toUpperCase();
  if (["PASS", "APPROVED", "OK", "HEALTHY", "CONNECTED", "CLEAR", "ALLOWED", "ENABLED"].includes(s)) return "pos";
  if (["BLOCKED", "REJECTED", "FAIL", "FAILED", "DANGER", "LIVE"].includes(s)) return "neg";
  if (["DEGRADED", "WARNING", "PARTIAL"].includes(s)) return "warn";
  if (["UNKNOWN", "UNAVAILABLE", "MOCK", "PAPER", "DEMO"].includes(s)) return "neutral";
  return "neutral";
}
