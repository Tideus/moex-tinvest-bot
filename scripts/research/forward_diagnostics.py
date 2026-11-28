"""Read-only explanations of verified frozen observations, never replacement prices."""
from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
PREFIX = "RESEARCH_REPORT_V1"
FIELDS = ("gap_empty", "open_empty", "request_errors", "unclassified", "missed_slots")


def stamp(value):
    at = datetime.fromisoformat(value)
    if at.utcoffset() is None:
        raise ValueError("timestamp_without_timezone")
    return at.astimezone(MSK)


def calendar_intervals(cache, day, symbol, before):
    """Only a previously received official calendar, with no symbol-specific override."""
    receipts = []
    for receipt in cache["receipts"]:
        request = receipt["request"]
        if (request.get("host") in {"iss.moex.com", "apim.moex.com"}
                and request.get("path") == "/iss/calendars/stock/session.json"
                and request.get("method") == "GET"
                and request.get("query", {}).get("date") == [day]):
            receipts.append(receipt)
    if len(receipts) != 1:
        raise ValueError("calendar_not_unambiguous")
    receipt = receipts[0]
    if not stamp(receipt["request_started_at"]) <= stamp(receipt["received_at"]) <= before:
        raise ValueError("calendar_not_available_at_request")
    block = receipt["payload"]["session_schedule"]
    rows = [dict(zip(block["columns"], row, strict=True)) for row in block["data"]]
    relevant = [r for r in rows if r.get("tradedate") == day
                and r.get("boardid") == "TQBR" and r.get("type") == "system"]
    if any(r.get("secid") == symbol for r in relevant):
        raise ValueError("symbol_calendar_override")
    intervals = []
    for row in relevant:
        if row.get("secid") != "":
            continue
        start = datetime.fromisoformat(day + "T" + row["time_from"]).replace(tzinfo=MSK)
        # Calendar endpoints are inclusive to a second, not to its first microsecond.
        end = (datetime.fromisoformat(day + "T" + row["time_till"]).replace(tzinfo=MSK)
               + timedelta(seconds=1))
        if not start < end or end.date().isoformat() != day:
            raise ValueError("invalid_session_interval")
        intervals.append((start, end))
    intervals.sort()
    if not intervals or any(left[1] > right[0] for left, right in
                            zip(intervals, intervals[1:], strict=False)):
        raise ValueError("invalid_session_intervals")
    return intervals


def empty_book_kind(previous, error, cache, day):
    """Runtime writes source immediately before book validation: require that exact pair."""
    if error.get("reason") != "ValueError" or error.get("source") != "T-Invest/read-only":
        return "unclassified"
    stage = error.get("stage", "")
    if not re.fullmatch(r"quote:[A-Z0-9]{1,20}", stage):
        return "unclassified"
    symbol = stage.split(":")[1]
    receipt = previous["receipt"]
    request, book = receipt["request"], receipt["payload"]
    if (receipt.get("stage") != "MarketDataService/GetOrderBook"
            or receipt.get("source") != "T-Invest/read-only"
            or request.get("host") != "invest-public-api.tbank.ru"
            or request.get("method") != "POST"
            or request.get("path") != "/rest/tinkoff.public.invest.api.contract.v1."
            "MarketDataService/GetOrderBook"
            or request.get("body", {}).get("instrumentId") != symbol + "_TQBR"
            or book.get("ticker") != symbol or book.get("classCode") != "TQBR"
            or book.get("bids") != [] or book.get("asks") != []):
        return "unclassified"
    started, received, failed = (stamp(receipt["request_started_at"]),
                                 stamp(receipt["received_at"]), stamp(error["observed_at"]))
    if (not started <= received <= failed or (failed - started).total_seconds() > 2
            or any(t.date().isoformat() != day for t in (started, received, failed))):
        return "unclassified"
    intervals = calendar_intervals(cache, day, symbol, started)
    if any(start <= started <= received <= failed < end for start, end in intervals):
        return "open_empty"
    if any(left[1] <= started <= received <= failed < right[0]
           for left, right in zip(intervals, intervals[1:], strict=False)):
        return "gap_empty"
    return "unclassified"  # Pre-open, post-close and crossings are not an intraday break.


def classify(store, day):
    """Use the same verified read transaction as the frozen report; never read future rows."""
    result = dict(protocol="research-diagnostics-v1", available=False,
                  historical_decisions_unchanged=True, execution_allowed=False)
    if store._verified is None:
        raise ValueError("unverified_diagnostics_denied")
    counts = dict.fromkeys(FIELDS, 0)
    try:
        with store.connect(readonly=True) as db:
            row = db.execute("SELECT seq,payload FROM events WHERE logical_key=?",
                             ("calendar:" + day,)).fetchone()
            calendar_seq, cache = (row[0], json.loads(row[1])) if row else (None, {})
            errors = db.execute("SELECT seq,payload FROM events WHERE day=? "
                                "AND kind='source_error' ORDER BY seq", (day,)).fetchall()
            for seq, raw in errors:
                kind = "unclassified"
                try:
                    error = json.loads(raw)
                    if (error.get("source") == "T-Invest/read-only"
                            and error.get("stage") == "MarketDataService/GetOrderBook"
                            and error.get("reason") in {"TimeoutError", "URLError", "HTTPError",
                                                        "ConnectionError"}):
                        kind = "request_errors"
                    else:
                        previous = db.execute(
                            "SELECT payload FROM events WHERE seq=? AND day=? AND kind='source'",
                            (seq-1, day)).fetchone()
                        if previous and calendar_seq is not None and calendar_seq < seq-1:
                            kind = empty_book_kind(json.loads(previous[0]), error, cache, day)
                except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                    pass  # Unknown is never silently zero, expected, or an API outage.
                counts[kind] += 1
            counts["missed_slots"] = db.execute("SELECT count(*) FROM events WHERE day=? "
                                                "AND kind='missed_slot'", (day,)).fetchone()[0]
        result.update(available=True, **counts, source_error_count=len(errors),
                      missed_slots_include_strategy_variants=True,
                      error_records_are_not_independent_incidents=True)
    except (sqlite3.Error, AttributeError, KeyError, TypeError, ValueError):
        result["reason"] = "diagnostic_evidence_unavailable"
    return result


