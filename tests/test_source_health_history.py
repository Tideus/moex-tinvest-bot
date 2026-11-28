import pytest
from source_health_history import source_history


def probes(statuses):
    return [dict(observed_at=f"2026-11-27T10:{i:02d}:00Z", rss={"status": s},
                 iss={"status": "OK"}) for i, s in enumerate(statuses)]


def test_iss_never_clears_rss_failures():
    rows = probes(["FAIL", "FAIL", "OK", "FAIL"])
    result = source_history(rows, "rss")
    assert result["checks"] == {"FAIL": 3, "OK": 1}
    first, last = result["failure_sequences"]
    assert first["failed_checks"] == 2 and first["left_censored"] is True
    assert first["recovered_at"] is not None and first["right_censored"] is False
    assert last["right_censored"] is True and last["preceding_ok_at"] is not None
    assert source_history(rows, "iss")["failure_sequences"] == []
    assert result["continuous_outage_duration"] is None


def test_unknown_is_not_recovery_and_cuts_sequence():
    result = source_history(probes(["OK", "FAIL", "UNAVAILABLE", "FAIL", "OK"]), "rss")
    first, second = result["failure_sequences"]
    assert first["right_censored"] is True and first["recovered_at"] is None
    assert first["boundary"] == "probe_status_unknown"
    assert second["left_censored"] is True


def test_probe_order_and_invalid_dates():
    rows = probes(["OK", "FAIL", "OK"])
    assert source_history(rows, "rss") == source_history(list(reversed(rows)), "rss")
    with pytest.raises(ValueError, match="duplicate"):
        source_history(rows + [rows[0]], "rss")
    with pytest.raises(ValueError):
        source_history([dict(observed_at="2026-11-27T10:00:00")], "rss")
    with pytest.raises(ValueError, match="unknown"):
        source_history(rows, "other")
