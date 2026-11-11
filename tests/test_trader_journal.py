import json
import sqlite3
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from moex_bot.journal_import import binding, import_artifacts, main, plan_events, portfolio_events
from moex_bot.trader_journal import (
    Journal,
    build_report,
    match_trades,
    number,
    period_result,
    timestamp,
)

SPEC = {"SBER": {"lot_size_verified": 10, "asset_class": "share"}}
PROFILE = {"id": "long", "environment": "sandbox"}
T0 = "2026-11-02T07:00:00+00:00"
T1 = "2026-11-02T08:00:00+00:00"
T2 = "2026-11-02T09:00:00+00:00"


def point(at=T0, equity="10000", positions=None):
    return {
        "source": "t_invest_sandbox",
        "account_id": "private-account",
        "observed_at": at,
        "reported_equity": equity,
        "positions": positions or {},
        "executed_operations": [],
    }


def operation(identity="one", at=T1, side="BUY", qty="10", gross="3000", fee="3", external="0"):
    return {
        "operation_id": identity,
        "occurred_at": at,
        "secid": "SBER",
        "side": side,
        "quantity": qty,
        "gross": gross,
        "commission": fee,
        "external_cashflow": external,
    }


def events(ops, **kwargs):
    raw = point(**kwargs)
    raw["executed_operations"] = ops
    return portfolio_events(raw, PROFILE, SPEC, "source-checksum")


def trades(ops, baseline=None):
    return match_trades(events(ops, at=T2)[1:], baseline or events([])[0])


def test_partial_close_fee_allocation_and_gross_percent():
    rows, errors = trades([operation(), operation("two", T2, "SELL", "4", "1240", "1.24")])
    assert not errors
    assert Decimal(rows[0]["gross"]) == 40
    assert Decimal(rows[0]["fees"]) == Decimal("2.44")
    assert Decimal(rows[0]["return_pct"]) == Decimal(40) / 1200 * 100


def test_short_and_reversal_are_split_without_duplicate_quantity():
    ops = [
        operation(side="SELL"),
        operation("two", T1, "BUY", "15", "4350", "0"),
        operation("three", T2, "SELL", "5", "1500", "0"),
    ]
    # Explicit timestamps avoid ambiguous same-time order sorting.
    ops[1]["occurred_at"] = "2026-11-02T08:01:00+00:00"
    result, errors = trades(ops)
    assert not errors
    assert [r["direction"] for r in result] == ["SHORT", "LONG"]
    assert [Decimal(r["gross"]) for r in result] == [100, 50]


def test_fifo_multiple_entries_and_month_boundary():
    ops = [
        operation(at="2026-10-31T10:00:00Z"),
        operation("two", "2026-11-01T10:00:00Z", "BUY", "10", "3100", "0"),
        operation("three", T2, "SELL", "15", "4800", "0"),
    ]
    baseline = events([], at="2026-10-31T09:00:00Z")[0]
    result, _ = trades(ops, baseline)
    assert [Decimal(r["gross"]) for r in result] == [200, 50]
    assert sum(Decimal(r["quantity"]) for r in result) == 15


def test_missing_fee_is_not_zero():
    result, _ = trades([operation(fee=None), operation("two", T2, "SELL", "10", "3100", "0")])
    assert result[0]["net"] is None and result[0]["fees"] is None
    assert Decimal(result[0]["gross"]) == 100


def test_initial_inventory_not_assumed_short():
    baseline = events([], positions={"SBER": {"lots": 1}})[0]
    result, warnings = trades([operation("exit", T2, "SELL", "10", "3100")], baseline)
    assert not result and warnings
    assert match_trades([], None)[1]


def test_unknown_instrument_and_incomplete_quantity_not_valued():
    ops = events([operation()], at=T2)[1:]
    ops[0]["asset_class"] = "futures"
    assert match_trades(ops, events([])[0])[1]
    ops[0]["asset_class"] = "share"
    ops[0]["quantity"] = "0"
    assert match_trades(ops, events([])[0])[1]


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", None, "", "broken"])
def test_invalid_decimals_raise(value):
    with pytest.raises(ValueError):
        number(value)


def test_timezones_normalized_naive_rejected():
    assert timestamp("2026-11-02T10:00:00+03:00").isoformat() == T0
    with pytest.raises(ValueError):
        timestamp("2026-11-02T10:00:00")


