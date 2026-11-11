import io
import json
import time
from copy import deepcopy
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from test_journal_readmodel import fixture_report

from moex_bot.journal_cards import (
    attach_charts,
    attach_order_context,
    chart_for,
    chart_requests,
    fetch_day,
    make_cards,
    reason_context,
)
from moex_bot.journal_import import binding
from moex_bot.journal_readmodel import QueryError, compare, publish, query
from moex_bot.trader_journal import encoded


def test_cards_preserve_fifo_results_partial_closures_and_unknown_fees(tmp_path):
    report = fixture_report()
    before = deepcopy(report)
    path = tmp_path / "read.sqlite3"
    generation = publish(report, path)
    assert report == before
    assert compare(report, path)["rows"] > 0
    stream = report["streams"][0]
    params = {"generation": generation, "stream": stream["id"], "period": "2026-11",
              "symbol": "SBER"}
    result = query(path, "detail", params)
    assert result["total"] == 4
    assert len({o["ref"] for o in result["options"]}) == 4
    for option in result["options"]:
        card = query(path, "detail", {**params, "ref": option["ref"]})["card"]
        if card["kind"] == "closed":
            original = next(t for t in stream["trades"] if t["closed_at"] == card["closed_at"])
            assert all(card[key] == value for key, value in original.items())
            assert card["net"] == original["net"]  # Unknown stays unknown, not zero.
            assert card["markers"][0]["price"] == original["entry"]
            assert card["entry_reason"]["status"] == "unlinked"
        else:
            assert "gross" not in card  # Operation turnover is not profit.
        assert not card["chart"]["points"]
    assert "private-account" not in encoded(result)
    # Closed trade includes entry outside the selected reporting month.
    assert any(c["opened_at"].startswith("2026-10") for c in make_cards(stream)
               if c["kind"] == "closed")
    with pytest.raises(QueryError, match="not found"):
        query(path, "detail", {**params, "stream": report["streams"][1]["id"],
                               "ref": result["options"][0]["ref"]})
    with pytest.raises(QueryError) as changed:
        query(path, "detail", {**params, "generation": "obsolete"})
    assert changed.value.status == 409
    with pytest.raises(QueryError):
        query(path, "detail", {**params, "symbol": "../bot.env"})


@pytest.mark.parametrize("key,value", [("offset", "-1"), ("offset", "9" * 30),
                                      ("ref", "../../private"), ("symbol", "SBER';drop")])
def test_card_query_rejects_bad_parameters(tmp_path, key, value):
    report = fixture_report()
    path = tmp_path / "read.sqlite3"
    gen = publish(report, path)
    with pytest.raises(QueryError):
        query(path, "detail", {"generation": gen, "stream": report["streams"][0]["id"],
                               "period": "2026-11", "symbol": "SBER", key: value})


def test_card_options_are_bounded_and_account_scoped(tmp_path):
    report = fixture_report()
    stream = report["streams"][0]
    original = stream["trades"][0]
    stream["trades"] = [{**original, "quantity": str(i + 1)} for i in range(125)]
    path = tmp_path / "read.sqlite3"
    gen = publish(report, path)
    p = {"generation": gen, "stream": stream["id"], "period": "2026-11", "symbol": "SBER"}
    seen = set()
    offset = 0
    while True:
        page = query(path, "detail", {**p, "offset": str(offset)})
        assert len(page["options"]) <= 50
        refs = {o["ref"] for o in page["options"]}
        assert not seen & refs
        seen |= refs
        offset = page["next_offset"]
        if offset is None:
            break
    assert len(seen) == 127
    # A click on a row outside the first page selects that exact closure.
    target = stream["trades"][124]
    anchor = {k: target[k] for k in ("opened_at", "quantity", "entry", "exit")}
    anchor.update(kind="closed", at=target["closed_at"])
    selected = query(path, "detail", {**p, "anchor": encoded(anchor)})
    assert selected["card"]["quantity"] == "125"
    assert selected["card"]["ref"] in {o["ref"] for o in selected["options"]}
    with pytest.raises(QueryError):
        query(path, "detail", {**p, "anchor": encoded({"kind": "closed", "at": "missing"})})
    with pytest.raises(QueryError):
        query(path, "detail", {**p, "anchor": encoded({"kind": "closed", "at": "x",
                                                        "invalid') OR 1=1 --": "x"})})


