"""No real tokens/network: durable one-shot, fees, manual holdings and crash boundaries."""

import argparse
import hashlib
import json
import shutil
import sqlite3
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_live_execution import _setup
from test_live_protection import _stop

from moex_bot import prod_long_once as once
from moex_bot import prod_long_once_operator as operator
from moex_bot.domain import OrderStatus
from moex_bot.integrations.tinvest_sandbox import decimal_to_quotation
from moex_bot.live_execution import LiveHolding, registered_live_journal
from moex_bot.long_auto_signal import Candidate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def case(tmp_path):
    broker, old, now = _setup(tmp_path / "state")
    broker.state = replace(broker.state, estimated_total=Decimal("1001"))
    broker.lookup = Mock(side_effect=lambda record: record)
    broker.cancel_pending = Mock()
    candidate = Candidate(
        broker.account_id,
        "a" * 64,
        old["intent"].instrument,
        Decimal("100"),
        Decimal("98"),
        str(uuid4()),
        str(uuid4()),
        now + timedelta(minutes=5),
    )
    stops = Mock(account_id=broker.account_id, environment="prod")
    stops.available_units.return_value = 10
    stops.list_stops.return_value = ()

    def post(plan, request_id):
        row = _stop(plan)
        row["stopPrice"] = decimal_to_quotation(plan.stop_trigger) | {"currency": "rub"}
        stops.list_stops.return_value = (row,)
        return {"stopOrderId": "known-stop", "orderRequestId": request_id}

    stops.post_stop.side_effect = post
    directory = tmp_path / "independent-control"
    directory.mkdir()
    return dict(
        candidate=candidate,
        policy=old["policy"],
        release=old["release_sha256"],
        journal=old["journal_path"],
        protection_journal=tmp_path / "protection.sqlite3",
        claim_directory=directory,
        broker=broker,
        stops=stops,
        authority=Mock(),
        clock=lambda: now,
    )


def execute(case):
    return once.run_once(**case)


def test_one_buy_one_native_stop_and_never_another_candidate(case):
    result = execute(case)
    assert result == {"state": "FILLED_PROTECTED", "filled_lots": 1, "protection": "ACTIVE"}
    assert len(case["broker"].calls) == 1
    assert case["stops"].post_stop.call_count == 1
    case["candidate"] = replace(case["candidate"], order_request_id=str(uuid4()))
    assert execute(case)["state"] == "ALREADY_CLAIMED"
    assert len(case["broker"].calls) == 1 and case["stops"].post_stop.call_count == 1
    assert (case["claim_directory"] / "buy-dispatch.json").is_file()
    assert (case["claim_directory"] / "stop-dispatch.json").is_file()


def test_grant_failure_never_touches_broker_or_claim(case):
    case["authority"].side_effect = ValueError("not reviewed")
    with pytest.raises(ValueError, match="not reviewed"):
        execute(case)
    assert not list(case["claim_directory"].iterdir())
    assert not case["broker"].calls and not case["stops"].post_stop.called


@pytest.mark.parametrize("value", ["10000.01", "NaN", "-1", "999"])
def test_invalid_total_or_commission_cap_blocks_before_post(case, value):
    case["broker"].state = replace(case["broker"].state, estimated_total=Decimal(value))
    with pytest.raises(ValueError):
        execute(case)
    assert not case["broker"].calls
    assert not list(case["claim_directory"].iterdir())


def test_exact_cash_cap_boundary(case):
    case["candidate"] = replace(
        case["candidate"], limit_price=Decimal("999.5"), stop_trigger=Decimal("979.51")
    )
    case["broker"].state = replace(
        case["broker"].state,
        bid=Decimal("999.5"),
        ask=Decimal("1000"),
        estimated_total=Decimal("10000"),
    )
    assert execute(case)["state"] == "FILLED_PROTECTED"


@pytest.mark.parametrize("units", [1, 10, 20, -1])
def test_any_existing_candidate_position_is_manual_and_blocks(case, units):
    i = case["candidate"].instrument
    state = case["broker"].state
    case["broker"].state = replace(
        state, positions={i.uid: LiveHolding(i, units, units, Decimal(100))}
    )
    with pytest.raises(ValueError, match="manual-position"):
        execute(case)
    assert not case["broker"].calls and not case["stops"].post_stop.called


def test_other_protected_manual_positions_untouched(case):
    i = replace(case["candidate"].instrument, secid="GAZP", uid="manual-gazp")
    case["broker"].state = replace(
        case["broker"].state,
        positions={i.uid: LiveHolding(i, 100, 100, Decimal(100))},
    )
    assert execute(case)["state"] == "FILLED_PROTECTED"
    assert case["broker"].state.positions[i.uid].units == 100
    assert case["broker"].calls[0].instrument.uid != i.uid
    assert case["stops"].post_stop.call_args[0][0].instrument.uid != i.uid


