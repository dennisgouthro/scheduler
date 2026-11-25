"""SQLite persistence layer (WAL mode, zero external dependencies)."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .schedule_math import compute_next_run, describe_schedule, normalize_schedule, weekly_slots


def iso_now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    script_ref TEXT NOT NULL,
                    args TEXT NOT NULL DEFAULT '',
                    python_exe TEXT NOT NULL DEFAULT '',
                    dashboard_url TEXT NOT NULL DEFAULT '',
                    freshness_path TEXT NOT NULL DEFAULT '',
                    schedule_type TEXT NOT NULL DEFAULT 'manual',
                    schedule_config TEXT NOT NULL DEFAULT '{}',
                    timeout_minutes INTEGER NOT NULL DEFAULT 45,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    owner TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    next_run_at TEXT
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    trigger TEXT NOT NULL DEFAULT 'manual',
                    triggered_by TEXT NOT NULL DEFAULT '',
                    queued_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    duration_sec REAL,
                    exit_code INTEGER,
                    error_summary TEXT NOT NULL DEFAULT '',
                    log_file TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_runs_job_queued ON runs(job_id, queued_at DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_next_run ON jobs(enabled, next_run_at);
                """
            )

    def recover_orphaned_runs(self) -> int:
        """Mark any runs left active from a prior crash/restart as stopped."""
        now = iso_now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE runs
                SET status = 'stopped',
                    finished_at = ?,
                    error_summary = 'Scheduler service restarted while this run was active.'
                WHERE status IN ('running', 'queued')
                """,
                (now,),
            )
            return cur.rowcount

    def _serialize_job(
        self,
        row: sqlite3.Row,
        recent_runs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        schedule_cfg = json.loads(row["schedule_config"] or "{}")
        enabled = bool(row["enabled"])
        last_run = recent_runs[0] if recent_runs else None
        active = next((r for r in recent_runs if r["status"] in ("running", "queued")), None)
        # Recent runs returned oldest->newest for sparkline/dot strip, plus last_run
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "script_ref": row["script_ref"],
            "args": row["args"],
            "python_exe": row["python_exe"],
            "dashboard_url": row["dashboard_url"],
            "freshness_path": row["freshness_path"],
            "schedule_type": row["schedule_type"],
            "schedule_config": schedule_cfg,
            "schedule_label": describe_schedule(row["schedule_type"], schedule_cfg),
            "weekly_slots": weekly_slots(row["schedule_type"], schedule_cfg, enabled=enabled),
            "timeout_minutes": row["timeout_minutes"],
            "enabled": enabled,
            "owner": row["owner"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
            "next_run_at": row["next_run_at"],
            "last_run": last_run,
            "active_run": active,
            "recent_runs": list(reversed(recent_runs)),
        }

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            job_rows = conn.execute("SELECT * FROM jobs ORDER BY LOWER(name) ASC").fetchall()
            runs_by_job: dict[str, list[dict[str, Any]]] = {r["id"]: [] for r in job_rows}

            if job_rows:
                # Fetch up to 20 most recent runs per job using window function
                run_rows = conn.execute(
                    """
                    SELECT * FROM (
                        SELECT id, job_id, status, trigger, triggered_by,
                               queued_at, started_at, finished_at, duration_sec,
                               exit_code, error_summary,
                               ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY queued_at DESC) AS rn
                        FROM runs
                    )
                    WHERE rn <= 20
                    ORDER BY queued_at DESC
                    """
                ).fetchall()
                for rr in run_rows:
                    jid = rr["job_id"]
                    if jid in runs_by_job:
                        runs_by_job[jid].append(dict(rr))

            return [self._serialize_job(jr, runs_by_job.get(jr["id"], [])) for jr in job_rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return None
            run_rows = conn.execute(
                """
                SELECT id, job_id, status, trigger, triggered_by,
                       queued_at, started_at, finished_at, duration_sec,
                       exit_code, error_summary
                FROM runs
                WHERE job_id = ?
                ORDER BY queued_at DESC
                LIMIT 20
                """,
                (job_id,),
            ).fetchall()
            return self._serialize_job(row, [dict(r) for r in run_rows])

    def create_job(self, payload: dict[str, Any], user: str, default_timeout: int = 45) -> dict[str, Any]:
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        now = iso_now()
        stype, scfg = normalize_schedule(payload.get("schedule_type", "manual"), payload.get("schedule_config"))
        enabled = bool(payload.get("enabled", True))
        nxt = compute_next_run(stype, scfg, enabled=enabled)
        timeout = max(1, min(480, int(payload.get("timeout_minutes") or default_timeout)))
        owner = str(payload.get("owner") or user).strip() or user

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, name, description, script_ref, args, python_exe,
                    dashboard_url, freshness_path, schedule_type, schedule_config,
                    timeout_minutes, enabled, owner, created_by, created_at,
                    updated_by, updated_at, next_run_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    str(payload["name"]).strip(),
                    str(payload.get("description") or "").strip(),
                    str(payload["script_ref"]).strip(),
                    str(payload.get("args") or "").strip(),
                    str(payload.get("python_exe") or "").strip(),
                    str(payload.get("dashboard_url") or "").strip(),
                    str(payload.get("freshness_path") or "").strip(),
                    stype,
                    json.dumps(scfg),
                    timeout,
                    1 if enabled else 0,
                    owner,
                    user,
                    now,
                    user,
                    now,
                    nxt.isoformat() if nxt else None,
                ),
            )
        job = self.get_job(job_id)
        assert job is not None
        return job

    def update_job(self, job_id: str, payload: dict[str, Any], user: str) -> dict[str, Any] | None:
        existing = self.get_job(job_id)
        if not existing:
            return None

        name = str(payload.get("name", existing["name"])).strip()
        if not name:
            raise ValueError("Schedule name cannot be empty.")

        stype, scfg = normalize_schedule(
            payload.get("schedule_type", existing["schedule_type"]),
            payload.get("schedule_config", existing["schedule_config"]),
        )
        enabled = bool(payload.get("enabled", existing["enabled"]))
        nxt = compute_next_run(stype, scfg, enabled=enabled)
        timeout = max(1, min(480, int(payload.get("timeout_minutes", existing["timeout_minutes"]))))
        now = iso_now()

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs SET
                    name = ?,
                    description = ?,
                    script_ref = ?,
                    args = ?,
                    python_exe = ?,
                    dashboard_url = ?,
                    freshness_path = ?,
                    schedule_type = ?,
                    schedule_config = ?,
                    timeout_minutes = ?,
                    enabled = ?,
                    owner = ?,
                    updated_by = ?,
                    updated_at = ?,
                    next_run_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    str(payload.get("description", existing["description"])).strip(),
                    str(payload.get("script_ref", existing["script_ref"])).strip(),
                    str(payload.get("args", existing["args"])).strip(),
                    str(payload.get("python_exe", existing["python_exe"])).strip(),
                    str(payload.get("dashboard_url", existing["dashboard_url"])).strip(),
                    str(payload.get("freshness_path", existing["freshness_path"])).strip(),
                    stype,
                    json.dumps(scfg),
                    timeout,
                    1 if enabled else 0,
                    str(payload.get("owner", existing["owner"])).strip() or existing["owner"],
                    user,
                    now,
                    nxt.isoformat() if nxt else None,
                    job_id,
                ),
            )
        return self.get_job(job_id)

    def delete_job(self, job_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            return cur.rowcount > 0

    def claim_due_jobs(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Find enabled jobs due at or before `now` and advance their next_run_at atomically."""
        ref = (now or datetime.now()).replace(microsecond=0)
        ref_iso = ref.isoformat()
        due: list[dict[str, Any]] = []

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                """,
                (ref_iso,),
            ).fetchall()

            for row in rows:
                scfg = json.loads(row["schedule_config"] or "{}")
                nxt = compute_next_run(row["schedule_type"], scfg, after=ref, enabled=True)
                conn.execute(
                    "UPDATE jobs SET next_run_at = ? WHERE id = ?",
                    (nxt.isoformat() if nxt else None, row["id"]),
                )
                due.append(dict(row))
        return due

    def has_active_run(self, job_id: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE job_id = ? AND status IN ('queued', 'running') LIMIT 1",
                (job_id,),
            ).fetchone()
            return row is not None

    def create_run(self, job_id: str, trigger: str, triggered_by: str, log_file: str) -> dict[str, Any]:
        run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = iso_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    id, job_id, status, trigger, triggered_by, queued_at, log_file
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?)
                """,
                (run_id, job_id, trigger, triggered_by, now, log_file),
            )
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return dict(row)

    def mark_run_started(self, run_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = 'running', started_at = ? WHERE id = ?",
                (iso_now(), run_id),
            )

    def mark_run_finished(
        self,
        run_id: str,
        status: str,
        exit_code: int | None,
        duration_sec: float,
        error_summary: str = "",
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?, duration_sec = ?,
                    exit_code = ?, error_summary = ?
                WHERE id = ?
                """,
                (status, iso_now(), round(duration_sec, 2), exit_code, error_summary[:500], run_id),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return dict(row) if row else None

    def list_runs_for_job(self, job_id: str, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE job_id = ? ORDER BY queued_at DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_recent_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*,
                       j.name AS job_name,
                       j.owner AS job_owner,
                       j.script_ref AS script_ref,
                       j.dashboard_url AS dashboard_url
                FROM runs r
                JOIN jobs j ON j.id = r.job_id
                ORDER BY r.queued_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def prune_old_runs(self, retention_days: int) -> list[str]:
        """Delete run records older than retention_days and return their log paths."""
        cutoff = (datetime.now() - timedelta(days=retention_days)).replace(microsecond=0).isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, log_file FROM runs WHERE queued_at < ? AND status NOT IN ('queued', 'running')",
                (cutoff,),
            ).fetchall()
            log_files = [r["log_file"] for r in rows if r["log_file"]]
            if rows:
                conn.execute(
                    "DELETE FROM runs WHERE queued_at < ? AND status NOT IN ('queued', 'running')",
                    (cutoff,),
                )
            return log_files
