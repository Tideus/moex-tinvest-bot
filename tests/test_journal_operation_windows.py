"""Mutable broker IDs must not double historical executions; no inferred aliases."""
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from test_trader_journal import PROFILE, SPEC, T0, T1, T2, events, operation, point

from moex_bot.journal_import import OperationsWindow, portfolio_events
from moex_bot.journal_readmodel import compare, publish, query
from moex_bot.trader_journal import OPERATIONS_SNAPSHOT, Journal, build_report, digest

T3 = "2026-11-02T10:00:00+00:00"
T4 = "2026-11-02T11:00:00+00:00"


def receipt(ops, *, at=T3, start=T0, end=None, account="private-account"):
    raw = point(at=at)
    raw.update(account_id=account, executed_operations=ops, journal_coverage_from=start,
               journal_coverage_to=end or at, journal_operations_snapshot={
                   "protocol": OPERATIONS_SNAPSHOT, "pages": 1, "count": len(ops),
                   "ids_sha256": digest(sorted(o["operation_id"] for o in ops))})
    return portfolio_events(raw, PROFILE, SPEC, digest(raw))


def ops(journal):
    return [r for r in journal.latest() if r["kind"] == "operation"]


def archive(path):
    with sqlite3.connect(path) as db:
        return dict(db.execute("SELECT rowid,payload FROM events"))


def test_changed_ids_do_not_double_pnl_and_source_history_is_immutable(tmp_path):
    journal = Journal(tmp_path / "history.sqlite3")
    journal.ingest(events([]))
    # A closed short appeared first under the preliminary IDs, then final broker IDs.
    old = [operation("old-sell", T1, "SELL", "34", "9969.48", None),
           operation("old-buy", T2, "BUY", "34", "9980.36", None)]
    journal.ingest(events(old, at=T2))
    before = archive(journal.path)
    new = [{**r, "operation_id": r["operation_id"].replace("old", "final"),
            "commission": "0", "journal_commission_source": "broker_reported"} for r in old]
    journal.ingest(receipt(new))
    assert all(archive(journal.path)[k] == v for k, v in before.items())
    assert {r["identity"] for r in ops(journal)} == {"final-buy", "final-sell"}
    report = build_report(journal.latest())
    stream = report["streams"][0]
    assert len(stream["trades"]) == 1
    assert stream["trades"][0]["net"] == "-10.88"
    assert stream["operation_reconciliation"]["retired_operation_ids"] == 2
    assert stream["periods"]["2026-11-02"]["net"] == "-10.88"
    projection = tmp_path / "projection.sqlite3"
    generation = publish(report, projection)
    compare(report, projection)
    params = {"stream": stream["id"], "period": "2026-11-02", "generation": generation}
    summary = query(projection, "summary", params)
    assert summary["stream"]["operation_reconciliation"] == stream["operation_reconciliation"]
    assert query(projection, "operations", params)["total"] == 2
    assert query(projection, "trades", params)["total"] == 1
    assert "old-sell" not in json.dumps(summary) and "private-account" not in json.dumps(summary)


def test_identical_prices_times_are_not_deduplicated_when_both_ids_are_in_broker_window(tmp_path):
    journal = Journal(tmp_path / "history.sqlite3")
    first = operation("a")
    second = {**first, "operation_id": "b"}
    journal.ingest(receipt([first, second]))
    assert len(ops(journal)) == 2


def test_legacy_events_do_not_assert_complete_history(tmp_path):
    journal = Journal(tmp_path / "history.sqlite3")
    journal.ingest(events([operation("old")], at=T2))
    journal.ingest(events([operation("new")], at=T3))
    assert {r["identity"] for r in ops(journal)} == {"old", "new"}


def test_later_legacy_receipt_cannot_reintroduce_withdrawn_id_or_replace_verified_fee(tmp_path):
    journal = Journal(tmp_path / "history.sqlite3")
    journal.ingest(receipt([operation("new", fee="0")]))
    journal.ingest(events([operation("old", fee=None), operation("new", fee=None)], at=T4))
    assert len(ops(journal)) == 1 and ops(journal)[0]["commission"] == "0"
    assert ops(journal)[0]["known_at"] == T3


def test_narrower_window_does_not_erase_older_history_or_other_accounts(tmp_path):
    journal = Journal(tmp_path / "history.sqlite3")
    journal.ingest(receipt([operation("early", T1), operation("old-late", T2)], at=T3))
    journal.ingest(receipt([operation("new-late", T2)], at=T4, start=T2))
    journal.ingest(receipt([operation("other-account")], at=T3, account="other"))
    journal.ingest(events([operation("future", "2026-11-02T12:00:00Z")],
                          at="2026-11-02T13:00:00Z"))
    assert {r["identity"] for r in ops(journal)} == {"early", "new-late", "other-account", "future"}


def test_later_complete_empty_window_is_not_confused_with_no_collection(tmp_path):
    journal = Journal(tmp_path / "history.sqlite3")
    journal.ingest(receipt([operation("withdrawn")], at=T3))
    journal.ingest(receipt([], at=T4))
    assert ops(journal) == []


@pytest.mark.parametrize("damage", ["missing", "changed", "count", "digest", "protocol", "range"])
def test_corrupt_manifest_does_not_publish_partial_history(tmp_path, damage):
    journal = Journal(tmp_path / "history.sqlite3")
    rows = receipt([operation()])
    if damage == "missing":
        rows = rows[:1]
    elif damage == "changed":
        rows[1]["gross"] = "6000"
    else:
        manifest = rows[0]["operations_manifest"]
        if damage == "range":
            manifest["to"] = T4
        else:
            manifest[{"count": "count", "digest": "sha256", "protocol": "protocol"}[damage]] = -1
    journal.ingest(rows)
    with pytest.raises(ValueError, match="operations snapshot"):
        journal.latest()


