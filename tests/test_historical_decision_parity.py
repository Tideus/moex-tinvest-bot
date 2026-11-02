"""Historical output is the oracle, not a second run of today's code."""

from copy import deepcopy
from datetime import timedelta

import pytest
from replay_decisions import CASES, comparison, instant, read


@pytest.mark.parametrize("kind", ["long", "intraday"])
def test_historical_parity_and_future_invariance(kind):
    source = read(CASES / f"{kind}_input.json")
    cutoff = instant(source["as_of"])
    past = [b for b in source["batches"] if instant(b["captured_at"]) <= cutoff]
    future = [b for b in source["batches"] if instant(b["captured_at"]) > cutoff]
    assert past and future
    expected, actual, extra = comparison(kind, tape=past)
    assert actual == expected
    assert actual["orders"]  # neither case can pass by merely returning no trades

    # Real later cycle, deliberately hostile future values, and a late revision
    # whose exchange timestamp is old. Delivery order must not leak into replay.
    revision = deepcopy(past[-1])
    revision["captured_at"] = (cutoff + timedelta(microseconds=1)).isoformat()
    key = "momentum" if kind == "long" else "close"
    for r in revision["rows"]:
        r[key] = "999999"
    mixed = deepcopy(past)
    not_closed = deepcopy(revision["rows"])
    for r in not_closed:
        r["observed_at"] = (cutoff + timedelta(seconds=1)).isoformat()
    mixed[-1]["rows"].extend(not_closed)
    assert comparison(kind, tape=[revision, *future, *reversed(mixed)]) == (expected, actual, extra)

    # Positive control: changing information genuinely available at the cutoff
    # must affect the decision; guards against using recorded orders as outputs.
    earlier = deepcopy(past)
    if kind == "long":
        for r in earlier[-1]["rows"]:
            r["momentum"] = "-0.90"
    else:
        for b in earlier:
            for r in b["rows"]:
                if r["secid"] == "PHOR":
                    r["close"] = "5538"
    _, changed, _ = comparison(kind, tape=earlier)
    assert changed["orders"] != expected["orders"]
