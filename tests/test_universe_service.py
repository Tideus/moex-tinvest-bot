import importlib.util
import json
import os
import subprocess
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import universe_capture as capture
import universe_launch as launch
import universe_service as svc
import universe_watch as watch

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 13)
AT = datetime.combine(DAY, time(10, 15, 1), svc.MSK)


def calendar_payload(day=DAY, opened=True, close="18:54:59"):
    row = {
        "date": str(day) + "T00:00:00Z",
        "isTradingDay": opened,
        "startTime": str(day) + "T06:10:00Z",
        "endTime": datetime.combine(day, time.fromisoformat(close), svc.MSK)
        .astimezone(UTC)
        .isoformat(),
    }
    return {
        "tinvest": {"payload": {"exchanges": [{"exchange": "MOEX", "days": [row]}]}},
        "moex": {
            "payload": {
                "session_schedule": {
                    "columns": [
                        "tradedate",
                        "boardid",
                        "tradingsession",
                        "type",
                        "secid",
                        "time_from",
                        "time_till",
                    ],
                    "data": [[str(day), "TQBR", 1, "system", None, "09:10:00", close]]
                    if opened
                    else [],
                }
            }
        },
    }


def cfg():
    return watch.policy(ROOT / "scripts/research/universe24.json")


def result(at=AT):
    config = cfg()
    snapshot = {"as_of": at.isoformat(), "rows": [], "errors": []}
    return {
        "started_at": at.isoformat(),
        "snapshot": snapshot,
        "screen": watch.screen(snapshot, config),
    }


def fixture_state(tmp_path):
    state, bundle = tmp_path / "state", tmp_path / "bundle"
    (state / "calendar").mkdir(parents=True)
    bundle.mkdir()
    (state / "calendar" / f"{DAY}.json").write_text(json.dumps(calendar_payload()))
    (bundle / "universe.json").write_text("[]")
    (bundle / "BUNDLE_MANIFEST.sha256").write_text("fixture")
    return state, bundle


@pytest.mark.parametrize(
    "opened,close,expected",
    [
        (True, "18:54:59", "OPEN"),
        (False, "18:54:59", "CLOSED"),
        (True, "14:00:00", "SHORT_SESSION_EXCLUDED"),
    ],
)
def test_real_calendar_gate(opened, close, expected):
    assert svc.calendar_status(calendar_payload(opened=opened, close=close), DAY) == expected


def test_calendar_disagreement_and_missing_fail():
    payload = calendar_payload()
    payload["tinvest"]["payload"]["exchanges"][0]["days"][0]["isTradingDay"] = False
    with pytest.raises(ValueError, match="disagree"):
        svc.calendar_status(payload, DAY)
    with pytest.raises(ValueError, match="missing"):
        svc.calendar_status(calendar_payload(), DAY + timedelta(days=1))
    payload = calendar_payload()
    payload["tinvest"]["payload"]["exchanges"][0]["days"][0]["endTime"] = str(DAY) + "T15:00:00Z"
    with pytest.raises(ValueError, match="disagree"):
        svc.calendar_status(payload, DAY)


def test_slots_are_frozen_and_no_calendar_weekday_guess():
    actual = svc.slots(DAY)
    assert len(actual) == 98 and actual[0].time() == time(10, 15)
    assert actual[-1].time() == time(18, 20)
    # A Sunday still needs the API's actual calendar, not a hard-coded weekday exclusion.
    sunday = date(2026, 11, 15)
    assert svc.calendar_status(calendar_payload(day=sunday), sunday) == "OPEN"


def test_reservation_prevents_repeated_capture_after_restart(tmp_path, monkeypatch):
    state, bundle = fixture_state(tmp_path)
    monkeypatch.setenv("T_INVEST_READONLY_TOKEN", "fixture-only")
    transport = Mock(return_value=result())
    first = svc.capture_slot(
        state, bundle, cfg(), AT, calendar=lambda *a: "OPEN", capture=transport
    )
    assert first["status"] == "CAPTURED" and first["execution_allowed"] is False
    second = svc.capture_slot(
        state, bundle, cfg(), AT, calendar=lambda *a: "OPEN", capture=transport
    )
    assert second["status"] == "ALREADY_ATTEMPTED" and transport.call_count == 1


@pytest.mark.parametrize(
    "at", [AT + timedelta(seconds=30), AT - timedelta(minutes=1), AT.replace(hour=18, minute=25)]
)
def test_no_late_or_out_of_window_catchup(tmp_path, at):
    transport = Mock()
    assert (
        svc.capture_slot(tmp_path, tmp_path, cfg(), at, capture=transport)["status"]
        == "OUTSIDE_SLOT"
    )
    transport.assert_not_called()


