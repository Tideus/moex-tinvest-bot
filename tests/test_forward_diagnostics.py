import copy
import json
import os
import socket
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from forward_diagnostics import (
    FIELDS,
    classify,
    empty_book_kind,
    notify_status,
    save_diagnostics,
    status_text,
)

from moex_bot.monitoring_labels import safe_reason
from moex_bot.monitoring_research import report_check

DAY = "2026-11-24"


@pytest.fixture
def facts():
    calendar = {"receipts": [{
        "request": {"host": "apim.moex.com", "method": "GET",
                    "path": "/iss/calendars/stock/session.json", "query": {"date": [DAY]}},
        "request_started_at": DAY + "T06:00:00Z", "received_at": DAY + "T06:00:01Z",
        "payload": {"session_schedule": {
            "columns": ["tradedate", "boardid", "secid", "type", "time_from", "time_till"],
            "data": [[DAY, "TQBR", "", "system", "10:00:00", "18:54:59"],
                     [DAY, "TQBR", "", "system", "19:00:01", "23:49:59"]]}}}]}
    previous = {"receipt": {
        "source": "T-Invest/read-only", "stage": "MarketDataService/GetOrderBook",
        "request": {"host": "invest-public-api.tbank.ru", "method": "POST",
                    "path": "/rest/tinkoff.public.invest.api.contract.v1."
                    "MarketDataService/GetOrderBook", "body": {"instrumentId": "SBER_TQBR"}},
        "payload": {"ticker": "SBER", "classCode": "TQBR", "bids": [], "asks": []},
        "request_started_at": DAY + "T15:55:00Z", "received_at": DAY + "T15:55:00.100Z"}}
    error = {"source": "T-Invest/read-only", "stage": "quote:SBER", "reason": "ValueError",
             "observed_at": DAY + "T18:55:00.110+03:00"}
    return previous, error, calendar


@pytest.mark.parametrize("second,kind", [
    ("18:54:59.5", "open_empty"), ("18:55:00", "gap_empty"),
    ("19:00:00.5", "gap_empty"), ("19:00:01", "open_empty"),
    ("09:59:59", "unclassified"), ("23:50:00", "unclassified"),
])
def test_exact_calendar_boundaries_no_hardcoded_break(facts, second, kind):
    previous, error, calendar = facts
    at = DAY + "T" + second + "+03:00"
    previous["receipt"].update(request_started_at=at, received_at=at)
    error["observed_at"] = at
    assert empty_book_kind(previous, error, calendar, DAY) == kind


@pytest.mark.parametrize("field,value", [
    ("ticker", "ROSN"), ("classCode", "OTHER"), ("bids", [{}]),
    ("asks", [{}]), ("bids", None), ("asks", ""),
])
def test_only_exact_empty_book_response_is_classified(facts, field, value):
    previous, error, calendar = facts
    previous["receipt"]["payload"][field] = value
    assert empty_book_kind(previous, error, calendar, DAY) == "unclassified"


@pytest.mark.parametrize("field,value", [
    ("host", "example.com"), ("method", "GET"), ("path", "/other"),
    ("body", {"instrumentId": "ROSN_TQBR"}),
])
def test_wrong_response_source_is_not_used(facts, field, value):
    previous, error, calendar = facts
    previous["receipt"]["request"][field] = value
    assert empty_book_kind(previous, error, calendar, DAY) == "unclassified"


def test_calendar_is_not_assumed_and_late_calendar_rejected(facts):
    previous, error, calendar = facts
    calendar["receipts"][0]["received_at"] = DAY + "T16:00:00Z"
    with pytest.raises(ValueError, match="not_available"):
        empty_book_kind(previous, error, calendar, DAY)
    with pytest.raises(KeyError):
        empty_book_kind(previous, error, {}, DAY)


