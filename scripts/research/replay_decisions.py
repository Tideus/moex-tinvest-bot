"""Offline, no broker calls. Replay the two archived sandbox planning decisions."""

import argparse
import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from moex_bot.cli import _verified_instruments_by_secid
from moex_bot.config import load_config
from moex_bot.domain import Instrument, MarketObservation
from moex_bot.intraday import IntradayStateStore, _bar, build_intraday_plan
from moex_bot.intraday_config import load_intraday_config
from moex_bot.shadow import load_universe, run_hourly_shadow

CASES = Path(__file__).resolve().parents[2] / "tests/fixtures/historical_decisions"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("missing timezone in archived input")
    return result


def visible_batches(tape, as_of):
    # captured_at = original decision-cycle timestamp, NOT an exchange publication time.
    for batch in sorted(tape, key=lambda b: instant(b["captured_at"])):
        captured = instant(batch["captured_at"])
        if captured <= as_of:
            yield [r for r in batch["rows"] if instant(r["observed_at"]) <= captured]


class FrozenMarket:
    def __init__(self, rows):
        self.rows = {r["instrument"]["secid"]: r for r in rows}

    def hourly_observation(self, *, secid, uid, board, **kwargs):
        r = self.rows[secid]
        instrument = dict(r["instrument"])
        if (instrument["uid"], instrument["board"]) != (uid, board):
            raise ValueError("instrument mismatch")
        instrument["tick_size"] = Decimal(instrument["tick_size"])
        return MarketObservation(
            Instrument(**instrument),
            *(Decimal(r[k]) for k in ("price", "trend", "momentum", "volatility")),
            instant(r["observed_at"]),
            r["complete"],
            r["tradable"],
        )


def replay(kind, case_dir=CASES, *, tape=None):
    """Compute without opening *_expected.json. Each run gets isolated mutable state."""
    inputs = read(case_dir / f"{kind}_input.json")
    as_of = instant(inputs["as_of"])
    batches = list(visible_batches(inputs["batches"] if tape is None else tape, as_of))
    universe = load_universe(case_dir / "universe.json")
    portfolio_path = case_dir / f"{kind}_portfolio.json"
    portfolio = read(portfolio_path)
    if instant(portfolio["observed_at"]) > as_of:
        raise ValueError("broker snapshot is from the future")
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "plan.json"
        if kind == "long":
            run_hourly_shadow(
                config=load_config(case_dir / "long_config.json"),
                universe=universe,
                portfolio_path=portfolio_path,
                geo_path=case_dir / "geo.json",
                output_path=out,
                as_of=as_of,
                market_data=FrozenMarket(batches[-1]),
            )
        else:
            cfg = load_intraday_config(case_dir / "intraday_config.json")
            resolved = json.loads(json.dumps(asdict(cfg), default=str))
            if any(
                resolved[k] != v
                for k, v in read(case_dir / "intraday_config_recorded.json").items()
            ):
                raise ValueError("archived intraday config does not match recorded fields")
            store = IntradayStateStore(Path(tmp) / "isolated.sqlite3")
            if inputs["prior_signals"]:
                raise ValueError("this fixture requires a session with no prior entries")
            day = as_of.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()
            store.opening_equity(day, Decimal(inputs["opening_equity"]))
            for rows in batches[:-1]:
                store.add(tuple(_bar(row) for row in rows))
            build_intraday_plan(
                config=cfg,
                instruments=_verified_instruments_by_secid(universe),
                portfolio=portfolio,
                store=store,
                bars=tuple(_bar(row) for row in batches[-1]),
                as_of=as_of,
            ).write(out)
        return read(out)


def comparison(kind, case_dir=CASES, *, tape=None):
    actual = replay(kind, case_dir, tape=tape)
    expected = read(case_dir / f"{kind}_expected.json")
    projection = {key: actual[key] for key in expected}
    extras = {}
    if kind == "long":
        for order in projection["orders"]:
            if order.get("status_detail") is None:
                order.pop("status_detail", None)  # optional field absent in September schema
    if kind == "intraday":
        # The contemporary engine additionally counts the newer cost-edge gate.
        counts = actual["signal_diagnostics"]
        extras = (
            {"failed_cost_edge": counts["failed_cost_edge"]} if "failed_cost_edge" in counts else {}
        )
        projection["signal_diagnostics"] = {k: v for k, v in counts.items() if k not in extras}
    return expected, projection, extras


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-dir", type=Path, default=CASES)
    args = parser.parse_args()
    passed = True
    for kind in ("long", "intraday"):
        expected, actual, extra = comparison(kind, args.case_dir)
        diff = [key for key in expected if expected[key] != actual[key]]
        print(kind, actual["run_id"], "FAIL: " + ",".join(diff) if diff else "MATCH")
        for order in actual["orders"]:
            intent = order["intent"]
            print(
                " ",
                intent["side"],
                intent["instrument"]["secid"],
                intent["lots"],
                "@",
                intent["limit_price"],
            )
        print("  additional counters:", extra)
        passed &= not diff
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
