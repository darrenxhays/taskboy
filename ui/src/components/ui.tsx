import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { STATE_COLORS, modelColor, modelLabel } from "../api";

export function CheckIcon() {
  return <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3 8.5L6.5 12L13 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>;
}

export function XIcon() {
  return <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3.5 3.5L12.5 12.5M12.5 3.5L3.5 12.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /></svg>;
}

export function RefreshIcon({ spinning }: { spinning?: boolean }) {
  return <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true" className={spinning ? "animate-spin" : undefined}><path d="M13.5 8a5.5 5.5 0 1 1-1.65-3.92M13.5 2.3v3.9h-3.9" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>;
}

export function PauseIcon() {
  return <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 3h2.5v10H4zM9.5 3H12v10H9.5z" fill="currentColor" /></svg>;
}

export function PlayIcon() {
  return <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4.5 3.2v9.6a.6.6 0 0 0 .92.5l7.6-4.8a.6.6 0 0 0 0-1l-7.6-4.8a.6.6 0 0 0-.92.5z" fill="currentColor" /></svg>;
}

export function TrashIcon() {
  return <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3.5 4.5h9M6.5 4.5V3a1 1 0 0 1 1-1h1a1 1 0 0 1 1 1v1.5M6 7.5v4M10 7.5v4M4.5 4.5l.6 8a1 1 0 0 0 1 .9h3.8a1 1 0 0 0 1-.9l.6-8" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" /></svg>;
}

export function PencilIcon() {
  return <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M11 2.5l2.5 2.5L5 13.5H2.5V11z" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" /></svg>;
}

export function IconButton({ onClick, disabled, title, borderColor, color, filled, children }: { onClick?: () => void; disabled?: boolean; title: string; borderColor: string; color: string; filled?: boolean; children: ReactNode }) {
  return <button onClick={onClick} disabled={disabled} title={title} aria-label={title} className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full border disabled:opacity-40" style={{ borderColor, color: filled ? "white" : color, background: filled ? borderColor : "transparent" }}>{children}</button>;
}

// arm-then-confirm icon button: first click arms it (shows a confirm/cancel pair), second click acts.
export function ConfirmIconButton({ onConfirm, disabled, title, borderColor, color, children }: { onConfirm: () => void; disabled?: boolean; title: string; borderColor: string; color: string; children: ReactNode }) {
  const [armed, setArmed] = useState(false);
  if (!armed) {
    return <IconButton onClick={() => setArmed(true)} disabled={disabled} title={title} borderColor={borderColor} color={color}>{children}</IconButton>;
  }
  return (
    <span className="inline-flex items-center gap-1">
      <IconButton onClick={() => { setArmed(false); onConfirm(); }} disabled={disabled} title={`confirm ${title}`} borderColor={borderColor} color={color} filled>{children}</IconButton>
      <IconButton onClick={() => setArmed(false)} disabled={disabled} title="cancel" borderColor="var(--hairline-strong)" color="var(--text-secondary)"><XIcon /></IconButton>
    </span>
  );
}

export function Panel({ title, right, children, className = "" }: { title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`panel p-4 ${className}`}>
      {(title || right) && (
        <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
          {title ? <h2 className="panel-title">{title}</h2> : <span />}
          {right}
        </header>
      )}
      {children}
    </section>
  );
}

export function StatusBadge({ state, pulse = false }: { state: string; pulse?: boolean }) {
  const color = STATE_COLORS[state] ?? "var(--status-neutral)";
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider" style={{ borderColor: "var(--hairline)", color: "var(--text-secondary)" }}>
      <span className={`h-1.5 w-1.5 rounded-full ${pulse && state === "running" ? "pulse glow-dot" : ""}`} style={{ background: color, color }} />
      {state}
    </span>
  );
}