@pytest.mark.parametrize("change", ["override", "overlap", "duplicate", "wrong_day", "host"])
def test_ambiguous_calendar_cannot_declare_expected_gap(facts, change):
    previous, error, calendar = facts
    rows = calendar["receipts"][0]["payload"]["session_schedule"]["data"]
    if change == "override":
        rows.append([DAY, "TQBR", "SBER", "system", "10:00:00", "23:49:59"])
    elif change == "overlap":
        rows[0][-1] = "19:02:00"
    elif change == "duplicate":
        rows.append(rows[0])
    elif change == "wrong_day":
        calendar["receipts"][0]["request"]["query"]["date"] = ["2026-11-23"]
    else:
        calendar["receipts"][0]["request"]["host"] = "example.com"
    with pytest.raises(ValueError):
        empty_book_kind(previous, error, calendar, DAY)


@pytest.mark.parametrize("started,received,failed", [
    ("15:54:59.9", "15:55:00.1", "15:55:00.2"),  # crosses the boundary
    ("15:55:00", "15:55:00.1", "15:55:03"),     # unbound late error
    ("15:55:00", "15:55:00.2", "15:55:00.1"),   # future response
])
def test_response_time_must_fit_same_gap(facts, started, received, failed):
    previous, error, calendar = facts
    previous["receipt"].update(request_started_at=DAY+"T"+started+"Z",
                                received_at=DAY+"T"+received+"Z")
    error["observed_at"] = DAY+"T"+failed+"Z"
    assert empty_book_kind(previous, error, calendar, DAY) == "unclassified"


def make_store(tmp_path, facts):
    path = tmp_path / "events.sqlite3"
    previous, error, calendar = facts
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE events(seq INTEGER PRIMARY KEY,kind,logical_key,day,payload)")
        rows = [(1, "preflight", "calendar:"+DAY, calendar),
                (2, "source", "source", previous), (3, "source_error", "error", error),
                (4, "missed_slot", "missing", {})]
        db.executemany("INSERT INTO events VALUES(?,?,?,?,?)",
                       [(seq, kind, key, DAY, json.dumps(v)) for seq, kind, key, v in rows])

    @contextmanager
    def connect(*, readonly):
        assert readonly
        connection = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)
        try:
            yield connection
        finally:
            connection.close()
    return SimpleNamespace(_verified={"head": "verified"}, connect=connect), path


def test_classifier_preserves_evidence_and_reports_true_counts(tmp_path, facts):
    store, path = make_store(tmp_path, facts)
    before = path.read_bytes()
    result = classify(store, DAY)
    assert result["available"] is True
    assert result["gap_empty"] == result["source_error_count"] == result["missed_slots"] == 1
    assert result["unclassified"] == result["open_empty"] == result["request_errors"] == 0
    assert path.read_bytes() == before
    # A future record, or a same-day source after the error, cannot rescue the wrong pair.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE events SET kind='quote' WHERE seq=2")
        db.execute("INSERT INTO events VALUES(5,'source','future',?,?)",
                   (DAY, json.dumps(facts[0])))
    assert classify(store, DAY)["unclassified"] == 1


def test_schema_and_proof_failure_are_not_reported_as_zero(tmp_path, facts):
    store, path = make_store(tmp_path, facts)
    store._verified = None
    with pytest.raises(ValueError, match="unverified"):
        classify(store, DAY)
    store._verified = {}
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE events")
    result = classify(store, DAY)
    assert result["available"] is False and "gap_empty" not in result


@pytest.mark.parametrize("reason,expected", [("TimeoutError", "request_errors"),
                                           ("HTTPError", "request_errors"),
                                           ("private secret", "unclassified")])
def test_request_failures_stay_separate(tmp_path, facts, reason, expected):
    facts[1].update(stage="MarketDataService/GetOrderBook", reason=reason)
    store, _ = make_store(tmp_path, facts)
    result = classify(store, DAY)
    assert result[expected] == 1 and result["gap_empty"] == 0
    assert "secret" not in json.dumps(result)


