// typed fetch layer for the mission-control api

export type TaskSlim = {
  task_id: string;
  state: string;
  request_text: string;
  task_type: string | null;
  complexity: string | null;
  model_alias: string | null;
  effort: string | null;
  persona: string | null;
  profile: string | null;
  attempt: number;
  cost_usd: number;
  num_turns: number;
  slack_user_id: string;
  requester: string | null;
  parent_task_id: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type TaskEvent = {
  id: number;
  task_id: string;
  ts: string;
  kind: string;
  tool_name: string | null;
  is_write: number | null;
  detail_json: string;
};

export type UsageTotals = {
  rows: number;
  task_count: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number;
  last_updated: string | null;
};

export type UsageCard = {
  label: string;
  since: string | null;
  totals: UsageTotals;
  by_model: { model: string; input_tokens: number; output_tokens: number; cache_tokens: number; cost_usd: number }[];
  total_tokens: number;
  limit_tokens: number | null;
  observed: { utilization: number; resets_at: number; status: string; observed_at: string } | null;
};

export type Overview = {
  counts: Record<string, number>;
  running: TaskSlim[];
  recent: TaskSlim[];
  errors: Record<string, unknown>[];
  intake_paused: boolean;
  usage_5h: UsageTotals;
  environment: string;
  bot_name: string;
  reviewer_name: string;
  max_concurrency: number;
  queue_max: number;
};

export type PermissionRequest = {
  id: number;
  task_id: string;
  kind: string;
  target: string;
  reason: string;
  status: string;
  decided_by: string | null;
  requested_at: string;
  decided_at: string | null;
};

export type TaskFeedback = {
  id: number;
  task_id: string;
  submitted_by: string;
  rating: number;
  comment: string | null;
  created_at: string;
  updated_at: string;
};

export type TaskDetail = {
  task: Record<string, unknown> & { task_id: string; state: string };
  bot_name: string;
  reviewer_name: string;
  requester: string | null;
  events: TaskEvent[];
  event_page: number;
  event_count: number;
  children: TaskSlim[];
  errors: Record<string, unknown>[];
  usage: Record<string, unknown>[];
  timings: TaskEvent[];
  artifacts: { kind: string; external_id: string; url: string | null; created_at: string }[];
  permission_requests: PermissionRequest[];
  feedback: TaskFeedback[];
  own_memory: string | null;
  parent_memory: string | null;
  can_cancel: boolean;
  can_retry: boolean;
};

export type Usage = {
  generated_at: string;
  fable_model: string;
  cards: { five_hour: UsageCard; weekly: UsageCard; fable: UsageCard };
  timeseries: { bucket: string; model: string; total_tokens: number; output_tokens: number; cost_usd: number }[];
};

export type Issue = {
  id: number;
  dedupe_key: string;
  repo: string;
  summary: string;
  issue_type: string;
  details: string;
  priority: number;
  status: string;
  source_json: string | null;
  spec: string | null;
  task_id: string | null;
  pr_url: string | null;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string;
  updated_at: string;
  rank: number | null;
  comment_count: number;
  refine_task_id: string | null;
};

export type IssueComment = {
  id: number;
  issue_id: number;
  parent_comment_id: number | null;
  author: string;
  body: string;
  resolved: boolean;
  resolved_by: string | null;
  resolved_at: string | null;
  edited_at: string | null;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
  replies: IssueComment[];
};

export type IssueAttachment = {
  id: number;
  issue_id: number;
  comment_id: number | null;
  filename: string;
  content_type: string | null;
  size_bytes: number;
  uploaded_by: string;
  created_at: string;
};

export type IssueDetail = {
  issue: Issue;
  comments: IssueComment[];
  attachments: IssueAttachment[];
  refine_task_id: string | null;
};

export type Schedule = {
  id: number;
  name: string;
  request_text: string;
  model_alias: string | null;
  effort: string | null;
  kind: "once" | "interval" | "daily";
  interval_minutes: number | null;
  at_time: string | null;
  run_at: string | null;
  timezone: string | null;
  max_runs: number | null;
  run_count: number;
  enabled: number;
  next_run_at: string;
  last_run_at: string | null;
  last_task_id: string | null;
  seed_key: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type ScheduleInput = {
  name: string;
  request_text: string;
  model_alias?: string | null;
  effort?: string | null;
  kind: "once" | "interval" | "daily";
  interval_minutes?: number | null;
  at_time?: string | null;
  run_at?: string | null;
  timezone?: string | null;
  max_runs?: number | null;
};

export type Me = { email: string; admin: boolean; bot_name: string; reviewer_name: string };

export type ManageTarget = {
  kind: string;
  name: string | null;
  title: string;
  content: string;
  base_hash: string;
  repo_path: string;
  auto_commit: boolean;
};

export type SaveResult = {
  saved: boolean;
  message?: string;
  diff: string;
  commit?: { commit_sha: string; html_url: string; unchanged: boolean } | null;
  commit_error?: string | null;
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* non-json error body */
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  me: () => request<Me>("/api/me"),
  overview: () => request<Overview>("/api/overview"),
  tasks: (params: { state?: string; q?: string; page?: number }) => {
    const search = new URLSearchParams();
    if (params.state) search.set("state", params.state);
    if (params.q) search.set("q", params.q);
    if (params.page && params.page > 1) search.set("page", String(params.page));
    const qs = search.toString();
    return request<{ tasks: TaskSlim[]; page: number; page_size: number; bot_name: string; reviewer_name: string }>(`/api/tasks${qs ? `?${qs}` : ""}`);
  },
  task: (taskId: string, eventPage = 1) => request<TaskDetail>(`/api/tasks/${taskId}?event_page=${eventPage}`),
  cancel: (taskId: string) => request<{ status: string; state: string | null }>(`/api/tasks/${taskId}/cancel`, { method: "POST", headers: { "x-harness-dashboard": "1" } }),
  retry: (taskId: string) => request<{ status: string; new_task_id: string | null }>(`/api/tasks/${taskId}/retry`, { method: "POST", headers: { "x-harness-dashboard": "1" } }),
  decidePermission: (taskId: string, body: { kind: string; target: string; decision: "granted" | "denied" }) =>
    request<{ status: string; state: string | null }>(`/api/tasks/${taskId}/permissions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-harness-dashboard": "1" },
      body: JSON.stringify(body),
    }),
  submitFeedback: (taskId: string, body: { rating: number; comment: string }) =>
    request<{ status: string; feedback: TaskFeedback }>(`/api/tasks/${taskId}/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-harness-dashboard": "1" },
      body: JSON.stringify(body),
    }),
  issues: (status?: string) => request<{ issues: Issue[]; repos: string[]; implementation_active: string | null }>(`/api/issues${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  issue: (id: number) => request<IssueDetail>(`/api/issues/${id}`),
  createIssue: (body: { repo: string; summary: string; issue_type: string; details: string; priority: number }) =>
    request<{ status: string; issue: Issue }>("/api/issues", {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-harness-dashboard": "1" },
      body: JSON.stringify(body),
    }),
  decideIssue: (id: number, decision: "approved" | "denied" | "proposed") =>
    request<{ status: string; issue: Issue }>(`/api/issues/${id}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-harness-dashboard": "1" },
      body: JSON.stringify({ decision }),
    }),
  updateIssue: (id: number, body: { summary: string; details: string }) =>
    request<{ status: string; issue: Issue }>(`/api/issues/${id}/update`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-harness-dashboard": "1" },
      body: JSON.stringify(body),
    }),
  setIssuePriority: (id: number, priority: number) =>
    request<{ status: string; issue: Issue }>(`/api/issues/${id}/priority`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-harness-dashboard": "1" },
      body: JSON.stringify({ priority }),
    }),
  refineIssue: (id: number) => request<{ status: string; task_id: string | null }>(`/api/issues/${id}/refine`, { method: "POST", headers: { "x-harness-dashboard": "1" } }),
  deleteIssue: (id: number) => request<{ status: string }>(`/api/issues/${id}`, { method: "DELETE", headers: { "x-harness-dashboard": "1" } }),
  bulkIssues: (ids: number[], action: "approve" | "deny" | "refine") =>
    request<{ results: { id: number; status: string; task_id?: string | null }[] }>("/api/issues/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-harness-dashboard": "1" },
      body: JSON.stringify({ ids, action }),
    }),
  addIssueComment: (id: number, body: string, parentCommentId: number | null = null) =>
    request<{ status: string; comment: IssueComment }>(`/api/issues/${id}/comments`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-harness-dashboard": "1" },
      body: JSON.stringify({ body, parent_comment_id: parentCommentId }),
    }),
  updateIssueComment: (id: number, commentId: number, body: string) =>
    request<{ status: string; comment: IssueComment }>(`/api/issues/${id}/comments/${commentId}/update`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-harness-dashboard": "1" },
      body: JSON.stringify({ body }),
    }),
  deleteIssueComment: (id: number, commentId: number) =>
    request<{ status: string; comment: IssueComment }>(`/api/issues/${id}/comments/${commentId}/delete`, { method: "POST", headers: { "x-harness-dashboard": "1" } }),
  uploadIssueAttachment: (id: number, file: File, commentId: number | null = null) => {
    const form = new FormData();
    form.append("file", file);
    if (commentId != null) form.append("comment_id", String(commentId));
    return request<{ status: string; attachment: IssueAttachment }>(`/api/issues/${id}/attachments`, { method: "POST", headers: { "x-harness-dashboard": "1" }, body: form });
  },
  issueAttachmentUrl: (id: number, attachmentId: number) => `/api/issues/${id}/attachments/${attachmentId}/download`,
  runIssues: (skill: "discoverissues" | "implementapprovedissues", repo?: string) =>
    request<{ status: string; task_id: string | null }>(`/api/issues/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-harness-dashboard": "1" },
      body: JSON.stringify({ skill, repo }),
    }),
  schedules: () => request<{ schedules: Schedule[]; models: string[] }>("/api/schedules"),
  createSchedule: (body: ScheduleInput) =>
    request<{ schedule: Schedule }>("/api/schedules", { method: "POST", headers: { "Content-Type": "application/json", "x-harness-dashboard": "1" }, body: JSON.stringify(body) }),
  updateSchedule: (id: number, body: ScheduleInput | { enabled: boolean }) =>
    request<{ schedule: Schedule }>(`/api/schedules/${id}`, { method: "POST", headers: { "Content-Type": "application/json", "x-harness-dashboard": "1" }, body: JSON.stringify(body) }),
  runSchedule: (id: number) => request<{ status: string; task_id: string | null }>(`/api/schedules/${id}/run`, { method: "POST", headers: { "x-harness-dashboard": "1" } }),
  deleteSchedule: (id: number) => request<{ status: string }>(`/api/schedules/${id}`, { method: "DELETE", headers: { "x-harness-dashboard": "1" } }),
  memory: (q: string) => request<{ records: { task_id: string; modified: string; size: number; preview: string }[] }>(`/api/memory${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  memoryDetail: (taskId: string) => request<{ task_id: string; content: string; state: string | null }>(`/api/memory/${taskId}`),
  usage: () => request<Usage>("/api/usage"),
  config: () => request<{ runtime: Record<string, string>; policy: Record<string, unknown>; dashboard: Record<string, unknown>; secret_presence: Record<string, boolean>; skills: string[] }>("/api/config"),
  adminEvents: () => request<{ events: Record<string, unknown>[] }>("/api/admin-events"),
  manageRead: (kind: string, name?: string) => request<ManageTarget>(`/api/manage/${kind}${name ? `?name=${encodeURIComponent(name)}` : ""}`),
  manageWrite: (kind: string, body: { name?: string | null; content: string; base_hash: string; action: "preview" | "confirm" }) =>
    request<SaveResult>(`/api/manage/${kind}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-harness-dashboard": "1" },
      body: JSON.stringify(body),
    }),
};

// mirrors agent_harness.models.EFFORT_LEVELS — the sdk's reasoning-effort levels
export const EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"] as const;

export const MODEL_COLORS: Record<string, string> = {
  fable: "var(--series-fable)",
  opus: "var(--series-opus)",
  sonnet: "var(--series-sonnet)",
  haiku: "var(--series-haiku)",
};

// color follows the model entity; unknown ids fold to a neutral slot
export function modelColor(model: string | null | undefined): string {
  if (!model) return "var(--series-other)";
  const lowered = model.toLowerCase();
  for (const key of Object.keys(MODEL_COLORS)) {
    if (lowered.includes(key)) return MODEL_COLORS[key];
  }
  return "var(--series-other)";
}

export function modelLabel(model: string | null | undefined): string {
  if (!model) return "unknown";
  const lowered = model.toLowerCase();
  for (const key of Object.keys(MODEL_COLORS)) {
    if (lowered.includes(key)) return key;
  }
  return model;
}

export const STATE_COLORS: Record<string, string> = {
  received: "var(--status-warning)",
  queued: "var(--status-warning)",
  running: "var(--accent)",
  blocked: "var(--status-serious)",
  completed: "var(--status-good)",
  failed: "var(--status-critical)",
  cancelled: "var(--status-neutral)",
  refused: "var(--status-neutral)",
};

export function formatTokens(value: number): string {
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)}B`;
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

export function formatUsd(value: number | null | undefined): string {
  return value == null ? "—" : `$${value.toFixed(2)}`;
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
