"""Native stop tests use only persisted local entry journals and fake broker IO."""

import json
import sqlite3
from dataclasses import replace
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from test_live_execution import _setup
from test_tinvest_sandbox import RecordingTransport

from moex_bot.live_execution import execute_live_intent, intent_digest
from moex_bot.live_protection import (
    NativeProtectionBroker,
    ProtectionJournal,
    ProtectionPlan,
    _verified_stop,
)


def _case(tmp_path, *, fill=True, kind="stop_loss_market"):
    entry_broker, kw, _ = _setup(tmp_path)
    entry = replace(kw["intent"], lots=1, notional=Decimal("1000"))
    kw["intent"] = entry
    kw["permission"] = replace(kw["permission"], intent_sha256=intent_digest(entry))
    entry_broker.state = replace(entry_broker.state, estimated_total=Decimal("1001"))
    request = str(uuid5(NAMESPACE_URL, "prod:real-iis:" + entry.order_request_id))
    plan = ProtectionPlan(
        "real-iis",
        entry.order_request_id,
        request,
        entry.instrument,
        1,
        Decimal(100),
        Decimal(95),
        Decimal(94) if kind == "stop_limit" else None,
        kind,
    )
    journal = ProtectionJournal(tmp_path / "protection.sqlite3")
    journal.prepare(plan, kw["journal_path"])
    if fill:
        execute_live_intent(**kw)

    class Broker:
        account_id = "real-iis"
        environment = "prod"
        units = 10
        stops = []
        posts = 0
        lost = False
        response = None

        def available_units(self, instrument):
            return self.units

        def list_stops(self):
            return tuple(self.stops)

        def post_stop(self, plan, request_id):
            self.posts += 1
            if self.lost:
                raise TimeoutError("lost stop POST response")
            self.stops = [_stop(plan)]
            return (
                self.response
                if self.response is not None
                else {"stopOrderId": "known-stop", "orderRequestId": request_id}
            )

    return plan, journal, kw["journal_path"], Broker()


def _stop(plan):
    row = {
        "stopOrderId": "known-stop",
        "instrumentUid": plan.instrument.uid,
        "lotsRequested": "1",
        "direction": "STOP_ORDER_DIRECTION_SELL",
        "currency": "rub",
        "orderType": "STOP_ORDER_TYPE_STOP_LOSS"
        if plan.kind == "stop_loss_market"
        else "STOP_ORDER_TYPE_STOP_LIMIT",
        "status": "STOP_ORDER_STATUS_ACTIVE",
        "exchangeOrderType": "EXCHANGE_ORDER_TYPE_MARKET"
        if plan.kind == "stop_loss_market"
        else "EXCHANGE_ORDER_TYPE_LIMIT",
        "stopPrice": {"units": "95", "currency": "rub"},
    }
    if plan.stop_limit is not None:
        row["price"] = {"units": "94", "currency": "rub"}
    return row


@pytest.mark.parametrize("kind", ["stop_loss_market", "stop_limit"])
def test_protection_is_prepared_before_entry_and_active_only_after_exact_readback(tmp_path, kind):
    plan, journal, entry, broker = _case(tmp_path, kind=kind)
    assert journal._row(plan)[0] == "PLANNED"
    assert journal.prepare(plan, entry) == plan.request_id
    assert journal.activate(plan, entry, broker) == "ACTIVE"
    assert broker.posts == 1
    assert journal.activate(plan, entry, broker) == "ACTIVE" and broker.posts == 1
    assert ProtectionJournal(journal.path)._row(plan)[0] == "ACTIVE"
    with pytest.raises(ValueError, match="blocks another"):
        journal.prepare(
            replace(plan, entry_request_id=str(uuid4()), entry_logical_id=str(uuid4())), entry
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", ""),
        ("lots", 2),
        ("lots", True),
        ("entry_logical_id", "bad"),
        ("entry_request_id", "bad"),
        ("entry_price", Decimal("NaN")),
        ("stop_trigger", Decimal(0)),
        ("stop_trigger", Decimal("95.001")),
        ("stop_trigger", Decimal(101)),
        ("kind", "trailing"),
        ("stop_limit", Decimal(94)),
        ("kind", "stop_limit"),
    ],
)
def test_invalid_plan_never_posts(tmp_path, field, value):
    plan, _, _, broker = _case(tmp_path)
    with pytest.raises(ValueError):
        replace(plan, **{field: value}).validate()
    assert not broker.posts


