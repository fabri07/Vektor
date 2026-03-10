"""Date and time utility helpers."""

from datetime import UTC, date, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def start_of_month(dt: date) -> date:
    return dt.replace(day=1)


def end_of_month(dt: date) -> date:
    next_month = (dt.replace(day=1) + timedelta(days=32)).replace(day=1)
    return next_month - timedelta(days=1)


def start_of_week(dt: date) -> date:
    return dt - timedelta(days=dt.weekday())


def date_range(start: date, end: date) -> list[date]:
    result = []
    current = start
    while current <= end:
        result.append(current)
        current += timedelta(days=1)
    return result


def days_between(start: date, end: date) -> int:
    return max(0, (end - start).days)
