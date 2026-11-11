"""Independent, rebuildable read model. Never imports an execution or broker client."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict, deque
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
ZERO = Decimal(0)
SCHEMA = "trader-journal-v1"


def number(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid journal decimal") from None
    if not result.is_finite():
        raise ValueError("non-finite journal decimal")
    return result


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("journal requires aware timestamps")
    return result.astimezone(UTC)


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class Journal:
    """Append revisions, never UPDATE/DELETE source history or execution journals."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata (version TEXT NOT NULL)")
            version = db.execute("SELECT version FROM metadata").fetchall()
            if version and version != [(SCHEMA,)]:
                raise ValueError("unsupported journal schema")
            if not version:
                db.execute("INSERT INTO metadata VALUES (?)", (SCHEMA,))
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                stream TEXT NOT NULL, kind TEXT NOT NULL, identity TEXT NOT NULL,
                known_at TEXT NOT NULL, checksum TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(stream,kind,identity,known_at))""")

    def ingest(self, events: list[dict[str, Any]]) -> int:
        count = 0
        with closing(sqlite3.connect(self.path)) as db, db:
            for event in events:
                if event["kind"] not in {"operation", "snapshot", "signal", "order"}:
                    raise ValueError("unsupported journal event")
                timestamp(event["at"])
                known = timestamp(event["known_at"]).isoformat()
                if timestamp(event["at"]) > timestamp(known):
                    raise ValueError("event after receipt")
                payload = encoded(event)
                checksum = digest(event)
                key = (event["stream"], event["kind"], event["identity"], known)
                previous = db.execute(
                    "SELECT checksum FROM events WHERE stream=? AND kind=? AND identity=? "
                    "AND known_at=?",
                    key,
                ).fetchone()
                if previous and previous[0] != checksum:
                    raise ValueError("conflicting source revision")
                if not previous:
                    db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (*key, checksum, payload))
                    count += 1
        return count

    def latest(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute("""SELECT payload FROM (
                SELECT payload, ROW_NUMBER() OVER (
                    PARTITION BY stream,kind,identity ORDER BY known_at DESC
                ) AS rank FROM events) WHERE rank=1""")
            return [json.loads(row[0]) for row in rows]


def match_trades(
    operations: list[dict[str, Any]],
    baseline: dict[str, Any] | None,
    open_basis: dict[str, Decimal] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """FIFO analytical lots, not broker tax lots. Unknown initial inventory stays unknown."""
    queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    results: list[dict[str, Any]] = []
    warnings: list[str] = []
    if baseline is None:
        return [], ["Нет начального снимка: доходность сделок не восстановлена"]
    start = timestamp(baseline["at"])
    unknown = {s for s, p in baseline["positions"].items() if number(p["units"]) != 0}
    if unknown:
        warnings.append("Начальный снимок содержит позиции без подтверждённой цены входа")
    if any(o["side"] in {"BUY", "SELL"} and timestamp(o["at"]) <= start for o in operations):
        warnings.append("Часть исполнений предшествует начальной оценке; результат неполный")
    directions: dict[tuple[str, str], set[str]] = defaultdict(set)
    for op in operations:
        if op["side"] in {"BUY", "SELL"}:
            directions[(op["symbol"], op["at"])].add(op["side"])
    ambiguous = {s for (s, _), sides in directions.items() if len(sides) > 1}
    unknown.update(ambiguous)
    for op in sorted(operations, key=lambda x: (timestamp(x["at"]), x["identity"])):
        if op["side"] not in {"BUY", "SELL"} or timestamp(op["at"]) <= start:
            continue
        symbol = op["symbol"]
        if symbol in unknown or op.get("asset_class") not in {"share", "equity"}:
            warnings.append(f"{symbol}: нет подтверждённой базы или модели инструмента")
            continue
        qty, gross = number(op["quantity"]), number(op["gross"])
        if qty <= 0 or gross <= 0:
            warnings.append(f"{symbol}: неполные количество/стоимость исполнения")
            continue
        price = gross / qty
        fee = None if op["commission"] is None else number(op["commission"]) / qty
        sign = 1 if op["side"] == "BUY" else -1
        remaining = qty
        queue = queues[symbol]
        while remaining > 0 and queue and queue[0]["sign"] != sign:
            entry = queue[0]
            take = min(remaining, entry["quantity"])
            pnl = (price - entry["price"]) * take * entry["sign"]
            fees = None if fee is None or entry["fee"] is None else (fee + entry["fee"]) * take
            results.append(
                {
                    "symbol": symbol,
                    "direction": "LONG" if entry["sign"] == 1 else "SHORT",
                    "opened_at": entry["at"],
                    "closed_at": op["at"],
                    "quantity": str(take),
                    "entry": str(entry["price"]),
                    "exit": str(price),
                    "gross": str(pnl),
                    "fees": None if fees is None else str(fees),
                    "net": None if fees is None else str(pnl - fees),
                    "return_pct": str(pnl / (entry["price"] * take) * 100),
                    "net_return_pct": None
                    if fees is None
                    else str((pnl - fees) / (entry["price"] * take) * 100),
                }
            )
            remaining -= take
            entry["quantity"] -= take
            if entry["quantity"] == 0:
                queue.popleft()
        if remaining:
            queue.append(
                {"quantity": remaining, "price": price, "fee": fee, "sign": sign, "at": op["at"]}
            )
    if open_basis is not None:
        open_basis.update(
            {
                s: sum((p["quantity"] * p["price"] * p["sign"] for p in lots), ZERO)
                for s, lots in queues.items()
                if lots
            }
        )
    return results, sorted(set(warnings))


def period_result(
    snapshots: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    period: str,
) -> dict[str, Any]:
    """Account performance is separate from closed-trade P&L. Windows are explicit."""
    points = [
        p for p in snapshots if timestamp(p["at"]).astimezone(MSK).isoformat().startswith(period)
    ]
    if len(points) < 2:
        return {"net": None, "gross": None, "return_pct": None, "window": None}
    first, last = points[0], points[-1]
    a, b = timestamp(first["at"]), timestamp(last["at"])
    ops = [o for o in operations if a < timestamp(o["at"]) <= b]
    flows = sum((number(o["external"]) for o in ops), ZERO)
    net = number(last["equity"]) - number(first["equity"]) - flows
    fees_complete = all(o["commission"] is not None for o in ops)
    fees = sum((number(o["commission"]) for o in ops if o["commission"] is not None), ZERO)
    duration = Decimal(str((b - a).total_seconds()))
    if duration <= 0:
        raise ValueError("non-positive valuation window")
    denominator = number(first["equity"]) + sum(
        (
            number(o["external"])
            * Decimal(str((b - timestamp(o["at"])).total_seconds()))
            / duration
            for o in ops
        ),
        ZERO,
    )
    # Explicit coverage must come from a completed broker operation query, not file presence.
    covered = any(
        p.get("coverage_from")
        and timestamp(p["coverage_from"]) <= a
        and timestamp(p.get("coverage_to", p["at"])) >= b
        for p in snapshots
    )
    return {
        "net": str(net) if covered else None,
        "gross": str(net + fees) if covered and fees_complete else None,
        "return_pct": str(net / denominator * 100) if covered and denominator > 0 else None,
        "window": [first["at"], last["at"]],
        "coverage": covered,
        "return_method": "Modified Dietz (net), observed window",
    }


def closed_summary(trades: list[dict[str, Any]], *, complete: bool) -> dict[str, Any]:
    """Describe FIFO closures, never sum trade percentages or infer missing costs."""
    result: dict[str, Any] = {"closures": len(trades)}
    for mode in ("gross", "net"):
        known = complete and all(t[mode] is not None for t in trades)
        values = [number(t[mode]) for t in trades] if known else []
        profits = sum((v for v in values if v > 0), ZERO)
        losses = -sum((v for v in values if v < 0), ZERO)
        wins = sum(v > 0 for v in values)
        result[mode] = {
            "pnl": str(sum(values, ZERO)) if known else None,
            "wins": wins if known else None,
            "losses": sum(v < 0 for v in values) if known else None,
            "flat": sum(v == 0 for v in values) if known else None,
            "win_rate_pct": str(Decimal(wins) / len(values) * 100) if values else None,
            "profit_factor": str(profits / losses) if known and losses else None,
            "profit_factor_state": "unknown"
            if not known
            else "no_closures"
            if not values
            else "no_losses"
            if not losses
            else "calculated",
            "fees": str(sum((number(t["fees"]) for t in trades), ZERO))
            if complete and all(t["fees"] is not None for t in trades)
            else None,
        }
    return result


def build_report(events: list[dict[str, Any]]) -> dict[str, Any]:
    streams: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        streams[event["stream"]].append(event)
    output: list[dict[str, Any]] = []
    for stream, rows in sorted(streams.items()):
        snapshots = sorted(
            (r for r in rows if r["kind"] == "snapshot"), key=lambda p: timestamp(p["at"])
        )
        operations = sorted(
            (r for r in rows if r["kind"] == "operation"), key=lambda p: timestamp(p["at"])
        )
        basis: dict[str, Decimal] = {}
        trades, warnings = match_trades(operations, snapshots[0] if snapshots else None, basis)
        # Quantity reconciliation detects missing history. No guessed cost basis or zero P&L.
        if len(snapshots) >= 2:
            first, last = snapshots[0], snapshots[-1]
            units = {s: number(p["units"]) for s, p in first["positions"].items()}
            for op in operations:
                if timestamp(first["at"]) < timestamp(op["at"]) <= timestamp(last["at"]) and op[
                    "side"
                ] in {"BUY", "SELL"}:
                    units[op["symbol"]] = units.get(op["symbol"], ZERO) + number(op["quantity"]) * (
                        1 if op["side"] == "BUY" else -1
                    )
            symbols = set(units) | set(last["positions"])
            if any(
                units.get(s, ZERO) != number(last["positions"].get(s, {}).get("units", "0"))
                for s in symbols
            ):
                warnings.append("Остатки не сошлись: доходность сделок скрыта до сверки")
                trades = []
        dates = sorted({timestamp(r["at"]).astimezone(MSK).date().isoformat() for r in rows})
        periods = sorted(set(dates + [d[:7] for d in dates]))
        stats: dict[str, Any] = {}
        for period in periods:
            selected = [
                t
                for t in trades
                if timestamp(t["closed_at"]).astimezone(MSK).date().isoformat().startswith(period)
            ]
            net_known = all(t["net"] is not None for t in selected)
            stats[period] = {
                "gross": str(sum((number(t["gross"]) for t in selected), ZERO)),
                "net": str(sum((number(t["net"]) for t in selected), ZERO)) if net_known else None,
                "closures": len(selected),
                "wins": sum(number(t["gross"]) > 0 for t in selected),
                "account": period_result(snapshots, operations, period),
                "summary": closed_summary(selected, complete=not warnings),
                "by_symbol": [
                    {
                        "symbol": symbol,
                        **closed_summary(
                            [t for t in selected if t["symbol"] == symbol], complete=not warnings
                        ),
                    }
                    for symbol in sorted({t["symbol"] for t in selected})
                ],
            }
            if warnings:
                stats[period]["gross"] = stats[period]["net"] = None
        current = snapshots[-1] if snapshots else None
        positions = {}
        for symbol, position in current["positions"].items() if current else []:
            value = position.get("value")
            cost = basis.get(symbol)
            pnl = number(value) - cost if value is not None and cost is not None else None
            positions[symbol] = {
                **position,
                "unrealized_gross": str(pnl) if pnl is not None and not warnings else None,
                "return_pct": str(pnl / abs(cost) * 100)
                if pnl is not None and cost and not warnings
                else None,
            }
        output.append(
            {
                "id": stream,
                "strategy": rows[0]["strategy"],
                "environment": rows[0]["environment"],
                "dates": dates,
                "periods": stats,
                "trades": trades,
                "warnings": warnings,
                "operations": [
                    {
                        k: o[k]
                        for k in (
                            "at",
                            "symbol",
                            "side",
                            "quantity",
                            "gross",
                            "commission",
                            "granularity",
                        )
                    }
                    for o in operations
                    if o["side"] in {"BUY", "SELL"}
                ],
                "signals": [
                    {k: r.get(k) for k in ("at", "symbol", "status", "reasons", "side")}
                    for r in rows
                    if r["kind"] == "signal"
                ],
                "orders": [
                    {k: r.get(k) for k in ("at", "symbol", "status", "filled_lots")}
                    for r in rows
                    if r["kind"] == "order"
                ],
                "equity": [{"at": p["at"], "equity": p["equity"]} for p in snapshots],
                "positions": positions,
                "observed_at": current["at"] if current else None,
            }
        )
    return {"schema": SCHEMA, "generated_at": datetime.now(UTC).isoformat(), "streams": output}