def test_calendar_failure_reserves_slot_and_reports_source_without_secret(tmp_path):
    state, bundle = fixture_state(tmp_path)
    error = svc.SourceError("MOEX_APIM", "official_calendar", "HTTP_403")
    calendar = Mock(side_effect=error)
    transport = Mock()
    actual = svc.capture_slot(state, bundle, cfg(), AT, calendar=calendar, capture=transport)
    assert actual["error"] == {
        "source": "MOEX_APIM",
        "stage": "official_calendar",
        "error": "HTTP_403",
    }
    assert actual["status"] == "FAILED"
    transport.assert_not_called()
    assert (
        svc.capture_slot(state, bundle, cfg(), AT, calendar=calendar)["status"]
        == "ALREADY_ATTEMPTED"
    )


@pytest.mark.parametrize("calendar", ["CLOSED", "SHORT_SESSION_EXCLUDED"])
def test_closed_calendar_does_not_contact_quote_api(tmp_path, calendar):
    transport = Mock()
    actual = svc.capture_slot(
        tmp_path, tmp_path, cfg(), AT, calendar=lambda *a: calendar, capture=transport
    )
    assert actual["status"] == calendar
    transport.assert_not_called()


def test_api_delay_cannot_backdate_slot(tmp_path, monkeypatch):
    state, bundle = fixture_state(tmp_path)
    monkeypatch.setenv("T_INVEST_READONLY_TOKEN", "fixture-only")
    actual = svc.capture_slot(
        state,
        bundle,
        cfg(),
        AT,
        calendar=lambda *a: "OPEN",
        capture=Mock(return_value=result(AT + timedelta(seconds=40))),
    )
    assert actual["status"] == "FAILED"
    assert not list(state.glob("captures/*/capture-*.json"))


def test_reports_have_separate_control_and_expansion_and_never_promote(tmp_path, monkeypatch):
    state, bundle = fixture_state(tmp_path)
    monkeypatch.setenv("T_INVEST_READONLY_TOKEN", "fixture-only")
    svc.capture_slot(
        state, bundle, cfg(), AT, calendar=lambda *a: "OPEN", capture=Mock(return_value=result())
    )
    before = {p: p.read_bytes() for p in state.glob("captures/*/*.json")}
    at = AT.replace(hour=23, minute=57)
    report = svc.report(state, cfg(), DAY, at)
    assert report["receipt"]["observed_snapshots"] == 1
    assert len(report["receipt"]["missing_slots"]) == 97
    assert {k: v["observations"] for k, v in report["cohorts"].items()} == {
        "control3": 3,
        "expansion21": 21,
        "all24": 24,
    }
    assert report["promotion_to_trading"] is False
    weekly = json.loads((state / "reports/weekly-latest.json").read_bytes())
    assert weekly["complete_sessions"] == 0
    assert weekly["profitability_status"] == "NOT_ESTABLISHED"
    assert {p: p.read_bytes() for p in before} == before
    svc.report(state, cfg(), DAY, at + timedelta(seconds=1))
    assert len(list(state.glob("reports/*/report-*.json"))) == 2


def test_report_before_end_blocked_and_empty_not_success(tmp_path):
    with pytest.raises(ValueError, match="not_closed"):
        svc.report(tmp_path, cfg(), DAY, AT)
    report = svc.report(tmp_path, cfg(), DAY, AT.replace(hour=23))
    assert report["receipt"]["observed_snapshots"] == 0
    assert report["calendar"] == "UNKNOWN"
    assert report["receipt"]["quote_coverage"] == 0


def test_launcher_strips_all_trading_and_arbitrary_environment(tmp_path):
    env = launch.clean_environment(
        {
            "MOEX_APIKEY": "read1",
            "T_INVEST_READONLY_TOKEN": "read2",
            "T_INVEST_PROD_TOKEN": "never",
            "TELEGRAM_BOT_TOKEN": "never",
            "T_INVEST_SANDBOX_ACCOUNT_ID": "never",
            "PYTHONPATH": "evil",
            "UNRELATED_SECRET": "never",
        },
        tmp_path,
    )
    assert env["PYTHONPATH"] == str(tmp_path)
    assert "never" not in env.values() and "evil" not in env.values()
    assert env["T_INVEST_READONLY_TOKEN"] == "read2"


