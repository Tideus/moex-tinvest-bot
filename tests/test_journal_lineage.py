import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import pytest

from moex_bot import journal_lineage as lineage
from moex_bot.journal_cards import make_cards
from moex_bot.journal_import import portfolio_events
from moex_bot.journal_readmodel import compare, publish, query
from moex_bot.trader_journal import build_report, encoded, match_trades, timestamp

ACCOUNT = "private-account"
NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
AT = "2026-11-11T10:00:00+00:00"


def fixture():
    instrument = {"secid": "SBER", "uid": "uid", "lot_size": 10}
    intent = {"instrument": instrument, "side": "buy", "lots": 2,
              "order_request_id": "logical", "limit_price": "100",
              "rationale": "intraday momentum;move=0.01;trade=0.2;order=0.3;book=0.4"}
    plan = {"run_id": "intraday-" + AT, "orders": [{"status": "validated", "intent": intent}],
            "analysis_input": {"portfolio_input": {"account_id": ACCOUNT}},
            "signal_journal": {"decisions": [{"secid": "SBER", "status": "planned",
                "order_request_id": "logical", "candidate": {"side": "buy"}}]}}
    record = {"intent": {**intent, "order_request_id": "request"},
              "filled_lots": 2, "broker_order_id": "exchange", "status": "filled"}
    row = {"account": ACCOUNT, "plan_id": plan["run_id"], "request_id": "request",
           "logical_id": "logical", "record": json.dumps(record),
           "payload_hash": hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()}
    state = {"orderRequestId": "request", "orderId": "exchange", "instrumentUid": "uid",
             "direction": "ORDER_DIRECTION_BUY", "lotsRequested": 2, "lotsExecuted": 2,
             "orderDate": AT, "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
             "stages": []}
    return row, plan, state


def evidence():
    row, plan, _ = fixture()
    return lineage.intraday_evidence(row, plan, ACCOUNT)


def test_exact_reserved_plan_and_request_and_exit():
    row, plan, state = fixture()
    result = lineage.intraday_evidence(row, plan, ACCOUNT)
    lineage.verify_state(result, state, NOW)
    assert result["logical"] != result["request"]
    assert result["intent"]["rationale"] == plan["orders"][0]["intent"]["rationale"]
    plan["signal_journal"] = {"decisions": [], "exit_intents": [{"secid": "SBER",
        "side": "buy", "order_request_id": "logical", "reason": result["intent"]["rationale"]}]}
    assert lineage.intraday_evidence(row, plan, ACCOUNT)["request"] == "request"


@pytest.mark.parametrize("change", ["account", "plan", "hash", "request", "intent",
                                    "signal", "duplicate", "side", "symbol", "exit"])
def test_chain_rejects_mismatch_not_proximity(change):
    row, plan, _ = fixture()
    if change == "account":
        row["account"] = "other"
    elif change == "plan":
        row["plan_id"] = "other"
    elif change == "hash":
        row["payload_hash"] = "wrong"
    elif change == "request":
        row["request_id"] = "other"
    elif change == "intent":
        raw = json.loads(row["record"])
        raw["intent"]["lots"] = 5
        row["record"] = json.dumps(raw)
    elif change == "signal":
        plan["signal_journal"]["decisions"][0]["order_request_id"] = "nearby"
    elif change == "duplicate":
        plan["signal_journal"]["decisions"] *= 2
    elif change == "side":
        plan["signal_journal"]["decisions"][0]["candidate"]["side"] = "sell"
    elif change == "symbol":
        plan["signal_journal"]["decisions"][0]["secid"] = "VTBR"
    else:
        plan["signal_journal"] = {"decisions": [], "exit_intents": [{"secid": "SBER",
            "side": "buy", "order_request_id": "logical", "reason": "wrong reason"}]}
    with pytest.raises(ValueError):
        lineage.intraday_evidence(row, plan, ACCOUNT)


@pytest.mark.parametrize("field,value", [("orderRequestId", "other"), ("orderId", "other"),
    ("instrumentUid", "other"), ("direction", "ORDER_DIRECTION_SELL"), ("lotsRequested", 3),
    ("lotsExecuted", 20), ("lotsExecuted", "NaN"), ("lotsExecuted", -1),
    ("lotsExecuted", 1), ("lotsExecuted", "1.5"), ("orderDate", "2026-11-10T00:00Z"),
    ("orderDate", "2026-11-12T00:00Z")])
