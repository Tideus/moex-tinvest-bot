import json
import socket
import sqlite3
import ssl
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError

import pytest
from test_long_auto_sandbox import case as case
from test_long_sandbox_verifier import saved as saved

from moex_bot import long_sandbox_verifier as verifier
from moex_bot.long_auto_once import run_sandbox_candidate
from moex_bot.long_verification_diagnostics import VerificationProgress
from moex_bot.notifications import SQLiteOutbox

SECRET = "secret-token-account-order-id-DO-NOT-LOG"


@pytest.fixture
def running(saved, monkeypatch, tmp_path):
    c, state, anchor, broker, transport, now = saved
    run_sandbox_candidate(c, state, broker, clock=lambda: now, anchor=anchor)
    transport.posts.clear()
    monkeypatch.setattr(verifier, "STATE", state)
    monkeypatch.setattr(verifier, "ANCHOR", anchor)
    monkeypatch.setattr(verifier, "OUTPUT", tmp_path / "verification")
    monkeypatch.setattr(verifier, "OUTBOX", tmp_path / "outbox.sqlite3")
    monkeypatch.setattr(verifier, "_scope", lambda _: None)
    monkeypatch.setattr(verifier, "_working_account", lambda: c.account_id)
    monkeypatch.setattr(verifier, "writer_lock", nullcontext)
    monkeypatch.setattr(verifier, "SandboxLongBroker", lambda *a, **kw: broker)
    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", SECRET)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr("sys.argv", ["verifier"])
    return c, state, anchor, broker, transport, now


@pytest.mark.parametrize(("method", "stage", "call_number"), [
    ("GetSandboxAccounts", "account_readback", 1),
    ("GetSandboxOrderState", "entry_readback", 1),
    ("GetSandboxPositions", "positions_readback", 1),
    ("GetSandboxOrders", "orders_readback", 1),
    ("GetSandboxStopOrders", "stops_readback", 1),
    ("GetSandboxStopOrders", "protection_readback", 2),
])
def test_main_persists_exact_failed_stage_and_api(running, monkeypatch, capsys,
                                               method, stage, call_number):
    c, state, anchor, broker, transport, now = running
    original = transport.post
    calls = []

    def post(url, **kwargs):
        name = url.rsplit("/", 1)[1]
        calls.append(name)
        if name == method and calls.count(name) == call_number:
            raise URLError(TimeoutError(SECRET))
        return original(url, **kwargs)

    monkeypatch.setattr(transport, "post", post)
    before = state.read_bytes(), anchor.read_bytes()
    assert verifier.main() == 2
    receipt = json.loads(next(verifier.OUTPUT.glob("*.json")).read_bytes())
    assert receipt["state"] == "UNAVAILABLE" and receipt["read_only"] is True
    assert receipt["failure"] == {
        "stage": stage, "source": "t_invest_sandbox",
        "host": "sandbox-invest-public-api.tbank.ru", "method": method,
        "category": "timeout", "reason": "истекло время ожидания ответа", "http_status": None,
    }
    rendered = verifier.render(receipt)
    assert method in rendered and "Этап:" in rendered and "НЕ проверена" in rendered
    stdout = capsys.readouterr().out
    assert "timeout" in stdout and SECRET not in stdout and c.account_id not in stdout
    assert SECRET not in json.dumps(receipt) + rendered
    assert c.account_id not in json.dumps(receipt) + rendered
    assert all(name in verifier.READS for name in calls)
    assert before == (state.read_bytes(), anchor.read_bytes())


def test_ledger_error_is_not_reported_as_api_outage(running):
    _, _, anchor, _, transport, _ = running
    anchor.unlink()
    assert verifier.main() == 2
    receipt = json.loads(next(verifier.OUTPUT.glob("*.json")).read_bytes())
    assert receipt["failure"]["stage"] == "ledger_status"
    assert receipt["failure"]["source"] == "local"
    assert receipt["failure"]["method"] is None
    assert not transport.posts


def test_stop_mismatch_is_specific_not_transport_outage(running):
    _, _, _, _, transport, _ = running
    transport.stop = False
    assert verifier.main() == 2
    receipt = json.loads(next(verifier.OUTPUT.glob("*.json")).read_bytes())
    assert receipt["failure"]["category"] == "validation"
    assert receipt["failure"]["stage"] == "protection_readback"
    assert "стоп не найден" in receipt["failure"]["reason"]


