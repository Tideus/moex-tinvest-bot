"""Independent, read-only LONG forward experiment. Never a broker execution plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from moex_bot.long_auto_operator import ANCHOR, STATE, _working_account
from moex_bot.long_auto_sandbox import SandboxLongBroker, _NoRedirect, _rows
from moex_bot.long_sandbox_verifier import ReadOnlySandboxTransport
from moex_bot.monitoring_long_once import ReadOnlyLedger
from moex_bot.notifications import SQLiteOutbox
from moex_bot.scheduler import MOSCOW

BASE = Path("/var/lib/moex-long-flow-shadow")
APP = Path("/opt/moex-tinvest-bot")
DATASETS = ("tradestats", "orderstats", "obstats")
VARIANTS = ("current", "book_mean_3", "book_mean_6")
PROTOCOL = {
    "id": "long-flow-forward-v1",
    "mode": "shadow_only",
    "max_sessions": 40,
    "spread_max_bps": "3",
    "trade_min": "-0.25",
    "order_min": "-0.10",
    "book_min": "-0.20",
    "fee_each_bps": "5",
    "slippage_each_bps": "2.5",
    "publication_cushion_seconds": 15,
    "minimum_execution_delay_seconds": 60,
}


def stamp(value: str) -> datetime:
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        at = at.replace(tzinfo=MOSCOW)
    return at.astimezone(MOSCOW)


def number(value: Any) -> Decimal:
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError("nonfinite metric")
    return d


def immutable(path: Path, value: Any) -> None:
    """No replacement, including symlinks. Interrupted receipts are errors, not success."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())


def read_json(path: Path) -> Any:
    return json.loads(path.read_bytes())


def get_rows(url: str, params: dict[str, Any], block: str, *, auth: bool) -> list[dict[str, Any]]:
    expected = (
        "https://apim.moex.com/iss/datashop/algopack/eq/"
        if auth
        else "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/"
    )
    if not url.startswith(expected):
        raise ValueError("fixed read-only market host required")
    rows: list[dict[str, Any]] = []
    for _ in range(100):
        headers = {"Authorization": "Bearer " + os.environ["MOEX_APIKEY"]} if auth else {}
        request = Request(url + "?" + urlencode(params | {"start": len(rows)}), headers=headers)
        with build_opener(_NoRedirect()).open(request, timeout=20) as response:
            raw = json.loads(response.read())[block]
        batch = [
            dict(zip([str(c).lower() for c in raw["columns"]], r, strict=True)) for r in raw["data"]
        ]
        if not batch:
            return rows
        rows.extend(batch)
    raise ValueError("pagination cap")


def flow_rows(ticker: str, day: date) -> dict[str, list[dict[str, Any]]]:
    return {
        dataset: get_rows(
            f"https://apim.moex.com/iss/datashop/algopack/eq/{dataset}/{ticker}.json",
            {"from": day.isoformat(), "till": day.isoformat()},
            "data",
            auth=True,
        )
        for dataset in DATASETS
    }


def available(row: Mapping[str, Any]) -> tuple[datetime, datetime]:
    at = stamp(str(row["tradedate"]) + " " + str(row["tradetime"]))
    ready = at + timedelta(minutes=5, seconds=15)
    if row.get("systime"):
        published = stamp(str(row["systime"]))
        # A later-day correction cannot be assumed known on the original day.
        ready = max(ready, published)
    return at, ready