def test_single_missing_warmup_does_not_crash_other_quotes(tmp_path, monkeypatch):
    config = cfg()
    config["symbols"] = ["SBER", "ROSN"]
    config["control_symbols"] = ["SBER"]

    def load(data, symbol, interval):
        if symbol == "ROSN":
            raise ValueError("missing")
        return []

    monkeypatch.setattr(capture, "load", load)
    monkeypatch.setattr(
        capture,
        "capture_one",
        lambda entry, *args: {
            "secid": entry["secid"],
            "received_at": AT.isoformat(),
            "exchange_at": AT.isoformat(),
            "source": "observed_top_of_book",
            "trading_allowed": False,
        },
    )
    actual = capture.capture([{"secid": "SBER"}, {"secid": "ROSN"}], config, tmp_path, None)
    assert len(actual["snapshot"]["rows"]) == 1
    assert actual["snapshot"]["errors"] == [{"secid": "ROSN", "error_type": "ValueError"}]


def test_frozen_warmup_hash_and_no_silent_rewrite(tmp_path, monkeypatch):
    config = cfg() | {"symbols": ["SBER"]}

    def download(symbol, board, interval, start, end, root):
        assert end == DAY - timedelta(days=1)
        (root / "SBER_TQBR_24.csv").write_text("fixture")
        return {"symbol": symbol, "rows": 30}

    fetch = Mock(side_effect=download)
    monkeypatch.setattr(svc.history, "candles", fetch)
    assert svc.prepare(tmp_path, config, DAY)["ready"] == 1
    assert svc.prepare(tmp_path, config, DAY)["ready"] == 1
    assert fetch.call_count == 1
    (tmp_path / "data" / str(DAY) / "SBER_TQBR_24.csv").write_text("corrupt")
    assert svc.prepare(tmp_path, config, DAY)["errors"]
    assert fetch.call_count == 1


def test_atomic_temp_cleanup(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    svc.atomic(path, {"ok": True})
    monkeypatch.setattr(svc.os, "replace", Mock(side_effect=OSError("blocked")))
    with pytest.raises(OSError):
        svc.atomic(path, {"ok": False})
    assert json.loads(path.read_bytes()) == {"ok": True}
    assert not list(tmp_path.glob(".write-*"))


def test_imports_do_not_load_working_bot():
    research = ROOT / "scripts/research"
    code = (
        "import sys; sys.path.insert(0," + repr(str(research)) + "); "
        "import universe_service; assert not any(k.startswith('moex_bot') for k in sys.modules)"
    )
    run = subprocess.run(
        [os.sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr


def test_rejected_calendar_preserves_raw_but_never_caches_success(tmp_path, monkeypatch):
    raw = calendar_payload()
    raw["moex"]["payload"]["session_schedule"]["data"] = []
    monkeypatch.setattr(svc, "calendar_fetch", lambda day: raw)
    monkeypatch.setattr(svc, "now", lambda: AT)
    with pytest.raises(svc.SourceError, match="moex_calendar_missing_or_ambiguous"):
        svc.latest_calendar(tmp_path, DAY, AT)
    assert len(list(tmp_path.glob("calendar-attempts/*.json"))) == 1
    assert not (tmp_path / "calendar" / f"{DAY}.json").exists()


def test_foreign_dynamic_private_directory_is_optional_but_still_denied():
    for name in ("moex-research-universe24.service", "moex-research-universe24-report.service"):
        unit = (ROOT / "deploy/research-universe24" / name).read_text()
        assert "DynamicUser=yes" in unit
        assert " -/var/lib/moex-research-forward-v2" in unit
        assert "InaccessiblePaths=/opt/moex-tinvest-bot" in unit
        assert "ProtectSystem=strict" in unit


@pytest.mark.skipif(os.name == "nt", reason="Linux staging and systemd validation")
def test_linux_installer_isolated_staging(tmp_path):
    run = subprocess.run(
        ["bash", str(ROOT / "deploy/research-universe24/install.sh"), "--staging", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    bundle = tmp_path / "opt/moex-universe24-a95e285-r2"
    assert svc.validate_bundle(bundle)["control_symbols"] == ["SBER", "ROSN", "CHMF"]
    report_unit = (
        tmp_path / "etc/systemd/system/moex-research-universe24-report.service"
    ).read_text()
    assert "PrivateNetwork=yes" in report_unit and "EnvironmentFile=" not in report_unit
    assert "moex_bot" not in (bundle / "universe_service.py").read_text()
    spec = importlib.util.spec_from_file_location("installed_launch", bundle / "universe_launch.py")
    assert spec is not None
    (bundle / "universe24.json").write_text("{}")
    with pytest.raises(ValueError, match="hash"):
        svc.validate_bundle(bundle)
