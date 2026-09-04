import { useEffect, useState } from "react";
import { api, type ManageTarget, type SaveResult } from "../api";
import { EmptyState, ErrorNote, Panel } from "../components/ui";
import { useLiveData } from "../stream";

function DiffView({ diff }: { diff: string }) {
  if (!diff.trim()) return <EmptyState label="no changes" />;
  return (
    <pre className="mono max-h-80 overflow-auto rounded-md border p-3 text-[11px] leading-relaxed" style={{ borderColor: "var(--hairline)", background: "var(--surface-2)" }}>
      {diff.split("\n").map((line, i) => (
        <div key={i} style={{ color: line.startsWith("+") ? "var(--status-good)" : line.startsWith("-") ? "var(--status-critical)" : "var(--text-muted)" }}>
          {line || " "}
        </div>
      ))}
    </pre>
  );
}

function Editor({ kind, name, onClose }: { kind: string; name?: string; onClose: () => void }) {
  const [target, setTarget] = useState<ManageTarget | null>(null);
  const [content, setContent] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<SaveResult | null>(null);
  const [result, setResult] = useState<SaveResult | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setTarget(null);
    setPreview(null);
    setResult(null);
    setError(null);
    api
      .manageRead(kind, name)
      .then((loaded) => {
        setTarget(loaded);
        setContent(loaded.content);
      })
      .catch((e: Error) => setError(e.message));
  }, [kind, name]);

  const submit = async (action: "preview" | "confirm") => {
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      const response = await api.manageWrite(kind, { name: name ?? null, content, base_hash: target.base_hash, action });
      if (action === "preview") {
        setPreview(response);
      } else {
        setResult(response);
        setPreview(null);
      }
    } catch (e) {
      setError((e as Error).message);
      setPreview(null);
    } finally {
      setBusy(false);
    }
  };

  if (error && !target) return <ErrorNote message={error} />;
  if (!target) return <EmptyState label="loading editor…" />;

  if (result) {
    return (
      <div className="space-y-3">
        <div className="rounded-md border px-3 py-2 text-[13px]" style={{ borderColor: "var(--status-good)", color: "var(--status-good)", background: "rgba(12,163,12,0.08)" }}>
          ✓ {result.message}
        </div>
        {result.commit && !result.commit.unchanged && (
          <div className="text-[12px]" style={{ color: "var(--text-secondary)" }}>
            committed to git:{" "}
            <a href={result.commit.html_url} target="_blank" rel="noreferrer" className="mono underline-offset-2 hover:underline" style={{ color: "var(--accent)" }}>
              {result.commit.commit_sha.slice(0, 10)}
            </a>
          </div>
        )}
        {result.commit_error && <ErrorNote message={`saved live, but the git auto-commit failed: ${result.commit_error}`} />}
        <DiffView diff={result.diff} />
        <button onClick={onClose} className="rounded-md border px-3 py-1.5 text-[12px]" style={{ borderColor: "var(--hairline-strong)", color: "var(--text-secondary)" }}>
          done
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-[14px] font-semibold">{target.title}</div>
          <div className="mono text-[11px]" style={{ color: "var(--text-muted)" }}>
            {target.repo_path}
            {target.auto_commit ? " · auto-commits to git on save" : ""}
          </div>
        </div>
        <button onClick={onClose} className="text-[12px]" style={{ color: "var(--text-muted)" }}>
          ✕ close
        </button>
      </div>
      {error && <ErrorNote message={error} />}
      <textarea
        value={content}
        onChange={(event) => {
          setContent(event.target.value);
          setPreview(null);
        }}
        spellCheck={false}
        rows={Math.min(Math.max(content.split("\n").length + 2, 8), 28)}
        className="mono w-full rounded-md border p-3 text-[12px] leading-relaxed outline-none focus:border-cyan-600"
        style={{ borderColor: "var(--hairline-strong)", background: "var(--surface-2)", color: "var(--text-primary)" }}
      />
      {preview && (
        <div>
          <div className="panel-title mb-1.5">Proposed diff</div>
          <DiffView diff={preview.diff} />
        </div>
      )}
      <div className="flex items-center gap-2">
        <button onClick={() => submit("preview")} disabled={busy} className="rounded-md border px-3 py-1.5 text-[12px] font-semibold disabled:opacity-40" style={{ borderColor: "var(--accent)", color: "var(--accent)" }}>
          PREVIEW DIFF
        </button>
        {preview && (
          <button onClick={() => submit("confirm")} disabled={busy} className="rounded-md px-3 py-1.5 text-[12px] font-bold text-white disabled:opacity-40" style={{ background: "var(--status-good)" }}>
            CONFIRM & SAVE
          </button>
        )}
        <span className="text-[11px]" style={{ color: "var(--text-muted)" }}>
          validation runs the same loader the service uses — a save that passes cannot brick the agent
        </span>
      </div>
    </div>
  );
}

