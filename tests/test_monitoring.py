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
    monkeypatch.setattr("moex_bot.monitoring.urlopen", fail)
    result = request_probe("https://api.telegram.org/botSECRET/getChat")
    assert result == "FAIL: HTTP 401"
    assert "SECRET" not in result
