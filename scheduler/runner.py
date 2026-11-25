"""Background scheduler loop, concurrency worker pool, live log streaming, and process runner."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig
from .db import Database, iso_now
from .scrubber import LogScrubber, scrub_text

logger = logging.getLogger("scheduler.runner")

MAX_TOTAL_SUBSCRIBERS = 50
MAX_PER_RUN_SUBSCRIBERS = 10

_CMD_METACHARS_RE = re.compile(r'[&|<>^%!\r\n"\x00]')

_ERROR_TOKENS = (
    "error:",
    "error in ",
    "exception:",
    "operationalerror:",
    "programmingerror:",
    "databaseerror:",
    "fullyqualifiederrorid",
    "failed:",
    "critical:",
    "fatal:",
    "runtime error:",
    "syntaxerror:",
    "traceback (most recent call last):",
)


def _tokenize_args(raw_args: str) -> list[str]:
    """Quote-aware argument tokenizer that preserves Windows backslashes.

    Handles embedded quoted values such as ``--out="C:\\My Dir\\a.csv"``
    and raises ``ValueError`` on unbalanced quotes or null/newline characters.
    """
    if not raw_args or not raw_args.strip():
        return []
    if "\x00" in raw_args or "\r" in raw_args or "\n" in raw_args:
        raise ValueError("Script arguments cannot contain newlines or null characters.")

    tokens: list[str] = []
    buf: list[str] = []
    in_quote: str | None = None
    has_token = False
    i = 0
    n = len(raw_args)

    while i < n:
        ch = raw_args[i]
        if in_quote is not None:
            if ch == "\\" and in_quote == '"' and i + 1 < n and raw_args[i + 1] == '"':
                buf.append('"')
                i += 2
                continue
            if ch == in_quote:
                in_quote = None
            else:
                buf.append(ch)
        else:
            if ch.isspace():
                if has_token:
                    tokens.append("".join(buf))
                    buf.clear()
                    has_token = False
            elif ch in ('"', "'"):
                in_quote = ch
                has_token = True
            else:
                buf.append(ch)
                has_token = True
        i += 1

    if in_quote is not None:
        raise ValueError("Unbalanced quotes in script arguments.")
    if has_token:
        tokens.append("".join(buf))
    return tokens


def validate_script_args(script_path: Path, raw_args: str) -> list[str]:
    """Parse and validate CLI arguments for the target script type.

    Shared by both ``scheduler.api`` (request validation) and ``PipelineRunner``
    (execution-time validation).
    """
    ext = script_path.suffix.lower()
    if ext in (".bat", ".cmd"):
        if _CMD_METACHARS_RE.search(str(script_path)):
            raise ValueError("Batch script path contains disallowed command-shell characters.")

    tokens = _tokenize_args(raw_args or "")

    if ext in (".bat", ".cmd"):
        for tok in tokens:
            if _CMD_METACHARS_RE.search(tok):
                raise ValueError(
                    "Batch script arguments cannot contain shell metacharacters (& | < > ^ % ! or quotes)."
                )
    elif ext == ".vbs":
        for tok in tokens:
            if tok.startswith("//"):
                raise ValueError("VBScript arguments cannot start with '//' (reserved for cscript host options).")

    return tokens


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Terminate a process and all its children safely on Windows or POSIX."""
    try:
        if os.name == "nt":
            if proc.poll() is None:
                sys_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
                taskkill = Path(sys_root) / "System32" / "taskkill.exe"
                taskkill_cmd = str(taskkill) if taskkill.is_file() else "taskkill"
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.run(
                    [taskkill_cmd, "/F", "/T", "/PID", str(proc.pid)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                    check=False,
                    timeout=10,
                )
            if proc.poll() is None:
                proc.kill()
        else:
            if proc.poll() is None:
                proc.kill()
    except Exception:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
    finally:
        # Always close stdout even if the parent already exited so a grandchild holding
        # the inherited pipe handle cannot wedge the worker thread indefinitely.
        stdout = getattr(proc, "stdout", None)
        if stdout is not None:
            try:
                stdout.close()
            except Exception:
                pass


def _extract_error_summary(log_lines: list[str], exit_code: int) -> str:
    """Pull a concise, human-readable error line from a traceback or stderr."""
    cleaned = [line.strip() for line in log_lines if line.strip()]
    if not cleaned:
        return f"Exited with code {exit_code}."

    # Walk backward to find the primary exception or explicit error line (case-insensitive)
    for line in reversed(cleaned[-40:]):
        lowered = line.lower()
        if any(token in lowered for token in _ERROR_TOKENS):
            return scrub_text(line)[:300]

    return scrub_text(cleaned[-1])[:300]


class PipelineRunner:
    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db

        self._stop_event = threading.Event()
        self._shutdown_started = False
        self._work_queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.RLock()
        self._active_procs: dict[str, subprocess.Popen[str]] = {}
        self._starting_runs: set[str] = set()
        self._stop_reasons: dict[str, str] = {}
        self._subscribers: dict[str, list[queue.Queue[dict[str, Any]]]] = {}
        self._threads: list[threading.Thread] = []
        self._last_prune_day: str = ""

    def start(self) -> None:
        with self._lock:
            self._stop_event.clear()
            self._shutdown_started = False

        recovered = self.db.recover_orphaned_runs()
        if recovered:
            logger.warning("Recovered %d orphaned active run(s) from previous session.", recovered)

        for idx in range(self.config.max_concurrent_runs):
            t = threading.Thread(target=self._worker_loop, name=f"worker-{idx+1}", daemon=True)
            t.start()
            self._threads.append(t)

        tick_thread = threading.Thread(target=self._scheduler_loop, name="scheduler-tick", daemon=True)
        tick_thread.start()
        self._threads.append(tick_thread)

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            self._stop_event.set()
            procs_to_kill: list[subprocess.Popen[str]] = []
            for run_id, proc in list(self._active_procs.items()):
                self._stop_reasons.setdefault(run_id, "stopped")
                procs_to_kill.append(proc)
            all_subs = [q for subs in self._subscribers.values() for q in subs]
            threads = list(self._threads)
            self._threads.clear()

        # Release self._lock BEFORE killing processes so API/SSE callers don't freeze
        for proc in procs_to_kill:
            _kill_process_tree(proc)

        # Wake all SSE subscribers so streaming HTTP threads exit cleanly
        done_event = {"type": "done", "status": "stopped"}
        for q in all_subs:
            self._enqueue_subscriber_event(q, done_event)

        for _ in range(self.config.max_concurrent_runs):
            self._work_queue.put(None)

        current = threading.current_thread()
        for t in threads:
            if t is not current and t.is_alive():
                t.join(timeout=5.0)

    def active_counts(self) -> dict[str, int]:
        with self._lock:
            running = len(self._active_procs) + len(self._starting_runs)
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

            # Validate script path and args before queueing
            _, script_path, _ = self.config.resolve_script_reference(job["script_ref"])
            validate_script_args(script_path, (job.get("args") or "").strip())

            run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
            log_name = f"{job_id}_{run_id}.log"
            log_path = self.config.logs_dir / log_name
            try:
                run = self.db.create_run(job_id, trigger, triggered_by, str(log_path), run_id=run_id)
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("This script is already running or queued.") from exc
            self._work_queue.put(run["id"])
            return run

    def stop_run(self, run_id: str, requested_by: str) -> bool:
        proc_to_kill: subprocess.Popen[str] | None = None
        cancelled_queued = False

        with self._lock:
            run = self.db.get_run(run_id)
            if not run:
                raise KeyError("Run not found.")

            proc = self._active_procs.get(run_id)
            if proc is not None:
                self._stop_reasons.setdefault(run_id, f"stopped by {requested_by}")
                proc_to_kill = proc
            elif run_id in self._starting_runs or run["status"] == "running":
                # Worker is between mark_run_started and Popen registration
                self._stop_reasons.setdefault(run_id, f"stopped by {requested_by}")
                return True
            elif run["status"] == "queued":
                self._stop_reasons[run_id] = f"stopped by {requested_by}"
                self.db.mark_run_finished(
                    run_id,
                    "stopped",
                    None,
                    0.0,
                    f"Cancelled by {requested_by} before start.",
                )
                cancelled_queued = True
            else:
                return False

        if proc_to_kill is not None:
            _kill_process_tree(proc_to_kill)
            return True

        if cancelled_queued:
            self._publish(run_id, {"type": "done", "status": "stopped"})
            return True
        return False

    def subscribe(self, run_id: str) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)
        with self._lock:
            total = sum(len(subs) for subs in self._subscribers.values())
            if total >= MAX_TOTAL_SUBSCRIBERS:
                raise RuntimeError("Too many active live log streams.")
            run_subs = self._subscribers.get(run_id, [])
            if len(run_subs) >= MAX_PER_RUN_SUBSCRIBERS:
                raise RuntimeError("Too many viewers connected to this run stream.")
            self._subscribers.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: str, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            subs = self._subscribers.get(run_id)
            if subs and q in subs:
                subs.remove(q)
                if not subs:
                    self._subscribers.pop(run_id, None)

    def _enqueue_subscriber_event(self, q: queue.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        is_control = event.get("type") in ("done", "status")
        for _ in range(3):
            try:
                q.put_nowait(event)
                return
            except queue.Full:
                if not is_control:
                    return
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass

    def _publish(self, run_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subscribers.get(run_id, []))
        for q in subs:
            self._enqueue_subscriber_event(q, event)

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
            f"A scheduled analytics script failed on the server. Please help diagnose and fix it.\n\n"
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
                grace = getattr(self.config, "misfire_grace_minutes", 60)
                due_jobs = self.db.claim_due_jobs(misfire_grace_minutes=grace)
                for job in due_jobs:
                    try:
                        self.enqueue_job(job["id"], trigger="scheduled", triggered_by="Schedule")
                    except RuntimeError as exc:
                        logger.info(
                            "Skipped scheduled run for job %s (%s): %s",
                            job["id"],
                            job["name"],
                            exc,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to enqueue scheduled run for job %s (%s)",
                            job["id"],
                            job["name"],
                        )

                today = datetime.now().strftime("%Y-%m-%d")
                if today != self._last_prune_day:
                    self._prune_logs()
                    self._last_prune_day = today
            except Exception:
                logger.exception("Unhandled error in scheduler tick loop")
            self._stop_event.wait(5.0)

    def _prune_logs(self) -> None:
        retention_days = self.config.log_retention_days
        old_files = self.db.prune_old_runs(retention_days)
        for path_str in old_files:
            try:
                Path(path_str).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Failed to delete pruned run log %s: %s", path_str, exc)

        # Sweep orphaned .log files older than retention_days (preserving service.log)
        cutoff_ts = time.time() - (retention_days * 86400)
        if self.config.logs_dir.is_dir():
            for entry in self.config.logs_dir.glob("*.log"):
                if entry.name == "service.log":
                    continue
                try:
                    if entry.is_file() and entry.stat().st_mtime < cutoff_ts:
                        entry.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Failed to sweep orphaned log %s: %s", entry, exc)

    def _write_freshness_badge(self, job: dict[str, Any], duration_sec: float) -> str | None:
        """Write optional freshness.json badge. Returns warning message on failure, else None."""
        freshness_ref = (job.get("freshness_path") or "").strip()
        if not freshness_ref:
            return None
        tmp: Path | None = None
        try:
            _, target_path, _ = self.config.resolve_script_reference(freshness_ref)
            if target_path.suffix.lower() != ".json":
                return f"Skipped freshness badge ({freshness_ref}): path must end in .json."
            if target_path.is_symlink():
                return f"Skipped freshness badge ({freshness_ref}): symlink target is not allowed."
            target_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "pipeline": job["name"],
                "owner": job["owner"],
                "status": "fresh",
                "updated_at": iso_now(),
                "duration_sec": round(duration_sec, 1),
                "schedule": job["schedule_label"],
            }
            tmp = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex[:8]}.tmp")
            if tmp.is_symlink():
                return f"Skipped freshness badge ({freshness_ref}): temp path is a symlink."
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, target_path)
            return None
        except Exception as exc:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            msg = scrub_text(f"Could not write freshness badge ({freshness_ref}): {exc}")
            logger.warning("%s", msg)
            return msg

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            run_id = self._work_queue.get()
            if run_id is None:
                self._work_queue.task_done()
                break
            try:
                self._execute_run(run_id)
            except Exception:
                logger.exception("Unhandled error executing run %s", run_id)
            finally:
                self._work_queue.task_done()

    def _split_args(self, raw_args: str) -> list[str]:
        return _tokenize_args(raw_args)

    def _lookup_custom_runner(self, ext: str) -> list[str] | None:
        template = self.config.script_runners.get(ext)
        if template is not None:
            return template
        for raw_key, val in self.config.script_runners.items():
            norm = raw_key.strip().lower()
            if norm and not norm.startswith("."):
                norm = "." + norm
            if norm == ext:
                return val
        return None

    def _build_script_command(
        self,
        script_path: Path,
        job: dict[str, Any],
        extra_args: list[str],
    ) -> tuple[list[str] | str, str]:
        """Build the subprocess command and runner label for any supported script type.

        Security: per-job ``python_exe`` overrides are intentionally ignored; all runner
        executables come exclusively from trusted server ``config.json`` settings.
        """
        _ = job  # Per-job executable overrides are disallowed
        ext = script_path.suffix.lower()

        # 1. Custom extension mapping in config.json ("script_runners")
        custom_template = self._lookup_custom_runner(ext)
        if custom_template:
            cmd: list[str] = []
            replaced = False
            for token in custom_template:
                if "{script}" in token:
                    cmd.append(token.replace("{script}", str(script_path)))
                    replaced = True
                else:
                    cmd.append(token)
            if not replaced:
                cmd.append(str(script_path))
            cmd.extend(extra_args)
            return cmd, cmd[0]

        # 2. Built-in script runners
        if ext == ".py":
            exe = self.config.python_exe or sys.executable or "python"
            return [exe, "-u", str(script_path), *extra_args], exe

        if ext == ".ps1":
            exe = self.config.powershell_exe or ("powershell.exe" if os.name == "nt" else "pwsh")
            return [
                exe,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                *extra_args,
            ], exe

        if ext in (".bat", ".cmd"):
            if _CMD_METACHARS_RE.search(str(script_path)):
                raise ValueError("Batch script path contains disallowed command-shell characters.")
            for arg in extra_args:
                if _CMD_METACHARS_RE.search(arg):
                    raise ValueError(
                        "Batch script arguments cannot contain shell metacharacters (& | < > ^ % ! or quotes)."
                    )
            if os.name == "nt":
                exe = os.environ.get("COMSPEC") or "cmd.exe"
                exe_part = f'"{exe}"' if any(ch.isspace() for ch in exe) else exe
                inner_parts = [f'"{script_path}"']
                for arg in extra_args:
                    if not arg or any(ch.isspace() for ch in arg):
                        inner_parts.append(f'"{arg}"')
                    else:
                        inner_parts.append(arg)
                inner_cmd = " ".join(inner_parts)
                # /d disables AutoRun; /s strips exactly the outer quote pair around inner_cmd
                return f'{exe_part} /d /s /c "{inner_cmd}"', exe
            exe = "sh"
            return [exe, str(script_path), *extra_args], exe

        if ext in (".js", ".mjs"):
            exe = "node"
            return [exe, str(script_path), *extra_args], exe

        if ext == ".r":
            exe = "Rscript"
            return [exe, str(script_path), *extra_args], exe

        if ext == ".vbs":
            for arg in extra_args:
                if arg.startswith("//"):
                    raise ValueError(
                        "VBScript arguments cannot start with '//' (reserved for cscript host options)."
                    )
            exe = "cscript.exe"
            return [exe, "//NoLogo", str(script_path), *extra_args], exe

        raise ValueError(f"Unsupported script type: {ext}")

    def _execute_run(self, run_id: str) -> None:
        with self._lock:
            run = self.db.get_run(run_id)
            if not run or run["status"] != "queued":
                self._stop_reasons.pop(run_id, None)
                return

            job = self.db.get_job(run["job_id"])
            if not job:
                self._stop_reasons.pop(run_id, None)
                self.db.mark_run_finished(run_id, "stopped", None, 0.0, "Schedule was deleted before run started.")
                return

            pre_stop = self._stop_reasons.pop(run_id, None)
            if pre_stop or self._stop_event.is_set():
                reason = pre_stop or "stopped"
                self.db.mark_run_finished(run_id, "stopped", None, 0.0, f"Run {reason}.")
                pre_stopped = True
            else:
                if not self.db.mark_run_started(run_id):
                    return
                self._starting_runs.add(run_id)
                pre_stopped = False

        if pre_stopped:
            self._publish(run_id, {"type": "done", "status": "stopped"})
            return

        start_monotonic = time.monotonic()
        self._publish(run_id, {"type": "status", "status": "running"})

        log_path = Path(run["log_file"])
        runner_exe = ""

        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                _, script_path, _ = self.config.resolve_script_reference(job["script_ref"])
                if not script_path.is_file():
                    raise FileNotFoundError(f"Script file not found: {job['script_ref']}")
                if not self.config.is_supported_script(script_path):
                    raise ValueError(f"Unsupported script type: {script_path.suffix or '(none)'}")
                extra_args = validate_script_args(script_path, (job.get("args") or "").strip())
                cmd, runner_exe = self._build_script_command(script_path, job, extra_args)
            except Exception as exc:
                err = scrub_text(str(exc))
                try:
                    log_path.write_text(f"[Scheduler Error] {err}\n", encoding="utf-8")
                except OSError:
                    logger.warning("Failed to write error log %s", log_path, exc_info=True)
                self.db.mark_run_finished(run_id, "failed", -1, 0.0, err)
                self._publish(run_id, {"type": "line", "text": f"[Scheduler Error] {err}\n"})
                self._publish(run_id, {"type": "done", "status": "failed"})
                return

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            env["ANALYTICS_SCHEDULER_RUN_ID"] = run_id
            env["ANALYTICS_SCHEDULER_JOB_NAME"] = job["name"]

            timeout_sec = max(60, int(job.get("timeout_minutes") or self.config.default_timeout_minutes) * 60)
            max_log_mb = max(1, int(getattr(self.config, "max_log_mb", 25)))
            max_log_bytes = max_log_mb * 1024 * 1024
            tail_buffer: list[str] = []
            timed_out = False
            scrubber = LogScrubber()

            header = (
                f"=== {job['name']} ===\n"
                f"Started : {iso_now()}\n"
                f"Trigger : {run['trigger']} ({run['triggered_by']})\n"
                f"Script  : {job['script_ref']}\n"
                f"Runner  : {runner_exe}\n"
                f"{'=' * 48}\n"
            )

            with log_path.open("w", encoding="utf-8", errors="replace") as log_fh:
                log_fh.write(header)
                log_fh.flush()
                bytes_written = len(header.encode("utf-8", errors="replace"))
                log_truncated = False
                self._publish(run_id, {"type": "line", "text": header})

                # Re-check for stop/shutdown immediately before spawning subprocess
                early_stop_reason: str | None = None
                with self._lock:
                    if run_id in self._stop_reasons or self._stop_event.is_set():
                        early_stop_reason = self._stop_reasons.pop(run_id, None) or "stopped"
                        self._starting_runs.discard(run_id)

                if early_stop_reason is not None:
                    duration = time.monotonic() - start_monotonic
                    footer = f"\n=== Finished: STOPPED in {duration:.1f}s ===\n"
                    log_fh.write(footer)
                    log_fh.flush()
                    self.db.mark_run_finished(run_id, "stopped", None, duration, f"Run {early_stop_reason}.")
                    self._publish(run_id, {"type": "line", "text": footer})
                    self._publish(run_id, {"type": "done", "status": "stopped", "duration_sec": round(duration, 2)})
                    return

                popen_kwargs: dict[str, Any] = {
                    "cwd": str(script_path.parent),
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                    "bufsize": 1,
                    "env": env,
                }
                if os.name == "nt":
                    popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

                proc = subprocess.Popen(cmd, **popen_kwargs)

                stop_after_launch = False
                with self._lock:
                    self._starting_runs.discard(run_id)
                    self._active_procs[run_id] = proc
                    if run_id in self._stop_reasons or self._stop_event.is_set():
                        self._stop_reasons.setdefault(run_id, "stopped")
                        stop_after_launch = True

                def on_timeout() -> None:
                    nonlocal timed_out
                    proc_to_kill: subprocess.Popen[str] | None = None
                    with self._lock:
                        if run_id not in self._active_procs:
                            return
                        timed_out = True
                        self._stop_reasons.setdefault(run_id, f"timed out after {job['timeout_minutes']} min")
                        proc_to_kill = self._active_procs.get(run_id)
                    if proc_to_kill is not None:
                        _kill_process_tree(proc_to_kill)

                timer = threading.Timer(timeout_sec, on_timeout)
                timer.daemon = True
                timer.start()

                if stop_after_launch:
                    _kill_process_tree(proc)

                exit_code: int | None = None
                try:
                    assert proc.stdout is not None
                    try:
                        for line in proc.stdout:
                            safe_line = scrubber.scrub_line(line)
                            if not safe_line:
                                continue
                            tail_buffer.append(safe_line)
                            if len(tail_buffer) > 120:
                                tail_buffer.pop(0)
                            if not log_truncated:
                                line_bytes = len(safe_line.encode("utf-8", errors="replace"))
                                if bytes_written + line_bytes <= max_log_bytes:
                                    log_fh.write(safe_line)
                                    log_fh.flush()
                                    bytes_written += line_bytes
                                    self._publish(run_id, {"type": "line", "text": safe_line})
                                else:
                                    log_truncated = True
                                    notice = (
                                        f"\n[Scheduler Warning] Log size limit ({max_log_mb} MB) reached; "
                                        f"further output truncated.\n"
                                    )
                                    log_fh.write(notice)
                                    log_fh.flush()
                                    self._publish(run_id, {"type": "line", "text": notice})
                    except (ValueError, OSError):
                        # stdout may have been closed externally by _kill_process_tree on stop/timeout
                        with self._lock:
                            expected_close = timed_out or (run_id in self._stop_reasons) or self._stop_event.is_set()
                        if not expected_close:
                            raise

                    with self._lock:
                        stopping = timed_out or (run_id in self._stop_reasons) or self._stop_event.is_set()

                    if stopping:
                        try:
                            exit_code = proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            _kill_process_tree(proc)
                            try:
                                exit_code = proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                exit_code = -1
                    else:
                        exit_code = proc.wait()
                finally:
                    timer.cancel()
                    with self._lock:
                        self._active_procs.pop(run_id, None)
                        stop_reason = self._stop_reasons.pop(run_id, None)
                        was_timed_out = timed_out
                    if proc.poll() is None:
                        _kill_process_tree(proc)
                    if proc.stdout is not None:
                        try:
                            proc.stdout.close()
                        except Exception:
                            pass
                    if proc.poll() is None:
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            pass

                duration = time.monotonic() - start_monotonic
                if was_timed_out:
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
                    summary = _extract_error_summary(tail_buffer, exit_code if exit_code is not None else -1)

                if status == "succeeded":
                    badge_warn = self._write_freshness_badge(job, duration)
                    if badge_warn:
                        warn_line = f"\n[Scheduler Warning] {badge_warn}\n"
                        log_fh.write(warn_line)
                        log_fh.flush()
                        self._publish(run_id, {"type": "line", "text": warn_line})

                footer = f"\n=== Finished: {status.upper()} in {duration:.1f}s (exit code {exit_code}) ===\n"
                log_fh.write(footer)
                log_fh.flush()
                self._publish(run_id, {"type": "line", "text": footer})

                self.db.mark_run_finished(run_id, status, exit_code, duration, summary)
                self._publish(run_id, {"type": "done", "status": status, "duration_sec": round(duration, 2)})

        except Exception as exc:
            duration = time.monotonic() - start_monotonic
            logger.exception("Execution error for run %s (%s)", run_id, job["name"])
            err = scrub_text(f"Failed to start runner ({runner_exe or 'runner'}): {exc}")
            try:
                with log_path.open("a", encoding="utf-8", errors="replace") as log_fh:
                    log_fh.write(f"\n[Scheduler Error] {err}\n")
            except OSError:
                logger.warning("Failed to append error to log %s", log_path, exc_info=True)
            self.db.mark_run_finished(run_id, "failed", -1, duration, err)
            self._publish(run_id, {"type": "line", "text": f"\n[Scheduler Error] {err}\n"})
            self._publish(run_id, {"type": "done", "status": "failed"})
        finally:
            with self._lock:
                self._starting_runs.discard(run_id)
                self._active_procs.pop(run_id, None)
                self._stop_reasons.pop(run_id, None)
