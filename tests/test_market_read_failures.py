import json
import socket
import ssl
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from moex_bot import cli
from moex_bot.integrations.moexalgo_data import MoexAlgoReadOnlyAdapter
from moex_bot.integrations.read_failure import MarketDataReadError, read_moex
from moex_bot.notifications import SQLiteOutbox, deliver_pending

httpx = pytest.importorskip("httpx")
NOW = datetime(2026, 11, 3, 15, 5, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]
URL = "https://user:SECRET@apim.moex.com/iss/securities/SBER.json?token=SECRET"


def fail(kind=httpx.ConnectError, cause=None, url=URL):
    exc = kind("raw SECRET", request=httpx.Request("GET", url))
    if cause:
        exc.__cause__ = cause
    return exc


@pytest.mark.parametrize("kind", [httpx.ConnectError, httpx.ConnectTimeout])
def test_connect_failure_retry_is_bounded_and_success_returns_original_result(kind):
    action = Mock(side_effect=[fail(kind), fail(kind), "result"])
    sleep = Mock()
    assert read_moex(action, stage="candles", secid="SBER", sleeper=sleep) == "result"
    assert action.call_count == 3
    assert [c.args[0] for c in sleep.call_args_list] == [0.25, 0.5]


def test_exhaustion_records_api_not_credentials_and_keeps_dns_cause():
    action = Mock(side_effect=fail(cause=socket.gaierror(-3, "SECRET")))
    with pytest.raises(MarketDataReadError) as got:
        read_moex(action, stage="ticker", secid="SBER", sleeper=lambda _: None)
    assert action.call_count == 3
    data = got.value.evidence
    assert data["host"] == "apim.moex.com" and data["path"] == "/iss/securities/SBER.json"
    assert data["category"] == "dns" and data["attempts"] == 3
    assert "SECRET" not in str(got.value) and "user:" not in str(got.value)


@pytest.mark.parametrize("cause", [ssl.SSLError("SECRET"), None])
def test_tls_never_retried(cause):
    exc = fail(cause=cause)
    if cause is None:
        exc.args = ("CERTIFICATE_VERIFY_FAILED SECRET",)
    action = Mock(side_effect=exc)
    with pytest.raises(MarketDataReadError) as got:
        read_moex(action, stage="ticker", secid="SBER", sleeper=Mock())
    assert action.call_count == 1 and got.value.evidence["category"] == "tls"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_http_responses_not_blindly_retried(status):
    request = httpx.Request("GET", URL)
    action = Mock(
        side_effect=httpx.HTTPStatusError(
            "SECRET",
            request=request,
            response=httpx.Response(status, request=request),
        )
    )
    with pytest.raises(MarketDataReadError) as got:
        read_moex(action, stage="metadata", secid="SBER", sleeper=Mock())
    assert action.call_count == 1 and got.value.evidence["http_status"] == status


@pytest.mark.parametrize("url", ["https://evil.test/SECRET", "https://apim.moex.com/SECRET"])
def test_untrusted_endpoint_paths_are_withheld(url):
    with pytest.raises(MarketDataReadError) as got:
        read_moex(
            Mock(side_effect=fail(httpx.ReadError, url=url)),
            stage="candles",
            secid="SECRET/invalid",
            sleeper=Mock(),
        )
    assert got.value.evidence["path"] == "withheld"
    assert "SECRET" not in str(got.value)


def test_programming_or_data_error_is_not_retried_or_relabelled():
    action = Mock(side_effect=ValueError("invalid candles"))
    with pytest.raises(ValueError, match="invalid candles"):
        read_moex(action, stage="candles", secid="SBER", sleeper=Mock())
    assert action.call_count == 1


@pytest.mark.parametrize("stage", ["ticker", "metadata", "candles"])
def test_adapter_identifies_exact_read_boundary(stage):
    ticker = Mock()
    factory = Mock(return_value=ticker)
    metadata = Mock(return_value={"lotsize": 10, "minstep": "0.01"})
    target = {"ticker": factory, "metadata": metadata, "candles": ticker.candles}[stage]
    target.side_effect = fail(httpx.ReadTimeout)
    with pytest.raises(MarketDataReadError) as got:
        MoexAlgoReadOnlyAdapter(factory, metadata).hourly_observation(
            secid="SBER",
            uid="test",
            board="TQBR",
            as_of=NOW,
        )
    assert got.value.evidence["stage"] == stage
    assert target.call_count == 1


