import json
from datetime import UTC, date, datetime
from pathlib import Path

from moex_bot.cli import daily_trade_report
from moex_bot.daily_report import render_daily_shadow_report, summarize_shadow_artifacts
from moex_bot.notifications import SQLiteOutbox


def _artifact(path: Path, *, run_at: str, orders: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps(
            {
                "run_id": f"shadow-{run_at}",
                "quality": {"passed": True},
                "orders": orders,
                "rejected": [{"reasons": ["limit"]}],
            }
        ),
        encoding="utf-8",
    )


def _order(side: str, secid: str, lots: int, notional: str) -> dict[str, object]:
    return {
        "status": "validated",
        "intent": {
            "side": side,
            "lots": lots,
            "notional": notional,
            "instrument": {"secid": secid},
        },
    }


def test_daily_report_aggregates_only_requested_moscow_day(tmp_path: Path) -> None:
    _artifact(
        tmp_path / "shadow-a.json",
        run_at="2026-08-18T07:05:00+00:00",
        orders=[_order("buy", "SBER", 2, "600")],
    )
    _artifact(
        tmp_path / "shadow-b.json",
        run_at="2026-08-18T12:05:00+00:00",
        orders=[_order("buy", "SBER", 1, "300"), _order("sell", "GAZP", 3, "500")],
    )
    _artifact(
        tmp_path / "shadow-old.json",
        run_at="2026-08-17T12:05:00+00:00",
        orders=[_order("buy", "LKOH", 1, "7000")],
    )
    summary = summarize_shadow_artifacts(
        tmp_path.glob("shadow-*.json"),
        report_date=date(2026, 8, 18),
        timezone="Europe/Moscow",
    )
    report = render_daily_shadow_report(summary)
    assert summary.cycles == 2
    assert summary.rejected_intents == 2
    assert "BUY SBER: 2 намер.; 3 лот.; 900 RUB" in report
    assert "SELL GAZP: 1 намер.; 3 лот.; 500 RUB" in report
    assert "LKOH" not in report
    assert "не исполненные брокером сделки" in report


def test_daily_report_cli_enqueues_exactly_once(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _artifact(
        artifacts / "shadow-a.json",
        run_at="2026-08-18T12:05:00+00:00",
        orders=[_order("buy", "SBER", 1, "300")],
    )
    outbox_path = tmp_path / "outbox.sqlite3"
    output = artifacts / "daily-2026-08-18.txt"
    arguments = {
        "artifacts_dir": artifacts,
        "report_date": date(2026, 8, 18),
        "timezone": "Europe/Moscow",
        "output_path": output,
        "outbox_path": outbox_path,
        "as_of": datetime(2026, 8, 18, 20, 20, tzinfo=UTC),
    }
    assert daily_trade_report(**arguments) == 0
    assert daily_trade_report(**arguments) == 0
    assert SQLiteOutbox(outbox_path).counts() == {"pending": 1}
    assert output.exists()
