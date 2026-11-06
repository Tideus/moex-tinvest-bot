"""Broken reads may repeat; uncertain order writes must never repeat."""

import io
import json
from datetime import datetime
from http.client import IncompleteRead, RemoteDisconnected
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

import pytest
from test_intraday import PROJECT_ROOT, _instrument, _portfolio
from test_tinvest_sandbox import _intent

from moex_bot import cli
from moex_bot.domain import OrderStatus
from moex_bot.integrations import tinvest_sandbox as broker
from moex_bot.integrations.algopack_intraday import AlgoPackIntradayAdapter, AlgoPackReadError
from moex_bot.monitoring_labels import intraday_read_failure, safe_reason


@pytest.mark.parametrize("fault", [
    IncompleteRead(b"SECRET", 9), RemoteDisconnected(), TimeoutError()])
def test_algopack_recovers_or_produces_safe_dataset_evidence(fault):
    transport = SimpleNamespace(get=Mock(
        side_effect=[fault, {"data": {"columns": [], "data": []}}]))
    adapter = AlgoPackIntradayAdapter("SECRET", transport, sleeper=lambda _: None)
    assert adapter._day_rows("obstats", "2026-11-06", frozenset({"SBER"})) == {}
    assert transport.get.call_count == 2
    transport.get = Mock(side_effect=fault)
    with pytest.raises(AlgoPackReadError, match="api=ALGOPACK/shared stage=obstats") as caught:
        adapter._day_rows("obstats", "2026-11-06", frozenset({"SBER"}))
    assert transport.get.call_count == 3
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("host", [
    "sandbox-invest-public-api.tbank.ru", "invest-public-api.tbank.ru"])
@pytest.mark.parametrize("fault", [IncompleteRead(b"SECRET", 9), TimeoutError(),
                                  URLError(TimeoutError()), RemoteDisconnected()])
def test_broker_safe_read_retries_and_reports_contour(monkeypatch, host, fault):
    opening = Mock(side_effect=[fault, io.BytesIO(b'{}')])
    monkeypatch.setattr(broker, "urlopen", opening)
    monkeypatch.setattr(broker.time, "sleep", lambda _: None)
    url = "https://" + host + "/rest" + broker.GET_ORDERS_PATH
    def call():
        return broker.UrlLibJsonTransport().post(
            url, headers={"Authorization": "Bearer SECRET"}, payload={"accountId": "PRIVATE"},
            timeout_seconds=1)
    assert call() == {}
    assert opening.call_count == 2
    opening.side_effect = fault
    opening.reset_mock()
    with pytest.raises(URLError) as caught:
        call()
    message = str(caught.value)
    assert opening.call_count == 3
    assert ("T-Invest/sandbox" if host.startswith("sandbox") else "T-Invest/prod") in message
    assert "stage=GetOrders" in message and "attempts=3" in message
    assert "SECRET" not in message and "PRIVATE" not in message


@pytest.mark.parametrize("path", [broker.POST_ORDER_PATH, broker.CANCEL_SANDBOX_ORDER_PATH,
                                  broker.OPEN_SANDBOX_ACCOUNT_PATH, broker.SANDBOX_PAY_IN_PATH,
                                  broker.SANDBOX_SERVICE_PATH + "/PostSandboxStopOrder",
                                  broker.SANDBOX_SERVICE_PATH + "/GetUnknown"])
@pytest.mark.parametrize("fault", [TimeoutError(), IncompleteRead(b"SECRET", 9)])
def test_mutations_and_unknown_methods_never_retry(monkeypatch, path, fault):
    opening = Mock(side_effect=fault)
    monkeypatch.setattr(broker, "urlopen", opening)
    with pytest.raises((TimeoutError, URLError)):
        broker.UrlLibJsonTransport().post(broker.SANDBOX_BASE_URL + path,
                                         headers={}, payload={}, timeout_seconds=1)
    assert opening.call_count == 1


@pytest.mark.parametrize("code", [401, 403, 400])
def test_read_auth_and_validation_errors_are_not_retried(monkeypatch, code):
    opening = Mock(side_effect=HTTPError("https://SECRET", code, "SECRET", {}, None))
    monkeypatch.setattr(broker, "urlopen", opening)
    with pytest.raises(URLError) as caught:
        broker.UrlLibJsonTransport().post(broker.SANDBOX_BASE_URL + broker.GET_ORDERS_PATH,
                                         headers={}, payload={}, timeout_seconds=1)
    assert opening.call_count == 1
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("has_position", [False, True])
def test_exhausted_algopack_does_not_block_broker_based_exit(tmp_path, monkeypatch, capsys,
                                                          has_position):
    adapter = AlgoPackIntradayAdapter("SECRET", SimpleNamespace(
        get=Mock(side_effect=IncompleteRead(b"SECRET", 12))), sleeper=lambda _: None)
    monkeypatch.setattr(cli.AlgoPackIntradayAdapter, "from_environment", lambda: adapter)
    monkeypatch.setattr(cli, "_verified_instruments_by_secid", lambda _: {"SBER": _instrument()})
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(json.dumps(_portfolio(
        positions={"SBER": {"lots": 10, "current_value": "980"}} if has_position else {})))
    output = tmp_path / "plan.json"
    result = cli.intraday_plan(config_path=PROJECT_ROOT / "config/intraday.json",
                              universe_path=PROJECT_ROOT / "config/universe.json",
                              portfolio_path=portfolio, state_path=tmp_path / "state.db",
                              output_path=output, as_of=datetime(2026, 8, 20, 11, 0,
                                                                tzinfo=ZoneInfo("Europe/Moscow")),
                              outbox_path=None)
    plan = json.loads(output.read_text())
    assert "stage=tradestats" in plan["analysis_input"]["read_failure"]
    assert "SECRET" not in capsys.readouterr().out
    if has_position:
        assert result == 0 and plan["phase"] == "position_exit"
        assert len(plan["orders"]) == 1 and plan["orders"][0]["intent"]["side"] == "sell"
    else:
        assert result == 2 and plan["orders"] == []
    alert = safe_reason(intraday_read_failure(plan["analysis_input"]["read_failure"]))
    assert "INTRADAY SANDBOX" in alert and "ALGOPACK/tradestats" in alert
    assert "IncompleteRead" in alert and "не подтверждение текущей недоступности" in alert


@pytest.mark.parametrize("value", [None, {}, "SECRET", "api=ALGOPACK/shared stage=SECRET"])
def test_monitor_rejects_arbitrary_failure_text(value):
    assert intraday_read_failure(value) is None


def test_incomplete_order_response_is_unknown_not_a_second_purchase(monkeypatch):
    opening = Mock(side_effect=IncompleteRead(b"SECRET", 9))
    monkeypatch.setattr(broker, "urlopen", opening)
    adapter = broker.TInvestSandboxExecutionAdapter(
        "token", "account", broker.UrlLibJsonTransport())
    record = adapter.submit(_intent())
    assert record.status is OrderStatus.UNKNOWN
    assert opening.call_count == 1


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_transient_http_read_recovers(monkeypatch, code):
    opening = Mock(side_effect=[
        HTTPError("https://test", code, "busy", {}, None), io.BytesIO(b'{}')])
    monkeypatch.setattr(broker, "urlopen", opening)
    monkeypatch.setattr(broker.time, "sleep", lambda _: None)
    assert broker.UrlLibJsonTransport().post(broker.SANDBOX_BASE_URL + broker.GET_ORDERS_PATH,
                                           headers={}, payload={}, timeout_seconds=1) == {}
    assert opening.call_count == 2
