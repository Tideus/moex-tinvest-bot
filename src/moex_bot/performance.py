from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class TradeOperation:
    operation_id: str
    occurred_at: datetime
    secid: str | None
    side: str | None
    quantity: int
    gross: Decimal
    commission: Decimal
    external_cashflow: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class PerformanceRow:
    secid: str
    start_units: int
    end_units: int
    buys: Decimal
    sells: Decimal
    commission: Decimal
    pnl: Decimal


@dataclass(frozen=True, slots=True)
class PerformanceSummary:
    label: str
    started_at: datetime
    ended_at: datetime
    start_equity: Decimal
    end_equity: Decimal
    pnl: Decimal
    return_pct: Decimal
    equal_weight_return_pct: Decimal
    excess_return_pct: Decimal
    max_drawdown_pct: Decimal
    operations: int
    cycles: int
    blocked_cycles: int
    rejected_intents: int
    rows: tuple[PerformanceRow, ...]
    unattributed_pnl: Decimal
    profitable_securities: int
    verdict: str


@dataclass(frozen=True, slots=True)
class _Point:
    observed_at: datetime
    equity: Decimal
    prices: Mapping[str, Decimal]
    units: Mapping[str, int]
    blocked: bool
    rejected: int


def summarize_performance(
    paths: Iterable[Path],
    operations: Iterable[TradeOperation],
    *,
    start_date: date,
    end_date: date,
    timezone: str,
    label: str,
) -> PerformanceSummary:
    zone = ZoneInfo(timezone)
    points = tuple(
        point
        for point in (_point(path) for path in sorted(paths))
        if start_date <= point.observed_at.astimezone(zone).date() <= end_date
    )
    if len(points) < 2:
        raise ValueError("performance report requires at least two portfolio snapshots")
    first, last = points[0], points[-1]
    period_operations = tuple(
        item for item in operations if first.observed_at <= item.occurred_at <= last.observed_at
    )
    external = sum((item.external_cashflow for item in period_operations), Decimal("0"))
    pnl = last.equity - first.equity - external
    return_pct = Decimal("0") if first.equity == 0 else pnl / first.equity * 100
    comparable = tuple(
        (last.prices[secid] / first_price - 1) * 100
        for secid, first_price in first.prices.items()
        if first_price > 0 and secid in last.prices
    )
    benchmark = (
        Decimal("0")
        if not comparable
        else sum(comparable, Decimal("0")) / Decimal(len(comparable))
    )
    aggregates: dict[str, list[Decimal | int]] = defaultdict(
        lambda: [Decimal("0"), Decimal("0"), Decimal("0")]
    )
    for item in period_operations:
        if item.secid is None or item.side not in {"BUY", "SELL"}:
            continue
        bucket = aggregates[item.secid]
        if item.side == "BUY":
            bucket[0] = Decimal(str(bucket[0])) + item.gross
        else:
            bucket[1] = Decimal(str(bucket[1])) + item.gross
        bucket[2] = Decimal(str(bucket[2])) + item.commission
    secids = sorted(set(first.units) | set(last.units) | set(aggregates))
    rows: list[PerformanceRow] = []
    for secid in secids:
        start_units = first.units.get(secid, 0)
        end_units = last.units.get(secid, 0)
        start_value = Decimal(start_units) * first.prices.get(secid, Decimal("0"))
        end_value = Decimal(end_units) * last.prices.get(secid, Decimal("0"))
        buy, sell, commission = aggregates[secid]
        contribution = end_value - start_value + Decimal(str(sell)) - Decimal(str(buy))
        contribution -= Decimal(str(commission))
        rows.append(
            PerformanceRow(
                secid,
                start_units,
                end_units,
                Decimal(str(buy)),
                Decimal(str(sell)),
                Decimal(str(commission)),
                contribution,
            )
        )
    drawdown = _max_drawdown(tuple(item.equity for item in points))
    blocked = sum(1 for item in points if item.blocked)
    rejected = sum(item.rejected for item in points)
    verdict = _verdict(len(points), len(period_operations), blocked, drawdown, return_pct)
    unattributed = pnl - sum((row.pnl for row in rows), Decimal("0"))
    return PerformanceSummary(
        label,
        first.observed_at,
        last.observed_at,
        first.equity,
        last.equity,
        pnl,
        return_pct,
        benchmark,
        return_pct - benchmark,
        drawdown,
        len(period_operations),
        len(points),
        blocked,
        rejected,
        tuple(rows),
        unattributed,
        sum(1 for row in rows if row.pnl > 0),
        verdict,
    )


