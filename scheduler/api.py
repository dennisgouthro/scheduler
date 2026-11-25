"""Zero-dependency HTTP API, SSE live log streaming, and static UI server."""

from __future__ import annotations

import json
import mimetypes
import queue
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .config import AppConfig, browse_directory, inspect_script
from .db import Database
from .runner import PipelineRunner


def _clean_iis_user(raw: str | None) -> str:
    if not raw:
        return ""
    val = raw.strip()
    if "\\" in val:
        val = val.split("\\", 1)[1]
    if "@" in val:
        val = val.split("@", 1)[0]
    return val.strip()


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

    def log_message(self, format: str, *args: Any) -> None:
        # Keep console output tidy and free of noisy static asset logs
        if args and isinstance(args[0], str) and args[0].startswith("GET /static"):
            return
        super().log_message(format, *args)

    def _current_user(self) -> str | None:
        header_name = self.server.config.iis_user_header
        raw = self.headers.get(header_name) or self.headers.get("X-Remote-User") or self.headers.get("REMOTE_USER")
        cleaned = _clean_iis_user(raw)
        if cleaned:
            return cleaned
        if self.server.config.dev_mode:
            return self.server.config.dev_user
        return None

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'",
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
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("Request payload too large.")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object.")
        return data

    def _serve_static(self, rel_path: str) -> None:
        clean = rel_path.lstrip("/") or "index.html"
        if ".." in clean.split("/"):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Forbidden"})
            return
        target = (self.server.static_dir / clean).resolve()
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

        sub_q = self.server.runner.subscribe(run_id)
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

            # Send existing log immediately so late-joining viewers see everything
            existing_log = self.server.runner.read_run_log(run)
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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            self.server.runner.unsubscribe(run_id, sub_q)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if not path.startswith("/api/"):
            if path in ("", "/"):
                self._serve_static("index.html")
            else:
                self._serve_static(path.lstrip("/"))
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
                        "roots": [{"id": r.id, "label": r.label} for r in cfg.scripts_roots],
                        "queue": self.server.runner.active_counts(),
                    },
                )
                return

            if path == "/api/browse":
                root_id = (query.get("root") or [self.server.config.scripts_roots[0].id])[0]
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
                        "jobs": jobs,
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
                        "runs": runs,
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
                self._send_json(HTTPStatus.OK, job)
                return

            # /api/jobs/{id}/runs
            if len(parts) == 4 and parts[1] == "jobs" and parts[3] == "runs":
                job = self.server.db.get_job(parts[2])
                if not job:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Schedule not found."})
                    return
                runs = self.server.db.list_runs_for_job(parts[2], limit=30)
                self._send_json(HTTPStatus.OK, {"runs": runs})
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
                        "run": run,
                        "job": job,
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
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Server error: {exc}"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        user = self._current_user()
        if not user:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Windows Authentication required via IIS."})
            return

        parts = [p for p in path.split("/") if p]
        try:
            # POST /api/jobs
            if path == "/api/jobs":
                body = self._read_json_body()
                self._validate_job_payload(body, is_create=True)
                job = self.server.db.create_job(body, user, self.server.config.default_timeout_minutes)
                self._send_json(HTTPStatus.CREATED, job)
                return

            # POST /api/jobs/{id}/run
            if len(parts) == 4 and parts[1] == "jobs" and parts[3] == "run":
                run = self.server.runner.enqueue_job(parts[2], trigger="manual", triggered_by=user)
                self._send_json(HTTPStatus.ACCEPTED, {"run": run})
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
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Server error: {exc}"})

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        user = self._current_user()
        if not user:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Windows Authentication required via IIS."})
            return

        parts = [p for p in path.split("/") if p]
        try:
            if len(parts) == 3 and parts[1] == "jobs":
                body = self._read_json_body()
                self._validate_job_payload(body, is_create=False)
                updated = self.server.db.update_job(parts[2], body, user)
                if not updated:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Schedule not found."})
                    return
                self._send_json(HTTPStatus.OK, updated)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})
        except (ValueError, FileNotFoundError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Server error: {exc}"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        user = self._current_user()
        if not user:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Windows Authentication required via IIS."})
            return

        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[1] == "jobs":
            if self.server.db.has_active_run(parts[2]):
                self._send_json(HTTPStatus.CONFLICT, {"error": "Stop the active run before deleting this schedule."})
                return
            deleted = self.server.db.delete_job(parts[2])
            if not deleted:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Schedule not found."})
                return
            self._send_json(HTTPStatus.OK, {"deleted": True})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})

    def _validate_job_payload(self, body: dict[str, Any], is_create: bool) -> None:
        if is_create:
            if not str(body.get("name") or "").strip():
                raise ValueError("Please give this schedule a name.")
            if not str(body.get("script_ref") or "").strip():
                raise ValueError("Please pick a Python script.")

        script_ref = body.get("script_ref")
        if script_ref is not None:
            _, script_path, _ = self.server.config.resolve_script_reference(str(script_ref))
            if not script_path.is_file() or script_path.suffix.lower() != ".py":
                raise FileNotFoundError("Selected script must be an existing .py file inside your analytics folder.")

        freshness_path = str(body.get("freshness_path") or "").strip()
        if freshness_path:
            _, target, _ = self.server.config.resolve_script_reference(freshness_path)
            if target.suffix.lower() != ".json":
                raise ValueError("Freshness badge path must end in .json.")

        dashboard_url = str(body.get("dashboard_url") or "").strip()
        if dashboard_url and not (
            dashboard_url.startswith("/")
            or dashboard_url.startswith("https://")
            or dashboard_url.startswith("http://")
        ):
            raise ValueError("Dashboard link must start with /, http://, or https://")
