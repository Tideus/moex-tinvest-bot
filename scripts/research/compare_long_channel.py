"""Offline paired H2 experiment. Run from the project root; no broker or live config writes."""

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from moex_bot.backtest import load_backtest_settings, run_backtest
from moex_bot.config import load_config
from moex_bot.integrations.moexalgo_data import HistoricalCandle
from moex_bot.shadow import load_universe

SYMBOLS = [
    "CHMF",
    "GAZP",
    "GMKN",
    "LKOH",
    "MOEX",
    "MTSS",
    "NVTK",
    "PHOR",
    "PLZL",
    "SBER",
    "T",
    "X5",
    "YDEX",
]
CUT = date(2026, 8, 17)
FIRST = date(2026, 8, 18)
LAST = date(2026, 10, 30)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse(rows):
    bars = tuple(
        HistoricalCandle(
            date.fromisoformat(r["trading_date"]),
            *(Decimal(str(r[k])) for k in ("open", "high", "low", "close", "volume")),
        )
        for r in rows
    )
    dates = [b.trading_date for b in bars]
    if not dates or dates != sorted(set(dates)):
        raise ValueError("missing, duplicate or out-of-order candles")
    return bars


def write(path, payload):
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def compare(snapshot, extension, output):
    sources = {}

    def read(path):
        raw = path.read_bytes()
        sources[str(path)] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    initial = read(snapshot)
    if set(initial["securities"]) != set(SYMBOLS) or initial["benchmark"]["secid"] != "IMOEX":
        raise ValueError("unexpected instruments")
    merged = {}
    for s, rows in (initial["securities"] | {"IMOEX": initial["benchmark"]["candles"]}).items():
        old = parse(rows)
        new = parse(read(extension / f"{s}.json"))
        if (
            old[-1] != new[0]
            or new[0].trading_date != CUT
            or new[1].trading_date != FIRST
            or new[-1].trading_date != LAST
        ):
            raise ValueError(f"incomplete period or OHLCV seam mismatch: {s}")
        merged[s] = old + new[1:]
    bench = merged.pop("IMOEX")
    reference_dates = {b.trading_date for b in merged["SBER"] if b.trading_date >= FIRST}
    for s, bars in merged.items():
        if {b.trading_date for b in bars if b.trading_date >= FIRST} != reference_dates:
            raise ValueError(f"incomplete holdout dates: {s}")
    config_paths = [Path("config") / f for f in ("shadow.json", "universe.json", "backtest.json")]
    for path in config_paths:
        sources[str(path)] = digest(path)
    cfg = load_config(config_paths[0])
    cfg = replace(cfg, strategy=replace(cfg.strategy, flow_confirmation_enabled=False))
    universe = tuple(v for v in load_universe(config_paths[1]) if v.secid in SYMBOLS)
    if {u.secid for u in universe} != set(SYMBOLS) or cfg.strategy.shorts_enabled:
        raise ValueError("expected long-only universe")
    base = load_backtest_settings(config_paths[2])
    summary = []
    output.mkdir(parents=True, exist_ok=False)
    for label, decision_from, stop in (("development", None, CUT), ("holdout", FIRST, LAST)):
        settings = replace(base, end_date=stop, oos_start_date=decision_from or base.oos_start_date)
        reference_axis = None
        for channel in (False, True):
            for mult in (1, 2):
                run = run_backtest(
                    bot_config=cfg,
                    settings=settings,
                    universe=universe,
                    candles=merged,
                    benchmark_candles=bench,
                    channel_long=channel,
                    first_decision_date=decision_from,
                    cost_multiplier=Decimal(mult),
                )
                axis = [e.trading_date for e in run.equity_curve]
                if reference_axis is not None and axis != reference_axis:
                    raise ValueError("strategy valuation dates differ")
                reference_axis = axis
                m = run.metrics
                assert m.start_equity == base.initial_cash
                name = f"{label}-{run.research_strategy}-{mult}x"
                write(output / (name + ".json"), asdict(run) | {"cost_multiplier": mult})
                row = dict(period=label, model=run.research_strategy, costs=mult, **asdict(m))
                summary.append(row)
                print(
                    name,
                    m.sessions,
                    f"net={m.return_pct:.2f}% DD={m.max_drawdown_pct:.2f}% "
                    f"fills={m.trades} turnover={m.turnover:.2f} fees={m.commissions:.2f} "
                    f"benchmark={m.benchmark_return_pct:.2f}%",
                )
    for path in [
        Path(__file__),
        *(
            Path("src/moex_bot") / f
            for f in (
                "backtest.py",
                "backtest_channel.py",
                "backtest_fills.py",
                "harness.py",
                "strategy.py",
                "execution.py",
                "risk.py",
            )
        ),
    ]:
        sources[path.name] = digest(path)
    write(
        output / "comparison.json",
        dict(
            sources_sha256=sources,
            universe=SYMBOLS,
            summary=summary,
            selected_parameters={"entry": 20, "exit": 10},
            benchmark="IMOEX price-only",
            full_strategy_evidence=False,
            split=str(FIRST),
        ),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=Path("artifacts/historical-backtest-long-short/market-data.json"),
    )
    parser.add_argument("--extension", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    compare(args.snapshot, args.extension, args.output_dir)
