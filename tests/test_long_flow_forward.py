import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import long_flow_forward as f
import pytest


@pytest.fixture
def now():
    return datetime(2026, 11, 3, 11, 5, 30, tzinfo=f.MOSCOW)


def blocks(now, books=None):
    rows = {k: [] for k in f.DATASETS}
    for i, value in enumerate(books or [-0.3, -0.1, 0.1, 0.1, 0.1, 0.1]):
        at = now.replace(minute=0, second=0) - timedelta(minutes=5 * i)
        common = {
            "tradedate": str(at.date()),
            "tradetime": at.strftime("%H:%M:%S"),
            "systime": (at + timedelta(minutes=5, seconds=2)).isoformat(),
        }
        rows["tradestats"].append(common | {"val_b": 60, "val_s": 40})
        rows["orderstats"].append(
            common | {"put_val_b": 100, "put_val_s": 100, "cancel_val_b": 10, "cancel_val_s": 10}
        )
        rows["obstats"].append(common | {"imbalance_val": value, "spread_bbo": 2})
    return rows


def test_single_vs_smoothed_without_changing_other_gates(now):
    result = f.evaluate(blocks(now), now)
    assert result["variants"]["current"]["state"] == "REJECTED"
    assert result["variants"]["book_mean_3"]["state"] == "RESEARCH_ELIGIBLE"
    assert result["variants"]["book_mean_6"]["state"] == "RESEARCH_ELIGIBLE"
    data = blocks(now)
    data["obstats"][0]["spread_bbo"] = 3.1
    assert all(v["state"] == "REJECTED" for v in f.evaluate(data, now)["variants"].values())


def test_future_data_and_publish_delay_cannot_change_result(now):
    data = blocks(now)
    expected = f.evaluate(data, now)
    for k in f.DATASETS:
        data[k].append(data[k][0] | {"tradetime": "11:05:00", "systime": "2026-11-03 11:10:02"})
    assert f.evaluate(data, now) == expected
    for k in f.DATASETS:
        data[k][0]["systime"] = "2026-11-03 11:06:00"
    assert f.evaluate(data, now)["flow_at"].endswith("10:55:00+03:00")


def test_stale_and_nonconsecutive_are_missing_not_pass(now):
    assert f.evaluate(blocks(now), now + timedelta(hours=1))["state"] == "DATA_MISSING"
    data = blocks(now)
    del data["obstats"][2]
    result = f.evaluate(data, now)
    assert result["variants"]["current"]["state"] == "REJECTED"
    assert result["variants"]["book_mean_3"]["state"] == "DATA_MISSING"


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", "abc"])
def test_bad_metric_fails_closed(now, value):
    data = blocks(now)
    data["obstats"][0]["spread_bbo"] = value
    with pytest.raises((ValueError, ArithmeticError)):
        f.evaluate(data, now)


def test_no_transactions_never_prove_neutral_flow(now):
    data = blocks(now)
    data["tradestats"][0].update(val_b=0, val_s=0)
    assert f.evaluate(data, now)["state"] == "DATA_MISSING"


def test_duplicates_rejected(now):
    data = blocks(now)
    data["tradestats"].append(data["tradestats"][0].copy())
    with pytest.raises(ValueError, match="duplicate"):
        f.evaluate(data, now)


def test_immutable_receipt(tmp_path):
    p = tmp_path / "day.json"
    f.immutable(p, {"state": "EVALUATED"})
    with pytest.raises(FileExistsError):
        f.immutable(p, {"state": "OVERRIDE"})
    assert f.read_json(p)["state"] == "EVALUATED"


@pytest.fixture
def collect_env(monkeypatch, tmp_path, now):
    monkeypatch.setattr(f, "BASE", tmp_path)
    monkeypatch.setattr(f, "APP", tmp_path)
    (tmp_path / "config").mkdir()
    f.immutable(tmp_path / "config/universe.json", [{"secid": "SBER"}])
    ledger = Mock()
    ledger.status.return_value = "WAITING"
    monkeypatch.setattr(f, "ReadOnlyLedger", Mock(return_value=ledger))
    monkeypatch.setattr(f, "_working_account", lambda: "sandbox-only-test")
    snapshot = Mock(return_value=True)
    monkeypatch.setattr(f, "empty_account", snapshot)
    plan = {
        "signal_diagnostics": [{"secid": "SBER", "momentum": "0.1", "eligible": True}],
        "quality": {"passed": True}, "geo": {"level": "normal"},
        "config_snapshot": {"strategy": {}},
        "flow_diagnostics": [],
    }
    monkeypatch.setattr(f, "morning_plan", lambda *args: (plan, "a" * 64))
    monkeypatch.setattr(f, "flow_rows", lambda *args: blocks(now))
    return ledger, snapshot


