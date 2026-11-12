"""Public MOEX data audit and frozen signal probes for the 24-stock research universe.

The output is a signal markout diagnostic, NOT a portfolio backtest. Historical bid/ask,
dividends and changing lot sizes are not inferred from candles. No broker API is used.
"""

import argparse
import csv
import hashlib
import io
import json
import math
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from time import sleep
from urllib.parse import urlencode
from urllib.request import urlopen

from universe_watch import MSK, cohorts, digest, policy, write_result


def candles(symbol, board, interval, start, end, root):
    if end >= datetime.now(MSK).date():
        raise ValueError("download completed dates only")
    stem = f"{symbol}_{board}_{interval}_{start}_{end}"
    raw_dir = root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rows, pages, seen = [], [], set()
    for _ in range(200):
        url = (
            "https://iss.moex.com/iss/engines/stock/markets/shares/boards/"
            f"{board}/securities/{symbol}/candles.json?"
            + urlencode(
                {
                    "iss.meta": "off",
                    "from": start.isoformat(),
                    "till": end.isoformat(),
                    "interval": interval,
                    "start": len(rows),
                }
            )
        )
        path = raw_dir / f"{stem}_{len(rows)}.json"
        if path.exists():
            raw = path.read_bytes()
        else:
            for attempt in range(3):
                try:
                    with urlopen(url, timeout=25) as response:
                        raw = response.read()
                    json.loads(raw)["candles"]
                    with path.open("xb") as stream:
                        stream.write(raw)
                    break
                except (OSError, ValueError, KeyError):
                    if attempt == 2:
                        raise
                    sleep(attempt + 1)
        block = json.loads(raw)["candles"]
        batch = [dict(zip(block["columns"], row, strict=True)) for row in block["data"]]
        pages.append(
            {
                "url": url,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "rows": len(batch),
                "received_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            }
        )
        if not batch:
            break
        for r in batch:
            if any(
                not math.isfinite(float(r[k]))
                for k in ("open", "high", "low", "close", "value", "volume")
            ):
                raise ValueError("nonfinite candle value")
            if r["begin"] in seen or (rows and r["begin"] <= rows[-1]["begin"]):
                raise ValueError("duplicate or unordered candles")
            if not start <= date.fromisoformat(r["begin"][:10]) <= end:
                raise ValueError("candle outside requested dates")
            if not 0 < r["low"] <= min(r["open"], r["close"]):
                raise ValueError("invalid candle low")
            if r["high"] < max(r["open"], r["close"]) or min(r["value"], r["volume"]) < 0:
                raise ValueError("invalid candle range or volume")
            seen.add(r["begin"])
            rows.append(r)
    else:
        raise ValueError("pagination limit")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=block["columns"])
    writer.writeheader()
    writer.writerows(rows)
    (root / f"{symbol}_{board}_{interval}.csv").write_text(output.getvalue())
    (root / f"{symbol}_{board}_{interval}.manifest.json").write_text(json.dumps(pages, indent=2))
    return {"symbol": symbol, "board": board, "interval": interval, "rows": len(rows)}


def load(root, symbol, interval, board="TQBR"):
    path = root / f"{symbol}_{board}_{interval}.csv"
    if not path.exists():
        raise ValueError(f"missing data file {path.name}")
    rows = []
    with path.open() as stream:
        for raw in csv.DictReader(stream):
            row = {k: float(raw[k]) for k in ("open", "high", "low", "close", "value", "volume")}
            if any(not math.isfinite(value) for value in row.values()):
                raise ValueError("nonfinite cached candle value")
            row.update(
                at=datetime.fromisoformat(raw["begin"]), end=datetime.fromisoformat(raw["end"])
            )
            if rows and row["at"] <= rows[-1]["at"]:
                raise ValueError("duplicate/unordered cached candles")
            rows.append(row)
    return rows


def net_markout(entry, exit_price, cost_multiplier, probe):
    fee = float(probe["commission_bps_per_side"]) / 10000 * cost_multiplier
    impact = (
        (float(probe["half_spread_bps_per_side"]) + float(probe["slippage_bps_per_side"]))
        / 10000
        * cost_multiplier
    )
    buy, sell = entry * (1 + impact), exit_price * (1 - impact)
    return ((sell * (1 - fee) - buy * (1 + fee)) / buy) * 10000


