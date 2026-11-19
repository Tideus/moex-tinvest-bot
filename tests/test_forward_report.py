import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from forward_report import generate, snapshot_store_type

from moex_bot.monitoring_labels import (
    check_action,
    check_description,
    safe_failure_details,
    safe_reason,
)
from moex_bot.monitoring_research import KEY, UNIT, report_check


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class FrozenStore:
    """Small frozen-store contract fixture; production delegates original methods."""
    scans = 0

    def __init__(self, path):
        raise AssertionError("report must never create/migrate a store")

    def connect(self, *, readonly=False):
        return sqlite3.connect(self.path, timeout=1)

    def verify(self):
        FrozenStore.scans += 1
        prev, count = "0" * 64, 0
        with self.connect(readonly=True) as db:
            for key, value, digest in db.execute(
                "SELECT key,value,digest FROM events ORDER BY seq"
            ):
                assert digest == sha([prev, key, value]), "research_hash_chain_broken"
                prev, count = digest, count + 1
        return {"head": prev, "events": count}

    def get(self, key):
        with self.connect(readonly=True) as db:
            return db.execute("SELECT value FROM events WHERE key=?", (key,)).fetchone()

    def append(self, kind, key, day, payload, *, now=None):
        with self.connect() as db:
            prev = db.execute("SELECT digest FROM events ORDER BY seq DESC LIMIT 1").fetchone()[0]
            raw = json.dumps(payload)
            db.execute("INSERT INTO events(key,value,digest) VALUES(?,?,?)",
                       (key, raw, sha([prev, key, raw])))


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "evidence.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE events("
                   "seq INTEGER PRIMARY KEY, key TEXT, value TEXT, digest TEXT)")
        db.execute("INSERT INTO events VALUES(1,?,?,?)", ("a", "1", sha(["0" * 64, "a", "1"])))
    FrozenStore.scans = 0
    return path


def test_full_scan_once_and_snapshot_stable_during_concurrent_append(store):
    view = snapshot_store_type(FrozenStore)(store)
    try:
        first = view.verify()
        # Append as another writer; report read view must remain unchanged.
        writer = object.__new__(FrozenStore)
        writer.path = store
        writer.append("decision", "b", "", {"execution_allowed": False})
        assert view.get("b") is None
        altered = view.verify()
        altered["head"] = "tampered return object"
        assert view.verify() == first and FrozenStore.scans == 1
        view.append("report", "r", "", {"execution_allowed": False})
        assert view.get("r") is None
        assert view.verify() == first
        assert writer.verify()["events"] == 3
    finally:
        view.close()
    # No verification cache survives a new report invocation.
    fresh = snapshot_store_type(FrozenStore)(store)
    try:
        assert fresh.verify()["events"] == 3
        assert FrozenStore.scans == 3
    finally:
        fresh.close()


def test_corruption_detected_and_mutations_denied(store):
    with sqlite3.connect(store) as db:
        db.execute("UPDATE events SET value='corrupt'")
    view = snapshot_store_type(FrozenStore)(store)
    try:
        with pytest.raises(AssertionError, match="chain_broken"):
            view.verify()
        with pytest.raises(ValueError, match="unverified_report"):
            view.append("report", "r", "", {"execution_allowed": False})
        for kind in ("registry", "decision", "quote", "model_entry", "source"):
            with pytest.raises(ValueError, match="write_denied"):
                view.append(kind, "r", "", {"execution_allowed": False})
        with pytest.raises(ValueError, match="write_denied"), view.connect():
            pass
    finally:
        view.close()


@pytest.mark.parametrize("incomplete,code", [(False, 0), (True, 3)])
def test_generate_uses_frozen_report_and_restores_module(store, incomplete, code):
    module = SimpleNamespace(Store=FrozenStore)

    class Runtime:
        def __init__(self, bundle, state, client):
            assert client is None
            self.store = module.Store(store)
            self.store.verify()

        def _now(self):
            return datetime(2026, 11, 18, 23, tzinfo=UTC)

        def report(self, day):
            return {"incomplete": incomplete, "chain": self.store.verify()}

    module.ForwardRuntime = Runtime
    module.expected_completed_day = lambda now: date(2026, 11, 18)
    module.latest_report_day = lambda store, now: date(2026, 11, 18)
    result, status = generate(module, store.parent, store.parent)
    assert status == code and result["execution_allowed"] is False
    assert module.Store is FrozenStore and FrozenStore.scans == 1
    with pytest.raises(ValueError, match="window_not_completed"):
        generate(module, store.parent, store.parent, date(2026, 11, 19))
    assert module.Store is FrozenStore
    module.latest_report_day = lambda store, now: None
    assert generate(module, store.parent, store.parent)[1] == 2


