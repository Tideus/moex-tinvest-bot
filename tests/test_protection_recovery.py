import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from test_live_protection import _case, _stop

from moex_bot import protection_recovery as recovery_module
from moex_bot.domain import OrderStatus, Side
from moex_bot.live_protection import (
    NativeProtectionBroker,
    ProtectionJournal,
    StopNotDispatched,
    _verified_stop,
)
from moex_bot.protection_recovery import (
    NativeRecoveryParentBroker,
    apply_recovery,
    load_recovery_plan,
    preview_recovery,
    recovery_digest,
)
from moex_bot.supervised_execution import GuardedProtectionBroker, read_parent


def setup_case(tmp_path):
    plan, journal, entry, stop = _case(tmp_path)
    now = datetime(2026, 10, 30, 11, tzinfo=UTC)
    parent = Mock(account_id=plan.account_id, environment="prod")
    parent.read_filled_parent.side_effect = lambda record: record
    return dict(plan=plan, protection_path=journal.path, entry_path=entry,
                parent_broker=parent, stop_broker=stop, release="a" * 64), now


def test_recovery_protects_without_entry_or_cancel(tmp_path):
    args, now = setup_case(tmp_path)
    loaded = load_recovery_plan(args["protection_path"], "real-iis", args["entry_path"])
    assert loaded == args["plan"]
    preview = preview_recovery(**args, now=now)
    gate = Mock()
    assert apply_recovery(**args, preview=preview, confirmed_sha256=recovery_digest(preview),
                          approval_gate=gate, clock=lambda: now) == "ACTIVE"
    assert args["stop_broker"].posts == 1
    assert not args["parent_broker"].submit.called
    assert not args["parent_broker"].cancel.called


@pytest.mark.parametrize("fault", ["expiry", "digest", "holding", "stop", "identity", "revoked"])
def test_recovery_fails_closed_before_post(tmp_path, fault):
    args, now = setup_case(tmp_path)
    preview = preview_recovery(**args, now=now)
    digest = recovery_digest(preview)
    moment = now
    def clock():
        return moment
    gate = Mock()
    if fault == "expiry":
        moment = now + timedelta(minutes=3)
    elif fault == "digest":
        digest = "bad"
    elif fault == "holding":
        args["stop_broker"].units = 20
    elif fault == "stop":
        args["stop_broker"].stops = [{"instrumentUid": args["plan"].instrument.uid}]
    elif fault == "identity":
        args["parent_broker"].account_id = "other"
    else:
        gate.side_effect = ValueError("revoked")
    with pytest.raises(ValueError):
        apply_recovery(**args, preview=preview, confirmed_sha256=digest,
                       approval_gate=gate, clock=clock)
    assert args["stop_broker"].posts == 0


def test_unknown_stop_never_reposts(tmp_path):
    args, now = setup_case(tmp_path)
    preview = preview_recovery(**args, now=now)
    args["stop_broker"].lost = True
    with pytest.raises(TimeoutError):
        apply_recovery(**args, preview=preview, confirmed_sha256=recovery_digest(preview),
                       approval_gate=lambda: None, clock=lambda: now)
    assert args["stop_broker"].posts == 1
    with pytest.raises(ValueError, match="PLANNED"):
        preview_recovery(**args, now=now)
    assert read_parent(args["entry_path"], args["plan"]).filled_lots == 1


def test_predispatch_gate_failure_restores_planned_and_allows_fresh_review(tmp_path):
    args, now = setup_case(tmp_path)
    journal = ProtectionJournal(args["protection_path"])
    def revoked():
        raise ValueError("revoked before dispatch")
    guarded = GuardedProtectionBroker(args["stop_broker"], revoked)
    with pytest.raises(StopNotDispatched):
        journal.activate(args["plan"], args["entry_path"], guarded)
    assert args["stop_broker"].posts == 0
    assert journal._row(args["plan"])[0] == "PLANNED"
    preview = preview_recovery(**args, now=now)
    assert apply_recovery(**args, preview=preview, confirmed_sha256=recovery_digest(preview),
                          approval_gate=lambda: None, clock=lambda: now) == "ACTIVE"


def test_delegated_valueerror_does_not_restore_planned(tmp_path):
    args, _ = setup_case(tmp_path)
    journal = ProtectionJournal(args["protection_path"])
    delegate = Mock(wraps=args["stop_broker"])
    delegate.account_id = "real-iis"
    delegate.environment = "prod"
    delegate.post_stop.side_effect = ValueError("transport/decode failure after dispatch")
    with pytest.raises(ValueError):
        journal.activate(args["plan"], args["entry_path"],
                         GuardedProtectionBroker(delegate, lambda: None))
    assert delegate.post_stop.call_count == 1
    assert journal._row(args["plan"])[0] == "UNKNOWN"


