from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    timezone: str
    long_morning_analysis_hour: int
    long_evening_report_enabled: bool
    intraday_notify_filled_operations: bool
    intraday_evening_report_enabled: bool
    persist_every_cycle: bool
    include_config_snapshot: bool
    include_market_inputs: bool
    include_portfolio_input: bool
    include_decisions_and_rejections: bool


def load_notification_policy(path: Path) -> NotificationPolicy:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("notification policy must be an object")
    long = _object(raw.get("long"), "long")
    intraday = _object(raw.get("intraday"), "intraday")
    audit = _object(raw.get("audit"), "audit")
    timezone = str(raw.get("timezone", ""))
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("notification timezone must be a valid IANA timezone") from exc
    hour = _integer(long.get("morning_analysis_hour"), "morning_analysis_hour")
    if not 0 <= hour <= 23:
        raise ValueError("morning_analysis_hour must be between 0 and 23")
    policy = NotificationPolicy(
        timezone=timezone,
        long_morning_analysis_hour=hour,
        long_evening_report_enabled=_boolean(
            long.get("evening_report_enabled"), "long.evening_report_enabled"
        ),
        intraday_notify_filled_operations=_boolean(
            intraday.get("notify_filled_operations"),
            "intraday.notify_filled_operations",
        ),
        intraday_evening_report_enabled=_boolean(
            intraday.get("evening_report_enabled"),
            "intraday.evening_report_enabled",
        ),
        persist_every_cycle=_boolean(
            audit.get("persist_every_cycle"), "audit.persist_every_cycle"
        ),
        include_config_snapshot=_boolean(
            audit.get("include_config_snapshot"), "audit.include_config_snapshot"
        ),
        include_market_inputs=_boolean(
            audit.get("include_market_inputs"), "audit.include_market_inputs"
        ),
        include_portfolio_input=_boolean(
            audit.get("include_portfolio_input"), "audit.include_portfolio_input"
        ),
        include_decisions_and_rejections=_boolean(
            audit.get("include_decisions_and_rejections"),
            "audit.include_decisions_and_rejections",
        ),
    )
    if not all(
        (
            policy.persist_every_cycle,
            policy.include_config_snapshot,
            policy.include_market_inputs,
            policy.include_portfolio_input,
            policy.include_decisions_and_rejections,
        )
    ):
        raise ValueError("all audit evidence fields are mandatory in this build")
    return policy


def should_send_long_morning(policy: NotificationPolicy, moment: datetime) -> bool:
    if moment.tzinfo is None:
        raise ValueError("notification moment must be timezone-aware")
    return (
        moment.astimezone(ZoneInfo(policy.timezone)).hour
        == policy.long_morning_analysis_hour
    )


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"notification {label} must be an object")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"notification {label} must be a boolean")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"notification {label} must be an integer")
    return value
