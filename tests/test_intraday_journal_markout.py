import json
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal as D

import pytest
from intraday_markout import Quote, Tape, analyze, markout
from replay_decisions import CASES, read
from test_intraday import MOSCOW, PROJECT_ROOT, _bars, _instrument, _portfolio

from moex_bot.intraday import IntradayStateStore, build_intraday_plan
from moex_bot.intraday_config import load_intraday_config

NOW = datetime(2026, 8, 20, 11, 0, tzinfo=MOSCOW)
CONFIG = load_intraday_config(PROJECT_ROOT / "config/intraday.json")


def calc(tmp_path, *, now=NOW, cfg=CONFIG, bars=None, portfolio=None):
    return build_intraday_plan(
        config=cfg,
        instruments={"SBER": _instrument()},
        store=IntradayStateStore(tmp_path / "state.sqlite"),
        portfolio=portfolio or _portfolio(),
        bars=_bars(now - timedelta(minutes=10)) if bars is None else bars,
        as_of=now,
    )


def test_journal_does_not_turn_rejections_into_orders(tmp_path):
    plan = calc(tmp_path)
    (item,) = plan.decisions
    assert (item.status, item.order_request_id) == ("planned", plan.orders[0].order_request_id)
    assert item.candidate == plan.signals[0]
    assert calc(tmp_path).decisions[0].reasons == ("duplicate",)
    zero = _portfolio()
    zero["cash"] = "0"
    rejection = calc(tmp_path, portfolio=zero).decisions[0]
    assert rejection.status == "entry_rejected" and set(rejection.reasons) == {
        "below_one_lot",
        "cash",
    }
    assert calc(tmp_path, cfg=replace(CONFIG, max_entries_per_day=1)).decisions[0].reasons == (
        "daily_entry_limit",
    )
    p = tmp_path / "plan.json"
    plan.write(p)
    assert json.loads(p.read_text())["signal_journal"]["decisions"][0]["candidate"]["side"] == "buy"


@pytest.mark.parametrize(
    "scenario", ["session", "slots", "short", "forced", "active", "filter", "data"]
)
def test_skip_reason_by_instrument(tmp_path, scenario):
    if scenario == "session":
        row = calc(tmp_path, now=NOW.replace(hour=9)).decisions[0]
        assert row.status == "entry_rejected" and row.reasons == ("outside_entry_window",)
    elif scenario == "slots":
        row = calc(
            tmp_path,
            portfolio=_portfolio(positions={"SBER": {"lots": 1, "blocked_lots": 0}}),
            cfg=replace(CONFIG, max_concurrent_positions=1),
        ).decisions[0]
        assert {"existing_position", "position_limit"} <= set(row.reasons)
    elif scenario == "short":
        row = calc(
            tmp_path,
            bars=_bars(NOW - timedelta(minutes=10), falling=True),
            cfg=replace(CONFIG, allow_short=False),
        ).decisions[0]
        assert row.reasons == ("short_unavailable",)
    elif scenario == "forced":
        plan = calc(
            tmp_path,
            now=NOW.replace(hour=19),
            portfolio=_portfolio(positions={"SBER": {"lots": 1, "blocked_lots": 0}}),
        )
        assert plan.decisions[0].status == "not_evaluated"
        path = tmp_path / "out.json"
        plan.write(path)
        assert json.loads(path.read_text())["signal_journal"]["exit_intents"][0]["secid"] == "SBER"
    elif scenario == "active":
        row = calc(tmp_path, portfolio=_portfolio() | {"open_orders": 1}).decisions[0]
        assert row.status == "not_evaluated" and row.reasons == ("open_orders",)
    elif scenario == "filter":
        row = calc(
            tmp_path, cfg=replace(CONFIG, min_price_move=D("0.04"), max_spread_bbo=D(".01"))
        ).decisions[0]
        assert {"spread_bbo", "price_move"} <= set(row.reasons)
        assert row.candidate and row.candidate.price_move > 0
    else:
        row = calc(tmp_path, bars=()).decisions[0]
        assert row.reasons == ("history",) and row.candidate is None


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_quotes_latency_spread_fees_and_missing_samples(side):
    def quote(sec, b, a, session="main"):
        return Quote(NOW + timedelta(seconds=sec), D(b), D(a), session)

    seq = [
        quote(0, 89, 90),
        quote(2, 99, 101),
        quote(300, 103, 105),
        quote(900, 120, 121, "evening"),
    ]
    tape = Tape({"S": {q.at: q for q in seq}}, proxy=False)

    def result(h):
        return markout(tape, "S", side, NOW, h, D(5), 2, 0, time(18, 35))

    m = result(5)
    entry, exit_ = (D(101), D(103)) if side == "buy" else (D(99), D(105))
    signed = 1 if side == "buy" else -1
    assert m["entry_price"] == entry and m["exit_price"] == exit_
    assert m["effective_delay_s"] == 2
    assert m["net_bps"] == signed * (exit_ / entry - 1) * 10000 - 5 * (1 + exit_ / entry)
    assert result(15)["missing"] == "invalid_holding_window"
    assert result(30)["missing"] == "no_exit_quote"
    assert result(600)["missing"] == "outside_session"
    assert markout(tape, "S", side, NOW, 5, D(5), 3, 0, time(18, 35))["missing"] == "no_entry_quote"


