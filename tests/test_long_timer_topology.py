import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from moex_bot import monitoring_long_once as once
from moex_bot import monitoring_probes as probes
from moex_bot.long_auto_once import init
from moex_bot.monitoring_labels import safe_reason

NOW = datetime(2026, 11, 2, 10, tzinfo=UTC)  # Monday 13:00 MSK


@pytest.fixture
def enrolled(tmp_path, monkeypatch):
    marker, anchor = tmp_path / "owner", tmp_path / "control/entry.claim"
    monkeypatch.setattr(once, "MARKER", marker)
    monkeypatch.setattr(once, "ANCHOR", anchor)
    monkeypatch.setenv("T_INVEST_SANDBOX_LONG_ACCOUNT_ID", "only-long")
    assert not once.enrolled(tmp_path)
    init(tmp_path / "long-once/sandbox.sqlite3", "only-long", anchor=anchor)
    assert once.enrolled(tmp_path)  # An orphan ledger must not select legacy.
    marker.write_bytes(b"sandbox-long-once/v1\n")
    os.utime(marker, (NOW.timestamp(), NOW.timestamp()))
    return tmp_path


def test_enrollment_grace_weekdays_and_no_writes(enrolled):
    assert once.evidence(enrolled, NOW) == {"data:long": "OK"}
    assert once.evidence(enrolled, NOW + timedelta(hours=21, minutes=20)) == {"data:long": "OK"}
    # Next Tuesday at 12:20 Moscow: the grace period is over.
    assert "missing" in once.evidence(enrolled, NOW + timedelta(hours=23, minutes=20))["data:long"]
    assert once.evidence(enrolled, NOW - timedelta(hours=4)) == {"data:long": "OK"}
    ledger = once.ReadOnlyLedger(
        enrolled / "long-once/sandbox.sqlite3", "only-long", anchor=once.ANCHOR
    )
    import sqlite3

    with ledger.connect() as db, pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute("delete from metadata")


@pytest.mark.parametrize(
    "case",
    ["valid", "blocked", "old", "future", "broken", "orphan", "spent", "unknown", "wrong-account"],
)
def test_native_plan_health(enrolled, monkeypatch, case):
    os.utime(once.MARKER, (1, 1))
    plan = enrolled / "long-once/run-20261102T070501Z.12345678/plan.json"
    plan.parent.mkdir()
    at = {"old": "2026-10-30", "future": "2026-11-03"}.get(case, "2026-11-02")
    plan.write_text(
        json.dumps(
            dict(run_id=f"shadow-{at}T07:05:10+00:00", quality=dict(passed=case != "blocked"))
        )
    )
    if case == "broken":
        plan.write_text("{")
    if case == "orphan":
        once.MARKER.write_text("wrong")
    if case == "wrong-account":
        monkeypatch.setenv("T_INVEST_SANDBOX_LONG_ACCOUNT_ID", "intraday")
    if case in ("spent", "unknown"):
        monkeypatch.setattr(
            once.ReadOnlyLedger,
            "status",
            lambda s: "FILLED_PROTECTED" if case == "spent" else "QUARANTINED",
        )
    checks = once.evidence(enrolled, NOW)
    if case == "valid":
        assert checks == {"data:long": "OK", "quality:long": "OK"}
    elif case == "spent":
        assert checks == {"data:long": "OK"}
    elif case == "blocked":
        assert checks["quality:long"].startswith("FAIL")
    else:
        assert checks["data:long"].startswith("FAIL")
        assert "LONG" in safe_reason(checks["data:long"])


@pytest.mark.parametrize("legacy", ["inactive", "active"])
def test_monitor_only_expects_enrolled_writer(enrolled, monkeypatch, legacy):
    def systemd(unit, field):
        if unit == "moex-tinvest-shadow.timer":
            return {"ActiveState": legacy, "UnitFileState": "disabled"}[field]
        return {"ActiveState": "active", "UnitFileState": "enabled", "Result": "success"}.get(
            field, "123"
        )

    monkeypatch.setattr(probes, "systemd", systemd)
    checks = probes.collect_services(enrolled, enrolled, {"calendar_required": False}, NOW)
    assert "timer:shadow" not in checks and checks["timer:long-once-sandbox"] == "OK"
    assert (checks["timer:legacy-long-excluded"] == "OK") == (legacy == "inactive")
    settings = dict(
        intraday_max_age_seconds=1200, long_max_age_seconds=7200, backup_max_age_hours=192
    )
    data = probes.collect_artifacts(enrolled, enrolled, settings, NOW)
    assert data["data:long"] == "OK" and data["data:intraday"].startswith("FAIL")


def test_start_does_not_resume_or_force_the_one_shot():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts/ubuntu/moex-botctl.sh").read_text(encoding="utf-8")
    block = script.split("start_bot() {", 1)[1].split("timers_are_stopped()", 1)[0]
    assert "local -a long_to_enable=()" in block
    assert 'else\n    long_to_enable=("${selected_long}")' in block
    assert 'systemctl enable --now "${long_to_enable[@]}"' in block
    assert "start moex-tinvest-long-once-sandbox.service" not in block


def test_inaccessible_prod_settings_does_not_hide_other_timers(enrolled, monkeypatch):
    original = Path.exists

    def exists(path):
        if str(path).replace("\\", "/") == "/etc/moex-tinvest-bot/prod-long-once.json":
            raise PermissionError("inaccessible")
        return original(path)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(probes, "systemd", lambda *args: "inactive")
    checks = probes.collect_services(enrolled, enrolled, {"calendar_required": False}, NOW)
    assert checks["timer:long-once-prod"].startswith("FAIL")
    assert "timer:long-once-sandbox" in checks and "timer:intraday" in checks
