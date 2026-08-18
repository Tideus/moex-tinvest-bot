from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .service_config import TInvestEnvironment

_DURATION = re.compile(r"^[1-9][0-9]*(?:s|min|h)$")
_DEFAULT_SHADOW_CALENDAR = "*-*-* *:05:00"
_DEFAULT_DAILY_REPORT_CALENDAR = "*-*-* 23:20:00"


@dataclass(frozen=True, slots=True)
class RuntimeSchedule:
    timezone: str = "Europe/Moscow"
    shadow_on_calendar: str = _DEFAULT_SHADOW_CALENDAR
    shadow_randomized_delay_seconds: int = 20
    daily_report_on_calendar: str = _DEFAULT_DAILY_REPORT_CALENDAR
    health_on_boot: str = "10min"
    health_interval: str = "15min"
    diagnostics_interval_seconds: int = 60


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    environment: TInvestEnvironment
    schedule: RuntimeSchedule
    sandbox_orders_enabled: bool = False
    sandbox_max_orders_per_cycle: int = 3


def load_runtime_config(path: Path) -> RuntimeConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("runtime configuration must be a JSON object")
    environment = TInvestEnvironment(str(raw["t_invest_environment"]))
    schedule_raw = raw.get("schedule", {})
    if not isinstance(schedule_raw, dict):
        raise ValueError("runtime schedule must be a JSON object")
    schedule = RuntimeSchedule(
        timezone=str(schedule_raw.get("timezone", "Europe/Moscow")),
        shadow_on_calendar=str(
            schedule_raw.get("shadow_on_calendar", _DEFAULT_SHADOW_CALENDAR)
        ),
        shadow_randomized_delay_seconds=_integer(
            schedule_raw.get("shadow_randomized_delay_seconds", 20),
            "shadow_randomized_delay_seconds",
        ),
        daily_report_on_calendar=str(
            schedule_raw.get("daily_report_on_calendar", _DEFAULT_DAILY_REPORT_CALENDAR)
        ),
        health_on_boot=str(schedule_raw.get("health_on_boot", "10min")),
        health_interval=str(schedule_raw.get("health_interval", "15min")),
        diagnostics_interval_seconds=_integer(
            schedule_raw.get("diagnostics_interval_seconds", 60),
            "diagnostics_interval_seconds",
        ),
    )
    _validate_schedule(schedule)
    sandbox_orders_enabled = raw.get("sandbox_orders_enabled", False)
    if not isinstance(sandbox_orders_enabled, bool):
        raise ValueError("sandbox_orders_enabled must be a boolean")
    sandbox_max_orders = _integer(
        raw.get("sandbox_max_orders_per_cycle", 3), "sandbox_max_orders_per_cycle"
    )
    if not 1 <= sandbox_max_orders <= 10:
        raise ValueError("sandbox_max_orders_per_cycle must be between 1 and 10")
    if sandbox_orders_enabled and environment is not TInvestEnvironment.SANDBOX:
        raise ValueError("sandbox orders require t_invest_environment=sandbox")
    return RuntimeConfig(
        environment=environment,
        schedule=schedule,
        sandbox_orders_enabled=sandbox_orders_enabled,
        sandbox_max_orders_per_cycle=sandbox_max_orders,
    )


def set_runtime_environment(path: Path, environment: TInvestEnvironment) -> None:
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("runtime configuration must be a JSON object")
        raw = loaded
    raw["t_invest_environment"] = environment.value
    if environment is TInvestEnvironment.PROD:
        raw["sandbox_orders_enabled"] = False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    materialize_runtime_defaults(path)


def set_sandbox_orders_enabled(path: Path, enabled: bool) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("runtime configuration must be a JSON object")
    if enabled and raw.get("t_invest_environment") != "sandbox":
        raise ValueError("sandbox orders require t_invest_environment=sandbox")
    raw["sandbox_orders_enabled"] = enabled
    _atomic_write(path, json.dumps(raw, indent=2) + "\n")
    load_runtime_config(path)


