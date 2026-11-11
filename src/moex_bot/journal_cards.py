"""Journal-only trade cards and public MOEX candle cache; never used by trading."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from .trader_journal import MSK, digest, encoded, number, timestamp

HOST = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/"
MAX_POINTS = 6000


def local_day(at: str) -> str:
    return timestamp(at).astimezone(MSK).date().isoformat()


def fetch_day(symbol: str, day: str, interval: int, deadline: float) -> dict[str, Any]:
    """Advance by returned row count, reject malformed/partial/unclosed data."""
    if not re.fullmatch(r"[A-Z0-9]{1,16}", symbol) or interval not in {1, 60}:
        raise ValueError("unsupported chart instrument/interval")
    date.fromisoformat(day)
    points: dict[str, dict[str, str]] = {}
    offset = 0
    observed = datetime.now(UTC)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or offset > MAX_POINTS:
            raise TimeoutError("chart collection budget exhausted")
        url = HOST + symbol + "/candles.json?" + urlencode({
            "from": day, "till": day, "interval": interval, "start": offset,
            "iss.meta": "off", "iss.only": "candles",
            "candles.columns": "begin,end,open,high,low,close",
        })
        with urlopen(url, timeout=min(4.0, remaining)) as response:
            payload = response.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError("oversized candle page")
        block = json.loads(payload)["candles"]
        columns, rows = block["columns"], block["data"]
        if not rows:
            break
        for raw in rows:
            row = dict(zip(columns, raw, strict=True))
            begin = datetime.fromisoformat(row["begin"]).replace(tzinfo=MSK)
            end = datetime.fromisoformat(row["end"]).replace(tzinfo=MSK)
            if begin.date().isoformat() != day or end < begin:
                raise ValueError("candle outside requested session")
            values = {key: number(row[key]) for key in ("open", "high", "low", "close")}
            if (values["low"] <= 0 or values["high"] < max(values.values())
                    or values["low"] > min(values.values())):
                raise ValueError("invalid OHLC")
            # A public minute is not an execution. Only finalized intervals are displayed.
            if end.astimezone(UTC) >= observed:
                continue
            at = end.isoformat()
            point = {"at": at, "begin": begin.isoformat(),
                     **{key: str(value) for key, value in values.items()}}
            if at in points and points[at] != point:
                raise ValueError("conflicting candle")
            points[at] = point
        offset += len(rows)
    return {"source": "MOEX ISS / TQBR", "interval": interval,
            "retrieved_at": observed.isoformat(), "status": "ok" if points else "empty",
            "points": sorted(points.values(), key=lambda p: p["at"])}


def chart_requests(report: dict[str, Any]) -> list[tuple[str, str, int]]:
    result: set[tuple[str, str, int]] = set()
    for stream in report["streams"]:
        interval = 1 if stream["strategy"] == "intraday" else 60
        windows = [(t["symbol"], t["opened_at"], t["closed_at"]) for t in stream["trades"]]
        windows += [(o["symbol"], o["at"], o["at"]) for o in stream["operations"]]
        for symbol, start, end in windows:
            first, last = date.fromisoformat(local_day(start)), date.fromisoformat(local_day(end))
            # Historical long charts are bounded, with visible coverage limits in the card.
            first = max(first, last - timedelta(days=90))
            while first <= last:
                result.add((symbol, first.isoformat(), interval))
                first += timedelta(days=1)
    return sorted(result, key=lambda r: (r[1], r[0], r[2]), reverse=True)


def attach_charts(
    report: dict[str, Any], directory: Path, *, refresh: bool = False,
    fetch: Callable[[str, str, int, float], dict[str, Any]] = fetch_day,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    charts = {}
    deadline = time.monotonic() + 25
    for symbol, day, interval in chart_requests(report):
        key = f"{symbol}:{day}:{interval}"
        path = directory / (digest(key) + ".json")
        try:
            cached = json.loads(path.read_bytes()) if path.exists() else None
        except (OSError, ValueError):
            cached = None
        now = datetime.now(UTC)
        old_day = date.fromisoformat(day) < now.astimezone(MSK).date() - timedelta(days=1)
        fresh = cached and (now - timestamp(cached["retrieved_at"])).total_seconds() < 600
        final = cached and cached["status"] in {"ok", "empty"} and old_day
        if refresh and not (fresh or final) and time.monotonic() < deadline:
            try:
                cached = fetch(symbol, day, interval, deadline)
            except Exception as exc:
                # Retain observations but never disguise a failed refresh as success.
                cached = {**(cached or {"points": [], "interval": interval}),
                          "source": "MOEX ISS / TQBR", "status": "error",
                          "error": type(exc).__name__, "retrieved_at": now.isoformat()}
            temporary = path.with_suffix(".tmp")
            temporary.write_text(encoded(cached), encoding="utf-8")
            temporary.replace(path)
        charts[key] = cached or {"source": "MOEX ISS / TQBR", "interval": interval,
                                 "status": "pending", "points": [], "retrieved_at": None}
    report["charts"] = charts


def reason_context(stream: dict[str, Any], symbol: str, at: str, side: str) -> dict[str, Any]:
    """Nearby signals are context, NOT a verified cause of the broker operation."""
    candidates = [s for s in stream["signals"] if s["symbol"] == symbol
                  and timedelta(0) <= timestamp(at) - timestamp(s["at"]) <= timedelta(minutes=30)
                  and str(s.get("side", "")).upper() == side]
    candidates.sort(key=lambda s: timestamp(s["at"]), reverse=True)
    orders = [o for o in stream.get("order_context", []) if o["symbol"] == symbol
              and o["side"] == side
              and timedelta(0) <= timestamp(at) - timestamp(o["at"]) <= timedelta(minutes=30)]
    orders.sort(key=lambda o: timestamp(o["at"]), reverse=True)
    return {"status": "unlinked", "reason": None, "context": candidates[:3],
            "order_context": orders[:3]}


def attach_order_context(report: dict[str, Any], config: dict[str, Any]) -> None:
    """Read only companion execution artifacts, bound by their own portfolio account.

    run_id is a cycle timestamp, never presented as the broker submission/fill time.
    No old source revisions or financial results are rewritten.
    """
    import hashlib

    streams = {s["id"]: s for s in report["streams"]}
    root = Path(config["artifact_root"])
    found: dict[str, dict[str, dict[str, Any]]] = {}
    for profile in config["profiles"]:
        for pattern in profile["portfolio_globs"]:
            for path in root.glob(pattern):
                companion = path.with_name(path.name.replace("-portfolio-", "-execution-"))
                if path == companion or not companion.is_file():
                    continue
                try:
                    portfolio = json.loads(path.read_bytes())
                    account = portfolio.get("account_id")
                    binding = (hashlib.sha256(account.encode()).hexdigest() if account
                               else portfolio.get("account_binding", ""))
                    stream = profile["id"] + ":" + binding[:16]
                    if stream not in streams or portfolio.get("source") != "t_invest_sandbox":
                        continue
                    raw = json.loads(companion.read_bytes())
                    at = timestamp(raw["run_id"].split("-", 1)[1]).isoformat()
                    for row in raw.get("submitted", []):
                        intent = row["intent"]
                        if not isinstance(intent.get("rationale"), str):
                            continue
                        context = {"at": at, "symbol": intent["instrument"]["secid"],
                                   "side": intent["side"], "rationale": intent["rationale"][:1000]}
                        found.setdefault(stream, {})[digest(context)] = context
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    # Missing context must not turn a nearby signal into a reason.
                    continue
    for key, contexts in found.items():
        streams[key]["order_context"] = list(contexts.values())


def make_cards(stream: dict[str, Any]) -> list[dict[str, Any]]:
    cards = []
    for index, trade in enumerate(stream["trades"]):
        side = "BUY" if trade["direction"] == "LONG" else "SELL"
        card = {"ref": digest([stream["id"], index, trade]), "kind": "closed", **trade,
                "at": trade["closed_at"],
                "entry_reason": reason_context(stream, trade["symbol"], trade["opened_at"], side),
                "exit_reason": reason_context(stream, trade["symbol"], trade["closed_at"],
                                              "SELL" if side == "BUY" else "BUY"),
                "markers": [
                    {"at": trade["opened_at"], "price": trade["entry"], "label": "Вход",
                     "side": side, "quantity": trade["quantity"]},
                    {"at": trade["closed_at"], "price": trade["exit"], "label": "Выход",
                     "side": "SELL" if side == "BUY" else "BUY", "quantity": trade["quantity"]},
                ]}
        links = stream.get("trade_links", {}).get(digest(trade), {})
        for label in ("entry", "exit"):
            if links.get(label):
                card[label + "_reason"] = links[label]
        cards.append(card)
    # Operations remain visible even if FIFO cannot establish a complete trade.
    for index, op in enumerate(stream["operations"]):
        price = op.get("price")
        if "price" not in op and number(op["quantity"]) > 0:
            price = str(number(op["gross"]) / number(op["quantity"]))
        cards.append({"ref": digest([stream["id"], "operation", index, op]),
                      "kind": "operation", "symbol": op["symbol"], "at": op["at"],
                      "side": op["side"],
                      "operation": op, "markers": [{"at": op["at"], "price": price,
                          "label": "Операция", "side": op["side"], "quantity": op["quantity"]}],
                      "operation_reason": reason_context(stream, op["symbol"], op["at"],
                                                         op["side"])})
        link = stream.get("operation_links", {}).get(digest(op))
        if link:
            cards[-1]["operation_reason"] = link
    cards.extend(stream.get("verified_orders", []))
    return cards


def chart_for(card: dict[str, Any], charts: dict[str, Any], interval: int) -> dict[str, Any]:
    start = timestamp(card.get("opened_at", card["at"])) - timedelta(minutes=30)
    end = timestamp(card.get("closed_at", card["at"])) + timedelta(minutes=30)
    if card["kind"] == "operation":
        start = timestamp(card["at"]).astimezone(MSK).replace(hour=0, minute=0, second=0)
        end = start + timedelta(days=1)
    points: list[dict[str, Any]] = []
    sources = []
    for key, chart in charts.items():
        symbol, day, step = key.split(":")
        if (symbol != card["symbol"] or int(step) != interval
                or not local_day(start.isoformat()) <= day <= local_day(end.isoformat())):
            continue
        sources.append({"day": day, **{k: chart.get(k) for k in
                                       ("status", "source", "retrieved_at", "error")}})
        points.extend(p for p in chart["points"] if start <= timestamp(p["at"]) <= end)
    points.sort(key=lambda p: timestamp(p["at"]))
    return {"interval": interval, "points": points[:MAX_POINTS], "sources": sources,
            "truncated": len(points) > MAX_POINTS or (end - start).days > 90,
            "from": start.isoformat(), "to": end.isoformat()}
