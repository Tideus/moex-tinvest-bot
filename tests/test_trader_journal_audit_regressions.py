import hashlib
import json
from decimal import Decimal

import pytest
from test_trader_journal import PROFILE, SPEC, T0, T1, T2, events, operation, point

from moex_bot.journal_import import (
    broker_operation_fields,
    collect,
    import_artifacts,
    portfolio_events,
)
from moex_bot.trader_journal import Journal, build_report, digest


def test_closed_history_coverage_is_not_equity_coverage_or_quantity_reconciliation():
    complete = [
        operation("first_buy", "2026-11-02T07:10:00Z", "BUY", "10", "3000", "0"),
        operation("first_sell", "2026-11-02T07:20:00Z", "SELL", "10", "3100", "0"),
        operation("second_buy", T1, "BUY", "10", "3200", "0"),
        operation("second_sell", T2, "SELL", "10", "3300", "0"),
    ]
    rows = events([]) + events(complete[2:], at=T2, equity="10200")
    stream = build_report(rows)["streams"][0]
    stats = stream["periods"]["2026-11"]
    assert not stream["warnings"]  # A missing whole round trip passes quantity reconciliation.
    assert stats["summary"]["gross"]["pnl"] == "100"  # Known closures remain usable.
    assert stats["trade_history_coverage"] == {
        "status": "unverified", "from": T0, "through": T2,
        "reason": "operation_coverage_missing",
    }
    assert stats["account"]["coverage"] is False
    rows = events([]) + events(complete, at=T2, equity="10200")
    rows[1].update(coverage_from=T0, coverage_to=T2)
    stream = build_report(rows)["streams"][0]
    assert stream["trade_history_coverage"]["status"] == "verified"
    assert stream["periods"]["2026-11"]["summary"]["gross"]["pnl"] == "200"


def test_trade_coverage_needs_no_second_equity_valuation_and_respects_gaps():
    rows = events([])
    rows[0].update(coverage_from=T0, coverage_to=T0)
    stats = build_report(rows)["streams"][0]["periods"]["2026-11-02"]
    assert stats["account"]["window"] is None
    assert stats["trade_history_coverage"]["status"] == "verified"
    rows += events([], at=T2)
    rows[-1].update(coverage_from=T1, coverage_to=T2)
    assert build_report(rows)["streams"][0]["trade_history_coverage"] == {
        "status": "incomplete", "from": T0, "through": T2,
        "reason": "operation_coverage_gap",
    }


def test_trade_coverage_uses_fifo_baseline_before_selected_period():
    rows = events([], at="2026-10-31T08:00:00Z") + events([], at=T2)
    rows[1].update(coverage_from=T0, coverage_to=T2)
    stats = build_report(rows)["streams"][0]["periods"]["2026-11-02"]
    assert stats["trade_history_coverage"]["from"] == "2026-10-31T08:00:00+00:00"
    assert stats["trade_history_coverage"]["status"] == "incomplete"


@pytest.mark.parametrize("commission,expected_source", [
    (None, "broker_missing"), ({}, "broker_invalid"),
    ({"units": "NaN"}, "broker_invalid"),
    ({"units": "1", "currency": "usd"}, "broker_invalid"),
])
def test_journal_raw_missing_or_invalid_commission_is_unknown(commission, expected_source):
    fields = broker_operation_fields({"commission": commission})
    assert fields["commission"] is None
    assert fields["journal_commission_source"] == expected_source


def test_journal_raw_explicit_zero_and_price_are_preserved():
    broker_item = {
        "commission": {"units": "0", "nano": 0, "currency": "rub"},
        "price": {"units": "300", "nano": 500000000, "currency": "rub"},
    }
    fields = broker_operation_fields(broker_item)
    assert fields == {"commission": "0", "journal_commission_source": "broker_reported",
                      "journal_price": "300.5", "journal_raw_operation_sha256": digest(broker_item)}
    raw = point(at=T2)
    raw["executed_operations"] = [operation() | fields]
    rows = portfolio_events(raw, PROFILE, SPEC, "synthetic")
    displayed = build_report(events([]) + rows)["streams"][0]["operations"][0]
    assert displayed["price"] == "300.5" and displayed["price_source"] == "broker"
    assert displayed["commission_source"] == "broker_reported"
    assert rows[1]["raw_operation_sha256"] == digest(broker_item)
    assert "raw_operation_sha256" not in displayed


