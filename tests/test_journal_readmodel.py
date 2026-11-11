import hashlib
import json
import os
import sqlite3
from copy import deepcopy
from urllib.parse import urlencode

import pytest
from test_trader_journal import T1, T2, events, operation

from moex_bot.journal_api import application
from moex_bot.journal_readmodel import KINDS, QueryError, compare, publish, query
from moex_bot.trader_journal import build_report


def fixture_report():
    rows = events([], at="2026-10-31T07:00:00Z") + events(
        [
            operation(at="2026-10-31T08:00:00Z"),
            operation("partial", T1, "SELL", "4", "1240", None),
            operation("close", T2, "SELL", "6", "1740", "0"),
        ],
        at=T2,
    )
    for i in range(257):
        rows.append(
            {
                **rows[0],
                "kind": "signal",
                "at": T1,
                "known_at": T1,
                "identity": str(i),
                "symbol": f"TEST{i}",
                "status": "entry_rejected",
                "side": "sell",
                "reasons": ["cost_edge"],
            }
        )
    second = [{**r, "stream": "intraday:another", "strategy": "intraday"} for r in rows]
    report = build_report(rows + second)
    report["import"] = {"errors": []}
    return report


@pytest.fixture
def projection(tmp_path):
    report = fixture_report()
    path = tmp_path / "model.sqlite3"
    generation = publish(report, path)
    params = {"stream": report["streams"][0]["id"], "period": "2026-11", "generation": generation}
    return path, report, params


def test_exact_parity_all_periods_both_accounts_and_unknown_commissions(projection):
    path, report, _ = projection
    result = compare(report, path)
    assert result["streams"] == 2 and result["periods"] >= 6 and result["rows"] > 1000
    data = path.read_bytes()
    assert b"private-account" not in data and b"source-checksum" not in data


def test_bounded_pages_no_gaps_or_duplicates_and_no_eager_history(projection):
    path, _, params = projection
    catalog = query(path, "catalog", {})
    assert len(json.dumps(catalog)) < 3000
    assert all(not set(KINDS) & set(s) for s in catalog["streams"])
    summary = query(path, "summary", params)
    assert not set(KINDS) & set(summary["stream"])
    cursor = ""
    rows = []
    while True:
        page = query(path, "signals", {**params, "limit": "37", "cursor": cursor})
        assert len(page["items"]) <= 37
        rows += page["items"]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(rows) == len({r["symbol"] for r in rows}) == 257


def test_filters_decimal_null_moscow_dates_and_empty_period(projection):
    path, _, params = projection
    assert query(path, "trades", {**params, "cost": "net", "outcome": "unknown"})["total"] == 1
    assert query(path, "trades", {**params, "cost": "gross", "outcome": "loss"})["total"] == 1
    assert query(path, "trades", {**params, "symbol": "SBER", "direction": "LONG"})["total"] == 2
    assert query(path, "trades", {**params, "symbol": "' OR 1=1 --"})["items"] == []
    assert query(path, "summary", {**params, "period": "2020-01"})["stream"]["periods"] == {}


@pytest.mark.parametrize(
    "extra",
    [
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "x"},
        {"period": "2026-13"},
        {"period": "2026-02-30"},
        {"cost": "gross); DROP TABLE rows;--"},
        {"outcome": "anything"},
        {"direction": "BUY"},
        {"unknown": "x"},
        {"cursor": "invalid"},
    ],
)
def test_invalid_queries_fail_closed(projection, extra):
    path, _, params = projection
    with pytest.raises(QueryError):
        query(path, "trades", params | extra)


def test_generation_and_cursor_are_bound_to_filters_account_and_period(projection):
    path, report, params = projection
    page = query(path, "signals", {**params, "limit": "1"})
    for changed in ({"period": "2026-11-02"}, {"stream": report["streams"][1]["id"]}):
        with pytest.raises(QueryError, match="cursor"):
            query(path, "signals", params | changed | {"cursor": page["next_cursor"]})
    updated = deepcopy(report)
    updated["generated_at"] = "2027-01-01T00:00:00Z"
    publish(updated, path)
    with pytest.raises(QueryError) as exc:
        query(path, "signals", params | {"cursor": page["next_cursor"]})
    assert exc.value.status == 409


