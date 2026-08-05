import { useEffect, useMemo, useState, type ChangeEvent, type ReactNode } from "react";
import type { Issue, IssueAttachment, IssueComment, IssueDetail } from "../api";
import { api, timeAgo } from "../api";
import { Markdown } from "../components/Markdown";
import { MarkdownEditor } from "../components/MarkdownEditor";
import { CardActions, CardHeader, CardMeta, ResponsiveTable, RowCard } from "../components/ResponsiveTable";
import { CheckIcon, ConfirmIconButton, EmptyState, ErrorNote, IconButton, Panel, RefreshIcon, TablePager, TaskLink, TrashIcon, XIcon } from "../components/ui";
import { useLiveData } from "../stream";

const ACTIVE_STATUSES = ["proposed", "approved", "implementation_queued", "failed"];
const ARCHIVE_STATUSES = ["done", "denied"];
const NO_REFINE_STATUSES = ["in_progress", "done", "denied"];
const NO_DELETE_STATUSES = ["in_progress", "in_review"]; // backend blocks these with 409 to avoid orphaning an active implementation
const SUGGESTED_TYPES = ["feature_request", "bug", "security", "user_experience", "reliability", "performance", "token_efficiency", "organization"];
const STATUS_COLOR: Record<string, string> = { proposed: "var(--status-warning)", approved: "var(--status-good)", implementation_queued: "var(--accent)", in_progress: "var(--accent)", in_review: "var(--accent)", done: "var(--status-good)", failed: "var(--status-critical)", denied: "var(--status-neutral)" };
const fieldClass = "rounded-md border px-2.5 py-1.5 text-[13px] outline-none focus:border-cyan-500";
const fieldStyle = { borderColor: "var(--hairline-strong)", background: "var(--surface-2)", color: "var(--text-primary)" };

function canRefine(status: string): boolean {
  return !NO_REFINE_STATUSES.includes(status);
}

function canDelete(status: string): boolean {
  return !NO_DELETE_STATUSES.includes(status);
}

// tolerate missing/malformed persisted state
function readStringList(key: string): string[] {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((value): value is string => typeof value === "string") : [];
  } catch {
    return [];
  }
}

function writeStringList(key: string, value: string[]) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable — persistence is a nice-to-have, not required for the page to work */
  }
}

function readSelectedRepos(): string[] | null {
  try {
    const raw = localStorage.getItem("issues:selectedRepos");
    if (raw == null) return null;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((value): value is string => typeof value === "string") : null;
  } catch {
    return null;
  }
}

function readBool(key: string): boolean {
  return localStorage.getItem(key) === "1";
}

function writeBool(key: string, value: boolean) {
  try {
    localStorage.setItem(key, value ? "1" : "0");
  } catch {
    /* ignore */
  }
}

type Column = "rank" | "summary" | "repo" | "type" | "status" | "added" | "updated";
type Direction = "asc" | "desc";

function sortRows(rows: Issue[], column: Column, direction: Direction): Issue[] {
  const copy = [...rows];
  if (column === "summary") copy.sort((a, b) => a.summary.localeCompare(b.summary, undefined, { sensitivity: "base" }));
  if (column === "repo") copy.sort((a, b) => a.repo.localeCompare(b.repo, undefined, { sensitivity: "base" }));
  if (column === "type") copy.sort((a, b) => a.issue_type.localeCompare(b.issue_type, undefined, { sensitivity: "base" }));
  if (column === "status") copy.sort((a, b) => a.status.localeCompare(b.status, undefined, { sensitivity: "base" }));
  if (column === "added") copy.sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id - b.id);
  if (column === "updated") copy.sort((a, b) => a.updated_at.localeCompare(b.updated_at) || a.id - b.id);
  if (direction === "desc") copy.reverse();
  return copy;
}

function SortHeader({ label, column, active, direction, onClick, align, className }: { label: string; column: Column; active: boolean; direction: Direction; onClick: (column: Column) => void; align?: "right"; className?: string }) {
  return <th className={`pb-2 pr-3 ${align === "right" ? "text-right" : ""} ${className ?? ""}`}><button onClick={() => onClick(column)} className={`inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-widest ${align === "right" ? "flex-row-reverse" : ""}`} style={{ color: active ? "var(--text-secondary)" : "var(--text-muted)" }}>{label}{active && <span aria-hidden="true">{direction === "asc" ? "▲" : "▼"}</span>}</button></th>;
}

function StatusPill({ status }: { status: string }) {
  return <span className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider" style={{ borderColor: "var(--hairline)", color: "var(--text-secondary)" }}><span className="h-1.5 w-1.5 rounded-full" style={{ background: STATUS_COLOR[status] ?? "var(--status-neutral)" }} />{status.replaceAll("_", " ")}</span>;
}

function Chip({ children }: { children: string }) {
  return <span className="mono inline-block max-w-48 truncate rounded border px-1.5 py-0.5 text-[10px]" title={children} style={{ borderColor: "var(--hairline-strong)", color: "var(--text-secondary)" }}>{children}</span>;
}