function AdminEvents() {
  const { data, error } = useLiveData(() => api.adminEvents());
  if (error) return <ErrorNote message={error} />;
  if (!data) return <EmptyState label="loading…" />;
  if (data.events.length === 0) return <EmptyState label="no management actions yet" />;
  return (
    <div className="max-h-72 space-y-1 overflow-y-auto">
      {data.events.map((event, i) => (
        <div key={i} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 border-b py-1.5 text-[11px] last:border-b-0" style={{ borderColor: "var(--hairline)" }}>
          <span className="mono shrink-0" style={{ color: "var(--text-muted)" }}>
            {String(event.ts).replace("T", " ").replace("+00:00", "Z")}
          </span>
          <span style={{ color: "var(--text-secondary)" }}>{String(event.actor)}</span>
          <span className="mono" style={{ color: "var(--accent)" }}>
            {String(event.action)}
          </span>
          <span className="min-w-0 flex-1 truncate" style={{ color: "var(--text-muted)" }}>
            {String(event.target)}
          </span>
          <span className="font-semibold" style={{ color: String(event.outcome) === "success" ? "var(--status-good)" : String(event.outcome).match(/denied|failed|rejected|blocked/) ? "var(--status-critical)" : "var(--text-secondary)" }}>
            {String(event.outcome)}
          </span>
        </div>
      ))}
    </div>
  );
}