def test_instrument_and_limit_validation(tmp_path):
    plan, journal, entry, _ = _case(tmp_path)
    for instrument in (
        replace(plan.instrument, currency="USD"),
        replace(plan.instrument, asset_class="future"),
    ):
        with pytest.raises(ValueError):
            replace(plan, instrument=instrument).validate()
    with pytest.raises(ValueError):
        replace(plan, kind="stop_limit", stop_limit=Decimal(96)).validate()
    with pytest.raises(ValueError, match="changed"):
        journal.prepare(replace(plan, stop_trigger=Decimal(90)), entry)
    with pytest.raises(ValueError, match="separate"):
        journal.path = entry
        journal.prepare(plan, entry)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "partial",
        "sell",
        "logical",
        "quantity",
        "price",
        "owned",
        "free",
        "manual",
        "broker",
        "enrollment",
        "no-plan",
    ],
)
def test_activation_requires_durable_parent_and_exact_free_owned_units(tmp_path, fault):
    plan, journal, entry, broker = _case(tmp_path, fill=fault != "missing")
    if fault in {"partial", "sell", "logical", "quantity", "price", "owned"}:
        with sqlite3.connect(entry) as db:
            raw = json.loads(db.execute("SELECT record FROM actions").fetchone()[0])
            if fault == "partial":
                raw["status"] = "partially_filled"
            elif fault in {"sell", "owned"}:
                raw["intent"]["side"] = "sell"
            elif fault == "quantity":
                raw["filled_lots"] = 2
            elif fault == "price":
                raw["intent"]["limit_price"] = "99"
            else:
                db.execute("UPDATE actions SET logical_id='wrong'")
            if fault == "owned":
                db.execute(
                    "INSERT INTO actions SELECT account,'sold',2,'sold','hash','sold','filled',"
                    "snapshot_at,completed_at,? FROM actions",
                    (json.dumps(raw),),
                )
            else:
                db.execute("UPDATE actions SET record=?", (json.dumps(raw),))
    elif fault == "free":
        broker.units = 0
    elif fault == "manual":
        broker.stops = [_stop(plan)]
    elif fault == "broker":
        broker.environment = "sandbox"
    elif fault == "enrollment":
        with sqlite3.connect(entry) as db:
            db.execute("UPDATE metadata SET value='changed' WHERE key='live_journal_uuid'")
    elif fault == "no-plan":
        plan = replace(plan, entry_request_id=str(uuid4()))
    with pytest.raises(ValueError):
        journal.activate(plan, entry, broker)
    assert not broker.posts


def test_lost_post_remains_unknown_without_blind_retry_or_manual_adoption(tmp_path):
    plan, journal, entry, broker = _case(tmp_path)
    broker.lost = True
    with pytest.raises(TimeoutError):
        journal.activate(plan, entry, broker)
    broker.lost = False
    broker.stops = [_stop(plan)]
    assert journal.activate(plan, entry, broker) == "UNKNOWN"
    assert journal.reconcile(plan, broker) == "UNKNOWN" and broker.posts == 1


@pytest.mark.parametrize("response", [{}, {"stopOrderId": "known-stop"}, {"stopOrderId": 7}])
def test_post_response_missing_identity_stays_blocked(tmp_path, response):
    plan, journal, entry, broker = _case(tmp_path)
    broker.response = response
    with pytest.raises(ValueError, match="identity"):
        journal.activate(plan, entry, broker)
    assert journal._row(plan)[0] == "UNKNOWN"


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "STOP_ORDER_STATUS_EXECUTED"),
        ("currency", "usd"),
        ("instrumentUid", "manual"),
        ("lotsRequested", "2"),
        ("direction", "STOP_ORDER_DIRECTION_BUY"),
        ("stopPrice", {}),
        ("stopPrice", None),
        ("stopPrice", {"currency": "usd", "units": "95"}),
        ("stopPrice", {"currency": "rub", "units": "90"}),
    ],
)
def test_readback_never_claims_mismatched_or_triggered_stop_active(tmp_path, field, value):
    plan, journal, entry, broker = _case(tmp_path)
    assert journal.activate(plan, entry, broker) == "ACTIVE"
    broker.stops[0][field] = value
    with pytest.raises(ValueError):
        journal.reconcile(plan, broker)
    assert journal._row(plan)[0] == "UNKNOWN"
    broker.account_id = "other"
    with pytest.raises(ValueError):
        journal.reconcile(plan, broker)


def test_corrupt_existing_journal_is_not_reinitialized(tmp_path):
    path = tmp_path / "protection"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE metadata(key,value)")
        db.execute("INSERT INTO metadata VALUES('format','unknown')")
    with pytest.raises(ValueError, match="restore"):
        ProtectionJournal(path)


@pytest.mark.parametrize("kind", ["stop_loss_market", "stop_limit"])
def test_fixed_production_transport_fields_and_read_only_positions(tmp_path, kind):
    plan, _, _, _ = _case(tmp_path, kind=kind)
    transport = RecordingTransport(
        {"securities": [{"instrumentUid": plan.instrument.uid, "balance": "20", "blocked": "10"}]}
    )
    broker = NativeProtectionBroker("fake", plan.account_id, transport)
    assert broker.available_units(plan.instrument) == 10
    transport.response = {"stopOrders": [_stop(plan)]}
    assert _verified_stop(plan, broker.list_stops()[0])
    broker.post_stop(plan, plan.request_id)
    assert transport.call["url"].startswith("https://invest-public-api.tbank.ru/rest/")
    payload = transport.call["payload"]
    assert payload["quantity"] == "1" and payload["confirmMarginTrade"] is False
    assert payload["orderId"] == plan.request_id
    assert ("price" in payload) == (kind == "stop_limit")


