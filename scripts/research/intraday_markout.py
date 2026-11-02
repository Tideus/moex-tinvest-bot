"""Offline forward returns from saved intraday-plan artifacts; no broker transport."""

import argparse
import json
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal as D
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from moex_bot.intraday import _bar, evaluate_candidate
from moex_bot.intraday_config import load_intraday_config

MSK = ZoneInfo("Europe/Moscow")
HORIZONS = (5, 15, 30, 60)
BPS = D(10000)


def stamp(raw):
    t = datetime.fromisoformat(str(raw))
    if t.tzinfo is None:
        raise ValueError("timestamps require timezone")
    return t.astimezone(MSK)


def grid(t):
    floor = t.replace(minute=t.minute // 5 * 5, second=0, microsecond=0)
    return floor if floor == t else floor + timedelta(minutes=5)


@dataclass(frozen=True)
class Quote:
    at: datetime
    bid: D
    ask: D
    session: str

    def __post_init__(self):
        if not (self.bid.is_finite() and self.ask.is_finite() and 0 < self.bid <= self.ask):
            raise ValueError("invalid bid/ask quote")


class Tape:
    def __init__(self, rows, *, proxy):
        self.proxy = proxy
        self.rows = rows
        self.times = {s: sorted(q) for s, q in rows.items()}

    def lookup(self, secid, target, tolerance):
        quotes = self.rows.get(secid, {})
        if self.proxy:
            return quotes.get(grid(target))
        times = self.times.get(secid, [])
        i = bisect_left(times, target)
        if i < len(times) and times[i] - target <= timedelta(seconds=tolerance):
            return quotes[times[i]]
        return None


def markout(tape, secid, side, at, horizon, fee_bps, latency, tolerance, cutoff):
    if side not in ("buy", "sell"):
        raise ValueError("unknown signal direction")
    # Horizons measured from the decision, not from a stale feature timestamp.
    entry_at = at + timedelta(seconds=latency)
    end_at = at + timedelta(minutes=horizon)
    entry = tape.lookup(secid, entry_at, tolerance)
    end = tape.lookup(secid, end_at, tolerance)
    if end_at.date() != at.date() or end_at.time() >= cutoff:
        return {"missing": "outside_session"}
    if entry is None or end is None:
        return {"missing": "no_entry_quote" if entry is None else "no_exit_quote"}
    if (
        entry.at >= end.at
        or entry.session != end.session
        or entry.at.date() != at.date()
        or end.at.date() != at.date()
        or end.at.time() >= cutoff
    ):
        return {"missing": "invalid_holding_window"}
    buy = side == "buy"
    sign = 1 if buy else -1
    start_price, end_price = (entry.ask, end.bid) if buy else (entry.bid, end.ask)
    gross = sign * (end_price / start_price - 1) * BPS
    commission = fee_bps * (1 + end_price / start_price)
    return {
        "entry_at": entry.at.isoformat(),
        "exit_at": end.at.isoformat(),
        "effective_delay_s": (entry.at - at).total_seconds(),
        "entry_price": start_price,
        "exit_price": end_price,
        "exit_mid": (end.ask + end.bid) / 2,
        "directional_mid_bps": sign * ((end.ask + end.bid) / (entry.ask + entry.bid) - 1) * BPS,
        "after_spread_bps": gross,
        "commission_bps": commission,
        "net_bps": gross - commission,
    }


def legacy_rows(plan, bars_by_symbol, cfg, symbols, now):
    """Recorded signals/orders are authoritative. Never invent a historical risk rejection."""
    old = plan["analysis_input"]["config_snapshot"]
    snapshot = json.loads(json.dumps(asdict(cfg), default=str))
    if any(snapshot.get(k) != v for k, v in old.items()):
        raise ValueError("legacy config mismatch; process different configurations separately")
    actual = {s["secid"]: s for s in plan["signals"]}
    intents = {o["intent"]["instrument"]["secid"]: o["intent"] for o in plan["orders"]}
    for s in symbols:
        sig = actual.get(s)
        reasons = []
        oid = None
        if sig:
            planned = plan["phase"] == "entries" and s in intents
            status = "planned" if planned else "entry_rejected"
            if planned:
                oid = intents[s]["order_request_id"]
            else:
                reasons = ["archived_no_entry_reason_unknown"]
            origin = "archived_signal"
        elif plan["phase"] not in ("entries", "monitor") or not plan["signal_diagnostics"]:
            status, reasons, origin = "not_evaluated", ["no_symbol_evaluation"], "archived_phase"
        else:
            history = bars_by_symbol[s]
            sample = tuple(history[k] for k in sorted(history)[-cfg.history_bars :])
            candidate, failures = evaluate_candidate(cfg, s, sample, now)
            sig = asdict(candidate) if candidate else None
            reasons = [r for r in failures if r != "cost_edge" or "min_edge_after_cost_bps" in old]
            status, origin = "filter_rejected", "reconstructed_not_recorded"
            if not reasons:
                reasons = ["historical_reason_unknown"]
        yield dict(
            secid=s,
            status=status,
            reasons=reasons,
            candidate=sig,
            order_request_id=oid,
            origin=origin,
        )


def analyze(
    directory,
    *,
    fee_bps,
    latency,
    tolerance=1,
    quotes_path=None,
    legacy_config=None,
    legacy_universe=None,
):
    if not fee_bps.is_finite() or not 0 <= fee_bps < BPS or min(latency, tolerance) < 0:
        raise ValueError("invalid fee, delay or quote tolerance")
    paths = sorted(directory.glob("intraday-plan-*.json"))
    if not paths:
        raise ValueError("no intraday-plan artifacts")
    observations, seen_runs, proxy_quotes = [], set(), defaultdict(dict)
    history = defaultdict(dict)
    day = previous = None
    cfg = load_intraday_config(legacy_config) if legacy_config else None
    symbols = (
        [v["secid"] for v in json.loads(legacy_universe.read_text())] if legacy_universe else []
    )
    for path in paths:
        plan = json.loads(path.read_bytes())
        inputs = plan["analysis_input"]
        as_of = stamp(inputs["as_of"])
        if previous is not None and as_of < previous:
            raise ValueError("plan files not in decision-time order")
        previous = as_of
        if plan["run_id"] in seen_runs:
            raise ValueError("duplicate plan run_id")
        seen_runs.add(plan["run_id"])
        at = max(as_of, stamp(inputs.get("decision_ready_at", inputs["as_of"])))
        cutoff = time.fromisoformat(inputs["config_snapshot"]["force_flat_moscow"])
        if day != as_of.date():
            history.clear()
            day = as_of.date()
        for raw in inputs["supercandles"]:
            bar = _bar(raw)
            if bar.observed_at > as_of or bar.observed_at.astimezone(MSK).date() != day:
                continue
            history[bar.secid].setdefault(bar.observed_at, bar)
            if quotes_path is None:
                half = bar.spread_bbo / (2 * BPS)
                proxy_quotes[bar.secid].setdefault(
                    bar.observed_at,
                    Quote(
                        bar.observed_at,
                        bar.close * (1 - half),
                        bar.close * (1 + half),
                        day.isoformat(),
                    ),
                )
        if "signal_journal" in plan:
            journal = plan["signal_journal"]
            if journal["version"] != 1:
                raise ValueError("unsupported signal journal")
            rows = journal["decisions"]
            strategy = journal["strategy_id"]
        else:
            if cfg is None or not symbols:
                raise ValueError("legacy plans require --legacy-config and --legacy-universe")
            rows = legacy_rows(plan, history, cfg, symbols, as_of)
            strategy = "intraday_momentum_v1"
        execution_path = path.with_name(
            path.name.replace("intraday-plan-", "intraday-execution-", 1)
        )
        execution = json.loads(execution_path.read_bytes()) if execution_path.is_file() else None
        if execution is not None and execution["run_id"] != plan["run_id"]:
            raise ValueError("execution run_id mismatch")
        submitted = (
            {}
            if execution is None
            else {x["intent"]["order_request_id"]: x for x in execution["submitted"]}
        )
        for row in rows:
            evidence = None
            if oid := row.get("order_request_id"):
                record = submitted.get(oid)
                evidence = {
                    "status": record["status"] if record else "not_observed",
                    "filled_lots": record["filled_lots"] if record else None,
                    "cycle_stopped_reason": execution.get("stopped_reason") if execution else None,
                }
            observations.append(
                dict(
                    **row,
                    run_id=plan["run_id"],
                    strategy_id=strategy,
                    decision_at=at.isoformat(),
                    phase=plan["phase"],
                    quality_passed=plan["quality"]["passed"],
                    cutoff=cutoff.isoformat(),
                    execution_evidence=evidence,
                )
            )
    if quotes_path:
        with quotes_path.open(encoding="utf-8") as stream:
            for line in stream:
                r = json.loads(line)
                q = Quote(stamp(r["at"]), D(str(r["bid"])), D(str(r["ask"])), r["session"])
                if q.at in proxy_quotes[r["secid"]]:
                    raise ValueError("duplicate quote timestamp for security")
                proxy_quotes[r["secid"]][q.at] = q
    tape = Tape(proxy_quotes, proxy=quotes_path is None)
    values, missing, unique = defaultdict(list), Counter(), set()
    for r in observations:
        cand = r["candidate"]
        if not cand or D(str(cand["price_move"])) == 0:
            r["excluded"] = "no_direction"
            continue
        group = r["status"]
        # Keep ALL rows, but only first evaluation of a bar per cohort enters its mean.
        key = (r["strategy_id"], r["secid"], stamp(cand["observed_at"]), str(cand["side"]), group)
        repeat = key in unique
        if repeat:
            r["summary_excluded"] = "repeat_in_cohort"
        unique.add(key)
        r["horizons"] = {}
        for h in HORIZONS:
            m = markout(
                tape,
                r["secid"],
                str(cand["side"]),
                stamp(r["decision_at"]),
                h,
                fee_bps,
                latency,
                tolerance,
                time.fromisoformat(r["cutoff"]),
            )
            r["horizons"][str(h)] = m
            if "missing" not in m:
                sign = 1 if str(cand["side"]) == "buy" else -1
                m["from_signal_close_bps"] = (
                    sign * (m["exit_mid"] / D(str(cand["price"])) - 1) * BPS
                )
            if repeat:
                continue
            label = f"{group}/{h}"
            if "missing" in m:
                missing[label] += 1
            else:
                values[label].append(m)
    summary = {}
    for label in sorted(set(values) | set(missing)):
        samples = values[label]
        v = [m["net_bps"] for m in samples]
        summary[label] = dict(
            n=len(v),
            missing=missing[label],
            mean_net_bps=sum(v) / len(v) if v else None,
            median_net_bps=median(v) if v else None,
            mean_directional_mid_bps=sum(m["directional_mid_bps"] for m in samples) / len(v)
            if v
            else None,
        )
    return dict(
        version=1,
        source="5m_close_spread_proxy" if tape.proxy else "top_of_book",
        fee_bps_per_side=fee_bps,
        latency_seconds=latency,
        tolerance_seconds=tolerance,
        runs=len(paths),
        counts=dict(Counter(r["status"] for r in observations)),
        rejection_reasons=dict(Counter(reason for r in observations for reason in r["reasons"])),
        summary=summary,
        observations=observations,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("artifacts", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--fee-bps", type=D, default=D(5))
    p.add_argument("--delay-seconds", type=int, default=1)
    p.add_argument("--quote-tolerance-seconds", type=int, default=1)
    p.add_argument("--quotes", type=Path)
    p.add_argument("--legacy-config", type=Path)
    p.add_argument("--legacy-universe", type=Path)
    a = p.parse_args()
    result = analyze(
        a.artifacts,
        fee_bps=a.fee_bps,
        latency=a.delay_seconds,
        tolerance=a.quote_tolerance_seconds,
        quotes_path=a.quotes,
        legacy_config=a.legacy_config,
        legacy_universe=a.legacy_universe,
    )
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(
        json.dumps(result, default=str, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps({k: v for k, v in result.items() if k != "observations"}, default=str, indent=2)
    )


if __name__ == "__main__":
    main()