def result_payload():
    return {"day": DAY, "status": "INCOMPLETE", "execution_allowed": False,
            "diagnostics": {"source_classification": {
                "available": True, **dict.fromkeys(FIELDS, 0),
                "gap_empty": 14, "missed_slots": 24}}}


def test_status_reaches_monitor_and_telegram_but_never_makes_green():
    result = result_payload()
    props = {"ActiveState": "active", "Result": "success", "ExecMainStatus": "3",
             "StatusText": status_text(result)}
    fail = report_check(lambda unit, prop: props.get(prop, ""),
                        datetime(2026, 11, 24, 22, tzinfo=UTC), 100)
    assert fail.startswith("FAIL:") and "gap_empty=14" in fail
    rendered = safe_reason(fail)
    assert "подтверждённом перерыве: 14" in rendered and "наблюдения неполны" in rendered
    assert "T-Invest: 0" in rendered and "вариантов): 24" in rendered
    # Old status, arbitrary text, malformed numbers and service failures cannot be laundered.
    props["Result"] = "timeout"
    assert "timeout" in report_check(lambda unit, prop: props.get(prop, ""),
                                     datetime(2026, 11, 24, 22, tzinfo=UTC), 100)
    props["Result"] = "success"
    props["StatusText"] += "\nTOKEN=secret"
    assert "secret" not in report_check(lambda unit, prop: props.get(prop, ""),
                                        datetime(2026, 11, 24, 22, tzinfo=UTC), 100)
    props["StatusText"] = status_text(result)
    assert "gap_empty" not in report_check(lambda unit, prop: props.get(prop, ""),
                                           datetime(2026, 11, 26, 22, tzinfo=UTC), 100)


def test_status_unknown_not_zero_and_sidecar_never_overwrites_frozen_report(tmp_path):
    result = result_payload()
    result["diagnostics"]["source_classification"]["gap_empty"] = True
    assert status_text(result).endswith("UNAVAILABLE")
    (tmp_path/"reports").mkdir()
    frozen = tmp_path/"reports"/(DAY+".json")
    frozen.write_text('"immutable original"')
    save_diagnostics(tmp_path, result)
    assert frozen.read_text() == '"immutable original"'
    assert json.loads((tmp_path/"reports"/(DAY+".diagnostics.json")).read_text()) == result
    invalid = copy.deepcopy(result)
    invalid["day"] = "../secret"
    save_diagnostics(tmp_path, invalid)
    assert len(list((tmp_path/"reports").iterdir())) == 2


def test_notify_missing_or_unreachable_does_not_change_result(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert notify_status("RESEARCH_REPORT_RUNNING") is False
    monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/moex-notify-test")
    assert notify_status("RESEARCH_REPORT_RUNNING") is False


def test_notify_only_main_status_with_exact_nonsecret_payload(monkeypatch):
    from unittest.mock import MagicMock

    sock = MagicMock()
    monkeypatch.setattr("forward_diagnostics.socket.AF_UNIX", 1, raising=False)
    monkeypatch.setenv("NOTIFY_SOCKET", "@unit-test")
    monkeypatch.setattr("forward_diagnostics.socket.socket", lambda *args: sock)
    assert notify_status(status_text(result_payload())) is True
    sock.__enter__().connect.assert_called_once_with("\0unit-test")
    sent = sock.__enter__().sendall.call_args.args[0].decode()
    assert sent.startswith("STATUS=RESEARCH_REPORT_V1") and "READY=" not in sent


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix systemd notification socket")
def test_real_local_systemd_datagram(tmp_path, monkeypatch):
    address = str(tmp_path/"notify.sock")
    # Linux AF_UNIX has a small address limit; pytest basetemp may itself be long.
    if len(address.encode()) >= 100:
        address = "\0moex-report-test-" + str(os.getpid())
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(address)
        server.settimeout(1)
        monkeypatch.setenv("NOTIFY_SOCKET", "@" + address[1:] if address[0] == "\0" else address)
        assert notify_status(status_text(result_payload()))
        assert server.recv(2048).decode() == "STATUS=" + status_text(result_payload())
