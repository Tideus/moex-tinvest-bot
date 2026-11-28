"""Offline failure sequences per news source, not inferred continuous outages."""
from collections import Counter

from moex_bot.trader_journal import timestamp


def source_history(runs, source):
    if source not in {"rss", "iss"}:
        raise ValueError("unknown_news_source")
    rows = sorted(runs, key=lambda row: timestamp(row["observed_at"]))
    if len({timestamp(r["observed_at"]) for r in rows}) != len(rows):
        raise ValueError("duplicate_probe_timestamp")
    sequences, active, statuses, prior = [], None, Counter(), None
    for row in rows:
        at = timestamp(row["observed_at"]).isoformat()
        status = row.get(source, {}).get("status", "UNAVAILABLE")
        status = status if status in {"OK", "FAIL"} else "UNAVAILABLE"
        statuses[status] += 1
        if status == "FAIL":
            if active is None:
                active = dict(first_failure_at=at, last_failure_at=at, failed_checks=0,
                              preceding_ok_at=prior, recovered_at=None,
                              left_censored=prior is None, right_censored=True)
                sequences.append(active)
            active["last_failure_at"] = at
            active["failed_checks"] += 1
        else:
            if active is not None:
                if status == "OK":
                    active.update(recovered_at=at, right_censored=False)
                else:
                    active["boundary"] = "probe_status_unknown"
                active = None
            prior = at if status == "OK" else None
    return dict(source=source, checks=dict(statuses), failure_sequences=sequences,
                continuous_outage_duration=None,
                note="Sequences of observed checks only; unobserved intervals are unknown")
