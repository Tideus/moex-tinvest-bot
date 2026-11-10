"""Independent news evidence. Never imported by signals, geo risk or execution.

ISS is all site news; RSS category 122 is a subset, not an interchangeable feed.
First discovery delay includes polling/network delay and is not publisher latency.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from time import monotonic
from typing import Any
from urllib.request import Request, urlopen
from uuid import uuid4
from xml.etree.ElementTree import ParseError, fromstring
from zoneinfo import ZoneInfo

from .geo_feed import FeedItem, GeoSource, parse_feed
from .probe_errors import failure_kind

ISS = "https://iss.moex.com/iss/sitenews.json?iss.meta=off&lang=ru"
RSS = "https://www.moex.com/export/news.aspx?cat=122"
Fetch = Callable[[str, float], bytes]
Clock = Callable[[], datetime]


def load_settings(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("mode") != "observation_only" or (
        raw.get("execution_allowed") is not False
    ):
        raise ValueError("news observer cannot influence execution")
    for key, maximum in (("lookback_hours", 168), ("max_pages", 100),
                         ("timeout_seconds", 30), ("budget_seconds", 120)):
        if type(raw.get(key)) is not int or not 1 <= raw[key] <= maximum:
            raise ValueError(f"invalid news observer setting: {key}")
    return raw


def fetch_public(url: str, timeout: float) -> bytes:
    if url != RSS and not re.fullmatch(re.escape(ISS) + r"&start=[0-9]+", url):
        raise ValueError("news observer endpoint is not allowlisted")
    request = Request(url, headers={"User-Agent": "moex-news-observer/1.0"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        content = response.read(4_000_001)
    if not isinstance(content, bytes) or len(content) > 4_000_000:
        raise ValueError("news response exceeds limit")
    return content


def publication_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("news timestamp must be a string")
    result = datetime.fromisoformat(value)
    # Verified against RSS +0300 for matching IDs; never use the server's local TZ.
    if result.tzinfo is None:
        result = result.replace(tzinfo=ZoneInfo("Europe/Moscow"))
    return result.astimezone(UTC)


def parse_iss(content: bytes) -> list[dict[str, Any]]:
    raw = json.loads(content)
    block = raw.get("sitenews") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        raise ValueError("ISS sitenews block missing")
    columns, rows = block.get("columns"), block.get("data")
    if (not isinstance(columns, list) or not all(isinstance(c, str) for c in columns)
            or len(columns) != len(set(columns))
            or not {"id", "title", "published_at"} <= set(columns)
            or not isinstance(rows, list)):
        raise ValueError("invalid ISS news schema")
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError("invalid ISS news row")
        item = dict(zip(columns, row, strict=True))
        if type(item["id"]) is not int or item["id"] <= 0 or (
            not isinstance(item["title"], str) or not item["title"].strip()
        ):
            raise ValueError("invalid news identity/title")
        item["published_at"] = publication_time(item["published_at"]).isoformat()
        result.append(item)
    return result


def collect_iss(settings: dict[str, Any], cutoff: datetime, *, fetch: Fetch,
                clock: Clock, deadline: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: dict[int, dict[str, Any]] = {}
    pages = []
    offset = 0
    previous_oldest: datetime | None = None
    for _ in range(settings["max_pages"]):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("news collection budget exhausted")
        content = fetch(ISS + f"&start={offset}", min(settings["timeout_seconds"], remaining))
        received = clock()
        rows = parse_iss(content)
        times = [publication_time(row["published_at"]) for row in rows]
        if times != sorted(times, reverse=True) or any(t > received for t in times):
            raise ValueError("ISS news ordering/future timestamp")
        if previous_oldest is not None and times and times[0] > previous_oldest:
            raise ValueError("ISS news pagination shifted; retry next observation")
        pages.append({"start": offset, "rows": len(rows), "received_at": received.isoformat(),
                      "sha256": hashlib.sha256(content).hexdigest()})
        for row in rows:
            if row["id"] in entries:
                raise ValueError("ISS pagination repeats news identity")
            entries[row["id"]] = row
        # Coverage means a bounded historical window was fully traversed, not that
        # every possible MOEX publication is proven present in this API.
        if not rows or times[-1] < cutoff:
            return ([r for r in entries.values() if publication_time(r["published_at"]) >= cutoff],
                    pages)
        previous_oldest = times[-1]
        offset += len(rows)
    raise ValueError("ISS news page limit; window coverage incomplete")


def parse_reference(content: bytes) -> tuple[FeedItem, ...]:
    items = parse_feed(GeoSource("moex_risk", RSS, "primary"), content)
    root = fromstring(content)
    nodes = root.findall(".//item") or root.findall(".//{*}entry")
    if len(items) != len(nodes) or len({item.link for item in items}) != len(items):
        raise ValueError("incomplete or duplicate RSS reference entries")
    return items


def compare_rss(content: bytes, news: list[dict[str, Any]], cutoff: datetime,
                received: datetime) -> dict[str, Any]:
    items = parse_reference(content)
    by_id = {row["id"]: row for row in news}
    compared = 0
    missing, different, reference = [], [], []
    for item in items:
        if not cutoff <= item.published_at <= received:
            continue
        match = re.fullmatch(r"https://(?:www\.)?moex\.com/n([0-9]+)(?:\?.*)?", item.link)
        if not match:
            raise ValueError("RSS news has no comparable MOEX identity")
        identity = int(match[1])
        reference.append({"id": identity, "title": item.title,
                          "published_at": item.published_at.isoformat()})
        compared += 1
        row = by_id.get(identity)
        if row is None:
            missing.append(identity)
        elif (row["title"] != item.title
              or publication_time(row["published_at"]) != item.published_at):
            different.append(identity)
    return {"status": "NO_REFERENCE_ITEMS" if compared == 0 else (
                "MATCH" if not missing and not different else "DIFFERENCES"),
            "reference_items": compared, "missing_ids": missing, "different_ids": different,
            "reference_received_at": received.isoformat(),
            "reference_sha256": hashlib.sha256(content).hexdigest(),
            "reference_records": reference,
            "scope": "RSS risk category subset in observation window; not all site news"}


def observe(state: Path, settings: dict[str, Any], *, fetch: Fetch = fetch_public,
            clock: Clock = lambda: datetime.now(UTC)) -> dict[str, Any]:
    started = clock()
    if started.tzinfo is None:
        raise ValueError("observer clock must be timezone-aware")
    cutoff = started - timedelta(hours=settings["lookback_hours"])
    report: dict[str, Any] = {
        "protocol": "moex-news-observation-v1", "execution_allowed": False,
        "started_at": started.isoformat(), "window_from": cutoff.isoformat(),
        "iss": {"status": "UNAVAILABLE"}, "rss": {"status": "UNAVAILABLE"},
        "comparison": {"status": "UNAVAILABLE"}, "news": [],
    }
    deadline = monotonic() + settings["budget_seconds"]
    news: list[dict[str, Any]] = []
    try:
        news, pages = collect_iss(settings, cutoff, fetch=fetch, clock=clock, deadline=deadline)
        report["iss"] = {"status": "OK", "window_covered": True, "pages": pages,
                         "items": len(news), "received_at": clock().isoformat()}
        report["news"] = news
    except (OSError, ValueError) as exc:
        report["iss"] = {"status": "FAIL", "reason": failure_kind(exc),
                         "window_covered": False}
    try:
        # RSS is independent: success cannot recover ISS and vice versa.
        content = fetch(RSS, settings["timeout_seconds"])
        received = clock()
        parse_reference(content)
        report["rss"] = {"status": "OK", "received_at": received.isoformat()}
        if report["iss"]["status"] == "OK":
            report["comparison"] = compare_rss(content, news, cutoff, received)
    except (OSError, ValueError, ParseError) as exc:
        report["rss"] = {"status": "FAIL", "reason": failure_kind(exc)}
    report["observed_at"] = clock().isoformat()
    state.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(state / "news.sqlite3", timeout=10)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS news "
                   "(id INTEGER PRIMARY KEY, first_seen_at TEXT NOT NULL, "
                   "baseline INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        anchor = db.execute("SELECT value FROM meta WHERE key='first_success'").fetchone()
        delays = []
        baseline, backfill = 0, 0
        if report["iss"]["status"] == "OK":
            received = publication_time(report["iss"]["received_at"])
            for item in news:
                seen = db.execute("SELECT first_seen_at,baseline FROM news WHERE id=?",
                                  (item["id"],)).fetchone()
                if seen is None:
                    is_baseline = anchor is None
                    db.execute("INSERT INTO news VALUES(?,?,?)",
                               (item["id"], received.isoformat(), int(is_baseline)))
                    published = publication_time(item["published_at"])
                    if is_baseline:
                        baseline += 1
                    elif published >= publication_time(anchor[0]):
                        delays.append((received - published).total_seconds())
                    else:
                        backfill += 1
                    seen = (received.isoformat(), int(is_baseline))
                item["first_seen_at"], item["baseline"] = seen[0], bool(seen[1])
            if anchor is None:
                db.execute("INSERT INTO meta VALUES('first_success',?)", (received.isoformat(),))
        report["discovery"] = {
            "baseline_new": baseline, "historical_backfill_new": backfill,
            "forward_new": len(delays), "delays_seconds": delays,
            "median_seconds": median(delays) if delays else None,
            "max_seconds": max(delays) if delays else None,
            "meaning": ("first discovery minus publication; includes polling/outages; "
                        "not API latency"),
        }
        identity = uuid4().hex
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        db.execute("INSERT INTO runs VALUES(?,?)", (identity, encoded))
    temporary = state / f".{identity}.tmp"
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(state / "latest.json")
    return report


def evidence(state: Path, now: datetime) -> dict[str, str]:
    try:
        raw = json.loads((state / "latest.json").read_text(encoding="utf-8"))
        age = (now - publication_time(raw["observed_at"])).total_seconds()
        valid = (raw["protocol"] == "moex-news-observation-v1"
                 and raw["execution_allowed"] is False and -30 <= age <= 900)
        if not valid:
            raise ValueError("news observer stale or invalid")
        status = raw["iss"]["status"]
        reason = raw["iss"].get("reason", "unknown")
        allowed = {"timeout", "dns", "tls", "connection", "schema", "http", "unknown"}
        return {
            "data:news-observer": "OK",
            "api:news_iss_observer": "OK" if (
                status == "OK" and raw["iss"].get("window_covered") is True
            ) else (
                f"FAIL: news observer {reason if reason in allowed else 'unknown'}"),
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {"data:news-observer": "FAIL: news observer stale or missing",
                "api:news_iss_observer": "FAIL: news observer stale or missing"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/news_observer.json"))
    parser.add_argument("--state", type=Path, default=Path("/var/lib/moex-news-observer"))
    args = parser.parse_args()
    try:
        result = observe(args.state, load_settings(args.config))
    except Exception as exc:
        print(f"FAIL: news observer {failure_kind(exc)}; no trading effects")
        return 2
    print(json.dumps({k: result[k] for k in ("observed_at", "iss", "rss", "comparison",
                                           "discovery", "execution_allowed")}, ensure_ascii=False))
    return 0 if result["iss"]["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
