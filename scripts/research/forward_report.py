"""Report-only wrapper for the immutable, separately installed research bundle.

Keep the frozen report/model implementation. Verify its complete chain once on
one SQLite read transaction; every report query sees exactly that verified view.
This is not a persistent verification cache and never reconstructs decisions.
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

# -I deliberately excludes the script directory; this admin-installed helper is trusted.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from forward_diagnostics import (  # noqa: E402
    classify,
    consumer_gaps,
    notify_status,
    save_diagnostics,
    status_text,
)


def report_diagnostics(report):
    """Bounded, non-secret summary of frozen facts; no reinterpretation or backfill."""
    def count(value):
        return value if type(value) is int and value >= 0 else None

    errors = report.get("source_errors", {})
    safe_errors = {}
    if isinstance(errors, dict):
        for name, value in errors.items():
            if re.fullmatch(
                r"(?:MarketDataService/GetOrderBook|quote:(?:SBER|ROSN|CHMF)):"
                r"(?:TimeoutError|URLError|ValueError|HTTPError|ConnectionError)", str(name)
            ):
                safe_errors[name] = count(value)
    strategies = {}
    raw = report.get("strategies", {})
    if isinstance(raw, dict):
        for name in ("intraday:current", "intraday:hour_trend", "intraday:normalized_trend",
                     "intraday:range_quantile", "long:current", "long:channel_20_10",
                     "long:ensemble_20_60_120"):
            item = raw.get(name)
            if not isinstance(item, dict):
                continue
            states = item.get("states", {})
            marks = item.get("markouts", {})
            strategies[name] = {
                key: count(item.get(key)) for key in ("expected", "decisions", "missed_slots")
            }
            strategies[name]["data_missing"] = (
                count(states.get("data_missing", 0)) if isinstance(states, dict) else None)
            strategies[name]["missing_quotes_by_horizon"] = {
                horizon: count(value.get("missing_quote"))
                for horizon in ("5", "15", "30", "60")
                if isinstance(marks, dict) and isinstance(value := marks.get(horizon), dict)
            }
    return {
        "source_error_count": count(report.get("source_error_count")),
        "source_errors": safe_errors,
        "source_error_entries_are_not_independent_incidents": True,
        "model_entry_missing": count(report.get("model_entry_missing")),
        "strategies": strategies,
        "historical_decisions_unchanged": True,
    }


def snapshot_store_type(original):
    class ReportSnapshot(original):
        def __init__(self, path):
            self.path = path
            # Do not run original constructor: reports must not create/migrate
            # an absent evidence store or initialize a new research registry.
            self._snapshot = sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10
            )
            self._snapshot.execute("PRAGMA query_only=ON")
            self._snapshot.execute("BEGIN")
            self._snapshot.execute("SELECT seq FROM events LIMIT 1").fetchone()
            self._verified = None
            self._report_write = False

        @contextmanager
        def connect(self, *, readonly=False):
            if readonly:
                yield self._snapshot  # Never commit/release this read view early.
            elif self._report_write:
                connection = super().connect()
                try:
                    with connection:
                        yield connection
                finally:
                    connection.close()
            else:
                raise ValueError("report_only_write_denied")

        def verify(self):
            if self._verified is None:
                self._verified = super().verify()
            return dict(self._verified)

        def append(self, kind, key, day, payload, *, now=None):
            if kind != "report" or payload.get("execution_allowed") is not False:
                raise ValueError("report_only_write_denied")
            if self._verified is None:
                raise ValueError("unverified_report_denied")
            self._report_write = True
            try:
                return super().append(kind, key, day, payload, now=now)
            finally:
                self._report_write = False

        def close(self):
            self._snapshot.close()

    return ReportSnapshot


def generate(runtime_module, bundle, state, day=None):
    original = runtime_module.Store
    stores = []
    snapshot_type = snapshot_store_type(original)

    def factory(path):
        store = snapshot_type(path)
        stores.append(store)
        return store

    runtime_module.Store = factory
    try:
        runtime = runtime_module.ForwardRuntime(bundle, state, None)
        completed = runtime_module.expected_completed_day(runtime._now())
        day = day or runtime_module.latest_report_day(runtime.store, runtime._now())
        if day is None:
            return {"execution_allowed": False, "status": "no_completed_observations"}, 2
        if day > completed:
            raise ValueError("observation_window_not_completed")
        report = runtime.report(day)
        # Completeness is from the frozen protocol, not inferred from exit=0.
        incomplete = bool(report.get("incomplete", True))
        diagnostics = report_diagnostics(report)
        diagnostics["source_classification"] = classify(runtime.store, str(day))
        diagnostics["consumer_gaps"] = consumer_gaps(runtime.store, str(day))
        return {
            "execution_allowed": False,
            "day": str(day),
            "status": "INCOMPLETE" if incomplete else "COMPLETE",
            "chain": report["chain"],
            "report": str(state / "reports" / f"{day}.json"),
            "diagnostics": diagnostics,
        }, 3 if incomplete else 0
    finally:
        runtime_module.Store = original
        for store in stores:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--day", type=date.fromisoformat)
    args = parser.parse_args()
    # Service uses -I; only the explicitly selected frozen bundle is imported.
    sys.path.insert(0, str(args.bundle.resolve(strict=True)))
    runtime = importlib.import_module("forward_shadow.runtime")
    notify_status("RESEARCH_REPORT_RUNNING")
    result, code = generate(runtime, args.bundle, args.state, args.day)
    save_diagnostics(args.state, result)
    notify_status(status_text(result))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
