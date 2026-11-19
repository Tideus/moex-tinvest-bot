import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest
from test_performance import _artifact
from test_trader_journal import T2, events, operation

from moex_bot.broker_fees import commission_amount
from moex_bot.domain import Instrument
from moex_bot.integrations.tinvest_sandbox import _trade_operation
from moex_bot.intraday_performance import (
    render_intraday_performance_report,
    summarize_intraday_performance,
)
from moex_bot.journal_import import broker_operation_fields
from moex_bot.long_daily_report import performance
from moex_bot.performance import TradeOperation, render_performance_report, summarize_performance
from moex_bot.trader_journal import Journal


@pytest.mark.parametrize("raw,expected", [
    ({"currency": "", "units": "0", "nano": 0}, D(0)),
    ({"currency": "rub", "units": "0"}, D(0)),
    ({"currency": "rub", "nano": -250000000}, D("0.25")),
    ({"currency": "RUB", "units": "-4", "nano": -878750000}, D("4.87875")),
    (None, None), ({}, None), ({"currency": "rub"}, None), ([], None),
    ({"units": "NaN"}, None), ({"units": "Infinity"}, None),
    ({"units": "1.5"}, None), ({"units": True}, None),
    ({"units": "9223372036854775808"}, None),
    ({"nano": 1000000000}, None), ({"units": "1", "nano": -1}, None),
    ({"units": "0", "currency": None}, None),
    ({"units": "0", "currency": "usd"}, None),
    ({"units": "1", "currency": ""}, None),
])
def test_same_strict_fee_interpretation_everywhere(raw, expected):
    assert commission_amount(raw) == expected
    fields = broker_operation_fields({"commission": raw})
    assert (None if fields["commission"] is None else D(fields["commission"])) == expected
    item = {"id": "synthetic", "date": "2026-11-18T08:00:00Z", "commission": raw,
            "type": "OPERATION_TYPE_BUY", "instrumentUid": "uid"}
    decoded = _trade_operation(item, {"uid": Instrument("T", "uid", "TQBR", 1, D("0.01"))})
    assert decoded.commission == expected


def test_unknown_fee_never_looks_like_net_in_daily_reports(tmp_path):
    day = date(2026, 11, 18)
    stamps = ["2026-11-18T07:00:00+00:00", "2026-11-18T20:59:00+00:00"]
    points = [dict(observed_at=s, reported_equity=e, positions={})
              for s, e in zip(stamps, ["1000", "990"], strict=True)]
    paths = []
    shadow = []
    for i, point in enumerate(points):
        p = tmp_path / f"p{i}.json"
        p.write_text(json.dumps(point))
        paths.append(p)
        s = tmp_path / f"shadow{i}.json"
        _artifact(s, stamps[i], point["reported_equity"], "300", 0)
        shadow.append(s)
    ops = (TradeOperation("b", datetime(2026, 11, 18, 8, tzinfo=UTC),
                          "SBER", "BUY", 1, D(100), None),
           TradeOperation("s", datetime(2026, 11, 18, 9, tzinfo=UTC),
                          "SBER", "SELL", 1, D(90), D(0)))
    intraday = summarize_intraday_performance(paths, [], ops, report_date=day, timezone="UTC")
    assert intraday.pnl == -10  # Observed account delta remains independently known.
    assert intraday.rows[0].cashflow_pnl is None and intraday.rows[0].gross_cashflow == -10
    assert intraday.unknown_commissions == 1 and intraday.unattributed_pnl is None
    assert "net н/д" in render_intraday_performance_report(intraday)
    long = performance(*points, ops, day)
    assert long["pnl"] == -10 and long["rows"]["SBER"]["pnl"] is None
    assert long["commission"] is None and long["unknown_commissions"] == 1
    legacy = summarize_performance(shadow, ops, start_date=day, end_date=day,
                                   timezone="UTC", label="test")
    assert legacy.total_commission is None and legacy.rows[0].pnl is None
    assert not legacy.attribution_reliable
    assert "net н/д" in render_performance_report(legacy, weekly=False)
    known = (replace(ops[0], commission=D(0)), ops[1])
    assert performance(*points, known, day)["rows"]["SBER"]["pnl"] == -10


def test_new_fee_receipt_does_not_rewrite_old_journal_events(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    old = events([operation(fee=None)], at=T2)
    journal.ingest(old)
    original = json.dumps(old, sort_keys=True)
    fresh = operation(fee="0") | broker_operation_fields(
        {"commission": {"currency": "", "units": "0", "nano": 0}})
    journal.ingest(events([fresh], at="2026-11-02T10:00:00+00:00"))
    assert json.dumps(old, sort_keys=True) == original
    # Append-only revision: the old source is still accepted, not overwritten.
    assert journal.ingest(old) == 0
    operations = [r for r in journal.latest() if r["kind"] == "operation"]
    assert len(operations) == 1 and operations[0]["commission"] == "0"
    assert operations[0]["commission_source"] == "broker_reported"