@pytest.mark.parametrize("environment", ["SANDBOX", "PROD OBSERVER"])
def test_long_failed_read_persists_evidence_and_alerts_not_trades(
    tmp_path,
    monkeypatch,
    capsys,
    environment,
):
    with pytest.raises(MarketDataReadError) as error:
        read_moex(Mock(side_effect=fail(httpx.ReadError)), stage="candles", secid="SBER")
    monkeypatch.setattr(cli.MoexAlgoReadOnlyAdapter, "from_environment", lambda **_: Mock())
    monkeypatch.setattr(cli, "run_hourly_shadow", Mock(side_effect=error.value))
    outbox = tmp_path / "alerts.sqlite3"
    output = tmp_path / "plan.json"
    args = dict(
        config_path=ROOT / "config/shadow.json",
        universe_path=ROOT / "config/universe.json",
        portfolio_path=tmp_path / "portfolio.json",
        geo_path=tmp_path / "geo.json",
        output_path=output,
        as_of=NOW,
        require_token=False,
        failure_outbox_path=outbox,
        report_environment=environment,
    )
    assert cli.hourly_shadow(**args) == 2
    assert cli.hourly_shadow(**args) == 2  # dedupe, not two messages for the same failure
    assert not output.exists()
    evidence = json.loads(output.with_suffix(".error.json").read_text(encoding="utf-8"))
    assert evidence["environment"] == environment
    assert evidence["failure"]["stage"] == "candles"
    sender = Mock()
    assert deliver_pending(
        SQLiteOutbox(outbox), sender, chat_id="trades", alert_chat_id="alerts", now=NOW
    ) == (1, 0)
    chat, body = sender.send_text.call_args.args
    assert chat == "alerts" and environment in body and "apim.moex.com" in body
    assert "SECRET" not in body and "SECRET" not in capsys.readouterr().out


def test_no_httpx_needed_for_successful_fake_port():
    assert read_moex(lambda: 123, stage="candles", secid="SBER") == 123


def test_alert_telegram_payload_retry_and_no_duplicate(tmp_path, monkeypatch):
    import io
    from datetime import timedelta
    from urllib.error import URLError
    from urllib.parse import parse_qs

    from moex_bot import notifications
    from moex_bot.long_sandbox_verifier import publish_planner_failures

    receipt = {
        "as_of": NOW.isoformat(),
        "environment": "SANDBOX",
        "failure": {
            "host": "apim.moex.com",
            "stage": "candles",
            "category": "dns",
            "secid": "SBER",
            "attempts": 3,
        },
    }
    path = tmp_path / "run-test" / "plan.error.json"
    path.parent.mkdir()
    path.write_text(json.dumps(receipt))
    db_path = tmp_path / "outbox.sqlite3"
    publish_planner_failures(tmp_path, db_path, NOW)
    db = SQLiteOutbox(db_path)
    sender = notifications.TelegramBotApiSender("test-only-token")
    http = Mock(side_effect=[URLError("offline"), io.BytesIO(b'{"ok":true}')])
    monkeypatch.setattr(notifications.request, "urlopen", http)
    assert deliver_pending(db, sender, chat_id="trades", alert_chat_id="alerts", now=NOW) == (0, 1)
    later = NOW + timedelta(hours=1)
    assert deliver_pending(db, sender, chat_id="trades", alert_chat_id="alerts", now=later) == (
        1,
        0,
    )
    publish_planner_failures(tmp_path, db_path, later)
    assert deliver_pending(db, sender, chat_id="trades", alert_chat_id="alerts", now=later) == (
        0,
        0,
    )
    for call in http.call_args_list:
        request = call.args[0]
        payload = parse_qs(request.data.decode())
        assert payload["chat_id"] == ["alerts"]
        text = payload["text"][0]
        assert all(term in text for term in ["SANDBOX", "apim.moex.com", "candles", "SBER", "dns"])
        assert "test-only-token" not in text and len(text) <= 4096


