import { api, formatTokens, formatUsd, timeAgo, type TaskSlim } from "../api";
import { CapacityGauge } from "../components/charts";
import { AgentAvatar, EffortChip, EmptyState, ErrorNote, ModelChip, Panel, StatusBadge, TaskLink } from "../components/ui";
import { useLiveData } from "../stream";

const STATE_ORDER = ["running", "queued", "received", "blocked", "completed", "failed", "cancelled", "refused"];

function StatTile({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className="panel px-4 py-3">
      <div className="panel-title">{label}</div>
      <div className="tnum mt-1 text-[26px] font-bold leading-none" style={{ color: tone ?? "var(--text-primary)" }}>
        {value}
      </div>
    </div>
  );
}

function TaskRow({ task, botName, reviewerName }: { task: TaskSlim; botName: string; reviewerName: string }) {
  return (
    <div className="flex items-center gap-3 border-b py-2.5 last:border-b-0" style={{ borderColor: "var(--hairline)" }}>
      <AgentAvatar persona={task.persona} botName={botName} reviewerName={reviewerName} />
      <StatusBadge state={task.state} pulse />
      <div className="min-w-0 flex-1">
        <div className="truncate text-[13px]" style={{ color: "var(--text-primary)" }}>
          {task.request_text || "—"}
        </div>
        <div className="mt-0.5 flex items-center gap-3 text-[11px]" style={{ color: "var(--text-muted)" }}>
          <TaskLink taskId={task.task_id} />
          <span>{task.requester ?? task.slack_user_id}</span>
          <span>{timeAgo(task.created_at)}</span>
        </div>
      </div>
      <ModelChip model={task.model_alias} />
      <EffortChip effort={task.effort} />
      <span className="tnum w-14 text-right text-[12px]" style={{ color: "var(--text-secondary)" }}>
        {formatUsd(task.cost_usd)}
      </span>
    </div>
  );
}

export function MissionControl() {
  const { data, error } = useLiveData(() => api.overview());
  if (error) return <ErrorNote message={error} />;
  if (!data) return <EmptyState label="establishing uplink…" />;

  const usage = data.usage_5h;
  const tokens5h = usage.input_tokens + usage.output_tokens + usage.cache_read_tokens + usage.cache_write_tokens;

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-[18px] font-bold tracking-wide">
          MISSION CONTROL
          <span className="ml-3 text-[11px] font-semibold uppercase tracking-widest" style={{ color: "var(--text-muted)" }}>
            {data.bot_name} · {data.environment}
          </span>
        </h1>
        {data.intake_paused && (
          <span className="rounded-md border px-2.5 py-1 text-[11px] font-bold tracking-wider" style={{ borderColor: "var(--status-warning)", color: "var(--status-warning)" }}>
            ⏸ INTAKE PAUSED
          </span>
        )}
      </header>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-8">
        {STATE_ORDER.map((state) => (
          <StatTile key={state} label={state} value={data.counts[state] ?? 0} tone={state === "failed" && (data.counts[state] ?? 0) > 0 ? "var(--status-critical)" : state === "running" ? "var(--accent)" : undefined} />
        ))}
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Panel title="Active Sessions" className="lg:col-span-2">
          {data.running.length === 0 ? <EmptyState label="no sessions in flight" /> : data.running.map((task) => <TaskRow key={task.task_id} task={task} botName={data.bot_name} reviewerName={data.reviewer_name} />)}
        </Panel>
        <div className="space-y-4">
          <Panel title="Capacity">
            <CapacityGauge value={data.running.length} max={data.max_concurrency} label="concurrency slots" />
            <div className="mt-3 grid grid-cols-2 gap-2 text-center">
              <div>
                <div className="tnum text-[18px] font-bold">{formatTokens(tokens5h)}</div>
                <div className="text-[10px] uppercase tracking-widest" style={{ color: "var(--text-muted)" }}>
                  tokens · 5h
                </div>
              </div>
              <div>
                <div className="tnum text-[18px] font-bold">{formatUsd(usage.cost_usd)}</div>
                <div className="text-[10px] uppercase tracking-widest" style={{ color: "var(--text-muted)" }}>
                  cost · 5h
                </div>
              </div>
            </div>
          </Panel>
          <Panel title="Recent Errors">
            {data.errors.length === 0 ? (
              <EmptyState label="no recent errors" />
            ) : (
              <div className="space-y-2">
                {data.errors.map((row, i) => (
                  <div key={i} className="text-[12px]">
                    <span className="mono" style={{ color: "var(--status-serious)" }}>
                      {String(row.component ?? "?")}
                    </span>
                    <span className="ml-2" style={{ color: "var(--text-secondary)" }}>
                      {String(row.message ?? "").slice(0, 120)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </Panel>
        </div>
      </div>

      <Panel title="Mission Feed — latest tasks">{data.recent.length === 0 ? <EmptyState label="no tasks yet" /> : data.recent.map((task) => <TaskRow key={task.task_id} task={task} botName={data.bot_name} reviewerName={data.reviewer_name} />)}</Panel>
    </div>
  );
}