@pytest.mark.parametrize("fault", ["units", "missing_uid"])
def test_final_activation_rechecks_late_state(tmp_path, fault):
    args, now = setup_case(tmp_path)
    preview = preview_recovery(**args, now=now)
    stop = args["stop_broker"]
    if fault == "units":
        stop.available_units = Mock(side_effect=[10, 20])
    else:
        stop.list_stops = Mock(side_effect=[(), ({"stopOrderId": "foreign"},)])
    with pytest.raises(ValueError):
        apply_recovery(**args, preview=preview, confirmed_sha256=recovery_digest(preview),
                       approval_gate=lambda: None, clock=lambda: now)
    assert stop.posts == 0


def test_uncorrelated_post_cannot_be_adopted_on_reconcile(tmp_path):
    args, now = setup_case(tmp_path)
    preview = preview_recovery(**args, now=now)
    args["stop_broker"].response = {"stopOrderId": "known-stop", "orderRequestId": "other"}
    with pytest.raises(ValueError):
        apply_recovery(**args, preview=preview, confirmed_sha256=recovery_digest(preview),
                       approval_gate=lambda: None, clock=lambda: now)
    journal = ProtectionJournal(args["protection_path"])
    assert journal._row(args["plan"])[:2] == ("UNKNOWN", None)
    assert journal.reconcile(args["plan"], args["stop_broker"]) == "UNKNOWN"


@pytest.mark.parametrize("field", ["expirationTime", "expireDate", "expiresAt"])
def test_timed_stop_not_approved_gtc(tmp_path, field):
    args, _ = setup_case(tmp_path)
    row = _stop(args["plan"])
    row[field] = "2000-01-01T00:00:00Z"
    assert not _verified_stop(args["plan"], row)


@pytest.mark.parametrize("fault", ["mixed", "exchange_blocked", "duplicate", "missing"])
def test_native_final_positions_are_exact_complete_and_unblocked(tmp_path, fault):
    args, _ = setup_case(tmp_path)
    row = {"instrumentUid": args["plan"].instrument.uid, "balance": "10", "blocked": "0"}
    if fault == "mixed":
        row.update(balance="20", blocked="10")
    elif fault == "exchange_blocked":
        row["exchangeBlocked"] = True
    elif fault == "missing":
        del row["blocked"]
    transport = Mock()
    transport.post.return_value = {"securities": [row, row] if fault == "duplicate" else [row]}
    native = NativeProtectionBroker("fake", "real-iis", transport)
    with pytest.raises(ValueError):
        native.available_units(args["plan"].instrument)


@pytest.mark.parametrize("fault", ["second_stop", "missing_uid", "child", "activation"])
def test_final_readback_cannot_claim_active_with_conflicting_or_triggered_stop(tmp_path, fault):
    args, now = setup_case(tmp_path)
    preview = preview_recovery(**args, now=now)
    stop = args["stop_broker"]
    def posted(plan, request_id):
        row = _stop(plan)
        stop.stops = [row]
        if fault == "second_stop":
            stop.stops.append(dict(row, stopOrderId="other-stop"))
        elif fault == "missing_uid":
            stop.stops.append({"stopOrderId": "unknown"})
        elif fault == "child":
            row["exchangeOrderId"] = "triggered-child"
        else:
            row["activationDateTime"] = now.isoformat()
        return {"stopOrderId": "known-stop", "orderRequestId": request_id}
    stop.post_stop = Mock(side_effect=posted)
    with pytest.raises(ValueError):
        apply_recovery(**args, preview=preview, confirmed_sha256=recovery_digest(preview),
                       approval_gate=lambda: None, clock=lambda: now)
    assert stop.post_stop.call_count == 1
    journal = ProtectionJournal(args["protection_path"])
    assert journal._row(args["plan"])[0] == "UNKNOWN"


@pytest.mark.parametrize("fault", ["naive", "wrong_parent", "unfilled", "terminal", "changed_id"])
def test_remaining_recovery_guards(tmp_path, monkeypatch, fault):
    args, now = setup_case(tmp_path)
    actual = read_parent(args["entry_path"], args["plan"])
    if fault == "naive":
        now = now.replace(tzinfo=None)
    elif fault == "wrong_parent":
        wrong = replace(actual, intent=replace(actual.intent, side=Side.SELL))
        monkeypatch.setattr(recovery_module, "read_parent", lambda *a: wrong)
    elif fault == "unfilled":
        args["parent_broker"].read_filled_parent.side_effect = None
        args["parent_broker"].read_filled_parent.return_value = replace(actual, filled_lots=0)
    elif fault == "terminal":
        monkeypatch.setattr(recovery_module, "read_parent",
                            lambda *a: replace(actual, status=OrderStatus.CANCELLED))
        args["parent_broker"].read_filled_parent.side_effect = lambda rec: actual
    else:
        preview = preview_recovery(**args, now=now)
        args["parent_broker"].read_filled_parent.side_effect = lambda rec: replace(
            actual, broker_order_id="changed")
        with pytest.raises(ValueError, match="exchange id changed"):
            apply_recovery(**args, preview=preview, confirmed_sha256=recovery_digest(preview),
                           approval_gate=lambda: None, clock=lambda: now)
        return
    with pytest.raises(ValueError):
        preview_recovery(**args, now=now)


