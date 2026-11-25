"""Entry point for Analytics Scheduler (zero third-party pip packages required).

Run locally:
    python server.py

Optional custom config path:
    python server.py --config C:\\AnalyticsScheduler\\config.json
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from scheduler.api import SchedulerHTTPServer
from scheduler.config import load_config
from scheduler.db import Database
from scheduler.runner import PipelineRunner


def main() -> None:
    parser = argparse.ArgumentParser(description="Analytics Scheduler — lightweight IIS Python scheduler")
    parser.add_argument("--config", default=None, help="Path to config.json")
    args = parser.parse_args()

    config = load_config(args.config)
    db = Database(config.db_path)
    runner = PipelineRunner(config, db)
    static_dir = Path(__file__).resolve().parent / "static"

    runner.start()
    httpd = SchedulerHTTPServer((config.host, config.port), config, db, runner, static_dir)

    def handle_shutdown(signum: int, _frame: object) -> None:
        print(f"\nShutting down ({signum})...")
        runner.shutdown()
        httpd.shutdown()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    mode = "DEV MODE" if config.dev_mode else "IIS PRODUCTION MODE"
    print(f"{config.app_name} listening on http://{config.host}:{config.port} [{mode}]")
    print(f"Database : {config.db_path}")
    for root in config.scripts_roots:
        print(f"Root     : [{root.id}] {root.label} -> {root.path}")

    try:
        httpd.serve_forever()
    finally:
        runner.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