def probes(symbol, daily, intra, cfg, start, end):
    probe = cfg["historical_probes"]
    jumps = [
        i
        for i in range(1, len(daily))
        if abs(daily[i]["open"] / daily[i - 1]["close"] - 1)
        >= float(probe["large_gap_quarantine_fraction"])
    ]
    outcomes, skips = [], Counter()
    n = cfg["turnover_sessions"]

    def liquid(i):
        return i >= n and median(r["value"] for r in daily[i - n : i]) >= float(
            cfg["min_median_daily_turnover_rub"]
        )

    def add(model, at, enter, leave, extra=None):
        record = {
            "symbol": symbol,
            "model": model,
            "signal_at": at.isoformat(),
            "entry_at": enter["at"].isoformat(),
            "exit_at": leave["at"].isoformat(),
            "entry_open": enter["open"],
            "exit_open": leave["open"],
            "gross_mid_bps": (leave["open"] / enter["open"] - 1) * 10000,
            "net_1x_bps": net_markout(enter["open"], leave["open"], 1, probe),
            "net_2x_bps": net_markout(enter["open"], leave["open"], 2, probe),
        }
        outcomes.append(record | (extra or {}))

    previous_month = None
    for i, row in enumerate(daily):
        month = row["at"].strftime("%Y-%m")
        first = month != previous_month
        previous_month = month
        if not first or not start <= row["at"].date() <= end:
            continue
        if i < 199:
            skips["long_warmup"] += 1
            continue
        if any(i - 199 <= j <= i for j in jumps):
            skips["long_corporate_action_quarantine"] += 1
            continue
        if row["close"] <= mean(r["close"] for r in daily[i - 199 : i + 1]):
            skips["long_no_signal"] += 1
            continue
        # Signal after the close; turnover up to this completed day is then known.
        if not liquid(i + 1):
            skips["long_liquidity"] += 1
            continue
        if i + 21 >= len(daily):
            skips["long_unresolved_horizon"] += 1
            continue
        # Flag events during the future evaluation window; never silently remove them.
        add(
            "L1_20session_markout",
            row["end"],
            daily[i + 1],
            daily[i + 21],
            {
                "corporate_action_in_holding_window": any(i < j <= i + 21 for j in jumps),
            },
        )
    day_index = {r["at"].date(): i for i, r in enumerate(daily)}
    intradays = {}
    for r in intra:
        intradays.setdefault(r["at"].date(), {})[r["at"].strftime("%H:%M")] = r
    for day, rows in sorted(intradays.items()):
        if not start <= day <= end:
            continue
        keys = [f"10:{m:02}" for m in range(0, 60, 10)]
        if not all(k in rows for k in keys):
            skips["intraday_missing_opening_bar"] += 1
            continue
        opening = rows["10:50"]["close"] / rows["10:00"]["open"] - 1
        if opening >= -0.005:
            skips["intraday_no_signal"] += 1
            continue
        i = day_index.get(day)
        if i is None or not liquid(i):
            skips["intraday_liquidity"] += 1
            continue
        if i in jumps:
            skips["intraday_corporate_action_quarantine"] += 1
            continue
        enter = datetime.combine(day, datetime.min.time()) + timedelta(hours=11, minutes=10)
        expected = [(enter + timedelta(minutes=10 * j)).strftime("%H:%M") for j in range(13)]
        if not all(k in rows for k in expected):
            skips["intraday_missing_holding_bar"] += 1
            continue
        add("I2_120min_markout", rows["10:50"]["end"], rows["11:10"], rows["13:10"])
    return outcomes, {
        "symbol": symbol,
        "daily_rows": len(daily),
        "intraday_rows": len(intra),
        "first_daily": daily[0]["at"].isoformat() if daily else None,
        "last_daily": daily[-1]["at"].isoformat() if daily else None,
        "large_gaps": [daily[i]["at"].isoformat() for i in jumps],
        "skips": dict(skips),
    }