def test_journal_idempotent_revisions_and_rollback(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    records = events([operation()], at=T2)
    assert journal.ingest(records) == 2 and journal.ingest(records) == 0
    revised = deepcopy(records[1])
    revised["commission"] = "4"
    with pytest.raises(ValueError, match="conflicting"):
        journal.ingest([revised])
    revised["known_at"] = "2026-11-03T09:00:00+00:00"
    journal.ingest([revised])
    assert [r for r in journal.latest() if r["kind"] == "operation"][0]["commission"] == "4"
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 3
    bad = deepcopy(revised)
    bad["kind"] = "unexpected"
    with pytest.raises(ValueError):
        journal.ingest([bad])


def test_schema_version_and_future_receipt_rejected(tmp_path):
    path = tmp_path / "x.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE metadata(version TEXT)")
        db.execute("INSERT INTO metadata VALUES ('future')")
    with pytest.raises(ValueError):
        Journal(path)
    journal = Journal(tmp_path / "valid.db")
    bad = events([])[0]
    bad["known_at"] = "2026-11-01T00:00:00Z"
    with pytest.raises(ValueError):
        journal.ingest([bad])


def test_accounts_separate_and_output_does_not_leak_ids():
    raw = point()
    one = portfolio_events(raw, PROFILE, SPEC, "a")
    raw["account_id"] = "other-account"
    two = portfolio_events(raw, PROFILE, SPEC, "b")
    result = build_report(one + two)
    assert len(result["streams"]) == 2
    assert "private-account" not in json.dumps(result)
    assert "other-account" not in json.dumps(result)
    raw["source"] = "t_invest_prod"
    with pytest.raises(ValueError):
        portfolio_events(raw, PROFILE, SPEC, "c")


def test_snapshot_quantity_reconciliation_blocks_wrong_history():
    records = events([]) + events([operation()], at=T2)  # BUY, but broker says flat
    stream = build_report(records)["streams"][0]
    assert stream["warnings"] and stream["periods"]["2026-11-02"]["gross"] is None


def test_account_cashflows_and_coverage_required():
    a = events([])[0]
    b = events([operation("deposit", T1, None, "0", "0", "0", "1000")], at=T2, equity="11100")
    assert period_result([a, b[0]], b[1:], "2026-11")["net"] is None
    b[0].update(coverage_from=T0, coverage_to=T2)
    p = period_result([a, b[0]], b[1:], "2026-11")
    assert Decimal(p["net"]) == 100
    assert Decimal(p["return_pct"]) == Decimal(100) / 10500 * 100


def test_day_uses_moscow_not_utc():
    records = events([], at="2026-11-02T22:00:00Z")
    assert build_report(records)["streams"][0]["dates"] == ["2026-11-03"]


def test_close_binding_matches_portfolio_without_account_id():
    raw = point()
    baseline = portfolio_events(raw, PROFILE, SPEC, "a")
    raw["account_binding"] = binding(raw.pop("account_id"))
    assert portfolio_events(raw, PROFILE, SPEC, "b")[0]["stream"] == baseline[0]["stream"]


def test_plan_is_signal_never_trade():
    raw = {
        "run_id": "intraday-" + T0,
        "signal_journal": {
            "decisions": [
                {
                    "secid": "SBER",
                    "status": "entry_rejected",
                    "reasons": ["cash"],
                    "candidate": {"side": "buy"},
                }
            ]
        },
    }
    plan = plan_events(raw, events([])[0])
    assert plan[0]["side"] == "buy" and plan[0]["kind"] == "signal"
    result = build_report(events([]) + plan)["streams"][0]
    assert result["operations"] == [] and result["trades"] == []


def test_import_artifacts_transactional_and_repeatable(tmp_path):
    raw = point()
    (tmp_path / "universe.json").write_text(json.dumps([{"secid": "SBER", **SPEC["SBER"]}]))
    (tmp_path / "portfolio.json").write_text(json.dumps(raw))
    cfg = {
        "artifact_root": str(tmp_path),
        "universe": str(tmp_path / "universe.json"),
        "profiles": [{**PROFILE, "portfolio_globs": ["portfolio.json"]}],
    }
    journal = Journal(tmp_path / "j.sqlite")
    assert import_artifacts(cfg, journal)["inserted_revisions"] == 1
    assert import_artifacts(cfg, journal)["inserted_revisions"] == 0
    raw["reported_equity"] = "NaN"
    (tmp_path / "portfolio.json").write_text(json.dumps(raw))
    assert import_artifacts(cfg, journal)["errors"]
    assert len(journal.latest()) == 1


def test_cli_missing_input_safe_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["journal", "--config", str(tmp_path / "none")])
    assert main() == 2
    assert "FileNotFoundError" in capsys.readouterr().out


def test_browser_has_no_remote_dependencies_or_order_actions():
    root = Path(__file__).resolve().parents[1] / "deploy/trader-journal"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "journal.js").read_text(encoding="utf-8")
    assert "https://" not in html and "innerHTML" not in js
    assert "fetch('data.json'" in js and "POST" not in js


