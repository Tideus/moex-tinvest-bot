"""Second admission cannot reset history, replay a signal or survive unsafe rollback."""
import json
import os
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_long_auto_once import setup  # noqa: F401

from moex_bot import long_sandbox_retry as retry
from moex_bot.domain import OrderStatus
from moex_bot.long_auto_once import OnceLedger, recover_sandbox_protection, run_sandbox_candidate
from moex_bot.long_sandbox_verifier import render, verify


@pytest.fixture
def previous(setup, monkeypatch):  # noqa: F811
    c, state, broker, now = setup
    ledger = OnceLedger(state, c.account_id)
    ledger.claim(c, now)
    cancelled = replace(broker.submit.return_value, status=OrderStatus.CANCELLED, filled_lots=0)
    ledger.record("ENTRY_NOT_FILLED", now, cancelled)
    broker._post.return_value = {"accounts": [
        {"id": c.account_id, "status": "ACCOUNT_STATUS_OPEN"}]}
    broker._sandbox.side_effect = lambda *a, **k: {}
    for helper in ("require_root", "inherit_owner", "receipt_permissions", "sync_directory",
                   "trusted_receipt"):
        monkeypatch.setattr(retry, helper, Mock())
    return c, state, ledger.anchor, broker, now + timedelta(days=1)


def grant(previous):
    c, state, anchor, broker, now = previous
    return retry.authorize(state, anchor, c.account_id, broker, now)


def test_admission_is_local_idempotent_and_preserves_original(previous):
    c, state, anchor, broker, now = previous
    original = state.read_bytes(), anchor.read_bytes()
    binding = grant(previous)
    assert binding.state != state and binding.anchor != anchor and binding.admitted_at == now
    assert OnceLedger(binding.state, c.account_id, anchor=binding.anchor).status() == "WAITING"
    assert retry.authorize(state, anchor, c.account_id, broker, now + timedelta(days=1)) == binding
    assert broker.lookup.call_count == 1
    assert (state.read_bytes(), anchor.read_bytes()) == original
    broker.submit.assert_not_called()
    broker.protect.assert_not_called()
    broker.cancel_unfilled_verified.assert_not_called()


@pytest.mark.parametrize("fault", ["partial", "unknown", "active", "filled", "wrong_order",
                                   "prod", "foreign", "positions", "orders", "stops", "loading",
                                   "future_positions", "option_positions", "closed", "timeout"])
def test_no_admission_on_uncertainty_or_existing_exposure(previous, fault):
    c, state, anchor, broker, now = previous
    if fault == "timeout":
        broker.lookup.side_effect = TimeoutError()
    elif fault in {"partial", "unknown", "active", "filled", "wrong_order"}:
        def lookup(parent):
            return replace(parent, **{
                "partial": {"filled_lots": 1}, "unknown": {"status": OrderStatus.UNKNOWN},
                "active": {"status": OrderStatus.ACCEPTED},
                "filled": {"status": OrderStatus.FILLED, "filled_lots": 1},
                "wrong_order": {"broker_order_id": "foreign"},
            }[fault])
        broker.lookup.side_effect = lookup
    elif fault in {"prod", "foreign"}:
        setattr(broker, "environment" if fault == "prod" else "account_id", fault)
    elif fault == "closed":
        broker._post.return_value = {"accounts": []}
    else:
        def rows(method, *a, **k):
            if method == "GetSandboxPositions":
                return {
                    "positions": {"securities": [{"balance": 1}]},
                    "future_positions": {"futures": [{"balance": 1}]},
                    "option_positions": {"options": [{"balance": 1}]},
                    "loading": {"limitsLoadingInProgress": True},
                }.get(fault, {})
            if method == "GetSandboxOrders" and fault == "orders":
                return {"orders": [{}]}
            if method == "GetSandboxStopOrders" and fault == "stops":
                return {"stopOrders": [{}]}
            return {}
        broker._sandbox.side_effect = rows
    with pytest.raises((ValueError, TimeoutError)):
        grant(previous)
    assert not retry.paths(state, anchor)[0].exists()
    broker.submit.assert_not_called()


