from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")


def next_hourly_run(now: datetime, minute: int = 5) -> datetime:
    """Return the next HH:minute boundary in Europe/Moscow without sleeping."""
    if not 0 <= minute <= 59:
        raise ValueError("minute must be in [0, 59]")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(MOSCOW)
    candidate = local.replace(minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(hours=1)
    return candidate


def is_conservative_stock_window(now: datetime) -> bool:
    """Weekday-only window inside published MOEX equity sessions."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(MOSCOW)
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return 7 * 60 + 5 <= minutes <= 23 * 60 + 5