export function ConfigPage({ admin }: { admin: boolean }) {
  const { data, error } = useLiveData(() => api.config());
  const [editing, setEditing] = useState<{ kind: string; name?: string } | null>(null);

  if (error) return <ErrorNote message={error} />;
  if (!data) return <EmptyState label="loading configuration…" />;

  const policy = data.policy as { agent?: { personality_file?: string }; reviewer?: { enabled?: boolean; personality_file?: string }; conventions?: { file?: string }; help?: { file?: string } };
  const personalityEnabled = Boolean(policy.agent?.personality_file);
  const reviewerEnabled = Boolean(policy.reviewer?.enabled && policy.reviewer?.personality_file);
  const conventionsEnabled = Boolean(policy.conventions?.file);
  const helpEnabled = Boolean(policy.help?.file);

  const editButton = (kind: string, label: string, name?: string) => (
    <button
      key={`${kind}:${name ?? ""}`}
      onClick={() => setEditing({ kind, name })}
      className="rounded-md border px-3 py-1.5 text-left text-[12px] font-medium transition-colors"
      style={{
        borderColor: editing?.kind === kind && editing?.name === name ? "var(--accent)" : "var(--hairline-strong)",
        color: editing?.kind === kind && editing?.name === name ? "var(--accent)" : "var(--text-secondary)",
        background: editing?.kind === kind && editing?.name === name ? "var(--accent-dim)" : "transparent",
      }}
    >
      {label}
    </button>
  );

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <h1 className="text-[18px] font-bold tracking-wide">CONFIGURATION</h1>

      {admin && (
        <Panel title="Management — edits apply live and auto-commit to git">
          <div className="flex flex-wrap gap-2">
            {editButton("config", "⚙ config.yaml")}
            {Object.entries(data.services ?? {})
              .filter(([, service]) => service.editable)
              .map(([name, service]) => editButton("service", `${service.enabled ? "●" : "○"} services/${name}.yaml`, name))}
            {personalityEnabled && editButton("personality", "◈ personality")}
            {reviewerEnabled && editButton("reviewer_personality", "◈ reviewer personality")}
            {conventionsEnabled && editButton("conventions", "§ conventions")}
            {helpEnabled && editButton("help", "❓ help")}
            {editButton("started", "✉ task-started phrases")}
            {data.skills.map((skill) => editButton("skill", `/${skill}`, skill))}
          </div>
          {editing && (
            <div className="mt-4 border-t pt-4" style={{ borderColor: "var(--hairline)" }}>
              <Editor kind={editing.kind} name={editing.name} onClose={() => setEditing(null)} />
            </div>
          )}
        </Panel>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        <Panel title="Runtime">
          <div className="space-y-1.5">
            {Object.entries(data.runtime).map(([key, value]) => (
              <div key={key} className="flex justify-between gap-3 text-[12px]">
                <span className="shrink-0" style={{ color: "var(--text-muted)" }}>{key}</span>
                <span className="mono min-w-0 truncate" style={{ color: "var(--text-secondary)" }} title={String(value)}>
                  {String(value)}
                </span>
              </div>
            ))}
          </div>
        </Panel>
        <Panel title="Secrets — presence only">
          <div className="space-y-1.5">
            {Object.entries(data.secret_presence).map(([key, present]) => (
              <div key={key} className="flex items-center justify-between text-[12px]">
                <span className="mono" style={{ color: "var(--text-secondary)" }}>
                  {key}
                </span>
                <span className="font-semibold" style={{ color: present ? "var(--status-good)" : "var(--text-muted)" }}>
                  {present ? "● set" : "○ not set"}
                </span>
              </div>
            ))}
          </div>
        </Panel>
        <Panel title="Dashboard">
          <div className="space-y-1.5">
            {Object.entries(data.dashboard).map(([key, value]) => (
              <div key={key} className="flex justify-between gap-3 text-[12px]">
                <span className="shrink-0" style={{ color: "var(--text-muted)" }}>{key}</span>
                <span className="mono min-w-0 truncate" style={{ color: "var(--text-secondary)" }}>
                  {JSON.stringify(value)}
                </span>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      <Panel title="AWS diagnostics roles — can the host assume each environment?">
        {Object.keys(data.aws_environments ?? {}).length === 0 ? (
          <EmptyState label="no diagnostics_role_arns configured in services/aws.yaml (or the aws service is disabled)" />
        ) : (
          <div className="space-y-1.5">
            {Object.entries(data.aws_environments).map(([environment, status]) => (
              <div key={environment} className="flex items-start justify-between gap-3 text-[12px]">
                <span className="mono shrink-0" style={{ color: "var(--text-secondary)" }}>
                  {environment}
                </span>
                <span className="mono min-w-0 break-all text-right font-semibold" style={{ color: status === "ok" ? "var(--status-good)" : "var(--status-critical)" }} title={status}>
                  {status === "ok" ? "● ok" : `○ ${status}`}
                </span>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel title="Operator policy — config.yaml + services/*.yaml merged (redacted)">
        <pre className="mono max-h-[480px] overflow-auto rounded-md border p-3 text-[11px] leading-relaxed" style={{ borderColor: "var(--hairline)", background: "var(--surface-2)", color: "var(--text-secondary)" }}>
          {JSON.stringify(data.policy, null, 2)}
        </pre>
      </Panel>

      {admin && (
        <Panel title="Management audit log">
          <AdminEvents />
        </Panel>
      )}
    </div>
  );
}
