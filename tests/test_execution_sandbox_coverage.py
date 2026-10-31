"""Reject malformed/stale plans before any sandbox network submission."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_sandbox_execution import FakeAdapter, _write_inputs

from moex_bot.domain import OrderRecord, OrderStatus
from moex_bot.runtime_config import RuntimeConfig, RuntimeSchedule
from moex_bot.sandbox_execution import (
    SandboxExecutionResult,
    decode_validated_intent,
    execute_shadow_plan,
    reconcile_execution_journal,
    render_sandbox_execution_report,
)
from moex_bot.service_config import TInvestEnvironment


@pytest.mark.parametrize(
    "fault",
    [
        "naive",
        "prod",
        "disabled",
        "source",
        "account",
        "orders_bool",
        "quality",
        "run",
        "stale",
        "future",
        "orders",
        "positions",
        "margin",
        "short",
        "flat",
        "object",
        "settle",
    ],
)
def test_malformed_plan_is_not_executed(tmp_path: Path, fault: str) -> None:
    shadow, portfolio = _write_inputs(tmp_path)
    plan, account = json.loads(shadow.read_text()), json.loads(portfolio.read_text())
    runtime = RuntimeConfig(TInvestEnvironment.SANDBOX, RuntimeSchedule(), True, 3)
    now = datetime(2026, 8, 18, 10, 1, tzinfo=UTC)
    if fault == "naive":
        now = now.replace(tzinfo=None)
    elif fault == "prod":
        runtime = replace(runtime, environment=TInvestEnvironment.PROD)
    elif fault == "disabled":
        runtime = replace(runtime, sandbox_orders_enabled=False)
    elif fault == "source":
        account["source"] = "prod"
    elif fault == "account":
        account["account_id"] = "other"
    elif fault == "orders_bool":
        account["open_orders"] = True
    elif fault == "quality":
        plan["quality"] = {}
    elif fault == "run":
        plan["run_id"] = "invalid"
    elif fault == "stale":
        plan["run_id"] = "shadow-2026-08-18T09:00:00+00:00"
    elif fault == "future":
        plan["run_id"] = "shadow-2026-08-18T11:00:00+00:00"
    elif fault == "orders":
        plan["orders"] = {}
    elif fault == "positions":
        account["positions"] = []
    elif fault in {"margin", "short"}:
        plan["orders"][0]["intent"]["side"] = "sell"
        plan["orders"][0]["intent"]["confirm_margin_trade"] = fault == "short"
    elif fault == "flat":
        plan["orders"][0]["intent"]["rationale"] = "intraday mandatory flat"
    shadow.write_text(json.dumps([] if fault == "object" else plan))
    portfolio.write_text(json.dumps(account))

    class EmptySettlement(FakeAdapter):
        def settle(self, records: tuple, *, ttl_seconds: int) -> tuple:
            return ()

    with pytest.raises(ValueError):
        execute_shadow_plan(
            shadow_path=shadow,
            portfolio_path=portfolio,
            output_path=tmp_path / "out",
            journal_path=tmp_path / "journal",
            runtime=runtime,
            adapter=EmptySettlement() if fault == "settle" else FakeAdapter(),
            as_of=now,
        )


@pytest.mark.parametrize(
    "record",
    [None, {}, {"intent": {}, "status": "rejected"}, {"intent": {}, "status": "validated"}],
)
def test_decode_rejects_nonvalidated_or_missing_instrument(record: object) -> None:
    with pytest.raises(ValueError):
        decode_validated_intent(record)


def test_decode_rejects_derivative_and_reconciliation_needs_lookup(tmp_path: Path) -> None:
    shadow, _ = _write_inputs(tmp_path)
    record = json.loads(shadow.read_text())["orders"][0]
    record["intent"]["instrument"]["asset_class"] = "future"
    with pytest.raises(ValueError, match="shares only"):
        decode_validated_intent(record)
    with pytest.raises(ValueError, match="read-only"):
        reconcile_execution_journal(tmp_path / "journal", FakeAdapter())
    text = render_sandbox_execution_report(SandboxExecutionResult("empty", (), "stopped"))
    assert "stopped" in text and "0" in text


@pytest.mark.parametrize("detail", ["", "broker detail"])
def test_report_includes_real_status_and_optional_detail(tmp_path: Path, detail: str) -> None:
    shadow, _ = _write_inputs(tmp_path)
    intent = decode_validated_intent(json.loads(shadow.read_text())["orders"][0])
    record = OrderRecord(intent, OrderStatus.ACCEPTED, status_detail=detail)
    text = render_sandbox_execution_report(SandboxExecutionResult("run", (record,), None))
    assert "BUY SBER" in text and "accepted" in text
    if detail:
        assert detail in text
