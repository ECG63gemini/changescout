from datetime import datetime, timedelta, timezone

FREQUENCY_INTERVALS = {
    "hourly": timedelta(hours=1),
    "every_6_hours": timedelta(hours=6),
    "daily": timedelta(days=1),
}

FREQUENCY_LABELS = {
    "hourly": "Hourly",
    "every_6_hours": "Every 6 hours",
    "daily": "Daily",
}


def validate_frequency(value: str) -> str:
    if value not in FREQUENCY_INTERVALS:
        raise ValueError("Choose hourly, every 6 hours, or daily.")
    return value


def _as_utc(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def next_check_at(
    last_checked_at: datetime | str | None,
    frequency: str,
) -> datetime | None:
    validate_frequency(frequency)
    if last_checked_at is None:
        return None
    return _as_utc(last_checked_at) + FREQUENCY_INTERVALS[frequency]


def target_is_due(
    *,
    enabled: bool,
    frequency: str,
    last_checked_at: datetime | str | None,
    now: datetime | None = None,
) -> bool:
    if not enabled:
        return False
    due_at = next_check_at(last_checked_at, frequency)
    if due_at is None:
        return True
    current = _as_utc(now or datetime.now(timezone.utc))
    return current >= due_at
