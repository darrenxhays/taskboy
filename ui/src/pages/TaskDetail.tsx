import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, formatUsd, type TaskEvent } from "../api";
import { Markdown } from "../components/Markdown";
import { AgentAvatar, EffortChip, EmptyState, ErrorNote, KeyValue, ModelChip, Panel, StatusBadge, TaskLink } from "../components/ui";
import { useLiveData } from "../stream";

const KIND_TONES: Record<string, string> = {
  state_change: "var(--accent)",
  security_denial: "var(--status-critical)",
  operator_action: "var(--status-warning)",
  rate_limit: "var(--status-serious)",
  model_fallback: "var(--status-serious)",
  recovery: "var(--status-warning)",
  permission_request: "var(--status-warning)",
  permission_decision: "var(--accent)",
  permissions_applied: "var(--status-good)",
  milestone: "var(--status-good)",
  tool_call: "var(--text-muted)",
};

function pretty(detailJson: string): string {
  try {
    return JSON.stringify(JSON.parse(detailJson), null, 2);
  } catch {
    return detailJson;
  }
}

function EventRow({ event }: { event: TaskEvent }) {
  const [open, setOpen] = useState(false);
  const tone = KIND_TONES[event.kind] ?? "var(--text-secondary)";
  return (
    <div className="relative border-l-2 pb-3 pl-4" style={{ borderColor: "var(--grid)" }}>
      <span className="absolute -left-[5px] top-1 h-2 w-2 rounded-full" style={{ background: tone }} />
      <button onClick={() => setOpen(!open)} className="flex w-full flex-wrap items-baseline gap-x-3 gap-y-0.5 text-left">
        <span className="mono shrink-0 text-[11px]" style={{ color: "var(--text-muted)" }}>
          {event.ts.replace("T", " ").replace("+00:00", "Z")}
        </span>
        <span className="mono min-w-0 max-w-full truncate text-[12px] font-semibold" style={{ color: tone }}>
          {event.kind}
        </span>
        {event.tool_name && (
          <span className="mono min-w-0 max-w-full truncate text-[11px]" style={{ color: "var(--text-secondary)" }}>
            {event.tool_name}
            {event.is_write ? " ✎" : ""}
          </span>
        )}
        <span className="mono min-w-0 flex-1 truncate text-[11px]" style={{ color: "var(--text-muted)" }}>
          {event.detail_json}
        </span>
      </button>
      {open && (
        <pre className="mono mt-1.5 whitespace-pre-wrap break-words rounded-md border p-3 text-[11px]" style={{ borderColor: "var(--hairline)", background: "var(--surface-2)", color: "var(--text-secondary)" }}>
          {pretty(event.detail_json)}
        </pre>
      )}
    </div>
  );
}

