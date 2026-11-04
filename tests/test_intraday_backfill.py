"""Late data informs new decisions; old artifacts and first-seen facts are immutable."""

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from test_intraday import _bars, _instrument, _portfolio

from moex_bot import cli
from moex_bot.integrations.algopack_intraday import AlgoPackIntradayAdapter
from moex_bot.intraday import IntradayStateStore, build_intraday_plan
from moex_bot.intraday_config import load_intraday_config

ROOT = Path(__file__).resolve().parents[1]
MSK = ZoneInfo("Europe/Moscow")
AT = datetime(2026, 11, 3, 11, 10, tzinfo=MSK)


def adapter_with_rows(monkeypatch, *, missing=None, include_future=True):
    adapter = AlgoPackIntradayAdapter("fixture", Mock())
    starts = [AT - timedelta(minutes=10), AT - timedelta(minutes=5), AT]
    starts += [AT - timedelta(days=1)]
    if include_future:
        starts.append(AT + timedelta(minutes=5))

    def day_rows(dataset, day, wanted):
        return {
            ("SBER", at): {
                "pr_close": 100,
                "pr_vwap": 100,
                "val_b": 700,
                "val_s": 300,
                "put_val_b": 900,
                "put_val_s": 100,
                "cancel_val_b": 0,
                "cancel_val_s": 0,
                "spread_bbo": 1,
                "imbalance_val": 0.4,
            }
            for at in starts
            if (dataset, at) != missing
        }

    monkeypatch.setattr(adapter, "_day_rows", day_rows)
    return adapter


def test_history_returns_bounded_current_session_common_window(monkeypatch):
    adapter = adapter_with_rows(monkeypatch)
    actual = adapter.history(secids=("SBER",), as_of=AT, bars_per_secid=3)
    assert [b.observed_at for b in actual] == [
        AT - timedelta(minutes=10),
        AT - timedelta(minutes=5),
        AT,
    ]
    assert adapter.latest(secids=("SBER",), as_of=AT) == actual[-1:]
    # Adding future rows cannot affect an earlier window.
    before = adapter_with_rows(monkeypatch, include_future=False).history(
        secids=("SBER",), as_of=AT, bars_per_secid=3
    )
    assert before == actual


@pytest.mark.parametrize("dataset", ["tradestats", "orderstats", "obstats"])
def test_missing_dataset_interval_is_not_synthesized(monkeypatch, dataset):
    adapter = adapter_with_rows(monkeypatch, missing=(dataset, AT - timedelta(minutes=5)))
    bars = adapter.history(secids=("SBER",), as_of=AT, bars_per_secid=3)
    assert [b.observed_at for b in bars] == [AT - timedelta(minutes=10), AT]


@pytest.mark.parametrize("count", [0, -1, 289, True, 1.5])
def test_history_count_is_bounded(count):
    with pytest.raises(ValueError, match="bars_per_secid"):
        AlgoPackIntradayAdapter("fixture", Mock()).history(
            secids=("SBER",), as_of=AT, bars_per_secid=count
        )


def test_late_backfill_new_signal_preserves_old_plan_and_availability(tmp_path):
    store = IntradayStateStore(tmp_path / "intraday.sqlite3")
    config = load_intraday_config(ROOT / "config/intraday.json")
    bars = _bars(AT - timedelta(minutes=10))
    first_received = AT + timedelta(seconds=30)
    first = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(),
        store=store,
        bars=(bars[0], bars[2]),
        as_of=AT,
        received_at=first_received,
    )
    old_path = tmp_path / "first.json"
    first.write(old_path, analysis_input={"as_of": AT, "received_at": first_received})
    original = old_path.read_bytes()
    assert not first.orders
    later = AT + timedelta(minutes=5)
    added = replace(bars[2], observed_at=later, close=bars[2].close + 1)
    second = build_intraday_plan(
        config=config,
        instruments={"SBER": _instrument()},
        portfolio=_portfolio(),
        store=store,
        bars=(bars[1], bars[2], added),
        as_of=later,
        received_at=later + timedelta(seconds=30),
    )
    assert second.orders
    assert store.recent("SBER", 3, as_of=later) == (bars[1], bars[2], added)
    assert store.recent("SBER", 3, as_of=AT, available_at=first_received) == (bars[0], bars[2])
    assert old_path.read_bytes() == original
    with pytest.raises(FileExistsError):
        second.write(old_path)
    assert old_path.read_bytes() == original
    # Published corrections never rewrite the first-seen payload or receipt.
    store.add((replace(bars[2], close=bars[2].close + 10),), received_at=later)
    assert store.recent("SBER", 3, as_of=later)[1] == bars[2]
    with sqlite3.connect(store.path) as db:
        assert db.execute(
            "SELECT received_at FROM intraday_bar_receipts WHERE observed_at=?",
            (bars[2].observed_at.isoformat(),),
        ).fetchone() == (first_received.isoformat(),)


def test_existing_plan_refused_before_api_or_state_mutation(tmp_path, monkeypatch):
    path = tmp_path / "old.json"
    path.write_bytes(b"old evidence")
    adapter = Mock()
    monkeypatch.setattr(cli.AlgoPackIntradayAdapter, "from_environment", adapter)
    assert (
        cli.intraday_plan(
            config_path=tmp_path,
            universe_path=tmp_path,
            portfolio_path=tmp_path,
            state_path=tmp_path / "never-created.db",
            output_path=path,
            as_of=AT,
            outbox_path=None,
        )
        == 2
    )
    adapter.assert_not_called()
    assert path.read_bytes() == b"old evidence"
    assert not (tmp_path / "never-created.db").exists()