def test_legacy_zero_remains_unverified_and_vwap_is_explicitly_calculated(tmp_path):
    legacy_operation = operation(fee="0")
    legacy_operation.pop("journal_commission_source")
    rows = events([legacy_operation], at=T2)
    journal = Journal(tmp_path / "source.sqlite3")
    journal.ingest(rows)
    assert "commission_source" not in rows[1] and "price" not in rows[1]
    op = build_report(rows)["streams"][0]["operations"][0]
    assert op["commission"] == "0" and op["commission_source"] == "legacy_unverified"
    assert op["price"] == "300" and op["price_source"] == "calculated_vwap"
    assert journal.ingest(rows) == 0  # No changed source bytes/conflicting historical revisions.
    rows[1]["quantity"] = "0"
    op = build_report(rows)["streams"][0]["operations"][0]
    assert op["price"] is None and op["price_source"] == "unknown"


def test_collector_preserves_missing_commission_without_changing_shared_decoder(
    tmp_path, monkeypatch
):
    from moex_bot.integrations import tinvest_sandbox

    universe = tmp_path / "universe.json"
    universe.write_text(json.dumps([
        {"secid": "SBER", "t_invest_uid": "uid", "board": "TQBR", **SPEC["SBER"]}
    ]))
    raw = {"id": "synthetic-buy", "date": T1, "type": "OPERATION_TYPE_BUY",
           "instrumentUid": "uid", "payment": {"units": "-3000", "nano": 0},
           "tradesInfo": {"trades": [{"quantity": 10,
                                      "price": {"units": "300", "nano": 0}}]}}
    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", "synthetic")
    monkeypatch.setenv("TEST_ACCOUNT", "synthetic-account")
    monkeypatch.setattr(tinvest_sandbox.UrlLibJsonTransport, "post",
                        lambda *args, **kwargs: {"items": [raw], "hasNext": False})

    class Snapshot:
        def as_portfolio_payload(self):
            return point()

    monkeypatch.setattr(tinvest_sandbox.TInvestSandboxAccountService, "broker_snapshot",
                        lambda *args, **kwargs: Snapshot())
    collect({"universe": str(universe), "collector_dir": str(tmp_path / "receipts"),
             "output": str(tmp_path / "public"), "artifact_root": str(tmp_path),
             "history_from": T0,
             "profiles": [{**PROFILE, "account_env": "TEST_ACCOUNT"}]})
    receipt = json.loads(next((tmp_path / "receipts").glob("*.json")).read_bytes())
    operation_row = receipt["executed_operations"][0]
    assert operation_row["commission"] is None
    assert operation_row["journal_commission_source"] == "broker_missing"
    assert operation_row["journal_raw_operation_sha256"] == digest(raw)
    # The shared strategy adapter still has its original behavior.
    from moex_bot.domain import Instrument
    instrument = Instrument("SBER", "uid", "TQBR", 10, Decimal("0.01"))
    assert tinvest_sandbox._trade_operation(raw, {"uid": instrument}).commission == 0


@pytest.mark.parametrize("known_time", [False, True])
def test_order_uses_execution_evidence_or_is_explicitly_omitted(tmp_path, known_time):
    (tmp_path / "universe.json").write_text(json.dumps([{"secid": "SBER", **SPEC["SBER"]}]))
    (tmp_path / "intraday-portfolio-test.json").write_text(json.dumps(
        point(at="2026-11-02T20:59:55Z")
    ))
    order = {"intent": {"instrument": {"secid": "SBER"}}, "status": "filled", "filled_lots": 1}
    execution = {"run_id": "intraday-2026-11-02T21:00:05Z", "submitted": [order]}
    if known_time:
        order["submitted_at"] = "2026-11-02T21:00:10Z"
        # A run's earlier observation must not supersede a valid completion receipt.
        execution["observed_at"] = "2026-11-02T20:59:55Z"
        execution["completed_at"] = "2026-11-02T21:00:15Z"
    payload = json.dumps(execution).encode()
    (tmp_path / "intraday-execution-test.json").write_bytes(payload)
    journal = Journal(tmp_path / "source.sqlite3")
    status = import_artifacts({
        "artifact_root": str(tmp_path), "universe": str(tmp_path / "universe.json"),
        "profiles": [{**PROFILE, "portfolio_globs": ["intraday-portfolio-test.json"]}],
    }, journal)
    assert not status["errors"]
    orders = [r for r in journal.latest() if r["kind"] == "order"]
    if known_time:
        assert not status["diagnostics"] and len(orders) == 1
        assert orders[0]["at"] == "2026-11-02T21:00:10+00:00"
        assert orders[0]["known_at"] == "2026-11-02T21:00:15+00:00"
        assert orders[0]["source_sha256"] == hashlib.sha256(payload).hexdigest()
        assert orders[0]["time_status"] == "known"
    else:
        assert not orders and "timestamp missing" in status["diagnostics"][0]