def materialize_runtime_defaults(path: Path) -> bool:
    """Add missing documented fields without replacing operator-owned values."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("runtime configuration must be a JSON object")
    config = load_runtime_config(path)
    schedule = raw.setdefault("schedule", {})
    if not isinstance(schedule, dict):
        raise ValueError("runtime schedule must be a JSON object")
    defaults: dict[str, object] = {
        "timezone": config.schedule.timezone,
        "shadow_on_calendar": config.schedule.shadow_on_calendar,
        "shadow_randomized_delay_seconds": config.schedule.shadow_randomized_delay_seconds,
        "daily_report_on_calendar": config.schedule.daily_report_on_calendar,
        "health_on_boot": config.schedule.health_on_boot,
        "health_interval": config.schedule.health_interval,
        "diagnostics_interval_seconds": config.schedule.diagnostics_interval_seconds,
    }
    changed = False
    for name, top_level_value in (
        ("sandbox_orders_enabled", config.sandbox_orders_enabled),
        ("sandbox_max_orders_per_cycle", config.sandbox_max_orders_per_cycle),
    ):
        if name not in raw:
            raw[name] = top_level_value
            changed = True
    for name, default_value in defaults.items():
        if name not in schedule:
            schedule[name] = default_value
            changed = True
    if changed:
        _atomic_write(path, json.dumps(raw, indent=2) + "\n")
    return changed


def render_systemd_timer_overrides(
    config: RuntimeConfig, output_dir: Path
) -> tuple[Path, Path, Path]:
    shadow = output_dir / "moex-tinvest-shadow.timer.d" / "runtime.conf"
    health = output_dir / "moex-tinvest-health.timer.d" / "runtime.conf"
    daily = output_dir / "moex-tinvest-daily-report.timer.d" / "runtime.conf"
    calendar = f"{config.schedule.shadow_on_calendar} {config.schedule.timezone}"
    _atomic_write(
        shadow,
        "[Timer]\n"
        "OnCalendar=\n"
        f"OnCalendar={calendar}\n"
        f"RandomizedDelaySec={config.schedule.shadow_randomized_delay_seconds}s\n",
    )
    _atomic_write(
        health,
        "[Timer]\n"
        "OnBootSec=\n"
        f"OnBootSec={config.schedule.health_on_boot}\n"
        "OnUnitActiveSec=\n"
        f"OnUnitActiveSec={config.schedule.health_interval}\n",
    )
    daily_calendar = f"{config.schedule.daily_report_on_calendar} {config.schedule.timezone}"
    _atomic_write(
        daily,
        "[Timer]\n"
        "OnCalendar=\n"
        f"OnCalendar={daily_calendar}\n"
        f"RandomizedDelaySec={config.schedule.shadow_randomized_delay_seconds}s\n",
    )
    return shadow, health, daily


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _validate_schedule(schedule: RuntimeSchedule) -> None:
    try:
        ZoneInfo(schedule.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("schedule timezone must be a valid IANA timezone") from exc
    if (
        not schedule.shadow_on_calendar.strip()
        or "\n" in schedule.shadow_on_calendar
        or "\r" in schedule.shadow_on_calendar
        or len(schedule.shadow_on_calendar) > 80
    ):
        raise ValueError("shadow_on_calendar must be a single systemd calendar expression")
    if (
        not schedule.daily_report_on_calendar.strip()
        or "\n" in schedule.daily_report_on_calendar
        or "\r" in schedule.daily_report_on_calendar
        or len(schedule.daily_report_on_calendar) > 80
    ):
        raise ValueError("daily_report_on_calendar must be a single systemd calendar expression")
    for label, value in (
        ("health_on_boot", schedule.health_on_boot),
        ("health_interval", schedule.health_interval),
    ):
        if not _DURATION.fullmatch(value):
            raise ValueError(f"{label} must look like 30s, 15min or 1h")
    if not 0 <= schedule.shadow_randomized_delay_seconds <= 3600:
        raise ValueError("shadow_randomized_delay_seconds must be between 0 and 3600")
    if not 10 <= schedule.diagnostics_interval_seconds <= 86400:
        raise ValueError("diagnostics_interval_seconds must be between 10 and 86400")


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
