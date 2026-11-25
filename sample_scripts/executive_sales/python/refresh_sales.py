"""Sample analytics pipeline: simulates a Snowflake refresh writing dashboard JSON."""

import json
import time
from datetime import datetime
from pathlib import Path


def main() -> None:
    print("Connecting to Snowflake using configured keypair (.env)...")
    time.sleep(1.0)
    print("Running query: PROD.ANALYTICS.V_EXEC_SALES_DAILY (last 90 days)...")
    time.sleep(1.2)
    print("Retrieved 42,180 rows across 6 regions.")
    time.sleep(0.8)

    reports_dir = Path(__file__).resolve().parent.parent / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out_file = reports_dir / "sales_summary.json"
    out_file.write_text(
        json.dumps(
            {
                "refreshed_at": datetime.now().isoformat(timespec="seconds"),
                "rows_processed": 42180,
                "regions": 6,
                "status": "ok",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote dashboard payload -> {out_file}")
    print("Done.")


if __name__ == "__main__":
    main()
