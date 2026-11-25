"""Sample pipeline that demonstrates clean error capture + 'AI Fix' copy."""

import time


def main() -> None:
    print("Loading Snowflake connection profile...")
    time.sleep(0.8)
    print("Querying DEMO.ANALYTICS.REVENUE_FORECAST for next 4 months...")
    time.sleep(0.8)
    # Example of secret scrubbing: even if a token leaks, the UI redacts it automatically
    print("Debug hint: token=demo_secret_12345 (automatically redacted in UI logs)")
    raise RuntimeError(
        "SnowflakeProgrammingError: 000904 (42000): invalid identifier 'FORECAST_BUCKET_DT' in DEMO.ANALYTICS.REVENUE_FORECAST"
    )


if __name__ == "__main__":
    main()
