"""Report-only wrapper for the immutable, separately installed research bundle.

Keep the frozen report/model implementation. Verify its complete chain once on
one SQLite read transaction; every report query sees exactly that verified view.
This is not a persistent verification cache and never reconstructs decisions.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path


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
        return {
            "execution_allowed": False,
            "day": str(day),
            "status": "INCOMPLETE" if incomplete else "COMPLETE",
            "chain": report["chain"],
            "report": str(state / "reports" / f"{day}.json"),
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
    result, code = generate(runtime, args.bundle, args.state, args.day)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
