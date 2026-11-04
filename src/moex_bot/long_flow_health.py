"""Distinguish a persisted, safe LONG skip from an unresolved feed incident."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
UNCONFIRMED = "FAIL: LONG flow recovery not confirmed"


def missing_only(passed: object, errors: object, orders: object, targets: object) -> set[str]:
    if (passed is not False or not isinstance(errors, (tuple, list)) or not errors
            or orders not in ([], ()) or targets not in ([], ())):
        return set()
    symbols = set()
    for error in errors:
        match = (isinstance(error, str) and
                 re.fullmatch(r"missing long flow for ([A-Z0-9]{1,12})", error))
        if not match:
            return set()
        symbols.add(match[1])
    return symbols


def first_receipt(artifacts: Path, decision: datetime, now: datetime,
                  symbols: set[str]) -> datetime | None:
    """Read fresh, actually received API bars, never cached history or reconstructed signals."""
    day = decision.astimezone(MOSCOW).date()
    for path in sorted(artifacts.glob(f"intraday-plan-{day:%Y%m%d}T*.json")):
        try:
            data = json.loads(path.read_bytes())["analysis_input"]
            at = datetime.fromisoformat(data["as_of"])
            received = datetime.fromisoformat(data["market_data_received_at"])
            if not (decision < at <= received <= now and received.astimezone(MOSCOW).date() == day):
                continue
            seen = set()
            invalid = {row["secid"] for row in data.get("invalid_bars", [])}
            for row in data["supercandles"]:
                stamp = datetime.fromisoformat(row["observed_at"])
                if (row["secid"] in symbols - invalid and decision < stamp <= at
                        and received - stamp <= timedelta(minutes=15)):
                    seen.add(row["secid"])
            if seen == symbols:
                return received
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue  # A malformed receipt is not recovery evidence.
    return None