@pytest.mark.parametrize(
    "field,value",
    [
        ("lots", True),
        ("lots", 2),
        ("limit_price", Decimal("1001")),
        ("stop_trigger", Decimal("99")),
        ("decision_sha256", "fake"),
    ],
)
def test_changed_candidate_rejected(case, field, value):
    case["candidate"] = replace(case["candidate"], **{field: value})
    with pytest.raises(ValueError):
        execute(case)
    assert not case["broker"].calls


@pytest.mark.parametrize("seconds", [-1, 0, 179, 301])
def test_stale_or_unbounded_decision_never_claimed(case, seconds):
    case["candidate"] = replace(
        case["candidate"],
        valid_until=case["clock"]() + timedelta(seconds=seconds),
    )
    with pytest.raises(ValueError, match="fresh decision"):
        execute(case)
    assert not list(case["claim_directory"].iterdir())


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("broker", "environment", "sandbox"),
        ("stops", "environment", "sandbox"),
        ("broker", "account_id", "another"),
        ("stops", "account_id", "another"),
    ],
)
def test_wrong_account_or_environment_fails_before_claim(case, target, field, value):
    setattr(case[target], field, value)
    with pytest.raises(ValueError):
        execute(case)
    assert not list(case["claim_directory"].iterdir())


def test_lost_buy_response_never_reposts(case):
    case["broker"].crash = True
    with pytest.raises(TimeoutError):
        execute(case)
    assert execute(case)["state"] == "ALREADY_CLAIMED"
    assert len(case["broker"].calls) == 1 and not case["stops"].post_stop.called


def test_lost_stop_response_never_reposts(case):
    case["stops"].post_stop.side_effect = TimeoutError("ambiguous")
    with pytest.raises(TimeoutError):
        execute(case)
    assert execute(case)["state"] == "ALREADY_CLAIMED"
    assert len(case["broker"].calls) == 1 and case["stops"].post_stop.call_count == 1
    with pytest.raises(ValueError, match="never repeat"):
        once.recover_stop_only(**{k: v for k, v in case.items() if k not in {"policy", "clock"}})


def test_restored_empty_journal_cannot_repeat_buy(case, tmp_path):
    saved = tmp_path / "before.sqlite3"
    shutil.copyfile(case["journal"], saved)
    assert execute(case)["state"] == "FILLED_PROTECTED"
    shutil.copyfile(saved, case["journal"])
    assert execute(case)["state"] == "ALREADY_CLAIMED"
    assert len(case["broker"].calls) == 1


def test_missing_claim_but_dispatch_fence_cannot_rearm(case):
    execute(case)
    (case["claim_directory"] / "claim.json").unlink()
    with pytest.raises(ValueError, match="surviving fence"):
        execute(case)
    assert len(case["broker"].calls) == 1


def test_exact_stop_only_recovery_never_buys_again(case, monkeypatch):
    original = once.ProtectionJournal.activate

    def crash(*args, **kwargs):
        raise RuntimeError("process stopped after FILLED before child dispatch")

    monkeypatch.setattr(once.ProtectionJournal, "activate", crash)
    with pytest.raises(RuntimeError):
        execute(case)
    monkeypatch.setattr(once.ProtectionJournal, "activate", original)
    recovery = {k: v for k, v in case.items() if k not in {"policy", "clock"}}
    assert once.recover_stop_only(**recovery) == "ACTIVE"
    assert len(case["broker"].calls) == 1 and case["stops"].post_stop.call_count == 1
    with pytest.raises(ValueError):
        once.recover_stop_only(**recovery)


def test_rollback_protection_before_dispatch_cannot_repeat_stop(case):
    execute(case)
    with sqlite3.connect(case["protection_journal"]) as db:
        db.execute("UPDATE protection SET state='PLANNED',stop_id=NULL")
    with pytest.raises(ValueError, match="never repeat"):
        once.recover_stop_only(**{k: v for k, v in case.items() if k not in {"policy", "clock"}})
    assert case["stops"].post_stop.call_count == 1


def test_terminal_rejection_spends_attempt_without_stop(case):
    case["broker"].status = OrderStatus.REJECTED
    assert execute(case)["state"] == "ENTRY_NOT_FILLED"
    assert execute(case)["state"] == "ALREADY_CLAIMED"
    assert not case["stops"].post_stop.called


def test_partial_or_unknown_result_is_not_protected(case):
    case["broker"].lookup.side_effect = lambda r: replace(
        r, status=OrderStatus.CANCELLED, filled_lots=1
    )
    with pytest.raises(ValueError, match="ambiguous/partial"):
        execute(case)
    assert not case["stops"].post_stop.called
    assert execute(case)["state"] == "ALREADY_CLAIMED"


