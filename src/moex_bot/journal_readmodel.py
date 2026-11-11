"""Disposable, atomic SQLite projection of the existing sanitized journal report.

No broker access. Financial calculations stay in trader_journal.build_report.
Every published inode is immutable; readers open their own read-only snapshot.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .trader_journal import MSK, SCHEMA, digest, encoded, number, timestamp

API_SCHEMA = "trader-journal-api-v1"
KINDS = ("trades", "signals", "operations", "orders", "equity")
STREAM_FIELDS = ("id", "strategy", "environment", "dates", "warnings", "positions", "observed_at")
OPTIONAL_STREAM_FIELDS = ("trade_history_coverage", "order_diagnostics")


class QueryError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def publish(report: dict[str, Any], path: Path) -> str:
    if report.get("schema") != SCHEMA or not isinstance(report.get("streams"), list):
        raise ValueError("unsupported source report")
    path.parent.mkdir(parents=True, exist_ok=True)
    generation = digest(report)
    descriptor, temporary = tempfile.mkstemp(
        prefix="projection-", suffix=".sqlite3", dir=path.parent
    )
    os.close(descriptor)
    candidate = Path(temporary)
    try:
        with closing(sqlite3.connect(candidate)) as db, db:
            db.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE streams (stream TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE summaries (stream TEXT, period TEXT, payload TEXT NOT NULL,
                    PRIMARY KEY(stream,period));
                CREATE TABLE rows (stream TEXT, kind TEXT, seq INTEGER, day TEXT, month TEXT,
                    symbol TEXT, direction TEXT, gross_sign INTEGER, net_sign INTEGER,
                    payload TEXT NOT NULL, PRIMARY KEY(stream,kind,seq));
                CREATE INDEX rows_day ON rows(stream,kind,day,seq);
                CREATE INDEX rows_month ON rows(stream,kind,month,seq);
            """)
            metadata = {
                "schema": API_SCHEMA,
                "generation": generation,
                "generated_at": report["generated_at"],
                "import": report.get("import", {}),
                "demo": report.get("demo", False),
            }
            db.execute("INSERT INTO metadata VALUES ('report',?)", (encoded(metadata),))
            for stream in report["streams"]:
                key = stream["id"]
                db.execute(
                    "INSERT INTO streams VALUES (?,?)",
                    (key, encoded({k: stream[k] for k in (*STREAM_FIELDS, *OPTIONAL_STREAM_FIELDS)
                                   if k in stream})),
                )
                db.executemany(
                    "INSERT INTO summaries VALUES (?,?,?)",
                    ((key, period, encoded(stats)) for period, stats in stream["periods"].items()),
                )
                for kind in KINDS:
                    rows = stream[kind]
                    if kind in {"signals", "operations", "orders"}:
                        rows = sorted(rows, key=lambda r: timestamp(r["at"]), reverse=True)
                    for seq, row in enumerate(rows):
                        day = timestamp(row["closed_at"] if kind == "trades" else row["at"])
                        local = day.astimezone(MSK).date().isoformat()
                        signs = [
                            None
                            if row.get(k) is None
                            else (1 if number(row[k]) > 0 else -1 if number(row[k]) < 0 else 0)
                            for k in ("gross", "net")
                        ]
                        db.execute(
                            "INSERT INTO rows VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (
                                key,
                                kind,
                                seq,
                                local,
                                local[:7],
                                row.get("symbol", ""),
                                row.get("direction", ""),
                                *signs,
                                encoded(row),
                            ),
                        )
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        # Read access for the API's group; no group/world writes. The setgid parent
        # assigns the web reader group without granting access to raw receipts.
        candidate.chmod(0o640)
        with candidate.open("r+b") as file:
            os.fsync(file.fileno())
        os.replace(candidate, path)
        return generation
    finally:
        candidate.unlink(missing_ok=True)


