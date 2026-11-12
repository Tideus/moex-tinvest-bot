import copy
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
import universe_capture as capture
import universe_history as history
import universe_watch as watch

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=watch.MSK)


@pytest.fixture
def cfg():
    return watch.policy(ROOT / "scripts/research/universe24.json")


def quote(**changes):
    return {
        "secid": "SBER",
        "source": "observed_top_of_book",
        "received_at": NOW.isoformat(),
        "exchange_at": (NOW - timedelta(seconds=1)).isoformat(),
        "instrument_verified_at": NOW.isoformat(),
        "trading_allowed": True,
        "bid": "99.99",
        "ask": "100.01",
        "lot_size": 10,
        "bid_size_units": 10000,
        "ask_size_units": 10000,
        "turnover_sessions": [
            {"date": (NOW - timedelta(days=n)).date().isoformat(), "value_rub": "100000000"}
            for n in range(1, 21)
        ],
        **changes,
    }


def test_universe_and_trading_parameters_are_separate(cfg):
    production = json.loads((ROOT / "config/universe.json").read_bytes())
    assert cfg["symbols"] == [e["secid"] for e in production]
    assert {k: len(v) for k, v in watch.cohorts(cfg).items()} == {
        "control3": 3,
        "expansion21": 21,
        "all24": 24,
    }
    assert cfg["mode"] == "research_only"
    assert cfg["order_budget_rub"] == "10000"


def test_spread_cost_units_and_control_coverage(cfg):
    r = watch.screen({"as_of": NOW.isoformat(), "rows": [quote()]}, cfg)
    row = r["rows"][0]
    assert row["eligible"] and row["spread_bps"] == Decimal("2")
    assert row["roundtrip_cost_bps"] == 22
    assert row["stress_roundtrip_cost_bps"] == 44
    assert row["hypothetical_units"] == 90 and row["capacity_units"] == 1000
    assert r["cohorts"]["control3"]["received"] == 1
    assert r["cohorts"]["all24"]["rejections"]["missing_symbol"] == 23


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"bid": "101", "ask": "100"}, "quote:invalid_bid_ask"),
        ({"bid": "NaN"}, "quote:finite number required"),
        ({"source": "5m_close_spread_proxy"}, "quote:unverified_quote_source"),
        ({"trading_allowed": False}, "quote:trading_unavailable"),
        ({"trading_allowed": "true"}, "quote:trading_unavailable"),
        ({"exchange_at": (NOW - timedelta(seconds=4)).isoformat()}, "quote:stale_or_future_quote"),
        ({"received_at": (NOW + timedelta(seconds=1)).isoformat()}, "quote:stale_or_future_quote"),
        ({"exchange_at": NOW.replace(tzinfo=None).isoformat()}, "quote:timezone required"),
        ({"bid": "99", "ask": "101"}, "wide_spread"),
        ({"bid_size_units": 100}, "insufficient_top_book_depth"),
        ({"lot_size": 1000}, "below_one_lot"),
        ({"lot_size": 1.5}, "metadata:invalid_lot_size"),
        ({"lot_size": True}, "metadata:boolean is not a number"),
        ({"ask_size_units": -1}, "metadata:invalid_depth_units"),
        (
            {"instrument_verified_at": (NOW - timedelta(days=8)).isoformat()},
            "stale_instrument_metadata",
        ),
        ({"turnover_sessions": []}, "insufficient_turnover_history"),
    ],
)
def test_invalid_or_missing_evidence_never_passes(cfg, changes, reason):
    r = watch.screen_one(quote(**changes), NOW, cfg)
    assert not r["eligible"] and reason in r["reasons"]


def test_future_turnover_cannot_improve_screen(cfg):
    row = quote()
    row["turnover_sessions"] = [r | {"value_rub": "1"} for r in row["turnover_sessions"]]
    before = watch.screen_one(row, NOW, cfg)
    row["turnover_sessions"].append({"date": NOW.date().isoformat(), "value_rub": "1e30"})
    assert watch.screen_one(row, NOW, cfg) == before
    assert "low_turnover" in before["reasons"]
    row["turnover_sessions"].append(row["turnover_sessions"][0])
    assert "metadata:duplicate_or_invalid_turnover" in watch.screen_one(row, NOW, cfg)["reasons"]


def test_snapshot_missing_duplicate_unknown_and_per_symbol_time(cfg):
    with pytest.raises(ValueError, match="duplicate"):
        watch.screen({"as_of": NOW.isoformat(), "rows": [quote(), quote()]}, cfg)
    with pytest.raises(ValueError, match="unknown"):
        watch.screen({"as_of": NOW.isoformat(), "rows": [quote(secid="UNKNOWN")]}, cfg)
    row = quote(decision_at=NOW.isoformat())
    after_batch = NOW + timedelta(seconds=25)
    assert watch.screen({"as_of": after_batch.isoformat(), "rows": [row]}, cfg)["rows"][0][
        "eligible"
    ]
    with pytest.raises(ValueError, match="decision time"):
        watch.screen({"as_of": (NOW - timedelta(seconds=1)).isoformat(), "rows": [row]}, cfg)