def test_report_repeats_missing_and_roundtrip(tmp_path):
    def write(i, t):
        plan = calc(tmp_path)
        plan.write(
            tmp_path / f"intraday-plan-{i:02}.json",
            analysis_input={
                "as_of": NOW,
                "decision_ready_at": t + timedelta(seconds=1),
                "config_snapshot": {"force_flat_moscow": "18:35:00"},
                "supercandles": [],
            },
        )
        p = tmp_path / f"intraday-plan-{i:02}.json"
        r = json.loads(p.read_text())
        r["run_id"] = str(i)
        r["signal_journal"]["decisions"][0]["status"] = "planned"
        r["analysis_input"]["supercandles"] = [
            {
                "secid": "SBER",
                "observed_at": t.isoformat(),
                "close": "100",
                "vwap": "100",
                "buy_value": "1",
                "sell_value": "1",
                "put_buy_value": "1",
                "put_sell_value": "1",
                "cancel_buy_value": "1",
                "cancel_sell_value": "1",
                "spread_bbo": "2",
                "book_imbalance": "0",
            }
        ]
        r["analysis_input"]["as_of"] = t.isoformat()
        p.write_text(json.dumps(r))

    for i in (0, 5, 10):
        write(i, NOW + timedelta(minutes=i))
    out = analyze(tmp_path, fee_bps=D(5), latency=1)
    assert out["source"] == "5m_close_spread_proxy" and out["runs"] == 3
    assert out["summary"]["planned/5"]["n"] == 1
    assert out["summary"]["planned/15"]["missing"] == 1
    assert out["observations"][1]["summary_excluded"] == "repeat_in_cohort"
    assert out["observations"][0]["horizons"]["5"]["net_bps"] < -11


def test_archived_signal_quotes_and_execution_evidence(tmp_path):
    inputs = read(CASES / "intraday_input.json")
    old = read(CASES / "intraday_expected.json")
    settings = read(CASES / "intraday_config_recorded.json")
    for index, batch in enumerate(inputs["batches"][:3]):
        r = dict(
            old,
            run_id=str(index),
            analysis_input={
                "as_of": batch["captured_at"],
                "config_snapshot": settings,
                "supercandles": batch["rows"],
            },
        )
        if index < 2:
            r.update(signals=[], orders=[])
        (tmp_path / f"intraday-plan-{index}.json").write_text(json.dumps(r))
    at = datetime.fromisoformat(inputs["as_of"])
    quotes = tmp_path / "book.jsonl"
    quotes.write_text(
        "\n".join(
            json.dumps(
                {
                    "at": (at + timedelta(seconds=n)).isoformat(),
                    "session": "main",
                    "secid": "PHOR",
                    "bid": "100",
                    "ask": "101",
                }
            )
            for n in (1, 300)
        )
    )
    record = dict(old["orders"][0], status="filled", filled_lots=1)
    execution = tmp_path / "intraday-execution-2.json"
    execution.write_text(json.dumps({"run_id": "2", "submitted": [record], "stopped_reason": None}))
    kwargs = dict(
        fee_bps=D(5),
        latency=1,
        quotes_path=quotes,
        legacy_config=CASES / "intraday_config.json",
        legacy_universe=CASES / "universe.json",
    )
    out = analyze(tmp_path, **kwargs)
    (sig,) = [r for r in out["observations"] if r["status"] == "planned"]
    assert sig["origin"] == "archived_signal" and sig["secid"] == "PHOR"
    assert sig["execution_evidence"] == {
        "status": "filled", "filled_lots": 1, "cycle_stopped_reason": None
    }
    assert sig["horizons"]["5"]["net_bps"] == D("-110.05")
    assert sig["horizons"]["60"] == {"missing": "no_exit_quote"}
    assert out["source"] == "top_of_book"
    execution.write_text(json.dumps({"run_id": "wrong"}))
    with pytest.raises(ValueError, match="run_id"):
        analyze(tmp_path, **kwargs)