def test_broker_identity_quantity_and_clock_fail_closed(field, value):
    _, _, state = fixture()
    state[field] = value
    with pytest.raises(ValueError):
        lineage.verify_state(evidence(), state, NOW)


def test_long_candidate_is_bound_to_bytes_and_uuid_not_symbol():
    _, plan, _ = fixture()
    plan["portfolio_input"] = {"account_id": ACCOUNT}
    raw = json.dumps(plan).encode()
    sha = hashlib.sha256(raw).hexdigest()
    intent = plan["orders"][0]["intent"]
    request = str(uuid5(NAMESPACE_URL, f"long-once/v1/sandbox/{ACCOUNT}/{sha}/logical"))
    candidate = {"account_id": ACCOUNT, "decision_sha256": sha, "order_request_id": request,
                 "instrument": intent["instrument"], "limit_price": "100", "lots": 1}
    record = {"intent": {**intent, "lots": 1, "order_request_id": request},
              "filled_lots": 1, "broker_order_id": "exchange"}
    row = {"candidate": json.dumps(candidate), "record": json.dumps(record)}
    assert lineage.long_evidence(row, raw, ACCOUNT)["intent"]["lots"] == 1
    with pytest.raises(ValueError):
        lineage.long_evidence(row, raw + b" ", ACCOUNT)
    with pytest.raises(ValueError):
        lineage.long_evidence(row, raw, "another-account")
    candidate["order_request_id"] = "not-selected"
    row["candidate"] = json.dumps(candidate)
    with pytest.raises(ValueError):
        lineage.long_evidence(row, raw, ACCOUNT)


def operation_fixture():
    _, _, state = fixture()
    price = {"units": "100", "nano": 0}
    state["stages"] = [{"tradeId": "trade", "quantity": 2, "price": price,
                         "executionTime": AT}]
    op = {"operation_id": "op", "side": "BUY", "secid": "SBER", "quantity": "20",
          "gross": "2000", "commission": "1", "occurred_at": AT,
          "journal_raw_operation_sha256": "a" * 64,
          "journal_trade_identity": {"instrument_uid": "uid", "trades": [{"id": "trade",
                                       "quantity": 20, "price": price, "at": AT}]}}
    receipt = {"evidence": evidence(), "state": state, "observed_at": NOW.isoformat()}
    return receipt, op


@pytest.mark.parametrize("change", ["no_ids", "other_ids", "duplicate", "units", "price",
                                    "time", "uid", "side", "total", "stage_duplicate"])
def test_operation_requires_shared_trade_id_and_full_consistency(change):
    receipt, op = operation_fixture()
    assert lineage.exact_operation(receipt, op)
    raw = op["journal_trade_identity"]
    if change == "no_ids":
        receipt["state"]["stages"] = []
    elif change == "other_ids":
        raw["trades"][0]["id"] = "different-namespace"
    elif change == "duplicate":
        raw["trades"] *= 2
    elif change == "units":
        raw["trades"][0]["quantity"] = 2
    elif change == "price":
        raw["trades"][0]["price"] = {"units": "101", "nano": 0}
    elif change == "time":
        raw["trades"][0]["at"] = "2026-11-11T10:00:01Z"
    elif change == "uid":
        raw["instrument_uid"] = "other"
    elif change == "side":
        op["side"] = "SELL"
    elif change == "total":
        op["quantity"] = "21"
    else:
        receipt["state"]["stages"] *= 2
    assert not lineage.exact_operation(receipt, op)