@pytest.mark.parametrize(("error", "category"), [
    (URLError(TimeoutError(SECRET)), "timeout"),
    (URLError(socket.gaierror(-2, SECRET)), "dns"),
    (URLError(ssl.SSLError(SECRET)), "tls"),
    (URLError(ConnectionResetError(SECRET)), "connection"),
    (URLError(SECRET), "connection"),
    (HTTPError("https://example.invalid/" + SECRET, 503, SECRET, {"token": SECRET}, None), "http"),
    (json.JSONDecodeError(SECRET, SECRET, 0), "invalid_json"),
    (sqlite3.OperationalError(SECRET), "sqlite"),
    (FileNotFoundError(SECRET), "missing_file"),
    (PermissionError(SECRET), "permission"),
    (OSError(SECRET), "io"),
    (ValueError(SECRET), "validation"),
    (KeyError(SECRET), "validation"),
    (TypeError(SECRET), "validation"),
    (RuntimeError(SECRET), "unexpected"),
])
def test_classification_does_not_serialize_secrets(error, category):
    failure = VerificationProgress("entry_readback", "GetSandboxOrderState").failure(error)
    assert failure["category"] == category
    assert SECRET not in json.dumps(failure)
    assert failure["http_status"] == (503 if category == "http" else None)


def test_exception_chain_is_bounded_and_keeps_network_cause():
    exc = ValueError(SECRET)
    exc.__cause__ = URLError(TimeoutError(SECRET))
    assert VerificationProgress().failure(exc)["category"] == "timeout"
    exc.__cause__ = exc
    assert VerificationProgress().failure(exc)["category"] == "validation"


@pytest.mark.parametrize("contended", [True, False])
def test_writer_contention_is_waiting_only_at_lock_stage(running, monkeypatch, contended):
    def fail(*a, **kw):
        raise ValueError(SECRET) from BlockingIOError(SECRET)

    monkeypatch.setattr(verifier, "writer_lock" if contended else "verify", fail)
    assert verifier.main() == (0 if contended else 2)
    assert verifier.OUTPUT.exists() is not contended


def test_each_failure_is_preserved_without_telegram_storm(tmp_path):
    now = datetime(2026, 11, 18, 7, tzinfo=UTC)
    output, outbox = tmp_path / "evidence", tmp_path / "outbox.sqlite3"
    result = {"state": "UNAVAILABLE", "observed_at": now.isoformat(),
              "failure": VerificationProgress("stops_readback", "GetSandboxStopOrders").failure(
                  TimeoutError(SECRET))}
    verifier.publish(result, output, outbox, now)
    original_path = next(output.glob("*.json"))
    original = original_path.read_bytes()
    later = result | {"observed_at": (now + timedelta(minutes=5)).isoformat()}
    verifier.publish(later, output, outbox, now)
    verifier.publish(later, output, outbox, now)
    assert len(list(output.glob("*.json"))) == 2
    assert original_path.read_bytes() == original
    assert SQLiteOutbox(outbox).health(now=now)["pending_due"] == 1
    # Changed cause on the same day is actionable and must not be suppressed.
    progress = VerificationProgress("stops_readback", "GetSandboxStopOrders")
    changed = later | {"failure": progress.failure(URLError(socket.gaierror(-2, SECRET)))}
    verifier.publish(changed, output, outbox, now)
    assert SQLiteOutbox(outbox).health(now=now)["pending_due"] == 2
    next_day = now + timedelta(days=1)
    verifier.publish(result | {"observed_at": next_day.isoformat()}, output, outbox, next_day)
    assert SQLiteOutbox(outbox).health(now=next_day)["pending_due"] == 3
    assert len(list(output.glob("*.json"))) == 4


def test_readonly_transport_tracks_only_allowlisted_method(monkeypatch):
    progress = VerificationProgress("protection_readback")
    monkeypatch.setattr(verifier.SandboxOnlyTransport, "post", lambda *a, **kw: {})
    transport = verifier.ReadOnlySandboxTransport(progress)
    assert transport.post(verifier.PREFIX + "GetSandboxStopOrders", headers={}, payload={},
                          timeout_seconds=1) == {}
    assert progress.api_method == "GetSandboxStopOrders"
    with pytest.raises(ValueError, match="reads only"):
        transport.post(verifier.PREFIX + "PostSandboxOrder", headers={}, payload={},
                       timeout_seconds=1)
    assert progress.api_method == "GetSandboxStopOrders"


def test_failure_receipt_timestamp_must_match(tmp_path):
    now = datetime(2026, 11, 18, 7, tzinfo=UTC)
    output, outbox = tmp_path / "evidence", tmp_path / "outbox.sqlite3"
    result = {"state": "UNAVAILABLE", "observed_at": now.isoformat()}
    verifier.publish(result, output, outbox, now)
    receipt = next(output.glob("*.json"))
    receipt.write_text(json.dumps(result | {"observed_at": "2000-01-01T00:00:00+00:00"}))
    with pytest.raises(ValueError, match="never silently replace"):
        verifier.publish(result, output, outbox, now)
