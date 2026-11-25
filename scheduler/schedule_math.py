"""Plain-English schedule math (no cron syntax required)."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Any

VALID_SCHEDULE_TYPES = {"manual", "daily", "weekdays", "weekly", "monthly", "interval"}
DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def parse_hhmm(value: str | None, default: tuple[int, int] = (7, 0)) -> tuple[int, int]:
    if not value or not isinstance(value, str):
        return default
    parts = value.strip().split(":")
    if len(parts) != 2:
        return default
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return default
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return default


def format_time_12h(hour: int, minute: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {suffix}"


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{['th', 'st', 'nd', 'rd', 'th'][min(n % 10, 4)]}"


def normalize_schedule(schedule_type: str, config: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    stype = (schedule_type or "manual").strip().lower()
    if stype not in VALID_SCHEDULE_TYPES:
        raise ValueError(f"Unsupported schedule type: {stype}")

    cfg = dict(config or {})

    if stype == "manual":
        return "manual", {}

    if stype in {"daily", "weekdays"}:
        h, m = parse_hhmm(cfg.get("time"), (7, 0))
        return stype, {"time": f"{h:02d}:{m:02d}"}

    if stype == "weekly":
        h, m = parse_hhmm(cfg.get("time"), (7, 0))
        raw_days = cfg.get("days")
        if not isinstance(raw_days, list) or not raw_days:
            days = [0]
        else:
            days = sorted({int(d) for d in raw_days if isinstance(d, (int, float, str)) and str(d).isdigit() and 0 <= int(d) <= 6})
            if not days:
                days = [0]
        return "weekly", {"time": f"{h:02d}:{m:02d}", "days": days}

    if stype == "monthly":
        h, m = parse_hhmm(cfg.get("time"), (7, 0))
        day_mode = str(cfg.get("day_mode") or "day_of_month")
        if day_mode not in {"day_of_month", "first_business_day", "last_business_day"}:
            day_mode = "day_of_month"
        out: dict[str, Any] = {"time": f"{h:02d}:{m:02d}", "day_mode": day_mode}
        if day_mode == "day_of_month":
            try:
                day_num = int(cfg.get("day", 1))
            except (TypeError, ValueError):
                day_num = 1
            out["day"] = max(1, min(31, day_num))
        return "monthly", out

    # interval
    try:
        every_hours = int(cfg.get("every_hours", 2))
    except (TypeError, ValueError):
        every_hours = 2
    every_hours = max(1, min(12, every_hours))

    try:
        start_hour = int(cfg.get("start_hour", 7))
    except (TypeError, ValueError):
        start_hour = 7
    try:
        end_hour = int(cfg.get("end_hour", 18))
    except (TypeError, ValueError):
        end_hour = 18

    start_hour = max(0, min(23, start_hour))
    end_hour = max(start_hour, min(23, end_hour))
    try:
        minute = int(cfg.get("minute", 0))
    except (TypeError, ValueError):
        minute = 0
    minute = max(0, min(59, minute))

    weekdays_only = bool(cfg.get("weekdays_only", True))
    return "interval", {
        "every_hours": every_hours,
        "start_hour": start_hour,
        "end_hour": end_hour,
        "minute": minute,
        "weekdays_only": weekdays_only,
    }


def _monthly_target_day(year: int, month: int, cfg: dict[str, Any]) -> int:
    last_day = calendar.monthrange(year, month)[1]
    mode = cfg.get("day_mode", "day_of_month")
    if mode == "first_business_day":
        for d in range(1, last_day + 1):
            if date(year, month, d).weekday() < 5:
                return d
        return 1
    if mode == "last_business_day":
        for d in range(last_day, 0, -1):
            if date(year, month, d).weekday() < 5:
                return d
        return last_day
    requested = int(cfg.get("day", 1))
    return min(max(1, requested), last_day)


def compute_next_run(
    schedule_type: str,
    config: dict[str, Any] | None,
    after: datetime | None = None,
    enabled: bool = True,
) -> datetime | None:
    """Compute the next local naive datetime strictly after `after`."""
    if not enabled:
        return None
    stype, cfg = normalize_schedule(schedule_type, config)
    if stype == "manual":
        return None

    now = (after or datetime.now()).replace(microsecond=0)
    # Always step at least 1 second past `now` so we never immediately re-trigger the same minute
    threshold = now + timedelta(seconds=1)

    if stype == "daily":
        h, m = parse_hhmm(cfg["time"])
        candidate = threshold.replace(hour=h, minute=m, second=0)
        if candidate < threshold:
            candidate += timedelta(days=1)
        return candidate

    if stype == "weekdays":
        h, m = parse_hhmm(cfg["time"])
        candidate = threshold.replace(hour=h, minute=m, second=0)
        if candidate < threshold:
            candidate += timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate

    if stype == "weekly":
        h, m = parse_hhmm(cfg["time"])
        days = set(cfg["days"])
        for offset in range(0, 8):
            d = threshold.date() + timedelta(days=offset)
            if d.weekday() in days:
                candidate = datetime(d.year, d.month, d.day, h, m, 0)
                if candidate >= threshold:
                    return candidate
        return None

    if stype == "monthly":
        h, m = parse_hhmm(cfg["time"])
        year, month = threshold.year, threshold.month
        for _ in range(0, 24):
            day_num = _monthly_target_day(year, month, cfg)
            candidate = datetime(year, month, day_num, h, m, 0)
            if candidate >= threshold:
                return candidate
            if month == 12:
                year += 1
                month = 1
            else:
                month += 1
        return None

    if stype == "interval":
        every = int(cfg["every_hours"])
        start_h = int(cfg["start_hour"])
        end_h = int(cfg["end_hour"])
        minute = int(cfg.get("minute", 0))
        weekdays_only = bool(cfg.get("weekdays_only", True))
        hours = list(range(start_h, end_h + 1, every))
        for offset in range(0, 8):
            d = threshold.date() + timedelta(days=offset)
            if weekdays_only and d.weekday() >= 5:
                continue
            for h in hours:
                candidate = datetime(d.year, d.month, d.day, h, minute, 0)
                if candidate >= threshold:
                    return candidate
        return None

    return None


def describe_schedule(schedule_type: str, config: dict[str, Any] | None) -> str:
    """Return a warm, human-readable description of a schedule."""
    stype, cfg = normalize_schedule(schedule_type, config)
    if stype == "manual":
        return "Manual only"
    if stype == "daily":
        h, m = parse_hhmm(cfg["time"])
        return f"Daily at {format_time_12h(h, m)}"
    if stype == "weekdays":
        h, m = parse_hhmm(cfg["time"])
        return f"Weekdays at {format_time_12h(h, m)}"
    if stype == "weekly":
        h, m = parse_hhmm(cfg["time"])
        days = cfg["days"]
        if days == [0, 1, 2, 3, 4]:
            return f"Weekdays at {format_time_12h(h, m)}"
        if days == [0, 1, 2, 3, 4, 5, 6]:
            return f"Daily at {format_time_12h(h, m)}"
        if len(days) == 1:
            return f"Every {DAY_FULL[days[0]]} at {format_time_12h(h, m)}"
        label = ", ".join(DAY_SHORT[d] for d in days)
        return f"{label} at {format_time_12h(h, m)}"
    if stype == "monthly":
        h, m = parse_hhmm(cfg["time"])
        mode = cfg["day_mode"]
        if mode == "first_business_day":
            return f"1st business day at {format_time_12h(h, m)}"
        if mode == "last_business_day":
            return f"Last business day at {format_time_12h(h, m)}"
        return f"Monthly on the {_ordinal(cfg['day'])} at {format_time_12h(h, m)}"
    if stype == "interval":
        every = cfg["every_hours"]
        unit = "hour" if every == 1 else f"{every} hrs"
        start_s = format_time_12h(cfg["start_hour"], cfg.get("minute", 0))
        end_s = format_time_12h(cfg["end_hour"], cfg.get("minute", 0))
        scope = "weekdays" if cfg["weekdays_only"] else "daily"
        if cfg["start_hour"] == 0 and cfg["end_hour"] == 23:
            return f"Every {unit} ({scope})"
        return f"Every {unit}, {start_s}–{end_s} ({scope})"
    return "Manual only"


def weekly_slots(schedule_type: str, config: dict[str, Any] | None, enabled: bool = True) -> list[dict[str, Any]]:
    """Return weekly timeline slots [{day: 0..6, hour: 0..23, minute: 0..59}] for UI timeline."""
    if not enabled:
        return []
    stype, cfg = normalize_schedule(schedule_type, config)
    slots: list[dict[str, Any]] = []

    if stype == "manual":
        return []
    if stype == "daily":
        h, m = parse_hhmm(cfg["time"])
        return [{"day": d, "hour": h, "minute": m} for d in range(7)]
    if stype == "weekdays":
        h, m = parse_hhmm(cfg["time"])
        return [{"day": d, "hour": h, "minute": m} for d in range(5)]
    if stype == "weekly":
        h, m = parse_hhmm(cfg["time"])
        return [{"day": d, "hour": h, "minute": m} for d in cfg["days"]]
    if stype == "interval":
        every = cfg["every_hours"]
        days = range(5) if cfg["weekdays_only"] else range(7)
        hours = range(cfg["start_hour"], cfg["end_hour"] + 1, every)
        minute = cfg.get("minute", 0)
        for d in days:
            for h in hours:
                slots.append({"day": d, "hour": h, "minute": minute})
        return slots
    if stype == "monthly":
        # Show the upcoming month's day-of-week so monthly jobs are visible on the timeline
        nxt = compute_next_run(stype, cfg, enabled=True)
        if nxt:
            return [{"day": nxt.weekday(), "hour": nxt.hour, "minute": nxt.minute, "monthly": True}]
    return slots
