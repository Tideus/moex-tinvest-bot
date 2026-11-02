from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_live_execution import _setup

from moex_bot.live_execution import _authorize, enroll_live_journal
from moex_bot.monitoring_labels import INACTIVE, compact_checks, safe_failure_details
from moex_bot.monitoring_probes import read_calendar_health


@pytest.mark.parametrize("change", ["empty-account", "naive", "reversed", "future"])
def test_initial_enrollment_refuses_incoherent_evidence(tmp_path, change):
    broker, kw, now = _setup(tmp_path)
    s = broker.state
    invalid = {
        "empty-account": {"account_id": " "},
        "naive": {"finished_at": now.replace(tzinfo=None)},
        "reversed": {"finished_at": s.started_at - timedelta(seconds=1)},
        "future": {"finished_at": now + timedelta(seconds=1)},
    }[change]
    target = tmp_path / "new-enrollment.sqlite3"
    with pytest.raises(ValueError, match="eligible fresh"):
        enroll_live_journal(target, policy=kw["policy"], state=replace(s, **invalid), now=now)
    assert not target.exists() and not broker.calls


def test_missing_calendar_and_nonfinite_prod_turnover(tmp_path):
    broker, kw, now = _setup(tmp_path)
    with pytest.raises(ValueError, match="monetary"):
        _authorize(
            kw["intent"],
            kw["policy"],
            replace(broker.state, daily_turnover=Decimal("NaN")),
            {},
            0,
            Decimal(0),
            now,
        )
    assert not broker.calls
    path = tmp_path / "absent"
    assert "events.sqlite3" in compact_checks({"data:calendar": read_calendar_health(path, now)})
    assert not path.exists()


def test_compact_metrics_keep_status_and_do_not_echo_secrets(tmp_path):
    import json
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    checks = {"timer:intraday": INACTIVE, "timer:shadow": INACTIVE, "service:health": "OK"}
    detail = compact_checks(checks)
    assert "2/3" in detail and "intraday, shadow" in detail
    path = tmp_path / "checks"
    path.write_text(json.dumps({"checks": checks, "observed_at": now.isoformat()}))
    assert safe_failure_details("Trading Timers", path, now) == detail
    assert "ожидаемых" in compact_checks({})
    assert "OK" in compact_checks({"api:tinvest_prod": "OK"})
    notes = compact_checks({f"private-secret-{i}": "secret" for i in range(6)})
    assert "6/6" in notes and "ещё причин: 2" in notes and "secret" not in notes


def test_update_never_dispatches_a_trading_cycle_and_checks_revision_before_copy():
    text = (Path(__file__).resolve().parents[1] / "scripts/ubuntu/update.sh").read_text()
    assert "systemctl start moex-tinvest-shadow.service" not in text
    assert text.index("MOEX_EXPECTED_REVISION") < text.index("rsync -a")


def test_kuma_failed_push_does_not_starve_other_monitors(monkeypatch):
    import importlib.util
    import io
    import sys
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlparse

    path = Path(__file__).resolve().parents[1] / "scripts/ubuntu/kuma-exporter.py"
    spec = importlib.util.spec_from_file_location("rc5_exporter", path)
    exporter = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "grp", SimpleNamespace())
    spec.loader.exec_module(exporter)
    seen = []

    def receive(url, timeout):
        seen.append(parse_qs(urlparse(url).query))
        if len(seen) == 1:
            raise OSError("fixture")
        return io.BytesIO(b'{"ok":true}')

    monkeypatch.setattr(exporter, "urlopen", receive)
    name = "api:tinvest_prod"
    assert (
        exporter.publish_groups(
            {"Resources": "a", name: "b", "Observer heartbeat": "c"},
            {name: "OK", name + "-bogus": "FAIL"},
            {"Resources": ("disk:",), name: (name,), "Observer heartbeat": ()},
        )
        == 2
    )
    assert [item["status"] for item in seen] == [["down"], ["up"], ["up"]]
