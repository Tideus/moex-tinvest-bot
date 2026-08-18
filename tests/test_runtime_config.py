import json
from pathlib import Path

import pytest

from moex_bot.runtime_config import (
    load_runtime_config,
    materialize_runtime_defaults,
    render_systemd_timer_overrides,
    set_runtime_environment,
)
from moex_bot.service_config import TInvestEnvironment


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_runtime_schedule_loads_and_renders_safe_dropins(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    _write(
        runtime,
        {
            "t_invest_environment": "sandbox",
            "schedule": {
                "timezone": "Europe/Moscow",
                "shadow_on_calendar": "Mon..Fri *-*-* *:10:00",
                "shadow_randomized_delay_seconds": 30,
                "daily_report_on_calendar": "Mon..Fri *-*-* 23:30:00",
                "health_on_boot": "5min",
                "health_interval": "20min",
                "diagnostics_interval_seconds": 120,
            },
        },
    )
    config = load_runtime_config(runtime)
    shadow, health, daily = render_systemd_timer_overrides(config, tmp_path / "systemd")
    assert "OnCalendar=Mon..Fri *-*-* *:10:00 Europe/Moscow" in shadow.read_text()
    assert "RandomizedDelaySec=30s" in shadow.read_text()
    assert "OnUnitActiveSec=20min" in health.read_text()
    assert "OnCalendar=Mon..Fri *-*-* 23:30:00 Europe/Moscow" in daily.read_text()


def test_runtime_rejects_multiline_calendar_injection(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    _write(
        runtime,
        {
            "t_invest_environment": "sandbox",
            "schedule": {"shadow_on_calendar": "hourly\nUnit=bad.service"},
        },
    )
    with pytest.raises(ValueError, match="single systemd calendar"):
        load_runtime_config(runtime)


def test_environment_switch_preserves_schedule(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    _write(
        runtime,
        {
            "t_invest_environment": "sandbox",
            "schedule": {"health_interval": "30min"},
        },
    )
    set_runtime_environment(runtime, TInvestEnvironment.PROD)
    raw = json.loads(runtime.read_text(encoding="utf-8"))
    assert raw["t_invest_environment"] == "prod"
    assert raw["schedule"]["health_interval"] == "30min"


def test_runtime_migration_completes_minimal_file_without_replacing_values(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime.json"
    _write(
        runtime,
        {
            "t_invest_environment": "sandbox",
            "schedule": {"health_interval": "30min"},
            "future_extension": {"keep": True},
        },
    )
    assert materialize_runtime_defaults(runtime)
    raw = json.loads(runtime.read_text(encoding="utf-8"))
    assert raw["t_invest_environment"] == "sandbox"
    assert raw["schedule"]["health_interval"] == "30min"
    assert raw["schedule"]["timezone"] == "Europe/Moscow"
    assert raw["schedule"]["diagnostics_interval_seconds"] == 60
    assert raw["schedule"]["daily_report_on_calendar"] == "*-*-* 23:20:00"
    assert raw["future_extension"] == {"keep": True}
    assert not materialize_runtime_defaults(runtime)
