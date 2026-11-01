"""Synthetic exits use durable fake fills, never external broker/Telegram IO."""

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_live_execution import RELEASE, _setup
from test_live_protection import _stop
from test_tinvest_sandbox import RecordingTransport

from moex_bot import take_profit as m
from moex_bot.domain import OrderStatus
from moex_bot.live_execution import LiveHolding, execute_live_intent, intent_digest
from moex_bot.live_protection import ProtectionJournal, ProtectionPlan


@pytest.fixture
def case(tmp_path):
    broker, kw, now = _setup(tmp_path)
    buy = replace(kw["intent"], lots=1, notional=Decimal(1000))
    kw["intent"] = buy
    kw["permission"] = replace(kw["permission"], intent_sha256=intent_digest(buy))
    broker.state = replace(broker.state, estimated_total=Decimal(1001))
    filled = execute_live_intent(**kw)
    plan = ProtectionPlan("real-iis", buy.order_request_id, filled.intent.order_request_id,
                          buy.instrument, 1, Decimal(100), Decimal(95),
                          take_profit=Decimal(102))
    journal = ProtectionJournal(tmp_path / "protection.sqlite3")
    journal.prepare(plan, kw["journal_path"])
    journal._state(plan, "ACTIVE", "known-stop")
    stops = Mock()
    stops.account_id = plan.account_id
    stops.environment = "prod"
    row = _stop(plan)
    stops.history.side_effect = lambda: (row.copy(),)

    def cancel(stop_id):
        row["status"] = "STOP_ORDER_STATUS_CANCELED"

    stops.cancel.side_effect = cancel
    broker.calls.clear()
    now += timedelta(seconds=1)
    broker.state = replace(
        broker.state, bid=Decimal(102), ask=Decimal("102.1"),
        started_at=now, finished_at=now, book_at=now,
        positions={buy.instrument.uid: LiveHolding(buy.instrument, 10, 10, Decimal(102))},
        stop_uids=frozenset(),
    )
    intent = m.exit_intent(plan)
    permission = replace(kw["permission"], intent_sha256=intent_digest(intent),
                         expires_at=now + timedelta(minutes=4))
    run = dict(plan=plan, journal=journal, entry_path=kw["journal_path"], policy=kw["policy"],
               broker=broker, stops=stops, permission=permission, release=RELEASE,
               gate=Mock(), clock=lambda: now)
    return run, row, now


def test_exit_cancels_before_exact_sell_and_closes_ownership(case):
    kw, _, now = case
    assert m.load_plan(kw["journal"].path, "real-iis", kw["entry_path"]) == kw["plan"]
    kw["broker"].state = replace(kw["broker"].state, stop_uids=frozenset({"uid-sber"}))
    original_cancel = kw["stops"].cancel.side_effect

    def cancel(stop):
        assert not kw["broker"].calls
        original_cancel(stop)
        kw["broker"].state = replace(kw["broker"].state, stop_uids=frozenset())

    kw["stops"].cancel.side_effect = cancel
    assert m.run_exit(**kw)["state"] == "CLOSED_TAKE_PROFIT"
    assert len(kw["broker"].calls) == 1
    assert kw["broker"].calls[0].side.value == "sell"
    with pytest.raises(ValueError, match="ACTIVE"):
        m.run_exit(**kw)
    assert len(kw["broker"].calls) == 1


@pytest.mark.parametrize("fault", ["history", "executed", "child", "activation", "mismatch",
                                   "lost_cancel", "unfilled", "none", "cas", "gate"])
def test_ambiguous_results_never_retry_sell(case, monkeypatch, fault):
    kw, row, _ = case
    original = kw["stops"].cancel.side_effect

    def cancel(stop):
        original(stop)
        if fault == "history":
            kw["stops"].history.side_effect = lambda: ()
        elif fault == "executed":
            row["status"] = "STOP_ORDER_STATUS_EXECUTED"
        elif fault == "child":
            row["exchangeOrderId"] = "child"
        elif fault == "activation":
            row["activationDateTime"] = "2026-01-01T00:00:00Z"
        elif fault == "mismatch":
            row["lotsRequested"] = "2"
        elif fault == "lost_cancel":
            raise TimeoutError()

    kw["stops"].cancel.side_effect = cancel
    if fault == "unfilled":
        kw["broker"].status = OrderStatus.ACCEPTED
    if fault == "none":
        monkeypatch.setattr(m, "execute_live_intent", lambda **kwargs: None)
    if fault == "cas":
        original_precheck = m.precheck

        def precheck(*args):
            original_precheck(*args)
            kw["journal"]._state(kw["plan"], "UNKNOWN", "known-stop")

        monkeypatch.setattr(m, "precheck", precheck)
    if fault == "gate":
        kw["gate"].side_effect = [None, ValueError("revoked")]
    with pytest.raises((ValueError, TimeoutError)):
        m.run_exit(**kw)
    kw["gate"].side_effect = None
    count = len(kw["broker"].calls)
    with pytest.raises(ValueError):
        m.run_exit(**kw)
    assert len(kw["broker"].calls) == count


@pytest.mark.parametrize("fault", ["account", "state", "missing", "duplicate", "foreign",
                                   "triggered", "identity", "ttl", "not_reached",
                                   "broker_account", "stop_environment"])