@pytest.mark.parametrize("fault", ["receipt", "old_db", "new_db", "new_waiting", "orphan"])
def test_rollback_or_incomplete_admission_never_rearms(previous, fault):
    c, state, anchor, broker, now = previous
    binding = grant(previous)
    empty = binding.state.read_bytes()
    ledger = OnceLedger(binding.state, c.account_id, anchor=binding.anchor)
    ledger.claim(c, now)
    receipt = retry.paths(state, anchor)[2]
    if fault == "receipt":
        raw = json.loads(receipt.read_text())
        raw["max_notional_rub"] = "20000"
        receipt.write_text(json.dumps(raw))
    elif fault == "old_db":
        OnceLedger(state, c.account_id, anchor=anchor).record("ENTRY_OBSERVED", now)
    elif fault == "new_db":
        binding.state.unlink()
    elif fault == "new_waiting":
        binding.state.write_bytes(empty)
    else:
        receipt.unlink()
    with pytest.raises((ValueError, OSError)):
        retry.resolve(state, anchor, c.account_id)
    with pytest.raises((ValueError, OSError)):
        grant(previous)
    broker.submit.assert_not_called()


@pytest.mark.parametrize("fault", ["old_signal", "same_hash", "two_lots", "over_budget", "expired"])
def test_only_new_signal_with_unchanged_sizing(previous, fault):
    c, _, _, _, now = previous
    binding = grant(previous)
    candidate = replace(c, decision_sha256="b" * 64, valid_until=now + timedelta(minutes=6))
    changes = {"old_signal": {"valid_until": now + timedelta(minutes=5)},
               "same_hash": {"decision_sha256": c.decision_sha256}, "two_lots": {"lots": 2},
               "over_budget": {"limit_price": Decimal("1001")},
               "expired": {"valid_until": now}}
    with pytest.raises(ValueError):
        binding.check_candidate(replace(candidate, **changes[fault]), now + timedelta(minutes=1))
    binding.check_candidate(candidate, now + timedelta(minutes=1))


def test_second_attempt_is_one_shot_even_after_restart(previous):
    c, state, anchor, broker, now = previous
    binding = grant(previous)
    fresh = replace(c, decision_sha256="b" * 64, valid_until=now + timedelta(minutes=6),
                    order_request_id=str(uuid4()), stop_request_id=str(uuid4()))
    broker.submit.return_value = replace(broker.submit.return_value,
        intent=replace(broker.submit.return_value.intent, order_request_id=fresh.order_request_id))
    binding.check_candidate(fresh, now + timedelta(minutes=1))
    assert run_sandbox_candidate(fresh, binding.state, broker, anchor=binding.anchor,
                                clock=lambda: now + timedelta(minutes=1)) == "FILLED_PROTECTED"
    restarted = retry.resolve(state, anchor, c.account_id)
    assert run_sandbox_candidate(fresh, restarted.state, broker, anchor=restarted.anchor,
                                clock=lambda: now + timedelta(minutes=2)) == "ALREADY_CLAIMED"
    assert broker.submit.call_count == broker.protect.call_count == 1
    assert grant(previous) == binding
    assert OnceLedger(state, c.account_id, anchor=anchor).status() == "ENTRY_NOT_FILLED"


def test_verifier_reads_second_attempt_not_original_cancel(previous):
    c, state, anchor, broker, now = previous
    binding = grant(previous)
    assert verify(state, anchor, c.account_id, broker, now) is None
    broker.lookup.reset_mock()
    ledger = OnceLedger(binding.state, c.account_id, anchor=binding.anchor)
    ledger.claim(c, now)
    ledger.record("ENTRY_NOT_FILLED", now,
                  replace(broker.submit.return_value, status=OrderStatus.CANCELLED, filled_lots=0))
    result = verify(state, anchor, c.account_id, broker, now)
    assert result["state"] == "ENTRY_NOT_FILLED" and result["attempt"] == 2
    assert "попытка 2" in render(result)


def test_real_root_permission_guard(tmp_path):
    if os.name == "posix" and os.geteuid() == 0:
        pytest.skip("separate root integration test")
    with pytest.raises(ValueError, match="root"):
        retry.require_root()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}")
    with pytest.raises(ValueError, match="root-owned"):
        retry.trusted_receipt(receipt)


