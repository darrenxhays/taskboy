import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, timeAgo } from "../api";
import { Markdown } from "../components/Markdown";
import { EmptyState, ErrorNote, Panel, TaskLink } from "../components/ui";
import { useLiveData } from "../stream";

export function Memory() {
  const { taskId } = useParams();
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const index = useLiveData(() => api.memory(search), [search]);
  const detail = useLiveData(() => (taskId ? api.memoryDetail(taskId) : Promise.resolve(null)), [taskId]);

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-[18px] font-bold tracking-wide">MEMORY VAULT</h1>
        <form
          className="w-full sm:w-auto"
          onSubmit={(event) => {
            event.preventDefault();
            setSearch(query);
          }}
        >
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="search memories…"
            className="w-full rounded-md border px-3 py-1.5 text-[12px] outline-none sm:w-72"
            style={{ borderColor: "var(--hairline-strong)", background: "var(--surface-2)", color: "var(--text-primary)" }}
          />
        </form>
      </header>
      {index.error && <ErrorNote message={index.error} />}
      <div className="grid gap-4 lg:grid-cols-5">
        <Panel title={`Records${index.data ? ` — ${index.data.records.length}` : ""}`} className="lg:col-span-2">
          <div className="max-h-[70vh] space-y-0.5 overflow-y-auto pr-1">
            {!index.data ? (
              <EmptyState label="loading…" />
            ) : index.data.records.length === 0 ? (
              <EmptyState label="no memories yet" />
            ) : (
              index.data.records.map((record) => (
                <Link
                  key={record.task_id}
                  to={`/memory/${record.task_id}`}
                  className="block rounded-md border-l-2 px-3 py-2 transition-colors"
                  style={{
                    borderColor: taskId === record.task_id ? "var(--accent)" : "transparent",
                    background: taskId === record.task_id ? "var(--accent-dim)" : "transparent",
                  }}
                >
                  <div className="mono text-[12px]" style={{ color: taskId === record.task_id ? "var(--accent)" : "var(--text-primary)" }}>
                    {record.task_id}
                  </div>
                  <div className="mt-0.5 truncate text-[11px]" style={{ color: "var(--text-muted)" }}>
                    {record.preview.replace(/^#+\s*/gm, "").slice(0, 90)}
                  </div>
                  <div className="mt-0.5 text-[10px]" style={{ color: "var(--text-muted)" }}>
                    {timeAgo(record.modified)} · {record.size}b
                  </div>
                </Link>
              ))
            )}
          </div>
        </Panel>
        <Panel title={taskId ? `Record — ${taskId}` : "Record"} className="lg:col-span-3">
          {!taskId ? (
            <EmptyState label="select a memory record" />
          ) : detail.error ? (
            <ErrorNote message={detail.error} />
          ) : !detail.data ? (
            <EmptyState label="loading…" />
          ) : (
            <>
              <div className="mb-2 text-[11px]" style={{ color: "var(--text-muted)" }}>
                task: <TaskLink taskId={taskId} />
              </div>
              <Markdown text={detail.data.content} />
            </>
          )}
        </Panel>
      </div>
    </div>
  );
}