def test_legacy_snapshot_dated_order_not_republished_as_real_event():
    rows = events([])
    rows.append({**rows[0], "kind": "order", "identity": "old-order", "symbol": "SBER",
                 "status": "filled", "filled_lots": 1})
    stream = build_report(rows)["streams"][0]
    assert stream["orders"] == []
    assert stream["order_diagnostics"]["unknown_time"] == 1


@pytest.mark.parametrize("legacy_side", ["BUY", "SELL", "BOTH"])
def test_legacy_zero_never_certifies_net_or_account_gross(tmp_path, legacy_side):
    buy = operation("buy", T1, "BUY", "10", "3000", "0")
    sell = operation("sell", T2, "SELL", "10", "3100", "0")
    for op in (buy, sell):
        if legacy_side in {op["side"], "BOTH"}:
            op.pop("journal_commission_source")
    rows = events([]) + events([buy, sell], at=T2, equity="10100")
    rows[1].update(coverage_from=T0, coverage_to=T2)
    journal = Journal(tmp_path / "source.sqlite3")
    journal.ingest(rows)
    before = journal.latest()
    stream = build_report(before)["streams"][0]
    trade = stream["trades"][0]
    assert trade["gross"] == "100" and trade["quantity"] == "10"
    assert trade["entry"] == "300" and trade["exit"] == "310"
    assert trade["net"] is None and trade["net_return_pct"] is None and trade["fees"] is None
    stats = stream["periods"]["2026-11-02"]
    assert stats["gross"] == "100" and stats["net"] is None
    assert stats["summary"]["net"]["pnl"] is None
    assert stats["summary"]["net"]["win_rate_pct"] is None
    assert stats["summary"]["net"]["profit_factor_state"] == "unknown"
    assert stats["summary"]["gross"]["fees"] is None
    assert stats["account"]["gross"] is None
    assert stats["account"]["net"] == "100"
    assert Decimal(stats["account"]["return_pct"]) == 1
    assert all(op["commission"] == "0" for op in stream["operations"])
    assert journal.latest() == before and journal.ingest(rows) == 0


@pytest.mark.parametrize("fee,legacy", [("0", False), ("3", True)])
def test_confirmed_zero_and_positive_legacy_fees_remain_known(fee, legacy):
    buy = operation("buy", T1, "BUY", "10", "3000", fee)
    sell = operation("sell", T2, "SELL", "10", "3100", fee)
    if legacy:
        for op in (buy, sell):
            op.pop("journal_commission_source")
    else:
        for op in (buy, sell):
            op.update(broker_operation_fields({
                "commission": {"units": "0", "nano": 0, "currency": "rub"}
            }))
    rows = events([]) + events([buy, sell], at=T2, equity="10100")
    rows[1].update(coverage_from=T0, coverage_to=T2)
    stream = build_report(rows)["streams"][0]
    expected_fees = Decimal(fee) * 2
    trade = stream["trades"][0]
    assert Decimal(trade["fees"]) == expected_fees
    assert Decimal(trade["net"]) == 100 - expected_fees
    assert Decimal(trade["net_return_pct"]) == (100 - expected_fees) / 3000 * 100
    account = stream["periods"]["2026-11-02"]["account"]
    assert Decimal(account["gross"]) == 100 + expected_fees


def test_new_raw_provenance_hash_is_validated_without_requiring_it_on_legacy():
    raw = point(at=T2)
    raw["executed_operations"] = [operation() | {"journal_raw_operation_sha256": "not-a-hash"}]
    with pytest.raises(ValueError, match="checksum"):
        portfolio_events(raw, PROFILE, SPEC, "synthetic")
