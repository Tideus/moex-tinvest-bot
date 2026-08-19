import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from moex_bot.cli import intraday_trade_notifications
from moex_bot.intraday_performance import (
    render_intraday_performance_report,
    summarize_intraday_performance,
)
from moex_bot.notification_policy import load_notification_policy, should_send_long_morning
from moex_bot.notifications import SQLiteOutbox
from moex_bot.performance import TradeOperation

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_notification_policy_keeps_telegram_compact_and_audit_complete() -> None:
    policy = load_notification_policy(PROJECT_ROOT / "config" / "notifications.json")
    assert policy.long_morning_analysis_hour == 10
    assert policy.long_evening_report_enabled
    assert policy.intraday_notify_filled_operations
    assert policy.intraday_evening_report_enabled
    assert policy.persist_every_cycle
    assert policy.include_market_inputs
    assert should_send_long_morning(policy, datetime(2026, 8, 20, 7, 5, tzinfo=UTC))
    assert not should_send_long_morning(
        policy, datetime(2026, 8, 20, 8, 5, tzinfo=UTC)
    )


def test_intraday_daily_report_uses_snapshots_operations_and_plan_evidence(
    tmp_path: Path,
) -> None:
    first = tmp_path / "intraday-portfolio-a.json"
    last = tmp_path / "intraday-portfolio-b.json"
    first.write_text(
        json.dumps(
            {
                "observed_at": "2026-08-20T07:00:00+00:00",
                "reported_equity": "300000",
                "positions": {},
            }
        ),
        encoding="utf-8",
    )
    last.write_text(
        json.dumps(
            {
                "observed_at": "2026-08-20T15:50:00+00:00",
                "reported_equity": "300500",
                "positions": {},
            }
        ),
        encoding="utf-8",
    )
    plan = tmp_path / "intraday-plan.json"
    plan.write_text(
        json.dumps(
            {
                "run_id": "intraday-2026-08-20T08:00:00+00:00",
                "phase": "entries",
                "quality": {"passed": True},
                "signals": [{"secid": "SBER"}],
                "orders": [{"intent": {"side": "buy"}}],
                "analysis_input": {"supercandles": [{"secid": "SBER"}]},
            }
        ),
        encoding="utf-8",
    )
    operations = (
        TradeOperation(
            "buy-1",
            datetime(2026, 8, 20, 8, tzinfo=UTC),
            "SBER",
            "BUY",
            10,
            Decimal("10000"),
            Decimal("10"),
        ),
        TradeOperation(
            "sell-1",
            datetime(2026, 8, 20, 12, tzinfo=UTC),
            "SBER",
            "SELL",
            10,
            Decimal("10510"),
            Decimal("10"),
        ),
    )
    summary = summarize_intraday_performance(
        (first, last),
        (plan,),
        operations,
        report_date=datetime(2026, 8, 20, tzinfo=UTC).date(),
        timezone="Europe/Moscow",
    )
    assert summary.pnl == Decimal("500")
    assert summary.rows[0].cashflow_pnl == Decimal("490")
    assert summary.plan_cycles == 1
    report = render_intraday_performance_report(summary)
    assert "INTRADAY · ИТОГ ДНЯ" in report
    assert "Баланс вечером: 300 500,00 ₽" in report
    assert "Полные входные данные" in report


def test_notification_policy_rejects_disabled_audit_evidence(tmp_path: Path) -> None:
    raw = json.loads(
        (PROJECT_ROOT / "config" / "notifications.json").read_text(encoding="utf-8")
    )
    raw["audit"]["include_market_inputs"] = False
    path = tmp_path / "notifications.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="audit evidence"):
        load_notification_policy(path)


def test_intraday_fill_notification_is_operation_based_and_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = TradeOperation(
        "broker-operation-1",
        datetime(2026, 8, 20, 8, tzinfo=UTC),
        "SBER",
        "BUY",
        10,
        Decimal("10000"),
        Decimal("10"),
    )

    class FakeService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def operations(self, *_args: object, **_kwargs: object) -> tuple[TradeOperation, ...]:
            return (operation,)

    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", "token")
    monkeypatch.setenv("T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID", "intraday-account")
    monkeypatch.setattr("moex_bot.cli.TInvestSandboxAccountService", FakeService)
    outbox = tmp_path / "notifications.sqlite3"
    arguments = {
        "accounts_path": PROJECT_ROOT / "config" / "accounts.json",
        "universe_path": PROJECT_ROOT / "config" / "universe.json",
        "notifications_path": PROJECT_ROOT / "config" / "notifications.json",
        "outbox_path": outbox,
        "as_of": datetime(2026, 8, 20, 9, tzinfo=UTC),
    }
    assert intraday_trade_notifications(**arguments) == 0
    assert intraday_trade_notifications(**arguments) == 0
    assert SQLiteOutbox(outbox).counts() == {"pending": 1}