function ActionButton({ label, tone, onClick, busy }: { label: string; tone: string; onClick: () => void; busy: boolean }) {
  const [arming, setArming] = useState(false);
  if (!arming) {
    return (
      <button onClick={() => setArming(true)} disabled={busy} className="rounded-md border px-3 py-1.5 text-[12px] font-semibold tracking-wide disabled:opacity-40" style={{ borderColor: tone, color: tone }}>
        {label}
      </button>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      <button
        onClick={() => {
          setArming(false);
          onClick();
        }}
        disabled={busy}
        className="rounded-md px-3 py-1.5 text-[12px] font-bold text-white disabled:opacity-40"
        style={{ background: tone }}
      >
        CONFIRM {label}
      </button>
      <button onClick={() => setArming(false)} className="rounded-md border px-2 py-1.5 text-[12px]" style={{ borderColor: "var(--hairline-strong)", color: "var(--text-secondary)" }}>
        ✕
      </button>
    </span>
  );
}

export function TaskDetailPage({ admin }: { admin: boolean }) {
  const { taskId = "" } = useParams();
  const navigate = useNavigate();
  const [eventPage, setEventPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [actionNote, setActionNote] = useState<string | null>(null);
  const { data, error, reload } = useLiveData(() => api.task(taskId, eventPage), [taskId, eventPage]);

  if (error) return <ErrorNote message={error} />;
  if (!data) return <EmptyState label="loading task…" />;
  const task = data.task;
  const eventPages = Math.max(1, Math.ceil(data.event_count / 100));

  const decide = async (kind: string, target: string, decision: "granted" | "denied") => {
    setBusy(true);
    setActionNote(null);
    try {
      const result = await api.decidePermission(taskId, { kind, target, decision });
      setActionNote(`${decision === "granted" ? "grant" : "deny"} ${target}: ${result.status}`);
      reload();
    } catch (e) {
      setActionNote((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const act = async (action: "cancel" | "retry") => {
    setBusy(true);
    setActionNote(null);
    try {
      if (action === "cancel") {
        const result = await api.cancel(taskId);
        setActionNote(`cancel: ${result.status}`);
      } else {
        const result = await api.retry(taskId);
        setActionNote(`retry: ${result.status}`);
        if (result.new_task_id) navigate(`/tasks/${result.new_task_id}`);
      }
      reload();
    } catch (e) {
      setActionNote((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <header className="flex flex-wrap items-center gap-3">
        <AgentAvatar persona={(task.persona as string | null) ?? null} botName={data.bot_name} reviewerName={data.reviewer_name} />
        <h1 className="mono text-[17px] font-bold">{taskId}</h1>
        <StatusBadge state={String(task.state)} pulse />
        <ModelChip model={task.model_alias as string | null} />
        <EffortChip effort={(task.effort_override ?? task.effort) as string | null} />
        <span className="flex-1" />
        <Link to={`/tasks/${taskId}/feedback`} className="rounded-md border px-3 py-1.5 text-[12px] font-semibold tracking-wide" style={{ borderColor: "var(--status-warning)", color: "var(--status-warning)" }}>
          ☆ FEEDBACK
        </Link>
        {admin && data.can_cancel && <ActionButton label="CANCEL" tone="var(--status-critical)" onClick={() => act("cancel")} busy={busy} />}
        {admin && data.can_retry && <ActionButton label="RETRY" tone="var(--accent)" onClick={() => act("retry")} busy={busy} />}
      </header>
      {actionNote && (
        <div className="rounded-md border px-3 py-2 text-[12px]" style={{ borderColor: "var(--hairline-strong)", color: "var(--text-secondary)" }}>
          {actionNote}
        </div>
      )}

      <Panel title="Request">
        <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed" style={{ color: "var(--text-primary)" }}>
          {String(task.request_text ?? "")}
        </p>
        {task.reply != null && String(task.reply) && (
          <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--hairline)" }}>
            <div className="panel-title mb-1.5">Reply</div>
            <Markdown text={String(task.reply)} />
          </div>
        )}
        {task.error != null && String(task.error) && (
          <div className="mt-3">
            <ErrorNote message={String(task.error)} />
          </div>
        )}
      </Panel>

      <Panel title="Telemetry">
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 md:grid-cols-4 lg:grid-cols-6">
          <KeyValue label="Requester">{data.requester ?? String(task.slack_user_id ?? "—")}</KeyValue>
          <KeyValue label="Type">{String(task.task_type ?? "—")}</KeyValue>
          <KeyValue label="Complexity">{String(task.complexity ?? "—")}</KeyValue>
          <KeyValue label="Risk">{String(task.risk ?? "—")}</KeyValue>
          <KeyValue label="Profile">{String(task.profile ?? "—")}</KeyValue>
          <KeyValue label="Attempt">{String(task.attempt ?? 0)}</KeyValue>
          <KeyValue label="Cost">{formatUsd(task.cost_usd as number)}</KeyValue>
          <KeyValue label="Turns">{String(task.num_turns ?? 0)}</KeyValue>
          <KeyValue label="Created">{String(task.created_at ?? "—")}</KeyValue>
          <KeyValue label="Started">{String(task.started_at ?? "—")}</KeyValue>
          <KeyValue label="Finished">{String(task.finished_at ?? "—")}</KeyValue>
          <KeyValue label="Session">{String(task.session_id ?? "—")}</KeyValue>
        </div>
        {task.routing_rationale != null && String(task.routing_rationale) && (
          <p className="mt-3 text-[12px] italic" style={{ color: "var(--text-muted)" }}>
            routing: {String(task.routing_rationale)}
          </p>
        )}
      </Panel>

      {data.permission_requests.length > 0 && (
        <Panel title="Permission Requests">
          <div className="space-y-2">
            {data.permission_requests.map((permission) => (
              <div key={permission.id} className="flex flex-wrap items-center gap-2 text-[12px]">
                <span className="mono rounded border px-1 py-px text-[10px]" style={{ borderColor: "var(--hairline)", color: "var(--text-muted)" }}>
                  {permission.kind}
                </span>
                <span className="mono min-w-0 break-all" style={{ color: "var(--text-primary)" }}>
                  {permission.target}
                </span>
                <span style={{ color: "var(--text-secondary)" }}>{permission.reason}</span>
                <span className="ml-auto mono text-[11px]" style={{ color: permission.status === "granted" ? "var(--status-good)" : permission.status === "denied" ? "var(--status-critical)" : "var(--status-warning)" }}>
                  {permission.status}
                  {permission.decided_by ? ` · ${permission.decided_by}` : ""}
                </span>
                {admin && permission.status === "pending" && (
                  <span className="inline-flex gap-1.5">
                    <ActionButton label="GRANT" tone="var(--status-good)" onClick={() => decide(permission.kind, permission.target, "granted")} busy={busy} />
                    <ActionButton label="DENY" tone="var(--status-critical)" onClick={() => decide(permission.kind, permission.target, "denied")} busy={busy} />
                  </span>
                )}
              </div>
            ))}
          </div>
        </Panel>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        <Panel
          title={`Audit Trail — ${data.event_count} events`}
          className="min-w-0 lg:col-span-2"
          right={
            eventPages > 1 ? (
              <span className="flex items-center gap-1.5 text-[11px]" style={{ color: "var(--text-muted)" }}>
                <button onClick={() => setEventPage(Math.max(1, eventPage - 1))} disabled={eventPage <= 1} className="disabled:opacity-30">
                  ←
                </button>
                <span className="tnum">
                  {eventPage}/{eventPages}
                </span>
                <button onClick={() => setEventPage(Math.min(eventPages, eventPage + 1))} disabled={eventPage >= eventPages} className="disabled:opacity-30">
                  →
                </button>
              </span>
            ) : undefined
          }
        >
          <div className="max-h-[560px] overflow-y-auto pr-1">{data.events.length === 0 ? <EmptyState label="no events" /> : data.events.map((event) => <EventRow key={event.id} event={event} />)}</div>
        </Panel>

        <div className="space-y-4">
          <Panel title="Usage">
            {data.usage.length === 0 ? (
              <EmptyState label="no usage recorded" />
            ) : (
              <div className="space-y-1.5">
                {data.usage.map((row, i) => (
                  <div key={i} className="flex items-center gap-2 text-[11px]">
                    <span className="mono w-20 shrink-0" style={{ color: "var(--text-muted)" }}>
                      {String(row.source)}
                    </span>
                    <ModelChip model={String(row.model)} />
                    <span className="tnum ml-auto" style={{ color: "var(--text-secondary)" }}>
                      {String(row.input_tokens ?? 0)}→{String(row.output_tokens ?? 0)} · {formatUsd(row.cost_usd as number)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </Panel>
          <Panel title="Artifacts">
            {data.artifacts.length === 0 ? (
              <EmptyState label="no artifacts" />
            ) : (
              <div className="space-y-1.5">
                {data.artifacts.map((artifact, i) => (
                  <div key={i} className="text-[12px]">
                    <span className="mono mr-2 rounded border px-1 py-px text-[10px]" style={{ borderColor: "var(--hairline)", color: "var(--text-muted)" }}>
                      {artifact.kind}
                    </span>
                    {artifact.url ? (
                      <a href={artifact.url} target="_blank" rel="noreferrer" className="underline-offset-2 hover:underline" style={{ color: "var(--accent)" }}>
                        {artifact.external_id}
                      </a>
                    ) : (
                      <span style={{ color: "var(--text-secondary)" }}>{artifact.external_id}</span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Panel>
          <Panel title="Lineage">
            {task.parent_task_id != null && String(task.parent_task_id) ? (
              <div className="mb-1.5 text-[12px]" style={{ color: "var(--text-secondary)" }}>
                parent: <TaskLink taskId={String(task.parent_task_id)} />
              </div>
            ) : null}
            {data.children.length === 0 && !task.parent_task_id ? (
              <EmptyState label="no lineage" />
            ) : (
              data.children.map((child) => (
                <div key={child.task_id} className="mb-1.5 flex items-center gap-2 text-[12px]">
                  <TaskLink taskId={child.task_id} />
                  <StatusBadge state={child.state} />
                </div>
              ))
            )}
          </Panel>
          {data.errors.length > 0 && (
            <Panel title="Errors">
              {data.errors.map((row, i) => (
                <div key={i} className="mb-2 text-[12px]">
                  <span className="mono" style={{ color: "var(--status-serious)" }}>
                    {String(row.component)}/{String(row.kind)}
                  </span>
                  <div style={{ color: "var(--text-secondary)" }}>{String(row.message ?? "").slice(0, 300)}</div>
                </div>
              ))}
            </Panel>
          )}
        </div>
      </div>

      {(data.own_memory || data.parent_memory) && (
        <div className="grid gap-4 lg:grid-cols-2">
          {data.own_memory && (
            <Panel title="Memory — this task">
              <Markdown text={data.own_memory} />
            </Panel>
          )}
          {data.parent_memory && (
            <Panel title="Memory — parent task">
              <Markdown text={data.parent_memory} />
            </Panel>
          )}
        </div>
      )}
    </div>
  );
}
