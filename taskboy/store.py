"""the only module that talks sql. everything durable lives here: tasks, slack dedup, audit events, artifacts, usage.

this interface is the future off-host seam — swapping sqlite for dynamodb/postgres touches nothing else.
"""

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from taskboy.models import ALLOWED_TRANSITIONS, QUEUED, Task, new_task_id, utcnow
from taskboy.redact import redactor


class IllegalTransition(Exception):
    pass


class TransitionRaced(Exception):
    """the row was not in from_state when the update ran — someone else transitioned it first."""


# the issues table's status vocabulary (see the CHECK constraint on the issues table below); shared with
# adapters/issues.py so tool-level validation stays in sync with the schema
ISSUE_STATUSES = ("proposed", "approved", "denied", "implementation_queued", "in_progress", "in_review", "done", "failed")

# columns transition() may update alongside state; anything else is a programming error
UPDATABLE_FIELDS = {
    "attempt",
    "resume_session_id",
    "not_before",
    "persona",
    "classification_json",
    "task_type",
    "complexity",
    "risk",
    "model_alias",
    "model_id",
    "profile",
    "routing_rationale",
    "model_override",
    "effort_override",
    "effort",
    "session_id",
    "workspace_path",
    "max_budget_usd",
    "max_turns",
    "max_runtime_minutes",
    "cost_usd",
    "num_turns",
    "blocked_reason",
    "error",
    "result_summary",
    "reply",
    "debug_thread_ts",
    "debug_permalink",
    "started_at",
    "finished_at",
}

# free-text columns are long-term memory: secrets must never land in them (MEM-011)
REDACTED_FIELDS = {"request_text", "thread_context", "result_summary", "reply", "error", "blocked_reason", "routing_rationale", "classification_json"}


def _redact_fields(fields: dict) -> dict:
    return {key: redactor.redact(value) if key in REDACTED_FIELDS and isinstance(value, str) else value for key, value in fields.items()}


MIGRATIONS = [
    # single base schema: this project ships with no deployed databases, so the historical
    # incremental migrations were squashed. append new migrations below; never edit this one.
    """
    CREATE TABLE tasks (
        task_id            TEXT PRIMARY KEY,
        idempotency_key    TEXT NOT NULL UNIQUE,
        state              TEXT NOT NULL CHECK (state IN ('received','queued','running','blocked','completed','failed','cancelled','refused')),
        attempt            INTEGER NOT NULL DEFAULT 0,
        resume_session_id  TEXT,
        slack_team_id      TEXT NOT NULL,
        slack_channel_id   TEXT NOT NULL,
        slack_thread_ts    TEXT NOT NULL,
        slack_message_ts   TEXT NOT NULL,
        slack_user_id      TEXT NOT NULL,
        request_text       TEXT NOT NULL,
        thread_context     TEXT,
        parent_task_id     TEXT REFERENCES tasks(task_id),
        classification_json TEXT,
        task_type          TEXT,
        complexity         TEXT,
        risk               TEXT,
        model_alias        TEXT,
        model_id           TEXT,
        profile            TEXT,
        routing_rationale  TEXT,
        model_override     TEXT,
        effort_override    TEXT,
        effort             TEXT,
        session_id         TEXT,
        workspace_path     TEXT,
        max_budget_usd     REAL,
        max_turns          INTEGER,
        max_runtime_minutes INTEGER,
        cost_usd           REAL NOT NULL DEFAULT 0,
        num_turns          INTEGER NOT NULL DEFAULT 0,
        blocked_reason     TEXT,
        error              TEXT,
        result_summary     TEXT,
        reply              TEXT,
        debug_thread_ts    TEXT,
        debug_permalink    TEXT,
        persona            TEXT,
        not_before         TEXT,
        schedule_name      TEXT,
        created_at         TEXT NOT NULL,
        started_at         TEXT,
        finished_at        TEXT,
        updated_at         TEXT NOT NULL
    );
    CREATE INDEX idx_tasks_state ON tasks(state);
    CREATE INDEX idx_tasks_thread ON tasks(slack_channel_id, slack_thread_ts, created_at);

    CREATE TABLE slack_events (
        event_id    TEXT PRIMARY KEY,
        received_at TEXT NOT NULL
    );

    CREATE TABLE task_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id     TEXT NOT NULL REFERENCES tasks(task_id),
        ts          TEXT NOT NULL,
        kind        TEXT NOT NULL,
        tool_name   TEXT,
        is_write    INTEGER,
        detail_json TEXT NOT NULL,
        hash        TEXT
    );
    CREATE INDEX idx_task_events_task ON task_events(task_id, id);

    CREATE TABLE artifacts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id     TEXT NOT NULL REFERENCES tasks(task_id),
        kind        TEXT NOT NULL,
        external_id TEXT NOT NULL,
        url         TEXT,
        created_at  TEXT NOT NULL,
        UNIQUE(task_id, kind, external_id)
    );

    CREATE TABLE usage (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id            TEXT NOT NULL REFERENCES tasks(task_id),
        ts                 TEXT NOT NULL,
        source             TEXT NOT NULL,
        model              TEXT NOT NULL,
        input_tokens       INTEGER,
        output_tokens      INTEGER,
        cache_read_tokens  INTEGER,
        cache_write_tokens INTEGER,
        cost_usd           REAL
    );

    -- unauthorized mentions never create a task, but the audit trail records the authorization result
    CREATE TABLE intake_denials (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         TEXT NOT NULL,
        team_id    TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        user_id    TEXT NOT NULL,
        reason     TEXT NOT NULL
    );

    CREATE TABLE slack_users (
        user_id      TEXT PRIMARY KEY,
        team_id      TEXT,
        username     TEXT,
        real_name    TEXT,
        display_name TEXT,
        email        TEXT,
        title        TEXT,
        tz           TEXT,
        is_bot       INTEGER,
        updated_at   TEXT NOT NULL
    );

    CREATE TABLE errors (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,
        task_id      TEXT,
        component    TEXT NOT NULL,
        kind         TEXT NOT NULL,
        message      TEXT NOT NULL,
        traceback    TEXT,
        context_json TEXT
    );
    CREATE INDEX idx_errors_component_ts ON errors(component, ts);

    CREATE TABLE admin_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        actor       TEXT NOT NULL,
        action      TEXT NOT NULL,
        target      TEXT NOT NULL,
        outcome     TEXT NOT NULL,
        detail_json TEXT NOT NULL,
        hash        TEXT NOT NULL
    );
    CREATE INDEX idx_admin_events_ts ON admin_events(ts, id);

    CREATE TABLE permission_requests (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id      TEXT NOT NULL REFERENCES tasks(task_id),
        kind         TEXT NOT NULL CHECK (kind IN ('tool','repo')),
        target       TEXT NOT NULL,
        reason       TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','granted','denied')),
        decided_by   TEXT,
        requested_at TEXT NOT NULL,
        decided_at   TEXT,
        UNIQUE(task_id, kind, target)
    );
    CREATE INDEX idx_permission_requests_task ON permission_requests(task_id, id);

    CREATE TABLE rate_limits (
        rate_limit_type TEXT PRIMARY KEY,
        status          TEXT NOT NULL,
        utilization     REAL,
        resets_at       INTEGER,
        observed_at     TEXT NOT NULL
    );

    CREATE TABLE task_questions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id     TEXT NOT NULL REFERENCES tasks(task_id),
        questions   TEXT NOT NULL,
        answer_text TEXT,
        answered_by TEXT,
        asked_at    TEXT NOT NULL,
        answered_at TEXT
    );
    CREATE INDEX idx_task_questions_task ON task_questions(task_id, id);

    CREATE TABLE task_feedback (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id      TEXT NOT NULL REFERENCES tasks(task_id),
        submitted_by TEXT NOT NULL,
        rating       INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
        comment      TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        UNIQUE(task_id, submitted_by)
    );
    CREATE INDEX idx_task_feedback_task ON task_feedback(task_id, id);

    CREATE TABLE schedules (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        name             TEXT NOT NULL,
        request_text     TEXT NOT NULL,
        model_alias      TEXT,
        effort           TEXT,
        kind             TEXT NOT NULL CHECK (kind IN ('once','interval','daily')),
        interval_minutes INTEGER,
        at_time          TEXT,
        run_at           TEXT,
        timezone         TEXT,
        max_runs         INTEGER,
        run_count        INTEGER NOT NULL DEFAULT 0,
        enabled          INTEGER NOT NULL DEFAULT 1,
        next_run_at      TEXT NOT NULL,
        last_run_at      TEXT,
        last_task_id     TEXT,
        seed_key         TEXT UNIQUE,
        created_by       TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    );
    CREATE INDEX idx_schedules_due ON schedules(enabled, next_run_at);

    CREATE TABLE issues (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        dedupe_key  TEXT NOT NULL UNIQUE,
        repo        TEXT NOT NULL,
        summary     TEXT NOT NULL,
        issue_type  TEXT NOT NULL,
        details     TEXT NOT NULL,
        priority    INTEGER NOT NULL DEFAULT 50,
        status      TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','approved','denied','implementation_queued','in_progress','in_review','done','failed')),
        source_json TEXT,
        spec        TEXT,
        task_id     TEXT REFERENCES tasks(task_id),
        pr_url      TEXT,
        decided_by  TEXT,
        decided_at  TEXT,
        reserved_by TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX idx_issues_status ON issues(status, priority DESC, id);
    CREATE INDEX idx_issues_repo ON issues(repo);

    CREATE TABLE issue_comments (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_id          INTEGER NOT NULL REFERENCES issues(id),
        parent_comment_id INTEGER REFERENCES issue_comments(id),
        author            TEXT NOT NULL,
        body              TEXT NOT NULL,
        resolved          INTEGER NOT NULL DEFAULT 0,
        resolved_by       TEXT,
        resolved_at       TEXT,
        edited_at         TEXT,
        deleted_at        TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    );
    CREATE INDEX idx_issue_comments_issue ON issue_comments(issue_id, id);

    CREATE TABLE issue_attachments (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_id     INTEGER NOT NULL REFERENCES issues(id),
        comment_id   INTEGER REFERENCES issue_comments(id),
        filename     TEXT NOT NULL,
        content_type TEXT,
        size_bytes   INTEGER NOT NULL,
        s3_key       TEXT NOT NULL,
        uploaded_by  TEXT NOT NULL,
        created_at   TEXT NOT NULL
    );
    CREATE INDEX idx_issue_attachments_issue ON issue_attachments(issue_id, id);
    """,
]