def test_additive_migration_retains_legacy_rows_and_current_day_boundary(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    bars = _bars(AT - timedelta(minutes=10))
    store = IntradayStateStore(path)
    store.add((replace(bars[0], observed_at=AT - timedelta(days=1)), *bars))
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE intraday_bar_receipts")
        before = db.execute("SELECT * FROM intraday_bars ORDER BY observed_at").fetchall()
    migrated = IntradayStateStore(path)
    assert migrated.recent("SBER", 3, as_of=AT.astimezone(UTC)) == bars
    with sqlite3.connect(path) as db:
        assert before == db.execute("SELECT * FROM intraday_bars ORDER BY observed_at").fetchall()
        assert db.execute("SELECT count(*) FROM intraday_bar_receipts").fetchone() == (0,)


def test_cli_records_actual_cached_window_and_receipt_time(tmp_path, monkeypatch):
    bars = _bars(AT - timedelta(minutes=10))
    adapter = Mock()
    adapter.history.return_value = bars
    monkeypatch.setattr(cli.AlgoPackIntradayAdapter, "from_environment", lambda: adapter)
    monkeypatch.setattr(cli, "_verified_instruments_by_secid", lambda _: {"SBER": _instrument()})
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(json.dumps(_portfolio()))
    output = tmp_path / "new.json"
    assert (
        cli.intraday_plan(
            config_path=ROOT / "config/intraday.json",
            universe_path=ROOT / "config/universe.json",
            portfolio_path=portfolio,
            state_path=tmp_path / "state.db",
            output_path=output,
            as_of=AT,
            outbox_path=None,
        )
        == 0
    )
    raw = json.loads(output.read_text())
    adapter.latest.assert_not_called()
    assert adapter.history.call_args.kwargs["bars_per_secid"] == 3
    assert len(raw["analysis_input"]["history_used"]) == 3
    assert raw["analysis_input"]["market_data_received_at"]
    assert "insert-only" in raw["analysis_input"]["history_policy"]


@pytest.mark.parametrize(("bad", "reason"), [
    (None, "null"), ("", "not_numeric"), ("malformed", "not_numeric"),
    ("NaN", "nonfinite"), (float("inf"), "nonfinite"), (False, "not_numeric"),
])
def test_bad_obstats_spread_is_not_zero_and_does_not_poison_other_symbols(monkeypatch, bad, reason):
    adapter = adapter_with_rows(monkeypatch)
    original = adapter._day_rows

    def mixed(dataset, day, wanted):
        rows = original(dataset, day, wanted)
        rows.update({("MOEX", at): row | {"spread_bbo": 0}
                     for (_, at), row in list(rows.items())})
        if dataset == "obstats":
            rows["SBER", AT]["spread_bbo"] = bad
        return rows

    monkeypatch.setattr(adapter, "_day_rows", mixed)
    issues = []
    bars = adapter.history(secids=("SBER", "MOEX"), as_of=AT, bars_per_secid=3, invalid=issues)
    issue, = issues
    assert (issue.secid, issue.observed_at, issue.dataset, issue.field, issue.reason) == (
        "SBER", AT, "obstats", "spread_bbo", reason)
    assert not any(b.secid == "SBER" and b.observed_at == AT for b in bars)
    assert any(b.secid == "MOEX" and b.observed_at == AT and b.spread_bbo == 0 for b in bars)
    with pytest.raises(ValueError, match="spread_bbo"):
        adapter.latest(secids=("SBER",), as_of=AT)


def test_invalid_obstats_window_cannot_enter_from_cached_signal(tmp_path):
    store = IntradayStateStore(tmp_path / "intraday.sqlite3")
    bars = _bars(AT - timedelta(minutes=10))
    store.add(bars)
    plan = build_intraday_plan(config=load_intraday_config(ROOT / "config/intraday.json"),
                              instruments={"SBER": _instrument()}, portfolio=_portfolio(),
                              store=store, bars=bars[:-1], as_of=AT,
                              invalid_symbols=frozenset({"SBER"}))
    assert not plan.orders and not plan.signals
    assert plan.decisions[0].reasons == ("invalid_algopack_metric",)


def test_cli_bad_spread_writes_failed_quality_plan_not_traceback(tmp_path, monkeypatch, capsys):
    adapter = adapter_with_rows(monkeypatch)
    original = adapter._day_rows

    def invalid_latest(dataset, day, wanted):
        rows = original(dataset, day, wanted)
        if dataset == "obstats":
            del rows["SBER", AT]["spread_bbo"]
        return rows

    monkeypatch.setattr(adapter, "_day_rows", invalid_latest)
    monkeypatch.setattr(cli.AlgoPackIntradayAdapter, "from_environment", lambda: adapter)
    monkeypatch.setattr(cli, "_verified_instruments_by_secid", lambda _: {"SBER": _instrument()})
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(json.dumps(_portfolio()))
    output = tmp_path / "plan.json"
    assert cli.intraday_plan(config_path=ROOT / "config/intraday.json",
                             universe_path=ROOT / "config/universe.json", portfolio_path=portfolio,
                             state_path=tmp_path / "state.db", output_path=output, as_of=AT,
                             outbox_path=None) == 2
    plan = json.loads(output.read_text())
    assert not plan["quality"]["passed"] and not plan["orders"]
    assert plan["analysis_input"]["history_used"] == []
    assert plan["analysis_input"]["invalid_bars"] == [
        {"secid": "SBER", "observed_at": str(AT), "dataset": "obstats",
         "field": "spread_bbo", "reason": "missing"},
    ]
    assert "obstats.spread_bbo missing; SBER" in capsys.readouterr().out
