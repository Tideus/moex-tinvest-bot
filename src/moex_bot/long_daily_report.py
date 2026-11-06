"""Read-only reporting for the enrolled LONG sandbox; never invoke the executor."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .account_profiles import load_account_registry
from .cli import _verified_instruments
from .integrations.tinvest_sandbox import TInvestSandboxAccountService
from .long_auto_operator import ANCHOR
from .long_auto_sandbox import SandboxOnlyTransport
from .long_sandbox_retry import resolve
from .monitoring_long_once import ReadOnlyLedger
from .notification_policy import load_notification_policy
from .notifications import SQLiteOutbox
from .performance import TradeOperation
from .scheduler import MOSCOW
from .shadow import load_universe

# Fixed close boundary for the current equity session. The report waits five more minutes.
CLOSE = time(23, 50)
REPORT = time(23, 55)


def report_day(now: datetime, requested: date | None = None) -> date:
    if now.tzinfo is None:
        raise ValueError("aware report time required")
    local = now.astimezone(MOSCOW)
    day = requested or (
        local.date() if local.time() >= REPORT else local.date() - timedelta(days=1)
    )
    if local < datetime.combine(day, CLOSE, MOSCOW):
        raise ValueError("evening session not closed")
    return day


def closing_window(day: date, now: datetime) -> bool:
    local = now.astimezone(MOSCOW)
    return (
        datetime.combine(day, CLOSE, MOSCOW)
        <= local
        < datetime.combine(day + timedelta(days=1), time(6, 50), MOSCOW)
    )


class ReportTransport(SandboxOnlyTransport):
    """Deny write methods even if a reporting caller accidentally selects one."""

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        allowed = {
            "GetSandboxPortfolio",
            "GetSandboxPositions",
            "GetSandboxOrders",
            "GetSandboxOperationsByCursor",
            "GetSandboxStopOrders",
        }
        if url.rsplit("/", 1)[-1] not in allowed:
            raise ValueError("report transport only permits sandbox reads")
        return super().post(url, headers=headers, payload=payload, timeout_seconds=timeout_seconds)


def decision_summary(base: Path, day: date, account: str) -> dict[str, Any]:
    plans = []
    for path in sorted(base.glob("run-*/plan.json")):
        raw = json.loads(path.read_bytes())
        at = datetime.fromisoformat(raw["run_id"].removeprefix("shadow-"))
        if at.tzinfo is None:
            raise ValueError("naive plan time")
        if at.astimezone(MOSCOW).date() != day:
            continue
        portfolio = json.loads(path.with_name("portfolio.json").read_bytes())
        if portfolio.get("account_id") != account:
            raise ValueError("foreign LONG portfolio")
        plans.append(raw)
    return {
        "cycles": len(plans),
        "quality_failures": sum(p["quality"]["passed"] is not True for p in plans),
        "planned_orders": sum(len(p["orders"]) for p in plans),
        "targets": sum(len(p["targets"]) for p in plans),
        "flow_rejected": dict(
            Counter(
                f["secid"] for p in plans for f in p.get("flow_diagnostics", []) if not f["passed"]
            )
        ),
        "risk_rejected": sum(len(p["rejected"]) for p in plans),
    }


def opening_point(base: Path, output: Path, day: date, account: str) -> dict[str, Any] | None:
    previous = output.parent / f"long-close-{day - timedelta(days=1)}.json"
    binding = hashlib.sha256(account.encode()).hexdigest()
    if previous.exists():
        raw = json.loads(previous.read_bytes())
        at = datetime.fromisoformat(raw["observed_at"])
        if (
            raw.get("account_binding") == binding
            and raw.get("report_date") == str(day - timedelta(days=1))
            and closing_window(day - timedelta(days=1), at)
        ):
            return dict(raw)
    points = []
    for path in base.glob("run-*/portfolio.json"):
        raw = json.loads(path.read_bytes())
        at = datetime.fromisoformat(raw["observed_at"])
        if at.tzinfo is None:
            raise ValueError("naive snapshot")
        if at.astimezone(MOSCOW).date() == day and raw.get("account_id") == account:
            points.append((at, raw))
    return min(points, key=lambda item: item[0])[1] if points else None


def performance(
    opening: dict[str, Any] | None,
    ending: dict[str, Any] | None,
    operations: tuple[TradeOperation, ...],
    day: date,
) -> dict[str, Any]:
    day_ops = tuple(o for o in operations if o.occurred_at.astimezone(MOSCOW).date() == day)
    rows: dict[str, dict[str, Decimal]] = {}
    for op in day_ops:
        if op.secid and op.side in {"BUY", "SELL"}:
            r = rows.setdefault(
                op.secid, {"buy": Decimal(0), "sell": Decimal(0), "commission": Decimal(0)}
            )
            r["buy" if op.side == "BUY" else "sell"] += op.gross
            r["commission"] += op.commission
    result: dict[str, Any] = {
        "trades": sum(o.side in {"BUY", "SELL"} for o in day_ops),
        "commission": sum((o.commission for o in day_ops), Decimal(0)),
        "rows": rows,
        "pnl": None,
        "period_started_at": None,
        "period_ended_at": None,
    }
    if opening is None or ending is None:
        return result
    a, b = (datetime.fromisoformat(p["observed_at"]) for p in (opening, ending))
    if a.tzinfo is None or b.tzinfo is None or b <= a:
        raise ValueError("invalid valuation window")
    external = sum((o.external_cashflow for o in operations if a < o.occurred_at <= b), Decimal(0))
    result.update(
        pnl=Decimal(ending["reported_equity"]) - Decimal(opening["reported_equity"]) - external,
        period_started_at=a.isoformat(),
        period_ended_at=b.isoformat(),
    )
    # Per-security P&L uses position value changes, not BUY/SELL cash flow alone.
    for s in set(rows) | set(opening["positions"]) | set(ending["positions"]):
        r = rows.setdefault(s, {"buy": Decimal(0), "sell": Decimal(0), "commission": Decimal(0)})
        first = opening["positions"].get(s, {"current_value": "0"})
        last = ending["positions"].get(s, {"current_value": "0"})
        flows = [o for o in operations if o.secid == s and a < o.occurred_at <= b]
        if "current_value" not in last or "current_value" not in first:
            continue
        r["pnl"] = (
            Decimal(last["current_value"])
            - Decimal(first["current_value"])
            + sum(
                (
                    o.gross * (1 if o.side == "SELL" else -1) - o.commission
                    for o in flows
                    if o.side in {"BUY", "SELL"}
                ),
                Decimal(0),
            )
        )
    return result


def render(data: dict[str, Any]) -> str:
    perf, decision = data["performance"], data["decisions"]
    current = data["current"]
    lines = [
        "🧪 MOEX BOT · LONG SANDBOX · ИТОГ ДНЯ",
        data["report_date"],
        "",
        "💼 СЧЁТ",
        f"Капитал на момент проверки: {Decimal(current['reported_equity']):,.2f} ₽",
        f"Свободно: {Decimal(current['cash']):,.2f} ₽ · заявок: {current['open_orders']}",
        f"Время проверки: {data['observed_at']} (не исторический снимок)",
        "",
        "📊 РЕЗУЛЬТАТ",
    ]
    if perf["pnl"] is None:
        lines.append("P&L: недостаточно начальной/заключительной оценки; это НЕ нулевой результат.")
    else:
        lines += [
            f"P&L окна наблюдения: {Decimal(perf['pnl']):+,.2f} ₽",
            f"От {perf['period_started_at']} до {perf['period_ended_at']}",
        ]
    lines += [f"Исполнений за день: {perf['trades']} · комиссии: {perf['commission']} ₽"]
    for s, r in perf["rows"].items():
        value = f"{Decimal(r['pnl']):+,.2f} ₽" if "pnl" in r else "P&L н/д"
        lines.append(f"• {s}: {value}; покупки {r['buy']} ₽; продажи {r['sell']} ₽")
    lines += [
        "",
        "🧠 РЕШЕНИЯ",
        f"Планов: {decision['cycles']} · намерений: {decision['planned_orders']}",
        f"Отказов качества: {decision['quality_failures']} · "
        f"риск-отказов: {decision['risk_rejected']}",
        "Отклонены потоком: " + (", ".join(decision["flow_rejected"]) or "нет"),
        f"Журнал одноразового входа сейчас: {data['ledger_state']}",
        f"Активных стоп-заявок у брокера сейчас: {data['active_stop_count']}",
        "Число стопов не доказывает покрытие позиции; состояние журнала не равно живому стопу.",
    ]
    if not decision["cycles"]:
        lines.append(
            "Плана за дату нет: завершённый одноразовый вход не требует новых планов; "
            "WAITING требует проверки расписания/сессии."
        )
    lines += ["", "Планы не равны сделкам. Отчёт не отправляет заявки и не изменяет защиту."]
    return "\n".join(lines)


def generate(args: argparse.Namespace, now: datetime) -> int:
    day = report_day(now, args.report_date)
    registry = load_account_registry(args.accounts)
    profile = registry.by_id("long")
    account = os.getenv(profile.account_id_env, "").strip()
    if (
        profile.environment.value != "sandbox"
        or profile.account_id_env != "T_INVEST_SANDBOX_LONG_ACCOUNT_ID"
        or not account
        or account == os.getenv("T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID", "").strip()
        or account == os.getenv("T_INVEST_PROD_ACCOUNT_ID", "").strip()
    ):
        raise ValueError("dedicated LONG sandbox account required")
    binding = resolve(args.base / "sandbox.sqlite3", args.anchor, account)
    ledger = ReadOnlyLedger(binding.state, account, anchor=binding.anchor)
    status = ledger.status()
    instruments = _verified_instruments(load_universe(args.universe))
    service = TInvestSandboxAccountService(os.environ["T_INVEST_SANDBOX_TOKEN"], ReportTransport())
    # No write path in this transport, including when execution is disabled.
    current = service.broker_snapshot(account, instruments).as_portfolio_payload()
    current.pop("account_id")
    observed = datetime.now(UTC)
    start = datetime.combine(day, time.min, MOSCOW)
    opening = opening_point(args.base, args.output, day, account)
    from_time = min(start, datetime.fromisoformat(opening["observed_at"])) if opening else start
    ops = service.operations(
        account,
        instruments,
        from_time=from_time,
        to_time=observed
        if closing_window(day, observed)
        else min(start + timedelta(days=1), observed),
    )
    stops = service.account_state(account)["stop_orders"]
    if not isinstance(stops, dict) or not isinstance(stops.get("stopOrders", []), list):
        raise ValueError("invalid stop response")
    ending = (
        {**current, "observed_at": observed.isoformat()} if closing_window(day, observed) else None
    )
    data = {
        "report_date": str(day),
        "observed_at": observed.isoformat(),
        "ledger_state": status,
        "current": current,
        "active_stop_count": len(stops.get("stopOrders", [])),
        "decisions": decision_summary(args.base, day, account),
        "performance": performance(opening, ending, ops, day),
    }
    body = render(data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(body + "\n", encoding="utf-8")
    args.output.with_suffix(".json").write_text(
        json.dumps(data, default=str, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if ending:
        close = {
            **ending,
            "account_binding": hashlib.sha256(account.encode()).hexdigest(),
            "report_date": str(day),
        }
        target = args.output.parent / f"long-close-{day}.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(close, default=str), encoding="utf-8")
        temp.replace(target)
    policy = load_notification_policy(args.notifications)
    if args.outbox and policy.long_evening_report_enabled:
        SQLiteOutbox(args.outbox).enqueue(
            kind="long_once_daily",
            dedupe_key=f"long-once-daily:{day}",
            body=body,
            now=observed,
        )
    print(f"PASS: LONG sandbox report written; date={day}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-only", action="store_true")
    parser.add_argument("--report-date", type=date.fromisoformat)
    parser.add_argument("--base", type=Path, default=Path("/var/lib/moex-tinvest-bot/long-once"))
    parser.add_argument("--anchor", type=Path, default=ANCHOR)
    parser.add_argument("--accounts", type=Path, default=Path("config/accounts.json"))
    parser.add_argument("--universe", type=Path, default=Path("config/universe.json"))
    parser.add_argument("--notifications", type=Path, default=Path("config/notifications.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--outbox", type=Path)
    args = parser.parse_args()
    try:
        if args.date_only:
            print(report_day(datetime.now(UTC), args.report_date))
            return 0
        if args.output is None:
            raise ValueError("output required")
        return generate(args, datetime.now(UTC))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error):
        # Broker errors may contain identifiers. Never print the raw exception.
        print("FAIL: LONG report incomplete; check account binding, journal and read-only API")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