@pytest.mark.parametrize("properties,reason", [
    ({}, "OK"), ({"Result": "timeout"}, "timeout"),
    ({"Result": "exit-code"}, "generation failed"),
    ({"ExecMainStatus": "3"}, "incomplete evidence"),
    ({"ExecMainStatus": "2"}, "generation failed"),
    ({"ActiveState": "inactive"}, "timer inactive"),
    ({"ExecMainExitTimestampMonotonic": "0"}, "completion unknown"),
    ({"ExecMainExitTimestampMonotonic": "x"}, "completion unknown"),
    ({"ExecMainExitTimestampMonotonic": "1000000000"}, "stale"),
])
def test_research_monitor_distinguishes_generation_and_incomplete(properties, reason):
    defaults = {"Result": "success", "ExecMainStatus": "0", "ActiveState": "active",
                "ExecMainExitTimestampMonotonic": "99900000000"}
    value = report_check(lambda unit, prop: (defaults | properties).get(prop, ""),
                         datetime(2026, 11, 19, 2, tzinfo=UTC), 100000)
    assert value == ("OK" if reason == "OK" else "FAIL: research report " + reason)
    if value != "OK":
        assert safe_reason(value) != "сбой; подробности в diagnose"
    assert "не торговый API" in check_description(KEY)
    assert UNIT in check_action(KEY)


def test_grace_does_not_declare_previous_night_report_stale():
    props = {"ActiveState": "active", "Result": "success", "ExecMainStatus": "0",
             "ExecMainExitTimestampMonotonic": str(13600 * 1000000)}
    # Previous night finished at 00:05; new nightly report still has until 00:12.
    show = lambda unit, prop: props[prop]  # noqa: E731
    assert report_check(show, datetime(2026, 11, 18, 21, 5, tzinfo=UTC), 100000) == "OK"
    assert "stale" in report_check(show, datetime(2026, 11, 18, 21, 13, tzinfo=UTC), 100480)


def test_installer_only_changes_report_wrapper_and_preserves_frozen_bundle():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts/ubuntu/install-research-report.sh").read_text()
    dropin = (root / "deploy/research-report/report.conf").read_text()
    update = (root / "scripts/ubuntu/update.sh").read_text()
    assert "sha256sum --check --status BUNDLE_MANIFEST.sha256" in script
    assert "SKIP: separate frozen research report is not installed" in script
    assert "systemctl" not in script and "bot.env" not in script
    assert "rm " not in script and "cp " not in script
    assert "CPUQuota=" not in dropin and "MemoryMax=" not in dropin
    assert "-I -B /usr/local/lib/moex-research-report/forward_report.py" in dropin
    assert "SuccessExitStatus=3" in dropin and "TimeoutStartSec=10min" in dropin
    assert update.index('scripts/ubuntu/install-research-report.sh') < update.index(
        'systemctl daemon-reload')


def test_kuma_report_diagnostics_do_not_mask_rss_with_iss(tmp_path):
    at = datetime(2026, 11, 19, 2, tzinfo=UTC)
    path = tmp_path / "monitor.json"
    path.write_text(json.dumps({"observed_at": at.isoformat(), "checks": {
        KEY: "FAIL: research report incomplete evidence",
        "api:geo_moex_risk": "FAIL: probe timeout (10.1s)",
        "api:news_iss_observer": "OK",
    }}))
    assert "наблюдения неполны" in safe_failure_details("Trading data", path, at)
    rss = safe_failure_details("RSS [api:geo_moex_risk]", path, at)
    assert "источник не ответил" in rss and "наблюдения неполны" not in rss
