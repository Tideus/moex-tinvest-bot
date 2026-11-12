"""Offline 24-stock liquidity checks and forward cohort reports. No broker transport.

Input snapshots must come from the read-only collector. A screen result is data/liquidity
eligibility only: neither a return forecast nor an order. Unknown evidence fails closed.
"""

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_DOWN
from decimal import Decimal as D
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
BPS = D(10000)


def timestamp(value):
    at = datetime.fromisoformat(str(value))
    if at.tzinfo is None:
        raise ValueError("timezone required")
    return at.astimezone(MSK)


def number(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    value = D(str(value))
    if not value.is_finite():
        raise ValueError("finite number required")
    return value


def digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def policy(path):
    raw = json.loads(path.read_bytes())
    symbols, control = raw["symbols"], raw["control_symbols"]
    if (
        raw["mode"] != "research_only"
        or not symbols
        or any(not re.fullmatch(r"[A-Z0-9]{1,12}", s) for s in symbols)
        or len(set(symbols)) != len(symbols)
        or not control
        or len(set(control)) != len(control)
        or not set(control) <= set(symbols)
    ):
        raise ValueError("invalid research universe")
    for key in (
        "order_budget_rub",
        "max_spread_bps",
        "commission_bps_per_side",
        "slippage_bps_per_side",
        "max_roundtrip_cost_bps",
        "min_median_daily_turnover_rub",
        "max_top_book_participation",
        "minimum_quote_coverage",
    ):
        if number(raw[key]) <= 0:
            raise ValueError(f"positive {key} required")
    for key in (
        "max_quote_age_seconds",
        "max_instrument_age_days",
        "turnover_sessions",
        "max_turnover_age_days",
        "minimum_complete_sessions",
    ):
        if type(raw[key]) is not int or raw[key] <= 0:
            raise ValueError(f"positive integer {key} required")
    if (
        number(raw["max_top_book_participation"]) > 1
        or number(raw["minimum_quote_coverage"]) > 1
        or raw["cost_multipliers"] != [1, 2]
        or number(raw["order_budget_rub"]) > 10000
    ):
        raise ValueError("invalid research limits")
    return raw


def cohorts(cfg):
    control = set(cfg["control_symbols"])
    return {
        "control3": control,
        "expansion21": set(cfg["symbols"]) - control,
        "all24": set(cfg["symbols"]),
    }


def check_quote(row, at, cfg):
    """Validate receipt time AND exchange time; price-only proxies never pass."""
    received, exchange = timestamp(row["received_at"]), timestamp(row["exchange_at"])
    age = timedelta(seconds=cfg["max_quote_age_seconds"])
    if not at - age <= exchange <= received <= at:
        raise ValueError("stale_or_future_quote")
    if row["source"] != "observed_top_of_book":
        raise ValueError("unverified_quote_source")
    if row["trading_allowed"] is not True:
        raise ValueError("trading_unavailable")
    bid, ask = number(row["bid"]), number(row["ask"])
    if not 0 < bid <= ask:
        raise ValueError("invalid_bid_ask")
    return bid, ask


def screen_one(row, at, cfg):
    result = {"eligible": False, "quote_valid": False, "reasons": []}
    reasons = result["reasons"]
    try:
        bid, ask = check_quote(row, at, cfg)
        result["quote_valid"] = True
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        reasons.append(f"quote:{exc}")
        return result
    try:
        verified = timestamp(row["instrument_verified_at"])
        if not at - timedelta(days=cfg["max_instrument_age_days"]) <= verified <= at:
            reasons.append("stale_instrument_metadata")
        lot = number(row["lot_size"])
        if lot <= 0 or lot != lot.to_integral_value():
            raise ValueError("invalid_lot_size")
        depths = [number(row[k]) for k in ("bid_size_units", "ask_size_units")]
        if any(n < 0 or n != n.to_integral_value() for n in depths):
            raise ValueError("invalid_depth_units")
        spread = (ask - bid) / ((ask + bid) / 2) * BPS
        cost = spread + 2 * (
            number(cfg["commission_bps_per_side"]) + number(cfg["slippage_bps_per_side"])
        )
        budget = number(cfg["order_budget_rub"])
        quantity = (budget / (ask * lot)).to_integral_value(rounding=ROUND_DOWN) * lot
        capacity = (
            min(depths) * number(cfg["max_top_book_participation"]) / lot
        ).to_integral_value(rounding=ROUND_DOWN) * lot
        result.update(
            spread_bps=spread,
            roundtrip_cost_bps=cost,
            stress_roundtrip_cost_bps=2 * cost,
            hypothetical_units=quantity,
            capacity_units=capacity,
            partial_capacity_ratio=min(capacity / quantity, D(1)) if quantity else D(0),
            quote_age_seconds=(at - timestamp(row["exchange_at"])).total_seconds(),
        )
        if spread > number(cfg["max_spread_bps"]):
            reasons.append("wide_spread")
        if cost > number(cfg["max_roundtrip_cost_bps"]):
            reasons.append("roundtrip_cost")
        if quantity <= 0:
            reasons.append("below_one_lot")
        elif capacity < quantity:
            reasons.append("insufficient_top_book_depth")
        sessions = {}
        for item in row["turnover_sessions"]:
            day = datetime.fromisoformat(item["date"]).date()
            # Today's final turnover and future rows must never enter the screen.
            if day >= at.date():
                continue
            value = number(item["value_rub"])
            if day in sessions or value < 0:
                raise ValueError("duplicate_or_invalid_turnover")
            sessions[day] = value
        days = sorted(sessions)[-cfg["turnover_sessions"] :]
        if len(days) < cfg["turnover_sessions"]:
            reasons.append("insufficient_turnover_history")
        elif at.date() - days[-1] > timedelta(days=cfg["max_turnover_age_days"]):
            reasons.append("stale_turnover_history")
        else:
            turnover = median(sessions[d] for d in days)
            result["median_daily_turnover_rub"] = turnover
            if turnover < number(cfg["min_median_daily_turnover_rub"]):
                reasons.append("low_turnover")
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        reasons.append(f"metadata:{exc}")
    result["eligible"] = not reasons
    return result


def screen(snapshot, cfg):
    at = timestamp(snapshot["as_of"])
    rows = snapshot["rows"]
    by_symbol = {r["secid"]: r for r in rows}
    if len(by_symbol) != len(rows) or not set(by_symbol) <= set(cfg["symbols"]):
        raise ValueError("duplicate or unknown snapshot symbol")
    results = []
    for symbol in cfg["symbols"]:
        # The collector evaluates each received quote immediately; a batch is not atomic.
        row_at = timestamp(by_symbol[symbol].get("decision_at", at)) if symbol in by_symbol else at
        if row_at > at or at - row_at > timedelta(minutes=2):
            raise ValueError("invalid per-symbol decision time")
        item = (
            screen_one(by_symbol[symbol], row_at, cfg)
            if symbol in by_symbol
            else {
                "eligible": False,
                "quote_valid": False,
                "reasons": ["missing_symbol"],
            }
        )
        if not time(10, 15) <= row_at.time() < time(18, 25):
            item["eligible"] = False
            item["reasons"].append("outside_research_entry_window")
        results.append({"secid": symbol, "decision_at": row_at.isoformat(), **item})
    return {
        "experiment_id": cfg["experiment_id"],
        "policy_sha256": digest(cfg),
        "snapshot_sha256": digest(snapshot),
        "as_of": at.isoformat(),
        "purpose": "liquidity eligibility, not a signal, forecast, order or execution",
        "rows": results,
        "cohorts": {
            name: {
                "expected": len(symbols),
                "received": len(symbols & set(by_symbol)),
                "eligible": sum(r["eligible"] for r in results if r["secid"] in symbols),
                "quote_valid": sum(r["quote_valid"] for r in results if r["secid"] in symbols),
                "rejections": dict(
                    Counter(
                        reason for r in results if r["secid"] in symbols for reason in r["reasons"]
                    )
                ),
            }
            for name, symbols in cohorts(cfg).items()
        },
    }


def cohort_markouts(report, cfg):
    """Keep archived eligibility separate from liquidity screen; never relabel orders."""
    output = {}
    for name, symbols in cohorts(cfg).items():
        groups = {}
        for row in report["observations"]:
            if row["secid"] not in symbols or row.get("summary_excluded"):
                continue
            for horizon, mark in row.get("horizons", {}).items():
                key = f"{row['strategy_id']}/{row['status']}/{horizon}"
                group = groups.setdefault(key, {"values": [], "missing": 0})
                if "missing" in mark:
                    group["missing"] += 1
                else:
                    group["values"].append(number(mark["net_bps"]))
        output[name] = {
            key: {
                "n": len(g["values"]),
                "missing": g["missing"],
                "mean_net_bps": sum(g["values"]) / len(g["values"]) if g["values"] else None,
                "median_net_bps": median(g["values"]) if g["values"] else None,
            }
            for key, g in sorted(groups.items())
        }
    return output


def session_receipt(directory, day, cfg, as_of=None):
    """Derive completeness from captured evidence on a fixed 5-minute observation grid."""
    as_of = as_of or datetime.now(UTC)
    start = datetime.combine(day, time(10, 15), MSK)
    close = datetime.combine(day, time(18, 25), MSK)
    slots = {start + timedelta(minutes=5 * i) for i in range(98)}
    captured, quotes, hashes = set(), 0, {}
    for path in sorted(directory.glob("capture-*.json")):
        raw = json.loads(path.read_bytes())
        started = timestamp(raw["started_at"])
        if started.date() != day:
            continue
        slot = started.replace(minute=started.minute // 5 * 5, second=0, microsecond=0)
        if slot not in slots:
            continue
        if (started - slot).total_seconds() > 30:
            raise ValueError("capture missed scheduled start tolerance")
        if slot in captured:
            raise ValueError("duplicate capture slot")
        snapshot = raw["snapshot"]
        if not started <= timestamp(snapshot["as_of"]) <= as_of:
            raise ValueError("invalid capture completion time")
        if raw["screen"]["policy_sha256"] != digest(cfg):
            raise ValueError("capture policy mismatch")
        recomputed = screen(snapshot, cfg)
        if digest(raw["screen"]) != digest(recomputed):
            raise ValueError("capture screen does not match its evidence")
        captured.add(slot)
        quotes += recomputed["cohorts"]["all24"]["quote_valid"]
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "date": day.isoformat(),
        "policy_sha256": digest(cfg),
        "symbols": cfg["symbols"],
        "expected_snapshots": len(slots),
        "observed_snapshots": len(captured),
        "quote_coverage": D(quotes) / (len(slots) * len(cfg["symbols"])),
        "closed": as_of >= close,
        "capture_sha256": hashes,
        "missing_slots": [s.isoformat() for s in sorted(slots - captured)],
    }


def weekly(receipts, cfg, as_of=None):
    """A completed session requires collector completeness evidence, not elapsed time."""
    as_of = as_of or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("aware review time required")
    days, valid, rejected = set(), [], []
    for r in receipts:
        day = r["date"]
        if day in days:
            raise ValueError("duplicate session receipt")
        days.add(day)
        reasons = []
        if date.fromisoformat(day) >= as_of.astimezone(MSK).date():
            reasons.append("unfinished_or_future_session")
        if r.get("policy_sha256") != digest(cfg):
            reasons.append("policy_mismatch")
        expected, seen = r.get("expected_snapshots", 0), r.get("observed_snapshots", 0)
        if type(expected) is not int or type(seen) is not int or not 0 < seen == expected:
            reasons.append("incomplete_session")
        try:
            coverage = number(r["quote_coverage"])
            if not number(cfg["minimum_quote_coverage"]) <= coverage <= 1:
                reasons.append("quote_coverage")
        except (KeyError, ValueError, ArithmeticError):
            reasons.append("quote_coverage")
        if r.get("symbols") != cfg["symbols"] or r.get("closed") is not True:
            reasons.append("unverified_session")
        (rejected if reasons else valid).append({"date": day, "reasons": reasons})
    return {
        "experiment_id": cfg["experiment_id"],
        "policy_sha256": digest(cfg),
        "complete_sessions": len(valid),
        "rejected_sessions": rejected,
        "operational_status": "READY_FOR_REVIEW"
        if len(valid) >= cfg["minimum_complete_sessions"]
        else "INSUFFICIENT_FORWARD_DATA",
        "profitability_status": "NOT_ESTABLISHED",
        "parameter_changes": [],
        "promotion_to_trading": False,
    }


def write_result(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    # No replacement: an experiment's evidence cannot be silently rewritten.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, default=str, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("screen", "markout", "session", "weekly"))
    parser.add_argument("--policy", type=Path, default=Path(__file__).with_name("universe24.json"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quotes", type=Path)
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--delay-seconds", type=int, default=1)
    args = parser.parse_args()
    cfg = policy(args.policy)
    if args.command == "screen":
        result = screen(json.loads(args.input.read_bytes()), cfg)
    elif args.command == "session":
        if args.date is None:
            parser.error("session requires --date")
        result = session_receipt(args.input, args.date, cfg)
    elif args.command == "weekly":
        result = weekly(json.loads(args.input.read_bytes()), cfg)
    else:
        from intraday_markout import analyze

        report = analyze(
            args.input,
            fee_bps=number(cfg["commission_bps_per_side"]),
            latency=args.delay_seconds,
            quotes_path=args.quotes,
        )
        result = {
            "experiment_id": cfg["experiment_id"],
            "policy_sha256": digest(cfg),
            "cohorts": cohort_markouts(report, cfg),
            "raw_report": report,
            "profitability_status": "NOT_ESTABLISHED",
        }
    write_result(args.output, result)


if __name__ == "__main__":
    main()
