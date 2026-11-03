import json
from datetime import timedelta

import pytest
from test_long_auto_sandbox import case as case

from moex_bot import long_sandbox_verifier as verifier
from moex_bot.long_auto_once import init, run_sandbox_candidate
from moex_bot.long_auto_signal import Candidate
from moex_bot.notifications import SQLiteOutbox


@pytest.fixture
def saved(case, tmp_path):
    c, broker, transport = case
    candidate = Candidate(account_id=broker.account_id, decision_sha256="a" * 64, **vars(c))
    now = candidate.valid_until - timedelta(minutes=3)
    state, anchor = tmp_path / "ledger.sqlite3", tmp_path / "control/claim"
    init(state, broker.account_id, anchor=anchor)
    return candidate, state, anchor, broker, transport, now


def test_waiting_quiet_no_api(saved):
    c, state, anchor, broker, transport, now = saved
    assert verifier.verify(state, anchor, c.account_id, broker, now) is None
    assert not transport.posts


def test_actual_readback_and_deduplication(saved, tmp_path):
    c, state, anchor, broker, transport, now = saved
    assert run_sandbox_candidate(c, state, broker, clock=lambda: now,
                                 anchor=anchor) == "FILLED_PROTECTED"
    transport.posts.clear()
    result = verifier.verify(state, anchor, c.account_id, broker, now)
    assert result["state"] == "FILLED_PROTECTED"
    assert result["buy_verified"] and result["stop_verified"]
    assert all(name in verifier.READS for name, _ in transport.posts)
    assert c.account_id not in json.dumps(result)
    outbox, output = tmp_path / "outbox.sqlite3", tmp_path / "evidence"
    verifier.publish(result, output, outbox, now)
    verifier.publish(result | {"observed_at": (now + timedelta(minutes=5)).isoformat()},
                     output, outbox, now)
    assert SQLiteOutbox(outbox).health(now=now)["pending_due"] == 1
    assert len(list(output.glob("*.json"))) == 1
    assert "PROD не включён" in verifier.render(result)


@pytest.mark.parametrize("fault", ["parent", "lost_stop", "foreign_account", "missing_anchor"])
def test_no_success_on_ambiguous_broker_or_journal(saved, fault):
    c, state, anchor, broker, transport, now = saved
    run_sandbox_candidate(c, state, broker, clock=lambda: now, anchor=anchor)
    if fault == "parent":
        transport.fault = "parent"
    elif fault == "lost_stop":
        transport.stop = False
    elif fault == "missing_anchor":
        anchor.unlink()
    with pytest.raises(ValueError):
        verifier.verify(state, anchor, "other" if fault == "foreign_account" else c.account_id,
                        broker, now)


@pytest.mark.parametrize("method", ["PostSandboxOrder", "PostSandboxStopOrder",
                                    "CancelSandboxOrder", "OpenSandboxAccount",
                                    "CloseSandboxAccount"])
def test_transport_forbids_any_mutation(method):
    with pytest.raises(ValueError, match="reads only"):
        verifier.ReadOnlySandboxTransport().post(verifier.PREFIX + method,
                                                 headers={}, payload={}, timeout_seconds=1)


def test_transport_forbids_production():
    with pytest.raises(ValueError):
        verifier.ReadOnlySandboxTransport().post(
            "https://invest-public-api.tbank.ru/rest/GetSandboxOrderState",
            headers={}, payload={}, timeout_seconds=1)


def test_corrupt_receipt_is_not_silently_used(tmp_path):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    result = {"state": "UNAVAILABLE", "observed_at": now.isoformat()}
    output, outbox = tmp_path / "evidence", tmp_path / "outbox.sqlite3"
    verifier.publish(result, output, outbox, now)
    receipt = next(output.glob("*.json"))
    receipt.write_text("{")
    with pytest.raises(ValueError):
        verifier.publish(result, output, outbox, now)


def test_interrupted_publish_leaves_no_final_receipt_or_notification(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    monkeypatch.setattr(verifier.os, "link", lambda *a: (_ for _ in ()).throw(OSError("crash")))
    monkeypatch.setattr(verifier.os, "rename", lambda *a: (_ for _ in ()).throw(OSError("crash")))
    output, outbox = tmp_path / "evidence", tmp_path / "outbox.sqlite3"
    with pytest.raises(OSError):
        verifier.publish({"state": "UNAVAILABLE", "observed_at": now.isoformat()},
                         output, outbox, now)
    assert not list(output.iterdir()) and not outbox.exists()
