"""Zero-dependency HTTP API, SSE live log streaming, and static UI server."""

from __future__ import annotations

import json
import logging
import mimetypes
import queue
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .config import AppConfig, browse_directory, inspect_script, server_timezone_name
from .db import Database
from .runner import PipelineRunner, validate_script_args

logger = logging.getLogger(__name__)


def _clean_iis_user(raw: str | None) -> str:
    if not raw:
        return ""
    val = raw.strip()
    if "\\" in val:
        val = val.split("\\", 1)[1]
    if "@" in val:
        val = val.split("@", 1)[0]
    return val.strip()


def _public_run(run: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a copy of a run record without internal server filesystem paths."""
    if run is None:
        return None
    return {k: v for k, v in run.items() if k != "log_file"}


def _public_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a copy of a job record with nested run objects sanitized."""
    if job is None:
        return None
    cleaned = dict(job)
    cleaned["last_run"] = _public_run(cleaned.get("last_run"))
    cleaned["active_run"] = _public_run(cleaned.get("active_run"))
    if isinstance(cleaned.get("recent_runs"), list):
        cleaned["recent_runs"] = [_public_run(r) for r in cleaned["recent_runs"]]
    return cleaned


class SchedulerHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        config: AppConfig,
        db: Database,
        runner: PipelineRunner,
        static_dir: Path,
    ):
        self.config = config
        self.db = db
        self.runner = runner
        self.static_dir = static_dir.resolve()
        super().__init__(server_address, SchedulerRequestHandler)