def test_submitted_stop_is_not_proof_without_broker_readback(case):
    case["stops"].post_stop.side_effect = lambda plan, rid: {
        "stopOrderId": "known-stop",
        "orderRequestId": rid,
    }
    with pytest.raises(ValueError, match="protection"):
        execute(case)
    assert case["stops"].post_stop.call_count == 1


@pytest.mark.parametrize("key", list(operator.FIXED))
def test_hard_settings_cannot_be_loosened(tmp_path, key):
    raw = dict(operator.FIXED, enabled=False)
    raw[key] = "wrong"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        operator.load_settings(path)


@pytest.mark.parametrize("enabled", ["true", 1, None])
def test_settings_enabled_is_strict_boolean(tmp_path, enabled):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(dict(operator.FIXED, enabled=enabled)))
    with pytest.raises(ValueError):
        operator.load_settings(path)


def test_disabled_operator_never_reads_account_or_calls_network(monkeypatch):
    monkeypatch.delenv("T_INVEST_PROD_TOKEN", raising=False)
    monkeypatch.delenv("T_INVEST_PROD_ACCOUNT_ID", raising=False)
    args = argparse.Namespace(action="run", settings=ROOT / "config/prod_long_once.json")
    assert operator.operate(args) == {"state": "DISABLED"}
    args.action = "config-check"
    assert operator.operate(args)["state"] == "CONFIG_VALID"


def grant(case):
    return dict(
        version=1,
        decision="OPERATOR_AUTHORIZED_PROD_LONG_ONCE",
        account_sha256=hashlib.sha256(case["candidate"].account_id.encode()).hexdigest(),
        release_sha256=case["release"],
        journal_uuid=registered_live_journal(case["journal"], case["candidate"].account_id),
        entry_lots=1,
        max_attempts_total=1,
        max_total_with_fees_rub="10000",
        native_stop_required=True,
        protect_manual_positions=True,
        reviewed_at=case["clock"]().isoformat(),
        expires_at=(case["clock"]() + timedelta(hours=1)).isoformat(),
        evidence_sha256={k: hashlib.sha256(k.encode()).hexdigest() for k in operator.EVIDENCE},
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("entry_lots", True),
        ("entry_lots", 2),
        ("max_attempts_total", 2),
        ("release_sha256", "changed"),
        ("account_sha256", "foreign"),
        ("journal_uuid", "other"),
        ("native_stop_required", False),
        ("protect_manual_positions", False),
        ("evidence_sha256", {}),
        ("max_total_with_fees_rub", "10001"),
        ("decision", "APPROVED_SUPERVISED_EXPERIMENT"),
    ],
)
def test_grant_is_not_interchangeable_with_manual_or_foreign_permission(case, field, value):
    raw = grant(case)
    raw[field] = value
    with pytest.raises(ValueError):
        operator.validate_grant(
            raw,
            account=case["candidate"].account_id,
            release=case["release"],
            journal_uuid=grant(case)["journal_uuid"],
            now=case["clock"](),
        )


def test_valid_grant_and_expiration(case):
    raw = grant(case)
    kw = dict(
        account=case["candidate"].account_id,
        release=case["release"],
        journal_uuid=raw["journal_uuid"],
        now=case["clock"](),
    )
    operator.validate_grant(raw, **kw)
    for hours in [-1, 25]:
        raw["expires_at"] = (case["clock"]() + timedelta(hours=hours)).isoformat()
        with pytest.raises(ValueError):
            operator.validate_grant(raw, **kw)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.invalid/PostOrder",
        "https://sandbox-invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1.OrdersService/PostOrder",
        operator.PROD_REST
        + "/tinkoff.public.invest.api.contract.v1.SandboxService/OpenSandboxAccount",
        operator.PROD_REST
        + "/tinkoff.public.invest.api.contract.v1.StopOrdersService/CancelStopOrder",
    ],
)
def test_prod_transport_does_not_allow_sandbox_or_unrelated_mutations(url):
    with pytest.raises(ValueError):
        operator.ProductionTransport().post(url, headers={}, payload={}, timeout_seconds=1)


def test_units_are_examples_not_automatically_installed():
    folder = ROOT / "deploy/ubuntu"
    assert not (folder / "moex-tinvest-long-once-prod.service").exists()
    assert not (folder / "moex-tinvest-long-once-prod.timer").exists()
    service = (folder / "moex-tinvest-long-once-prod.service.example").read_text()
    timer = (folder / "moex-tinvest-long-once-prod.timer.example").read_text()
    assert "ConditionPathExists=/etc/moex-tinvest-bot/prod-long-once-admission.json" in service
    assert "Persistent=false" in timer
    script = (ROOT / "scripts/ubuntu/run-long-once-prod.sh").read_text()
    assert '--runtime "${app}/config/prod_runtime.json"' in script
    assert "environment-set" not in script and "sandbox-cycle" not in script
