import logging
import os
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("changescout")


def display_zone() -> tzinfo:
    name = os.getenv("DISPLAY_TIMEZONE", "").strip()
    try:
        return ZoneInfo(name) if name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Invalid DISPLAY_TIMEZONE %r; using UTC.", name)
        return timezone.utc


def _aware(value: str | datetime) -> datetime:
    timestamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp


def format_display_time(value: str | datetime) -> str:
    local = _aware(value).astimezone(display_zone())
    clock = local.strftime("%I:%M %p %Z").lstrip("0")
    return f"{local.strftime('%b')} {local.day}, {local.year} at {clock}"


def format_compact_time(
    value: str | datetime,
    *,
    now: datetime | None = None,
) -> str:
    """Dense-list format such as "Oct 1, 8:52 PM"; adds the year when it differs."""
    zone = display_zone()
    local = _aware(value).astimezone(zone)
    current = _aware(now or datetime.now(timezone.utc)).astimezone(zone)
    clock = local.strftime("%I:%M %p").lstrip("0")
    day = f"{local.strftime('%b')} {local.day}"
    if local.year != current.year:
        day = f"{day}, {local.year}"
    return f"{day}, {clock}"


def machine_time(value: str | datetime) -> str:
    """UTC timestamp for <time datetime="..."> attributes."""
    return _aware(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_ZONE_LABELS = {
    "America/New_York": "Eastern Time",
    "America/Chicago": "Central Time",
    "America/Denver": "Mountain Time",
    "America/Phoenix": "Arizona Time",
    "America/Los_Angeles": "Pacific Time",
    "America/Anchorage": "Alaska Time",
    "Pacific/Honolulu": "Hawaii Time",
    "US/Eastern": "Eastern Time",
    "US/Central": "Central Time",
    "US/Mountain": "Mountain Time",
    "US/Arizona": "Arizona Time",
    "US/Pacific": "Pacific Time",
    "US/Alaska": "Alaska Time",
    "US/Hawaii": "Hawaii Time",
}


def display_timezone_name() -> str:
    """Plain-language name of the display timezone, such as "Eastern Time".

    Other region/city zones read as "London time"; anything else falls back to
    the zone's abbreviation, such as "UTC".
    """
    zone = display_zone()
    key = str(getattr(zone, "key", "") or "")
    if key in _ZONE_LABELS:
        return _ZONE_LABELS[key]
    if "/" in key and not key.startswith("Etc/"):
        return f"{key.rsplit('/', 1)[-1].replace('_', ' ')} time"
    abbreviation = datetime.now(zone).tzname() or "UTC"
    return f"UTC{abbreviation}" if abbreviation[0] in "+-" else abbreviation
