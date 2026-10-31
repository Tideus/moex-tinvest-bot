"""Supervised execution tests have durable local ledgers and fake broker IO only."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from itertools import count
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid5

import pytest
from test_live_execution import _setup
from test_live_protection import _stop

from moex_bot import supervised_execution as module
from moex_bot.domain import OrderRecord, OrderStatus, Side
from moex_bot.live_execution import intent_digest
from moex_bot.live_protection import ProtectionJournal, ProtectionPlan


@pytest.fixture
def case(tmp_path):
    broker, kw, now = _setup(tmp_path)
    intent = replace(kw["intent"], lots=1, notional=Decimal("1000"))
    permission = replace(
        kw["permission"], intent_sha256=intent_digest(intent), expires_at=now + timedelta(minutes=4)
    )
    broker.state = replace(broker.state, estimated_total=Decimal("1001"))
    request = str(uuid5(NAMESPACE_URL, "prod:real-iis:" + intent.order_request_id))
    plan = ProtectionPlan(
        "real-iis",
        intent.order_request_id,
        request,
        intent.instrument,
        1,
        Decimal("100"),
        Decimal("95"),
        None,
        "stop_loss_market",
    )
    stop = Mock(account_id="real-iis", environment="prod")
    stop.available_units.return_value = 10
    stop.list_stops.return_value = ()

    def post(protection, request_id):
        stop.list_stops.return_value = (_stop(protection),)
        return {"stopOrderId": "known-stop", "orderRequestId": request_id}

    stop.post_stop.side_effect = post
    broker.cancel_pending = Mock()
    original_submit = broker.submit

    def submit(entry):
        assert ProtectionJournal(tmp_path / "protection.sqlite3")._row(plan)[0] == "PLANNED"
        assert entry.order_request_id == plan.entry_request_id
        return original_submit(entry)

    broker.submit = submit
    approval = Mock()
    return dict(
        intent=intent,
        policy=kw["policy"],
        permission=permission,
        release=kw["release_sha256"],
        journal_path=kw["journal_path"],
        protection_path=tmp_path / "protection.sqlite3",
        protection=plan,
        confirmed_protection_sha256=plan.digest,
        broker=broker,
        stop_broker=stop,
        approval_gate=approval,
        clock=lambda: now,
        monotonic=lambda: 0,
        sleeper=Mock(),
    )


def test_fill_is_protected_and_replay_does_not_resubmit(case):
    assert module.run_supervised_entry(**case) == {
        "state": "FILLED_PROTECTED",
        "filled_lots": 1,
        "protection": "ACTIVE",
    }
    assert case["approval_gate"].call_count == 3
    assert module.run_supervised_entry(**case)["state"] == "FILLED_PROTECTED"
    assert len(case["broker"].calls) == 1
    assert case["stop_broker"].post_stop.call_count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("side", Side.SELL),
        ("lots", 2),
        ("lots", True),
        ("instrument", None),
        ("limit_price", Decimal("99")),
    ],
)
def test_paired_entry_must_match_exactly(case, field, value):
    case["intent"] = replace(case["intent"], **{field: value})
    with pytest.raises(ValueError, match="one-lot paired"):
        module.run_supervised_entry(**case)
    assert not case["broker"].calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("confirmed_protection_sha256", "wrong"),
        ("wait_seconds", True),
        ("wait_seconds", 0),
        ("wait_seconds", 31),
    ],
)
def test_confirmation_digest_and_poll_bounds_fail_closed(case, field, value):
    case[field] = value
    with pytest.raises(ValueError, match="one-lot paired"):
        module.run_supervised_entry(**case)


@pytest.mark.parametrize("change", ["account", "logical_id"])
def test_protection_cannot_target_another_parent_or_account(case, change):
    if change == "account":
        case["broker"].account_id = "other"
    else:
        case["intent"] = replace(
            case["intent"], order_request_id=str(uuid5(NAMESPACE_URL, "other"))
        )
    with pytest.raises(ValueError, match="one-lot paired"):
        module.run_supervised_entry(**case)


def test_copied_protection_request_uuid_is_rejected_before_any_buy(case):
    case["protection"] = replace(
        case["protection"], entry_request_id=str(uuid5(NAMESPACE_URL, "copied"))
    )
    case["confirmed_protection_sha256"] = case["protection"].digest
    with pytest.raises(ValueError, match="one-lot paired"):
        module.run_supervised_entry(**case)
    assert not case["broker"].calls
    assert not case["stop_broker"].post_stop.call_count


@pytest.mark.parametrize("field,value", [("account_id", "other"), ("environment", "sandbox")])
def test_incorrect_protection_adapter_blocks_entry_before_buy(case, field, value):
    setattr(case["stop_broker"], field, value)
    with pytest.raises(ValueError, match="one-lot paired"):
        module.run_supervised_entry(**case)
    assert not case["broker"].calls


def test_read_parent_requires_existing_durable_entry(case):
    with pytest.raises(ValueError, match="parent order is missing"):
        module.read_parent(case["journal_path"], case["protection"])


@pytest.mark.parametrize("outcome", ["fill", "cancel", "reject", "ambiguous"])
def test_pending_order_is_polled_then_only_that_entry_may_be_cancelled(case, outcome):
    case["broker"].status = OrderStatus.ACCEPTED
    statuses = (
        [OrderStatus.FILLED]
        if outcome == "fill"
        else [
            OrderStatus.ACCEPTED,
            OrderStatus.ACCEPTED,
            {
                "cancel": OrderStatus.CANCELLED,
                "reject": OrderStatus.REJECTED,
                "ambiguous": OrderStatus.UNKNOWN,
            }[outcome],
        ]
    )
    iterator = iter(statuses)

    def lookup(record):
        status = next(iterator)
        return replace(record, status=status, filled_lots=1 if status is OrderStatus.FILLED else 0)

    case["broker"].lookup = Mock(side_effect=lookup)
    times = count()
    case.update(monotonic=lambda: next(times), wait_seconds=3)
    if outcome == "ambiguous":
        with pytest.raises(ValueError, match="CRITICAL"):
            module.run_supervised_entry(**case)
    else:
        result = module.run_supervised_entry(**case)
        assert result["state"] == ("FILLED_PROTECTED" if outcome == "fill" else "ENTRY_NOT_FILLED")
    assert case["broker"].cancel_pending.call_count == int(outcome != "fill")
    assert len(case["broker"].calls) == 1


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_approval_revocation_checked_before_entry_and_stop(case, stage):
    calls = count(1)

    def gate():
        if next(calls) == stage:
            raise ValueError("review expired")

    case["approval_gate"] = gate
    with pytest.raises(ValueError, match="review expired"):
        module.run_supervised_entry(**case)
    assert len(case["broker"].calls) == int(stage == 3)
    assert not case["stop_broker"].post_stop.call_count


def test_unknown_stop_never_claims_active_or_retries_buy(case):
    case["stop_broker"].post_stop.side_effect = TimeoutError("lost response")
    with pytest.raises(TimeoutError):
        module.run_supervised_entry(**case)
    assert ProtectionJournal(case["protection_path"])._row(case["protection"])[0] == "UNKNOWN"
    with pytest.raises(ValueError, match="lacks verified"):
        module.run_supervised_entry(**case)
    assert len(case["broker"].calls) == 1
    assert case["stop_broker"].post_stop.call_count == 1


def test_nonplanned_protection_cannot_precede_a_new_entry(case, monkeypatch):
    original = ProtectionJournal.prepare

    def changed(self, plan, entry):
        result = original(self, plan, entry)
        self._state(plan, "UNKNOWN")
        return result

    monkeypatch.setattr(ProtectionJournal, "prepare", changed)
    with pytest.raises(ValueError, match="original planned"):
        module.run_supervised_entry(**case)
    assert not case["broker"].calls


def test_approval_headroom_is_checked_before_preparing_any_entry(case):
    case["permission"] = replace(
        case["permission"], expires_at=case["clock"]() + timedelta(seconds=179)
    )
    with pytest.raises(ValueError, match="180 seconds"):
        module.run_supervised_entry(**case)
    assert not case["broker"].calls
    assert not case["protection_path"].exists()


def test_slow_capture_cannot_consume_reserved_protection_time(case):
    now = case["clock"]()
    current = [now]
    case["clock"] = lambda: current[0]

    def capture(intent):
        current[0] = now + timedelta(seconds=65)
        return replace(
            case["broker"].state, started_at=current[0], finished_at=current[0], book_at=current[0]
        )

    case["broker"].capture = capture
    with pytest.raises(ValueError, match="time reserved"):
        module.run_supervised_entry(**case)
    assert not case["broker"].calls


@pytest.mark.parametrize("revoked", [True, False])
def test_extra_entry_review_gate_runs_only_before_buy(case, revoked):
    gate = Mock(side_effect=ValueError("entry review revoked") if revoked else None)
    case["entry_approval_gate"] = gate
    if revoked:
        with pytest.raises(ValueError, match="entry review revoked"):
            module.run_supervised_entry(**case)
        assert not case["broker"].calls
    else:
        assert module.run_supervised_entry(**case)["state"] == "FILLED_PROTECTED"
    gate.assert_called_once_with()


@pytest.mark.parametrize("fault", ["missing", "request_id", "terminal", "naive", "none"])
def test_production_cancel_only_accepts_verified_pending_exchange_id(case, fault):
    transport = Mock()
    transport.post.return_value = {
        "time": "2026-10-30T10:00:00" + ("Z" if fault != "naive" else "")
    }
    broker = module.SupervisedTInvestBroker("fake", "real-iis", transport, {})
    record = OrderRecord(
        case["intent"],
        OrderStatus.ACCEPTED,
        0,
        None
        if fault == "missing"
        else case["intent"].order_request_id
        if fault == "request_id"
        else "exchange-id",
    )
    if fault == "terminal":
        record = replace(record, status=OrderStatus.CANCELLED)
    if fault == "none":
        broker.cancel_pending(record)
        assert transport.post.call_args.kwargs["payload"]["orderId"] == "exchange-id"
    else:
        with pytest.raises(ValueError):
            broker.cancel_pending(record)
    assert transport.post.call_count == int(fault in {"none", "naive"})