def test_id_moved_across_nonoverlapping_windows_is_not_guessed(tmp_path):
    journal = Journal(tmp_path / "history.sqlite3")
    journal.ingest(receipt([operation("same", T1)], at=T3, end=T1))
    journal.ingest(receipt([operation("same", T2)], at=T4, start=T2))
    with pytest.raises(ValueError, match="conflicting broker windows"):
        journal.latest()


def request(cursor=None):
    result = {"accountId": "account", "from": T0, "to": T3, "state": "OPERATION_STATE_EXECUTED",
              "withoutTrades": False, "withoutCommissions": False, "withoutOvernights": False}
    if cursor is not None:
        result["cursor"] = cursor
    return result


def item(identity="op"):
    return {"id": identity, "date": T1, "state": "OPERATION_STATE_EXECUTED"}


def test_full_pagination_proof_and_exact_duplicate_page_overlap():
    window = OperationsWindow()
    window.page({"items": [item()], "hasNext": True, "nextCursor": "next"}, request())
    with pytest.raises(ValueError, match="incomplete"):
        window.proof(["op"])
    window.page({"items": [item(), item("second")], "hasNext": False}, request("next"))
    assert window.proof(["op", "second"]) == {
        "protocol": OPERATIONS_SNAPSHOT, "pages": 2, "count": 2,
        "ids_sha256": digest(["op", "second"])}


@pytest.mark.parametrize("response", [None, {}, {"items": []}, {"hasNext": False},
    {"items": [], "hasNext": 0}, {"items": [], "hasNext": "false"},
    {"items": [], "hasNext": True}, {"items": [item()], "hasNext": True, "nextCursor": 123},
    {"items": [None], "hasNext": False},
    {"items": [dict(item(), state="CANCELED")], "hasNext": False},
    {"items": [dict(item(), brokerAccountId="wrong")], "hasNext": False},
    {"items": [dict(item(), date=T4)], "hasNext": False},
    {"items": [dict(item(), id=" ")], "hasNext": False}])
def test_malformed_response_cannot_erase_operations(response):
    with pytest.raises(ValueError):
        OperationsWindow().page(response, request())


@pytest.mark.parametrize("damage", ["changed_duplicate", "changed_query", "cursor_loop",
                                    "truncated", "missing_item", "duplicate_parsed", "extra_item",
                                    "no_pages"])
def test_incomplete_or_inconsistent_pages_rejected(damage):
    window = OperationsWindow()
    with pytest.raises(ValueError):
        if damage == "no_pages":
            window.proof([])
        window.page({"items": [item()], "hasNext": True, "nextCursor": "next"}, request())
        following = request("next")
        page = {"items": [], "hasNext": False}
        if damage == "changed_duplicate":
            page["items"] = [dict(item(), extra="conflict")]
        elif damage == "changed_query":
            following["to"] = T4
        elif damage == "cursor_loop":
            page.update(hasNext=True, nextCursor="next")
        elif damage == "truncated":
            window.proof(["op"])
        window.page(page, following)
        window.proof({"missing_item": [], "duplicate_parsed": ["op", "op"],
                      "extra_item": ["op", "extra"]}.get(damage, ["op"]))


@pytest.mark.parametrize("damage", ["count", "ids", "pages", "duplicate", "future", "range"])
def test_import_receipt_proof_is_bound_to_normalized_operations(damage):
    raw = point(at=T3)
    raw.update(executed_operations=[operation()], journal_coverage_from=T0,
               journal_coverage_to=T3, journal_operations_snapshot={
                   "protocol": OPERATIONS_SNAPSHOT, "pages": 1, "count": 1,
                   "ids_sha256": digest(["one"])})
    if damage in {"count", "pages"}:
        raw["journal_operations_snapshot"][damage] = False
    elif damage == "ids":
        raw["journal_operations_snapshot"]["ids_sha256"] = "bad"
    elif damage == "duplicate":
        raw["executed_operations"] *= 2
    elif damage == "future":
        raw["executed_operations"][0]["occurred_at"] = T4
    else:
        raw["journal_coverage_to"] = T4
    with pytest.raises(ValueError, match="complete operations receipt"):
        portfolio_events(deepcopy(raw), PROFILE, SPEC, "source")


@pytest.mark.parametrize("change", [{"operationTypes": ["OPERATION_TYPE_BUY"]},
    {"instrumentId": "filtered"}, {"withoutTrades": True}, {"withoutCommissions": True},
    {"withoutOvernights": True}, {"state": "OPERATION_STATE_CANCELED"}])
def test_filtered_request_never_claims_complete_window(change):
    with pytest.raises(ValueError, match="filtered or invalid request"):
        OperationsWindow().page({"items": [], "hasNext": False}, request() | change)


def test_financial_reconciliation_failure_is_not_healthy_with_fresh_source(tmp_path):
    report = build_report(events([], at=datetime.now(UTC).isoformat()))
    stream = report["streams"][0]
    stream["warnings"] = ["Остатки не сошлись: доходность сделок скрыта до сверки"]
    path = tmp_path / "readmodel.sqlite3"
    publish(report, path)
    health = query(path, "health", {})
    assert health["status"] == "financial_degraded" and health["financial_warnings"] == 1
    assert health["source_status"] == "ok" and health["build_status"] == "ok"
    stream["warnings"] = []
    publish(report, path)
    assert query(path, "health", {})["status"] == "ok"
