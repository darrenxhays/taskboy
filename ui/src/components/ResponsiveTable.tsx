import type { MouseEventHandler, ReactNode } from "react";

// table is hidden (not shrunk) below sm, so its min-w never forces a phone scrollbar
export function ResponsiveTable({ table, cards, className = "" }: { table: ReactNode; cards: ReactNode; className?: string }) {
  return (
    <div className={className}>
      <div className="hidden sm:block">{table}</div>
      <div className="space-y-2 sm:hidden">{cards}</div>
    </div>
  );
}

/** A single mobile row rendered as a card. Pass `onClick` to make the whole card (e.g. expand-to-detail) tappable. */
export function RowCard({ onClick, selected, children, className = "" }: { onClick?: MouseEventHandler<HTMLDivElement>; selected?: boolean; children: ReactNode; className?: string }) {
  return (
    <div
      onClick={onClick}
      className={`rounded-md border p-3 text-[13px] ${onClick ? "cursor-pointer" : ""} ${className}`}
      style={{ borderColor: selected ? "var(--accent)" : "var(--hairline)", background: "var(--surface-2)" }}
    >
      {children}
    </div>
  );
}

/** Primary line of a card: title/summary on the left, a status pill or similar on the right. Wraps on narrow widths. */
export function CardHeader({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap items-start justify-between gap-2">{children}</div>;
}

/** Secondary metadata row: repo/type chips, relative times, etc. */
export function CardMeta({ children }: { children: ReactNode }) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]" style={{ color: "var(--text-muted)" }}>
      {children}
    </div>
  );
}

/** Touch-friendly action row, right-aligned with a separating hairline. Stops propagation so it works inside a tappable RowCard. */
export function CardActions({ children, className = "justify-end" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`mt-2.5 flex flex-wrap items-center gap-2 border-t pt-2.5 ${className}`} style={{ borderColor: "var(--hairline)" }} onClick={(event) => event.stopPropagation()}>
      {children}
    </div>
  );
}
