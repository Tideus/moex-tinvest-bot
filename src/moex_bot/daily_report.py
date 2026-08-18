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
class DailyTradeRow:
    side: str
    secid: str
    intents: int
    lots: int
    notional: Decimal


@dataclass(frozen=True, slots=True)
class DailyShadowSummary:
    report_date: date
    timezone: str
    cycles: int
    blocked_cycles: int
    rejected_intents: int
    rows: tuple[DailyTradeRow, ...]


def summarize_shadow_artifacts(
    paths: Iterable[Path], *, report_date: date, timezone: str
) -> DailyShadowSummary:
    zone = ZoneInfo(timezone)
    totals: dict[tuple[str, str], list[Decimal | int]] = defaultdict(
        lambda: [0, 0, Decimal("0")]
    )
    cycles = blocked_cycles = rejected = 0
    for path in sorted(paths):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError(f"shadow artifact must be an object: {path}")
        run_id = str(raw.get("run_id", ""))
        if not run_id.startswith("shadow-"):
            continue
        run_at = datetime.fromisoformat(run_id.removeprefix("shadow-"))
        if run_at.tzinfo is None:
            raise ValueError(f"shadow run timestamp must be timezone-aware: {path}")
        if run_at.astimezone(zone).date() != report_date:
            continue
        cycles += 1
        quality = raw.get("quality", {})
        if not isinstance(quality, Mapping) or quality.get("passed") is not True:
            blocked_cycles += 1
        rejected_raw = raw.get("rejected", [])
        if not isinstance(rejected_raw, list):
            raise ValueError(f"shadow rejected section must be a list: {path}")
        rejected += len(rejected_raw)
        orders = raw.get("orders", [])
        if not isinstance(orders, list):
            raise ValueError(f"shadow orders section must be a list: {path}")
        for record in orders:
            if not isinstance(record, Mapping) or not isinstance(record.get("intent"), Mapping):
                raise ValueError(f"invalid shadow order record: {path}")
            intent = record["intent"]
            instrument = intent.get("instrument")
            if not isinstance(instrument, Mapping):
                raise ValueError(f"shadow order has no instrument: {path}")
            side = str(intent.get("side", "")).upper()
            secid = str(instrument.get("secid", ""))
            if side not in {"BUY", "SELL"} or not secid:
                raise ValueError(f"invalid shadow order identity: {path}")
            bucket = totals[(side, secid)]
            bucket[0] = int(bucket[0]) + 1
            bucket[1] = int(bucket[1]) + int(intent.get("lots", 0))
            bucket[2] = Decimal(str(bucket[2])) + Decimal(str(intent.get("notional", "0")))
    rows = tuple(
        DailyTradeRow(side, secid, int(values[0]), int(values[1]), Decimal(str(values[2])))
        for (side, secid), values in sorted(totals.items())
    )
    return DailyShadowSummary(
        report_date, timezone, cycles, blocked_cycles, rejected, rows
    )


def render_daily_shadow_report(summary: DailyShadowSummary) -> str:
    buy_notional = sum(
        (row.notional for row in summary.rows if row.side == "BUY"), start=Decimal("0")
    )
    sell_notional = sum(
        (row.notional for row in summary.rows if row.side == "SELL"), start=Decimal("0")
    )
    lines = [
        f"MOEX bot — дневной отчёт {summary.report_date.isoformat()}",
        f"Часовой пояс: {summary.timezone}",
        f"Циклов: {summary.cycles}; blocked: {summary.blocked_cycles}; "
        f"отклонено risk-gate: {summary.rejected_intents}",
        f"Виртуальный оборот: BUY {buy_notional} RUB; SELL {sell_notional} RUB",
        "",
        "Виртуальные сделки за день:",
    ]
    if not summary.rows:
        lines.append("- нет прошедших risk-gate BUY/SELL")
    for row in summary.rows:
        lines.append(
            f"- {row.side} {row.secid}: {row.intents} намер.; "
            f"{row.lots} лот.; {row.notional} RUB"
        )
    lines.extend(
        (
            "",
            "SHADOW: это торговые намерения, а не исполненные брокером сделки.",
            "Источником истины остаются shadow JSON и audit JSONL.",
        )
    )
    report = "\n".join(lines)
    if len(report) > 4096:
        return report[:4000] + "\n… отчёт сокращён; полный файл сохранён на сервере."
    return report
