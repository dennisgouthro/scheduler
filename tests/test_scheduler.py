"""Unit & integration tests for Analytics Scheduler (pure Python stdlib)."""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scheduler.api import SchedulerHTTPServer
from scheduler.config import AppConfig, ScriptRoot, browse_directory, inspect_script, load_config
from scheduler.db import Database
from scheduler.runner import PipelineRunner, validate_script_args
from scheduler.schedule_math import compute_next_run, describe_schedule
from scheduler.scrubber import LogScrubber, scrub_text


class ScheduleMathTests(unittest.TestCase):
    def test_weekdays_skips_weekend(self) -> None:
        # Friday 2026-11-27 09:00 -> Monday 2026-11-30 07:00
        fri = datetime(2026, 11, 27, 9, 0, 0)
        nxt = compute_next_run("weekdays", {"time": "07:00"}, after=fri)
        self.assertEqual(nxt, datetime(2026, 11, 30, 7, 0, 0))
        self.assertEqual(describe_schedule("weekdays", {"time": "07:00"}), "Weekdays at 7:00 AM")

    def test_monthly_first_business_day(self) -> None:
        # Nov 2026: Nov 1 is Sunday -> 1st business day is Mon Nov 2
        before = datetime(2026, 10, 31, 12, 0, 0)
        nxt = compute_next_run("monthly", {"time": "06:30", "day_mode": "first_business_day"}, after=before)
        self.assertEqual(nxt, datetime(2026, 11, 2, 6, 30, 0))

    def test_monthly_last_business_day(self) -> None:
        # Oct 2026: Oct 31 is Saturday -> last business day is Fri Oct 30
        before = datetime(2026, 10, 15, 8, 0, 0)
        nxt = compute_next_run("monthly", {"time": "17:00", "day_mode": "last_business_day"}, after=before)
        self.assertEqual(nxt, datetime(2026, 10, 30, 17, 0, 0))

    def test_interval_window_wraps_to_next_weekday(self) -> None:
        fri_evening = datetime(2026, 11, 27, 19, 0, 0)
        nxt = compute_next_run(
            "interval",
            {"every_hours": 2, "start_hour": 7, "end_hour": 18, "minute": 0, "weekdays_only": True},
            after=fri_evening,
        )
        self.assertEqual(nxt, datetime(2026, 11, 30, 7, 0, 0))

    def test_paused_and_manual_return_none(self) -> None:
        self.assertIsNone(compute_next_run("manual", {}))
        self.assertIsNone(compute_next_run("daily", {"time": "07:00"}, enabled=False))


