import argparse
import hashlib
import json
import os
import subprocess
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

import moex_bot.long_daily_report as report
from moex_bot.integrations.tinvest_sandbox import SandboxBrokerSnapshot
from moex_bot.long_auto_once import init
from moex_bot.performance import TradeOperation
from moex_bot.scheduler import MOSCOW

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 3)
NOW = datetime(2026, 11, 3, 23, 55, tzinfo=MOSCOW)


@pytest.mark.parametrize("hour,minute,expected", [(23, 55, 3), (23, 59, 3), (0, 2, 2), (12, 0, 2)])
def test_closed_report_date(hour, minute, expected):
    assert report.report_day(NOW.replace(hour=hour, minute=minute)).day == expected


def test_reject_unclosed_future_and_naive_dates():
    for now, day in [(NOW.replace(hour=23, minute=20), DAY), (NOW, DAY + timedelta(days=1))]:
        with pytest.raises(ValueError):
            report.report_day(now, day)
    with pytest.raises(ValueError):
        report.report_day(NOW.replace(tzinfo=None))
    assert report.report_day(NOW.replace(hour=0), DAY - timedelta(days=1)) == DAY - timedelta(
        days=1
    )


def test_weekend_not_silently_skipped():
    assert report.report_day(datetime(2026, 11, 8, 23, 55, tzinfo=MOSCOW)).weekday() == 6


@pytest.mark.parametrize("offset,valid", [(-10, False), (0, True), (360, True), (420, False)])
def test_no_next_session_valuation(offset, valid):
    assert report.closing_window(DAY, NOW + timedelta(minutes=offset)) is valid


@pytest.mark.parametrize(
    "method", ["PostOrder", "CancelOrder", "PostSandboxStopOrder", "OpenSandboxAccount"]
)
def test_transport_rejects_every_write_before_network(method):
    with pytest.raises(ValueError, match="only permits"):
        report.ReportTransport().post(
            "https://sandbox-invest-public-api.tbank.ru/rest/" + method,
            headers={},
            payload={},
            timeout_seconds=1,
        )


def point(at, equity, positions=None):
    return {
        "observed_at": at.isoformat(),
        "reported_equity": str(equity),
        "positions": positions or {},
    }


def operation(at, side, gross, fee=0, external=0):
    return TradeOperation("test", at, "SBER", side, 10, D(gross), D(fee), D(external))


def test_buy_is_not_loss_and_transfer_is_not_profit():
    a = point(NOW.replace(hour=10), 300000)
    b = point(NOW, 310099, {"SBER": {"current_value": "3100"}})
    ops = (
        operation(NOW - timedelta(hours=1), "BUY", 3000, 1),
        operation(NOW - timedelta(minutes=10), None, 0, external=10000),
    )
    p = report.performance(a, b, ops, DAY)
    assert p["pnl"] == 99 and p["rows"]["SBER"]["pnl"] == 99
    assert p["trades"] == 1 and p["commission"] == 1


def test_missing_points_not_fake_zero_and_historical_day_filtered():
    p = report.performance(None, None, (operation(NOW + timedelta(days=1), "BUY", 99),), DAY)
    assert p["pnl"] is None and p["trades"] == 0


def test_transfer_after_midnight_before_snapshot_is_excluded():
    a = point(NOW - timedelta(days=1), 300000)
    b = point(NOW + timedelta(minutes=20), 301000)
    p = report.performance(
        a, b, (operation(NOW + timedelta(minutes=10), None, 0, external=1000),), DAY
    )
    assert p["pnl"] == 0


def test_unknown_position_value_not_fabricated():
    p = report.performance(
        point(NOW - timedelta(hours=1), 300000, {"SBER": {"lots": 1}}),
        point(NOW, 300001, {"SBER": {"current_value": "3001"}}),
        (),
        DAY,
    )
    assert "pnl" not in p["rows"]["SBER"]
    with pytest.raises(ValueError):
        report.performance(point(NOW, 1), point(NOW - timedelta(hours=1), 1), (), DAY)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_opening_prefers_previous_close_and_rejects_other_account(tmp_path):
    base = tmp_path / "base"
    output = tmp_path / "out/report.txt"
    morning = {**point(NOW.replace(hour=10), 300000), "account_id": "long"}
    write(base / "run-first/portfolio.json", morning)
    prev = {
        **point(NOW - timedelta(days=1), 299000),
        "account_binding": hashlib.sha256(b"long").hexdigest(),
        "report_date": "2026-11-02",
    }
    write(output.parent / "long-close-2026-11-02.json", prev)
    assert report.opening_point(base, output, DAY, "long") == prev
    assert report.opening_point(base, output, DAY, "other") is None
    prev["account_binding"] = "foreign"
    write(output.parent / "long-close-2026-11-02.json", prev)
    assert report.opening_point(base, output, DAY, "long") == morning


def test_decision_day_and_scope(tmp_path):
    p = {
        "run_id": "shadow-" + NOW.isoformat(),
        "quality": {"passed": True},
        "orders": [],
        "targets": [],
        "rejected": [],
        "flow_diagnostics": [{"secid": "SBER", "passed": False}],
    }
    write(tmp_path / "run-1/plan.json", p)
    write(tmp_path / "run-1/portfolio.json", {"account_id": "long"})
    assert report.decision_summary(tmp_path, DAY, "long")["flow_rejected"] == {"SBER": 1}
    assert report.decision_summary(tmp_path, DAY - timedelta(days=1), "long")["cycles"] == 0
    with pytest.raises(ValueError, match="foreign"):
        report.decision_summary(tmp_path, DAY, "other")


def fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("T_INVEST_SANDBOX_LONG_ACCOUNT_ID", "long")
    monkeypatch.setenv("T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID", "other")
    monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "prod")
    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", "fixture")
    base = tmp_path / "long-once"
    anchor = tmp_path / "anchor/claim"
    init(base / "sandbox.sqlite3", "long", anchor=anchor)
    args = argparse.Namespace(
        report_date=DAY,
        base=base,
        anchor=anchor,
        accounts=ROOT / "config/accounts.json",
        universe=ROOT / "config/universe.json",
        notifications=ROOT / "config/notifications.json",
        output=tmp_path / "out/long.txt",
        outbox=tmp_path / "outbox.sqlite3",
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW

    monkeypatch.setattr(report, "datetime", Clock)

    class Service:
        def __init__(self, token, transport):
            assert isinstance(transport, report.ReportTransport)

        def broker_snapshot(self, account, instruments):
            assert account == "long"
            return SandboxBrokerSnapshot("long", D(300000), D(0), D(300000), {}, {}, {}, 0)

        def operations(self, account, instruments, **kwargs):
            assert account == "long"
            return ()

        def account_state(self, account):
            assert account == "long"
            return {"stop_orders": {"stopOrders": []}}

    monkeypatch.setattr(report, "TInvestSandboxAccountService", Service)
    return args


def test_generate_waiting_without_plan_does_not_skip_or_mutate_ledger(tmp_path, monkeypatch):
    args = fixture(tmp_path, monkeypatch)
    original = (args.base / "sandbox.sqlite3").read_bytes()
    assert report.generate(args, NOW) == 0
    assert "WAITING" in args.output.read_text(encoding="utf-8")
    assert "недостаточно" in args.output.read_text(encoding="utf-8")
    assert "account_id" not in args.output.with_suffix(".json").read_text()
    assert original == (args.base / "sandbox.sqlite3").read_bytes()
    assert not args.anchor.exists()
    report.generate(args, NOW)
    import sqlite3

    with sqlite3.connect(args.outbox) as db:
        assert db.execute("SELECT count(*) FROM notification_outbox").fetchone() == (1,)
    assert (args.output.parent / "long-close-2026-11-03.json").is_file()


@pytest.mark.parametrize("fault", ["alias", "prod", "missing", "ledger"])
def test_generate_fails_closed_before_broker(tmp_path, monkeypatch, fault):
    args = fixture(tmp_path, monkeypatch)
    if fault == "alias":
        monkeypatch.setenv("T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID", "long")
    if fault == "prod":
        monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "long")
    if fault == "missing":
        monkeypatch.delenv("T_INVEST_SANDBOX_LONG_ACCOUNT_ID")
    if fault == "ledger":
        args.base = tmp_path / "absent"
    with pytest.raises(ValueError):
        report.generate(args, NOW)
    assert not args.output.exists() and not args.outbox.exists()


def test_script_keeps_profiles_independent_and_legacy_fallback():
    shell = (ROOT / "scripts/ubuntu/run-daily-report.sh").read_text()
    assert "sandbox-long-once.owner" in shell and "moex_bot.long_daily_report" in shell
    assert "account-performance-report" in shell and "intraday-performance-report" in shell
    assert shell.index('exit "${report_status}"') > shell.index("telegram-send")
    assert 'weekday}" -gt 5' not in shell


@pytest.mark.skipif(os.name == "nt", reason="Linux deployment shell")
@pytest.mark.parametrize("enrolled,failure", [(True, False), (True, True), (False, False)])
def test_daily_shell_executes_both_reports(tmp_path, enrolled, failure):
    app, state, config = (tmp_path / n for n in ("app", "state", "config"))
    config.mkdir()
    if enrolled:
        (config / "sandbox-long-once.owner").write_text("fixture")
    fake = tmp_path / "python"
    fake.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\n'
        'if [[ "$*" == *"--date-only"* ]]; then echo 2026-11-03; exit 0; fi\n'
        'if [[ "$*" == *"moex_bot.long_daily_report"* && "$FAIL_LONG" == 1 ]]; then exit 2; fi\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "MOEX_BOT_APP_DIR": str(app),
        "MOEX_BOT_STATE_DIR": str(state),
        "MOEX_BOT_CONFIG_DIR": str(config),
        "MOEX_BOT_LOG_DIR": str(tmp_path / "logs"),
        "MOEX_BOT_PYTHON": str(fake),
        "CALLS": str(tmp_path / "calls"),
        "FAIL_LONG": str(int(failure)),
    }
    p = subprocess.run(["bash", str(ROOT / "scripts/ubuntu/run-daily-report.sh")], env=env)
    calls = (tmp_path / "calls").read_text()
    assert p.returncode == (2 if failure else 0)
    assert "intraday-performance-report" in calls and "telegram-send" in calls
    assert ("account-performance-report" not in calls) == enrolled