@pytest.mark.parametrize("variation", ["valid", "stale", "future", "prod", "secret", "malformed"])
def test_verifier_routes_only_fresh_sanitized_sandbox_failure(tmp_path, variation):
    from datetime import timedelta

    from moex_bot.long_sandbox_verifier import publish_planner_failures

    at = NOW + timedelta(days={"stale": -2, "future": 1}.get(variation, 0))
    receipt = {
        "as_of": at.isoformat(),
        "environment": "SANDBOX",
        "failure": {
            "host": "apim.moex.com",
            "stage": "candles",
            "category": "dns",
            "secid": "SBER",
            "attempts": 3,
        },
    }
    if variation == "prod":
        receipt["environment"] = "PROD"
    if variation == "secret":
        receipt["failure"]["host"] = "SECRET"
    path = tmp_path / "run-test" / "plan.error.json"
    path.parent.mkdir()
    path.write_text("{" if variation == "malformed" else json.dumps(receipt))
    outbox = tmp_path / "notifications.sqlite3"
    publish_planner_failures(tmp_path, outbox, NOW)
    publish_planner_failures(tmp_path, outbox, NOW)
    sender = Mock()
    sent, failed = deliver_pending(
        SQLiteOutbox(outbox), sender, chat_id="trades", alert_chat_id="alerts", now=NOW
    )
    assert sent == int(variation == "valid") and failed == 0
    if sent:
        chat, body = sender.send_text.call_args.args
        assert chat == "alerts" and "apim.moex.com" in body and "dns" in body


def test_optional_httpx_absent_does_not_mask_original_error(monkeypatch):
    from moex_bot.integrations import read_failure

    monkeypatch.setattr(read_failure, "import_module", Mock(side_effect=ImportError()))
    with pytest.raises(ValueError, match="original"):
        read_moex(Mock(side_effect=ValueError("original")), stage="candles", secid="SBER")


def test_connection_exception_without_request_still_has_safe_evidence():
    with pytest.raises(MarketDataReadError) as error:
        read_moex(Mock(side_effect=httpx.ReadError("SECRET")), stage="metadata", secid="SBER")
    assert error.value.evidence["host"] == "unknown"
    assert "SECRET" not in str(error.value)


def test_retry_does_not_move_asof_or_include_future_candles(monkeypatch):
    from test_moexalgo_data import FakeTicker

    from moex_bot.integrations import read_failure

    monkeypatch.setattr(read_failure.time, "sleep", lambda _: None)
    # Inject no sleep at the read boundary, without changing the frozen observation time.
    from moex_bot.integrations import moexalgo_data

    monkeypatch.setattr(
        moexalgo_data, "read_moex", lambda call, **kw: read_moex(call, **kw, sleeper=lambda _: None)
    )
    ticker = FakeTicker(NOW)
    request = Mock(
        side_effect=[fail(), ticker.candles(start="2026-10-01", end="2026-11-03", period="1h")]
    )
    ticker.candles = request
    adapter = MoexAlgoReadOnlyAdapter(
        lambda *_: ticker, lambda *_: {"lotsize": 10, "minstep": "0.01"}
    )
    observation = adapter.hourly_observation(secid="SBER", uid="test", board="TQBR", as_of=NOW)
    assert request.call_args_list[0] == request.call_args_list[1]
    assert observation.price == 124 and observation.observed_at < NOW


@pytest.mark.parametrize("unhandled", [False, True])
def test_pr6_records_failure_code_and_safe_endpoint(tmp_path, monkeypatch, capsys, unhandled):
    import pr6_observe as pr6

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(pr6, "datetime", FrozenDateTime)
    registry = Mock()
    registry.sandbox_bindings.return_value = {pr6.AccountPurpose("long"): "sandbox-test"}
    monkeypatch.setattr(pr6, "load_account_registry", lambda _: registry)
    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", "SECRET")
    service = Mock()
    service.operations.return_value = ()
    service.broker_snapshot.return_value.as_portfolio_payload.return_value = {
        "reported_equity": "300000",
        "cash": "300000",
        "positions": {},
        "open_orders": 0,
    }
    monkeypatch.setattr(pr6.sb, "TInvestSandboxAccountService", lambda *a, **k: service)
    monkeypatch.setattr(pr6.cli, "load_universe", lambda _: ())
    monkeypatch.setattr(pr6.cli, "_verified_instruments", lambda _: {})
    monkeypatch.setattr(pr6.cli, "geo_refresh", lambda **_: 0)

    def planner(**kwargs):
        if unhandled:
            raise ValueError("SECRET")
        kwargs["output_path"].with_suffix(".error.json").write_text(
            json.dumps({"failure": {"host": "apim.moex.com", "stage": "ticker"}})
        )
        return 2

    monkeypatch.setattr(pr6.cli, "hourly_shadow", planner)
    assert pr6.observe(tmp_path, "long") == 2
    cycle = json.loads(next(tmp_path.rglob("cycle-*.json")).read_text())
    assert cycle["code"] == 2 and cycle["stage"] == "long-planner"
    if not unhandled:
        assert cycle["failure"]["failure"]["host"] == "apim.moex.com"
        assert "apim.moex.com" in capsys.readouterr().err
    assert "SECRET" not in json.dumps(cycle)
