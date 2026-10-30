import json
from datetime import UTC, datetime, timedelta

import pytest

from moex_bot.event_calendar import EventStore, refresh, stamp

NOW = datetime(2026, 10, 30, tzinfo=UTC)


def test_timezone_required():
    with pytest.raises(ValueError):
        stamp(datetime(2026, 10, 30))


def test_dedup_revisions_and_stale(tmp_path):
    store = EventStore(tmp_path / "events.db")
    row = {"reportDate": "2026-11-01T00:00:00Z", "periodYear": 2026, "periodNum": 3}
    for _ in range(2):
        store.record("report", "uid", "T", {"events": [row]}, NOW)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM event_revisions").fetchone()[0] == 1
        db.execute("INSERT INTO refreshes VALUES(?,0)", (stamp(NOW),))
    assert "· OK" in store.status(NOW)
    assert "T · report" in store.status(NOW)
    assert "STALE" in store.status(NOW + timedelta(days=2))
    row["periodType"] = "PERIOD_TYPE_QUARTER"
    store.record("report", "uid", "T", {"events": [row]}, NOW)


def test_invalid_batch_rolls_back(tmp_path):
    store = EventStore(tmp_path / "events.db")
    with pytest.raises(ValueError):
        store.record("report", "uid", "T", {"events": [{}]}, NOW)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0


def test_empty_not_proof_of_no_events(tmp_path):
    store = EventStore(tmp_path / "events.db")
    assert "не гарантирует" in store.status(NOW)
    assert "STALE" in store.status(NOW)


def test_conflicting_dates_do_not_overwrite_current_event(tmp_path):
    store = EventStore(tmp_path / "events.db")
    first = {"reportDate": "2026-11-01T00:00:00Z", "periodYear": 2026,
             "periodNum": 3, "periodType": "PERIOD_TYPE_QUARTER"}
    second = {**first, "reportDate": "2026-11-20T00:00:00Z"}
    store.record("report", "uid", "T", {"events": [first]}, NOW)
    with pytest.raises(ValueError, match="ambiguous"):
        store.record("report", "uid", "T", {"events": [first, second]}, NOW)
    assert "2026-11-01" in store.status(NOW)


def test_today_and_rescheduled_report(tmp_path):
    store = EventStore(tmp_path / "events.db")
    row = {"reportDate": "2026-10-30T00:00:00Z", "periodYear": 2026,
           "periodNum": 3, "periodType": "PERIOD_TYPE_QUARTER"}
    store.record("report", "uid", "T", {"events": [row]}, NOW)
    assert "T · report" in store.status(NOW + timedelta(hours=12))
    row["reportDate"] = "2026-11-01T00:00:00Z"
    store.record("report", "uid", "T", {"events": [row]}, NOW + timedelta(hours=1))
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM event_revisions").fetchone()[0] == 2
    with pytest.raises(ValueError):
        store.record("report", "uid", "T", {"error": "unexpected"}, NOW)


def test_refresh_contract_and_partial_failure(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    universe = tmp_path / "universe.json"
    config.write_text(json.dumps({"mode": "observe", "token_env": "CALENDAR_TEST_TOKEN",
                                  "horizon_days": 90, "exchange": "MOEX"}))
    universe.write_text(json.dumps([{"secid": "T", "t_invest_uid": "uid"}]))
    database = tmp_path / "events.db"
    with pytest.raises(ValueError, match="token"):
        refresh(config, universe, database, NOW)
    monkeypatch.setenv("CALENDAR_TEST_TOKEN", "test-only")

    class Fake:
        def post(self, url, *, headers, payload, timeout_seconds):
            assert headers["Authorization"] == "Bearer test-only"
            assert timeout_seconds == 20
            assert "InstrumentsService/" in url
            assert "Order" not in url
            if url.endswith("GetAssetReports"):
                assert payload["instrumentId"] == "uid"
                return {"events": []}
            if url.endswith("GetDividends"):
                raise RuntimeError("failure")
            assert url.endswith("TradingSchedules")
            return {"exchanges": []}

    assert refresh(config, universe, database, NOW, Fake()) == 2
    assert "INCOMPLETE" in EventStore(database).status(NOW)