def report(root, cfg, start, end):
    outcomes, inventory = [], []
    for s in cfg["symbols"]:
        rows, audit = probes(s, load(root, s, 24), load(root, s, 10), cfg, start, end)
        outcomes.extend(rows)
        inventory.append(audit)
    fund = {}
    for board in ("TQTF", "TQBR"):
        path = root / f"LQDT_{board}_24.csv"
        if path.exists():
            for row in load(root, "LQDT", 24, board):
                day = row["at"].date()
                if day in fund:
                    raise ValueError("overlapping fund board history")
                fund[day] = row
    for row in outcomes:
        if row["model"] != "L1_20session_markout":
            continue
        enter, leave = (datetime.fromisoformat(row[k]).date() for k in ("entry_at", "exit_at"))
        row["lqdt_same_dates_net_1x_bps"] = (
            net_markout(fund[enter]["open"], fund[leave]["open"], 1, cfg["historical_probes"])
            if enter in fund and leave in fund
            else None
        )
    summary = {}
    for cohort, symbols in cohorts(cfg).items():
        summary[cohort] = {}
        for model in ("L1_20session_markout", "I2_120min_markout"):
            records = [r for r in outcomes if r["symbol"] in symbols and r["model"] == model]
            summary[cohort][model] = {
                "n": len(records),
                "symbols": len({r["symbol"] for r in records}),
                "mean_net_1x_bps": mean(r["net_1x_bps"] for r in records) if records else None,
                "mean_net_2x_bps": mean(r["net_2x_bps"] for r in records) if records else None,
                "median_net_1x_bps": median(r["net_1x_bps"] for r in records) if records else None,
                "holding_event_flags": sum(
                    r.get("corporate_action_in_holding_window", False) for r in records
                ),
            }
            fund_samples = [
                r["lqdt_same_dates_net_1x_bps"]
                for r in records
                if r.get("lqdt_same_dates_net_1x_bps") is not None
            ]
            summary[cohort][model]["lqdt_same_dates_n"] = len(fund_samples)
            summary[cohort][model]["mean_lqdt_same_dates_net_1x_bps"] = (
                mean(fund_samples) if fund_samples else None
            )
    return {
        "experiment_id": cfg["experiment_id"],
        "policy_sha256": digest(cfg),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "summary": summary,
        "inventory": inventory,
        "observations": outcomes,
        "meaning": cfg["historical_probes"]["meaning"],
        "benchmark_note": (
            "LQDT matched LONG entry/exit dates; same cost proxy, no tax, different risk."
        ),
        "dividend_coverage": "NOT_VERIFIED",
        "historical_lot_coverage": "NOT_VERIFIED",
        "quote_liquidity_coverage": "UNAVAILABLE_FROM_CANDLES",
        "survivorship_bias": True,
        "untouched_out_of_sample": False,
        "profitability_status": "NOT_ESTABLISHED",
        "promotion_to_trading": False,
        "data_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("*.csv"))
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "report"))
    parser.add_argument("--policy", type=Path, default=Path(__file__).with_name("universe24.json"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 9, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 11))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.start > args.end:
        raise ValueError("invalid date range")
    cfg = policy(args.policy)
    if args.command == "fetch":
        args.data.mkdir(parents=True, exist_ok=True)
        jobs = [
            (
                s,
                "TQBR",
                interval,
                date(2017, 1, 1) if interval == 24 else args.start,
                args.end,
                args.data,
            )
            for s in cfg["symbols"]
            for interval in (24, 10)
        ]
        jobs += [("LQDT", board, 24, args.start, args.end, args.data) for board in ("TQTF", "TQBR")]
        results, errors = [], []
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(candles, *job): job for job in jobs}
            for future in as_completed(futures):
                try:
                    row = future.result()
                    results.append(row)
                    print(json.dumps(row), flush=True)
                except Exception as exc:
                    errors.append({"job": str(futures[future][:-1]), "error": type(exc).__name__})
        write_result(args.output, {"results": results, "errors": errors})
        if errors:
            raise SystemExit(2)
    else:
        result = report(args.data, cfg, args.start, args.end)
        write_result(args.output, result)
        print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