@pytest.mark.parametrize(
    "response",
    [
        {"limitsLoadingInProgress": True},
        {"securities": {}},
        {"securities": [None]},
        {},
        {"securities": [{"instrumentUid": "uid-sber", "balance": "0", "blocked": "1"}]},
    ],
)
def test_native_position_guards(tmp_path, response):
    plan, _, _, _ = _case(tmp_path)
    with pytest.raises(ValueError):
        NativeProtectionBroker(
            "fake", plan.account_id, RecordingTransport(response)
        ).available_units(plan.instrument)


def test_native_credentials_stop_schema_and_binding_guards(tmp_path):
    plan, _, _, _ = _case(tmp_path)
    with pytest.raises(ValueError):
        NativeProtectionBroker("", plan.account_id, RecordingTransport())
    broker = NativeProtectionBroker(
        "fake", plan.account_id, RecordingTransport({"stopOrders": [None]})
    )
    with pytest.raises(ValueError):
        broker.list_stops()
    with pytest.raises(ValueError):
        broker.post_stop(plan, str(uuid4()))


def test_submission_reservation_failure_never_posts(tmp_path):
    plan, journal, entry, broker = _case(tmp_path)
    with sqlite3.connect(journal.path) as db:
        db.execute(
            "CREATE TRIGGER reject_reservation BEFORE UPDATE ON protection "
            "WHEN NEW.state='POST_PENDING' BEGIN SELECT RAISE(IGNORE); END"
        )
    with pytest.raises(ValueError, match="already reserved"):
        journal.activate(plan, entry, broker)
    assert broker.posts == 0 and journal._row(plan)[0] == "PLANNED"


def test_other_instrument_fills_do_not_count_toward_protected_ownership(tmp_path):
    plan, journal, entry, broker = _case(tmp_path)
    with sqlite3.connect(entry) as db:
        raw = json.loads(db.execute("SELECT record FROM actions").fetchone()[0])
        raw["intent"]["instrument"]["uid"] = "other-uid"
        db.execute(
            "INSERT INTO actions SELECT account,'other',2,'other','hash','other','filled',"
            "snapshot_at,completed_at,? FROM actions",
            (json.dumps(raw),),
        )
    assert journal.activate(plan, entry, broker) == "ACTIVE"


def test_prepare_rejects_changed_enrollment_before_new_entry(tmp_path):
    plan, journal, entry, _ = _case(tmp_path)
    with sqlite3.connect(entry) as db:
        db.execute("UPDATE metadata SET value='new-enrollment' WHERE key='live_journal_uuid'")
    with pytest.raises(ValueError, match="another enrollment"):
        journal.prepare(plan, entry)


def test_strict_lot_types_and_lossless_broker_prices(tmp_path):
    plan, _, _, _ = _case(tmp_path)
    with pytest.raises(ValueError):
        replace(plan, lots=1.0).validate()
    with pytest.raises(ValueError):
        replace(plan, instrument=replace(plan.instrument, lot_size=True)).validate()
    fractional = replace(plan.instrument, tick_size=Decimal(".0000000001"))
    with pytest.raises(ValueError, match="nanos"):
        replace(plan, instrument=fractional, stop_trigger=Decimal("95.0000000001")).validate()


@pytest.mark.parametrize(
    "balance,blocked", [(True, 0), (1.0, 0), ("1.0", 0), ("10", False), (10, 0)]
)
def test_native_quantities_never_coerce_boolean_or_fractional_units(tmp_path, balance, blocked):
    plan, _, _, _ = _case(tmp_path)
    response = {
        "securities": [
            {"instrumentUid": plan.instrument.uid, "balance": balance, "blocked": blocked}
        ]
    }
    broker = NativeProtectionBroker("fake", plan.account_id, RecordingTransport(response))
    if type(balance) is int and type(blocked) is int:
        assert broker.available_units(plan.instrument) == 10
    else:
        with pytest.raises(ValueError, match="integers"):
            broker.available_units(plan.instrument)


@pytest.mark.parametrize("kind", ["stop_loss_market", "stop_limit"])
@pytest.mark.parametrize("child", [None, "EXCHANGE_ORDER_TYPE_UNSPECIFIED", 0, "0", "wrong"])
def test_non_takeprofit_optional_child_type_is_verified_by_documented_stop_type(
    tmp_path, kind, child
):
    plan, _, _, _ = _case(tmp_path, kind=kind)
    row = _stop(plan)
    if child is None:
        row.pop("exchangeOrderType")
    else:
        row["exchangeOrderType"] = child
    assert _verified_stop(plan, row) is (child != "wrong")
    row["orderType"] = "STOP_ORDER_TYPE_TAKE_PROFIT"
    assert not _verified_stop(plan, row)
