"""SQLite persistence layer (WAL mode, zero external dependencies)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .schedule_math import compute_next_run, describe_schedule, normalize_schedule, weekly_slots

logger = logging.getLogger("scheduler.db")


def iso_now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _clean_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    return str(val).strip()


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA busy_timeout=15000;")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
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
                CREATE INDEX IF NOT EXISTS idx_runs_queued ON runs(queued_at DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_next_run ON jobs(enabled, next_run_at);
                """
            )
            try:
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active_per_job
                    ON runs(job_id)
                    WHERE status IN ('queued', 'running')
                    """
                )
            except sqlite3.IntegrityError:
                # Existing legacy database has duplicate orphaned active rows; mark them stopped and retry.
                logger.warning("Duplicate active runs detected during schema init; recovering before creating unique index.")
                now = iso_now()
                conn.execute(
                    """
                    UPDATE runs
                    SET status = 'stopped',
                        finished_at = ?,
                        error_summary = 'Recovered duplicate active run during startup.'
                    WHERE status IN ('running', 'queued')
                    """,
                    (now,),
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active_per_job
                    ON runs(job_id)
                    WHERE status IN ('queued', 'running')
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

    def _fetch_recent_runs_for_job(self, conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
        run_rows = conn.execute(
            """
            SELECT id, job_id, status, trigger, triggered_by,
                   queued_at, started_at, finished_at, duration_sec,
                   exit_code, error_summary
            FROM runs
            WHERE job_id = ?
            ORDER BY queued_at DESC, rowid DESC
            LIMIT 20
            """,
            (job_id,),
        ).fetchall()
        return [dict(r) for r in run_rows]

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            job_rows = conn.execute("SELECT * FROM jobs ORDER BY LOWER(name) ASC").fetchall()
            runs_by_job: dict[str, list[dict[str, Any]]] = {r["id"]: [] for r in job_rows}

            if job_rows:
                # Fetch up to 20 most recent runs per job using window function (excluding rn/_rid from output)
                run_rows = conn.execute(
                    """
                    SELECT id, job_id, status, trigger, triggered_by,
                           queued_at, started_at, finished_at, duration_sec,
                           exit_code, error_summary
                    FROM (
                        SELECT id, job_id, status, trigger, triggered_by,
                               queued_at, started_at, finished_at, duration_sec,
                               exit_code, error_summary, rowid AS _rid,
                               ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY queued_at DESC, rowid DESC) AS rn
                        FROM runs
                    )
                    WHERE rn <= 20
                    ORDER BY queued_at DESC, _rid DESC
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
            return self._serialize_job(row, self._fetch_recent_runs_for_job(conn, job_id))

    def create_job(self, payload: dict[str, Any], user: str, default_timeout: int = 45) -> dict[str, Any]:
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        now = iso_now()

        name = _clean_str(payload.get("name"))
        if not name:
            raise ValueError("Schedule name cannot be empty.")
        script_ref = _clean_str(payload.get("script_ref"))
        if not script_ref:
            raise ValueError("Script reference cannot be empty.")

        stype, scfg = normalize_schedule(
            payload.get("schedule_type") or "manual",
            payload.get("schedule_config"),
        )
        enabled = True if payload.get("enabled") is None else bool(payload.get("enabled"))
        nxt = compute_next_run(stype, scfg, enabled=enabled)

        raw_timeout = payload.get("timeout_minutes")
        timeout_val = default_timeout if raw_timeout is None or raw_timeout == "" else int(raw_timeout)
        timeout = max(1, min(480, timeout_val))
        owner = _clean_str(payload.get("owner")) or user

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
                    name,
                    _clean_str(payload.get("description")),
                    script_ref,
                    _clean_str(payload.get("args")),
                    "",  # Never persist untrusted per-job python_exe overrides
                    _clean_str(payload.get("dashboard_url")),
                    _clean_str(payload.get("freshness_path")),
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
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert row is not None
            return self._serialize_job(row, [])

    def update_job(self, job_id: str, payload: dict[str, Any], user: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return None

            if "name" in payload and payload["name"] is not None:
                name = _clean_str(payload["name"])
                if not name:
                    raise ValueError("Schedule name cannot be empty.")
            else:
                name = row["name"]

            if "script_ref" in payload and payload["script_ref"] is not None:
                script_ref = _clean_str(payload["script_ref"])
                if not script_ref:
                    raise ValueError("Script reference cannot be empty.")
            else:
                script_ref = row["script_ref"]

            description = (
                row["description"]
                if "description" not in payload
                else _clean_str(payload["description"])
            )
            args = (
                row["args"]
                if "args" not in payload
                else _clean_str(payload["args"])
            )
            dashboard_url = (
                row["dashboard_url"]
                if "dashboard_url" not in payload
                else _clean_str(payload["dashboard_url"])
            )
            freshness_path = (
                row["freshness_path"]
                if "freshness_path" not in payload
                else _clean_str(payload["freshness_path"])
            )
            if "owner" not in payload or payload["owner"] is None:
                owner = row["owner"]
            else:
                owner = _clean_str(payload["owner"]) or row["owner"]

            # Ignore python_exe in update payload and preserve existing column value
            python_exe = row["python_exe"]

            existing_stype = row["schedule_type"]
            existing_scfg = json.loads(row["schedule_config"] or "{}")
            existing_enabled = bool(row["enabled"])

            raw_stype = (
                existing_stype
                if ("schedule_type" not in payload or payload["schedule_type"] is None)
                else payload["schedule_type"]
            )
            raw_scfg = (
                existing_scfg
                if ("schedule_config" not in payload or payload["schedule_config"] is None)
                else payload["schedule_config"]
            )
            stype, scfg = normalize_schedule(raw_stype, raw_scfg)

            enabled = (
                existing_enabled
                if ("enabled" not in payload or payload["enabled"] is None)
                else bool(payload["enabled"])
            )

            schedule_changed = (
                stype != existing_stype
                or scfg != existing_scfg
                or enabled != existing_enabled
            )
            if schedule_changed:
                nxt = compute_next_run(stype, scfg, enabled=enabled)
                next_run_at = nxt.isoformat() if nxt else None
            else:
                next_run_at = row["next_run_at"]

            raw_timeout = payload.get("timeout_minutes")
            if "timeout_minutes" not in payload or raw_timeout is None or raw_timeout == "":
                timeout = row["timeout_minutes"]
            else:
                timeout = max(1, min(480, int(raw_timeout)))

            now = iso_now()

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
                    description,
                    script_ref,
                    args,
                    python_exe,
                    dashboard_url,
                    freshness_path,
                    stype,
                    json.dumps(scfg),
                    timeout,
                    1 if enabled else 0,
                    owner,
                    user,
                    now,
                    next_run_at,
                    job_id,
                ),
            )
            updated_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert updated_row is not None
            return self._serialize_job(updated_row, self._fetch_recent_runs_for_job(conn, job_id))

    def delete_job_with_logs(self, job_id: str) -> tuple[bool, list[str]]:
        """Atomically fetch associated run log paths and delete the job."""
        with self._lock, self._connect() as conn:
            log_rows = conn.execute(
                "SELECT log_file FROM runs WHERE job_id = ? AND log_file != ''",
                (job_id,),
            ).fetchall()
            log_files = [r["log_file"] for r in log_rows if r["log_file"]]
            cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            if cur.rowcount > 0:
                return True, log_files
            return False, []

    def delete_job(self, job_id: str) -> bool:
        deleted, _ = self.delete_job_with_logs(job_id)
        return deleted

    def claim_due_jobs(
        self,
        now: datetime | None = None,
        misfire_grace_minutes: int | None = None,
    ) -> list[dict[str, Any]]:
        """Find enabled jobs due at or before `now` and advance their next_run_at atomically.

        When `misfire_grace_minutes` is provided, jobs whose scheduled `next_run_at` is older
        than the grace window have `next_run_at` advanced to the next future slot without
        being returned for execution (preventing post-outage catch-up stampedes).
        """
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

                if misfire_grace_minutes is not None and misfire_grace_minutes >= 0:
                    try:
                        scheduled_dt = datetime.fromisoformat(row["next_run_at"])
                        age_seconds = (ref - scheduled_dt).total_seconds()
                        if age_seconds > misfire_grace_minutes * 60:
                            logger.warning(
                                "Skipping overdue scheduled run for job %s (%s): scheduled for %s, "
                                "overdue by %.1f min (grace window: %d min). Next run advanced to %s.",
                                row["id"],
                                row["name"],
                                row["next_run_at"],
                                age_seconds / 60.0,
                                misfire_grace_minutes,
                                nxt.isoformat() if nxt else "None",
                            )
                            continue
                    except (TypeError, ValueError):
                        pass

                due.append(dict(row))
        return due

    def has_active_run(self, job_id: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE job_id = ? AND status IN ('queued', 'running') LIMIT 1",
                (job_id,),
            ).fetchone()
            return row is not None

    def create_run(
        self,
        job_id: str,
        trigger: str,
        triggered_by: str,
        log_file: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        actual_run_id = run_id or f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = iso_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    id, job_id, status, trigger, triggered_by, queued_at, log_file
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?)
                """,
                (actual_run_id, job_id, trigger, triggered_by, now, log_file),
            )
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (actual_run_id,)).fetchone()
            assert row is not None
            return dict(row)

    def mark_run_started(self, run_id: str) -> bool:
        """Atomically transition a queued run to running. Returns True only if transitioned."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE runs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                (iso_now(), run_id),
            )
            return cur.rowcount == 1

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
                "SELECT * FROM runs WHERE job_id = ? ORDER BY queued_at DESC, rowid DESC LIMIT ?",
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
                ORDER BY r.queued_at DESC, r.rowid DESC
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