class SchedulerRequestHandler(BaseHTTPRequestHandler):
    server: SchedulerHTTPServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"
    timeout = 30

    def setup(self) -> None:
        self.timeout = 30
        super().setup()

    def log_message(self, format: str, *args: Any) -> None:
        # Keep console output tidy and free of noisy static asset logs
        if args and isinstance(args[0], str) and args[0].startswith("GET /static"):
            return
        super().log_message(format, *args)

    def _current_user(self) -> str | None:
        header_name = self.server.config.iis_user_header
        raw = self.headers.get(header_name)
        cleaned = _clean_iis_user(raw)
        if cleaned:
            return cleaned
        if self.server.config.dev_mode:
            return self.server.config.dev_user
        return None

    def _check_csrf(self) -> bool:
        if (self.headers.get("X-Scheduler-Request") or "").strip() != "1":
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "Missing required X-Scheduler-Request header."},
            )
            return False
        sec_fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sec_fetch_site and sec_fetch_site not in ("same-origin", "none"):
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "Cross-site state-changing requests are not allowed."},
            )
            return False
        return True

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'",
        )

    def _send_json(self, status: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        raw_len = (self.headers.get("Content-Length") or "0").strip()
        try:
            length = int(raw_len)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header.") from exc
        if length < 0:
            raise ValueError("Invalid Content-Length header.")
        if length == 0:
            return {}
        if length > 1_000_000:
            raise ValueError("Request payload too large.")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object.")
        return data

    def _serve_static(self, rel_path: str) -> None:
        if (
            not rel_path
            or "\x00" in rel_path
            or any(ord(ch) < 32 for ch in rel_path)
            or "\\" in rel_path
            or ":" in rel_path
            or rel_path.startswith("/")
        ):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Forbidden"})
            return
        segments = rel_path.split("/")
        if any(seg in ("", ".", "..") for seg in segments):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Forbidden"})
            return

        target = (self.server.static_dir / rel_path).resolve()
        try:
            target.relative_to(self.server.static_dir)
        except ValueError:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Forbidden"})
            return
        if not target.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        content = target.read_bytes()
        mime, _ = mimetypes.guess_type(str(target))
        content_type = mime or "application/octet-stream"
        if content_type.startswith(("text/", "application/javascript")):
            content_type += "; charset=utf-8"

        self.send_response(HTTPStatus.OK)
        self._send_security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _handle_sse_stream(self, run_id: str) -> None:
        run = self.server.db.get_run(run_id)
        if not run:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Run not found."})
            return

        try:
            sub_q = self.server.runner.subscribe(run_id)
        except RuntimeError as exc:
            self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(exc)})
            return

        try:
            self.close_connection = True
            self.send_response(HTTPStatus.OK)
            self._send_security_headers()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def send_event(payload: dict[str, Any]) -> None:
                chunk = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()

            # Re-check run status after subscribe() so a run finishing mid-handshake does not stall
            run = self.server.db.get_run(run_id) or run
            existing_log = self.server.runner.read_run_log(run)
            run = self.server.db.get_run(run_id) or run
            send_event({"type": "snapshot", "status": run["status"], "log": existing_log})

            if run["status"] not in ("queued", "running"):
                send_event({"type": "done", "status": run["status"]})
                return

            while True:
                try:
                    event = sub_q.get(timeout=15.0)
                    send_event(event)
                    if event.get("type") == "done":
                        break
                except queue.Empty:
                    # Keep-alive comment so IIS/proxies keep the SSE stream open
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    latest = self.server.db.get_run(run_id)
                    if not latest or latest["status"] not in ("queued", "running"):
                        send_event({"type": "done", "status": latest["status"] if latest else "stopped"})
                        break
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            pass
        finally:
            self.server.runner.unsubscribe(run_id, sub_q)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path == "/healthz":
            try:
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "queue": self.server.runner.active_counts(),
                    },
                )
            except Exception:
                logger.exception("Unhandled error in GET /healthz")
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error."})
            return

        if not path.startswith("/api/"):
            if path in ("", "/"):
                self._serve_static("index.html")
            elif path.startswith("/"):
                self._serve_static(path[1:])
            else:
                self._serve_static(path)
            return

        user = self._current_user()
        if not user:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Windows Authentication required via IIS."})
            return

        try:
            if path == "/api/me":
                cfg = self.server.config
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "user": user,
                        "app_name": cfg.app_name,
                        "dev_mode": cfg.dev_mode,
                        "default_timeout_minutes": cfg.default_timeout_minutes,
                        "timezone": server_timezone_name(),
                        "roots": [{"id": r.id, "label": r.label} for r in cfg.scripts_roots],
                        "supported_extensions": sorted(cfg.allowed_extensions()),
                        "queue": self.server.runner.active_counts(),
                    },
                )
                return

            if path == "/api/browse":
                default_root = self.server.config.scripts_roots[0].id if self.server.config.scripts_roots else ""
                root_id = (query.get("root") or [default_root])[0]
                rel_path = (query.get("path") or [""])[0]
                data = browse_directory(self.server.config, root_id, rel_path)
                self._send_json(HTTPStatus.OK, data)
                return

            if path == "/api/inspect":
                script_ref = (query.get("script_ref") or [""])[0]
                if not script_ref:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "script_ref is required."})
                    return
                data = inspect_script(self.server.config, script_ref)
                self._send_json(HTTPStatus.OK, data)
                return

            if path == "/api/jobs":
                jobs = self.server.db.list_jobs()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "jobs": [_public_job(j) for j in jobs],
                        "queue": self.server.runner.active_counts(),
                    },
                )
                return

            if path == "/api/runs":
                limit_raw = (query.get("limit") or ["100"])[0]
                try:
                    limit = max(1, min(300, int(limit_raw)))
                except ValueError:
                    limit = 100
                runs = self.server.db.list_recent_runs(limit=limit)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "runs": [_public_run(r) for r in runs],
                        "queue": self.server.runner.active_counts(),
                    },
                )
                return

            parts = [p for p in path.split("/") if p]
            # /api/jobs/{id}
            if len(parts) == 3 and parts[1] == "jobs":
                job = self.server.db.get_job(parts[2])
                if not job:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Schedule not found."})
                    return
                self._send_json(HTTPStatus.OK, _public_job(job))
                return

            # /api/jobs/{id}/runs
            if len(parts) == 4 and parts[1] == "jobs" and parts[3] == "runs":
                job = self.server.db.get_job(parts[2])
                if not job:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Schedule not found."})
                    return
                runs = self.server.db.list_runs_for_job(parts[2], limit=30)
                self._send_json(HTTPStatus.OK, {"runs": [_public_run(r) for r in runs]})
                return

            # /api/runs/{run_id}
            if len(parts) == 3 and parts[1] == "runs":
                run = self.server.db.get_run(parts[2])
                if not run:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Run not found."})
                    return
                job = self.server.db.get_job(run["job_id"])
                log_text = self.server.runner.read_run_log(run)
                ai_prompt = self.server.runner.build_ai_diagnostic_prompt(job, run, log_text) if job else ""
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "run": _public_run(run),
                        "job": _public_job(job),
                        "log": log_text,
                        "ai_prompt": ai_prompt,
                    },
                )
                return

            # /api/runs/{run_id}/stream
            if len(parts) == 4 and parts[1] == "runs" and parts[3] == "stream":
                self._handle_sse_stream(parts[2])
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})
        except (ValueError, FileNotFoundError, PermissionError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            logger.exception("Unhandled error in GET %s", path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error."})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        user = self._current_user()
        if not user:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Windows Authentication required via IIS."})
            return
        if not self._check_csrf():
            return

        parts = [p for p in path.split("/") if p]
        try:
            # POST /api/jobs
            if path == "/api/jobs":
                body = self._read_json_body()
                self._validate_job_payload(body, is_create=True)
                job = self.server.db.create_job(body, user, self.server.config.default_timeout_minutes)
                self._send_json(HTTPStatus.CREATED, _public_job(job))
                return

            # POST /api/jobs/{id}/run
            if len(parts) == 4 and parts[1] == "jobs" and parts[3] == "run":
                run = self.server.runner.enqueue_job(parts[2], trigger="manual", triggered_by=user)
                self._send_json(HTTPStatus.ACCEPTED, {"run": _public_run(run)})
                return

            # POST /api/runs/{run_id}/stop
            if len(parts) == 4 and parts[1] == "runs" and parts[3] == "stop":
                stopped = self.server.runner.stop_run(parts[2], requested_by=user)
                self._send_json(HTTPStatus.OK, {"stopped": stopped})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})
        except KeyError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc).strip("'")})
        except RuntimeError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except (ValueError, FileNotFoundError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            logger.exception("Unhandled error in POST %s", path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error."})

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        user = self._current_user()
        if not user:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Windows Authentication required via IIS."})
            return
        if not self._check_csrf():
            return

        parts = [p for p in path.split("/") if p]
        try:
            if len(parts) == 3 and parts[1] == "jobs":
                existing = self.server.db.get_job(parts[2])
                if not existing:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Schedule not found."})
                    return
                body = self._read_json_body()
                self._validate_job_payload(body, is_create=False, existing_job=existing)
                updated = self.server.db.update_job(parts[2], body, user)
                if not updated:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Schedule not found."})
                    return
                self._send_json(HTTPStatus.OK, _public_job(updated))
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})
        except (ValueError, FileNotFoundError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            logger.exception("Unhandled error in PUT %s", path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error."})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        user = self._current_user()
        if not user:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Windows Authentication required via IIS."})
            return
        if not self._check_csrf():
            return

        parts = [p for p in path.split("/") if p]
        try:
            if len(parts) == 3 and parts[1] == "jobs":
                job_id = parts[2]
                with self.server.runner._lock:
                    if self.server.db.has_active_run(job_id):
                        self._send_json(
                            HTTPStatus.CONFLICT,
                            {"error": "Stop the active run before deleting this schedule."},
                        )
                        return
                    deleted, log_files = self.server.db.delete_job_with_logs(job_id)

                if not deleted:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Schedule not found."})
                    return

                logs_root = self.server.config.logs_dir.resolve()
                for path_str in log_files:
                    if not path_str:
                        continue
                    try:
                        lp = Path(path_str).resolve()
                        lp.relative_to(logs_root)
                        lp.unlink(missing_ok=True)
                    except (OSError, ValueError):
                        logger.warning("Failed to remove log file %s for deleted job %s", path_str, job_id)

                self._send_json(HTTPStatus.OK, {"deleted": True})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})
        except (ValueError, FileNotFoundError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            logger.exception("Unhandled error in DELETE %s", path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error."})

    def _validate_job_payload(
        self,
        body: dict[str, Any],
        is_create: bool,
        existing_job: dict[str, Any] | None = None,
    ) -> None:
        if "python_exe" in body:
            raw_py = body["python_exe"]
            if raw_py is not None and not isinstance(raw_py, str):
                raise ValueError("Custom interpreter paths must be configured in config.json.")
            if isinstance(raw_py, str) and raw_py.strip():
                raise ValueError("Custom interpreter paths must be configured in config.json.")
            body["python_exe"] = ""

        if "name" in body:
            raw_name = body["name"]
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise ValueError("Please give this schedule a name.")
            if len(raw_name.strip()) > 200:
                raise ValueError("Schedule name must be 200 characters or fewer.")
        elif is_create:
            raise ValueError("Please give this schedule a name.")

        if "description" in body:
            raw_desc = body["description"]
            if raw_desc is None:
                body["description"] = ""
            elif not isinstance(raw_desc, str):
                raise ValueError("Description must be a string.")
            elif len(raw_desc) > 1000:
                raise ValueError("Description must be 1000 characters or fewer.")

        if "owner" in body:
            raw_owner = body["owner"]
            if raw_owner is None:
                body["owner"] = ""
            elif not isinstance(raw_owner, str):
                raise ValueError("Owner must be a string.")
            elif len(raw_owner.strip()) > 120:
                raise ValueError("Owner must be 120 characters or fewer.")

        if "args" in body:
            raw_args = body["args"]
            if raw_args is None:
                body["args"] = ""
            elif not isinstance(raw_args, str):
                raise ValueError("Script arguments must be a string.")
            elif len(raw_args) > 2000:
                raise ValueError("Script arguments must be 2000 characters or fewer.")

        if "timeout_minutes" in body:
            raw_timeout = body["timeout_minutes"]
            if raw_timeout is None or isinstance(raw_timeout, bool):
                raise ValueError("Timeout must be a whole number of minutes (1-480).")
            try:
                timeout_val = int(raw_timeout)
            except (TypeError, ValueError) as exc:
                raise ValueError("Timeout must be a whole number of minutes (1-480).") from exc
            if timeout_val < 1 or timeout_val > 480:
                raise ValueError("Timeout must be between 1 and 480 minutes.")
            body["timeout_minutes"] = timeout_val

        if "enabled" in body and not isinstance(body["enabled"], bool):
            raise ValueError("Enabled must be true or false.")

        resolved_script_path: Path | None = None
        if "script_ref" in body:
            raw_ref = body["script_ref"]
            if not isinstance(raw_ref, str) or not raw_ref.strip():
                raise ValueError("Please pick a script.")
            _, resolved_script_path, _ = self.server.config.resolve_script_reference(raw_ref.strip())
            if not resolved_script_path.is_file() or not self.server.config.is_supported_script(resolved_script_path):
                exts = ", ".join(sorted(self.server.config.allowed_extensions()))
                raise FileNotFoundError(
                    f"Selected script must be an existing supported script ({exts}) inside your analytics folder."
                )
        elif is_create:
            raise ValueError("Please pick a script.")
        elif existing_job is not None and "args" in body:
            _, resolved_script_path, _ = self.server.config.resolve_script_reference(str(existing_job["script_ref"]))

        if resolved_script_path is not None:
            effective_args = (
                body["args"]
                if "args" in body
                else (str(existing_job.get("args") or "") if existing_job is not None else "")
            )
            validate_script_args(resolved_script_path, effective_args.strip())

        if "freshness_path" in body:
            raw_fresh = body["freshness_path"]
            if raw_fresh is None:
                body["freshness_path"] = ""
            elif not isinstance(raw_fresh, str):
                raise ValueError("Freshness badge path must be a string.")
            else:
                freshness_path = raw_fresh.strip()
                if freshness_path:
                    _, target, _ = self.server.config.resolve_script_reference(freshness_path)
                    if target.suffix.lower() != ".json":
                        raise ValueError("Freshness badge path must end in .json.")

        if "dashboard_url" in body:
            raw_url = body["dashboard_url"]
            if raw_url is None:
                body["dashboard_url"] = ""
            elif not isinstance(raw_url, str):
                raise ValueError("Dashboard link must be a string.")
            else:
                dashboard_url = raw_url.strip()
                if dashboard_url:
                    if len(dashboard_url) > 2000:
                        raise ValueError("Dashboard link must be 2000 characters or fewer.")
                    if any(ord(ch) < 32 or ord(ch) == 127 for ch in dashboard_url):
                        raise ValueError("Dashboard link contains invalid control characters.")
                    if dashboard_url.startswith("/"):
                        if dashboard_url.startswith("//") or dashboard_url.startswith("/\\"):
                            raise ValueError(
                                "Dashboard link must be a relative path starting with / or an http(s) URL."
                            )
                    else:
                        parsed_url = urlparse(dashboard_url)
                        if parsed_url.scheme.lower() not in ("http", "https") or not parsed_url.netloc:
                            raise ValueError("Dashboard link must start with /, http://, or https://")
