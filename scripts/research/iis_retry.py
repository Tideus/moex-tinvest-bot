"""Offline signal experiment. No broker/account access. Times are Moscow local."""
import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, time
from pathlib import Path
from statistics import mean

from long_flow_forward import DATASETS, MOSCOW, evaluate


def read(path):
    return json.loads(path.read_bytes())


def ranking(bars, day):
    ranks = []
    for symbol, rows in bars.items():
        closes = [r["close"] for r in sorted(rows, key=lambda r: r["end"])
                  if r["end"][:10] < day]
        if len(closes) >= 60:
            mom = mean(closes[-1] / closes[-w-1] - 1 for w in (5, 10, 20))
            if mom > .01 and closes[-1] > mean(closes[-60:]):
                ranks.append((-mom, symbol))
    return [s for _, s in sorted(ranks)[:4]]


def select(ranks, flow, at):
    passed = []
    for s in ranks:
        blocks = flow[s].get(str(at.date()))
        if not blocks:
            return "missing", None
        r = evaluate(blocks, at)
        if r["state"] != "EVALUATED":
            return "missing", None
        if r["variants"]["current"]["state"] == "RESEARCH_ELIGIBLE":
            passed.append((s, float(r["spread_bps"])))
    return ("signal", passed[0]) if passed else ("no_signal", None)


def run(root, policy):
    allowed = set(policy["allowed_secids"]) - set(policy["protected_secids"])
    daily = {s: read(root / f"daily-{s}.json")["rows"] for s in allowed}
    lots = {r["ticker"]: r["lot_size"] for r in read(root / "universe.json")}
    sessions = sorted({r["begin"][:10] for r in daily["SBER"]
                       if datetime.fromisoformat(r["begin"]).weekday() < 5 and r["volume"] > 0})
    prices, flow = {}, {}
    for s in allowed:
        flow[s] = defaultdict(lambda: {key: [] for key in DATASETS})
        for key in DATASETS:
            file = root / f"{key}-{s}.json"
            if file.exists():
                for r in read(file)["rows"]:
                    flow[s][r["tradedate"]][key].append(r)
        file = root / f"minutes10-{s}.json"
        prices[s] = ({r["begin"]: r["open"] for r in read(file)["rows"] if r["volume"] > 0}
                     if file.exists() else {})
    events = []
    for i, day in enumerate(sessions):
        if not "2026-10-01" <= day <= "2026-10-30":
            continue
        rank = ranking(daily, day)
        event = {"day": day, "rank": rank, "period": "early" if day <= "2026-10-16" else "late"}
        for h in (10, 14):
            at = datetime.combine(datetime.fromisoformat(day).date(), time(h, 10), MOSCOW)
            state, hit = select(rank, flow, at)
            event[str(h)] = state
            if state == "missing" or not rank:
                break
            if hit is None:
                continue
            s, spread = hit
            entry = prices[s].get(f"{day} {h:02}:20:00")
            event.update(symbol=s, hour=h, entry=entry)
            if not entry or not 0 < entry * lots[s] <= 10000:
                event["price_or_cap_block"] = True
                break
            for horizon in (0, 1, 5):
                if i + horizon >= len(sessions):
                    continue
                end = prices[s].get(f"{sessions[i+horizon]} 18:40:00")
                if end:
                    gross = (end / entry - 1) * 100
                    cost = (15 + (spread + 3) / 2) / 100
                    event[f"net{horizon}"] = gross - cost
                    event[f"stress{horizon}"] = gross - 2 * cost
            break
        events.append(event)
    return events


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("policy", type=Path)
    args = parser.parse_args()
    events = run(args.archive, read(args.policy))
    summary = {}
    for period in ("all", "early", "late"):
        sample = [e for e in events if period in ("all", e["period"])]
        stats = {"days": len(sample), "morning": dict(Counter(e["10"] for e in sample)),
                 "retry": dict(Counter(e["14"] for e in sample if "14" in e))}
        for h in (10, 14):
            for n in (0, 1, 5):
                rows = [e for e in sample if e.get("hour") == h and f"net{n}" in e]
                if rows:
                    stats[f"{h}h_exit{n}"] = {
                        "n": len(rows), "net_mean_pct": mean(e[f"net{n}"] for e in rows),
                        "stress_mean_pct": mean(e[f"stress{n}"] for e in rows),
                        "positive": sum(e[f"net{n}"] > 0 for e in rows),
                    }
        summary[period] = stats
    print(json.dumps({"summary": summary, "dates": events}, indent=2))