def test_atomic_failed_publish_preserves_previous_projection(projection, monkeypatch):
    path, report, params = projection
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        publish(report, path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert query(path, "summary", params)["generation"] == params["generation"]
    assert not list(path.parent.glob("projection-*"))


def test_read_api_never_changes_database_or_creates_missing_one(projection, tmp_path):
    path, _, params = projection
    before = path.read_bytes()
    for route in (*KINDS, "summary", "catalog", "health"):
        query(path, route, params if route not in {"catalog", "health"} else {})
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
    with pytest.raises(sqlite3.OperationalError):
        query(tmp_path / "absent.sqlite3", "catalog", {})
    assert not (tmp_path / "absent.sqlite3").exists()


@pytest.mark.skipif(os.name == "nt", reason="atomic inode replacement is Linux deployment contract")
def test_reader_keeps_old_inode_while_new_snapshot_publishes(projection):
    path, report, params = projection
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as reader:
        old = reader.execute("SELECT payload FROM metadata").fetchone()[0]
        report["generated_at"] = "2027-01-01T00:00:00Z"
        publish(report, path)
        assert reader.execute("SELECT payload FROM metadata").fetchone()[0] == old
    assert query(path, "catalog", {})["generation"] != params["generation"]


@pytest.mark.parametrize(
    "method,path,raw,status",
    [
        ("GET", "/journal/api/catalog", "", 200),
        ("HEAD", "/journal/api/catalog", "", 200),
        ("POST", "/journal/api/catalog", "", 405),
        ("DELETE", "/journal/api/catalog", "", 405),
        ("GET", "/journal/api/../bot.env", "", 404),
        ("GET", "/journal/api/sql", "", 404),
        ("GET", "/journal/api/catalog", "stream=x&stream=y", 400),
        ("GET", "/journal/api/catalog", "x=" + "x" * 2100, 400),
    ],
)
def test_wsgi_transport_read_only_and_safe_errors(
    projection, monkeypatch, method, path, raw, status
):
    database, _, _ = projection
    monkeypatch.setenv("JOURNAL_READMODEL", str(database))
    captured = []
    body = b"".join(
        application(
            {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": raw},
            lambda code, headers: captured.append((code, dict(headers))),
        )
    )
    assert captured[0][0].startswith(str(status))
    assert captured[0][1]["Cache-Control"] == "no-store"
    assert b"private-account" not in body
    if method == "HEAD":
        assert not body


def test_wsgi_snapshot_conflict_and_missing_projection_are_explicit(projection, monkeypatch):
    path, _, params = projection
    monkeypatch.setenv("JOURNAL_READMODEL", str(path))
    for query_string, expected in [(urlencode(params | {"generation": "old"}), "409")]:
        captured = []
        application(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/journal/api/trades",
                "QUERY_STRING": query_string,
            },
            lambda code, _, captured=captured: captured.append(code),
        )
        assert captured[0].startswith(expected)
    monkeypatch.setenv("JOURNAL_READMODEL", str(path.parent / "missing.sqlite3"))
    captured = []
    result = application(
        {"REQUEST_METHOD": "GET", "PATH_INFO": "/journal/api/catalog"},
        lambda code, _: captured.append(code),
    )
    assert captured[0].startswith("503") and str(path.parent).encode() not in result[0]


def test_month_and_day_indexes_are_used(projection):
    path, _, params = projection
    with sqlite3.connect(path) as db:
        for column, period, index in [
            ("day", "2026-11-02", "rows_day"),
            ("month", "2026-11", "rows_month"),
        ]:
            plan = db.execute(
                "EXPLAIN QUERY PLAN SELECT seq,payload FROM rows WHERE stream=? AND kind=? "
                f"AND {column}=? AND seq>? ORDER BY seq LIMIT ?",
                (params["stream"], "signals", period, -1, 51),
            ).fetchall()
            assert index in str(plan)


def test_rejected_projection_does_not_replace_good_data(projection):
    path, report, _ = projection
    old = path.read_bytes()
    report["streams"][0]["trades"][0]["gross"] = "NaN"
    with pytest.raises(ValueError):
        publish(report, path)
    assert path.read_bytes() == old