def test_collect_shadow_only_bound_account_redacted(now, collect_env):
    result = f.collect(now, clock=lambda: now + timedelta(seconds=20))
    assert result["state"] == "EVALUATED" and result["execution_allowed"] is False
    assert result["selected"]["book_mean_3"] == "SBER"
    assert result["selected"]["current"] is None
    assert "sandbox-only-test" not in json.dumps(result)
    assert "orders" not in result


@pytest.mark.parametrize("state", ["FILLED_PROTECTED", "ENTRY_NOT_FILLED", "UNKNOWN"])
def test_attempted_entry_never_researched_or_rearmed(now, collect_env, state):
    ledger, snapshot = collect_env
    ledger.status.return_value = state
    assert f.collect(now, clock=lambda: now)["state"] == "SKIPPED"
    snapshot.assert_not_called()


def test_changed_account_and_expired_collection_skip(now, collect_env):
    ledger, snapshot = collect_env
    snapshot.side_effect = [True, False]
    assert f.collect(now, clock=lambda: now)["state"] == "SKIPPED"
    snapshot.side_effect = None
    snapshot.return_value = True
    assert f.collect(now, clock=lambda: now + timedelta(minutes=20))["state"] == "SKIPPED"
    ledger.status.side_effect = ["WAITING", "FILLED_PROTECTED"]
    assert f.collect(now, clock=lambda: now)["state"] == "SKIPPED"


def test_outside_window_never_contacts_account(now, monkeypatch):
    get = Mock(side_effect=AssertionError("must not read account"))
    monkeypatch.setattr(f, "_working_account", get)
    assert f.collect(now.replace(hour=12))["state"] == "SKIPPED"
    get.assert_not_called()


def test_markout_proxy_costs_and_no_unmatured_outcome(now, monkeypatch):
    entry = now.replace(minute=10, second=0)
    end = entry + timedelta(hours=1)
    rows = [
        {"begin": entry.isoformat(), "open": 100, "volume": 1},
        {"begin": end.isoformat(), "open": 101, "volume": 1},
    ]
    fetch = Mock(return_value=rows)
    monkeypatch.setattr(f, "candles", fetch)
    r = {"ticker": "SBER", "spread_bps": "2"}
    assert f.markout(r, now, end, now)["state"] == "NOT_MATURE"
    fetch.assert_not_called()
    out = f.markout(r, now, end, end + timedelta(hours=1))
    assert out["state"] == "PRICE_MARKOUT_NOT_FILL"
    assert Decimal(out["net_pct"]) == Decimal("0.825")
    assert Decimal(out["double_cost_pct"]) == Decimal("0.65")


def test_report_catchup_no_future_date_or_missing_as_profit(now, tmp_path, monkeypatch):
    monkeypatch.setattr(f, "BASE", tmp_path)
    event = {
        "protocol": f.PROTOCOL,
        "execution_allowed": False,
        "state": "SKIPPED",
        "date": "2026-11-02",
        "reason": "position exists",
    }
    f.immutable(tmp_path / "decisions/2026-11-02.json", event)
    f.immutable(tmp_path / "decisions/2026-11-03.json", event | {"date": "2026-11-03"})
    monkeypatch.setattr(f, "get_rows", lambda *args, **kwargs: [])
    result = f.report(now)
    assert result["report_day"] == "2026-11-02" and len(result["records"]) == 1
    text = f.render(result, datetime(2026, 11, 2).date())
    assert "position exists" in text and "0.00%" not in text


