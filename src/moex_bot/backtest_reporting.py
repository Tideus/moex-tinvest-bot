from __future__ import annotations

import html
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from .backtest import BacktestResult


@dataclass(frozen=True, slots=True)
class PromotionGates:
    min_oos_sessions: int
    min_oos_trades: int
    min_oos_sharpe: Decimal
    min_oos_excess_return_pct: Decimal
    max_oos_drawdown_pct: Decimal
    require_positive_stress_return: bool
    require_survivorship_safe: bool
    require_dividends: bool
    min_sandbox_weeks: int
    min_reconciled_orders: int
    max_unresolved_orders: int


@dataclass(frozen=True, slots=True)
class OperationalEvidence:
    sandbox_weeks: int = 0
    reconciled_orders: int = 0
    unresolved_orders: int = 0


@dataclass(frozen=True, slots=True)
class GateCheck:
    name: str
    passed: bool
    observed: str
    required: str


@dataclass(frozen=True, slots=True)
class PromotionAssessment:
    passed: bool
    checks: tuple[GateCheck, ...]


def load_promotion_gates(path: Path) -> PromotionGates:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("promotion gates must be a JSON object")
    return PromotionGates(
        min_oos_sessions=int(raw["min_oos_sessions"]),
        min_oos_trades=int(raw["min_oos_trades"]),
        min_oos_sharpe=Decimal(str(raw["min_oos_sharpe"])),
        min_oos_excess_return_pct=Decimal(str(raw["min_oos_excess_return_pct"])),
        max_oos_drawdown_pct=Decimal(str(raw["max_oos_drawdown_pct"])),
        require_positive_stress_return=_bool(
            raw["require_positive_stress_return"], "require_positive_stress_return"
        ),
        require_survivorship_safe=_bool(
            raw["require_survivorship_safe"], "require_survivorship_safe"
        ),
        require_dividends=_bool(raw["require_dividends"], "require_dividends"),
        min_sandbox_weeks=int(raw["min_sandbox_weeks"]),
        min_reconciled_orders=int(raw["min_reconciled_orders"]),
        max_unresolved_orders=int(raw["max_unresolved_orders"]),
    )


def assess_promotion(
    base: BacktestResult,
    stress: BacktestResult,
    gates: PromotionGates,
    operations: OperationalEvidence | None = None,
) -> PromotionAssessment:
    operations = operations or OperationalEvidence()
    oos = base.oos_metrics
    checks = (
        _check(
            "oos_sessions",
            oos.sessions >= gates.min_oos_sessions,
            oos.sessions,
            gates.min_oos_sessions,
        ),
        _check(
            "oos_trades",
            oos.trades >= gates.min_oos_trades,
            oos.trades,
            gates.min_oos_trades,
        ),
        _check(
            "oos_sharpe",
            oos.annualized_sharpe >= gates.min_oos_sharpe,
            oos.annualized_sharpe,
            gates.min_oos_sharpe,
        ),
        _check(
            "oos_excess_return_pct",
            oos.excess_return_pct is not None
            and oos.excess_return_pct >= gates.min_oos_excess_return_pct,
            oos.excess_return_pct,
            gates.min_oos_excess_return_pct,
        ),
        _check(
            "oos_drawdown_pct",
            oos.max_drawdown_pct <= gates.max_oos_drawdown_pct,
            oos.max_drawdown_pct,
            f"<= {gates.max_oos_drawdown_pct}",
        ),
        _check(
            "stress_return_positive",
            not gates.require_positive_stress_return or stress.oos_metrics.return_pct > 0,
            stress.oos_metrics.return_pct,
            "> 0" if gates.require_positive_stress_return else "not required",
        ),
        _check(
            "survivorship_safe",
            not gates.require_survivorship_safe
            or base.evidence_flags.get("survivorship_safe") is True,
            base.evidence_flags.get("survivorship_safe"),
            gates.require_survivorship_safe,
        ),
        _check(
            "dividends_included",
            not gates.require_dividends or base.evidence_flags.get("dividends_included") is True,
            base.evidence_flags.get("dividends_included"),
            gates.require_dividends,
        ),
        _check(
            "sandbox_weeks",
            operations.sandbox_weeks >= gates.min_sandbox_weeks,
            operations.sandbox_weeks,
            gates.min_sandbox_weeks,
        ),
        _check(
            "reconciled_orders",
            operations.reconciled_orders >= gates.min_reconciled_orders,
            operations.reconciled_orders,
            gates.min_reconciled_orders,
        ),
        _check(
            "unresolved_orders",
            operations.unresolved_orders <= gates.max_unresolved_orders,
            operations.unresolved_orders,
            f"<= {gates.max_unresolved_orders}",
        ),
    )
    return PromotionAssessment(all(item.passed for item in checks), checks)