class ScrubberTests(unittest.TestCase):
    def test_scrubs_secrets_and_ssn(self) -> None:
        raw = (
            "Connecting password=superSecret! token='abc123' "
            "https://alice:mypass@acct.snowflakecomputing.com "
            "SSN 123-45-6789 "
            "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADAN\n-----END PRIVATE KEY-----"
        )
        clean = scrub_text(raw)
        self.assertNotIn("superSecret!", clean)
        self.assertNotIn("abc123", clean)
        self.assertNotIn("mypass", clean)
        self.assertNotIn("123-45-6789", clean)
        self.assertNotIn("MIIEvQIBADAN", clean)
        self.assertIn("[PRIVATE KEY REDACTED]", clean)
        self.assertIn("[SSN REDACTED]", clean)

    def test_streaming_multiline_pem_and_ansi_scrubbing(self) -> None:
        scrubber = LogScrubber()
        lines = [
            "\x1b[32mStarting job...\x1b[0m\n",
            "Loading key: -----BEGIN RSA PRIVATE KEY-----\n",
            "MIIEpAIBAAKCAQEAsecretKeyBodyLine1\n",
            "secretKeyBodyLine2==\n",
            "-----END RSA PRIVATE KEY----- SNOWFLAKE_PASSWORD=AfterKeySecret99\n",
            "\x1b[31mDone\x1b[0m\n",
        ]
        scrubbed = "".join(scrubber.scrub_line(line) for line in lines)
        self.assertNotIn("\x1b[", scrubbed)
        self.assertNotIn("secretKeyBodyLine1", scrubbed)
        self.assertNotIn("secretKeyBodyLine2", scrubbed)
        self.assertNotIn("AfterKeySecret99", scrubbed)
        self.assertIn("Starting job...", scrubbed)
        self.assertIn("[PRIVATE KEY REDACTED]", scrubbed)
        self.assertIn("SNOWFLAKE_PASSWORD=[REDACTED]", scrubbed)
        self.assertIn("Done", scrubbed)

    def test_scrubs_env_json_powershell_jwt_and_query_secrets(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhbmFseXN0MTIzIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        samples = [
            "SNOWFLAKE_PASSWORD=Winter2026!Pass",
            '{"api_key": "sk-live-secret-json-value", "client_secret": "jsonClientSecret99", "ok": true}',
            "ConvertTo-SecureString -String 'PsPlainSecret1!' -AsPlainText -Force",
            'ConvertTo-SecureString -AsPlainText -Force "PsPlainSecret2!"',
            "Invoke-Sqlcmd -ServerInstance db01 -Password 'CliFlagSecret!'",
            f"auth_header={jwt}",
            "GET https://api.corp.local/v1/data?api_key=QueryKey123&token=QueryTok456&page=2#top",
        ]
        clean = scrub_text("\n".join(samples))
        for secret in (
            "Winter2026!Pass",
            "sk-live-secret-json-value",
            "jsonClientSecret99",
            "PsPlainSecret1!",
            "PsPlainSecret2!",
            "CliFlagSecret!",
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "QueryKey123",
            "QueryTok456",
        ):
            self.assertNotIn(secret, clean)
        self.assertIn("SNOWFLAKE_PASSWORD=[REDACTED]", clean)
        self.assertIn("[JWT REDACTED]", clean)
        self.assertIn("api_key=[REDACTED]", clean)
        self.assertIn("token=[REDACTED]", clean)
        self.assertIn("page=2", clean)


class ArgValidationTests(unittest.TestCase):
    def test_windows_quoting_preserves_backslashes_and_spaces(self) -> None:
        tokens = validate_script_args(
            Path("refresh.py"),
            '--out="C:\\My Dir\\sales report.csv" --env prod \'single quoted\'',
        )
        self.assertEqual(
            tokens,
            ["--out=C:\\My Dir\\sales report.csv", "--env", "prod", "single quoted"],
        )

    def test_unbalanced_quotes_and_newlines_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_script_args(Path("refresh.py"), '--out="C:\\unclosed')
        with self.assertRaises(ValueError):
            validate_script_args(Path("refresh.py"), "--arg safe\nwhoami")
        with self.assertRaises(ValueError):
            validate_script_args(Path("refresh.py"), "--arg safe\x00bad")

    def test_batch_metacharacters_rejected(self) -> None:
        bat = Path("nightly.bat")
        cmd = Path("nightly.cmd")
        self.assertEqual(
            validate_script_args(bat, '--env prod --out="C:\\Reports\\daily output.csv"'),
            ["--env", "prod", "--out=C:\\Reports\\daily output.csv"],
        )
        for bad_args in (
            "prod & whoami",
            "prod | calc",
            "prod > out.txt",
            "prod < in.txt",
            "prod ^& whoami",
            "%USERNAME%",
            "!SECRET!",
            '--msg="escaped\\"quote"',
        ):
            with self.subTest(bad_args=bad_args):
                with self.assertRaises(ValueError):
                    validate_script_args(bat, bad_args)
                with self.assertRaises(ValueError):
                    validate_script_args(cmd, bad_args)

        with self.assertRaises(ValueError):
            validate_script_args(Path("bad&name.bat"), "")

    def test_vbs_host_option_prefix_rejected(self) -> None:
        vbs = Path("export.vbs")
        self.assertEqual(validate_script_args(vbs, "/mode fast --year 2026"), ["/mode", "fast", "--year", "2026"])
        with self.assertRaises(ValueError):
            validate_script_args(vbs, "//E:jscript")
        with self.assertRaises(ValueError):
            validate_script_args(vbs, "//B")


class ConfigSecurityTests(unittest.TestCase):
    def test_path_hardening_blocks_unc_ads_dotfiles_and_excluded_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp) / "projects"
            (root_dir / "valid").mkdir(parents=True)
            (root_dir / ".git").mkdir(parents=True)
            (root_dir / "__pycache__").mkdir(parents=True)
            (root_dir / "node_modules").mkdir(parents=True)
            (root_dir / ".hidden").mkdir(parents=True)
            (root_dir / "valid" / "run.py").write_text("print('ok')\n", encoding="utf-8")
            (root_dir / ".env").write_text("SECRET=1\n", encoding="utf-8")

            cfg = AppConfig(
                base_dir=Path(tmp),
                data_dir=Path(tmp) / "data",
                scripts_roots=[ScriptRoot(id="projects", label="Projects", path=root_dir)],
            )

            # Valid relative path succeeds
            root, resolved, rel = cfg.resolve_script_reference("projects:valid/run.py")
            self.assertEqual(root.id, "projects")
            self.assertEqual(rel, "valid/run.py")
            self.assertEqual(resolved, (root_dir / "valid" / "run.py").resolve())

            # UNC paths rejected
            for unc in ("\\\\server\\share\\run.py", "//server/share/run.py"):
                with self.subTest(unc=unc):
                    with self.assertRaises(ValueError):
                        cfg.resolve_script_reference(unc)
                    with self.assertRaises(ValueError):
                        cfg.resolve_safe_path("projects", unc)

            # ADS / colon paths rejected
            for ads in ("projects:valid/run.py:stream", "C:\\projects\\run.py:ads", "projects:foo:bar.py"):
                with self.subTest(ads=ads):
                    with self.assertRaises(ValueError):
                        cfg.resolve_script_reference(ads)

            with self.assertRaises(ValueError):
                cfg.resolve_safe_path("projects", "valid/run.py:ads")

            # Dotfiles and EXCLUDED_DIRS blocked
            for blocked in (".env", ".hidden/run.py", ".git/config", "__pycache__/a.py", "node_modules/a.js"):
                with self.subTest(blocked=blocked):
                    with self.assertRaises(ValueError):
                        cfg.resolve_safe_path("projects", blocked)
                    with self.assertRaises(ValueError):
                        cfg.resolve_script_reference(f"projects:{blocked}")

    def test_script_runners_normalization_and_load_config_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            scripts_dir = Path(tmp) / "scripts"
            scripts_dir.mkdir(parents=True)
            cfg_path.write_text(
                json.dumps(
                    {
                        "data_dir": "./data",
                        "misfire_grace_minutes": 0,
                        "max_log_mb": 50,
                        "script_runners": {
                            "YXMD": ["C:\\Alteryx\\AlteryxEngineCmd.exe", "{script}"],
                            " .SQL ": "sqlcmd.exe",
                        },
                        "scripts_roots": [{"id": "main", "label": "Main", "path": "./scripts"}],
                    }
                ),
                encoding="utf-8",
            )

            cfg = load_config(cfg_path)
            self.assertFalse(cfg.dev_mode)
            self.assertEqual(cfg.misfire_grace_minutes, 0)
            self.assertEqual(cfg.max_log_mb, 50)
            self.assertEqual(cfg.script_runners[".yxmd"], ["C:\\Alteryx\\AlteryxEngineCmd.exe", "{script}"])
            self.assertEqual(cfg.script_runners[".sql"], ["sqlcmd.exe"])
            self.assertIn(".yxmd", cfg.allowed_extensions())
            self.assertIn(".sql", cfg.allowed_extensions())

            # Direct AppConfig construction also normalizes script_runners keys and blank python_exe
            direct = AppConfig(
                base_dir=Path(tmp),
                python_exe="   ",
                script_runners={"SAS": ["sas.exe", "{script}"]},
            )
            self.assertFalse(direct.dev_mode)
            self.assertTrue(bool(direct.python_exe.strip()))
            self.assertIn(".sas", direct.script_runners)