// FilterOverlay renders an anchored dropdown at `md` and up (matching the pre-existing
// desktop layout) and a full-screen scrim + bottom-sheet panel below `md`, so multi-select
// filter panels never overflow off the left edge of a narrow viewport. Esc and the scrim (or
// the mobile-only close button) all dismiss it.
function FilterOverlay({ open, onClose, label, panelClassName, children }: { open: boolean; onClose: () => void; label: string; panelClassName?: string; children: ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <>
      <button aria-label={`close ${label}`} className="fixed inset-0 z-40 bg-black/60 md:z-10 md:bg-transparent" onClick={onClose} />
      <div
        className={`fixed inset-x-3 bottom-3 z-50 max-h-[75vh] overflow-y-auto rounded-md border p-3 shadow-xl md:absolute md:inset-x-auto md:inset-y-auto md:bottom-auto md:right-0 md:z-20 md:mt-1 md:p-2 md:shadow-lg ${panelClassName ?? ""}`}
        style={{ borderColor: "var(--hairline-strong)", background: "var(--surface-2)" }}
      >
        <div className="mb-2 flex items-center justify-between md:hidden">
          <span className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>{label}</span>
          <button onClick={onClose} aria-label={`close ${label}`} className="flex h-7 w-7 items-center justify-center rounded-full border text-[13px]" style={{ borderColor: "var(--hairline-strong)", color: "var(--text-secondary)" }}>✕</button>
        </div>
        {children}
      </div>
    </>
  );
}

