import json
import shutil
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from moex_bot.daily_report import (
    DailyShadowSummary,
    DailyTradeRow,
    render_daily_shadow_report,
    summarize_shadow_artifacts,
)
from moex_bot.event_calendar import EventStore, load_calendar_config, stamp
from moex_bot.ownership import load_ownership_disclosures, render_ownership_report
from moex_bot.service_config import TInvestEnvironment, load_service_config, resolve_tinvest_runtime
from moex_bot.validation import _array, _object, validate_project_configs

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "file,location,value",
    [
        ("config/replay.json", ("mode",), "shadow"),
        ("config/prod_shadow.json", ("mode",), "replay"),
        (
            "config/accounts.json",
            ("profiles", 0, "strategies", 0, "config_path"),
            "config/missing.json",
        ),
        ("config/accounts.json", ("profiles", 0, "order_execution_enabled"), False),
        ("config/accounts.json", ("profiles", 1, "order_execution_enabled"), False),
        ("config/prod_runtime.json", ("t_invest_environment",), "sandbox"),
        ("config/prod_shadow.json", ("max_order_notional",), "9999"),
        ("config/prod_shadow.json", ("max_daily_turnover",), "19999"),
        ("config/prod_shadow.json", ("max_open_orders",), 1),
        ("config/ownership_disclosures.json", (), [None]),
        ("config/ownership_disclosures.json", (), [{}]),
        ("examples/portfolio_empty.json", (), {}),
        ("examples/replay_snapshot.json", ("market",), []),
    ],
)
def test_cross_file_validation_rejects_mismatch(tmp_path, file, location, value):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    path = tmp_path / file
    raw = json.loads(path.read_text())
    if not location:
        raw = value
    else:
        current = raw
        for key in location[:-1]:
            current = current[key]
        current[location[-1]] = value
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        validate_project_configs(tmp_path)


@pytest.mark.parametrize("function,payload", [(_array, "{}"), (_object, "[]")])
def test_config_shapes(tmp_path, function, payload):
    path = tmp_path / "wrong.json"
    path.write_text(payload)
    with pytest.raises(ValueError):
        function(path)


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "other"},
        {"run_id": "shadow-2026-10-29T12:00:00+00:00"},
        {"run_id": "shadow-2026-10-30T12:00:00"},
        {"rejected": {}},
        {"orders": {}},
        {"orders": [None]},
        {"orders": [{"intent": {}}]},
        {"orders": [{"intent": {"instrument": {"secid": ""}, "side": "bad"}}]},
    ],
)
def test_daily_artifact_boundaries(tmp_path, change):
    path = tmp_path / "report.json"
    raw = dict(run_id="shadow-2026-10-30T12:00:00+00:00", quality={"passed": True}, orders=[])
    path.write_text(json.dumps(raw | change))
    if "run_id" in change and (change["run_id"] == "other" or "29T" in change["run_id"]):
        assert (
            summarize_shadow_artifacts(
                [path], report_date=date(2026, 10, 30), timezone="Europe/Moscow"
            ).cycles
            == 0
        )
    else:
        with pytest.raises(ValueError):
            summarize_shadow_artifacts(
                [path], report_date=date(2026, 10, 30), timezone="Europe/Moscow"
            )


def test_daily_report_invalid_root_and_size_limit(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("[]")
    with pytest.raises(ValueError):
        summarize_shadow_artifacts([path], report_date=NOW.date(), timezone="Europe/Moscow")
    summary = DailyShadowSummary(
        NOW.date(),
        "Europe/Moscow",
        1,
        0,
        0,
        tuple(DailyTradeRow("BUY", f"LONGNAME{i}", 1, 1, Decimal(1)) for i in range(200)),
    )
    report = render_daily_shadow_report(summary)
    assert "сокращён" in report
    assert len(report) <= 4096
    path.write_text(json.dumps({"run_id": "shadow-2026-10-30T12:00:00+00:00",
                                "quality": {"passed": False}}))
    blocked = summarize_shadow_artifacts([path], report_date=NOW.date(), timezone="UTC")
    assert blocked.cycles == blocked.blocked_cycles == 1


@pytest.mark.parametrize(
    "change",
    [
        {"verified": "true"},
        {"published_at": "2026-10-30T10:00:00"},
        {"published_at": "2027-01-01T00:00:00+00:00"},
        {"source_url": "http://example.org"},
        {"holder_name": ""},
        {"stake_percent": "101"},
    ],
)
def test_ownership_rejects_invalid_disclosure(tmp_path, change):
    item = dict(
        secid="SBER",
        holder_name="Fund",
        holder_type="fund",
        report_date="2026-01-01",
        published_at="2026-01-02T00:00:00+00:00",
        source_url="https://example.org/filing",
        source_kind="issuer",
        verified=True,
    )
    path = tmp_path / "ownership.json"
    path.write_text(json.dumps([item | change]))
    with pytest.raises(ValueError):
        load_ownership_disclosures(path, as_of=NOW)


def test_ownership_shapes_filter_and_unknown_stake(tmp_path):
    assert "Нет актуального" in render_ownership_report((), secid="SBER")
    path = tmp_path / "ownership.json"
    path.write_text("{}")
    with pytest.raises(ValueError):
        load_ownership_disclosures(path, as_of=NOW)
    with pytest.raises(ValueError):
        load_ownership_disclosures(path, as_of=datetime(2026, 1, 1))
    item = dict(
        secid="SBER",
        holder_name="Fund",
        holder_type="fund",
        report_date="2026-01-01",
        published_at="2026-01-02T00:00:00+00:00",
        source_url="https://example.org/filing",
        source_kind="issuer",
        verified=True,
    )
    path.write_text(json.dumps([item]))
    assert not load_ownership_disclosures(path, as_of=NOW, secid="OTHER")
    report = render_ownership_report(load_ownership_disclosures(path, as_of=NOW), secid="SBER")
    assert "доля не раскрыта" in report


@pytest.mark.parametrize(
    "key,value",
    [
        ("horizon_days", 0),
        ("mode", "trade"),
        ("token_env", ""),
        ("exchange", ""),
    ],
)
def test_calendar_config_invalid(tmp_path, key, value):
    raw = json.loads((ROOT / "config/event_calendar.json").read_text())
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(raw | {key: value}))
    with pytest.raises(ValueError):
        load_calendar_config(path)


def test_calendar_rejects_unexpected_rows_and_missing_dates(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    with pytest.raises(ValueError):
        stamp(datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="unexpected"):
        store.record("report", "uid", "SBER", {"other": []}, NOW)
    with pytest.raises(ValueError, match="no date"):
        store.record("report", "uid", "SBER", {"events": [{}]}, NOW)


def test_prod_service_runtime_and_endpoint_validation(tmp_path, monkeypatch):
    config = load_service_config(ROOT / "config/services.json")
    monkeypatch.setenv("T_INVEST_PROD_TOKEN", "token")
    monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "id")
    assert resolve_tinvest_runtime(config, environment=TInvestEnvironment.PROD).token == "token"
    with pytest.raises(ValueError, match="sandbox-only"):
        resolve_tinvest_runtime(config, environment=TInvestEnvironment.PROD, account_id_env="OTHER")
    monkeypatch.delenv("T_INVEST_PROD_ACCOUNT_ID")
    with pytest.raises(ValueError, match="together"):
        resolve_tinvest_runtime(config, environment=TInvestEnvironment.PROD)
    raw = json.loads((ROOT / "config/services.json").read_text())
    path = tmp_path / "services.json"
    raw["t_invest"]["prod_rest"] += "/wrong"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="path"):
        load_service_config(path)
