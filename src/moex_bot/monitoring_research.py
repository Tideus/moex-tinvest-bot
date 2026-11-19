"""Separate completeness/freshness of the non-trading research night report."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

UNIT = "moex-research-forward-report"
KEY = "data:research-report"


def report_check(show: Callable[[str, str], str], now: datetime, uptime: float) -> str:
    if show(UNIT + ".timer", "ActiveState") != "active":
        return "FAIL: research report timer inactive"
    result = show(UNIT + ".service", "Result")
    if result == "timeout":
        return "FAIL: research report timeout"
    if result != "success":
        return "FAIL: research report generation failed"
    status = show(UNIT + ".service", "ExecMainStatus")
    if status == "3":
        return "FAIL: research report incomplete evidence"
    if status != "0":
        return "FAIL: research report generation failed"
    try:
        stamp = int(show(UNIT + ".service", "ExecMainExitTimestampMonotonic")) / 1_000_000
    except ValueError:
        return "FAIL: research report completion unknown"
    if stamp <= 0 or uptime < stamp:
        return "FAIL: research report completion unknown"
    local = now.astimezone(ZoneInfo("Europe/Moscow"))
    # Allow the 23:57 run 15 minutes to finish. This is generator freshness,
    # not an assumption that every calendar day is an exchange trading day.
    due_date = (local - timedelta(minutes=12)).date() - timedelta(days=1)
    due = datetime.combine(due_date, time(23, 57), local.tzinfo)
    finished = local - timedelta(seconds=uptime - stamp)
    if finished < due:
        return "FAIL: research report stale"
    return "OK"