def test_closed_window_and_partial_capacity(cfg):
    row = quote(bid_size_units=500)
    result = watch.screen_one(row, NOW, cfg)
    assert result["capacity_units"] == 50
    assert result["partial_capacity_ratio"] == Decimal(50) / 90
    after = NOW.replace(hour=19)
    row = quote(received_at=after.isoformat(), exchange_at=after.isoformat())
    r = watch.screen({"as_of": after.isoformat(), "rows": [row]}, cfg)["rows"][0]
    assert "outside_research_entry_window" in r["reasons"]


def test_cohort_means_do_not_include_repeats_or_merge_rejected_signals(cfg):
    row = {
        "secid": "SBER",
        "strategy_id": "v1",
        "status": "planned",
        "horizons": {"5": {"net_bps": 20}, "15": {"missing": "no_exit_quote"}},
    }
    report = {
        "observations": [
            row,
            row | {"summary_excluded": "repeat_in_cohort"},
            row | {"secid": "LKOH", "status": "filter_rejected"},
        ]
    }
    groups = watch.cohort_markouts(report, cfg)
    assert groups["control3"]["v1/planned/5"] == {
        "n": 1,
        "missing": 0,
        "mean_net_bps": 20,
        "median_net_bps": 20,
    }
    assert groups["all24"]["v1/planned/15"]["missing"] == 1
    assert "v1/filter_rejected/5" in groups["expansion21"]


def test_weekly_does_not_promote_or_count_incomplete_sessions(cfg):
    base = {
        "policy_sha256": watch.digest(cfg),
        "expected_snapshots": 100,
        "observed_snapshots": 100,
        "quote_coverage": "0.99",
        "symbols": cfg["symbols"],
        "closed": True,
    }
    receipts = [base | {"date": f"2026-10-{d:02}"} for d in range(1, 11)]
    r = watch.weekly(receipts, cfg)
    assert r["complete_sessions"] == 10 and r["operational_status"] == "READY_FOR_REVIEW"
    assert not r["promotion_to_trading"] and r["parameter_changes"] == []
    assert r["profitability_status"] == "NOT_ESTABLISHED"
    receipts[0]["observed_snapshots"] = 99
    receipts[1]["policy_sha256"] = "changed"
    receipts[2]["quote_coverage"] = "NaN"
    receipts[3]["symbols"] = cfg["control_symbols"]
    r = watch.weekly(receipts, cfg)
    assert r["complete_sessions"] == 6 and len(r["rejected_sessions"]) == 4
    with pytest.raises(ValueError, match="duplicate"):
        watch.weekly([receipts[0], receipts[0]], cfg)


def test_future_session_cannot_count_as_completed(cfg):
    receipt = {
        "date": NOW.date().isoformat(),
        "policy_sha256": watch.digest(cfg),
        "expected_snapshots": 98,
        "observed_snapshots": 98,
        "quote_coverage": 1,
        "symbols": cfg["symbols"],
        "closed": True,
    }
    result = watch.weekly([receipt], cfg, as_of=NOW)
    assert result["complete_sessions"] == 0
    assert "unfinished_or_future_session" in result["rejected_sessions"][0]["reasons"]


def test_session_receipt_recomputes_evidence_and_counts_missing_slots(cfg, tmp_path):
    snapshot = {"as_of": NOW.isoformat(), "rows": [quote()]}
    capture = {
        "started_at": NOW.isoformat(),
        "snapshot": snapshot,
        "screen": watch.screen(snapshot, cfg),
    }
    path = tmp_path / "capture-1200.json"
    watch.write_result(path, capture)
    receipt = watch.session_receipt(tmp_path, NOW.date(), cfg, as_of=NOW + timedelta(days=1))
    assert receipt["observed_snapshots"] == 1 and receipt["expected_snapshots"] == 98
    assert len(receipt["missing_slots"]) == 97
    assert receipt["quote_coverage"] == Decimal(1) / (24 * 98)
    assert receipt["closed"]
    capture["screen"]["rows"][0]["eligible"] = False
    path.write_text(json.dumps(capture, default=str))
    with pytest.raises(ValueError, match="does not match"):
        watch.session_receipt(tmp_path, NOW.date(), cfg, as_of=NOW + timedelta(days=1))


def test_evidence_is_immutable(tmp_path):
    target = tmp_path / "receipt.json"
    watch.write_result(target, {"n": 1})
    with pytest.raises(FileExistsError):
        watch.write_result(target, {"n": 2})
    assert json.loads(target.read_bytes()) == {"n": 1}


def test_reader_blocks_orders_accounts_and_redirects():
    reader = capture.Reader("test-only-not-a-credential")
    for method, body in [
        ("OrdersService/PostOrder", {}),
        ("MarketDataService/GetOrderBook", {"accountId": "forbidden"}),
    ]:
        with pytest.raises(PermissionError):
            reader.read(method, body)
    assert capture.NoRedirect().redirect_request(None) is None
    with pytest.raises(ValueError, match="missing"):
        capture.Reader("")


