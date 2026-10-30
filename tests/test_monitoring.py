from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError

from moex_bot.monitoring import disk_status, load_settings, notify, request_probe
from moex_bot.notifications import SQLiteOutbox

ROOT = Path(__file__).resolve().parents[1]


def test_disk_thresholds():
    settings = load_settings(ROOT / "config/monitoring.json")
    assert disk_status(100 * 2**30, 20 * 2**30, settings) == "OK"
    assert disk_status(100 * 2**30, 10 * 2**30, settings).startswith("WARNING")
    assert disk_status(100 * 2**30, 1 * 2**30, settings).startswith("CRITICAL")


def test_incident_debounce_recovery_and_repeat(tmp_path):
    settings = load_settings(ROOT / "config/monitoring.json")
    outbox = SQLiteOutbox(tmp_path / "outbox.db")
    database = tmp_path / "incidents.db"
    now = datetime(2026, 10, 30, tzinfo=UTC)
    notify({"api": "FAIL"}, database, outbox, now, settings)
    assert outbox.health(now=now)["pending_due"] == 0
    notify({"api": "FAIL"}, database, outbox, now + timedelta(minutes=15), settings)
    assert outbox.health(now=now + timedelta(hours=1))["pending_due"] == 1
    notify({"api": "FAIL"}, database, outbox, now + timedelta(minutes=30), settings)
    assert outbox.health(now=now + timedelta(hours=1))["pending_due"] == 1
    notify({"api": "OK"}, database, outbox, now + timedelta(hours=1), settings)
    assert outbox.health(now=now + timedelta(hours=1))["pending_due"] == 2
    notify({"disk": "CRITICAL: full"}, database, outbox, now, settings)
    assert outbox.health(now=now + timedelta(hours=1))["pending_due"] == 3


def test_probe_does_not_leak_telegram_secret(monkeypatch):
    def fail(*args, **kwargs):
        raise HTTPError("https://api.telegram.org/botSECRET/getChat", 401,
                        "secret", {}, None)
    monkeypatch.setattr("moex_bot.monitoring_probes.urlopen", fail)
    result = request_probe("https://api.telegram.org/botSECRET/getChat")
    assert result == "FAIL: HTTP 401"
    assert "SECRET" not in result


def test_collector_failure_is_isolated(tmp_path, monkeypatch):
    from moex_bot import monitoring_probes as probes

    def broken(*args):
        raise OSError("private detail")
    monkeypatch.setattr(probes, "collect_resources", broken)
    for name in ("services", "integrations", "artifacts"):
        monkeypatch.setattr(probes, "collect_" + name, lambda *args: {"survived": "OK"})
    checks = probes.collect(tmp_path, tmp_path, {}, datetime.now(UTC))
    assert checks["survived"] == "OK"
    assert checks["collector:resources"].startswith("FAIL: OSError")
    assert "private detail" not in str(checks)


def test_readonly_probe_does_not_create_missing_database(tmp_path):
    import sqlite3

    import pytest

    from moex_bot.monitoring_probes import read_outbox_health

    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        read_outbox_health(missing, datetime.now(UTC))
    assert not missing.exists()


def test_same_snapshot_does_not_advance_failure_count(tmp_path):
    settings = load_settings(ROOT / "config/monitoring.json")
    outbox = SQLiteOutbox(tmp_path / "outbox.db")
    now = datetime.now(UTC)
    for _ in range(3):
        notify({"api": "FAIL"}, tmp_path / "incident.db", outbox, now, settings)
    assert outbox.health(now=now)["pending_due"] == 0


def test_collect_only_snapshot_without_incident_writes(tmp_path, monkeypatch):
    import json
    import sys

    from moex_bot import monitoring

    monkeypatch.setattr(sys, "argv", ["monitoring", "--app", str(ROOT),
                                     "--state", str(tmp_path), "--collect-only"])
    monkeypatch.setattr(monitoring, "collect", lambda *args: {"sample": "OK"})
    assert monitoring.main() == 0
    snapshot = json.loads((tmp_path / "artifacts/monitoring-latest.json").read_text())
    assert snapshot["schema_version"] == 1
    assert snapshot["checks"] == {"sample": "OK"}
    assert not (tmp_path / "data").exists()
    assert not list((tmp_path / "artifacts").glob("*.tmp"))


def test_crash_after_enqueue_does_not_duplicate_incident(tmp_path, monkeypatch):
    import pytest

    settings = {**load_settings(ROOT / "config/monitoring.json"), "failures_to_alert": 1}
    outbox = SQLiteOutbox(tmp_path / "outbox.db")
    original = outbox.enqueue

    def crash(**kwargs):
        original(**kwargs)
        raise RuntimeError("simulated crash before ledger commit")
    monkeypatch.setattr(outbox, "enqueue", crash)
    now = datetime.now(UTC)
    with pytest.raises(RuntimeError):
        notify({"api": "FAIL"}, tmp_path / "incident.db", outbox, now, settings)
    monkeypatch.setattr(outbox, "enqueue", original)
    notify({"api": "FAIL"}, tmp_path / "incident.db", outbox,
           now + timedelta(minutes=15), settings)
    assert outbox.health(now=now + timedelta(hours=1))["pending_due"] == 1