class DatabaseTests(unittest.TestCase):
    def test_atomic_mark_run_started_and_delete_job_with_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "scheduler.db")
            job = db.create_job(
                {"name": "Sales ETL", "script_ref": "projects:sales.py", "schedule_type": "manual"},
                user="Analyst",
            )

            run1 = db.create_run(job["id"], "manual", "Analyst", str(Path(tmp) / "run1.log"), run_id="run_test_1")
            self.assertTrue(db.mark_run_started(run1["id"]))
            # Already running -> second transition fails atomically
            self.assertFalse(db.mark_run_started(run1["id"]))
            db.mark_run_finished(run1["id"], "succeeded", 0, 1.2)

            run2 = db.create_run(job["id"], "manual", "Analyst", str(Path(tmp) / "run2.log"), run_id="run_test_2")
            db.mark_run_finished(run2["id"], "stopped", None, 0.0, "Cancelled")
            # Stopped -> cannot transition to running
            self.assertFalse(db.mark_run_started(run2["id"]))

            # list_jobs omits window helper 'rn' and internal 'log_file'
            listed = db.list_jobs()
            self.assertEqual(len(listed), 1)
            for run_rec in listed[0]["recent_runs"]:
                self.assertNotIn("rn", run_rec)
                self.assertNotIn("_rid", run_rec)
                self.assertNotIn("log_file", run_rec)

            deleted, log_files = db.delete_job_with_logs(job["id"])
            self.assertTrue(deleted)
            self.assertEqual(set(log_files), {str(Path(tmp) / "run1.log"), str(Path(tmp) / "run2.log")})
            self.assertIsNone(db.get_job(job["id"]))
            self.assertEqual(db.delete_job_with_logs("job_missing"), (False, []))

    def test_claim_due_jobs_misfire_grace_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "scheduler.db")
            stale_job = db.create_job(
                {
                    "name": "Stale Morning Job",
                    "script_ref": "projects:stale.py",
                    "schedule_type": "daily",
                    "schedule_config": {"time": "07:00"},
                },
                user="Analyst",
            )
            fresh_job = db.create_job(
                {
                    "name": "Fresh Job",
                    "script_ref": "projects:fresh.py",
                    "schedule_type": "daily",
                    "schedule_config": {"time": "09:50"},
                },
                user="Analyst",
            )

            now = datetime(2026, 11, 25, 10, 0, 0)
            stale_due = (now - timedelta(hours=3)).isoformat()
            fresh_due = (now - timedelta(minutes=10)).isoformat()
            with db._connect() as conn:
                conn.execute("UPDATE jobs SET next_run_at = ? WHERE id = ?", (stale_due, stale_job["id"]))
                conn.execute("UPDATE jobs SET next_run_at = ? WHERE id = ?", (fresh_due, fresh_job["id"]))

            claimed = db.claim_due_jobs(now=now, misfire_grace_minutes=60)
            claimed_ids = [j["id"] for j in claimed]
            self.assertEqual(claimed_ids, [fresh_job["id"]])

            # Both jobs had next_run_at advanced strictly past `now`
            updated_stale = db.get_job(stale_job["id"])
            updated_fresh = db.get_job(fresh_job["id"])
            assert updated_stale is not None and updated_fresh is not None
            self.assertGreater(datetime.fromisoformat(updated_stale["next_run_at"]), now)
            self.assertGreater(datetime.fromisoformat(updated_fresh["next_run_at"]), now)

    def test_update_job_none_safety_ignores_python_exe_and_preserves_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "scheduler.db")
            job = db.create_job(
                {
                    "name": "Daily KPI",
                    "description": "Original note",
                    "script_ref": "projects:kpi.py",
                    "python_exe": "C:\\evil.exe",
                    "schedule_type": "daily",
                    "schedule_config": {"time": "08:00"},
                    "owner": "Alice",
                },
                user="Alice",
            )
            self.assertEqual(job["python_exe"], "")

            # Pin a specific next_run_at so we can verify metadata-only edits preserve it
            pinned_next = "2026-12-01T08:00:00"
            with db._connect() as conn:
                conn.execute("UPDATE jobs SET next_run_at = ? WHERE id = ?", (pinned_next, job["id"]))

            updated = db.update_job(
                job["id"],
                {
                    "name": "Daily KPI Renamed",
                    "description": None,
                    "owner": None,
                    "python_exe": "C:\\still-ignored.exe",
                    "schedule_type": None,
                    "schedule_config": None,
                    "enabled": None,
                    "timeout_minutes": None,
                },
                user="Bob",
            )
            assert updated is not None
            self.assertEqual(updated["name"], "Daily KPI Renamed")
            self.assertEqual(updated["description"], "")
            self.assertEqual(updated["owner"], "Alice")
            self.assertEqual(updated["python_exe"], "")
            self.assertEqual(updated["next_run_at"], pinned_next)

            # Pausing changes schedule state and clears next_run_at
            paused = db.update_job(job["id"], {"enabled": False}, user="Bob")
            assert paused is not None
            self.assertFalse(paused["enabled"])
            self.assertIsNone(paused["next_run_at"])