def status_text(result):
    diag = result.get("diagnostics", {}).get("source_classification", {})
    day = result.get("day", "")
    if (diag.get("available") is not True or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
            or any(type(diag.get(k)) is not int or not 0 <= diag[k] <= 999999 for k in FIELDS)):
        return "RESEARCH_REPORT_DIAGNOSTICS_UNAVAILABLE"
    return f"{PREFIX} day={day} " + " ".join(f"{k}={diag[k]}" for k in FIELDS)


def consumer_gaps(store, day):
    """Explain missing consumers, not causes: no replacement quotes or new decisions.

    A calendar break can explain why a window is unobservable, but it does not
    prove which network request caused a missing price. Preserve INCOMPLETE.
    The observation window is the frozen v1 protocol's inclusive 60 seconds.
    """
    if store._verified is None:
        raise ValueError("unverified_diagnostics_denied")
    result = dict(available=False, execution_allowed=False,
                  historical_decisions_unchanged=True, completeness_unchanged=True)
    try:
        calendar = store.get("calendar:" + day)
        decisions = {r["key"]: r for r in store.rows(day, "decision")}
        entries = {r["digest"]: r for r in store.rows(day, "model_entry")}
        rows = []
        for kind in ("model_entry", "markout"):
            for event in store.rows(day, kind):
                value = event["value"]
                if value.get("state") == "observed":
                    continue
                row = {"kind": kind, "key": event["key"], "digest": event["digest"],
                       "symbol": value.get("symbol"), "strategy": value.get("strategy"),
                       "decision_key": value.get("decision_key"),
                       "target_at": value.get("target_at"),
                       "horizon_minutes": value.get("horizon_minutes"),
                       "classification": "unclassified", "causal_api_failure": None}
                try:
                    decision = decisions[value["decision_key"]]
                    d = decision["value"]
                    if (value["decision_digest"] != decision["digest"]
                            or any(value[k] != d[k] for k in ("symbol", "strategy"))):
                        raise ValueError("unbound_consumer")
                    target = stamp(value["target_at"])
                    anchor = stamp(d["decision_at"])
                    if kind == "markout":
                        entry = entries[value["entry_digest"]]["value"]
                        if (entry["state"] != "observed"
                                or entry["decision_digest"] != decision["digest"]
                                or entry["decision_key"] != value["decision_key"]
                                or any(entry[k] != d[k] for k in ("symbol", "strategy"))):
                            raise ValueError("unbound_entry")
                        anchor = stamp(entry["quote"]["exchange_at"])
                        horizon = value["horizon_minutes"]
                        if type(horizon) is not int or horizon not in (5, 15, 30, 60):
                            raise ValueError("unknown_horizon")
                        if target != anchor + timedelta(minutes=horizon):
                            raise ValueError("wrong_target")
                    elif not 0 <= (target - anchor).total_seconds() <= 30:
                        raise ValueError("invalid_entry_delay")
                    end = target + timedelta(seconds=60)
                    if any(t.date().isoformat() != day for t in (anchor, target, end)):
                        row["classification"] = "cross_day_window"
                    else:
                        intervals = calendar_intervals(
                            calendar["value"], day, value["symbol"], stamp(d["decision_at"]))
                        if any(a <= anchor <= target <= end < b for a, b in intervals):
                            row["classification"] = "in_session_missing"
                        elif any(a <= target <= end < b for a, b in intervals):
                            row["classification"] = "cross_session_window"
                        elif all(end < a or target >= b for a, b in intervals):
                            row["classification"] = "outside_session_window"
                        else:
                            row["classification"] = "session_boundary_window"
                except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                    row["reason"] = "consumer_binding_or_calendar_unavailable"
                rows.append(row)
        missing_inputs = [
            {"decision_key": r["key"], "digest": r["digest"],
             "symbol": r["value"].get("symbol"), "strategy": r["value"].get("strategy"),
             "decision_at": r["value"].get("decision_at"),
             "reasons": r["value"].get("reasons", [])}
            for r in decisions.values() if r["value"].get("state") == "data_missing"
        ]
        result.update(available=True, missing_price_consumers=rows,
                      data_missing_decisions=missing_inputs,
                      observations_are_not_independent_trades=True,
                      scope="persisted_consumers_only_not_absent_events",
                      decision_reasons_are_original_mixed_filter_and_data_codes=True)
    except (sqlite3.Error, AttributeError, KeyError, TypeError, ValueError):
        result["reason"] = "consumer_evidence_unavailable"
    return result


def notify_status(text):
    address = os.getenv("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1)
            sock.connect(address)
            sock.sendall(("STATUS=" + text).encode())
        return True
    except (AttributeError, OSError):
        return False  # Exit=3 and private JSON still preserve incomplete evidence.


def save_diagnostics(state, result):
    day = result.get("day", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return
    destination = state / "reports" / (day + ".diagnostics.json")
    if destination.is_symlink():
        raise ValueError("diagnostic_symlink_denied")
    # A separate explanation, never the frozen report, decisions, or evidence database.
    fd, name = tempfile.mkstemp(prefix=".diagnostic-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
