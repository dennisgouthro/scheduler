"""Background scheduler loop, concurrency worker pool, live log streaming, and process runner."""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig
from .db import Database, iso_now
from .scrubber import scrub_text


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Terminate a process and all its children safely on Windows or POSIX."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _extract_error_summary(log_lines: list[str], exit_code: int) -> str:
    """Pull a concise, human-readable error line from a Python traceback or stderr."""
    cleaned = [line.strip() for line in log_lines if line.strip()]
    if not cleaned:
        return f"Exited with code {exit_code}."

    # Walk backward to find the primary Python exception or explicit error line
    for line in reversed(cleaned[-40:]):
        if any(
            token in line
            for token in (
                "Error:",
                "Exception:",
                "OperationalError:",
                "ProgrammingError:",
                "DatabaseError:",
                "FAILED:",
                "CRITICAL:",
            )
        ):
            return scrub_text(line)[:300]

    return scrub_text(cleaned[-1])[:300]


class PipelineRunner:
    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db

        self._stop_event = threading.Event()
        self._work_queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.RLock()
        self._active_procs: dict[str, subprocess.Popen[str]] = {}
        self._stop_reasons: dict[str, str] = {}
        self._subscribers: dict[str, list[queue.Queue[dict[str, Any]]]] = {}
        self._threads: list[threading.Thread] = []
        self._last_prune_day: str = ""

    def start(self) -> None:
        self.db.recover_orphaned_runs()

        for idx in range(self.config.max_concurrent_runs):
            t = threading.Thread(target=self._worker_loop, name=f"worker-{idx+1}", daemon=True)
            t.start()
            self._threads.append(t)

        tick_thread = threading.Thread(target=self._scheduler_loop, name="scheduler-tick", daemon=True)
        tick_thread.start()
        self._threads.append(tick_thread)

    def shutdown(self) -> None:
        self._stop_event.set()
        with self._lock:
            for run_id, proc in list(self._active_procs.items()):
                self._stop_reasons[run_id] = "stopped"
                _kill_process_tree(proc)
        for _ in range(self.config.max_concurrent_runs):
            self._work_queue.put(None)
        current = threading.current_thread()
        for t in list(self._threads):
            if t is not current and t.is_alive():
                t.join(timeout=5.0)
        self._threads.clear()

    def active_counts(self) -> dict[str, int]:
        with self._lock:
            running = len(self._active_procs)
        return {
            "running": running,
            "queued": self._work_queue.qsize(),
            "max_concurrent": self.config.max_concurrent_runs,
        }

    def enqueue_job(self, job_id: str, trigger: str, triggered_by: str) -> dict[str, Any]:
        """Queue a job run unless an active run already exists."""
        job = self.db.get_job(job_id)
        if not job:
            raise KeyError("Schedule not found.")

        with self._lock:
            if self.db.has_active_run(job_id):
                raise RuntimeError("This script is already running or queued.")

            # Validate script path before queueing
            self.config.resolve_script_reference(job["script_ref"])

            log_name = f"{job_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            log_path = self.config.logs_dir / log_name
            run = self.db.create_run(job_id, trigger, triggered_by, str(log_path))
            self._work_queue.put(run["id"])
            return run

    def stop_run(self, run_id: str, requested_by: str) -> bool:
        run = self.db.get_run(run_id)
        if not run:
            raise KeyError("Run not found.")

        with self._lock:
            proc = self._active_procs.get(run_id)
            if proc is not None:
                self._stop_reasons[run_id] = f"stopped by {requested_by}"
                _kill_process_tree(proc)
                return True

        # If still queued (not yet started), mark stopped immediately
        if run["status"] == "queued":
            self.db.mark_run_finished(run_id, "stopped", None, 0.0, f"Cancelled by {requested_by} before start.")
            self._publish(run_id, {"type": "done", "status": "stopped"})
            return True
        return False

    def subscribe(self, run_id: str) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: str, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            subs = self._subscribers.get(run_id)
            if subs and q in subs:
                subs.remove(q)
                if not subs:
                    self._subscribers.pop(run_id, None)

    def _publish(self, run_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subscribers.get(run_id, []))
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def read_run_log(self, run: dict[str, Any], max_bytes: int = 512_000) -> str:
        log_file = run.get("log_file")
        if not log_file:
            return ""
        path = Path(log_file)
        if not path.is_file():
            return ""
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                if size > max_bytes:
                    fh.seek(size - max_bytes)
                    raw = fh.read()
                    prefix = f"... [showing last {max_bytes // 1024} KB of {size // 1024} KB log] ...\n"
                    text = prefix + raw.decode("utf-8", errors="replace")
                else:
                    text = fh.read().decode("utf-8", errors="replace")
            return scrub_text(text)
        except OSError:
            return ""

    def build_ai_diagnostic_prompt(self, job: dict[str, Any], run: dict[str, Any], log_text: str) -> str:
        """Build a clean, scrubbed context snippet ready to paste into an AI assistant."""
        tail_lines = log_text.splitlines()[-80:]
        tail = "\n".join(tail_lines).strip() or "(No console output captured)"
        return (
            f"A scheduled analytics Python script failed on the server. Please help diagnose and fix it.\n\n"
            f"- **Pipeline:** {job['name']}\n"
            f"- **Script:** `{job['script_ref']}`\n"
            f"- **Status:** {run['status']} (exit code: {run.get('exit_code')})\n"
            f"- **Error summary:** {run.get('error_summary') or 'See log tail below'}\n"
            f"- **Started:** {run.get('started_at') or run.get('queued_at')}\n"
            f"- **Duration:** {run.get('duration_sec') or 0}s\n\n"
            f"### Recent Log Output (secrets scrubbed)\n"
            f"```text\n{tail}\n```\n"
        )

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                due_jobs = self.db.claim_due_jobs()
                for job in due_jobs:
                    try:
                        self.enqueue_job(job["id"], trigger="scheduled", triggered_by="Schedule")
                    except Exception:
                        # Skip quietly if already running; next tick/schedule stays clean
                        pass

                today = datetime.now().strftime("%Y-%m-%d")
                if today != self._last_prune_day:
                    self._last_prune_day = today
                    self._prune_logs()
            except Exception:
                pass
            self._stop_event.wait(5.0)

    def _prune_logs(self) -> None:
        old_files = self.db.prune_old_runs(self.config.log_retention_days)
        for path_str in old_files:
            try:
                Path(path_str).unlink(missing_ok=True)
            except OSError:
                pass

    def _write_freshness_badge(self, job: dict[str, Any], duration_sec: float) -> None:
        freshness_ref = (job.get("freshness_path") or "").strip()
        if not freshness_ref:
            return
        try:
            _, target_path, _ = self.config.resolve_script_reference(freshness_ref)
            if target_path.suffix.lower() != ".json":
                return
            target_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "pipeline": job["name"],
                "owner": job["owner"],
                "status": "fresh",
                "updated_at": iso_now(),
                "duration_sec": round(duration_sec, 1),
                "schedule": job["schedule_label"],
            }
            tmp = target_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, target_path)
        except Exception:
            # Never fail a successful pipeline solely because an optional badge path wasn't writable
            pass

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            run_id = self._work_queue.get()
            if run_id is None:
                break
            try:
                self._execute_run(run_id)
            except Exception:
                pass
            finally:
                self._work_queue.task_done()

    def _execute_run(self, run_id: str) -> None:
        run = self.db.get_run(run_id)
        if not run or run["status"] != "queued":
            return

        job = self.db.get_job(run["job_id"])
        if not job:
            self.db.mark_run_finished(run_id, "stopped", None, 0.0, "Schedule was deleted before run started.")
            return

        start_monotonic = time.monotonic()
        self.db.mark_run_started(run_id)
        self._publish(run_id, {"type": "status", "status": "running"})

        log_path = Path(run["log_file"])
        log_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            _, script_path, _ = self.config.resolve_script_reference(job["script_ref"])
            if not script_path.is_file():
                raise FileNotFoundError(f"Script file not found: {job['script_ref']}")
        except Exception as exc:
            err = scrub_text(str(exc))
            log_path.write_text(f"[Scheduler Error] {err}\n", encoding="utf-8")
            self.db.mark_run_finished(run_id, "failed", -1, 0.0, err)
            self._publish(run_id, {"type": "line", "text": f"[Scheduler Error] {err}\n"})
            self._publish(run_id, {"type": "done", "status": "failed"})
            return

        python_exe = (job.get("python_exe") or self.config.python_exe or "python").strip()
        raw_args = (job.get("args") or "").strip()
        try:
            extra_args = shlex.split(raw_args, posix=True) if raw_args else []
        except ValueError:
            extra_args = raw_args.split()
        cmd = [python_exe, "-u", str(script_path), *extra_args]

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["ANALYTICS_SCHEDULER_RUN_ID"] = run_id
        env["ANALYTICS_SCHEDULER_JOB_NAME"] = job["name"]

        timeout_sec = max(60, int(job.get("timeout_minutes") or self.config.default_timeout_minutes) * 60)
        tail_buffer: list[str] = []
        timed_out = False

        header = (
            f"=== {job['name']} ===\n"
            f"Started : {iso_now()}\n"
            f"Trigger : {run['trigger']} ({run['triggered_by']})\n"
            f"Script  : {job['script_ref']}\n"
            f"{'=' * 48}\n"
        )

        try:
            with log_path.open("w", encoding="utf-8", errors="replace") as log_fh:
                log_fh.write(header)
                log_fh.flush()
                self._publish(run_id, {"type": "line", "text": header})

                proc = subprocess.Popen(
                    cmd,
                    cwd=str(script_path.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                )

                with self._lock:
                    self._active_procs[run_id] = proc

                def on_timeout() -> None:
                    nonlocal timed_out
                    timed_out = True
                    with self._lock:
                        self._stop_reasons[run_id] = f"timed out after {job['timeout_minutes']} min"
                    _kill_process_tree(proc)

                timer = threading.Timer(timeout_sec, on_timeout)
                timer.daemon = True
                timer.start()

                try:
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        safe_line = scrub_text(line)
                        log_fh.write(safe_line)
                        log_fh.flush()
                        tail_buffer.append(safe_line)
                        if len(tail_buffer) > 120:
                            tail_buffer.pop(0)
                        self._publish(run_id, {"type": "line", "text": safe_line})

                    exit_code = proc.wait()
                finally:
                    timer.cancel()
                    with self._lock:
                        self._active_procs.pop(run_id, None)
                        stop_reason = self._stop_reasons.pop(run_id, None)

                duration = time.monotonic() - start_monotonic
                if timed_out:
                    status = "timed_out"
                    summary = f"Timed out after {job['timeout_minutes']} minutes."
                elif stop_reason:
                    status = "stopped"
                    summary = f"Run {stop_reason}."
                elif exit_code == 0:
                    status = "succeeded"
                    summary = ""
                else:
                    status = "failed"
                    summary = _extract_error_summary(tail_buffer, exit_code)

                footer = f"\n=== Finished: {status.upper()} in {duration:.1f}s (exit code {exit_code}) ===\n"
                log_fh.write(footer)
                log_fh.flush()
                self._publish(run_id, {"type": "line", "text": footer})

                if status == "succeeded":
                    self._write_freshness_badge(job, duration)
                self.db.mark_run_finished(run_id, status, exit_code, duration, summary)
                self._publish(run_id, {"type": "done", "status": status, "duration_sec": round(duration, 2)})

        except Exception as exc:
            duration = time.monotonic() - start_monotonic
            err = scrub_text(f"Failed to start Python ({python_exe}): {exc}")
            with log_path.open("a", encoding="utf-8", errors="replace") as log_fh:
                log_fh.write(f"\n[Scheduler Error] {err}\n")
            self.db.mark_run_finished(run_id, "failed", -1, duration, err)
            self._publish(run_id, {"type": "line", "text": f"\n[Scheduler Error] {err}\n"})
            self._publish(run_id, {"type": "done", "status": "failed"})
