"""Print sanitized, reproducible golden files from the Sept–Oct 2026 report.

No network, no writes. Expected results are taken only from the archived bot.
"""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path


def extract(report: Path) -> dict[str, object]:
    hashes = {}

    def read(name: str):
        raw = (report / name).read_bytes()
        hashes[name] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    def artifact(name: str):
        return read("state/artifacts/" + name + ".json")

    def portfolio(raw):
        return {
            k: raw[k]
            for k in (
                "cash",
                "blocked_cash",
                "reported_equity",
                "positions",
                "daily_turnover",
                "open_orders",
                "source",
                "observed_at",
            )
        }

    lt = "20260907T070519Z"
    it = "20260907T134002Z"
    long = artifact("shadow-" + lt)
    intra = artifact("intraday-plan-" + it)
    inp = intra["analysis_input"]
    batches = []
    for stamp in ("20260907T133003Z", "20260907T133502Z", it, "20260907T134502Z"):
        plan = artifact("intraday-plan-" + stamp)["analysis_input"]
        batches.append({"captured_at": plan["as_of"], "rows": plan["supercandles"]})
    later = artifact("shadow-20260908T070502Z")
    database = report / "state/data/intraday.sqlite3"
    hashes["state/data/intraday.sqlite3"] = hashlib.sha256(database.read_bytes()).hexdigest()
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        (opening,) = db.execute(
            "SELECT opening_equity FROM intraday_sessions WHERE trade_day=?", ("2026-09-07",)
        ).fetchone()
        # First signal of the day is the one just emitted by the selected plan.
        first = db.execute(
            "SELECT secid, observed_at, side FROM intraday_signals "
            "WHERE trade_day=? ORDER BY observed_at LIMIT 1",
            ("2026-09-07",),
        ).fetchone()
        (sig,) = intra["signals"]
        assert first == (sig["secid"], sig["observed_at"].replace(" ", "T"), sig["side"])
    files = {
        "universe": read("config/universe.json"),
        "long_config": long["config_snapshot"],
        "intraday_config": read("config/intraday.json"),
        "intraday_config_recorded": inp["config_snapshot"],
        "long_portfolio": portfolio(artifact("portfolio-" + lt)),
        "intraday_portfolio": portfolio(inp["portfolio_input"]),
        "geo": artifact("geo-" + lt),
        "long_input": {
            "as_of": long["run_id"].removeprefix("shadow-"),
            "batches": [
                {"captured_at": x["run_id"].removeprefix("shadow-"), "rows": x["market"]}
                for x in (long, later)
            ],
        },
        "intraday_input": {
            "as_of": inp["as_of"],
            "opening_equity": opening,
            "prior_signals": [],
            "batches": batches,
        },
        "long_expected": {
            k: long[k]
            for k in (
                "run_id",
                "quality",
                "geo",
                "targets",
                "orders",
                "rejected",
                "rebalance_allowed",
                "signal_diagnostics",
            )
        },
        "intraday_expected": {
            k: intra[k]
            for k in (
                "run_id",
                "quality",
                "phase",
                "signals",
                "orders",
                "notes",
                "signal_diagnostics",
            )
        },
    }
    return {**files, "source_hashes": hashes}


if __name__ == "__main__":
    print(json.dumps(extract(Path(sys.argv[1])), ensure_ascii=True))
