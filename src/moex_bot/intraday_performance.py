from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .performance import TradeOperation


@dataclass(frozen=True, slots=True)
class IntradayPerformanceRow:
    secid: str
    buys: Decimal
    sells: Decimal
    commission: Decimal
    cashflow_pnl: Decimal
    end_lots: int


@dataclass(frozen=True, slots=True)
class IntradayPerformanceSummary:
    report_date: date
    start_equity: Decimal
    end_equity: Decimal
    pnl: Decimal
    return_pct: Decimal
    operations: int
    plan_cycles: int
    signals: int
    planned_orders: int
    forced_flat_cycles: int
    quality_failures: int
    rows: tuple[IntradayPerformanceRow, ...]
    unattributed_pnl: Decimal


def summarize_intraday_performance(
    portfolio_paths: Iterable[Path],
    plan_paths: Iterable[Path],
    operations: Iterable[TradeOperation],
    *,
    report_date: date,
    timezone: str,
) -> IntradayPerformanceSummary:
    zone = ZoneInfo(timezone)
    snapshots = tuple(
        sorted(
            (
                snapshot
                for snapshot in (_portfolio_point(path) for path in portfolio_paths)
                if snapshot[0].astimezone(zone).date() == report_date
            ),
            key=lambda item: item[0],
        )
    )
    if len(snapshots) < 2:
        raise ValueError("intraday report requires at least two portfolio snapshots")
    first, last = snapshots[0], snapshots[-1]
    period_operations = tuple(
        item
        for item in operations
        if item.occurred_at.astimezone(zone).date() == report_date
    )
    external = sum((item.external_cashflow for item in period_operations), Decimal("0"))
    pnl = last[1] - first[1] - external
    return_pct = Decimal("0") if first[1] == 0 else pnl / first[1] * 100
    totals: dict[str, list[Decimal]] = defaultdict(
        lambda: [Decimal("0"), Decimal("0"), Decimal("0")]
    )
    for operation in period_operations:
        if operation.secid is None or operation.side not in {"BUY", "SELL"}:
            continue
        bucket = totals[operation.secid]
        bucket[0 if operation.side == "BUY" else 1] += operation.gross
        bucket[2] += operation.commission
    end_positions = last[2]
    rows = tuple(
        IntradayPerformanceRow(
            secid=secid,
            buys=values[0],
            sells=values[1],
            commission=values[2],
            cashflow_pnl=values[1] - values[0] - values[2],
            end_lots=end_positions.get(secid, 0),
        )
        for secid, values in sorted(totals.items())
    )
    plan_cycles = signals = planned_orders = forced = quality_failures = 0
    for path in sorted(plan_paths):
        raw = _object(path)
        run_id = str(raw.get("run_id", ""))
        if not run_id.startswith("intraday-"):
            continue
        observed_at = datetime.fromisoformat(run_id.removeprefix("intraday-"))
        if observed_at.astimezone(zone).date() != report_date:
            continue
        plan_cycles += 1
        signals_raw = raw.get("signals", [])
        orders_raw = raw.get("orders", [])
        signals += len(signals_raw) if isinstance(signals_raw, list) else 0
        planned_orders += len(orders_raw) if isinstance(orders_raw, list) else 0
        forced += int(raw.get("phase") in {"force_flat", "loss_limit_flat"})
        quality = raw.get("quality")
        quality_failures += int(
            not isinstance(quality, Mapping) or quality.get("passed") is not True
        )
    attributed = sum((row.cashflow_pnl for row in rows), Decimal("0"))
    return IntradayPerformanceSummary(
        report_date,
        first[1],
        last[1],
        pnl,
        return_pct,
        len(period_operations),
        plan_cycles,
        signals,
        planned_orders,
        forced,
        quality_failures,
        rows,
        pnl - attributed,
    )


def render_intraday_performance_report(summary: IntradayPerformanceSummary) -> str:
    lines = [
        "⚡ MOEX BOT · INTRADAY · ИТОГ ДНЯ",
        summary.report_date.strftime("%d.%m.%Y"),
        "",
        "💼 СЧЁТ",
        f"Баланс утром: {_money(summary.start_equity)}",
        f"Баланс вечером: {_money(summary.end_equity)}",
        f"Результат: {_signed_money(summary.pnl)} ({summary.return_pct:+.2f}%)",
        "",
        "🧾 ИСПОЛНЕННЫЕ СДЕЛКИ",
    ]
    if not summary.rows:
        lines.append("• исполненных BUY/SELL операций не было")
    for row in sorted(summary.rows, key=lambda item: (-item.cashflow_pnl, item.secid)):
        residual = "" if row.end_lots == 0 else f" · остаток {row.end_lots} лот."
        lines.append(
            f"• {row.secid}: {_signed_money(row.cashflow_pnl)} · "
            f"BUY {_money(row.buys)} · SELL {_money(row.sells)}{residual}"
        )
    if summary.unattributed_pnl:
        lines.append(f"• Сверка счёта: {_signed_money(summary.unattributed_pnl)}")
    lines.extend(
        (
            "",
            "🧠 РАБОТА МОДЕЛИ",
            f"Циклов: {summary.plan_cycles} · сигналов: {summary.signals}",
            f"Планов заявок: {summary.planned_orders} · операций: {summary.operations}",
            f"Forced-flat циклов: {summary.forced_flat_cycles}",
            f"Ошибок качества данных: {summary.quality_failures}",
            "Полные входные данные, признаки и решения сохранены в JSON/SQLite для weekly review.",
            "Sandbox P&L не доказывает исполнимость и доходность production.",
        )
    )
    report = "\n".join(lines)
    return report if len(report) <= 4096 else report[:4000] + "\n… полный отчёт на сервере."


def _portfolio_point(path: Path) -> tuple[datetime, Decimal, dict[str, int]]:
    raw = _object(path)
    observed_at = datetime.fromisoformat(str(raw.get("observed_at", "")))
    if observed_at.tzinfo is None:
        raise ValueError(f"intraday portfolio timestamp must be timezone-aware: {path}")
    positions_raw = raw.get("positions", {})
    if not isinstance(positions_raw, Mapping):
        raise ValueError(f"intraday portfolio positions are invalid: {path}")
    positions: dict[str, int] = {}
    for secid, value in positions_raw.items():
        if not isinstance(value, Mapping):
            raise ValueError(f"intraday portfolio position is invalid: {path}")
        positions[str(secid)] = int(value.get("lots", 0))
    return observed_at, Decimal(str(raw.get("reported_equity", "0"))), positions


def _object(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected JSON object: {path}")
    return raw


def _money(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _signed_money(value: Decimal) -> str:
    return ("+" if value > 0 else "") + _money(value)