def evaluate(blocks: dict[str, list[dict[str, Any]]], cutoff: datetime) -> dict[str, Any]:
    if cutoff.tzinfo is None:
        raise ValueError("aware cutoff required")
    indexed: dict[str, dict[datetime, dict[str, Any]]] = {}
    for dataset in DATASETS:
        indexed[dataset] = {}
        for row in blocks[dataset]:
            at, ready = available(row)
            if ready > cutoff or at.date() != cutoff.astimezone(MOSCOW).date():
                continue
            if at in indexed[dataset]:
                raise ValueError("duplicate flow interval")
            indexed[dataset][at] = row
    common = sorted(set.intersection(*(set(indexed[k]) for k in DATASETS)))
    if not common or cutoff - common[-1] > timedelta(minutes=15):
        return {"state": "DATA_MISSING", "reason": "no fresh fully published common interval"}
    last = common[-1]
    tr, order, book = (indexed[k][last] for k in DATASETS)
    buy, sell = number(tr["val_b"]), number(tr["val_s"])
    pb, ps, cb, cs = (
        number(order[k]) for k in ("put_val_b", "put_val_s", "cancel_val_b", "cancel_val_s")
    )
    if min(buy, sell, pb, ps, cb, cs) < 0 or buy + sell <= 0 or pb + ps + cb + cs <= 0:
        return {"state": "DATA_MISSING", "reason": "empty/invalid trade or order flow"}
    trade = (buy - sell) / (buy + sell)
    order_flow = (pb - cb - ps + cs) / (pb + ps + cb + cs)
    spread = number(book["spread_bbo"])
    if spread < 0:
        raise ValueError("negative spread")
    variants = {}
    for name, count in zip(VARIANTS, (1, 3, 6), strict=True):
        times = [last - timedelta(minutes=5 * i) for i in range(count)]
        if any(at not in indexed["obstats"] for at in times):
            variants[name] = {"state": "DATA_MISSING", "reasons": ["nonconsecutive book window"]}
            continue
        values = [number(indexed["obstats"][at]["imbalance_val"]) for at in times]
        if any(abs(v) > 1 for v in values):
            raise ValueError("book imbalance out of range")
        mean = sum(values) / count
        reasons = [
            label
            for fail, label in (
                (trade < Decimal("-.25"), "trade imbalance below -0.25"),
                (order_flow < Decimal("-.10"), "order flow below -0.10"),
                (spread > 3, "spread above 3 bps"),
                (mean < Decimal("-.20"), "book imbalance below -0.20"),
            )
            if fail
        ]
        variants[name] = {
            "state": "REJECTED" if reasons else "RESEARCH_ELIGIBLE",
            "reasons": reasons,
            "book_mean": str(mean),
            "book_times": [t.isoformat() for t in times],
        }
    # Keep exact selected inputs, including systime. Future rows are not recorded as evidence.
    start = last - timedelta(minutes=25)
    inputs = {
        k: [indexed[k][t] for t in sorted(indexed[k]) if start <= t <= last] for k in DATASETS
    }
    return {
        "state": "EVALUATED",
        "flow_at": last.isoformat(),
        "trade": str(trade),
        "order_flow": str(order_flow),
        "spread_bps": str(spread),
        "variants": variants,
        "inputs": inputs,
    }


def morning_plan(account: str, now: datetime) -> tuple[dict[str, Any], str]:
    candidates = []
    for path in STATE.parent.glob("run-*/plan.json"):
        raw = read_json(path)
        if not raw["run_id"].startswith("shadow-"):
            raise ValueError("invalid morning run identity")
        if datetime.fromisoformat(raw["run_id"].removeprefix("shadow-")).tzinfo is None:
            raise ValueError("naive morning decision time")
        at = stamp(raw["run_id"].removeprefix("shadow-"))
        if at.date() == now.astimezone(MOSCOW).date() and time(10, 5) <= at.time() <= time(10, 25):
            candidates.append((at, path, raw))
    if not candidates:
        raise ValueError("scheduled morning plan missing")
    _, path, plan = min(candidates, key=lambda x: x[0])
    portfolio = path.with_name("portfolio.json").read_bytes()
    state = json.loads(portfolio)
    if (
        plan["portfolio_input_sha256"] != hashlib.sha256(portfolio).hexdigest()
        or state.get("account_id") != account
        or plan["portfolio_input"].get("account_id") != account
        or state.get("source") != "t_invest_sandbox"
        or plan["quality"]["passed"] is not True
        or plan["geo"]["level"] != "normal"
    ):
        raise ValueError("morning plan binding/quality/georisk not eligible")
    strategy = plan["config_snapshot"]["strategy"]
    expected = {
        "top_n": 5,
        "min_momentum": "0.01",
        "candle_period": "1D",
        "flow_min_trade_imbalance": "-0.25",
        "flow_min_order_flow": "-0.10",
        "flow_min_book_imbalance": "-0.20",
        "flow_max_spread_bbo": "3.0",
        "flow_confirmation_enabled": True,
        "trend_window": 60,
        "momentum_windows": [5, 10, 20],
        "require_above_trend": True,
    }
    if any(strategy.get(k) != v for k, v in expected.items()):
        raise ValueError("strategy changed: new protocol review required")
    return plan, hashlib.sha256(path.read_bytes()).hexdigest()