def test_morning_plan_binding_quality_and_strategy(now, tmp_path, monkeypatch):
    state = tmp_path / "sandbox.sqlite3"
    monkeypatch.setattr(f, "STATE", state)
    run = tmp_path / "run-test"
    run.mkdir()
    portfolio = {"account_id": "expected", "source": "t_invest_sandbox"}
    f.immutable(run / "portfolio.json", portfolio)
    strategy = {
        "top_n": 5,
        "min_momentum": "0.01",
        "candle_period": "1D",
        "flow_min_trade_imbalance": "-0.25",
        "flow_min_order_flow": "-0.10",
        "flow_min_book_imbalance": "-0.20",
        "flow_max_spread_bbo": "3.0",
        "flow_confirmation_enabled": True,
        "trend_window": 60,
        "momentum_windows": [5, 10, 20],
        "require_above_trend": True,
    }
    plan = {
        "run_id": "shadow-2026-11-03T10:05:00+03:00",
        "portfolio_input": portfolio,
        "portfolio_input_sha256": hashlib.sha256((run / "portfolio.json").read_bytes()).hexdigest(),
        "quality": {"passed": True},
        "geo": {"level": "normal"},
        "config_snapshot": {"strategy": strategy},
    }
    f.immutable(run / "plan.json", plan)
    assert f.morning_plan("expected", now)[0] == plan
    with pytest.raises(ValueError, match="binding"):
        f.morning_plan("foreign", now)


def test_deployment_is_separate_and_nonmutating():
    root = Path(__file__).parents[1]
    service = (root / "deploy/ubuntu/moex-long-flow-shadow.service").read_text()
    report = (root / "deploy/ubuntu/moex-long-flow-shadow-report.service").read_text()
    assert "ReadWritePaths=/var/lib/moex-long-flow-shadow\n" in service
    assert "EnvironmentFile=" not in report
    assert "/opt/moex-long-flow-shadow/long_flow_forward.py collect" in service
    assert "Persistent=false" in (root / "deploy/ubuntu/moex-long-flow-shadow.timer").read_text()
    assert (
        "11:05:30 Europe/Moscow" in (root / "deploy/ubuntu/moex-long-flow-shadow.timer").read_text()
    )
    source = (root / "scripts/research/long_flow_forward.py").read_text()
    for forbidden in (
        "PostSandboxOrder",
        "PostSandboxStopOrder",
        "CancelSandbox",
        "run_sandbox_candidate",
    ):
        assert forbidden not in source


def test_quality_failure_is_auditable_and_never_fetches_candidates(now, collect_env, monkeypatch):
    digest = "a" * 64
    plan = {"quality": {"passed": False, "errors": [
        "missing long flow for X5", "missing long flow for T", "private raw message",
    ]}}
    monkeypatch.setattr(f, "morning_plan", lambda *_: (plan, digest))
    flow = Mock(side_effect=AssertionError)
    monkeypatch.setattr(f, "flow_rows", flow)
    result = f.collect(now)
    assert result["state"] == "DATA_MISSING" and not result["execution_allowed"]
    assert result["missing_flow"] == ["T", "X5"]
    assert result["morning_plan_sha256"] == digest
    assert "selected" not in result and "private" not in str(result)
    flow.assert_not_called()
    f.immutable(f.BASE / "decisions" / f"{now.date()}.json", result)
    monkeypatch.setattr(f, "OUTBOX", f.BASE / "outbox.db")
    f.queue_alert(now.date())
    f.queue_alert(now.date())
    messages = f.SQLiteOutbox(f.OUTBOX).claim(now=datetime.now(f.UTC) + timedelta(seconds=2))
    assert len(messages) == 1 and messages[0].kind == "health_failure"
    assert "проверка утреннего плана" in messages[0].body and "T, X5" in messages[0].body


def test_failure_alert_is_redacted_and_prevents_replay(tmp_path, monkeypatch, capsys):
    import sys

    monkeypatch.setattr(f, "BASE", tmp_path)
    monkeypatch.setattr(f, "OUTBOX", tmp_path / "outbox.db")
    monkeypatch.setattr(sys, "argv", ["long-flow", "collect"])

    def fail(_now):
        with f.stage("positions"):
            raise ValueError("do-not-forward")

    collector = Mock(side_effect=fail)
    monkeypatch.setattr(f, "collect", collector)
    assert f.main() == 2
    assert f.main() == 2
    collector.assert_called_once()
    day = datetime.now(f.MOSCOW).date()
    assert not (tmp_path / "decisions").exists()
    assert "do-not-forward" not in (tmp_path / "failures" / f"{day}.json").read_text()
    log = capsys.readouterr().out
    assert "stage=positions" in log and "do-not-forward" not in log
    f.queue_alert(day)
    msg, = f.SQLiteOutbox(f.OUTBOX).claim(now=datetime.now(f.UTC) + timedelta(seconds=2))
    assert "T-Invest Sandbox" in msg.body and "do-not-forward" not in msg.body
