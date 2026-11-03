"""TTL is an expected unfilled outcome, not permission to hide uncertain execution."""

import io
import json
import runpy
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from moex_bot import cli
from moex_bot.domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from moex_bot.monitoring_execution import TTL_NOTE, intraday_execution_note
from moex_bot.sandbox_execution import (
    SandboxExecutionResult,
    expected_intraday_ttl_stop,
    render_sandbox_execution_report,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 3, 11, 23, tzinfo=UTC)


def outcome():
    intent = OrderIntent(
        "request",
        Instrument("X5", "uid", "TQBR", 1, Decimal("0.01")),
        Side.BUY,
        4,
        Decimal("2310.50"),
        Decimal("9242"),
        "signal",
    )
    order = OrderRecord(intent, OrderStatus.CANCELLED, 0, "broker-order", "ttl_expired")
    return SandboxExecutionResult(
        "intraday-2026-11-03T11:20:08+00:00", (order,), "submission stopped after cancelled order"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "partial",
        "unknown",
        "rejected",
        "no_broker_id",
        "no_detail",
        "market",
        "long",
        "empty",
        "reason",
        "previous_unfilled",
        "previous_partial",
        "bool_fill",
    ],
)
def test_only_confirmed_unfilled_intraday_ttl_is_expected(fault):
    result = outcome()
    order = result.submitted[0]
    if fault == "partial":
        order = replace(order, filled_lots=1)
    if fault == "unknown":
        order = replace(order, status=OrderStatus.ACCEPTED)
    if fault == "rejected":
        order = replace(order, status=OrderStatus.REJECTED)
    if fault == "no_broker_id":
        order = replace(order, broker_order_id=None)
    if fault == "no_detail":
        order = replace(order, status_detail=None)
    if fault == "market":
        order = replace(order, intent=replace(order.intent, order_type="market"))
    if fault == "bool_fill":
        order = replace(order, filled_lots=False)
    result = replace(result, submitted=(order,))
    if fault == "long":
        result = replace(result, run_id=result.run_id.replace("intraday", "shadow"))
    if fault == "empty":
        result = replace(result, submitted=())
    if fault == "reason":
        result = replace(result, stopped_reason="network timeout")
    if fault.startswith("previous_"):
        previous = replace(order, status=OrderStatus.FILLED, filled_lots=1)
        if fault == "previous_unfilled":
            previous = replace(previous, status=OrderStatus.ACCEPTED)
        result = replace(result, submitted=(previous, order))
    assert not expected_intraday_ttl_stop(result)


def test_ttl_report_distinguishes_cancellation_from_execution_failure():
    result = outcome()
    assert expected_intraday_ttl_stop(result)
    assert "Штатная отмена по TTL" in render_sandbox_execution_report(result)
    assert "⛔" not in render_sandbox_execution_report(result)
    preceding = replace(result.submitted[0], status=OrderStatus.FILLED, filled_lots=4)
    assert expected_intraday_ttl_stop(replace(result, submitted=(preceding, *result.submitted)))


@pytest.mark.parametrize("partial,code", [(False, 0), (True, 2)])
def test_cli_ttl_exit_code_keeps_partial_execution_fail_closed(
    tmp_path, monkeypatch, capsys, partial, code
):
    result = outcome()
    if partial:
        result = replace(result, submitted=(replace(result.submitted[0], filled_lots=1),))
    registry = Mock()
    profile = cli.load_account_registry(ROOT / "config/accounts.json").by_id("intraday")
    registry.by_id.return_value = replace(profile, order_execution_enabled=True)
    monkeypatch.setattr(cli, "load_account_registry", lambda _: registry)
    config = cli.load_intraday_config(ROOT / "config/intraday.json")
    monkeypatch.setattr(
        cli, "load_intraday_config", lambda _: replace(config, execution_stage="sandbox")
    )
    monkeypatch.setattr(cli.TInvestSandboxExecutionAdapter, "from_environment", lambda **kw: Mock())
    execute = Mock(return_value=result)
    monkeypatch.setattr(cli, "execute_shadow_plan", execute)
    assert (
        cli.intraday_sandbox_execute(
            accounts_path=tmp_path,
            config_path=tmp_path,
            plan_path=tmp_path,
            portfolio_path=tmp_path,
            output_path=tmp_path,
            journal_path=tmp_path / "journal",
            outbox_path=None,
            as_of=NOW,
        )
        == code
    )
    assert execute.call_count == 1
    assert ("expected_ttl_cancelled" in capsys.readouterr().out) is (not partial)


@pytest.mark.parametrize(
    "fault",
    ["none", "missing", "malformed", "stale", "future", "naive", "partial", "wrong_contour",
     "bad_decimal", "no_timezone"],
)
def test_monitor_observation_is_fresh_safe_and_informational(tmp_path, fault):
    root = tmp_path / "artifacts"
    root.mkdir()
    record = asdict(outcome())
    if fault == "partial":
        record["submitted"][0]["filled_lots"] = 1
    if fault == "wrong_contour":
        record["run_id"] = "shadow-2026-11-03T11:20:08+00:00"
    if fault == "bad_decimal":
        record["submitted"][0]["intent"]["limit_price"] = "not-a-price"
    if fault == "no_timezone":
        record["run_id"] = "intraday-2026-11-03T11:20:08"
    if fault != "missing":
        (root / "intraday-execution-test.json").write_text(
            "{bad" if fault == "malformed" else json.dumps(record, default=str), encoding="utf-8"
        )
    now = NOW
    if fault == "stale":
        now += timedelta(hours=1)
    if fault == "future":
        now -= timedelta(hours=1)
    if fault == "naive":
        now = now.replace(tzinfo=None)
    assert intraday_execution_note(tmp_path, now) == (TTL_NOTE if fault == "none" else "")


@pytest.mark.parametrize(
    "failed,observation", [(False, TTL_NOTE), (True, TTL_NOTE), (False, "token=secret")]
)
def test_kuma_ttl_note_never_masks_down_or_forwards_arbitrary_text(
    monkeypatch, failed, observation
):
    monkeypatch.setitem(sys.modules, "grp", SimpleNamespace())
    module = runpy.run_path(str(ROOT / "scripts/ubuntu/kuma-exporter.py"))
    sent = []

    def send(url, **kwargs):
        sent.append(parse_qs(urlsplit(url).query))
        return io.BytesIO(b'{"ok":true}')

    module["publish_groups"].__globals__["urlopen"] = send
    code = module["publish_groups"](
        {"Timers and services": "test"},
        {"service:intraday": "FAIL: result=exit-code" if failed else "OK"},
        {"Timers and services": ("service:",)},
        {"intraday_execution": observation},
    )
    assert code == 0
    assert sent[0]["status"] == (["down"] if failed else ["up"])
    assert (TTL_NOTE in sent[0]["msg"][0]) is (not failed and observation == TTL_NOTE)
    assert "secret" not in sent[0]["msg"][0]