def render_performance_report(summary: PerformanceSummary, *, weekly: bool) -> str:
    title = "НЕДЕЛЬНЫЙ" if weekly else "ДНЕВНОЙ"
    lines = [
        f"📈 MOEX BOT · {title} ОТЧЁТ",
        summary.label,
        "",
        "💼 ОБЩИЙ РЕЗУЛЬТАТ",
        f"Баланс в начале: {_money(summary.start_equity)}",
        f"Баланс в конце: {_money(summary.end_equity)}",
        f"Результат: {_signed_money(summary.pnl)} ({summary.return_pct:+.2f}%)",
        f"Равновзвешенный universe: {summary.equal_weight_return_pct:+.2f}%",
        f"Разница к нему: {summary.excess_return_pct:+.2f} п.п.",
        f"Максимальная просадка: {summary.max_drawdown_pct:.2f}%",
        "",
        "📊 ПО БУМАГАМ",
    ]
    if not summary.rows:
        lines.append("• позиции и исполненные сделки отсутствуют")
    for row in sorted(summary.rows, key=lambda item: (-item.pnl, item.secid)):
        lines.append(
            f"• {row.secid}: {_signed_money(row.pnl)} · "
            f"позиция {row.start_units}→{row.end_units} шт."
        )
    if summary.unattributed_pnl != 0:
        lines.append(
            f"• Прочее/сверка счёта: {_signed_money(summary.unattributed_pnl)} "
            "(комиссии, денежные операции или разница снимков)"
        )
    lines.extend(
        (
            "",
            "🧭 КАЧЕСТВО ПРОЦЕССА",
            f"Циклов: {summary.cycles} · blocked: {summary.blocked_cycles}",
            f"Операций: {summary.operations} · risk-отклонений: {summary.rejected_intents}",
            f"Бумаг с положительным вкладом: {summary.profitable_securities}/{len(summary.rows)}",
        )
    )
    if weekly:
        lines.extend(
            (
                "",
                "🧠 ВЫВОД",
                summary.verdict,
                "",
                "🔎 ДЛЯ НЕДЕЛЬНОГО РАЗБОРА",
                "• отделить ошибки данных/исполнения от ошибки сигнала",
                "• проверить новости и геориски, возникшие после решения",
                "• сравнить результат, просадку и оборот с равновзвешенным universe",
                "• оценить, помогли бы меньший размер, cash-reserve или хедж",
                "Параметры автоматически не изменяются.",
            )
        )
    lines.append("Прошлый P&L проверяет процесс, но не доказывает будущую доходность.")
    report = "\n".join(lines)
    return report if len(report) <= 4096 else report[:4000] + "\n… полный отчёт на сервере."


def _point(path: Path) -> _Point:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"invalid shadow artifact: {path}")
    run_id = str(raw.get("run_id", ""))
    if not run_id.startswith("shadow-"):
        raise ValueError(f"invalid shadow run id: {path}")
    observed_at = datetime.fromisoformat(run_id.removeprefix("shadow-"))
    portfolio = raw.get("portfolio_input")
    market = raw.get("market")
    if not isinstance(portfolio, Mapping) or not isinstance(market, list):
        raise ValueError(f"artifact has no portfolio/market: {path}")
    equity = Decimal(str(portfolio.get("reported_equity") or "0"))
    prices: dict[str, Decimal] = {}
    lotsizes: dict[str, int] = {}
    for item in market:
        if not isinstance(item, Mapping) or not isinstance(item.get("instrument"), Mapping):
            raise ValueError(f"invalid market item: {path}")
        instrument = item["instrument"]
        secid = str(instrument["secid"])
        prices[secid] = Decimal(str(item["price"]))
        lotsizes[secid] = int(instrument["lot_size"])
    units: dict[str, int] = {}
    positions = portfolio.get("positions", {})
    if not isinstance(positions, Mapping):
        raise ValueError(f"invalid portfolio positions: {path}")
    for secid, value in positions.items():
        if not isinstance(value, Mapping):
            raise ValueError(f"invalid position: {path}")
        units[str(secid)] = int(value["lots"]) * lotsizes[str(secid)]
    quality = raw.get("quality", {})
    rejected = raw.get("rejected", [])
    return _Point(
        observed_at,
        equity,
        prices,
        units,
        not isinstance(quality, Mapping) or quality.get("passed") is not True,
        len(rejected) if isinstance(rejected, list) else 0,
    )


def _max_drawdown(values: tuple[Decimal, ...]) -> Decimal:
    peak = Decimal("0")
    worst = Decimal("0")
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value - peak) / peak * 100)
    return abs(worst)


def _verdict(
    cycles: int, operations: int, blocked: int, drawdown: Decimal, return_pct: Decimal
) -> str:
    if blocked:
        return "REVIEW_DATA: сначала устранить заблокированные циклы; стратегию не менять."
    if cycles < 20 or operations < 5:
        return "COLLECT_MORE: данных за одну неделю недостаточно для изменения стратегии."
    if drawdown > Decimal("2"):
        return (
            "REVIEW_RISK: проверить размеры позиций и защитные лимиты; "
            "сигнал не менять автоматически."
        )
    if return_pct < 0:
        return (
            "OBSERVE: неделя отрицательная; менять стратегию только после "
            "устойчивого OOS-ухудшения."
        )
    return "CONTINUE_OOS: процесс стабилен; продолжить без изменения параметров по одной неделе."


def _money(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _signed_money(value: Decimal) -> str:
    sign = "+" if value > 0 else ""
    return sign + _money(value)
