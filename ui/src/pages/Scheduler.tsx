import { useState } from "react";
import type { Schedule, ScheduleInput } from "../api";
import { api, EFFORT_LEVELS, timeAgo } from "../api";
import { CardActions, CardHeader, CardMeta, ResponsiveTable, RowCard } from "../components/ResponsiveTable";
import { CheckIcon, ConfirmIconButton, EffortChip, EmptyState, ErrorNote, IconButton, Panel, PauseIcon, PencilIcon, PlayIcon, TaskLink, TrashIcon } from "../components/ui";
import { useLiveData } from "../stream";

const GUESS_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Los_Angeles";

function whenLabel(s: Schedule): string {
  if (s.kind === "interval") return `every ${s.interval_minutes} min`;
  if (s.kind === "daily") return `daily at ${s.at_time}${s.timezone ? ` ${s.timezone}` : " UTC"}`;
  return "once";
}

function runsLabel(s: Schedule): string {
  return s.max_runs == null ? `${s.run_count} / ∞` : `${s.run_count} / ${s.max_runs}`;
}

function fmt(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function StatusDot({ enabled }: { enabled: number | boolean }) {
  return <span aria-hidden="true" className="mr-1.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full align-middle" style={{ background: enabled ? "var(--status-good)" : "var(--text-muted)" }} />;
}

type Draft = ScheduleInput & { kind: "once" | "interval" | "daily" };

const BLANK: Draft = { name: "", request_text: "", model_alias: "", effort: "", kind: "daily", interval_minutes: 30, at_time: "13:00", run_at: "", timezone: GUESS_TZ, max_runs: null };

function draftFrom(s: Schedule): Draft {
  return {
    name: s.name,
    request_text: s.request_text,
    model_alias: s.model_alias ?? "",
    effort: s.effort ?? "",
    kind: s.kind,
    interval_minutes: s.interval_minutes ?? 30,
    at_time: s.at_time ?? "13:00",
    run_at: s.run_at ? new Date(s.run_at).toISOString().slice(0, 16) : "",
    timezone: s.timezone ?? GUESS_TZ,
    max_runs: s.max_runs,
  };
}

export function Scheduler({ admin }: { admin: boolean }) {
  const { data, error, reload } = useLiveData(() => api.schedules(), []);
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const [draft, setDraft] = useState<Draft>(BLANK);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const models = data?.models ?? [];

  const openNew = () => {
    setDraft(BLANK);
    setEditing("new");
    setNote(null);
  };
  const openEdit = (s: Schedule) => {
    setDraft(draftFrom(s));
    setEditing(s.id);
    setNote(null);
  };

  const payload = (): ScheduleInput => ({
    name: draft.name,
    request_text: draft.request_text,
    model_alias: draft.model_alias || null,
    effort: draft.effort || null,
    kind: draft.kind,
    interval_minutes: draft.kind === "interval" ? Number(draft.interval_minutes) : null,
    at_time: draft.kind === "daily" ? draft.at_time : null,
    run_at: draft.kind === "once" ? draft.run_at : null,
    timezone: draft.timezone || null,
    max_runs: draft.max_runs == null || (draft.max_runs as unknown as string) === "" ? null : Number(draft.max_runs),
  });

  const save = async () => {
    setBusy(true);
    setNote(null);
    try {
      if (editing === "new") await api.createSchedule(payload());
      else if (typeof editing === "number") await api.updateSchedule(editing, payload());
      setEditing(null);
      reload();
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setNote(null);
    try {
      await fn();
      reload();
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-[18px] font-bold tracking-wide">SCHEDULER</h1>
        {admin && (
          <button onClick={openNew} className="rounded-md px-3 py-1.5 text-[12px] font-bold text-white disabled:opacity-40" style={{ background: "var(--accent)" }} disabled={busy}>
            NEW SCHEDULE
          </button>
        )}
      </header>

      {note && (
        <div className="text-[12px]" style={{ color: "var(--status-serious)" }}>
          {note}
        </div>
      )}
      {error && <ErrorNote message={error} />}

      {editing !== null && admin && <ScheduleForm draft={draft} setDraft={setDraft} models={models} onSave={save} onCancel={() => setEditing(null)} busy={busy} isNew={editing === "new"} />}

      <Panel>
        {!data ? (
          <EmptyState label="loading…" />
        ) : data.schedules.length === 0 ? (
          <EmptyState label="no schedules yet" />
        ) : (
          <ResponsiveTable
            table={
              <div className="max-w-full overflow-x-auto">
                <table className="w-full min-w-[860px] border-collapse text-left">
                <thead>
                  <tr className="text-[10px] font-semibold uppercase tracking-widest" style={{ color: "var(--text-muted)" }}>
                    <th className="pb-2 pr-3">Name</th>
                    <th className="pb-2 pr-3">When</th>
                    <th className="pb-2 pr-3">Request</th>
                    <th className="pb-2 pr-3">Model</th>
                    <th className="pb-2 pr-3">Effort</th>
                    <th className="pb-2 pr-3">Runs</th>
                    <th className="pb-2 pr-3">Next run</th>
                    <th className="pb-2 pr-3">Last</th>
                    <th className="pb-2 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {data.schedules.map((s) => (
                    <tr key={s.id} className="border-t align-middle" style={{ borderColor: "var(--hairline)", opacity: s.enabled ? 1 : 0.5 }}>
                      <td className="py-2.5 pr-3 text-[13px] font-medium" style={{ color: "var(--text-primary)" }}><StatusDot enabled={s.enabled} />{s.name}</td>
                      <td className="py-2.5 pr-3 text-[12px]" style={{ color: "var(--text-secondary)" }}>{whenLabel(s)}</td>
                      <td className="mono max-w-[220px] truncate py-2.5 pr-3 text-[12px]" style={{ color: "var(--text-secondary)" }}>{s.request_text}</td>
                      <td className="mono py-2.5 pr-3 text-[11px]" style={{ color: "var(--text-secondary)" }}>{s.model_alias || "auto"}</td>
                      <td className="py-2.5 pr-3">{s.effort ? <EffortChip effort={s.effort} /> : <span className="text-[11px]" style={{ color: "var(--text-muted)" }}>auto</span>}</td>
                      <td className="tnum py-2.5 pr-3 text-[12px]" style={{ color: "var(--text-secondary)" }}>{runsLabel(s)}</td>
                      <td className="py-2.5 pr-3 text-[12px]" style={{ color: "var(--text-secondary)" }}>{s.enabled ? fmt(s.next_run_at) : "—"}</td>
                      <td className="py-2.5 pr-3 text-[12px]" style={{ color: "var(--text-muted)" }}>
                        {s.last_run_at ? timeAgo(s.last_run_at) : "—"} {s.last_task_id && <TaskLink taskId={s.last_task_id} />}
                      </td>
                      <td className="py-2.5 text-right">
                        {admin ? (
                          <span className="inline-flex gap-1.5">
                            <IconButton onClick={() => act(() => api.updateSchedule(s.id, { enabled: !s.enabled }))} disabled={busy} title={s.enabled ? "pause" : "enable"} borderColor={s.enabled ? "var(--hairline-strong)" : "var(--status-good)"} color={s.enabled ? "var(--text-muted)" : "var(--status-good)"}>{s.enabled ? <PauseIcon /> : <CheckIcon />}</IconButton>
                            <IconButton onClick={() => act(() => api.runSchedule(s.id))} disabled={busy} title="run" borderColor="var(--accent)" color="var(--accent)"><PlayIcon /></IconButton>
                            <IconButton onClick={() => openEdit(s)} disabled={busy} title="edit" borderColor="var(--hairline-strong)" color="var(--text-secondary)"><PencilIcon /></IconButton>
                            <ConfirmIconButton onConfirm={() => act(() => api.deleteSchedule(s.id))} disabled={busy} title="delete" borderColor="var(--hairline-strong)" color="var(--text-muted)"><TrashIcon /></ConfirmIconButton>
                          </span>
                        ) : (
                          <span className="text-[11px]" style={{ color: "var(--text-muted)" }}>{s.enabled ? "active" : "paused"}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
                </table>
              </div>
            }
            cards={data.schedules.map((s) => (
              <RowCard key={s.id} className={s.enabled ? "" : "opacity-50"}>
                <CardHeader>
                  <span className="text-[13px] font-medium" style={{ color: "var(--text-primary)" }}><StatusDot enabled={s.enabled} />{s.name}</span>
                  <span className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>{whenLabel(s)}</span>
                </CardHeader>
                <div className="mono mt-2 truncate text-[12px]" style={{ color: "var(--text-secondary)" }}>{s.request_text}</div>
                <CardMeta>
                  <span className="mono">{s.model_alias || "auto"}</span>
                  {s.effort && <EffortChip effort={s.effort} />}
                  <span className="tnum">{runsLabel(s)} runs</span>
                  <span>next {s.enabled ? fmt(s.next_run_at) : "—"}</span>
                  <span>last {s.last_run_at ? timeAgo(s.last_run_at) : "—"} {s.last_task_id && <TaskLink taskId={s.last_task_id} />}</span>
                </CardMeta>
                <CardActions>
                  {admin ? (
                    <>
                      <IconButton onClick={() => act(() => api.updateSchedule(s.id, { enabled: !s.enabled }))} disabled={busy} title={s.enabled ? "pause" : "enable"} borderColor={s.enabled ? "var(--hairline-strong)" : "var(--status-good)"} color={s.enabled ? "var(--text-muted)" : "var(--status-good)"}>{s.enabled ? <PauseIcon /> : <CheckIcon />}</IconButton>
                      <IconButton onClick={() => act(() => api.runSchedule(s.id))} disabled={busy} title="run" borderColor="var(--accent)" color="var(--accent)"><PlayIcon /></IconButton>
                      <IconButton onClick={() => openEdit(s)} disabled={busy} title="edit" borderColor="var(--hairline-strong)" color="var(--text-secondary)"><PencilIcon /></IconButton>
                      <ConfirmIconButton onConfirm={() => act(() => api.deleteSchedule(s.id))} disabled={busy} title="delete" borderColor="var(--hairline-strong)" color="var(--text-muted)"><TrashIcon /></ConfirmIconButton>
                    </>
                  ) : (
                    <span className="text-[11px]" style={{ color: "var(--text-muted)" }}>{s.enabled ? "active" : "paused"}</span>
                  )}
                </CardActions>
              </RowCard>
            ))}
          />
        )}
      </Panel>
    </div>
  );
}

function ScheduleForm({ draft, setDraft, models, onSave, onCancel, busy, isNew }: { draft: Draft; setDraft: (d: Draft) => void; models: string[]; onSave: () => void; onCancel: () => void; busy: boolean; isNew: boolean }) {
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch });
  const field = "rounded-md border px-2.5 py-1.5 text-[13px] outline-none focus:border-cyan-500";
  const fieldStyle = { borderColor: "var(--hairline-strong)", background: "var(--surface-2)", color: "var(--text-primary)" };
  const label = "text-[10px] font-semibold uppercase tracking-widest";
  return (
    <Panel title={isNew ? "New schedule" : "Edit schedule"}>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1">
          <span className={label} style={{ color: "var(--text-muted)" }}>Name</span>
          <input className={field} style={fieldStyle} value={draft.name} onChange={(e) => set({ name: e.target.value })} placeholder="Nightly discovery" />
        </label>
        <label className="flex flex-col gap-1">
          <span className={label} style={{ color: "var(--text-muted)" }}>Model</span>
          <select className={field} style={fieldStyle} value={draft.model_alias ?? ""} onChange={(e) => set({ model_alias: e.target.value })}>
            <option value="">auto (orchestrator decides)</option>
            {models.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className={label} style={{ color: "var(--text-muted)" }}>Effort</span>
          <select className={field} style={fieldStyle} value={draft.effort ?? ""} onChange={(e) => set({ effort: e.target.value })}>
            <option value="">auto (profile decides)</option>
            {EFFORT_LEVELS.map((level) => (
              <option key={level} value={level}>{level}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 sm:col-span-2">
          <span className={label} style={{ color: "var(--text-muted)" }}>Request (what to run — e.g. /discoverissues owner/repo or a plain instruction)</span>
          <input className={field} style={fieldStyle} value={draft.request_text} onChange={(e) => set({ request_text: e.target.value })} placeholder="/discoverissues owner/repo" />
        </label>
        <label className="flex flex-col gap-1">
          <span className={label} style={{ color: "var(--text-muted)" }}>Kind</span>
          <select className={field} style={fieldStyle} value={draft.kind} onChange={(e) => set({ kind: e.target.value as Draft["kind"] })}>
            <option value="daily">daily at a time</option>
            <option value="interval">every N minutes</option>
            <option value="once">once at a date/time</option>
          </select>
        </label>
        {draft.kind === "interval" && (
          <label className="flex flex-col gap-1">
            <span className={label} style={{ color: "var(--text-muted)" }}>Every (minutes)</span>
            <input type="number" min={1} className={field} style={fieldStyle} value={draft.interval_minutes ?? 30} onChange={(e) => set({ interval_minutes: Number(e.target.value) })} />
          </label>
        )}
        {draft.kind === "daily" && (
          <label className="flex flex-col gap-1">
            <span className={label} style={{ color: "var(--text-muted)" }}>At (HH:MM)</span>
            <input type="time" className={field} style={fieldStyle} value={draft.at_time ?? "13:00"} onChange={(e) => set({ at_time: e.target.value })} />
          </label>
        )}
        {draft.kind === "once" && (
          <label className="flex flex-col gap-1">
            <span className={label} style={{ color: "var(--text-muted)" }}>Date & time</span>
            <input type="datetime-local" className={field} style={fieldStyle} value={draft.run_at ?? ""} onChange={(e) => set({ run_at: e.target.value })} />
          </label>
        )}
        {draft.kind !== "interval" && (
          <label className="flex flex-col gap-1">
            <span className={label} style={{ color: "var(--text-muted)" }}>Timezone (IANA)</span>
            <input className={field} style={fieldStyle} value={draft.timezone ?? ""} onChange={(e) => set({ timezone: e.target.value })} placeholder="America/Los_Angeles" />
          </label>
        )}
        {draft.kind !== "once" && (
          <label className="flex flex-col gap-1">
            <span className={label} style={{ color: "var(--text-muted)" }}>Max runs (blank = forever)</span>
            <input type="number" min={1} className={field} style={fieldStyle} value={draft.max_runs ?? ""} onChange={(e) => set({ max_runs: e.target.value === "" ? null : Number(e.target.value) })} />
          </label>
        )}
      </div>
      <div className="mt-3 flex items-center gap-2">
        <button onClick={onSave} disabled={busy || !draft.name || !draft.request_text} className="rounded-md px-4 py-1.5 text-[12px] font-bold text-white disabled:opacity-40" style={{ background: "var(--accent)" }}>
          {isNew ? "CREATE" : "SAVE"}
        </button>
        <button onClick={onCancel} disabled={busy} className="rounded-md border px-4 py-1.5 text-[12px] font-semibold" style={{ borderColor: "var(--hairline-strong)", color: "var(--text-secondary)" }}>
          CANCEL
        </button>
      </div>
    </Panel>
  );
}