def empty_account(account: str) -> bool:
    broker = SandboxLongBroker(
        os.environ["T_INVEST_SANDBOX_TOKEN"], account, transport=ReadOnlySandboxTransport()
    )
    accounts = _rows(broker._sandbox("GetSandboxAccounts"), "accounts")
    if not any(
        r.get("id") == account and r.get("status") == "ACCOUNT_STATUS_OPEN" for r in accounts
    ):
        raise ValueError("bound sandbox account unavailable")
    positions = broker._sandbox("GetSandboxPositions")
    if positions.get("limitsLoadingInProgress"):
        raise ValueError("positions loading")
    return not (
        any(_rows(positions, k) for k in ("securities", "futures", "options"))
        or _rows(broker._sandbox("GetSandboxOrders"), "orders")
        or _rows(broker._sandbox("GetSandboxStopOrders"), "stopOrders")
    )


def collect(
    now: datetime, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> dict[str, Any]:
    if now.tzinfo is None:
        raise ValueError("aware observation required")
    local = now.astimezone(MOSCOW)
    result: dict[str, Any] = {
        "protocol": PROTOCOL,
        "observed_at": now.isoformat(),
        "date": local.date().isoformat(),
        "state": "SKIPPED",
        "execution_allowed": False,
        "candidates": [],
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    if local.weekday() >= 5 or not time(11, 5) <= local.time() < time(11, 20):
        return result | {"reason": "outside 11:05-11:20 MSK weekday research window"}
    completed = [
        p for p in (BASE / "decisions").glob("*.json") if read_json(p)["state"] == "EVALUATED"
    ]
    if len(completed) >= 40:
        return result | {"reason": "40 sessions complete: manual review required"}
    account = _working_account()
    ledger = ReadOnlyLedger(STATE, account, anchor=ANCHOR)
    if ledger.status() != "WAITING":
        return result | {"reason": "morning entry attempted or completed; no retry research"}
    if not empty_account(account):
        return result | {"reason": "position, active order or stop exists"}
    plan, digest = morning_plan(account, now)
    universe = {r["secid"] for r in read_json(APP / "config/universe.json")}
    top = sorted(
        (r for r in plan["signal_diagnostics"] if r["eligible"]),
        key=lambda r: (-number(r["momentum"]), r["secid"]),
    )[:5]
    cutoff = now  # Fixed before requests, not after seeing later data.
    rows = []
    for rank, candidate in enumerate(top, 1):
        secid = candidate["secid"]
        if secid not in universe or not re.fullmatch("[A-Z0-9]{1,12}", secid):
            raise ValueError("invalid security")
        evaluation = evaluate(flow_rows(secid, local.date()), cutoff)
        rows.append({"ticker": secid, "rank": rank, "morning_signal": candidate, **evaluation})
    done = clock()
    if (
        done.astimezone(MOSCOW).time() >= time(11, 20)
        or ledger.status() != "WAITING"
        or not empty_account(account)
    ):
        return result | {"reason": "collection expired or account changed during observation"}
    if any(r["state"] != "EVALUATED" for r in rows):
        return result | {
            "state": "DATA_MISSING",
            "reason": "incomplete current flow; no candidate selected",
            "candidates": rows,
            "morning_plan_sha256": digest,
        }
    return result | {
        "state": "EVALUATED",
        "decision_at": done.isoformat(),
        "data_cutoff": cutoff.isoformat(),
        "morning_plan_sha256": digest,
        "account_sha256": hashlib.sha256(account.encode()).hexdigest(),
        "morning_flow": plan["flow_diagnostics"],
        "morning_strategy": plan["config_snapshot"]["strategy"],
        "candidates": rows,
        "selected": {
            v: next(
                (
                    r["ticker"]
                    for r in rows
                    if r.get("variants", {}).get(v, {}).get("state") == "RESEARCH_ELIGIBLE"
                ),
                None,
            )
            for v in VARIANTS
        },
    }


def candles(ticker: str, day: date) -> list[dict[str, Any]]:
    if not re.fullmatch("[A-Z0-9]{1,12}", ticker):
        raise ValueError("invalid ticker")
    path = BASE / "prices" / f"{day}-{ticker}.json"
    if path.exists():
        return list(read_json(path)["rows"])
    rows = get_rows(
        f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/{ticker}/candles.json",
        {"from": str(day), "till": str(day), "interval": 10},
        "candles",
        auth=False,
    )
    # Empty responses are not cached as permanent no-trade observations.
    if rows:
        immutable(path, {"retrieved_at": datetime.now(UTC).isoformat(), "rows": rows})
    return rows


def grid(at: datetime) -> datetime:
    floor = at.replace(minute=at.minute // 10 * 10, second=0, microsecond=0)
    return floor if floor == at else floor + timedelta(minutes=10)


def markout(
    row: dict[str, Any], decision: datetime, exit_at: datetime, now: datetime
) -> dict[str, Any]:
    entry_at = grid(decision.astimezone(MOSCOW) + timedelta(seconds=60))
    if exit_at <= entry_at or exit_at + timedelta(minutes=10) > now:
        return {"state": "NOT_MATURE"}
    quotes = {
        stamp(r["begin"]): r
        for day in {entry_at.date(), exit_at.date()}
        for r in candles(row["ticker"], day)
        if number(r["volume"]) > 0
    }
    if entry_at not in quotes or exit_at not in quotes:
        return {"state": "DATA_MISSING"}
    entry, end = number(quotes[entry_at]["open"]), number(quotes[exit_at]["open"])
    if entry <= 0 or end <= 0:
        raise ValueError("invalid candle price")
    gross = (end / entry - 1) * 100
    # Exit spread is a declared proxy, not an observed historical quote.
    cost_bps = Decimal(15) + (number(row["spread_bps"]) + Decimal(3)) / 2
    return {
        "state": "PRICE_MARKOUT_NOT_FILL",
        "entry_at": entry_at.isoformat(),
        "exit_at": exit_at.isoformat(),
        "entry": str(entry),
        "exit": str(end),
        "gross_pct": str(gross),
        "net_pct": str(gross - cost_bps / 100),
        "double_cost_pct": str(gross - cost_bps / 50),
        "cost_bps": str(cost_bps),
        "exit_spread_assumption_bps": "3",
    }


def report(now: datetime) -> dict[str, Any]:
    local = now.astimezone(MOSCOW)
    day = local.date() if local.time() >= time(23, 55) else local.date() - timedelta(days=1)
    sessions = get_rows(
        "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/SBER/candles.json",
        {"from": str(day - timedelta(days=70)), "till": str(day), "interval": 24},
        "candles",
        auth=False,
    )
    # Only infer session existence, never use unfinished daily OHLC as an outcome.
    days = sorted(
        {
            stamp(r["begin"]).date()
            for r in sessions
            if stamp(r["begin"]).weekday() < 5
            and stamp(r["begin"]).date() <= day
            and number(r["volume"]) > 0
        }
    )
    records = []
    for path in sorted((BASE / "decisions").glob("*.json"))[-60:]:
        event = read_json(path)
        if date.fromisoformat(event["date"]) > day:
            continue
        if event["protocol"] != PROTOCOL or event["execution_allowed"] is not False:
            raise ValueError("untrusted protocol record")
        record = {
            "date": event["date"],
            "state": event["state"],
            "selected": event.get("selected"),
            "reason": event.get("reason"),
            "candidates": [],
        }
        if event["state"] == "EVALUATED":
            decision = stamp(event["decision_at"])
            exits = {
                "60min": grid(decision + timedelta(seconds=60)) + timedelta(minutes=60),
                "evening": datetime.combine(decision.date(), time(18, 40), MOSCOW),
            }
            if decision.date() in days:
                i = days.index(decision.date())
                for n in (1, 5):
                    if i + n < len(days):
                        exits[f"{n}session"] = datetime.combine(days[i + n], time(18, 40), MOSCOW)
            for row in event["candidates"]:
                outcomes = (
                    {key: markout(row, decision, at, now) for key, at in exits.items()}
                    if row["state"] == "EVALUATED"
                    else {}
                )
                record["candidates"].append(
                    {
                        "ticker": row["ticker"],
                        "state": row["state"],
                        "variants": row.get("variants"),
                        "outcomes": outcomes,
                    }
                )
        records.append(record)
    return {
        "protocol": PROTOCOL,
        "as_of": now.isoformat(),
        "report_day": str(day),
        "records": records,
        "warning": "Price markouts, not executed trades or account P&L; exit spread assumed 3bp.",
    }


def render(result: dict[str, Any], day: date) -> str:
    today = next((r for r in result["records"] if r["date"] == str(day)), None)
    lines = ["🔬 LONG · эксперимент 11:05 · SHADOW", str(day), "Заявки не отправляются."]
    if today is None:
        lines.append("Наблюдение отсутствует; это не «сигнала нет».")
    elif today["state"] != "EVALUATED":
        lines.append("Пропуск: " + str(today.get("reason")))
    else:
        names = {
            "current": "Текущий фильтр",
            "book_mean_3": "Стакан: среднее 3 интервалов",
            "book_mean_6": "Стакан: среднее 6 интервалов",
        }
        for key, label in names.items():
            lines.append(f"{label}: {today['selected'].get(key) or 'нет кандидата'}")
        for row in today["candidates"]:
            out = row["outcomes"].get("evening", {})
            value = f"{number(out['net_pct']):+.2f}%" if "net_pct" in out else "нет оценки"
            reasons = "; ".join((row.get("variants") or {}).get("current", {}).get("reasons", []))
            lines.append(
                f"• {row['ticker']}: к 18:40 net {value}" + (f" · {reasons}" if reasons else "")
            )
    valid = sum(r["state"] == "EVALUATED" for r in result["records"])
    lines += [
        f"Наблюдений: {valid}/40. Контрольная оценка после 20 и 40 сессий.",
        "Это движение цены с модельными затратами, не прибыль сделки. Выходной спред принят 3 б.п.",
        "PROD не включается; утренний LONG и Intraday не меняются.",
    ]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("collect", "report"))
    args = p.parse_args()
    now = datetime.now(UTC)
    day = now.astimezone(MOSCOW).date()
    try:
        if args.action == "collect":
            path = BASE / "decisions" / f"{day}.json"
            if path.exists():
                read_json(path)  # A broken receipt must not silently count as done.
                print("ALREADY_RECORDED: never overwrite or cherry-pick")
                return 0
            result = collect(now)
            if result.get("reason", "").startswith("outside"):
                print("SKIPPED: outside research window")
                return 0
            immutable(path, result)
            print(result["state"], result.get("reason", "research decisions persisted"))
        else:
            result = report(now)
            day = date.fromisoformat(result["report_day"])
            path = BASE / "reports" / f"{day}.json"
            if path.exists():
                result = read_json(path)
            else:
                immutable(path, result)
            SQLiteOutbox(Path("/var/lib/moex-tinvest-bot/data/notifications.sqlite3")).enqueue(
                kind="long_flow_shadow",
                dedupe_key="long-flow-shadow:" + str(day),
                body=render(result, day),
                now=now,
            )
            print("REPORTED: durable Telegram outbox; no broker mutations")
        return 0
    except Exception as exc:
        # Never leak tokens, account IDs or full HTTP bodies into journalctl.
        print("FAILED: LONG shadow research", type(exc).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
