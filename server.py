"""Entry point for Analytics Scheduler (zero third-party pip packages required).

Run locally:
    python server.py

Optional custom config path:
    python server.py --config C:\\AnalyticsScheduler\\config.json
"""

from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import signal
import sys
import threading
from pathlib import Path

from scheduler.api import SchedulerHTTPServer
from scheduler.config import load_config
from scheduler.db import Database
from scheduler.runner import PipelineRunner

logger = logging.getLogger("scheduler.server")


def _configure_logging(logs_dir: Path) -> None:
    """Configure rotating file logging (5 MB x 5 backups) plus console output."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "service.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    file_handler = RotatingFileHandler(
        filename=str(log_file),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analytics Scheduler — lightweight IIS Python scheduler")
    parser.add_argument("--config", default=None, help="Path to config.json")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except Exception:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        logger.exception("Failed to load scheduler configuration.")
        raise

    _configure_logging(config.logs_dir)

    runner: PipelineRunner | None = None
    httpd: SchedulerHTTPServer | None = None

    try:
        db = Database(config.db_path)
        runner = PipelineRunner(config, db)
        static_dir = Path(__file__).resolve().parent / "static"

        runner.start()
        httpd = SchedulerHTTPServer((config.host, config.port), config, db, runner, static_dir)

        shutdown_triggered = threading.Event()

        def _shutdown_async(signum: int) -> None:
            logger.info("Shutdown signal received (%s); stopping HTTP server...", signum)
            assert httpd is not None
            httpd.shutdown()

        def handle_shutdown(signum: int, _frame: object) -> None:
            if shutdown_triggered.is_set():
                return
            shutdown_triggered.set()
            # Run httpd.shutdown() on a daemon thread to avoid main-thread deadlock
            # while serve_forever() is executing on the main thread.
            threading.Thread(
                target=_shutdown_async,
                args=(signum,),
                name="shutdown-trigger",
                daemon=True,
            ).start()

        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, handle_shutdown)

        mode = "DEV MODE" if config.dev_mode else "IIS PRODUCTION MODE"
        logger.info("%s listening on http://%s:%s [%s]", config.app_name, config.host, config.port, mode)
        logger.info("Database : %s", config.db_path)
        logger.info("Log file : %s", config.logs_dir / "service.log")
        for root in config.scripts_roots:
            logger.info("Root     : [%s] %s -> %s", root.id, root.label, root.path)

        httpd.serve_forever()
    except Exception:
        logger.exception("Fatal error in Analytics Scheduler server.")
        raise
    finally:
        logger.info("Cleaning up runner and HTTP server...")
        if runner is not None:
            try:
                runner.shutdown()
            except Exception:
                logger.exception("Error while shutting down pipeline runner.")
        if httpd is not None:
            try:
                httpd.server_close()
            except Exception:
                logger.exception("Error while closing HTTP server.")
        logger.info("Analytics Scheduler stopped.")


if __name__ == "__main__":
    main()