def validate_period(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", value):
        raise QueryError("invalid period")
    try:
        date.fromisoformat(value if len(value) == 10 else value + "-01")
    except ValueError:
        raise QueryError("invalid period") from None
    return value


def _one(db: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> Any:
    row = db.execute(sql, args).fetchone()
    return json.loads(row[0]) if row else None


def _cursor(value: str, scope: str) -> int:
    if not value:
        return -1
    try:
        raw = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if (raw["scope"] != scope or type(raw["seq"]) is not int
                or not 0 <= raw["seq"] <= 2**63 - 1):
            raise ValueError
        return int(raw["seq"])
    except (ValueError, KeyError, TypeError):
        raise QueryError("invalid cursor for this query") from None


def _page(db: sqlite3.Connection, kind: str, params: dict[str, str]) -> dict[str, Any]:
    period, stream = params["period"], params["stream"]
    column = "day" if len(period) == 10 else "month"
    base = f"stream=? AND kind=? AND {column}=?"
    values: list[Any] = [stream, kind, period]
    filters = ""
    if kind == "operations" and params.get("symbol"):
        filters += " AND symbol=?"
        values.append(params["symbol"])
    if kind == "trades":
        for key in ("symbol", "direction"):
            if params.get(key):
                filters += f" AND {key}=?"
                values.append(params[key])
        outcome = params.get("outcome", "")
        if outcome:
            cost = params.get("cost", "gross")
            filters += (
                f" AND {cost}_sign IS NULL" if outcome == "unknown" else f" AND {cost}_sign=?"
            )
            if outcome != "unknown":
                values.append({"win": 1, "loss": -1, "flat": 0}[outcome])
    where = base + filters
    total = db.execute("SELECT COUNT(*) FROM rows WHERE " + where, values).fetchone()[0]
    scope = digest(
        {k: v for k, v in params.items() if k not in {"cursor", "limit"}} | {"kind": kind}
    )
    after = _cursor(params.get("cursor", ""), scope)
    limit = int(params.get("limit", "50"))
    rows = db.execute(
        "SELECT seq,payload FROM rows WHERE " + where + " AND seq>? ORDER BY seq LIMIT ?",
        (*values, after, limit + 1),
    ).fetchall()
    page = rows[:limit]
    cursor = (
        base64.urlsafe_b64encode(encoded({"seq": page[-1][0], "scope": scope}).encode()).decode()
        if len(rows) > limit
        else None
    )
    return {
        "items": [json.loads(r[1]) for r in page],
        "total": total,
        "next_cursor": cursor,
        "period_total": db.execute(
            "SELECT COUNT(*) FROM rows WHERE " + base, (stream, kind, period)
        ).fetchone()[0],
    }


def query(path: Path, route: str, params: dict[str, str]) -> dict[str, Any]:
    allowed = {"generation", "stream", "period"}
    if route in KINDS:
        allowed |= {"cursor", "limit"}
        if route == "trades":
            allowed |= {"symbol", "direction", "outcome", "cost"}
        elif route == "operations":
            allowed.add("symbol")
    elif route not in {"catalog", "summary", "health"}:
        raise QueryError("not found", 404)
    if set(params) - allowed or any(len(v) > 512 for v in params.values()):
        raise QueryError("invalid parameters")
    if route in KINDS:
        if (
            not params.get("limit", "50").isdigit()
            or not 1 <= int(params.get("limit", "50")) <= 100
        ):
            raise QueryError("limit must be 1..100")
        if (
            params.get("direction", "") not in {"", "LONG", "SHORT"}
            or params.get("outcome", "") not in {"", "win", "loss", "flat", "unknown"}
            or params.get("cost", "gross") not in {"gross", "net"}
        ):
            raise QueryError("invalid trade filter")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        metadata = _one(db, "SELECT payload FROM metadata WHERE key='report'")
        if not metadata or metadata.get("schema") != API_SCHEMA:
            raise QueryError("projection unavailable", 503)
        streams = [
            json.loads(r[0]) for r in db.execute("SELECT payload FROM streams ORDER BY stream")
        ]
        if route == "health":
            now = datetime.now(UTC)
            age = (now - timestamp(metadata["generated_at"])).total_seconds()
            build_status = "stale" if age > 1200 or age < -60 else "ok"
            sources = []
            for stream in streams:
                observed = stream.get("observed_at")
                source_age = (now - timestamp(observed)).total_seconds() if observed else None
                sources.append({
                    "id": stream["id"], "observed_at": observed,
                    "age_seconds": round(source_age) if source_age is not None else None,
                    "status": "missing" if source_age is None else
                    "stale" if source_age > 1200 or source_age < -60 else "ok",
                })
            source_status = (
                "missing" if not sources or any(s["status"] == "missing" for s in sources)
                else "stale" if any(s["status"] == "stale" for s in sources) else "ok"
            )
            return {
                "schema": API_SCHEMA,
                "generation": metadata["generation"],
                "generated_at": metadata["generated_at"],
                "status": "stale" if build_status != "ok" else "import_error"
                if metadata.get("import", {}).get("errors") else
                "source_" + source_status if source_status != "ok" else "ok",
                "age_seconds": round(age),
                "build_status": build_status,
                "source_status": source_status,
                "sources": sources,
            }
        if route == "catalog":
            return {
                **metadata,
                "streams": [
                    {k: v for k, v in s.items() if k not in {"positions", "warnings"}}
                    for s in streams
                ],
            }
        if params.get("generation") != metadata["generation"]:
            raise QueryError("snapshot changed; refresh journal", 409)
        period = validate_period(params.get("period", ""))
        stream = next((s for s in streams if s["id"] == params.get("stream")), None)
        if stream is None:
            raise QueryError("stream not found", 404)
        if route in KINDS:
            return {**metadata, **_page(db, route, params)}
        month = period[:7]
        periods = dict(
            (p, json.loads(data))
            for p, data in db.execute(
                "SELECT period,payload FROM summaries WHERE stream=? AND period>=? AND period<?",
                (stream["id"], month, month + "~"),
            )
        )
        overview = [
            {
                "id": s["id"],
                "strategy": s["strategy"],
                "environment": s["environment"],
                "stats": _one(
                    db,
                    "SELECT payload FROM summaries WHERE stream=? AND period=?",
                    (s["id"], period),
                ),
            }
            for s in streams
        ]
        # Equity is fetched separately using the same bounded pagination as other rows.
        column = "day" if len(period) == 10 else "month"
        operations = [
            json.loads(r[0])
            for r in db.execute(
                f"SELECT payload FROM rows WHERE stream=? AND kind='operations' AND {column}=?",
                (stream["id"], period),
            )
        ]
        return {
            **metadata,
            "stream": {**stream, "periods": periods},
            "overview": overview,
            "operation_symbols": sorted({o["symbol"] for o in operations if o.get("symbol")}),
            "operation_quality": {
                "missing": sum(o["commission"] is None for o in operations),
                "zero": sum(
                    o["commission"] is not None and number(o["commission"]) == 0 for o in operations
                ),
                "legacy": sum(o.get("commission_source", "legacy_unverified") ==
                              "legacy_unverified" for o in operations),
            },
        }


def compare(report: dict[str, Any], path: Path) -> dict[str, int]:
    """Exact Decimal-string/null/coverage parity, including every paginated row."""
    catalog = query(path, "catalog", {})
    assert catalog["generation"] == digest(report)
    counts = {"streams": 0, "periods": 0, "rows": 0}
    for stream in report["streams"]:
        counts["streams"] += 1
        for period, expected in stream["periods"].items():
            params = {"stream": stream["id"], "period": period, "generation": catalog["generation"]}
            bundle = query(path, "summary", params)
            assert bundle["stream"]["periods"][period] == expected
            for field in STREAM_FIELDS:
                assert bundle["stream"][field] == stream[field]
            for field in OPTIONAL_STREAM_FIELDS:
                if field in stream:
                    assert bundle["stream"][field] == stream[field]
            counts["periods"] += 1
            # A daily and monthly projection must both match; do not sum percentages.
            for kind in KINDS:
                expected_rows = [
                    r
                    for r in stream[kind]
                    if timestamp(r["closed_at"] if kind == "trades" else r["at"])
                    .astimezone(MSK)
                    .date()
                    .isoformat()
                    .startswith(period)
                ]
                if kind in {"signals", "operations", "orders"}:
                    expected_rows.sort(key=lambda r: timestamp(r["at"]), reverse=True)
                result: list[Any] = []
                cursor = ""
                while True:
                    page = query(path, kind, {**params, "cursor": cursor, "limit": "100"})
                    result.extend(page["items"])
                    cursor = page["next_cursor"]
                    if not cursor:
                        break
                assert result == expected_rows, (stream["strategy"], period, kind)
                counts["rows"] += len(result)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare-only", action="store_true")
    args = parser.parse_args()
    report = json.loads(args.source_report.read_bytes())
    if not args.compare_only:
        publish(report, args.output)
    print("PARITY:", encoded(compare(report, args.output)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