SCHEDULE_UPDATABLE = {"name", "request_text", "model_alias", "effort", "kind", "interval_minutes", "at_time", "run_at", "timezone", "max_runs", "enabled", "next_run_at", "run_count", "last_run_at", "last_task_id"}


def _clamp_priority(priority: int) -> int:
    try:
        return max(1, min(100, int(priority)))
    except (TypeError, ValueError):
        return 50


def event_hash(prev_hash: str, ts: str, task_id: str, kind: str, tool_name: str | None, detail_json: str) -> str:
    return hashlib.sha256(f"{prev_hash}|{ts}|{task_id}|{kind}|{tool_name or ''}|{detail_json}".encode()).hexdigest()


def admin_event_hash(prev_hash: str, ts: str, actor: str, action: str, target: str, outcome: str, detail_json: str) -> str:
    return hashlib.sha256(f"{prev_hash}|{ts}|{actor}|{action}|{target}|{outcome}|{detail_json}".encode()).hexdigest()


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            self.conn.executescript(script)
            self.conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(i),))
        self.conn.commit()

    # -- tasks --------------------------------------------------------------

    def create_task(
        self,
        *,
        slack_team_id: str,
        slack_channel_id: str,
        slack_thread_ts: str,
        slack_message_ts: str,
        slack_user_id: str,
        request_text: str,
        parent_task_id: str | None = None,
        model_override: str | None = None,
        effort_override: str | None = None,
        thread_context: str | None = None,
        pre_classification: dict | None = None,
        persona: str | None = None,
        schedule_name: str | None = None,
        debug_thread_ts: str | None = None,
        debug_permalink: str | None = None,
    ) -> tuple[Task, bool]:
        """durably record a task before anything executes (REL-001). one task per slack message, ever (SLK-004/008)."""
        idempotency_key = f"{slack_team_id}:{slack_channel_id}:{slack_message_ts}"
        task_id = new_task_id()
        now = utcnow()
        fields = _redact_fields(
            {
                "request_text": request_text,
                "thread_context": thread_context,
                "classification_json": json.dumps(pre_classification) if pre_classification is not None else None,
            }
        )
        cur = self.conn.execute(
            """INSERT INTO tasks (task_id, idempotency_key, state, attempt, slack_team_id, slack_channel_id, slack_thread_ts, slack_message_ts, slack_user_id, request_text, thread_context, classification_json, persona, schedule_name, parent_task_id, model_override, effort_override, debug_thread_ts, debug_permalink, cost_usd, num_turns, created_at, updated_at)
               VALUES (?, ?, 'received', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (
                task_id,
                idempotency_key,
                slack_team_id,
                slack_channel_id,
                slack_thread_ts,
                slack_message_ts,
                slack_user_id,
                fields["request_text"],
                fields["thread_context"],
                fields["classification_json"],
                persona,
                schedule_name,
                parent_task_id,
                model_override,
                effort_override,
                debug_thread_ts,
                debug_permalink,
                now,
                now,
            ),
        )
        created = cur.rowcount == 1
        if created:
            self._insert_event(task_id, "intake", {"user": slack_user_id, "channel": slack_channel_id})
        self.conn.commit()
        if created:
            task = self.get_task(task_id)
            assert task is not None
            return task, True
        row = self.conn.execute("SELECT * FROM tasks WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        return Task(**dict(row)), False

    def record_quick_answer(
        self,
        *,
        slack_team_id: str,
        slack_channel_id: str,
        slack_thread_ts: str,
        slack_message_ts: str,
        slack_user_id: str,
        request_text: str,
        answer_text: str,
        model_alias: str,
        model_id: str,
        parent_task_id: str | None = None,
        latency_s: float = 0.0,
        debug_thread_ts: str | None = None,
        debug_permalink: str | None = None,
    ) -> Task:
        """record a quick answer as completed from birth so the dispatcher never sees it."""
        idempotency_key = f"{slack_team_id}:{slack_channel_id}:{slack_message_ts}"
        task_id = new_task_id()
        now = utcnow()
        fields = _redact_fields({"request_text": request_text, "result_summary": answer_text})
        cur = self.conn.execute(
            """INSERT INTO tasks (task_id, idempotency_key, state, attempt, slack_team_id, slack_channel_id, slack_thread_ts, slack_message_ts, slack_user_id, request_text, parent_task_id, task_type, model_alias, model_id, routing_rationale, result_summary, reply, debug_thread_ts, debug_permalink, cost_usd, num_turns, created_at, started_at, finished_at, updated_at)
               VALUES (?, ?, 'completed', 0, ?, ?, ?, ?, ?, ?, ?, 'question', ?, ?, 'quick-answer', ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (task_id, idempotency_key, slack_team_id, slack_channel_id, slack_thread_ts, slack_message_ts, slack_user_id, fields["request_text"], parent_task_id, model_alias, model_id, fields["result_summary"], fields["result_summary"], debug_thread_ts, debug_permalink, now, now, now, now),
        )
        if cur.rowcount == 1:
            self._insert_event(task_id, "intake", {"user": slack_user_id, "channel": slack_channel_id})
            self._insert_event(task_id, "quick_answer", {"escalated": False, "latency_s": latency_s})
            self.conn.commit()
            task = self.get_task(task_id)
            assert task is not None
            return task
        self.conn.rollback()
        row = self.conn.execute("SELECT * FROM tasks WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        return Task(**dict(row))

    def get_task(self, task_id: str) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return Task(**dict(row)) if row else None

    def tasks_in_state(self, state: str) -> list[Task]:
        # rowid = insertion order; created_at alone is only second-precise
        rows = self.conn.execute("SELECT * FROM tasks WHERE state = ? ORDER BY rowid", (state,)).fetchall()
        return [Task(**dict(row)) for row in rows]

    def next_queued(self) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE state = ? AND (not_before IS NULL OR not_before <= ?) ORDER BY rowid LIMIT 1", (QUEUED, utcnow())).fetchone()
        return Task(**dict(row)) if row else None

    def count_tasks(self, state: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE state = ?", (state,)).fetchone()
        return int(row["n"])

    def terminal_tasks_updated_before(self, cutoff_iso: str) -> list[Task]:
        """finished tasks whose workspaces are candidates for the retention sweep (MEM-012)."""
        rows = self.conn.execute("SELECT * FROM tasks WHERE state IN ('completed','failed','cancelled') AND updated_at < ? AND workspace_path IS NOT NULL", (cutoff_iso,)).fetchall()
        return [Task(**dict(row)) for row in rows]

    def purge_slack_events(self, cutoff_iso: str) -> int:
        cur = self.conn.execute("DELETE FROM slack_events WHERE received_at < ?", (cutoff_iso,))
        self.conn.commit()
        return cur.rowcount

    def recent_tasks(self, limit: int = 20) -> list[Task]:
        rows = self.conn.execute("SELECT * FROM tasks ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [Task(**dict(row)) for row in rows]

    def list_tasks(self, state: str | None = None, limit: int = 50, offset: int = 0, query: str | None = None, task_type: str | None = None) -> list[Task]:
        clauses: list[str] = []
        values: list[object] = []
        if state:
            clauses.append("state = ?")
            values.append(state)
        if task_type:
            clauses.append("task_type = ?")
            values.append(task_type)
        if query:
            clauses.append("(task_id LIKE ? OR request_text LIKE ?)")
            pattern = f"%{query}%"
            values.extend((pattern, pattern))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(f"SELECT * FROM tasks{where} ORDER BY rowid DESC LIMIT ? OFFSET ?", (*values, min(max(limit, 1), 200), max(offset, 0))).fetchall()
        return [Task(**dict(row)) for row in rows]

    def children_of(self, parent_task_id: str) -> list[Task]:
        rows = self.conn.execute("SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY rowid", (parent_task_id,)).fetchall()
        return [Task(**dict(row)) for row in rows]

    def task_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state").fetchall()
        return {row["state"]: int(row["n"]) for row in rows}

    def transition(self, task_id: str, from_state: str, to_state: str, reason: str = "", **fields) -> Task:
        """the only way a task changes state: guarded update + audit event in one transaction. raced updates fail loudly."""
        if to_state not in ALLOWED_TRANSITIONS.get(from_state, []):
            raise IllegalTransition(f"{from_state} -> {to_state} is not allowed")
        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"transition cannot update fields: {sorted(unknown)}")
        fields = _redact_fields(fields)
        sets = "".join(f", {name} = ?" for name in fields)
        cur = self.conn.execute(
            f"UPDATE tasks SET state = ?, updated_at = ?{sets} WHERE task_id = ? AND state = ?",
            (to_state, utcnow(), *fields.values(), task_id, from_state),
        )
        if cur.rowcount == 0:
            self.conn.rollback()
            current = self.get_task(task_id)
            raise TransitionRaced(f"task {task_id} is {current.state if current else 'missing'}, expected {from_state}")
        self._insert_event(task_id, "state_change", {"from": from_state, "to": to_state, "reason": reason})
        self.conn.commit()
        task = self.get_task(task_id)
        assert task is not None
        return task

    def set_fields(self, task_id: str, **fields) -> None:
        """update task columns without a state change — e.g. persisting the sdk session id the moment it appears."""
        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"set_fields cannot update fields: {sorted(unknown)}")
        fields = _redact_fields(fields)
        sets = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(f"UPDATE tasks SET updated_at = ?, {sets} WHERE task_id = ?", (utcnow(), *fields.values(), task_id))
        self.conn.commit()

    def latest_task_in_thread(self, channel_id: str, thread_ts: str) -> Task | None:
        """most recent task rooted at this slack thread — the lineage anchor for follow-up mentions (MEM-009/010)."""
        row = self.conn.execute("SELECT * FROM tasks WHERE slack_channel_id = ? AND slack_thread_ts = ? ORDER BY rowid DESC LIMIT 1", (channel_id, thread_ts)).fetchone()
        return Task(**dict(row)) if row else None

    # -- slack dedup / intake authz --------------------------------------------

    def slack_event_seen(self, event_id: str) -> bool:
        """true if this event id was already recorded (SLK-008)."""
        cur = self.conn.execute("INSERT OR IGNORE INTO slack_events (event_id, received_at) VALUES (?, ?)", (event_id, utcnow()))
        self.conn.commit()
        return cur.rowcount == 0

    def record_intake_denial(self, team_id: str, channel_id: str, user_id: str, reason: str) -> None:
        self.conn.execute("INSERT INTO intake_denials (ts, team_id, channel_id, user_id, reason) VALUES (?, ?, ?, ?, ?)", (utcnow(), team_id, channel_id, user_id, reason))
        self.conn.commit()

    # -- audit / artifacts / usage -------------------------------------------

    def _insert_event(self, task_id: str, kind: str, detail: dict, tool_name: str | None = None, is_write: bool | None = None) -> None:
        # two connections write the chain (orchestrator + dashboard/cli); lock before reading prev hash
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        previous = self.conn.execute("SELECT hash FROM task_events ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = (previous["hash"] if previous else None) or ""
        ts = utcnow()
        detail_json = redactor.redact(json.dumps(detail))
        self.conn.execute(
            "INSERT INTO task_events (task_id, ts, kind, tool_name, is_write, detail_json, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, ts, kind, tool_name, None if is_write is None else int(is_write), detail_json, event_hash(prev_hash, ts, task_id, kind, tool_name, detail_json)),
        )

    def add_event(self, task_id: str, kind: str, detail: dict, tool_name: str | None = None, is_write: bool | None = None) -> None:
        self._insert_event(task_id, kind, detail, tool_name, is_write)
        self.conn.commit()

    def events_for(self, task_id: str, limit: int | None = None, offset: int = 0) -> list[dict]:
        if limit is None:
            rows = self.conn.execute("SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM task_events WHERE task_id = ? ORDER BY id LIMIT ? OFFSET ?", (task_id, min(max(limit, 1), 1000), max(offset, 0))).fetchall()
        return [dict(row) for row in rows]

    def event_count(self, task_id: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM task_events WHERE task_id = ?", (task_id,)).fetchone()
        return int(row["n"])

    def events_for_kinds(self, task_id: str, kinds: set[str], limit: int = 500) -> list[dict]:
        if not kinds:
            return []
        placeholders = ",".join("?" for _ in kinds)
        rows = self.conn.execute(f"SELECT * FROM task_events WHERE task_id = ? AND kind IN ({placeholders}) ORDER BY id LIMIT ?", (task_id, *sorted(kinds), min(max(limit, 1), 1000))).fetchall()
        return [dict(row) for row in rows]

    def last_event_ts(self, task_id: str, kind: str, field: str, value: str) -> str | None:
        row = self.conn.execute(
            "SELECT ts FROM task_events WHERE task_id = ? AND kind = ? AND json_extract(detail_json, '$.' || ?) = ? ORDER BY id DESC LIMIT 1",
            (task_id, kind, field, value),
        ).fetchone()
        return str(row["ts"]) if row else None

    def events_after(self, after_id: int, limit: int = 10000) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM task_events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def latest_event_id(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM task_events").fetchone()
        return int(row["n"])

    def add_artifact(self, task_id: str, kind: str, external_id: str, url: str | None = None) -> bool:
        """true if newly recorded; false if this artifact was already known (retry-safe, ORC-012)."""
        cur = self.conn.execute("INSERT OR IGNORE INTO artifacts (task_id, kind, external_id, url, created_at) VALUES (?, ?, ?, ?, ?)", (task_id, kind, external_id, url, utcnow()))
        self.conn.commit()
        return cur.rowcount == 1

    def artifacts_for(self, task_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM artifacts WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_usage(self, task_id: str, source: str, model: str, input_tokens: int | None = None, output_tokens: int | None = None, cache_read_tokens: int | None = None, cache_write_tokens: int | None = None, cost_usd: float | None = None) -> None:
        self.conn.execute(
            "INSERT INTO usage (task_id, ts, source, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, utcnow(), source, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd),
        )
        self.conn.commit()

    def record_rate_limit(self, rate_limit_type: str, status: str, utilization: float | None, resets_at: int | None) -> None:
        self.conn.execute(
            """INSERT INTO rate_limits (rate_limit_type, status, utilization, resets_at, observed_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(rate_limit_type) DO UPDATE SET
                   status = excluded.status,
                   utilization = excluded.utilization,
                   resets_at = excluded.resets_at,
                   observed_at = excluded.observed_at""",
            (rate_limit_type, status, utilization, resets_at, utcnow()),
        )
        self.conn.commit()

    def rate_limit_windows(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM rate_limits ORDER BY rate_limit_type").fetchall()
        return [dict(row) for row in rows]

    def usage_for(self, task_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM usage WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def usage_totals(self, since_iso: str | None = None, model: str | None = None) -> dict:
        clauses: list[str] = []
        values: list[object] = []
        if since_iso:
            clauses.append("ts >= ?")
            values.append(since_iso)
        if model:
            clauses.append("model = ?")
            values.append(model)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.conn.execute(
            f"""SELECT COUNT(*) AS rows, COUNT(DISTINCT task_id) AS task_count,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd, MAX(ts) AS last_updated
                FROM usage{where}""",
            values,
        ).fetchone()
        return dict(row)

    def usage_by_model(self, since_iso: str | None = None, model: str | None = None) -> list[dict]:
        clauses: list[str] = []
        values: list[object] = []
        if since_iso:
            clauses.append("ts >= ?")
            values.append(since_iso)
        if model:
            clauses.append("model = ?")
            values.append(model)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""SELECT model, COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) + COALESCE(SUM(cache_write_tokens), 0) AS cache_tokens,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd
                FROM usage{where} GROUP BY model ORDER BY input_tokens + output_tokens + cache_tokens DESC""",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    def usage_timeseries(self, since_iso: str, bucket_hours: int = 1) -> list[dict]:
        """hourly-bucketed totals per model for charts; ts is iso so substr gives the hour."""
        rows = self.conn.execute(
            """SELECT substr(ts, 1, 13) AS bucket, model,
                      COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
                      + COALESCE(SUM(cache_read_tokens), 0) + COALESCE(SUM(cache_write_tokens), 0) AS total_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(cost_usd), 0) AS cost_usd
               FROM usage WHERE ts >= ? GROUP BY bucket, model ORDER BY bucket""",
            (since_iso,),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- permission requests -------------------------------------------------

    def request_permission(self, task_id: str, kind: str, target: str, reason: str) -> dict:
        """record (or re-open) a sub-agent's request for one additional tool or repo. retry-safe: a repeat resets it to pending."""
        now = utcnow()
        self.conn.execute(
            """INSERT INTO permission_requests (task_id, kind, target, reason, status, requested_at)
               VALUES (?, ?, ?, ?, 'pending', ?)
               ON CONFLICT(task_id, kind, target) DO UPDATE SET
                   reason = excluded.reason, status = 'pending', requested_at = excluded.requested_at, decided_by = NULL, decided_at = NULL""",
            (task_id, kind, target, redactor.redact(reason), now),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM permission_requests WHERE task_id = ? AND kind = ? AND target = ?", (task_id, kind, target)).fetchone()
        return dict(row)

    def permission_requests_for(self, task_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM permission_requests WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def decide_permission_request(self, task_id: str, kind: str, target: str, status: str, actor: str) -> dict | None:
        """grant or deny a pending request; returns the updated row, or None when nothing was pending (already decided or unknown)."""
        if status not in ("granted", "denied"):
            raise ValueError(f"permission decision must be 'granted' or 'denied', got {status!r}")
        cur = self.conn.execute(
            "UPDATE permission_requests SET status = ?, decided_by = ?, decided_at = ? WHERE task_id = ? AND kind = ? AND target = ? AND status = 'pending'",
            (status, actor, utcnow(), task_id, kind, target),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        row = self.conn.execute("SELECT * FROM permission_requests WHERE task_id = ? AND kind = ? AND target = ?", (task_id, kind, target)).fetchone()
        return dict(row)

    def granted_permissions_for(self, task_id: str) -> dict[str, list[str]]:
        """tools and repos an operator has granted this task — the runner merges them into the session's scope on (re)start."""
        rows = self.conn.execute("SELECT kind, target FROM permission_requests WHERE task_id = ? AND status = 'granted' ORDER BY id", (task_id,)).fetchall()
        result: dict[str, list[str]] = {"tools": [], "repos": []}
        for row in rows:
            bucket = "tools" if row["kind"] == "tool" else "repos"
            if row["target"] not in result[bucket]:
                result[bucket].append(row["target"])
        return result

    # -- follow-up questions ---------------------------------------------------

    def ask_questions(self, task_id: str, questions: str) -> dict:
        """record one round of follow-up questions for the requester."""
        cur = self.conn.execute("INSERT INTO task_questions (task_id, questions, asked_at) VALUES (?, ?, ?)", (task_id, redactor.redact(questions), utcnow()))
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM task_questions WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def pending_questions_for(self, task_id: str) -> dict | None:
        """the newest unanswered question round, if any — the thing a thread reply would be answering."""
        row = self.conn.execute("SELECT * FROM task_questions WHERE task_id = ? AND answered_at IS NULL ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        return dict(row) if row else None

    def answer_questions(self, task_id: str, answer_text: str, answered_by: str) -> dict | None:
        """attach the requester's answer to the pending round; returns None when nothing was pending."""
        pending = self.pending_questions_for(task_id)
        if pending is None:
            return None
        self.conn.execute("UPDATE task_questions SET answer_text = ?, answered_by = ?, answered_at = ? WHERE id = ?", (redactor.redact(answer_text), answered_by, utcnow(), pending["id"]))
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM task_questions WHERE id = ?", (pending["id"],)).fetchone()
        return dict(row)

    def answered_questions_for(self, task_id: str) -> list[dict]:
        """all answered rounds in order — the runner replays them into the prompt on (re)start."""
        rows = self.conn.execute("SELECT * FROM task_questions WHERE task_id = ? AND answered_at IS NOT NULL ORDER BY id", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def questions_for(self, task_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM task_questions WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    # -- task feedback (dashboard input, read by the issues agent) --

    def add_feedback(self, task_id: str, submitted_by: str, rating: int, comment: str | None = None) -> dict:
        """one row per (task, reviewer); resubmitting revises the earlier rating."""
        now = utcnow()
        self.conn.execute(
            """INSERT INTO task_feedback (task_id, submitted_by, rating, comment, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id, submitted_by) DO UPDATE SET rating = excluded.rating, comment = excluded.comment, updated_at = excluded.updated_at""",
            (task_id, submitted_by, rating, redactor.redact(comment) if comment else None, now, now),
        )
        self._insert_event(task_id, "feedback", {"by": submitted_by, "rating": rating})
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM task_feedback WHERE task_id = ? AND submitted_by = ?", (task_id, submitted_by)).fetchone()
        return dict(row)

    def feedback_for(self, task_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM task_feedback WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def recent_feedback(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM task_feedback ORDER BY updated_at DESC, id DESC LIMIT ?", (min(max(limit, 1), 1000),)).fetchall()
        return [dict(row) for row in rows]

    # -- issues (issues loop: discovery -> approval -> implementation) --

    def record_issue(self, dedupe_key: str, repo: str, summary: str, issue_type: str, details: str, priority: int, source: dict | None = None) -> dict:
        """record (or refresh) a proposed issue. re-running discovery updates the content and priority of an
        existing proposal without disturbing an operator's decision or an in-flight implementation."""
        now = utcnow()
        source_json = redactor.redact(json.dumps(source, default=str)) if source is not None else None
        self.conn.execute(
            """INSERT INTO issues (dedupe_key, repo, summary, issue_type, details, priority, source_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(dedupe_key) DO UPDATE SET
                   repo = excluded.repo, summary = excluded.summary, issue_type = excluded.issue_type,
                   details = excluded.details, priority = excluded.priority, source_json = excluded.source_json,
                   updated_at = excluded.updated_at
               WHERE issues.status = 'proposed'""",
            (dedupe_key, repo, redactor.redact(summary[:300]), issue_type[:60], redactor.redact(details[:4000]), _clamp_priority(priority), source_json, now, now),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM issues WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
        return dict(row)

    def list_issues(self, status: str | None = None, limit: int = 500, offset: int = 0, repo: str | None = None) -> list[dict]:
        """issues ordered most-important first; rank is this ordering, so a new higher-priority item shifts the rest."""
        limit = min(max(limit, 1), 1000)
        offset = max(offset, 0)
        clauses: list[str] = []
        values: list[object] = []
        if status:
            clauses.append("status = ?")
            values.append(status)
        if repo:
            clauses.append("repo = ?")
            values.append(repo)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(f"SELECT * FROM issues{where} ORDER BY priority DESC, id LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
        return [dict(row) for row in rows]

    def get_issue(self, issue_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        return dict(row) if row else None

    def decide_issue(self, issue_id: int, status: str, actor: str) -> dict | None:
        """operator approve/deny from the dashboard. only a not-yet-implemented row may be re-decided; returns None otherwise."""
        if status not in ("approved", "denied", "proposed"):
            raise ValueError(f"issue decision must be approved, denied, or proposed, got {status!r}")
        cur = self.conn.execute(
            "UPDATE issues SET status = ?, decided_by = ?, decided_at = ?, updated_at = ? WHERE id = ? AND status IN ('proposed','approved','denied')",
            (status, actor, utcnow(), utcnow(), issue_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_issue(issue_id)

    def update_issue(self, issue_id: int, summary: str, details: str, priority: int | None = None) -> dict | None:
        """operator edit from the dashboard: correct the summary/details before it is decided or implemented."""
        if priority is None:
            current = self.get_issue(issue_id)
            if current is None:
                return None
            priority = current["priority"]
        cur = self.conn.execute(
            "UPDATE issues SET summary = ?, details = ?, priority = ?, updated_at = ? WHERE id = ? AND status IN ('proposed','approved')",
            (redactor.redact(summary[:300]), redactor.redact(details[:4000]), _clamp_priority(priority), utcnow(), issue_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_issue(issue_id)

    def set_issue_priority(self, issue_id: int, priority: int) -> dict | None:
        """set priority while an issue is still editable; callers validate the 1-100 input before clamping."""
        cur = self.conn.execute(
            "UPDATE issues SET priority = ?, updated_at = ? WHERE id = ? AND status IN ('proposed','approved')",
            (_clamp_priority(priority), utcnow(), issue_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_issue(issue_id)

    def delete_issue(self, issue_id: int) -> dict | None:
        """hard-delete the issue with its comments/attachments; blocked while in_progress/in_review."""
        row = self.get_issue(issue_id)
        if row is None or row["status"] in ("in_progress", "in_review"):
            return None
        self.conn.execute("DELETE FROM issue_attachments WHERE issue_id = ?", (issue_id,))
        self.conn.execute("DELETE FROM issue_comments WHERE issue_id = ?", (issue_id,))
        cur = self.conn.execute("DELETE FROM issues WHERE id = ?", (issue_id,))
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return row

    def start_issue(self, issue_id: int, task_id: str | None, spec: str) -> dict | None:
        """the implementation agent claims a reserved issue: store its spec and (once known) link the PR task it
        enqueued. plain 'approved' is still accepted too, for the legacy manual-enqueue path."""
        cur = self.conn.execute(
            "UPDATE issues SET status = 'in_progress', spec = ?, task_id = ?, updated_at = ? WHERE id = ? AND status IN ('approved','implementation_queued')",
            (redactor.redact(spec), task_id, utcnow(), issue_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_issue(issue_id)

    def link_issue_task(self, issue_id: int, task_id: str) -> None:
        """attach the spec2pr child task id to an issue already claimed by start_issue."""
        self.conn.execute("UPDATE issues SET task_id = ?, updated_at = ? WHERE id = ?", (task_id, utcnow(), issue_id))
        self.conn.commit()

    def active_implementation_run(self) -> str | None:
        """the id of a not-yet-terminal /implementapprovedissues coordinator task, if one exists. used to keep the
        dashboard button disabled and repeated clicks from starting a second coordinator."""
        row = self.conn.execute("SELECT task_id FROM tasks WHERE request_text LIKE '/implementapprovedissues%' AND state IN ('received','queued','running','blocked') ORDER BY created_at DESC LIMIT 1").fetchone()
        return row["task_id"] if row else None

    def active_refine_task(self, issue_id: int) -> str | None:
        """return the active /refineissue task for exactly this id, without letting #7 match #70."""
        return self.active_refine_tasks_by_issue().get(issue_id)

    def active_refine_tasks_by_issue(self) -> dict[int, str]:
        """return the newest active /refineissue task for each issue in one task-table scan."""
        rows = self.conn.execute(
            "SELECT task_id, request_text FROM tasks WHERE state IN ('received','queued','running','blocked') ORDER BY created_at DESC",
        ).fetchall()
        pattern = re.compile(r"/refineissue ([0-9]+)(?![0-9])")
        result: dict[int, str] = {}
        for row in rows:
            match = pattern.search(row["request_text"] or "")
            if match:
                result.setdefault(int(match.group(1)), row["task_id"])
        return result

    def has_active_main_task_referencing(self, fragment: str) -> bool:
        """True if a non-terminal, non-reviewer task's request text references `fragment` as a bounded token (e.g. a PR URL); excludes `blocked` so a main-agent task parked on follow-up questions can't suppress reviews indefinitely."""
        rows = self.conn.execute(
            "SELECT request_text FROM tasks WHERE state IN ('received','queued','running') AND (persona IS NULL OR persona != 'reviewer')",
        ).fetchall()
        # bounded match, not a raw substring: require a non-digit (or end of string) after the fragment so .../pull/7 doesn't match .../pull/70, and escape the fragment so any _/% in a repo/org name aren't treated as wildcards
        pattern = re.compile(re.escape(fragment) + r"(?![0-9])")
        return any(row["request_text"] and pattern.search(row["request_text"]) for row in rows)

    def reserve_issues(self, reserved_by: str, limit: int = 5) -> list[dict]:
        """atomically reserve up to `limit` highest-priority approved issues for one implementation run,
        moving them to implementation_queued. one UPDATE, synchronous on the shared connection, so two callers
        racing this can never reserve the same row."""
        self.conn.execute(
            """UPDATE issues SET status = 'implementation_queued', reserved_by = ?, updated_at = ?
               WHERE id IN (SELECT id FROM issues WHERE status = 'approved' ORDER BY priority DESC, id LIMIT ?)""",
            (reserved_by, utcnow(), limit),
        )
        self.conn.commit()
        return self.issues_reserved_by(reserved_by)

    def issues_reserved_by(self, reserved_by: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM issues WHERE status = 'implementation_queued' AND reserved_by = ? ORDER BY priority DESC, id", (reserved_by,)).fetchall()
        return [dict(row) for row in rows]

    def assign_reservation(self, reserved_by: str, task_id: str) -> int:
        """move a pending marker's reservation onto the real coordinator task id once accept_task creates it."""
        cur = self.conn.execute(
            "UPDATE issues SET reserved_by = ?, updated_at = ? WHERE reserved_by = ? AND status = 'implementation_queued'",
            (task_id, utcnow(), reserved_by),
        )
        self.conn.commit()
        return cur.rowcount

    def release_reserved_issues(self, reserved_by: str) -> int:
        """restore issues reserved by `reserved_by` back to approved; a no-op for anything not holding a
        reservation. used when a coordinator never gets created, or dies/finishes with rows still queued."""
        cur = self.conn.execute(
            "UPDATE issues SET status = 'approved', reserved_by = NULL, updated_at = ? WHERE reserved_by = ? AND status = 'implementation_queued'",
            (utcnow(), reserved_by),
        )
        self.conn.commit()
        return cur.rowcount

    def release_stale_reservations(self) -> int:
        """startup safety net: restore every implementation_queued row whose reservation can never be fulfilled —
        no reserved_by, a pending marker that never became a task, or a coordinator task already in a terminal state."""
        cur = self.conn.execute(
            """UPDATE issues SET status = 'approved', reserved_by = NULL, updated_at = ?
               WHERE status = 'implementation_queued' AND (
                   reserved_by IS NULL
                   OR reserved_by LIKE 'pending:%'
                   OR reserved_by NOT IN (SELECT task_id FROM tasks)
                   OR reserved_by IN (SELECT task_id FROM tasks WHERE state IN ('completed','failed','cancelled','refused'))
               )""",
            (utcnow(),),
        )
        self.conn.commit()
        return cur.rowcount

    def finish_issue(self, issue_id: int, status: str, pr_url: str | None = None) -> dict | None:
        """records the outcome of a PR attempt (spec2pr agent) or resolves one whose PR has now merged/closed
        (the sync_in_review housekeeping process) — both transition out of in_progress or in_review alike."""
        if status not in ("in_review", "done", "failed"):
            raise ValueError(f"issue finish status must be in_review, done, or failed, got {status!r}")
        cur = self.conn.execute(
            "UPDATE issues SET status = ?, pr_url = ?, updated_at = ? WHERE id = ? AND status IN ('in_progress','in_review')",
            (status, pr_url, utcnow(), issue_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_issue(issue_id)

    # -- issue discussion -----------------------------------------------------

    def add_issue_comment(self, issue_id: int, author: str, body: str, parent_comment_id: int | None = None) -> dict:
        if self.get_issue(issue_id) is None:
            raise LookupError("issue not found")
        if parent_comment_id is not None:
            parent = self.get_issue_comment(parent_comment_id)
            if parent is None or parent["issue_id"] != issue_id:
                raise ValueError("parent comment must belong to this issue")
            if parent["parent_comment_id"] is not None:
                raise ValueError("replies may only target top-level comments")
        now = utcnow()
        cur = self.conn.execute(
            """INSERT INTO issue_comments (issue_id, parent_comment_id, author, body, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (issue_id, parent_comment_id, author, redactor.redact(body[:4000]), now, now),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM issue_comments WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def get_issue_comment(self, comment_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM issue_comments WHERE id = ?", (comment_id,)).fetchone()
        return dict(row) if row else None

    def list_issue_comments(self, issue_id: int) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM issue_comments WHERE issue_id = ? ORDER BY created_at, id", (issue_id,)).fetchall()
        return [dict(row) for row in rows]

    def count_issue_comments(self, issue_id: int) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM issue_comments WHERE issue_id = ?", (issue_id,)).fetchone()
        return int(row["n"])

    def count_issue_comments_by_issue(self) -> dict[int, int]:
        rows = self.conn.execute("SELECT issue_id, COUNT(*) AS n FROM issue_comments GROUP BY issue_id").fetchall()
        return {int(row["issue_id"]): int(row["n"]) for row in rows}

    def update_issue_comment(self, comment_id: int, body: str) -> dict | None:
        now = utcnow()
        cur = self.conn.execute(
            "UPDATE issue_comments SET body = ?, edited_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (redactor.redact(body[:4000]), now, now, comment_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_issue_comment(comment_id)

    def delete_issue_comment(self, comment_id: int) -> dict | None:
        now = utcnow()
        cur = self.conn.execute(
            "UPDATE issue_comments SET body = '', deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (now, now, comment_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_issue_comment(comment_id)

    def resolve_issue_comment(self, comment_id: int, actor: str, resolved: bool = True) -> dict | None:
        now = utcnow()
        cur = self.conn.execute(
            "UPDATE issue_comments SET resolved = ?, resolved_by = ?, resolved_at = ?, updated_at = ? WHERE id = ?",
            (int(resolved), actor if resolved else None, now if resolved else None, now, comment_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_issue_comment(comment_id)

    # -- issue attachments ----------------------------------------------------

    def add_issue_attachment(self, issue_id: int, comment_id: int | None, filename: str, content_type: str | None, size_bytes: int, s3_key: str, uploaded_by: str) -> dict:
        now = utcnow()
        cur = self.conn.execute(
            """INSERT INTO issue_attachments (issue_id, comment_id, filename, content_type, size_bytes, s3_key, uploaded_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (issue_id, comment_id, filename, content_type, size_bytes, s3_key, uploaded_by, now),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM issue_attachments WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def list_issue_attachments(self, issue_id: int) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM issue_attachments WHERE issue_id = ? ORDER BY id", (issue_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_issue_attachment(self, attachment_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM issue_attachments WHERE id = ?", (attachment_id,)).fetchone()
        return dict(row) if row else None

    # -- schedules (dashboard-defined recurring/one-off tasks) ----------------

    def create_schedule(
        self, *, name: str, request_text: str, model_alias: str | None, kind: str, interval_minutes: int | None, at_time: str | None, run_at: str | None, timezone: str | None, max_runs: int | None, next_run_at: str, created_by: str, seed_key: str | None = None, effort: str | None = None
    ) -> dict:
        now = utcnow()
        cur = self.conn.execute(
            """INSERT INTO schedules (name, request_text, model_alias, effort, kind, interval_minutes, at_time, run_at, timezone, max_runs, next_run_at, seed_key, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (redactor.redact(name), redactor.redact(request_text), model_alias, effort, kind, interval_minutes, at_time, run_at, timezone, max_runs, next_run_at, seed_key, created_by, now, now),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM schedules WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def seed_schedule_once(self, seed_key: str, **fields) -> dict | None:
        """create a shipped default schedule exactly once. returns the row if created, None if the seed key already existed."""
        cur = self.conn.execute("SELECT 1 FROM schedules WHERE seed_key = ?", (seed_key,)).fetchone()
        if cur is not None:
            return None
        return self.create_schedule(seed_key=seed_key, **fields)

    def list_schedules(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM schedules ORDER BY enabled DESC, next_run_at").fetchall()
        return [dict(row) for row in rows]

    def get_schedule(self, schedule_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        return dict(row) if row else None

    def due_schedules(self, now_iso: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM schedules WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at", (now_iso,)).fetchall()
        return [dict(row) for row in rows]

    def update_schedule(self, schedule_id: int, **fields) -> dict | None:
        unknown = set(fields) - SCHEDULE_UPDATABLE
        if unknown:
            raise ValueError(f"update_schedule cannot update fields: {sorted(unknown)}")
        if "request_text" in fields and isinstance(fields["request_text"], str):
            fields["request_text"] = redactor.redact(fields["request_text"])
        if "name" in fields and isinstance(fields["name"], str):
            fields["name"] = redactor.redact(fields["name"])
        sets = ", ".join(f"{name} = ?" for name in fields)
        cur = self.conn.execute(f"UPDATE schedules SET updated_at = ?, {sets} WHERE id = ?", (utcnow(), *fields.values(), schedule_id))
        self.conn.commit()
        return self.get_schedule(schedule_id) if cur.rowcount else None

    def record_schedule_fire(self, schedule_id: int, task_id: str | None, next_run_at: str, enabled: bool) -> None:
        """advance a schedule after a fire: bump run_count, remember the task, set the next fire time, and disable if exhausted."""
        self.conn.execute(
            "UPDATE schedules SET run_count = run_count + 1, last_run_at = ?, last_task_id = ?, next_run_at = ?, enabled = ?, updated_at = ? WHERE id = ?",
            (utcnow(), task_id, next_run_at, 1 if enabled else 0, utcnow(), schedule_id),
        )
        self.conn.commit()

    def delete_schedule(self, schedule_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # -- requester profiles / errors -----------------------------------------

    def upsert_slack_user(self, user_id: str, **fields) -> dict:
        allowed = {"team_id", "username", "real_name", "display_name", "email", "title", "tz", "is_bot"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown slack user fields: {sorted(unknown)}")
        values = {key: redactor.redact(value) if isinstance(value, str) else value for key, value in fields.items()}
        columns = ["user_id", *values, "updated_at"]
        params = [user_id, *values.values(), utcnow()]
        updates = ", ".join(f"{column} = excluded.{column}" for column in [*values, "updated_at"])
        placeholders = ", ".join("?" for _ in columns)
        self.conn.execute(f"INSERT INTO slack_users ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT(user_id) DO UPDATE SET {updates}", params)
        self.conn.commit()
        result = self.get_slack_user(user_id)
        assert result is not None
        return result

    def get_slack_user(self, user_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM slack_users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def add_error(self, component: str, kind: str, message: str, task_id: str | None = None, traceback: str | None = None, context: dict | None = None) -> None:
        self.conn.execute(
            "INSERT INTO errors (ts, task_id, component, kind, message, traceback, context_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (utcnow(), task_id, component, kind, redactor.redact(str(message)), redactor.redact(traceback) if traceback else None, redactor.redact(json.dumps(context, default=str)) if context is not None else None),
        )
        self.conn.commit()

    def recent_errors(self, limit: int = 20, offset: int = 0, component: str | None = None, kind: str | None = None) -> list[dict]:
        clauses: list[str] = []
        values: list[object] = []
        if component:
            clauses.append("component = ?")
            values.append(component)
        if kind:
            clauses.append("kind = ?")
            values.append(kind)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(f"SELECT * FROM errors{where} ORDER BY id DESC LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
        return [dict(row) for row in rows]

    def errors_for(self, task_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM errors WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    # -- dashboard administration audit -------------------------------------

    def add_admin_event(self, actor: str, action: str, target: str, outcome: str, detail: dict | None = None) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        previous = self.conn.execute("SELECT hash FROM admin_events ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = previous["hash"] if previous else ""
        ts = utcnow()
        detail_json = redactor.redact(json.dumps(detail or {}, default=str, sort_keys=True))
        digest = admin_event_hash(prev_hash, ts, actor, action, target, outcome, detail_json)
        self.conn.execute("INSERT INTO admin_events (ts, actor, action, target, outcome, detail_json, hash) VALUES (?, ?, ?, ?, ?, ?, ?)", (ts, actor, action, target, outcome, detail_json, digest))
        self.conn.commit()

    def admin_events(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM admin_events ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 1000),)).fetchall()
        return [dict(row) for row in rows]

    def admin_events_after(self, after_id: int, limit: int = 10000) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM admin_events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
        return [dict(row) for row in rows]

    # -- meta -----------------------------------------------------------------

    def meta_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.conn.commit()