export function AgentAvatar({ persona, botName = "Agent", reviewerName = "Reviewer" }: { persona: string | null | undefined; botName?: string; reviewerName?: string }) {
  // initials avatar: accent circle for the main agent, neutral for the reviewer persona
  const reviewer = persona === "reviewer";
  const name = reviewer ? reviewerName : botName;
  return (
    <span
      title={name}
      aria-label={name}
      className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[10px] font-black"
      style={{ borderColor: "var(--hairline)", background: reviewer ? "var(--status-warning)" : "var(--accent)", color: "#0a0f16" }}
    >
      {name.charAt(0).toUpperCase()}
    </span>
  );
}

export function ModelChip({ model }: { model: string | null | undefined }) {
  if (!model) return <span className="text-[11px]" style={{ color: "var(--text-muted)" }}>—</span>;
  return (
    <span className="mono inline-flex items-center gap-1.5 text-[11px]" style={{ color: "var(--text-secondary)" }}>
      <span className="h-2 w-2 rounded-sm" style={{ background: modelColor(model) }} />
      {modelLabel(model)}
    </span>
  );
}

export function EffortChip({ effort }: { effort: string | null | undefined }) {
  if (!effort) return null;
  return (
    <span className="mono inline-flex items-center rounded-full border px-1.5 py-0.5 text-[10px] uppercase tracking-wider" style={{ borderColor: "var(--hairline)", color: "var(--text-muted)" }} title="reasoning effort">
      {effort}
    </span>
  );
}

export function TaskLink({ taskId }: { taskId: string }) {
  return (
    <Link to={`/tasks/${taskId}`} className="mono text-[12px] underline-offset-2 hover:underline" style={{ color: "var(--accent)" }}>
      {taskId}
    </Link>
  );
}

export function EmptyState({ label }: { label: string }) {
  return (
    <div className="py-8 text-center text-[13px]" style={{ color: "var(--text-muted)" }}>
      {label}
    </div>
  );
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div className="rounded-md border px-3 py-2 text-[13px]" style={{ borderColor: "var(--status-critical)", color: "var(--status-serious)", background: "rgba(208,59,59,0.08)" }}>
      {message}
    </div>
  );
}

export function TablePager({ total, page, rowsPerPage, storageKey, onPageChange, onRowsPerPageChange }: { total: number; page: number; rowsPerPage: number; storageKey: string; onPageChange: (page: number) => void; onRowsPerPageChange: (rows: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / rowsPerPage));
  const changeRows = (rows: number) => {
    localStorage.setItem(storageKey, String(rows));
    onRowsPerPageChange(rows);
    onPageChange(1);
  };
  return (
    <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t pt-3 text-[11px]" style={{ borderColor: "var(--hairline)", color: "var(--text-muted)" }}>
      <label className="flex items-center gap-2">Rows per page
        <select value={rowsPerPage} onChange={(event) => changeRows(Number(event.target.value))} className="rounded border px-2 py-1" style={{ borderColor: "var(--hairline-strong)", background: "var(--surface-2)", color: "var(--text-secondary)" }}>
          {[10, 25, 50].map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
      </label>
      <div className="flex items-center gap-2">
        <button onClick={() => onPageChange(Math.max(1, page - 1))} disabled={page <= 1} className="rounded border px-2 py-1 disabled:opacity-40" style={{ borderColor: "var(--hairline-strong)" }}>Previous</button>
        <span>Page {Math.min(page, pages)} of {pages}</span>
        <button onClick={() => onPageChange(Math.min(pages, page + 1))} disabled={page >= pages} className="rounded border px-2 py-1 disabled:opacity-40" style={{ borderColor: "var(--hairline-strong)" }}>Next</button>
      </div>
    </div>
  );
}

export function KeyValue({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] font-semibold uppercase tracking-widest" style={{ color: "var(--text-muted)" }}>
        {label}
      </div>
      <div className="mt-0.5 truncate text-[13px]" style={{ color: "var(--text-primary)" }}>
        {children}
      </div>
    </div>
  );
}