def test_second_attempt_lost_buy_ack_is_never_reposted(previous):
    c, state, anchor, broker, now = previous
    binding = grant(previous)
    broker.submit.side_effect = TimeoutError()
    assert run_sandbox_candidate(c, binding.state, broker, anchor=binding.anchor,
                                clock=lambda: now) == "QUARANTINED"
    restored = retry.resolve(state, anchor, c.account_id)
    assert run_sandbox_candidate(c, restored.state, broker, anchor=restored.anchor,
                                clock=lambda: now) == "ALREADY_CLAIMED"
    assert broker.submit.call_count == 1
    broker.protect.assert_not_called()


def test_second_attempt_stop_only_recovery_and_rollback_fence(previous):
    c, state, anchor, broker, now = previous
    binding = grant(previous)
    ledger = OnceLedger(binding.state, c.account_id, anchor=binding.anchor)
    ledger.claim(c, now)
    ledger.record("ENTRY_OBSERVED", now, broker.submit.return_value)
    before_stop = binding.state.read_bytes()
    broker.set_mutation_guard.side_effect = lambda guard: setattr(broker, "guard", guard)

    def protect(*args):
        broker.guard()
        return {"stopOrderId": "stop"}

    broker.protect.side_effect = protect
    assert recover_sandbox_protection(c, binding.state, broker, anchor=binding.anchor,
                                     clock=lambda: now) == "FILLED_PROTECTED"
    broker.submit.assert_not_called()
    assert broker.protect.call_count == 1
    with pytest.raises(ValueError):
        recover_sandbox_protection(c, binding.state, broker, anchor=binding.anchor,
                                   clock=lambda: now)
    binding.state.write_bytes(before_stop)
    with pytest.raises(ValueError, match="predates"):
        retry.resolve(state, anchor, c.account_id)
    assert broker.protect.call_count == 1


@pytest.mark.skipif(os.name != "posix", reason="root-owned Linux receipt integration")
def test_root_receipt_and_restart(setup):  # noqa: F811
    if os.geteuid() != 0:
        pytest.skip("run explicitly as root in isolated Linux fixture")
    c, state, broker, now = setup
    ledger = OnceLedger(state, c.account_id)
    ledger.claim(c, now)
    ledger.record("ENTRY_NOT_FILLED", now,
                  replace(broker.submit.return_value, status=OrderStatus.CANCELLED, filled_lots=0))
    broker._post.return_value = {"accounts": [
        {"id": c.account_id, "status": "ACCOUNT_STATUS_OPEN"}]}
    broker._sandbox.side_effect = lambda *a, **k: {}
    binding = retry.authorize(state, ledger.anchor, c.account_id, broker, now)
    receipt = retry.paths(state, ledger.anchor)[2]
    assert receipt.stat().st_uid == 0 and receipt.stat().st_mode & 0o777 == 0o640
    assert retry.resolve(state, ledger.anchor, c.account_id) == binding
    receipt.chmod(0o666)
    with pytest.raises(ValueError, match="root-owned"):
        retry.resolve(state, ledger.anchor, c.account_id)


def test_operator_command_requires_confirmation_and_does_not_start_trading():
    from test_intraday import PROJECT_ROOT
    script = (PROJECT_ROOT / "scripts/ubuntu/moex-botctl.sh").read_text(encoding="utf-8")
    block = script.split("admit_second_sandbox() {", 1)[1].split("enable_prod_long_once()", 1)[0]
    assert "--confirm-second-sandbox" in block and "load_secrets" in block
    assert "long-sandbox-verifier.timer" in block and "daily-report.timer" in block
    assert "systemctl start" not in block and "systemctl enable" not in block
    assert "admit-second-sandbox" in block
    backup = (PROJECT_ROOT / "scripts/ubuntu/backup.sh").read_text(encoding="utf-8")
    assert "sandbox-long-retry-2.json" in backup and ".sandbox-control.tar.gz" in backup


def test_legacy_readonly_research_resolves_current_admission(tmp_path, monkeypatch):
    from moex_bot.long_auto_once import init
    from moex_bot.monitoring_long_once import ReadOnlyLedger

    original = tmp_path / "sandbox.sqlite3"
    active, anchor = tmp_path / "second.db", tmp_path / "c/a"
    init(active, "account", anchor=anchor)
    monkeypatch.setattr("moex_bot.monitoring_long_once.resolve",
                        lambda *a: retry.Binding(active, anchor))
    reader = ReadOnlyLedger(original, "account", anchor=anchor)
    assert reader.path == active and reader.status() == "WAITING"
