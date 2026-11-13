import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "success_with_warnings", "timed_out"}

class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def connection(self):
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init(self):
        with self.connection() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, repository TEXT NOT NULL, task TEXT NOT NULL,
              test_command TEXT NOT NULL DEFAULT 'auto', max_agent_steps INTEGER, status TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT,
              finished_at TEXT, duration_ms INTEGER, success INTEGER, total_tokens INTEGER NOT NULL DEFAULT 0,
              prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
              llm_calls INTEGER NOT NULL DEFAULT 0, result_json TEXT, metrics_json TEXT NOT NULL DEFAULT '{}',
              error TEXT, workspace_path TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
              timestamp TEXT NOT NULL, event_type TEXT NOT NULL, agent TEXT, message TEXT NOT NULL,
              data_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
            """)
            # Backward-compatible migration for databases created by earlier project builds.
            columns = {row[1] for row in c.execute("PRAGMA table_info(runs)").fetchall()}
            if "max_agent_steps" not in columns:
                c.execute("ALTER TABLE runs ADD COLUMN max_agent_steps INTEGER")

    def create_run(self, run_id: str, repository: str, task: str, test_command: str, max_agent_steps: int | None = None):
        now = utcnow()
        with self.connection() as c:
            c.execute("INSERT INTO runs(id,repository,task,test_command,max_agent_steps,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                      (run_id, repository, task, test_command, max_agent_steps, "queued", now, now))
        self.add_event(run_id, "run_queued", None, "Run queued", {"repository": repository})

    def update_run(self, run_id: str, force: bool = False, **fields):
        allowed = {"status", "updated_at", "started_at", "finished_at", "duration_ms", "success", "total_tokens",
                   "prompt_tokens", "completion_tokens", "llm_calls", "result_json", "metrics_json", "error", "workspace_path"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        fields.setdefault("updated_at", utcnow())
        if not fields: return

        # Enforce terminal state immutability unless force is explicitly set
        with self.connection() as c:
            if not force and "status" in fields:
                cur = c.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
                if cur and cur[0] in TERMINAL_STATUSES:
                    new_status = fields["status"]
                    # If already in an unrecoverable terminal state, do not overwrite status
                    if cur[0] in {"cancelled", "timed_out", "failed"}:
                        fields.pop("status", None)
                    elif new_status not in TERMINAL_STATUSES:
                        fields.pop("status", None)
                    if not fields:
                        return

            assignments = ",".join(f"{key}=?" for key in fields)
            c.execute(f"UPDATE runs SET {assignments} WHERE id=?", (*fields.values(), run_id))

    def cleanup_stale_runs(self) -> int:
        """Mark any runs left in non-terminal states after server restart/crash as failed."""
        now = utcnow()
        with self.connection() as c:
            cur = c.execute("""
                UPDATE runs
                SET status='failed', finished_at=?, error='Execution interrupted: server restarted or worker process terminated unexpectedly.'
                WHERE status NOT IN ('succeeded', 'failed', 'cancelled', 'success_with_warnings', 'timed_out')
            """, (now,))
            return cur.rowcount

    def add_event(self, run_id: str, event_type: str, agent: str | None, message: str, data: dict[str, Any] | None = None):
        with self.connection() as c:
            c.execute("INSERT INTO events(run_id,timestamp,event_type,agent,message,data_json) VALUES(?,?,?,?,?,?)",
                      (run_id, utcnow(), event_type, agent, message, json.dumps(data or {}, ensure_ascii=False)))

    def get_run(self, run_id: str):
        with self.connection() as c:
            row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 50):
        with self.connection() as c:
            rows = c.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get_events(self, run_id: str, after_id: int = 0, limit: int = 500):
        with self.connection() as c:
            rows = c.execute("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?", (run_id, after_id, limit)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["data"] = json.loads(item.pop("data_json") or "{}")
            result.append(item)
        return result

    def aggregate_metrics(self):
        with self.connection() as c:
            row = c.execute("""SELECT COUNT(*) total,
              SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) successes,
              AVG(CASE WHEN duration_ms IS NOT NULL THEN duration_ms END) avg_duration_ms,
              SUM(total_tokens) total_tokens, SUM(llm_calls) llm_calls,
              SUM(prompt_tokens) prompt_tokens, SUM(completion_tokens) completion_tokens
              FROM runs WHERE status IN ('succeeded','failed','success_with_warnings','timed_out')""").fetchone()
            recent = c.execute("SELECT duration_ms FROM runs WHERE status IN ('succeeded','failed','success_with_warnings','timed_out') AND duration_ms IS NOT NULL ORDER BY duration_ms").fetchall()
        values = dict(row)
        durations = [r[0] for r in recent]
        p95 = durations[min(len(durations)-1, int((len(durations)-1)*0.95))] if durations else None
        total = values.get("total") or 0
        successes = values.get("successes") or 0
        return {
            "completed_runs": total, "successful_runs": successes,
            "success_rate": round(successes / total * 100, 1) if total else None,
            "average_duration_ms": round(values.get("avg_duration_ms")) if values.get("avg_duration_ms") is not None else None,
            "p95_duration_ms": p95, "total_tokens": values.get("total_tokens") or 0,
            "llm_calls": values.get("llm_calls") or 0, "prompt_tokens": values.get("prompt_tokens") or 0,
            "completion_tokens": values.get("completion_tokens") or 0,
        }