def test_fail_before_any_mutation(case, fault):
    kw, row, now = case
    if fault == "account":
        kw["stops"].account_id = "other"
    elif fault == "broker_account":
        kw["broker"].account_id = "other"
    elif fault == "stop_environment":
        kw["stops"].environment = "sandbox"
    elif fault == "state":
        kw["journal"]._state(kw["plan"], "UNKNOWN", "known-stop")
    elif fault == "missing":
        kw["stops"].history.side_effect = lambda: ()
    elif fault == "duplicate":
        kw["stops"].history.side_effect = lambda: (row, row)
    elif fault == "foreign":
        row["stopOrderId"] = "manual"
    elif fault == "triggered":
        row["status"] = "STOP_ORDER_STATUS_EXECUTED"
    elif fault == "identity":
        kw["permission"] = replace(kw["permission"], journal_uuid="other")
    elif fault == "ttl":
        kw["permission"] = replace(kw["permission"], expires_at=now + timedelta(seconds=119))
    else:
        kw["broker"].state = replace(kw["broker"].state, bid=Decimal(100))
    with pytest.raises(ValueError):
        m.run_exit(**kw)
    kw["stops"].cancel.assert_not_called()
    assert not kw["broker"].calls


def test_plan_validation_and_precheck_boundaries(case):
    kw, _, now = case
    plan = kw["plan"]
    for target in (Decimal(100), Decimal("NaN"), Decimal("102.001")):
        with pytest.raises(ValueError):
            replace(plan, take_profit=target).validate()
    with pytest.raises(ValueError):
        m.exit_intent(replace(plan, take_profit=None))
    with pytest.raises(ValueError):
        m.precheck(plan, replace(m.exit_intent(plan), lots=2), kw["policy"],
                   kw["broker"], kw["entry_path"], lambda: now)
    kw["broker"].state = replace(kw["broker"].state, account_id="other")
    with pytest.raises(ValueError):
        m.precheck(plan, m.exit_intent(plan), kw["policy"], kw["broker"],
                   kw["entry_path"], lambda: now)


@pytest.mark.parametrize("fault", ["uuid", "rows", "digest", "account", "target", "parent"])
def test_load_plan_requires_exact_durable_identity(case, fault):
    kw, _, _ = case
    plan = kw["plan"]
    with sqlite3.connect(kw["journal"].path) as db:
        if fault == "uuid":
            db.execute("UPDATE protection SET live_uuid='other'")
        elif fault == "rows":
            db.execute("DELETE FROM protection")
        else:
            changed = plan
            if fault == "account":
                changed = replace(plan, account_id="other")
            elif fault == "target":
                changed = replace(plan, take_profit=None)
            digest = "other" if fault == "digest" else changed.digest
            db.execute("UPDATE protection SET plan=?,plan_hash=?",
                       (json.dumps(asdict(changed), default=str), digest))
    if fault == "parent":
        with sqlite3.connect(kw["entry_path"]) as db:
            raw = json.loads(db.execute("SELECT record FROM actions").fetchone()[0])
            raw["status"] = "accepted"
            db.execute("UPDATE actions SET record=?", (json.dumps(raw),))
    with pytest.raises(ValueError):
        m.load_plan(kw["journal"].path, "real-iis", kw["entry_path"])


def test_fresh_exit_preview_is_private_and_bound_to_every_action(case, tmp_path):
    kw, _, now = case
    raw = m.preview_report(kw["plan"], "known-stop", RELEASE, kw["policy"],
                           kw["permission"].journal_uuid, now)
    path = tmp_path / "exit.json"
    assert len(m.write_preview(path, raw)) == 64
    permission = m.exit_permission(json.loads(path.read_text()), kw["plan"], "known-stop",
                                    RELEASE, kw["policy"], kw["permission"].journal_uuid, now)
    assert permission.account_id == "real-iis"
    raw["protection_gap_acknowledged"] = False
    with pytest.raises(ValueError):
        m.exit_permission(raw, kw["plan"], "known-stop", RELEASE, kw["policy"],
                          kw["permission"].journal_uuid, now)
    with pytest.raises(ValueError):
        m.write_preview(tmp_path / "exit.txt", raw)
    with pytest.raises(FileExistsError):
        m.write_preview(path, raw)


def test_native_exit_only_exact_cancel_and_all_history():
    transport = RecordingTransport({"stopOrders": []})
    broker = m.NativeExitBroker("fake", "real-iis", transport)
    assert broker.history() == ()
    transport.response = {"time": "2026-11-01T10:00:00Z"}
    broker.cancel("exact-stop")
    transport.response = {"time": "2026-11-01T10:00:00"}
    with pytest.raises(ValueError):
        broker.cancel("exact-stop")
    for bad in (None, [1]):
        transport.response = {"stopOrders": bad}
        with pytest.raises(ValueError):
            broker.history()


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_private_preview_platform_modes(case, tmp_path, monkeypatch, platform):
    kw, _, now = case
    monkeypatch.setattr(m, "os", SimpleNamespace(name=platform))
    report = m.preview_report(kw["plan"], "known-stop", RELEASE, kw["policy"],
                              kw["permission"].journal_uuid, now)
    assert len(m.write_preview(tmp_path / "private.json", report)) == 64