class ConfigAndRunnerTests(unittest.TestCase):
    def test_path_boundary_and_readme_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp) / "projects"
            proj = root_dir / "exec_sales"
            (proj / "python").mkdir(parents=True)
            (proj / "reports").mkdir(parents=True)
            (proj / "README.md").write_text(
                "# Executive Sales\n\nRefreshes executive sales KPIs from Snowflake.\n",
                encoding="utf-8",
            )
            script = proj / "python" / "run.py"
            script.write_text("print('hello analytics')\n", encoding="utf-8")

            cfg = AppConfig(
                base_dir=Path(tmp),
                data_dir=Path(tmp) / "data",
                python_exe=sys.executable,
                scripts_roots=[ScriptRoot(id="projects", label="Projects", path=root_dir)],
            )
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
            cfg.logs_dir.mkdir(parents=True, exist_ok=True)

            # Traversal rejected
            with self.assertRaises(ValueError):
                cfg.resolve_safe_path("projects", "../secret.py")

            # Browse & README inspect
            listing = browse_directory(cfg, "projects", "")
            self.assertEqual(listing["folders"][0]["name"], "exec_sales")
            self.assertTrue(listing["folders"][0]["is_project"])

            info = inspect_script(cfg, "projects:exec_sales/python/run.py")
            self.assertEqual(info["suggested_name"], "Executive Sales")
            self.assertIn("Snowflake", info["suggested_description"])
            self.assertTrue(info["readme_found"])

            # End-to-end execution + freshness badge
            db = Database(cfg.db_path)
            runner = PipelineRunner(cfg, db)
            runner.start()
            try:
                job = db.create_job(
                    {
                        "name": "Executive Sales",
                        "script_ref": "projects:exec_sales/python/run.py",
                        "freshness_path": "projects:exec_sales/reports/freshness.json",
                        "schedule_type": "weekdays",
                        "schedule_config": {"time": "07:00"},
                    },
                    user="Analyst",
                )
                run = runner.enqueue_job(job["id"], trigger="manual", triggered_by="Analyst")
                for _ in range(50):
                    latest = db.get_run(run["id"])
                    if latest and latest["status"] not in ("queued", "running"):
                        break
                    time.sleep(0.1)

                latest = db.get_run(run["id"])
                assert latest is not None
                self.assertEqual(latest["status"], "succeeded")
                self.assertIn("hello analytics", runner.read_run_log(latest))

                badge = json.loads((proj / "reports" / "freshness.json").read_text(encoding="utf-8"))
                self.assertEqual(badge["status"], "fresh")
            finally:
                runner.shutdown()

    def test_multi_script_types_and_custom_runners(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp) / "scripts"
            root_dir.mkdir(parents=True)
            (root_dir / "refresh_data.ps1").write_text("Write-Output 'hello powershell'\n", encoding="utf-8")
            (root_dir / "nightly_job.bat").write_text("@echo hello batch\r\n", encoding="utf-8")
            (root_dir / "model.R").write_text("cat('hello r')\n", encoding="utf-8")
            (root_dir / "workflow.yxmd").write_text("<AlteryxDocument />\n", encoding="utf-8")
            (root_dir / "notes.txt").write_text("ignored\n", encoding="utf-8")

            cfg = AppConfig(
                base_dir=Path(tmp),
                data_dir=Path(tmp) / "data",
                python_exe=sys.executable,
                script_runners={".yxmd": ["C:\\Program Files\\Alteryx\\bin\\AlteryxEngineCmd.exe", "{script}"]},
                scripts_roots=[ScriptRoot(id="scripts", label="Scripts", path=root_dir)],
            )
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
            cfg.logs_dir.mkdir(parents=True, exist_ok=True)

            listing = browse_directory(cfg, "scripts", "")
            names = [s["name"] for s in listing["scripts"]]
            self.assertIn("refresh_data.ps1", names)
            self.assertIn("nightly_job.bat", names)
            self.assertIn("model.R", names)
            self.assertIn("workflow.yxmd", names)
            self.assertNotIn("notes.txt", names)

            ps_info = inspect_script(cfg, "scripts:refresh_data.ps1")
            self.assertEqual(ps_info["suggested_name"], "Refresh Data")

            db = Database(cfg.db_path)
            runner = PipelineRunner(cfg, db)

            ps_cmd, _ = runner._build_script_command(root_dir / "refresh_data.ps1", {}, ["-Env", "Prod"])
            self.assertIn("-ExecutionPolicy", ps_cmd)
            self.assertIn("Bypass", ps_cmd)
            self.assertEqual(ps_cmd[-3:], [str(root_dir / "refresh_data.ps1"), "-Env", "Prod"])

            yxmd_cmd, _ = runner._build_script_command(root_dir / "workflow.yxmd", {}, [])
            self.assertEqual(
                yxmd_cmd,
                ["C:\\Program Files\\Alteryx\\bin\\AlteryxEngineCmd.exe", str(root_dir / "workflow.yxmd")],
            )

            if os.name == "nt":
                runner.start()
                try:
                    bat_job = db.create_job(
                        {
                            "name": "Nightly Batch",
                            "script_ref": "scripts:nightly_job.bat",
                            "schedule_type": "manual",
                        },
                        user="Analyst",
                    )
                    run = runner.enqueue_job(bat_job["id"], trigger="manual", triggered_by="Analyst")
                    for _ in range(50):
                        latest = db.get_run(run["id"])
                        if latest and latest["status"] not in ("queued", "running"):
                            break
                        time.sleep(0.1)
                    latest = db.get_run(run["id"])
                    assert latest is not None
                    self.assertEqual(latest["status"], "succeeded")
                    self.assertIn("hello batch", runner.read_run_log(latest))
                finally:
                    runner.shutdown()


