from collections import defaultdict
from datetime import datetime, timedelta

import iis_retry as r


def test_no_future_daily_close_in_morning_ranking():
    day = datetime(2026, 10, 1)
    bars = [{"end": str(day - timedelta(days=80-i)), "close": 100+i} for i in range(80)]
    assert r.ranking({"T": bars}, str(day.date())) == ["T"]
    assert r.ranking({"T": bars + [{"end": str(day), "close": 1}]}, str(day.date())) == ["T"]


def test_missing_higher_or_lower_rank_never_permits_incomplete_window(monkeypatch):
    at = datetime(2026, 10, 1, 14, 10, tzinfo=r.MOSCOW)
    flow = defaultdict(dict, {"T": {"2026-10-01": {"ok": True}}})
    monkeypatch.setattr(r, "evaluate", lambda *_: {"state": "EVALUATED", "spread_bps": 1,
                        "variants": {"current": {"state": "RESEARCH_ELIGIBLE"}}})
    assert r.select(["T"], flow, at) == ("signal", ("T", 1))
    assert r.select(["T", "MOEX"], flow, at) == ("missing", None)
