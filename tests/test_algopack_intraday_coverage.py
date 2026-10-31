import io
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from moex_bot.integrations import algopack_intraday as module
from moex_bot.integrations.algopack_intraday import AlgoPackIntradayAdapter, _decimal, _normalize


@pytest.mark.parametrize(
    "kwargs",
    [
        {"token": ""},
        {"timeout": 0},
        {"retry_attempts": 0},
        {"retry_attempts": 6},
        {"retry_backoff": -1},
    ],
)
def test_intraday_client_configuration(kwargs):
    with pytest.raises(ValueError):
        AlgoPackIntradayAdapter(**(dict(token="test", transport=None) | kwargs))


def test_intraday_environment_and_native_transport(monkeypatch):
    monkeypatch.setenv("MOEX_APIKEY", "test")
    assert AlgoPackIntradayAdapter.from_environment().token == "test"
    monkeypatch.setattr(module, "urlopen", lambda *a, **k: io.BytesIO(b'{"data":{}}'))
    assert module.UrlLibJsonGetTransport().get(
        "https://apim.moex.com/test", headers={}, timeout=1
    ) == {"data": {}}


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"data": []},
        {"data": {"columns": "bad", "data": []}},
        {"data": {"columns": [], "data": "bad"}},
        {"data": {"columns": ["a"], "data": [[]]}},
        {"data": {"columns": [], "data": [False]}},
    ],
)
def test_intraday_payload_schema(payload):
    with pytest.raises(ValueError):
        _normalize(payload)


def test_intraday_decimal_must_be_finite():
    with pytest.raises(ValueError, match="finite"):
        _decimal({"value": "Infinity"}, "value")
    assert _decimal({"value": "1.25"}, "value") == Decimal("1.25")


def test_intraday_empty_universe_and_naive_time():
    adapter = AlgoPackIntradayAdapter("test", None)
    assert adapter.latest(secids=(), as_of=datetime.now(UTC)) == ()
    with pytest.raises(ValueError):
        adapter.latest(secids=["SBER"], as_of=datetime(2026, 1, 1))


@pytest.mark.parametrize(
    "error", [HTTPError("https://test", 503, "busy", {}, None), URLError("offline"), TimeoutError()]
)
def test_transient_read_final_attempt_is_not_retried(error):
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise error

    adapter = AlgoPackIntradayAdapter("test", SimpleNamespace(get=fail), retry_attempts=1)
    with pytest.raises(type(error)):
        adapter._get_with_retry("https://test")
    assert len(calls) == 1


def test_non_retryable_http_error_and_retry_state_guard():
    def fail(*args, **kwargs):
        raise HTTPError("https://test", 401, "denied", {}, None)

    adapter = AlgoPackIntradayAdapter("test", SimpleNamespace(get=fail), sleeper=lambda x: None)
    with pytest.raises(HTTPError):
        adapter._get_with_retry("https://test")


def test_retryable_http_failure_uses_bounded_backoff():
    calls = []
    sleeps = []

    def get(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise HTTPError("https://test", 503, "busy", {}, None)
        return {"data": {}}

    adapter = AlgoPackIntradayAdapter(
        "test", SimpleNamespace(get=get), retry_attempts=2, retry_backoff=0.5, sleeper=sleeps.append
    )
    assert adapter._get_with_retry("https://test") == {"data": {}}
    assert len(calls) == 2 and sleeps == [0.5]
    adapter.retry_attempts = 0
    with pytest.raises(RuntimeError, match="retry state"):
        adapter._get_with_retry("https://test")


def test_pagination_skips_unwanted_rows_and_enforces_bound():
    body = {
        "data": {
            "columns": ["secid", "tradedate", "tradetime"],
            "data": [["OTHER", "2026-10-30", "10:00:00"]] * 100001,
        }
    }
    adapter = AlgoPackIntradayAdapter("test", SimpleNamespace(get=lambda *a, **k: body))
    with pytest.raises(ValueError, match="safety limit"):
        adapter._day_rows("tradestats", "2026-10-30", frozenset({"SBER"}))


def test_future_common_window_is_not_used(monkeypatch):
    adapter = AlgoPackIntradayAdapter("test", None)
    future = datetime(2027, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(adapter, "_day_rows", lambda *a: {("SBER", future): {}})
    assert not adapter.latest(secids=["SBER"], as_of=datetime(2026, 1, 1, tzinfo=UTC))


@pytest.mark.parametrize("newest_first", [True, False])
def test_latest_common_bar_does_not_depend_on_set_iteration(monkeypatch, newest_first):
    class OrderedSet(set):
        def __and__(self, other):
            return OrderedSet(super().__and__(other))

        def __iter__(self):
            return iter(sorted(super().__iter__(), reverse=newest_first))

    # Mathematical set semantics stay unchanged; exercise both valid traversal orders.
    monkeypatch.setitem(module.__dict__, "set", OrderedSet)
    adapter = AlgoPackIntradayAdapter("test", None)
    earlier = datetime(2026, 10, 30, 10, tzinfo=UTC)
    latest = datetime(2026, 10, 30, 11, tzinfo=UTC)
    rows = {}
    for moment, price in ((earlier, 100), (latest, 110)):
        rows[("SBER", moment)] = dict(
            pr_close=price,
            pr_vwap=price,
            val_b=1,
            val_s=1,
            put_val_b=1,
            put_val_s=1,
            cancel_val_b=0,
            cancel_val_s=0,
            spread_bbo=1,
            imbalance_val=0,
        )
    monkeypatch.setattr(adapter, "_day_rows", lambda *a: rows)
    bars = adapter.latest(secids=["SBER"], as_of=datetime(2026, 10, 30, 12, tzinfo=UTC))
    assert len(bars) == 1
    assert bars[0].observed_at == latest
    assert bars[0].close == 110