class ApiIntegrationTests(unittest.TestCase):
    def _request_json(
        self,
        base_url: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        req_headers = dict(headers or {})
        data: bytes | None = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(f"{base_url}{path}", data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            try:
                raw = err.read().decode("utf-8")
                parsed = json.loads(raw) if raw else {}
            finally:
                err.close()
            return err.code, parsed

    def _raw_get(self, port: int, raw_path: str) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", raw_path, headers={"Connection": "close"})
            resp = conn.getresponse()
            body = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, hdrs, body
        finally:
            conn.close()

    def test_http_api_security_and_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp) / "projects"
            root_dir.mkdir(parents=True)
            (root_dir / "hello.py").write_text("print('api test ok')\n", encoding="utf-8")

            static_dir = Path(tmp) / "static"
            static_dir.mkdir(parents=True)
            (static_dir / "index.html").write_text("<!doctype html><title>Scheduler</title>", encoding="utf-8")

            cfg = AppConfig(
                base_dir=Path(tmp),
                data_dir=Path(tmp) / "data",
                dev_mode=False,
                python_exe=sys.executable,
                scripts_roots=[ScriptRoot(id="projects", label="Projects", path=root_dir)],
            )
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
            cfg.logs_dir.mkdir(parents=True, exist_ok=True)

            db = Database(cfg.db_path)
            runner = PipelineRunner(cfg, db)
            runner.start()
            httpd = SchedulerHTTPServer(("127.0.0.1", 0), cfg, db, runner, static_dir)
            port = httpd.server_address[1]
            base_url = f"http://127.0.0.1:{port}"
            server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            server_thread.start()

            try:
                auth_headers = {"X-IIS-User": "CORP\\jdoe"}
                csrf_headers = {"X-IIS-User": "CORP\\jdoe", "X-Scheduler-Request": "1"}

                # 1. /healthz is unauthenticated
                status, health = self._request_json(base_url, "GET", "/healthz")
                self.assertEqual(status, 200)
                self.assertEqual(health["status"], "ok")
                self.assertIn("queue", health)

                # 2. /api/me requires configured IIS header (ignores X-Remote-User)
                status, _ = self._request_json(base_url, "GET", "/api/me", headers={"X-Remote-User": "spoof"})
                self.assertEqual(status, 401)
                status, me = self._request_json(base_url, "GET", "/api/me", headers=auth_headers)
                self.assertEqual(status, 200)
                self.assertEqual(me["user"], "jdoe")
                self.assertTrue(bool(me.get("timezone")))
                self.assertIn(".py", me["supported_extensions"])

                # 3. CSRF enforcement on state-changing requests
                job_payload = {
                    "name": "API Hello",
                    "script_ref": "projects:hello.py",
                    "schedule_type": "manual",
                }
                status, err = self._request_json(base_url, "POST", "/api/jobs", job_payload, headers=auth_headers)
                self.assertEqual(status, 403)
                self.assertIn("X-Scheduler-Request", err["error"])

                cross_site = dict(csrf_headers, **{"Sec-Fetch-Site": "cross-site"})
                status, _ = self._request_json(base_url, "POST", "/api/jobs", job_payload, headers=cross_site)
                self.assertEqual(status, 403)

                # 4. python_exe override rejected
                bad_py = dict(job_payload, python_exe="C:\\Windows\\System32\\cmd.exe")
                status, err = self._request_json(base_url, "POST", "/api/jobs", bad_py, headers=csrf_headers)
                self.assertEqual(status, 400)
                self.assertIn("config.json", err["error"])

                # 5. dashboard_url protocol-relative / unsafe URLs rejected
                for bad_url in ("//evil.com/phish", "/\\evil.com", "javascript:alert(1)"):
                    with self.subTest(bad_url=bad_url):
                        bad_payload = dict(job_payload, dashboard_url=bad_url)
                        status, _ = self._request_json(base_url, "POST", "/api/jobs", bad_payload, headers=csrf_headers)
                        self.assertEqual(status, 400)

                # 6. Create valid job with CSRF header -> 201
                valid_payload = dict(job_payload, dashboard_url="/reports/sales")
                status, created = self._request_json(base_url, "POST", "/api/jobs", valid_payload, headers=csrf_headers)
                self.assertEqual(status, 201)
                job_id = created["id"]

                # 7. Trigger run, wait for completion, verify log_file omitted everywhere
                status, run_resp = self._request_json(
                    base_url, "POST", f"/api/jobs/{job_id}/run", headers=csrf_headers
                )
                self.assertEqual(status, 202)
                self.assertNotIn("log_file", run_resp["run"])
                run_id = run_resp["run"]["id"]

                for _ in range(50):
                    s, detail = self._request_json(base_url, "GET", f"/api/runs/{run_id}", headers=auth_headers)
                    self.assertEqual(s, 200)
                    if detail["run"]["status"] not in ("queued", "running"):
                        break
                    time.sleep(0.1)

                status, detail = self._request_json(base_url, "GET", f"/api/runs/{run_id}", headers=auth_headers)
                self.assertEqual(status, 200)
                self.assertEqual(detail["run"]["status"], "succeeded")
                self.assertNotIn("log_file", detail["run"])
                self.assertIn("api test ok", detail["log"])

                status, runs_list = self._request_json(base_url, "GET", "/api/runs", headers=auth_headers)
                self.assertEqual(status, 200)
                self.assertTrue(len(runs_list["runs"]) >= 1)
                for r in runs_list["runs"]:
                    self.assertNotIn("log_file", r)

                status, job_runs = self._request_json(base_url, "GET", f"/api/jobs/{job_id}/runs", headers=auth_headers)
                self.assertEqual(status, 200)
                for r in job_runs["runs"]:
                    self.assertNotIn("log_file", r)

                status, jobs_list = self._request_json(base_url, "GET", "/api/jobs", headers=auth_headers)
                self.assertEqual(status, 200)
                for j in jobs_list["jobs"]:
                    if j["last_run"]:
                        self.assertNotIn("log_file", j["last_run"])
                    for r in j["recent_runs"]:
                        self.assertNotIn("log_file", r)

                # 8. Static serving security: valid index + reject backslash/UNC/colon/traversal
                status, hdrs, body = self._raw_get(port, "/")
                self.assertEqual(status, 200)
                self.assertIn(b"Scheduler", body)
                self.assertEqual(hdrs.get("x-content-type-options"), "nosniff")
                self.assertIn("object-src 'none'", hdrs.get("content-security-policy", ""))

                for bad_static in (
                    "/%5C%5Cserver%5Cshare",
                    "//server/share",
                    "/C:/Windows/win.ini",
                    "/../config.json",
                ):
                    with self.subTest(bad_static=bad_static):
                        st, _, _ = self._raw_get(port, bad_static)
                        self.assertEqual(st, 403)

                # 9. DELETE removes job and cleans up log file
                raw_run = db.get_run(run_id)
                assert raw_run is not None
                log_path = Path(raw_run["log_file"])
                self.assertTrue(log_path.is_file())

                status, _ = self._request_json(base_url, "DELETE", f"/api/jobs/{job_id}", headers=auth_headers)
                self.assertEqual(status, 403)

                status, del_resp = self._request_json(base_url, "DELETE", f"/api/jobs/{job_id}", headers=csrf_headers)
                self.assertEqual(status, 200)
                self.assertTrue(del_resp["deleted"])
                self.assertFalse(log_path.exists())
            finally:
                httpd.shutdown()
                httpd.server_close()
                server_thread.join(timeout=5)
                runner.shutdown()


if __name__ == "__main__":
    unittest.main()
