import { useState } from "react";
import { Link } from "react-router-dom";
import { api, formatUsd, timeAgo } from "../api";
import { CardActions, CardHeader, CardMeta, ResponsiveTable, RowCard } from "../components/ResponsiveTable";
import { AgentAvatar, EffortChip, EmptyState, ErrorNote, ModelChip, Panel, StatusBadge, TaskLink } from "../components/ui";
import { useLiveData } from "../stream";

const STATES = ["", "received", "queued", "running", "blocked", "completed", "failed", "cancelled", "refused"];

export function Tasks() {
  const [state, setState] = useState("");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const { data, error } = useLiveData(() => api.tasks({ state: state || undefined, q: search || undefined, page }), [state, search, page]);

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-[18px] font-bold tracking-wide">TASK EXPLORER</h1>
        <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
          <select
            value={state}
            onChange={(event) => {
              setState(event.target.value);
              setPage(1);
            }}
            className="rounded-md border px-2.5 py-1.5 text-[12px] outline-none"
            style={{ borderColor: "var(--hairline-strong)", background: "var(--surface-2)", color: "var(--text-primary)" }}
          >
            {STATES.map((value) => (
              <option key={value} value={value}>
                {value || "all states"}
              </option>
            ))}
          </select>
          <form
            className="min-w-0 flex-1 sm:flex-none"
            onSubmit={(event) => {
              event.preventDefault();
              setSearch(query);
              setPage(1);
            }}
          >
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="search id or request…"
              className="w-full rounded-md border px-3 py-1.5 text-[12px] outline-none focus:border-cyan-500 sm:w-64"
              style={{ borderColor: "var(--hairline-strong)", background: "var(--surface-2)", color: "var(--text-primary)" }}
            />
          </form>
        </div>
      </header>

      {error && <ErrorNote message={error} />}
      <Panel>
        {!data ? (
          <EmptyState label="loading…" />
        ) : data.tasks.length === 0 ? (
          <EmptyState label="no matching tasks" />
        ) : (
          <ResponsiveTable
            table={
              <div className="max-w-full overflow-x-auto">
                <table className="w-full min-w-[760px] border-collapse text-left">
                <thead>
                  <tr className="text-[10px] font-semibold uppercase tracking-widest" style={{ color: "var(--text-muted)" }}>
                    <th className="pb-2 pr-3">Agent</th>
                    <th className="pb-2 pr-3">Task</th>
                    <th className="pb-2 pr-3">State</th>
                    <th className="pb-2 pr-3">Request</th>
                    <th className="pb-2 pr-3">Requester</th>
                    <th className="pb-2 pr-3">Model</th>
                    <th className="pb-2 pr-3 text-right">Cost</th>
                    <th className="pb-2 pr-3 text-right">Age</th>
                    <th className="pb-2" />

                  </tr>
                </thead>
                <tbody>
                  {data.tasks.map((task) => (
                    <tr key={task.task_id} className="border-t align-middle" style={{ borderColor: "var(--hairline)" }}>
                      <td className="py-2.5 pr-3">
                        <AgentAvatar persona={task.persona} botName={data.bot_name} reviewerName={data.reviewer_name} />
                      </td>
                      <td className="py-2.5 pr-3">
                        <TaskLink taskId={task.task_id} />
                      </td>
                      <td className="py-2.5 pr-3">
                        <StatusBadge state={task.state} pulse />
                      </td>
                      <td className="max-w-[320px] truncate py-2.5 pr-3 text-[13px]" style={{ color: "var(--text-primary)" }}>
                        {task.request_text || "—"}
                      </td>
                      <td className="py-2.5 pr-3 text-[12px]" style={{ color: "var(--text-secondary)" }}>
                        {task.requester ?? task.slack_user_id}
                      </td>
                      <td className="py-2.5 pr-3">
                        <span className="inline-flex items-center gap-1.5">
                          <ModelChip model={task.model_alias} />
                          <EffortChip effort={task.effort} />
                        </span>
                      </td>
                      <td className="tnum py-2.5 pr-3 text-right text-[12px]" style={{ color: "var(--text-secondary)" }}>
                        {formatUsd(task.cost_usd)}
                      </td>
                      <td className="py-2.5 pr-3 text-right text-[12px]" style={{ color: "var(--text-muted)" }}>
                        {timeAgo(task.created_at)}
                      </td>
                      <td className="py-2.5 text-right">
                        <Link to={`/tasks/${task.task_id}/feedback`} title="give feedback" className="text-[13px] underline-offset-2 hover:underline" style={{ color: "var(--status-warning)" }}>
                          ☆
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
                </table>
              </div>
            }
            cards={data.tasks.map((task) => (
              <RowCard key={task.task_id}>
                <CardHeader>
                  <div className="flex min-w-0 items-center gap-2">
                    <AgentAvatar persona={task.persona} botName={data.bot_name} reviewerName={data.reviewer_name} />
                    <TaskLink taskId={task.task_id} />
                  </div>
                  <StatusBadge state={task.state} pulse />
                </CardHeader>
                <div className="mt-2 text-[13px]" style={{ color: "var(--text-primary)" }}>
                  {task.request_text || "—"}
                </div>
                <CardMeta>
                  <span>{task.requester ?? task.slack_user_id}</span>
                  <ModelChip model={task.model_alias} />
                  <EffortChip effort={task.effort} />
                  <span className="tnum">{formatUsd(task.cost_usd)}</span>
                  <span>{timeAgo(task.created_at)}</span>
                </CardMeta>
                <CardActions>
                  <Link to={`/tasks/${task.task_id}/feedback`} className="rounded border px-3 py-1.5 text-[12px] font-semibold" style={{ borderColor: "var(--status-warning)", color: "var(--status-warning)" }}>
                    ☆ feedback
                  </Link>
                </CardActions>
              </RowCard>
            ))}
          />
        )}
        <div className="mt-3 flex items-center justify-end gap-2">
          <button
            onClick={() => setPage(Math.max(1, page - 1))}
            disabled={page <= 1}
            className="rounded-md border px-2.5 py-1 text-[12px] disabled:opacity-40"
            style={{ borderColor: "var(--hairline-strong)", color: "var(--text-secondary)" }}
          >
            ← prev
          </button>
          <span className="tnum text-[12px]" style={{ color: "var(--text-muted)" }}>
            page {page}
          </span>
          <button
            onClick={() => setPage(page + 1)}
            disabled={!data || data.tasks.length < data.page_size}
            className="rounded-md border px-2.5 py-1 text-[12px] disabled:opacity-40"
            style={{ borderColor: "var(--hairline-strong)", color: "var(--text-secondary)" }}
          >
            next →
          </button>
        </div>
      </Panel>
    </div>
  );
}