@pytest.mark.parametrize("side,value,expected", [("BUY", "3100", "100"), ("SELL", "-2900", "100")])
def test_open_position_pnl_long_and_short(side, value, expected):
    qty = 1 if side == "BUY" else -1
    records = events([]) + events(
        [operation(side=side)], at=T2, positions={"SBER": {"lots": qty, "current_value": value}}
    )
    stream = build_report(records)["streams"][0]
    assert not stream["warnings"]
    assert Decimal(stream["positions"]["SBER"]["unrealized_gross"]) == Decimal(expected)
    assert stream["trades"] == []


def test_open_partial_basis_and_unknown_initial_inventory():
    records = events([]) + events(
        [operation(), operation("two", T2, "SELL", "4", "1240")],
        at=T2,
        positions={"SBER": {"lots": "0.6", "current_value": "1860"}},
    )
    assert (
        Decimal(build_report(records)["streams"][0]["positions"]["SBER"]["unrealized_gross"]) == 60
    )
    initial = events([], positions={"SBER": {"lots": 1, "current_value": "3000"}})
    stream = build_report(initial)["streams"][0]
    assert stream["warnings"] and stream["positions"]["SBER"]["unrealized_gross"] is None


def test_collector_is_sandbox_read_only_and_keeps_coverage(tmp_path, monkeypatch):
    from moex_bot import journal_import
    from moex_bot.integrations import tinvest_sandbox
    from moex_bot.service_config import SANDBOX_REST

    universe = tmp_path / "universe.json"
    universe.write_text(
        json.dumps([{"secid": "SBER", "t_invest_uid": "uid", "board": "TQBR", **SPEC["SBER"]}])
    )
    cfg = {
        "universe": str(universe),
        "collector_dir": str(tmp_path / "receipts"),
        "history_from": T0,
        "profiles": [{**PROFILE, "account_env": "TEST_ACCOUNT"}],
    }
    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", "fake")
    monkeypatch.setenv("TEST_ACCOUNT", "fake-account")
    called = []
    monkeypatch.setattr(
        tinvest_sandbox.UrlLibJsonTransport, "post", lambda *a, **kw: called.append(a[1])
    )

    class Snapshot:
        def as_portfolio_payload(self):
            return point()

    class Service:
        def __init__(self, token, transport):
            for url in [
                SANDBOX_REST + "/SandboxService/PostSandboxOrder",
                "https://invest-public-api.tbank.ru/rest/GetSandboxPortfolio",
            ]:
                with pytest.raises(ValueError, match="forbidden"):
                    transport.post(url)
            transport.post(SANDBOX_REST + "/SandboxService/GetSandboxPortfolio")

        def broker_snapshot(self, account, instruments):
            return Snapshot()

        def operations(self, account, instruments, **kwargs):
            assert kwargs["from_time"] == timestamp(T0)
            return []

    monkeypatch.setattr(tinvest_sandbox, "TInvestSandboxAccountService", Service)
    journal_import.collect(cfg)
    receipt = json.loads(next((tmp_path / "receipts").glob("*.json")).read_bytes())
    assert len(called) == 1 and receipt["journal_coverage_from"] == T0
    assert timestamp(receipt["journal_snapshot_completed_at"]) >= timestamp(receipt["observed_at"])
    cfg["profiles"].append(cfg["profiles"][0])
    with pytest.raises(ValueError, match="distinct"):
        journal_import.collect(cfg)


def test_deployment_auth_and_isolation_contract():
    root = Path(__file__).resolve().parents[1] / "deploy/trader-journal"
    nginx = (root / "nginx.conf").read_text()
    service = (root / "moex-trader-journal.service").read_text()
    installer = (root / "install.sh").read_text()
    assert 'auth_basic "Private trading journal"' in nginx
    assert "limit_except GET" in nginx and "autoindex off" in nginx
    assert "ReadWritePaths=/var/lib/moex-trader-journal" in service
    assert "ProtectSystem=strict" in service
    assert "-m 2750" in installer and "chmod 0600" in installer


def test_analysis_provenance_hashed_once_per_plan(monkeypatch):
    from moex_bot import journal_import

    original = journal_import.digest
    hashed = []

    def counted(value):
        hashed.append(value)
        return original(value)

    monkeypatch.setattr(journal_import, "digest", counted)
    raw = {
        "run_id": "intraday-" + T0,
        "analysis_input": {"quotes": [1, 2, 3]},
        "signal_journal": {
            "decisions": [{"secid": "SBER", "status": "planned", "reasons": []}] * 24
        },
    }
    plan = plan_events(raw, events([])[0])
    assert len(plan) == 24 and len(hashed) == 2
    assert {r["input_sha256"] for r in plan} == {original(raw["analysis_input"])}
