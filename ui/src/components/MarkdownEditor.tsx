import { useState } from "react";
import { Markdown } from "./Markdown";

export function MarkdownEditor({ value, onChange, rows = 5, placeholder, disabled = false }: { value: string; onChange: (value: string) => void; rows?: number; placeholder?: string; disabled?: boolean }) {
  const [tab, setTab] = useState<"write" | "preview">("write");
  const button = (value: "write" | "preview", label: string) => (
    <button type="button" onClick={() => setTab(value)} className="rounded-t border px-3 py-1 text-[11px] font-semibold uppercase" style={{ borderColor: "var(--hairline-strong)", color: tab === value ? "var(--accent)" : "var(--text-muted)", background: tab === value ? "var(--surface-2)" : "transparent" }}>
      {label}
    </button>
  );
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap gap-1">{button("write", "Write")}{button("preview", "Preview")}</div>
      {tab === "write" ? (
        <textarea rows={rows} value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} disabled={disabled} className="w-full rounded-b-md rounded-tr-md border px-2.5 py-2 text-[13px] outline-none focus:border-cyan-500" style={{ borderColor: "var(--hairline-strong)", background: "var(--surface-2)", color: "var(--text-primary)" }} />
      ) : (
        <div className="min-h-24 rounded-b-md rounded-tr-md border p-3" style={{ borderColor: "var(--hairline-strong)", background: "var(--surface-2)" }}>
          {value.trim() ? <Markdown text={value} /> : <span className="text-[12px]" style={{ color: "var(--text-muted)" }}>nothing to preview</span>}
        </div>
      )}
    </div>
  );
}
