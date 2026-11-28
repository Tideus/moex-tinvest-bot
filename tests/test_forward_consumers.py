import copy
from types import SimpleNamespace

import pytest
from forward_diagnostics import consumer_gaps

DAY = "2026-11-27"


@pytest.fixture
def consumers():
    def at(clock):
        return DAY + "T" + clock + "+03:00"

    decision = dict(key="decision:test", digest="decision-hash", value=dict(
        symbol="SBER", strategy="intraday:current", decision_at=at("18:25:00"),
        state="data_missing", reasons=["historical_coverage_missing"]))
    entry = dict(key="entry:test", digest="entry-hash", value=dict(
        symbol="SBER", strategy="intraday:current", decision_key=decision["key"],
        decision_digest=decision["digest"], target_at=at("18:25:02"), state="observed",
        quote={"exchange_at": at("18:25:02")}))
    mark = dict(key="mark:test", digest="mark-hash", value=dict(
        symbol="SBER", strategy="intraday:current", decision_key=decision["key"],
        decision_digest=decision["digest"], entry_digest=entry["digest"],
        target_at=at("18:55:02"), horizon_minutes=30, state="missing_quote"))
    calendar = {"value": {"receipts": [{
        "request": dict(host="iss.moex.com", method="GET",
                        path="/iss/calendars/stock/session.json", query={"date": [DAY]}),
        "request_started_at": at("09:00:00"), "received_at": at("09:00:01"),
        "payload": {"session_schedule": {
            "columns": ["tradedate", "boardid", "secid", "type", "time_from", "time_till"],
            "data": [[DAY, "TQBR", "", "system", "10:00:00", "18:54:59"],
                     [DAY, "TQBR", "", "system", "19:00:01", "23:49:59"]]}}}]}}
    data = dict(decision=[decision], model_entry=[entry], markout=[mark])
    store = SimpleNamespace(_verified={}, get=lambda key: calendar,
                            rows=lambda day, kind: data[kind])
    return store, data, calendar


def classification(consumers):
    return consumer_gaps(consumers[0], DAY)["missing_price_consumers"][0]["classification"]


def test_gap_is_bound_to_consumer_but_not_claimed_as_api_cause(consumers):
    before = copy.deepcopy(consumers[1:])
    result = consumer_gaps(consumers[0], DAY)
    row = result["missing_price_consumers"][0]
    assert row["classification"] == "outside_session_window"
    assert row["horizon_minutes"] == 30 and row["symbol"] == "SBER"
    assert row["causal_api_failure"] is None
    assert result["data_missing_decisions"][0]["reasons"] == ["historical_coverage_missing"]
    assert result["completeness_unchanged"] is True
    assert consumers[1:] == before


@pytest.mark.parametrize("start,target,expected", [
    ("18:00:02", "18:30:02", "in_session_missing"),
    ("18:24:30", "18:54:30", "session_boundary_window"),
    ("18:30:02", "19:00:02", "cross_session_window"),
])
def test_continuous_session_not_just_end_quote(consumers, start, target, expected):
    data = consumers[1]
    data["decision"][0]["value"]["decision_at"] = DAY + "T" + start + "+03:00"
    data["model_entry"][0]["value"]["quote"]["exchange_at"] = DAY + "T" + start + "+03:00"
    data["markout"][0]["value"]["target_at"] = DAY + "T" + target + "+03:00"
    assert classification(consumers) == expected


@pytest.mark.parametrize("mutation", ["decision_hash", "entry_hash", "symbol", "horizon",
                                     "target", "calendar", "future_calendar"])
def test_unbound_evidence_never_excused_as_expected_gap(consumers, mutation):
    mark = consumers[1]["markout"][0]["value"]
    if mutation == "decision_hash":
        mark["decision_digest"] = "other"
    elif mutation == "entry_hash":
        mark["entry_digest"] = "other"
    elif mutation == "symbol":
        mark["symbol"] = "ROSN"
    elif mutation == "horizon":
        mark["horizon_minutes"] = True
    elif mutation == "target":
        mark["target_at"] = DAY + "T18:56:00+03:00"
    elif mutation == "calendar":
        consumers[2]["value"]["receipts"] = []
    else:
        consumers[2]["value"]["receipts"][0]["received_at"] = DAY + "T23:59:00+03:00"
    assert classification(consumers) == "unclassified"


def test_future_quote_does_not_rewrite_missing_observation(consumers):
    before = consumer_gaps(consumers[0], DAY)
    consumers[1]["quote"] = [dict(value={"symbol": "SBER", "bid": "100", "ask": "101"})]
    assert consumer_gaps(consumers[0], DAY) == before


def test_unverified_denied_and_schema_failure_unavailable(consumers):
    consumers[0]._verified = None
    with pytest.raises(ValueError, match="unverified"):
        consumer_gaps(consumers[0], DAY)
    consumers[0]._verified = {}
    consumers[1].pop("decision")
    assert consumer_gaps(consumers[0], DAY)["available"] is False


def test_missing_entry_explained_separately(consumers):
    consumers[1]["model_entry"][0]["value"].update(state="missing_quote", quote=None)
    consumers[1]["markout"] = []
    assert classification(consumers) == "in_session_missing"