function RepoFilter({ repos, selected, onChange }: { repos: string[]; selected: string[]; onChange: (repos: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const toggle = (repo: string) => onChange(selected.includes(repo) ? selected.filter((value) => value !== repo) : [...selected, repo]);
  return (
    <div className="relative">
      <button onClick={() => setOpen(!open)} className="rounded-md border px-3 py-1.5 text-[12px] font-semibold" style={{ borderColor: selected.length === repos.length ? "var(--hairline-strong)" : "var(--accent)", color: "var(--text-secondary)", background: "var(--surface-2)" }}>REPOS ({selected.length}/{repos.length})</button>
      <FilterOverlay open={open} onClose={() => setOpen(false)} label="repo filter" panelClassName="md:max-h-80 md:w-[min(22rem,90vw)]">
        <div className="mb-2 flex flex-wrap gap-2"><button onClick={() => onChange(repos)} className="text-[11px] font-semibold" style={{ color: "var(--accent)" }}>Select All</button><button onClick={() => onChange([])} className="text-[11px] font-semibold" style={{ color: "var(--accent)" }}>Clear All</button></div>
        {repos.map((repo) => <div key={repo} className="flex items-center gap-2 px-1 py-1 text-[12px]"><input type="checkbox" checked={selected.includes(repo)} onChange={() => toggle(repo)} className="accent-cyan-500" /><button onClick={() => onChange([repo])} className="min-w-0 truncate text-left hover:underline" title={`show only ${repo}`}>{repo}</button></div>)}
      </FilterOverlay>
    </div>
  );
}

function TypeFilter({ rows, statuses, types, onChange }: { rows: Issue[]; statuses: string[]; types: string[]; onChange: (statuses: string[], types: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const options = [...new Set(rows.map((row) => row.issue_type))].sort();
  const toggle = (list: string[], value: string) => list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
  return <div className="relative"><button onClick={() => setOpen(!open)} className="rounded-md border px-2.5 py-1.5 text-[12px] font-semibold" style={{ borderColor: statuses.length + types.length ? "var(--accent)" : "var(--hairline-strong)", color: "var(--text-secondary)" }}>FILTER{statuses.length + types.length ? ` (${statuses.length + types.length})` : ""}</button>
    <FilterOverlay open={open} onClose={() => setOpen(false)} label="filter" panelClassName="md:w-56">
      <div className="mb-1 text-[10px] font-semibold uppercase" style={{ color: "var(--text-muted)" }}>Status</div>{[...new Set(rows.map((row) => row.status))].map((value) => <label key={value} className="flex gap-2 py-1 text-[12px]"><input type="checkbox" checked={statuses.includes(value)} onChange={() => onChange(toggle(statuses, value), types)} />{value.replaceAll("_", " ")}</label>)}
      <div className="mb-1 mt-2 text-[10px] font-semibold uppercase" style={{ color: "var(--text-muted)" }}>Type</div>{options.map((value) => <label key={value} className="flex gap-2 py-1 text-[12px]"><input type="checkbox" checked={types.includes(value)} onChange={() => onChange(statuses, toggle(types, value))} />{value}</label>)}
      <button onClick={() => onChange([], [])} className="mt-2 text-[11px] font-semibold" style={{ color: "var(--accent)" }}>clear filters</button>
    </FilterOverlay>
  </div>;
}

function CreateForm({ repos, knownTypes, defaultRepo, onRepoChange, onCreated, onCancel }: { repos: string[]; knownTypes: string[]; defaultRepo: string; onRepoChange: (repo: string) => void; onCreated: () => void; onCancel: () => void }) {
  const [repo, setRepo] = useState(defaultRepo || repos[0] || "");
  const [summary, setSummary] = useState("");
  const [issueType, setIssueType] = useState("");
  const [details, setDetails] = useState("");
  const [priority, setPriority] = useState(50);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { if (!repo && repos.length) setRepo(repos[0]); }, [repo, repos]);
  const changeRepo = (value: string) => { setRepo(value); onRepoChange(value); };
  const save = async () => { setBusy(true); setError(null); try { await api.createIssue({ repo, summary: summary.trim(), issue_type: issueType.trim(), details: details.trim(), priority }); onCreated(); } catch (reason) { setError((reason as Error).message); setBusy(false); } };
  return <Panel title="New issue"><div className="grid grid-cols-1 gap-3 sm:grid-cols-2"><label className="flex flex-col gap-1"><span className="panel-title">Repo</span><select value={repo} onChange={(event) => changeRepo(event.target.value)} className={fieldClass} style={fieldStyle}>{repos.map((value) => <option key={value}>{value}</option>)}</select></label><label className="flex flex-col gap-1"><span className="panel-title">Type</span><input value={issueType} onChange={(event) => setIssueType(event.target.value)} list="issue-types" className={fieldClass} style={fieldStyle} placeholder="feature_request" /><datalist id="issue-types">{[...new Set([...SUGGESTED_TYPES, ...knownTypes])].sort().map((value) => <option key={value} value={value} />)}</datalist></label><label className="flex flex-col gap-1 sm:col-span-2"><span className="panel-title">Summary</span><input value={summary} onChange={(event) => setSummary(event.target.value)} className={fieldClass} style={fieldStyle} placeholder="One-line issue title" /></label><label className="sm:col-span-2"><span className="panel-title mb-1 block">Description</span><MarkdownEditor value={details} onChange={setDetails} placeholder="What should change, why, and how to verify it." /></label><label className="flex flex-col gap-1"><span className="panel-title">Priority: {priority}</span><input type="range" min={1} max={100} value={priority} onChange={(event) => setPriority(Number(event.target.value))} /></label></div>{error && <div className="mt-3"><ErrorNote message={error} /></div>}<div className="mt-3 flex flex-wrap gap-2"><button onClick={save} disabled={busy || !repo || !summary.trim() || !issueType.trim() || !details.trim()} className="rounded-md px-4 py-1.5 text-[12px] font-bold text-white disabled:opacity-40" style={{ background: "var(--accent)" }}>CREATE</button><button onClick={onCancel} disabled={busy} className="rounded-md border px-4 py-1.5 text-[12px]" style={{ borderColor: "var(--hairline-strong)" }}>CANCEL</button></div></Panel>;
}

export function Issues({ admin, email }: { admin: boolean; email: string | null }) {
  const { data, error, reload } = useLiveData(() => api.issues(), []);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [selectedRepos, setSelectedReposState] = useState<string[] | null>(() => readSelectedRepos());
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const repos = data?.repos ?? [];
  const setSelectedRepos = (next: string[]) => { setSelectedReposState(next); writeStringList("issues:selectedRepos", next); };
  // guard against repos that were removed since the last visit
  useEffect(() => {
    if (selectedRepos && repos.length) {
      const filtered = selectedRepos.filter((repo) => repos.includes(repo));
      if (filtered.length !== selectedRepos.length) setSelectedRepos(filtered);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repos]);
  const effectiveRepos = selectedRepos ?? repos;
  const rows = useMemo(() => (data?.issues ?? []).filter((row) => effectiveRepos.includes(row.repo)), [data, effectiveRepos]);
  const knownTypes = useMemo(() => [...new Set((data?.issues ?? []).map((row) => row.issue_type))].sort(), [data]);
  const [discoveryRepo, setDiscoveryRepoState] = useState(() => localStorage.getItem("issues:lastRepo") ?? "");
  const setDiscoveryRepo = (repo: string) => { setDiscoveryRepoState(repo); if (repo) { try { localStorage.setItem("issues:lastRepo", repo); } catch { /* ignore */ } } };
  // guard against a stale/missing last-used repo once the real repo list has loaded
  useEffect(() => { if (repos.length && !repos.includes(discoveryRepo)) setDiscoveryRepo(repos[0]); }, [discoveryRepo, repos]);

  const mutate = async (work: () => Promise<unknown>, success?: string) => { setBusy(true); setNote(null); try { await work(); if (success) setNote(success); reload(); return true; } catch (reason) { setNote((reason as Error).message); return false; } finally { setBusy(false); } };
  const decide = (id: number, decision: "approved" | "denied") => mutate(() => api.decideIssue(id, (data?.issues.find((row) => row.id === id)?.status === decision ? "proposed" : decision)), undefined);
  const refine = (id: number) => mutate(() => api.refineIssue(id), `refine task started for issue #${id}`);
  const remove = (id: number) => mutate(() => api.deleteIssue(id), `issue #${id} deleted`);
  const bulk = async (action: "approve" | "deny" | "refine") => { let ids = [...selected]; if (action === "refine") ids = ids.filter((id) => { const row = data?.issues.find((item) => item.id === id); return row != null && canRefine(row.status); }); if (!ids.length) return; try { await mutate(async () => { const result = await api.bulkIssues(ids, action); const successes = result.results.filter((row) => !["skipped", "not_found", "already_running"].includes(row.status)).length; setNote(`${action}: ${successes} succeeded, ${result.results.length - successes} skipped`); setSelected(new Set()); }); } catch { /* note is set by mutate */ } };
  const run = async (skill: "discoverissues" | "implementapprovedissues") => { try { await mutate(async () => { const result = await api.runIssues(skill, skill === "discoverissues" ? discoveryRepo : undefined); if (result.status === "created") setNote(`${skill === "discoverissues" ? "discovery" : "implementation"} started (${result.task_id})`); else if (result.status === "no_approved_issues") setNote("no approved issues to implement"); else setNote(`${result.status}${result.task_id ? ` (${result.task_id})` : ""}`); }); } catch { /* note is set by mutate */ } };

  return <div className="mx-auto max-w-7xl space-y-4 overflow-x-hidden"><header className="flex flex-wrap items-center justify-between gap-3"><h1 className="text-[18px] font-bold tracking-wide">ISSUES</h1><div className="flex flex-wrap items-center gap-2"><RepoFilter repos={repos} selected={effectiveRepos} onChange={setSelectedRepos} />{admin && <><button onClick={() => setCreating(true)} disabled={busy || creating} className="rounded-md px-3 py-1.5 text-[12px] font-bold text-white disabled:opacity-40" style={{ background: "var(--accent)" }}>CREATE</button><div className="flex flex-wrap items-center gap-1"><select value={discoveryRepo} onChange={(event) => setDiscoveryRepo(event.target.value)} className="max-w-56 rounded-md border px-2 py-1.5 text-[12px]" style={fieldStyle}>{repos.map((repo) => <option key={repo}>{repo}</option>)}</select><button onClick={() => run("discoverissues")} disabled={busy || !discoveryRepo} className="rounded-md border px-3 py-1.5 text-[12px] font-bold disabled:opacity-40" style={{ borderColor: "var(--accent)", color: "var(--accent)" }}>RUN DISCOVERY</button></div><button onClick={() => run("implementapprovedissues")} disabled={busy || data?.implementation_active != null} className="rounded-md border px-3 py-1.5 text-[12px] font-bold disabled:opacity-40" style={{ borderColor: "var(--status-good)", color: "var(--status-good)" }}>{data?.implementation_active ? "IMPLEMENTING" : "IMPLEMENT APPROVED"}</button></>}</div></header>
    {data?.implementation_active && <div className="text-[11px]" style={{ color: "var(--text-muted)" }}>implementation run: <TaskLink taskId={data.implementation_active} /></div>}{note && <div className="text-[12px]" style={{ color: "var(--text-secondary)" }}>{note}</div>}{error && <ErrorNote message={error} />}
    {creating && admin && <CreateForm repos={repos} knownTypes={knownTypes} defaultRepo={discoveryRepo} onRepoChange={setDiscoveryRepo} onCreated={() => { setCreating(false); setNote("Issue created."); reload(); }} onCancel={() => setCreating(false)} />}
    {admin && selected.size > 0 && <div className="sticky top-0 z-[5] flex flex-wrap items-center gap-2 rounded-md border p-3" style={{ borderColor: "var(--accent)", background: "var(--surface-1)" }}><span className="text-[12px] font-semibold">{selected.size} selected</span>{(["approve", "deny", "refine"] as const).map((action) => <IconButton key={action} onClick={() => bulk(action)} disabled={busy} title={action} borderColor={action === "approve" ? "var(--status-good)" : action === "deny" ? "var(--status-critical)" : "var(--accent)"} color={action === "approve" ? "var(--status-good)" : action === "deny" ? "var(--status-serious)" : "var(--accent)"}>{action === "approve" ? <CheckIcon /> : action === "deny" ? <XIcon /> : <RefreshIcon />}</IconButton>)}<button onClick={() => setSelected(new Set())} className="text-[11px]" style={{ color: "var(--text-muted)" }}>clear</button></div>}
    <IssueTable title="In progress" rows={rows.filter((row) => row.status === "in_review" || row.status === "in_progress")} loaded={data != null} empty="nothing in progress" tableKey="in-review" showRank={false} {...{ admin, email, busy, selected, setSelected, decide, refine, remove, reload, setNote }} />
    <IssueTable title="Active" rows={rows.filter((row) => ACTIVE_STATUSES.includes(row.status))} loaded={data != null} empty={effectiveRepos.length ? "no active issues" : "select at least one repo"} tableKey="active" showRank {...{ admin, email, busy, selected, setSelected, decide, refine, remove, reload, setNote }} />
    <IssueTable title="Done / Denied" rows={rows.filter((row) => ARCHIVE_STATUSES.includes(row.status))} loaded={data != null} empty="nothing finished or denied" tableKey="archive" showRank={false} {...{ admin, email, busy, selected, setSelected, decide, refine, remove, reload, setNote }} />
  </div>;
}

type TableProps = { title: string; rows: Issue[]; loaded: boolean; empty: string; tableKey: string; showRank: boolean; admin: boolean; email: string | null; busy: boolean; selected: Set<number>; setSelected: (selected: Set<number>) => void; decide: (id: number, decision: "approved" | "denied") => Promise<unknown>; refine: (id: number) => Promise<unknown>; remove: (id: number) => Promise<unknown>; reload: () => void; setNote: (note: string | null) => void };

function IssueTable({ title, rows, loaded, empty, tableKey, showRank, admin, email, busy, selected, setSelected, decide, refine, remove, reload, setNote }: TableProps) {
  const storageBase = `issues:${tableKey}`;
  const [statuses, setStatusesState] = useState<string[]>(() => readStringList(`${storageBase}:statuses`));
  const [types, setTypesState] = useState<string[]>(() => readStringList(`${storageBase}:types`));
  const [collapsed, setCollapsed] = useState<boolean>(() => readBool(`${storageBase}:collapsed`));
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [paneRow, setPaneRow] = useState<Issue | null>(null);
  const [page, setPage] = useState(1);
  const [sortColumn, setSortColumn] = useState<Column>("rank");
  const [sortDirection, setSortDirection] = useState<Direction>("asc");
  const rowsStorageKey = `${storageBase}:rows`;
  const [rowsPerPage, setRowsPerPage] = useState(() => { const stored = Number(localStorage.getItem(rowsStorageKey)); return [10, 25, 50].includes(stored) ? stored : 25; });
  const applyFilters = (nextStatuses: string[], nextTypes: string[]) => { setStatusesState(nextStatuses); setTypesState(nextTypes); writeStringList(`${storageBase}:statuses`, nextStatuses); writeStringList(`${storageBase}:types`, nextTypes); setPage(1); };
  const toggleCollapsed = () => { const next = !collapsed; setCollapsed(next); writeBool(`${storageBase}:collapsed`, next); };
  const filtered = rows.filter((row) => (!statuses.length || statuses.includes(row.status)) && (!types.length || types.includes(row.issue_type)));
  const sorted = sortRows(filtered, sortColumn, sortDirection);
  const pages = Math.max(1, Math.ceil(filtered.length / rowsPerPage));
  const safePage = Math.min(page, pages);
  const visible = sorted.slice((safePage - 1) * rowsPerPage, safePage * rowsPerPage);
  const allSelected = filtered.length > 0 && filtered.every((row) => selected.has(row.id));
  const changeSort = (column: Column) => { if (column === sortColumn) setSortDirection(sortDirection === "asc" ? "desc" : "asc"); else { setSortColumn(column); setSortDirection("asc"); } setPage(1); };
  const toggleAll = () => { const next = new Set(selected); filtered.forEach((row) => allSelected ? next.delete(row.id) : next.add(row.id)); setSelected(next); };
  const toggleOne = (id: number) => { const next = new Set(selected); next.has(id) ? next.delete(id) : next.add(id); setSelected(next); };
  const toggleExpand = (id: number) => setExpandedId((current) => (current === id ? null : id));
  // paneRow only retains the last-opened row so the pane has content to animate out with after
  // expandedId clears; while open we render the current row directly (below) to avoid a one-frame lag.
  useEffect(() => {
    if (expandedId != null) {
      const found = rows.find((row) => row.id === expandedId);
      if (found) setPaneRow(found);
    }
  }, [expandedId, rows]);
  const shownRow = rows.find((row) => row.id === expandedId) ?? paneRow;
  const titleNode = <button onClick={toggleCollapsed} className="flex items-center gap-1.5 text-left" aria-expanded={!collapsed}><span aria-hidden="true" className="text-[10px]" style={{ color: "var(--text-muted)" }}>{collapsed ? "▸" : "▾"}</span><span>{title} ({filtered.length}{filtered.length !== rows.length ? ` of ${rows.length}` : ""})</span></button>;
  return <Panel title={titleNode} right={<TypeFilter rows={rows} statuses={statuses} types={types} onChange={applyFilters} />}>
    <div className={`grid transition-[grid-template-rows] duration-200 ease-in-out ${collapsed ? "grid-rows-[0fr]" : "grid-rows-[1fr]"}`}>
      <div className="overflow-hidden">
        {!loaded ? <EmptyState label="loading…" /> : !filtered.length ? <EmptyState label={empty} /> : <>
          <ResponsiveTable
            table={<div className="max-w-full overflow-x-auto"><table className="w-full min-w-[520px] border-collapse text-left"><thead><tr style={{ color: "var(--text-muted)" }}>{admin && <th className="w-8 pb-2"><input aria-label={`select all ${title}`} type="checkbox" checked={allSelected} onChange={toggleAll} /></th>}{showRank && <SortHeader label="Rank" column="rank" active={sortColumn === "rank"} direction={sortDirection} onClick={changeSort} align="right" className="hidden w-12 sm:table-cell" />}<SortHeader label="Summary" column="summary" active={sortColumn === "summary"} direction={sortDirection} onClick={changeSort} /><SortHeader label="Repo" column="repo" active={sortColumn === "repo"} direction={sortDirection} onClick={changeSort} className="hidden sm:table-cell" /><SortHeader label="Type" column="type" active={sortColumn === "type"} direction={sortDirection} onClick={changeSort} className="hidden sm:table-cell" /><SortHeader label="Status" column="status" active={sortColumn === "status"} direction={sortDirection} onClick={changeSort} /><SortHeader label="Added" column="added" active={sortColumn === "added"} direction={sortDirection} onClick={changeSort} className="hidden sm:table-cell" /><SortHeader label="Updated" column="updated" active={sortColumn === "updated"} direction={sortDirection} onClick={changeSort} className="hidden sm:table-cell" /><th className="pb-2 text-right text-[10px] font-semibold uppercase tracking-widest">Actions</th></tr></thead><tbody>{visible.map((row) => <IssueRow key={row.id} row={row} expanded={expandedId === row.id} onToggle={() => toggleExpand(row.id)} selected={selected.has(row.id)} onSelect={() => toggleOne(row.id)} {...{ admin, busy, decide, refine, showRank }} />)}</tbody></table></div>}
            cards={<>{visible.map((row) => <IssueCard key={row.id} row={row} expanded={expandedId === row.id} onToggle={() => toggleExpand(row.id)} selected={selected.has(row.id)} onSelect={() => toggleOne(row.id)} {...{ admin, busy, decide, refine, showRank }} />)}</>}
          />
          <TablePager total={filtered.length} page={safePage} rowsPerPage={rowsPerPage} storageKey={rowsStorageKey} onPageChange={setPage} onRowsPerPageChange={setRowsPerPage} />
        </>}
      </div>
    </div>
    <DetailPane open={expandedId != null} row={shownRow} admin={admin} email={email} busy={busy} decide={decide} refine={refine} remove={remove} reload={reload} setNote={setNote} onClose={() => setExpandedId(null)} />
  </Panel>;
}

function DetailPane({ open, row, admin, email, busy, decide, refine, remove, reload, setNote, onClose }: { open: boolean; row: Issue | null; admin: boolean; email: string | null; busy: boolean; decide: (id: number, decision: "approved" | "denied") => Promise<unknown>; refine: (id: number) => Promise<unknown>; remove: (id: number) => Promise<unknown>; reload: () => void; setNote: (note: string | null) => void; onClose: () => void }) {
  return (
    <>
      <div className={`fixed inset-0 z-40 bg-black/60 transition-opacity duration-200 ${open ? "opacity-100" : "pointer-events-none opacity-0"}`} onClick={onClose} aria-hidden="true" />
      <aside
        className={`fixed inset-y-0 right-0 z-50 flex w-full flex-col overflow-y-auto border-l p-4 shadow-xl transition-transform duration-200 sm:w-[40vw] sm:min-w-[26rem] sm:p-6 ${open ? "translate-x-0" : "translate-x-full"}`}
        style={{ borderColor: "var(--hairline)", background: "var(--surface-1)" }}
        aria-hidden={!open}
      >
        <div className="mb-3 flex items-center justify-between gap-2">
          <h2 className="panel-title truncate">{row ? `Issue #${row.id}` : "Issue"}</h2>
          <button onClick={onClose} aria-label="close detail pane" className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border text-[14px]" style={{ borderColor: "var(--hairline-strong)", color: "var(--text-secondary)" }}>✕</button>
        </div>
        {row && <ExpandedIssue key={row.id} issueId={row.id} initial={row} admin={admin} email={email} busy={busy} decide={decide} refine={refine} remove={remove} reload={reload} setNote={setNote} onDeleted={onClose} />}
      </aside>
    </>
  );
}

function IssueRow({ row, admin, busy, expanded, selected, onToggle, onSelect, decide, refine, showRank }: { row: Issue; admin: boolean; busy: boolean; expanded: boolean; selected: boolean; onToggle: () => void; onSelect: () => void; decide: (id: number, decision: "approved" | "denied") => Promise<unknown>; refine: (id: number) => Promise<unknown>; showRank: boolean }) {
  const decisionLocked = !["proposed", "approved", "denied"].includes(row.status);
  return <tr onClick={onToggle} className="cursor-pointer border-t align-middle hover:bg-white/[0.02]" style={{ borderColor: "var(--hairline)" }}>{admin && <td onClick={(event) => event.stopPropagation()} className="py-2.5"><input aria-label={`select issue ${row.id}`} type="checkbox" checked={selected} onChange={onSelect} /></td>}{showRank && <td className="tnum hidden py-2.5 pr-3 text-right text-[12px] sm:table-cell">{row.rank ?? "—"}</td>}<td className="py-2.5 pr-3 text-[13px]" style={{ color: "var(--text-primary)" }}><span className="mr-1.5 text-[10px]" style={{ color: "var(--text-muted)" }}>{expanded ? "▾" : "▸"}</span>{row.summary}<span className="ml-2 text-[10px]" style={{ color: "var(--text-muted)" }}>💬 {row.comment_count}</span></td><td className="hidden py-2.5 pr-3 sm:table-cell"><Chip>{row.repo}</Chip></td><td className="hidden py-2.5 pr-3 sm:table-cell"><Chip>{row.issue_type}</Chip></td><td className="py-2.5 pr-3"><StatusPill status={row.status} /></td><td className="hidden whitespace-nowrap py-2.5 pr-3 text-[11px] sm:table-cell">{timeAgo(row.created_at)}</td><td className="hidden whitespace-nowrap py-2.5 pr-3 text-[11px] sm:table-cell">{timeAgo(row.updated_at)}</td><td onClick={(event) => event.stopPropagation()} className="py-2.5 text-right"><div className="flex flex-wrap justify-end gap-1">{admin && !decisionLocked && <><IconButton onClick={() => decide(row.id, "approved")} disabled={busy} title="approve" borderColor="var(--status-good)" color="var(--status-good)" filled={row.status === "approved"}><CheckIcon /></IconButton><IconButton onClick={() => decide(row.id, "denied")} disabled={busy} title="deny" borderColor="var(--status-critical)" color="var(--status-serious)" filled={row.status === "denied"}><XIcon /></IconButton></>}{admin && canRefine(row.status) && <IconButton onClick={() => refine(row.id)} disabled={busy || row.refine_task_id != null} title={row.refine_task_id ? "refining" : "refine"} borderColor="var(--accent)" color="var(--accent)"><RefreshIcon spinning={row.refine_task_id != null} /></IconButton>}</div></td></tr>;
}

function IssueCard({ row, admin, busy, expanded, selected, onToggle, onSelect, decide, refine, showRank }: { row: Issue; admin: boolean; busy: boolean; expanded: boolean; selected: boolean; onToggle: () => void; onSelect: () => void; decide: (id: number, decision: "approved" | "denied") => Promise<unknown>; refine: (id: number) => Promise<unknown>; showRank: boolean }) {
  const decisionLocked = !["proposed", "approved", "denied"].includes(row.status);
  return (
    <RowCard onClick={onToggle} selected={expanded}>
      <CardHeader>
        <div className="flex min-w-0 items-start gap-2">
          {admin && <input aria-label={`select issue ${row.id}`} type="checkbox" checked={selected} onChange={onSelect} onClick={(event) => event.stopPropagation()} className="mt-1 shrink-0" />}
          <div className="min-w-0">
            <div className="flex items-start gap-1.5" style={{ color: "var(--text-primary)" }}><span className="mt-0.5 shrink-0 text-[10px]" style={{ color: "var(--text-muted)" }}>{expanded ? "▾" : "▸"}</span><span>{row.summary}</span></div>
            <span className="text-[10px]" style={{ color: "var(--text-muted)" }}>💬 {row.comment_count}</span>
          </div>
        </div>
        <StatusPill status={row.status} />
      </CardHeader>
      <CardMeta>
        <Chip>{row.repo}</Chip>
        <Chip>{row.issue_type}</Chip>
        {showRank && row.rank != null && <span>rank {row.rank}</span>}
        <span>added {timeAgo(row.created_at)}</span>
        <span>updated {timeAgo(row.updated_at)}</span>
      </CardMeta>
      {admin && <CardActions>
        {!decisionLocked && <><IconButton onClick={() => decide(row.id, "approved")} disabled={busy} title="approve" borderColor="var(--status-good)" color="var(--status-good)" filled={row.status === "approved"}><CheckIcon /></IconButton><IconButton onClick={() => decide(row.id, "denied")} disabled={busy} title="deny" borderColor="var(--status-critical)" color="var(--status-serious)" filled={row.status === "denied"}><XIcon /></IconButton></>}
        {canRefine(row.status) && <IconButton onClick={() => refine(row.id)} disabled={busy || row.refine_task_id != null} title={row.refine_task_id ? "refining" : "refine"} borderColor="var(--accent)" color="var(--accent)"><RefreshIcon spinning={row.refine_task_id != null} /></IconButton>}
      </CardActions>}
    </RowCard>
  );
}

function ExpandedIssue({ issueId, initial, admin, email, busy, decide, refine, remove, reload: reloadList, setNote, onDeleted }: { issueId: number; initial: Issue; admin: boolean; email: string | null; busy: boolean; decide: (id: number, decision: "approved" | "denied") => Promise<unknown>; refine: (id: number) => Promise<unknown>; remove: (id: number) => Promise<unknown>; reload: () => void; setNote: (note: string | null) => void; onDeleted: () => void }) {
  const [detail, setDetail] = useState<IssueDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [summary, setSummary] = useState(initial.summary);
  const [description, setDescription] = useState(initial.details);
  const [priority, setPriority] = useState(initial.priority);
  const load = async () => { setLoading(true); try { const value = await api.issue(issueId); setDetail(value); setSummary(value.issue.summary); setDescription(value.issue.details); setPriority(value.issue.priority); } catch (reason) { setNote((reason as Error).message); } finally { setLoading(false); } };
  useEffect(() => { void load(); }, [issueId]);
  const issue = detail?.issue ?? initial;
  const editable = admin && ["proposed", "approved"].includes(issue.status);
  const saveDetails = async () => { try { await api.updateIssue(issueId, { summary: summary.trim(), details: description.trim() }); setEditing(false); await load(); reloadList(); } catch (reason) { setNote((reason as Error).message); } };
  const savePriority = async () => { try { await api.setIssuePriority(issueId, priority); await load(); reloadList(); } catch (reason) { setPriority(issue.priority); setNote((reason as Error).message); } };
  const changedPriority = priority !== issue.priority;
  const refresh = async () => { await load(); reloadList(); };
  const decisionLocked = !["proposed", "approved", "denied"].includes(issue.status);
  const deleteIssue = async () => { if (await remove(issueId)) onDeleted(); };
  if (loading && !detail) return <div className="rounded-md border p-4 text-[12px]" style={{ borderColor: "var(--hairline)", color: "var(--text-muted)" }}>loading discussion…</div>;
  const attachments = detail?.attachments ?? [];
  return <div className="space-y-4 rounded-md border p-3" style={{ borderColor: "var(--hairline)", background: "var(--surface-2)" }}><div>{editing ? <div className="space-y-2"><input value={summary} onChange={(event) => setSummary(event.target.value)} className={`${fieldClass} w-full`} style={fieldStyle} /><MarkdownEditor value={description} onChange={setDescription} /><div className="flex gap-2"><button onClick={saveDetails} disabled={!summary.trim() || !description.trim()} className="rounded border px-3 py-1 text-[11px]" style={{ borderColor: "var(--status-good)", color: "var(--status-good)" }}>✓ save</button><button onClick={() => { setEditing(false); setSummary(issue.summary); setDescription(issue.details); }} className="rounded border px-3 py-1 text-[11px]" style={{ borderColor: "var(--status-critical)", color: "var(--status-serious)" }}>✗ undo</button></div></div> : <div><div className="flex flex-wrap items-start justify-between gap-2"><h3 className="text-[15px] font-semibold">{issue.summary}</h3>{editable && <button onClick={() => setEditing(true)} className="text-[11px]" style={{ color: "var(--accent)" }}>edit</button>}</div><Markdown text={issue.details} /></div>}</div>
    <div className="flex flex-wrap items-center gap-3 text-[11px]" style={{ color: "var(--text-muted)" }}><Chip>{issue.repo}</Chip><Chip>{issue.issue_type}</Chip><span>key {issue.dedupe_key}</span>{issue.task_id && <span>PR task <TaskLink taskId={issue.task_id} /></span>}{issue.pr_url && <a href={issue.pr_url} target="_blank" rel="noreferrer" style={{ color: "var(--accent)" }}>view PR</a>}</div>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="flex flex-wrap items-center gap-2"><label className="text-[11px] font-semibold">Priority {priority}</label><input type="range" min={1} max={100} value={priority} disabled={!editable} onChange={(event) => setPriority(Number(event.target.value))} className="w-24" />{changedPriority && <><button onClick={savePriority} className="rounded border px-2 py-0.5 text-[11px]" style={{ borderColor: "var(--status-good)", color: "var(--status-good)" }}>✓</button><button onClick={() => setPriority(issue.priority)} className="rounded border px-2 py-0.5 text-[11px]" style={{ borderColor: "var(--status-critical)", color: "var(--status-serious)" }}>✗</button></>}</div>
      {admin && <div className="flex items-center gap-1.5">
        {!decisionLocked && <><IconButton onClick={() => decide(issueId, "approved")} disabled={busy} title="approve" borderColor="var(--status-good)" color="var(--status-good)" filled={issue.status === "approved"}><CheckIcon /></IconButton><IconButton onClick={() => decide(issueId, "denied")} disabled={busy} title="deny" borderColor="var(--status-critical)" color="var(--status-serious)" filled={issue.status === "denied"}><XIcon /></IconButton></>}
        {canRefine(issue.status) && <IconButton onClick={() => refine(issueId)} disabled={busy || issue.refine_task_id != null} title={issue.refine_task_id ? "refining" : "refine"} borderColor="var(--accent)" color="var(--accent)"><RefreshIcon spinning={issue.refine_task_id != null} /></IconButton>}
        {canDelete(issue.status) && <ConfirmIconButton onConfirm={deleteIssue} disabled={busy} title="delete" borderColor="var(--hairline-strong)" color="var(--text-muted)"><TrashIcon /></ConfirmIconButton>}
      </div>}
    </div>
    <Discussion issueId={issueId} comments={detail?.comments ?? []} attachments={attachments} email={email} onReload={refresh} setNote={setNote} />
  </div>;
}

function Discussion({ issueId, comments, attachments, email, onReload, setNote }: { issueId: number; comments: IssueComment[]; attachments: IssueAttachment[]; email: string | null; onReload: () => Promise<void>; setNote: (note: string | null) => void }) {
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => { setBusy(true); try { await api.addIssueComment(issueId, body.trim()); setBody(""); await onReload(); } catch (reason) { setNote((reason as Error).message); } finally { setBusy(false); } };
  return <section className="space-y-3 border-t pt-4" style={{ borderColor: "var(--hairline)" }}><h3 className="panel-title">Discussion ({comments.reduce((count, comment) => count + 1 + comment.replies.length, 0)})</h3>{comments.length ? comments.map((comment) => <Comment key={comment.id} issueId={issueId} comment={comment} attachments={attachments} email={email} onReload={onReload} setNote={setNote} />) : <div className="text-[12px]" style={{ color: "var(--text-muted)" }}>No comments yet.</div>}<div className="space-y-2"><MarkdownEditor value={body} onChange={setBody} rows={4} placeholder="Add a markdown comment" disabled={busy} /><button onClick={submit} disabled={busy || !body.trim()} className="rounded-md px-3 py-1.5 text-[11px] font-bold text-white disabled:opacity-40" style={{ background: "var(--accent)" }}>COMMENT</button></div></section>;
}

function Comment({ issueId, comment, attachments, email, onReload, setNote, reply = false }: { issueId: number; comment: IssueComment; attachments: IssueAttachment[]; email: string | null; onReload: () => Promise<void>; setNote: (note: string | null) => void; reply?: boolean }) {
  const [editing, setEditing] = useState(false);
  const [replying, setReplying] = useState(false);
  const [body, setBody] = useState(comment.body);
  const [replyBody, setReplyBody] = useState("");
  const own = email != null && comment.author.toLowerCase() === email.toLowerCase();
  const commentAttachments = attachments.filter((item) => item.comment_id === comment.id);
  const act = async (work: () => Promise<unknown>) => { try { await work(); await onReload(); return true; } catch (reason) { setNote((reason as Error).message); return false; } };
  const upload = async (event: ChangeEvent<HTMLInputElement>) => { const file = event.target.files?.[0]; if (file) await act(() => api.uploadIssueAttachment(issueId, file, comment.id)); event.target.value = ""; };
  return <div className={`${reply ? "ml-4 sm:ml-8" : ""} rounded-md border p-3`} style={{ borderColor: comment.resolved ? "var(--accent)" : "var(--hairline)", background: "var(--surface-1)" }}><div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px]" style={{ color: "var(--text-muted)" }}><strong style={{ color: "var(--text-secondary)" }}>{comment.author}</strong><span>{timeAgo(comment.created_at)}</span>{comment.edited_at && <span>edited</span>}{comment.resolved && <span title="resolved" aria-label="resolved" style={{ color: "var(--accent)" }}>✓</span>}</div>{editing ? <div className="space-y-2"><MarkdownEditor value={body} onChange={setBody} rows={3} /><div className="flex gap-2"><button onClick={() => act(() => api.updateIssueComment(issueId, comment.id, body.trim())).then((ok) => { if (ok) setEditing(false); })} disabled={!body.trim()} className="text-[11px]" style={{ color: "var(--status-good)" }}>✓ save</button><button onClick={() => { setEditing(false); setBody(comment.body); }} className="text-[11px]" style={{ color: "var(--status-serious)" }}>✗ undo</button></div></div> : comment.deleted_at ? <div className="text-[12px] italic line-through" style={{ color: "var(--text-muted)" }}>[deleted]</div> : <Markdown text={comment.body} />}
    {commentAttachments.length > 0 && <div className="mt-2 flex flex-wrap gap-2">{commentAttachments.map((item) => <a key={item.id} href={api.issueAttachmentUrl(issueId, item.id)} className="text-[11px] underline" style={{ color: "var(--accent)" }}>📎 {item.filename}</a>)}</div>}
    <div className="mt-2 flex flex-wrap items-center gap-3 text-[10px]">{!reply && <button onClick={() => setReplying(!replying)} style={{ color: "var(--accent)" }}>reply</button>}{own && !comment.deleted_at && <><button onClick={() => setEditing(true)} style={{ color: "var(--accent)" }}>edit</button><button onClick={() => { void act(() => api.deleteIssueComment(issueId, comment.id)); }} style={{ color: "var(--status-serious)" }}>delete</button></>}<label className="cursor-pointer" style={{ color: "var(--accent)" }}>attach file<input type="file" className="hidden" onChange={upload} /></label></div>
    {replying && <div className="mt-3 space-y-2"><MarkdownEditor value={replyBody} onChange={setReplyBody} rows={3} placeholder="Reply with markdown" /><div className="flex gap-2"><button onClick={() => act(() => api.addIssueComment(issueId, replyBody.trim(), comment.id)).then((ok) => { if (ok) { setReplyBody(""); setReplying(false); } })} disabled={!replyBody.trim()} className="text-[11px]" style={{ color: "var(--status-good)" }}>post reply</button><button onClick={() => setReplying(false)} className="text-[11px]" style={{ color: "var(--text-muted)" }}>cancel</button></div></div>}
    {comment.replies.map((child) => <Comment key={child.id} issueId={issueId} comment={child} attachments={attachments} email={email} onReload={onReload} setNote={setNote} reply />)}
  </div>;
}
