import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from moex_bot.performance import TradeOperation, render_performance_report, summarize_performance


def _artifact(
    path: Path,
    stamp: str,
    equity: str,
    price: str,
    lots: int,
    *,
    price_observed_at: str | None = None,
    current_value: str | None = None,
    account_id: str | None = None,
) -> None:
    position = {"lots": lots}
    if current_value is not None:
        position["current_value"] = current_value
    market = {
        "instrument": {"secid": "SBER", "lot_size": 10},
        "price": price,
    }
    if price_observed_at is not None:
        market["observed_at"] = price_observed_at
    path.write_text(
        json.dumps(
            {
                "run_id": f"shadow-{stamp}",
                "portfolio_input": {
                    "reported_equity": equity,
                    "positions": {"SBER": position},
                    **({"account_id": account_id} if account_id else {}),
                },
                "market": [market],
                "quality": {"passed": True},
                "rejected": [],
            }
        ),
        encoding="utf-8",
    )


def test_performance_reports_broker_equity_and_security_contribution(tmp_path: Path) -> None:
    first = tmp_path / "shadow-a.json"
    last = tmp_path / "shadow-b.json"
    _artifact(first, "2026-08-18T07:00:00+00:00", "300000", "300", 0)
    _artifact(last, "2026-08-18T15:00:00+00:00", "301990", "320", 1)
    operation = TradeOperation(
        "buy-1",
        datetime(2026, 8, 18, 8, tzinfo=UTC),
        "SBER",
        "BUY",
        10,
        Decimal("1200"),
        Decimal("10"),
    )

    summary = summarize_performance(
        [first, last],
        [operation],
        start_date=date(2026, 8, 18),
        end_date=date(2026, 8, 18),
        timezone="Europe/Moscow",
        label="18.08.2026",
    )

    assert summary.pnl == Decimal("1990")
    assert summary.end_equity == Decimal("301990")
    assert summary.rows[0].secid == "SBER"
    assert summary.rows[0].pnl == Decimal("1990")
    report = render_performance_report(summary, weekly=False)
    assert "SBER: +1\u00a0990,00 ₽" in report
    assert "Баланс в конце: 301\u00a0990,00 ₽" in report


def test_weekly_report_refuses_automatic_strategy_change_on_small_sample(
    tmp_path: Path,
) -> None:
    first = tmp_path / "shadow-a.json"
    last = tmp_path / "shadow-b.json"
    _artifact(first, "2026-08-17T07:00:00+00:00", "300000", "300", 0)
    _artifact(last, "2026-08-21T15:00:00+00:00", "300000", "300", 0)
    summary = summarize_performance(
        [first, last],
        [],
        start_date=date(2026, 8, 17),
        end_date=date(2026, 8, 21),
        timezone="Europe/Moscow",
        label="week",
    )
    assert summary.verdict.startswith("COLLECT_MORE")
    assert "🧠 ВЫВОД" in render_performance_report(summary, weekly=True)


def test_legacy_artifact_outside_requested_period_does_not_break_report(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "shadow-legacy.json"
    legacy.write_text(
        json.dumps({"run_id": "shadow-2026-08-18T07:00:00+00:00"}),
        encoding="utf-8",
    )
    first = tmp_path / "shadow-current-a.json"
    last = tmp_path / "shadow-current-b.json"
    _artifact(first, "2026-08-25T07:00:00+00:00", "300000", "300", 0)
    _artifact(last, "2026-08-25T15:00:00+00:00", "300100", "301", 0)

    summary = summarize_performance(
        [legacy, first, last],
        [],
        start_date=date(2026, 8, 25),
        end_date=date(2026, 8, 25),
        timezone="Europe/Moscow",
        label="25.08.2026",
    )

    assert summary.pnl == Decimal("100")


def test_performance_uses_broker_position_value_and_flags_shifted_benchmark(
    tmp_path: Path,
) -> None:
    first = tmp_path / "shadow-a.json"
    last = tmp_path / "shadow-b.json"
    _artifact(
        first,
        "2026-08-31T07:00:00+00:00",
        "300000",
        "4638",
        0,
        price_observed_at="2026-08-30T23:59:59+03:00",
    )
    _artifact(
        last,
        "2026-08-31T20:00:00+00:00",
        "300144.3785",
        "4638",
        2,
        price_observed_at="2026-08-30T23:59:59+03:00",
        current_value="9392",
    )
    operation = TradeOperation(
        "buy-1",
        datetime(2026, 8, 31, 8, tzinfo=UTC),
        "SBER",
        "BUY",
        20,
        Decimal("9243"),
        Decimal("4.6215"),
    )
    summary = summarize_performance(
        [first, last],
        [operation],
        start_date=date(2026, 8, 31),
        end_date=date(2026, 8, 31),
        timezone="Europe/Moscow",
        label="31.08.2026",
    )
    assert summary.rows[0].pnl == Decimal("144.3785")
    assert summary.unattributed_pnl == 0
    assert not summary.benchmark_aligned
    assert "2026-08-30" in summary.benchmark_period
    assert "Benchmark не совпадает" in render_performance_report(summary, weekly=False)


def test_performance_refuses_to_merge_different_broker_accounts(tmp_path: Path) -> None:
    first = tmp_path / "shadow-a.json"
    last = tmp_path / "shadow-b.json"
    _artifact(
        first,
        "2026-08-31T07:00:00+00:00",
        "300000",
        "300",
        0,
        account_id="old-account",
    )
    _artifact(
        last,
        "2026-08-31T20:00:00+00:00",
        "300000",
        "300",
        0,
        account_id="new-account",
    )
    with pytest.raises(ValueError, match="different broker account ids"):
        summarize_performance(
            [first, last],
            [],
            start_date=date(2026, 8, 31),
            end_date=date(2026, 8, 31),
            timezone="Europe/Moscow",
            label="31.08.2026",
        )