def test_reasons_do_not_infer_causation_or_use_future_signals():
    stream = {"signals": [
        {"symbol": "SBER", "at": "2026-11-01T07:01Z", "side": "buy"},
        {"symbol": "SBER", "at": "2026-11-01T06:59Z", "side": "sell"},
        {"symbol": "SBER", "at": "2026-11-01T06:58Z", "side": "buy"},
        {"symbol": "SBER", "at": "2026-11-01T05:00Z", "side": "buy"},
    ]}
    reason = reason_context(stream, "SBER", "2026-11-01T07:00Z", "BUY")
    assert reason["status"] == "unlinked" and reason["reason"] is None
    assert len(reason["context"]) == 1
    assert reason["context"][0]["at"].endswith("06:58Z")


def test_order_context_is_account_bound_not_a_confirmed_execution_reason(tmp_path):
    account = "private-account"
    stream = {"id": "intraday:" + binding(account)[:16]}
    config = {"artifact_root": str(tmp_path), "profiles": [
        {"id": "intraday", "portfolio_globs": ["*-portfolio-*.json"]}]}
    (tmp_path / "test-portfolio-1.json").write_text(encoded({
        "account_id": account, "source": "t_invest_sandbox"}))
    (tmp_path / "test-execution-1.json").write_text(encoded({
        "run_id": "intraday-2026-11-01T07:00:00Z", "submitted": [{"intent": {
            "instrument": {"secid": "SBER"}, "side": "SELL", "rationale": "stop_loss:20bps"}}]}))
    report = {"streams": [stream, {"id": "intraday:other"}]}
    attach_order_context(report, config)
    assert stream["order_context"][0]["rationale"] == "stop_loss:20bps"
    assert "order_context" not in report["streams"][1]
    assert account not in encoded(report)


def page(rows):
    return io.BytesIO(json.dumps({"candles": {"columns": ["begin", "end", "open", "high",
                                                         "low", "close"], "data": rows}}).encode())


def test_public_candles_paginate_by_returned_count_and_use_msk(monkeypatch):
    requests = []
    rows = [["2026-11-01 10:00:00", "2026-11-01 10:00:59", 100, 102, 99, 101],
            ["2026-11-01 10:01:00", "2026-11-01 10:01:59", 101, 103, 100, 102]]

    def get(url, timeout):
        params = parse_qs(urlsplit(url).query)
        requests.append((url, timeout))
        start = int(params["start"][0])
        return page(rows[start:start + 1])

    monkeypatch.setattr("moex_bot.journal_cards.urlopen", get)
    result = fetch_day("SBER", "2026-11-01", 1, time.monotonic() + 10)
    assert [parse_qs(urlsplit(r[0]).query)["start"] for r in requests] == [["0"], ["1"], ["2"]]
    assert all(r[0].startswith("https://iss.moex.com/") for r in requests)
    assert len(result["points"]) == 2
    assert result["points"][0]["at"] == "2026-11-01T10:00:59+03:00"


@pytest.mark.parametrize("values", [(100, 102, 99, "NaN"), (100, 95, 99, 100),
                                    (100, 102, 0, 100)])
def test_candle_invalid_prices_not_replaced_by_zero(monkeypatch, values):
    monkeypatch.setattr("moex_bot.journal_cards.urlopen", lambda *a, **k: page([
        ["2026-11-01 10:00:00", "2026-11-01 10:00:59", *values]]))
    with pytest.raises(ValueError):
        fetch_day("SBER", "2026-11-01", 1, time.monotonic() + 10)


def test_cache_read_does_not_network_and_error_is_visible_with_old_prices(tmp_path):
    report = fixture_report()
    calls = []

    def fetch(symbol, day, interval, deadline):
        calls.append(symbol)
        raise TimeoutError("sensitive url must not be published")

    attach_charts(report, tmp_path, refresh=False, fetch=fetch)
    assert not calls and all(c["status"] == "pending" for c in report["charts"].values())
    attach_charts(report, tmp_path, refresh=True, fetch=fetch)
    assert calls and all(c["status"] == "error" for c in report["charts"].values())
    assert "sensitive" not in encoded(report)
    calls.clear()
    attach_charts(report, tmp_path, refresh=True, fetch=fetch)
    assert not calls  # Backoff for failed chart source, not a request storm.


def test_chart_is_clipped_keeps_gaps_and_never_changes_markers():
    stream = fixture_report()["streams"][0]
    card = next(c for c in make_cards(stream) if c["kind"] == "closed")
    before = deepcopy(card)
    day = card["closed_at"][:10]
    chart = chart_for(card, {f"SBER:{day}:60": {
        "status": "ok", "source": "MOEX ISS", "points": [],
        "retrieved_at": datetime.now(UTC).isoformat()}}, 60)
    assert chart["points"] == [] and card == before
    assert len(chart_requests({"streams": [stream]})) < 100