def test_readonly_ledger_and_prospective_activation(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sample(value TEXT)")
        db.execute("INSERT INTO sample VALUES ('original')")
    before = path.read_bytes()
    assert lineage.read_db(path, "SELECT * FROM sample") == [{"value": "original"}]
    with pytest.raises(sqlite3.OperationalError):
        lineage.read_db(path, "DELETE FROM sample")
    assert before == path.read_bytes()
    assert lineage.activation(tmp_path / "lineage", NOW) == NOW
    assert lineage.activation(tmp_path / "lineage", timestamp("2027-01-01T00:00Z")) == NOW


def test_collector_retries_partial_but_never_submits_and_preserves_receipts(tmp_path, monkeypatch):
    config = {"output": str(tmp_path / "public"), "artifact_root": str(tmp_path)}
    lineage.activation(tmp_path / "lineage", timestamp("2026-11-11T09:00Z"))
    e = evidence()
    e["record"]["filled_lots"] = 0
    monkeypatch.setattr(lineage, "candidates", lambda *a: [e])
    _, _, state = fixture()
    state.update(lotsExecuted=1, executionReportStatus="EXECUTION_REPORT_STATUS_PARTIALLYFILL")
    calls = []
    def lookup(account, request):
        calls.append((account, request))
        return state
    lineage.collect_lineage(config, ACCOUNT, "intraday", lookup, NOW)
    first = list((tmp_path / "lineage/receipts").glob("*.json"))[0].read_bytes()
    state.update(lotsExecuted=2, executionReportStatus="EXECUTION_REPORT_STATUS_FILL")
    lineage.collect_lineage(config, ACCOUNT, "intraday", lookup, NOW)
    lineage.collect_lineage(config, ACCOUNT, "intraday", lookup, NOW)
    assert calls == [(ACCOUNT, "request")] * 2
    assert first in [p.read_bytes() for p in (tmp_path / "lineage/receipts").glob("*.json")]


def test_collector_reports_failure_without_fabricating_zero(tmp_path, monkeypatch):
    config = {"output": str(tmp_path / "public"), "artifact_root": str(tmp_path)}
    lineage.activation(tmp_path / "lineage", timestamp("2026-11-11T09:00Z"))
    monkeypatch.setattr(lineage, "candidates", lambda *a: [evidence()])
    def failed(*args):
        raise TimeoutError("SECRET ACCOUNT")
    lineage.collect_lineage(config, ACCOUNT, "intraday", failed, NOW)
    status = lineage.load(tmp_path / "lineage/intraday-status.json")
    assert status["errors"] == ["broker_order_state:TimeoutError"]
    assert "SECRET" not in encoded(status)
    assert not list((tmp_path / "lineage/receipts").glob("*.json"))


def test_sandbox_share_count_is_visible_unconfirmed_not_fabricated_link(tmp_path, monkeypatch):
    config = {"output": str(tmp_path / "public"), "artifact_root": str(tmp_path)}
    lineage.activation(tmp_path / "lineage", timestamp("2026-11-11T09:00Z"))
    e = evidence()
    monkeypatch.setattr(lineage, "candidates", lambda *a: [e])
    _, _, state = fixture()
    state["lotsExecuted"] = "20"
    original = deepcopy(state)
    lineage.collect_lineage(config, ACCOUNT, "intraday", lambda *a: state, NOW)
    status = lineage.load(tmp_path / "lineage/intraday-status.json")
    assert status["errors"] == []
    assert status["verified"] == 0
    assert status["unconfirmed"][0]["code"] == "sandbox_quantity_units_mismatch"
    assert status["unconfirmed"][0]["reported_executed"] == "20"
    assert status["unconfirmed"][0]["requested_lots"] == "2"
    assert state == original
    assert ACCOUNT not in encoded(status) and '"request"' not in encoded(status)
    assert not list((tmp_path / "lineage/receipts").glob("*.json"))
    # A corrected broker response can be verified on the next cycle; no quarantine lockout.
    state["lotsExecuted"] = "2"
    lineage.collect_lineage(config, ACCOUNT, "intraday", lambda *a: state, NOW)
    status = lineage.load(tmp_path / "lineage/intraday-status.json")
    assert status["verified"] == 1 and status["unconfirmed"] == []


@pytest.mark.parametrize("change", ["request", "uid", "status", "quantity", "ledger", "strategy"])
def test_quantity_anomaly_classification_never_hides_other_corruption(change):
    e = evidence()
    _, _, state = fixture()
    state["lotsExecuted"] = "20"
    if change == "request":
        state["orderRequestId"] = "wrong"
    elif change == "uid":
        state["instrumentUid"] = "wrong"
    elif change == "status":
        state["executionReportStatus"] = "EXECUTION_REPORT_STATUS_PARTIALLYFILL"
    elif change == "quantity":
        state["lotsExecuted"] = "21"
    elif change == "ledger":
        e["record"]["filled_lots"] = 0
    else:
        e["strategy"] = "long"
    with pytest.raises(ValueError) as error:
        lineage.verify_state(e, state, NOW)
    assert not isinstance(error.value, lineage.SandboxQuantityUnavailable)


def test_future_lineage_public_projection_keeps_old_cards_and_financials(tmp_path):
    receipt, op = operation_fixture()
    config = {"output": str(tmp_path / "public"), "collector_dir": str(tmp_path / "receipts")}
    lineage.activation(tmp_path / "lineage", timestamp("2026-11-11T09:00Z"))
    (tmp_path / "lineage/receipts").mkdir()
    (tmp_path / "lineage/receipts/new.json").write_text(encoded(receipt))
    (tmp_path / "receipts").mkdir()
    profile = {"id": "intraday", "environment": "sandbox"}
    instruments = {"SBER": {"asset_class": "share", "lot_size_verified": 10}}
    base = {"account_id": ACCOUNT, "source": "t_invest_sandbox", "positions": {},
            "reported_equity": "10000", "observed_at": "2026-11-11T08:00Z"}
    events = portfolio_events(base, profile, instruments, "baseline")
    raw = {**base, "observed_at": NOW.isoformat(), "executed_operations": [op]}
    (tmp_path / "receipts/intraday-new.json").write_text(encoded(raw))
    events += portfolio_events(raw, profile, instruments, "new")
    report = build_report(events)
    before = deepcopy(report)
    lineage.attach_lineage(report, events, config)
    stream = report["streams"][0]
    cards = make_cards(stream)
    assert cards[0]["operation_reason"]["status"] == "verified_execution"
    assert cards[1]["kind"] == "broker_order"
    assert cards[1]["markers"] == []  # orderDate is never fabricated as a fill timestamp.
    assert "gross" not in cards[1]
    for key, value in before["streams"][0].items():
        assert stream[key] == value
    public = encoded(report)
    for secret in (ACCOUNT, '"request"', '"exchange"', '"logical"', '"trade"'):
        assert secret not in public
    path = tmp_path / "readmodel.sqlite3"
    generation = publish(report, path)
    assert compare(report, path)["rows"] > 0
    detail = query(path, "detail", {"stream": stream["id"], "period": "2026-11-11",
                    "symbol": "SBER", "generation": generation, "ref": cards[1]["ref"]})
    assert detail["card"]["operation_reason"]["status"] == "verified_order"
    # A LONG/operation-only stream uses the same exact anchor as the UI and deploy verifier.
    operation = stream["operations"][0]
    detail = query(path, "detail", {"stream": stream["id"], "period": "2026-11-11",
        "symbol": "SBER", "generation": generation,
        "anchor": encoded({"kind": "operation", "at": operation["at"],
                           "side": operation["side"]})})
    assert detail["card"]["kind"] == "operation"
    assert detail["card"]["operation"]["quantity"] == operation["quantity"]
    # Activation after both decision and operation: every historical card remains byte-identical.
    (tmp_path / "lineage/activation.json").write_text(encoded({
        "protocol": "execution-lineage-v1", "from": "2026-11-11T11:00Z"}))
    historical = deepcopy(before)
    lineage.attach_lineage(historical, events, config)
    assert make_cards(historical["streams"][0]) == make_cards(before["streams"][0])


def test_persisted_plan_scan_rejects_old_decisions_even_if_file_copied_today(tmp_path):
    row, plan, _ = fixture()
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts/intraday-plan-old.json").write_text(encoded(plan))
    assert lineage.candidates(tmp_path, "intraday", ACCOUNT, NOW) == []
    (tmp_path / "execution").mkdir()
    with sqlite3.connect(tmp_path / "execution/orders.sqlite3") as db:
        db.execute("CREATE TABLE actions(account,plan_id,request_id,logical_id,"
                   "record,payload_hash)")
        db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?)", tuple(row.values()))
    results = lineage.candidates(tmp_path, "intraday", ACCOUNT, timestamp("2026-11-11T09:00Z"))
    assert len(results) == 1
    assert results[0]["request"] == "request"


def test_fifo_partial_and_short_reversal_keep_exact_operation_ids():
    base = {"at": "2026-11-11T09:00Z", "positions": {}}
    def op(identity, side, at, qty, price):
        return {"identity": identity, "side": side, "at": at, "quantity": str(qty),
                "gross": str(qty * price), "symbol": "SBER", "asset_class": "share",
                "commission": "1"}
    ops = [op("buy-a", "BUY", "2026-11-11T10:00Z", 20, 100),
           op("sell-b", "SELL", "2026-11-11T11:00Z", 10, 101),
           op("sell-c", "SELL", "2026-11-11T12:00Z", 20, 102),
           op("buy-d", "BUY", "2026-11-11T13:00Z", 10, 99)]
    expected = match_trades(ops, base)
    pairs = []
    assert match_trades(ops, base, lineage_pairs=pairs) == expected
    assert pairs == [("buy-a", "sell-b"), ("buy-a", "sell-c"), ("sell-c", "buy-d")]