def render_backtest_report(
    base: BacktestResult,
    stress: BacktestResult,
    assessment: PromotionAssessment,
) -> str:
    metrics = base.metrics
    oos = base.oos_metrics
    lines = [
        "# MOEX Strategy v2 — исторический backtest",
        "",
        "## Полный период",
        "",
        f"- Период: {metrics.start_date} — {metrics.end_date}",
        f"- Результат: {metrics.return_pct:+.2f}%",
        f"- Benchmark: {_optional_pct(metrics.benchmark_return_pct)}",
        f"- Excess: {_optional_pct(metrics.excess_return_pct)}",
        f"- Max drawdown: {metrics.max_drawdown_pct:.2f}%",
        f"- Sharpe (без risk-free): {metrics.annualized_sharpe:.3f}",
        f"- Сделок: {metrics.trades}; неисполненных DAY-заявок: {metrics.unfilled_orders}",
        f"- Оборот: {_money(metrics.turnover)}; комиссии: {_money(metrics.commissions)}",
        f"- Финансирование short: {_money(metrics.short_financing)} "
        f"при годовой ставке {base.settings.short_financing_rate_annual * 100:.2f}%",
        "",
        "## Out-of-sample",
        "",
        f"- Период: {oos.start_date} — {oos.end_date}",
        f"- Результат: {oos.return_pct:+.2f}%",
        f"- Benchmark: {_optional_pct(oos.benchmark_return_pct)}",
        f"- Excess: {_optional_pct(oos.excess_return_pct)}",
        f"- Max drawdown: {oos.max_drawdown_pct:.2f}%",
        f"- Sharpe: {oos.annualized_sharpe:.3f}",
        f"- Cost stress ×{base.settings.cost_stress_multiplier}: "
        f"{stress.oos_metrics.return_pct:+.2f}%",
        f"- OOS финансирование short: {_money(oos.short_financing)}",
        "",
        "## Вклад бумаг",
        "",
    ]
    lines.extend(
        f"- {secid}: {_signed_money(value)}"
        for secid, value in sorted(
            base.security_pnl.items(), key=lambda item: (-item[1], item[0])
        )
    )
    lines.extend(("", "## Gate перехода к production", ""))
    lines.extend(
        f"- {'PASS' if item.passed else 'FAIL'} `{item.name}`: "
        f"observed={item.observed}; required={item.required}"
        for item in assessment.checks
    )
    lines.extend(
        (
            "",
            f"**Итог: {'PASS' if assessment.passed else 'BLOCKED'}**",
            "",
            "Статический текущий universe и price-only данные не доказывают доходность. "
            "Benchmark IMOEX также price-only: дивиденды не включены. Production остаётся "
            "закрыт до прохождения всех gates.",
        )
    )
    return "\n".join(lines) + "\n"


def write_backtest_bundle(
    *,
    output_dir: Path,
    base: BacktestResult,
    stress: BacktestResult,
    assessment: PromotionAssessment,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "backtest.json").write_text(
        json.dumps(asdict(base), ensure_ascii=False, default=str, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "backtest-stress.json").write_text(
        json.dumps(asdict(stress), ensure_ascii=False, default=str, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "promotion-assessment.json").write_text(
        json.dumps(asdict(assessment), ensure_ascii=False, default=str, indent=2) + "\n",
        encoding="utf-8",
    )
    report = render_backtest_report(base, stress, assessment)
    (output_dir / "REPORT.md").write_text(report, encoding="utf-8")
    (output_dir / "equity-curve.csv").write_text(
        "date,strategy_equity,benchmark_equity\n"
        + "".join(
            f"{item.trading_date},{item.strategy_equity},"
            f"{'' if item.benchmark_equity is None else item.benchmark_equity}\n"
            for item in base.equity_curve
        ),
        encoding="utf-8",
    )
    (output_dir / "equity-curve.html").write_text(
        _html_chart(base), encoding="utf-8"
    )


def _html_chart(result: BacktestResult) -> str:
    points = result.equity_curve
    width, height, padding = 1000, 420, 45
    strategy = [float(item.strategy_equity / points[0].strategy_equity * 100) for item in points]
    benchmark = [
        None
        if item.benchmark_equity is None or points[0].benchmark_equity is None
        else float(item.benchmark_equity / points[0].benchmark_equity * 100)
        for item in points
    ]
    values = strategy + [item for item in benchmark if item is not None]
    low, high = min(values), max(values)
    span = high - low or 1

    def polyline(series: Sequence[float | None]) -> str:
        rendered: list[str] = []
        for index, value in enumerate(series):
            if value is None:
                continue
            x = padding + index * (width - 2 * padding) / max(1, len(series) - 1)
            y = height - padding - (value - low) * (height - 2 * padding) / span
            rendered.append(f"{x:.1f},{y:.1f}")
        return " ".join(rendered)

    return f"""<!doctype html>
<html lang="ru"><meta charset="utf-8"><title>MOEX Strategy v2 backtest</title>
<style>body{{font-family:system-ui;margin:24px;background:#111827;color:#e5e7eb}}
.card{{max-width:1050px;background:#1f2937;padding:20px;border-radius:14px}}
svg{{width:100%;height:auto;background:#0f172a;border-radius:10px}}
.legend span{{margin-right:20px}} .strategy{{color:#34d399}} .benchmark{{color:#60a5fa}}</style>
<div class="card"><h1>MOEX Strategy v2</h1>
<p>{html.escape(str(points[0].trading_date))} —
{html.escape(str(points[-1].trading_date))}; индекс=100</p>
<div class="legend"><span class="strategy">● Strategy</span>
<span class="benchmark">● Benchmark</span></div>
<svg viewBox="0 0 {width} {height}" role="img" aria-label="Equity curve">
<polyline fill="none" stroke="#34d399" stroke-width="3" points="{polyline(strategy)}"/>
<polyline fill="none" stroke="#60a5fa" stroke-width="3" points="{polyline(benchmark)}"/>
<text x="10" y="25" fill="#e5e7eb">max {high:.1f}</text>
<text x="10" y="{height - 10}" fill="#e5e7eb">min {low:.1f}</text></svg>
<p>График использует реальные исторические цены, но результат зависит от допущений по universe,
дивидендам, spread, slippage и исполнению.</p></div></html>"""


def _check(name: str, passed: bool, observed: object, required: object) -> GateCheck:
    return GateCheck(name, passed, str(observed), str(required))


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a JSON boolean")
    return value


def _optional_pct(value: Decimal | None) -> str:
    return "н/д" if value is None else f"{value:+.2f}%"


def _money(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ") + " ₽"


def _signed_money(value: Decimal) -> str:
    return ("+" if value > 0 else "") + _money(value)