def market_reader():
    reader = Mock()
    reader.read.side_effect = [
        {
            "instrument": {
                "uid": "uid",
                "ticker": "SBER",
                "classCode": "TQBR",
                "currency": "rub",
                "apiTradeAvailableFlag": True,
                "lot": 10,
            }
        },
        {
            "instrumentUid": "uid",
            "apiTradeAvailableFlag": True,
            "limitOrderAvailableFlag": True,
            "tradingStatus": "SECURITY_TRADING_STATUS_NORMAL_TRADING",
        },
        {
            "instrumentUid": "uid",
            "orderbookTs": NOW.isoformat(),
            "bids": [{"price": {"units": "99", "nano": 990000000}, "quantity": "50"}],
            "asks": [{"price": {"units": "100", "nano": 10000000}, "quantity": "60"}],
        },
    ]
    return reader


def test_capture_converts_book_lots_to_units_and_preserves_raw_times():
    reader = market_reader()
    row = capture.capture_one(
        {"secid": "SBER", "t_invest_uid": "uid", "class_code": "TQBR"}, [], reader, lambda: NOW
    )
    assert row["bid_size_units"] == "500" and row["ask_size_units"] == "600"
    assert row["bid"] == "99.99" and row["ask"] == "100.01"
    assert row["received_at"] == row["exchange_at"] == row["decision_at"] == NOW.isoformat()
    assert row["trading_allowed"]
    assert {call.args[0] for call in reader.read.call_args_list} == capture.READS


def test_capture_rejects_identity_mismatch():
    with pytest.raises(ValueError, match="identity"):
        capture.capture_one(
            {"secid": "LKOH", "t_invest_uid": "uid", "class_code": "TQBR"},
            [],
            market_reader(),
            lambda: NOW,
        )


def daily_bars(n=250):
    result = []
    for i in range(n):
        at = datetime(2025, 1, 1) + timedelta(days=i)
        price = 100 + i / 10
        result.append(
            {
                "at": at,
                "end": at + timedelta(hours=23),
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price + 0.5,
                "value": 100_000_000,
                "volume": 1_000_000,
            }
        )
    return result


def test_cost_calculation_includes_both_commissions_and_double_impact(cfg):
    params = cfg["historical_probes"]
    cost1 = history.net_markout(100, 100, 1, params)
    expected = (99.9 * 0.9995 - 100.1 * 1.0005) / 100.1 * 10000
    assert cost1 == pytest.approx(expected)
    assert history.net_markout(100, 100, 2, params) < cost1 < -29


def test_long_probe_uses_next_open_and_ignores_future_signal_inputs(cfg):
    daily = daily_bars(310)
    records, _ = history.probes("SBER", daily, [], cfg, date(2025, 1, 1), date(2025, 12, 31))
    assert records
    first = records[0]
    assert first["signal_at"] < first["entry_at"] < first["exit_at"]
    changed = copy.deepcopy(daily)
    for r in changed:
        if r["at"] > datetime.fromisoformat(first["exit_at"]):
            r["close"] *= 10
    again, _ = history.probes("SBER", changed, [], cfg, date(2025, 1, 1), date(2025, 12, 31))
    assert again[0] == first


def test_split_discontinuity_is_quarantined_without_backfill(cfg):
    daily = daily_bars(310)
    for r in daily[200:]:
        for k in ("open", "high", "low", "close"):
            r[k] /= 100
    records, audit = history.probes("SBER", daily, [], cfg, date(2025, 1, 1), date(2025, 12, 31))
    assert not records
    assert audit["large_gaps"] == [daily[200]["at"].isoformat()]
    assert audit["skips"]["long_corporate_action_quarantine"] > 0


def test_intraday_requires_all_bars_and_does_not_use_future_turnover(cfg):
    daily = daily_bars(30)
    day = daily[-1]["at"]
    bars = []
    for minute in range(600, 800, 10):
        at = day + timedelta(minutes=minute)
        price = 100 if minute < 650 else 99
        bars.append(
            {
                "at": at,
                "end": at + timedelta(minutes=9, seconds=59),
                "open": price,
                "close": price,
                "volume": 10000,
                "value": 1_000_000,
            }
        )
    records, _ = history.probes("SBER", daily, bars, cfg, day.date(), day.date())
    assert len(records) == 1
    assert records[0]["entry_at"].endswith("11:10:00")
    assert records[0]["exit_at"].endswith("13:10:00")
    daily[-1]["value"] = 0
    assert history.probes("SBER", daily, bars, cfg, day.date(), day.date())[0] == records
    bars.pop(8)
    missing, audit = history.probes("SBER", daily, bars, cfg, day.date(), day.date())
    assert not missing and audit["skips"]["intraday_missing_holding_bar"] == 1


def test_downloader_refuses_unfinished_dates(tmp_path):
    today = datetime.now(UTC).date()
    with pytest.raises(ValueError, match="completed"):
        history.candles("SBER", "TQBR", 10, today, today, tmp_path)
