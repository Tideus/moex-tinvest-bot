"""Separate completeness/freshness of the non-trading research night report."""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

UNIT = "moex-research-forward-report"
KEY = "data:research-report"
DETAIL_PATTERN = (r"day=(\d{4}-\d{2}-\d{2}) gap_empty=(\d{1,6}) open_empty=(\d{1,6}) "
                  r"request_errors=(\d{1,6}) unclassified=(\d{1,6}) missed_slots=(\d{1,6})")


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
        # Counts explain a frozen failure; no category turns an incomplete report green.
        detail = show(UNIT + ".service", "StatusText")
        match = re.fullmatch("RESEARCH_REPORT_V1 " + DETAIL_PATTERN, detail)
        base = "FAIL: research report incomplete evidence"
        if match:
            local = now.astimezone(ZoneInfo("Europe/Moscow"))
            due = (local - timedelta(minutes=12)).date() - timedelta(days=1)
            if match[1] == due.isoformat():
                return base + "; " + detail.removeprefix("RESEARCH_REPORT_V1 ")
        return base
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
