import json
import os
import sqlite3
from copy import deepcopy

import pytest
from test_trader_journal import PROFILE, SPEC, T0, events, point

from moex_bot import journal_import as ji
from moex_bot.trader_journal import Journal, build_report


@pytest.fixture
def case(tmp_path):
    (tmp_path / "universe.json").write_text(json.dumps([{"secid": "SBER", **SPEC["SBER"]}]))
    (tmp_path / "portfolio.json").write_text(json.dumps(point()))
    config = {"artifact_root": str(tmp_path), "universe": str(tmp_path / "universe.json"),
              "profiles": [{**PROFILE, "portfolio_globs": ["portfolio.json"]}]}
    return config, Journal(tmp_path / "journal.sqlite3"), tmp_path


def test_cached_import_equals_full_and_skips_parsing(case, monkeypatch):
    cfg, journal, root = case
    assert ji.import_artifacts(cfg, journal)["inserted_revisions"] == 1
    expected = journal.latest()
    report = build_report(expected)
    original = ji.portfolio_events
    monkeypatch.setattr(ji, "portfolio_events", lambda *args: pytest.fail("unchanged input parsed"))
    assert ji.import_artifacts(cfg, journal)["inserted_revisions"] == 0
    monkeypatch.setattr(ji, "portfolio_events", original)
    assert ji.import_artifacts(cfg | {"import_cache": False}, journal)["errors"] == []
    assert journal.latest() == expected
    actual = build_report(journal.latest())
    actual.pop("generated_at")
    report.pop("generated_at")
    assert actual == report
    # Restoring a fresh analytical database does not reuse another DB's cache.
    restored = Journal(root / "fresh.sqlite3")
    assert ji.import_artifacts(cfg, restored)["inserted_revisions"] == 1


def test_same_size_mtime_changed_bytes_are_not_skipped_or_rewritten(case):
    cfg, journal, root = case
    ji.import_artifacts(cfg, journal)
    original = journal.latest()
    path = root / "portfolio.json"
    stamp = path.stat()
    path.write_text(path.read_text().replace('10000', '10001'))
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert path.stat().st_size == stamp.st_size
    assert ji.import_artifacts(cfg, journal)["errors"]
    assert ji.import_artifacts(cfg, journal)["errors"]  # Failed import never cached.
    assert journal.latest() == original


def test_late_plan_is_imported_and_later_conflict_is_rejected(case):
    cfg, journal, root = case
    ji.import_artifacts(cfg, journal)
    plan = {"run_id": "shadow-" + T0, "flow_diagnostics": [
        {"secid": "SBER", "passed": True, "reasons": []}]}
    path = root / "plan.json"
    path.write_text(json.dumps(plan))
    assert ji.import_artifacts(cfg, journal)["inserted_revisions"] == 1
    assert len(journal.latest()) == 2
    plan["flow_diagnostics"][0]["passed"] = False
    path.write_text(json.dumps(plan))
    assert ji.import_artifacts(cfg, journal)["errors"]
    assert next(r for r in journal.latest() if r["kind"] == "signal")["status"] == "filter_passed"


def test_profile_metadata_changes_invalidate_cache(case):
    cfg, journal, root = case
    ji.import_artifacts(cfg, journal)
    changed = deepcopy(cfg)
    changed["profiles"][0]["environment"] = "prod"
    assert ji.import_artifacts(changed, journal)["errors"]
    (root / "universe.json").write_text("null")
    with pytest.raises(TypeError):
        ji.import_artifacts(cfg, journal)


def test_cache_and_events_commit_atomically_and_clear_on_source_mutation(tmp_path):
    journal = Journal(tmp_path / "j.sqlite3")
    first = events([])
    journal.ingest(first, source_key="first", omitted_orders=2)
    assert journal.source_imported("first") == 2
    conflicting = deepcopy(first)
    conflicting[0]["equity"] = "999"
    with pytest.raises(ValueError, match="conflicting"):
        journal.ingest(conflicting, source_key="bad")
    assert journal.source_imported("bad") is None
    with sqlite3.connect(journal.path) as db:
        db.execute("UPDATE events SET checksum=checksum")
    assert journal.source_imported("first") is None
    journal.ingest(first, source_key="again")
    with sqlite3.connect(journal.path) as db:
        db.execute("DELETE FROM events")
    assert journal.source_imported("again") is None


def test_omitted_order_count_is_identical_on_cached_import(case):
    cfg, journal, root = case
    (root / "portfolio.json").rename(root / "intraday-portfolio-test.json")
    cfg["profiles"][0]["portfolio_globs"] = ["intraday-portfolio-*.json"]
    (root / "intraday-execution-test.json").write_text(json.dumps({"submitted": [{}]}))
    first = ji.import_artifacts(cfg, journal)
    cached = ji.import_artifacts(cfg, journal)
    assert first["unknown_order_times"] == cached["unknown_order_times"] == 1
    assert first["diagnostics"] == cached["diagnostics"] and not cached["errors"]
