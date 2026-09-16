import type { ReactNode } from "react";

export function Panel({
  title,
  right,
  children,
  className = "",
  bodyClassName = "",
  noPadding = false,
}: {
  title?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  noPadding?: boolean;
}) {
  return (
    <div className={`flex flex-col rounded-md border border-[var(--color-border)] bg-[var(--color-panel)] ${className}`}>
      {title && (
        <div className="flex items-center justify-between border-b border-[var(--color-border-soft)] px-3.5 py-2.5">
          <h3 className="text-[11px] font-semibold uppercase tracking-wider text-[var(--color-text-secondary)]">
            {title}
          </h3>
          {right}
        </div>
      )}
      <div className={`${noPadding ? "" : "p-3.5"} flex-1 ${bodyClassName}`}>{children}</div>
    </div>
  );
}
