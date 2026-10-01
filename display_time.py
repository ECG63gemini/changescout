import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("changescout")


def format_display_time(value: str | datetime) -> str:
    name = os.getenv("DISPLAY_TIMEZONE", "").strip()
    try:
        display_zone = ZoneInfo(name) if name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Invalid DISPLAY_TIMEZONE %r; using UTC.", name)
        display_zone = timezone.utc

    timestamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    local = timestamp.astimezone(display_zone)
    clock = local.strftime("%I:%M %p %Z").lstrip("0")
    return f"{local.strftime('%b')} {local.day}, {local.year} at {clock}"