@pytest.mark.parametrize("fault", ["state", "hash"])
def test_loader_refuses_changed_local_pair(tmp_path, fault):
    args, _ = setup_case(tmp_path)
    with sqlite3.connect(args["protection_path"]) as db:
        if fault == "state":
            db.execute("UPDATE protection SET state='UNKNOWN'")
        else:
            db.execute("UPDATE protection SET plan_hash='tampered'")
    with pytest.raises(ValueError):
        load_recovery_plan(args["protection_path"], "real-iis", args["entry_path"])


def test_predispatch_reset_cas_and_delegate_marker_are_not_ambiguity_bypasses(tmp_path):
    args, _ = setup_case(tmp_path)
    journal = ProtectionJournal(args["protection_path"])
    def changed_guard():
        journal._state(args["plan"], "UNKNOWN")
        raise ValueError("revoked")
    with pytest.raises(ValueError, match="reservation changed"):
        journal.activate(args["plan"], args["entry_path"],
                         GuardedProtectionBroker(args["stop_broker"], changed_guard))
    journal._state(args["plan"], "PLANNED")
    args["stop_broker"].post_stop = Mock(
        side_effect=StopNotDispatched("delegate cannot prove no POST"))
    with pytest.raises(RuntimeError):
        journal.activate(args["plan"], args["entry_path"],
                         GuardedProtectionBroker(args["stop_broker"], lambda: None))
    assert journal._row(args["plan"])[0] == "UNKNOWN"


def test_protection_rejects_invalid_take_profit_and_timed_expiration(tmp_path):
    args, _ = setup_case(tmp_path)
    replace(args["plan"], take_profit=Decimal("101")).validate()
    with pytest.raises(ValueError):
        replace(args["plan"], take_profit=Decimal("99")).validate()
    row = _stop(args["plan"])
    row["expirationType"] = "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_DATE"
    assert not _verified_stop(args["plan"], row)


@pytest.mark.parametrize("fault", [None, "uid", "side", "lots", "history", "orders",
                                   "blocked", "malformed", "wrong_exchange"])
def test_native_recovery_strict_readonly_parent(tmp_path, fault):
    args, _ = setup_case(tmp_path)
    record = read_parent(args["entry_path"], args["plan"])
    raw = dict(instrumentUid=record.intent.instrument.uid,
               orderRequestId=record.intent.order_request_id,
               direction="ORDER_DIRECTION_BUY", lotsRequested="1", lotsExecuted="1",
               executionReportStatus="EXECUTION_REPORT_STATUS_FILL", orderId="exchange-id")
    if fault == "uid":
        raw["instrumentUid"] = "other"
    elif fault == "side":
        raw["direction"] = "ORDER_DIRECTION_SELL"
    elif fault == "lots":
        raw["lotsExecuted"] = "0"
    elif fault == "wrong_exchange":
        raw["orderId"] = "other-exchange"
    responses = {
        "OperationsService/GetPositions": {"securities": [{
            "instrumentUid": record.intent.instrument.uid, "balance": "10", "blocked": "0"}]},
        "OrdersService/GetOrders": {"orders": [{}] if fault == "orders" else []},
        "StopOrdersService/GetStopOrders": {"stopOrders": [{
            "instrumentUid": record.intent.instrument.uid, "status": "EXECUTED"
        }] if fault == "history" else []},
        "OrdersService/GetOrderState": raw,
    }
    if fault == "blocked":
        responses["OperationsService/GetPositions"]["securities"][0]["exchangeBlocked"] = True
    elif fault == "malformed":
        responses["OrdersService/GetOrders"]["orders"] = "malformed"
    delegate = Mock(account_id="real-iis", environment="prod")
    delegate._post.side_effect = lambda method, payload: responses[method]
    native = NativeRecoveryParentBroker(delegate)
    assert native.environment == "prod"
    if fault is None:
        assert native.read_filled_parent(record).filled_lots == 1
    else:
        with pytest.raises(ValueError):
            native.read_filled_parent(record)
    assert all("Get" in call.args[0] for call in delegate._post.call_args_list)
    assert not delegate.capture.called


@pytest.mark.parametrize("fault", [None, "stale", "crossed", "uid", "empty"])
def test_native_fresh_market_guard(tmp_path, fault):
    args, now = setup_case(tmp_path)
    raw = {"instrumentUid": args["plan"].instrument.uid, "orderbookTs": now.isoformat(),
           "bids": [{"price": {"units": "100", "nano": 0}, "quantity": 10}]}
    if fault == "stale":
        raw["orderbookTs"] = (now - timedelta(seconds=11)).isoformat()
    elif fault == "crossed":
        raw["bids"][0]["price"]["units"] = "94"
    elif fault == "uid":
        raw["instrumentUid"] = "other"
    elif fault == "empty":
        raw["bids"] = []
    delegate = Mock(account_id="real-iis", environment="prod")
    delegate._post.return_value = raw
    delegate.clock.return_value = now
    broker = NativeRecoveryParentBroker(delegate)
    if fault is None:
        broker.validate_market(args["plan"], now)
    else:
        with pytest.raises(ValueError):
            broker.validate_market(args["plan"], now)
