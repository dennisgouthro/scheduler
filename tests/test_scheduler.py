"""Unit & integration tests for Analytics Scheduler (pure Python stdlib)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from scheduler.config import AppConfig, ScriptRoot, browse_directory, inspect_script
from scheduler.db import Database
from scheduler.runner import PipelineRunner
from scheduler.schedule_math import compute_next_run, describe_schedule
from scheduler.scrubber import scrub_text


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


if __name__ == "__main__":
    unittest.main()
