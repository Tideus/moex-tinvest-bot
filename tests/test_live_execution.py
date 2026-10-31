from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from moex_bot.domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from moex_bot.execution_journal import ExecutionJournal
from moex_bot.live_execution import (
    LiveHolding,
    LivePermission,
    LiveState,
    enroll_live_journal,
    execute_live_intent,
    intent_digest,
    policy_digest,
)
from moex_bot.prod_long import load_prod_long_policy

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "a" * 64


class FakeBroker:
    account_id = "real-iis"
    environment = "prod"

    def __init__(self, state: LiveState) -> None:
        self.state = state
        self.calls: list[OrderIntent] = []
        self.status = OrderStatus.FILLED
        self.crash = False

    def capture(self, intent: OrderIntent) -> LiveState:
        return self.state

    def submit(self, intent: OrderIntent) -> OrderRecord:
        self.calls.append(intent)
        if self.crash:
            raise TimeoutError("post response lost")
        return OrderRecord(
            intent,
            self.status,
            intent.lots if self.status is OrderStatus.FILLED else 0,
            "exchange-id",
        )

    def lookup(self, record: OrderRecord) -> OrderRecord:
        return replace(record, status=OrderStatus.UNKNOWN)


def _setup(tmp_path: Path) -> tuple:
    now = datetime(2026, 10, 30, 10, tzinfo=UTC)
    policy = replace(
        load_prod_long_policy(ROOT / "config/prod_long.json"), execution_stage="canary"
    )
    instrument = Instrument(
        "SBER",
        "uid-sber",
        "TQBR",
        10,
        Decimal(".01"),
        issuer_id="sber",
        sector="finance",
        risk_cluster="domestic",
    )
    intent = OrderIntent(
        str(uuid4()),
        instrument,
        Side.BUY,
        5,
        Decimal("100"),
        Decimal("5000"),
        "confirmed live test",
    )
    state = LiveState(
        "real-iis",
        "ACCOUNT_TYPE_TINKOFF_IIS",
        "ACCOUNT_STATUS_OPEN",
        "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS",
        now,
        now,
        instrument,
        True,
        now,
        Decimal("100"),
        Decimal("100.1"),
        Decimal("40000"),
        Decimal("160000"),
        Decimal("0"),
        Decimal("0"),
        Decimal("5005"),
        {},
        0,
        frozenset(),
    )
    permission = LivePermission(
        "real-iis",
        intent_digest(intent),
        policy_digest(policy),
        RELEASE,
        now - timedelta(seconds=1),
        now + timedelta(minutes=2),
        True,
    )
    broker = FakeBroker(state)
    kwargs = dict(
        intent=intent,
        policy=policy,
        broker=broker,
        permission=permission,
        release_sha256=RELEASE,
        journal_path=tmp_path / "live.sqlite3",
        clock=lambda: now,
    )
    enrollment = enroll_live_journal(kwargs["journal_path"], policy=policy, state=state, now=now)
    kwargs["permission"] = replace(permission, journal_uuid=enrollment)
    return broker, kwargs, now


def test_confirmed_live_order_has_durable_distinct_uuid_and_replay_is_skipped(
    tmp_path: Path,
) -> None:
    broker, kwargs, _ = _setup(tmp_path)
    result = execute_live_intent(**kwargs)
    assert result and result.status is OrderStatus.FILLED
    assert result.intent.order_request_id != kwargs["intent"].order_request_id
    assert execute_live_intent(**kwargs) is None
    assert len(broker.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "another-iis"),
        ("execution_admission_passed", False),
        ("intent_sha256", "wrong"),
        ("policy_sha256", "wrong"),
        ("release_sha256", "wrong"),
        ("expires_at", datetime(2026, 10, 29, tzinfo=UTC)),
    ],
)
def test_confirmation_is_bound_and_cannot_be_bypassed(
    tmp_path: Path, field: str, value: object
) -> None:
    broker, kwargs, _ = _setup(tmp_path)
    kwargs["permission"] = replace(kwargs["permission"], **{field: value})
    with pytest.raises(ValueError):
        execute_live_intent(**kwargs)
    assert not broker.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_type", "ACCOUNT_TYPE_TINKOFF"),
        ("access_level", "ACCOUNT_ACCESS_LEVEL_READ_ONLY"),
        ("account_status", "ACCOUNT_STATUS_CLOSED"),
        ("account_id", "wrong-iis"),
        ("cash_available", Decimal("5000")),
        ("estimated_total", Decimal("6000")),
        ("active_orders", 1),
        ("stop_uids", frozenset({"uid-sber"})),
        ("daily_pnl", Decimal("-600")),
        ("daily_turnover", Decimal("16000")),
        ("bid", Decimal("99")),
        ("trading_allowed", False),
        ("equity", Decimal("NaN")),
        ("book_at", datetime(2026, 10, 30, 9, tzinfo=UTC)),
    ],
)
def test_broker_risk_state_blocks_before_submit(tmp_path: Path, field: str, value: object) -> None:
    broker, kwargs, _ = _setup(tmp_path)
    broker.state = replace(broker.state, **{field: value})
    with pytest.raises(ValueError):
        execute_live_intent(**kwargs)
    assert not broker.calls


@pytest.mark.parametrize(
    "fault", ["protected", "manual_sell", "margin", "market", "notional", "tick"]
)
def test_manual_holdings_and_unsupported_orders_are_protected(tmp_path: Path, fault: str) -> None:
    broker, kwargs, _ = _setup(tmp_path)
    intent = kwargs["intent"]
    if fault == "protected":
        intent = replace(intent, instrument=replace(intent.instrument, secid="LKOH"))
        broker.state = replace(broker.state, instrument=intent.instrument)
    if fault == "manual_sell":
        intent = replace(intent, side=Side.SELL)
        holding = LiveHolding(intent.instrument, 100, 100, Decimal("100"))
        broker.state = replace(broker.state, positions={intent.instrument.uid: holding})
    if fault == "margin":
        intent = replace(intent, confirm_margin_trade=True)
    if fault == "market":
        intent = replace(intent, order_type="market")
    if fault == "notional":
        intent = replace(intent, notional=Decimal("4999"))
    if fault == "tick":
        intent = replace(intent, limit_price=Decimal("100.001"))
    kwargs["intent"] = intent
    kwargs["permission"] = replace(kwargs["permission"], intent_sha256=intent_digest(intent))
    with pytest.raises(ValueError):
        execute_live_intent(**kwargs)
    assert not broker.calls


@pytest.mark.parametrize(
    "status,crash",
    [(OrderStatus.UNKNOWN, False), (OrderStatus.ACCEPTED, False), (OrderStatus.UNKNOWN, True)],
)
def test_ambiguous_submit_blocks_even_another_plan_after_restart(
    tmp_path: Path,
    status: OrderStatus,
    crash: bool,
) -> None:
    broker, kwargs, _ = _setup(tmp_path)
    broker.status, broker.crash = status, crash
    if crash:
        with pytest.raises(TimeoutError):
            execute_live_intent(**kwargs)
    else:
        execute_live_intent(**kwargs)
    new_intent = replace(kwargs["intent"], order_request_id=str(uuid4()))
    kwargs["intent"] = new_intent
    kwargs["permission"] = replace(kwargs["permission"], intent_sha256=intent_digest(new_intent))
    with pytest.raises(ValueError, match="reconciliation"):
        execute_live_intent(**kwargs)
    assert len(broker.calls) == 1


def test_daily_attempt_budget_survives_restart(tmp_path: Path) -> None:
    broker, kwargs, now = _setup(tmp_path)
    seed_now = now - timedelta(seconds=4)
    journal = ExecutionJournal(kwargs["journal_path"], clock=lambda: seed_now)
    for index in range(2):
        seed_now = now - timedelta(seconds=4 - index * 2)
        intent = replace(kwargs["intent"], order_request_id=str(uuid4()))
        key = journal.reserve(
            "prod:real-iis",
            intent,
            f"old-{index}",
            seed_now - timedelta(seconds=1),
            residual_exit=False,
        )
        journal.record(
            "prod:real-iis",
            OrderRecord(replace(intent, order_request_id=key), OrderStatus.REJECTED),
        )
    with pytest.raises(ValueError, match="daily order-attempt"):
        execute_live_intent(**kwargs)
    assert not broker.calls


def test_observer_cannot_send_even_with_a_valid_confirmation(tmp_path: Path) -> None:
    broker, kwargs, _ = _setup(tmp_path)
    kwargs["policy"] = replace(kwargs["policy"], execution_stage="observer")
    with pytest.raises(ValueError, match="canary stage"):
        execute_live_intent(**kwargs)
    assert not broker.calls


def test_missing_or_different_journal_is_never_created_by_live_execution(tmp_path: Path) -> None:
    broker, kwargs, _ = _setup(tmp_path)
    kwargs["journal_path"] = tmp_path / "wrong-path.sqlite3"
    with pytest.raises(ValueError, match="restore it"):
        execute_live_intent(**kwargs)
    assert not kwargs["journal_path"].exists() and not broker.calls


def test_sandbox_journal_cannot_be_substituted_for_registered_live_ledger(tmp_path: Path) -> None:
    broker, kwargs, _ = _setup(tmp_path)
    kwargs["journal_path"] = tmp_path / "sandbox.sqlite3"
    ExecutionJournal(kwargs["journal_path"])
    with pytest.raises(ValueError, match="not enrolled"):
        execute_live_intent(**kwargs)
    assert not broker.calls


def test_post_reservation_delay_cannot_send_a_stale_order(tmp_path: Path) -> None:
    broker, kwargs, now = _setup(tmp_path)
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return now if calls < 4 else now + timedelta(seconds=21)

    kwargs["clock"] = clock
    with pytest.raises(ValueError, match="expired before submit"):
        execute_live_intent(**kwargs)
    assert not broker.calls
    # Non-delivery is proven because the failure branch precedes broker.submit.
    assert ExecutionJournal(kwargs["journal_path"]).unresolved_count() == 0


def test_last_moment_kill_switch_prevents_post(tmp_path: Path) -> None:
    broker, kwargs, _ = _setup(tmp_path)

    def revoked() -> None:
        raise ValueError("production kill-switch revoked approval")

    kwargs["before_submit"] = revoked
    with pytest.raises(ValueError, match="kill-switch"):
        execute_live_intent(**kwargs)
    assert not broker.calls
    assert ExecutionJournal(kwargs["journal_path"]).unresolved_count() == 0


def test_live_sell_is_limited_to_bot_units_even_when_manual_units_share_same_ticker(
    tmp_path: Path,
) -> None:
    broker, kwargs, now = _setup(tmp_path)
    intent = kwargs["intent"]
    journal = ExecutionJournal(kwargs["journal_path"], clock=lambda: now - timedelta(seconds=1))
    bought = replace(intent, lots=1, notional=Decimal("1000"))
    request = journal.reserve(
        "prod:real-iis", bought, "earlier", now - timedelta(seconds=2), residual_exit=False
    )
    journal.record(
        "prod:real-iis",
        OrderRecord(replace(bought, order_request_id=request), OrderStatus.FILLED, 1),
    )
    # Broker holds 110 units: 10 bot-owned and 100 manual. Selling 20 must fail.
    broker.state = replace(
        broker.state,
        positions={intent.instrument.uid: LiveHolding(intent.instrument, 110, 110, Decimal("100"))},
    )
    sold = replace(intent, order_request_id=str(uuid4()), side=Side.SELL,
                   lots=2, notional=Decimal("2000"))
    kwargs["intent"] = sold
    kwargs["permission"] = replace(kwargs["permission"], intent_sha256=intent_digest(sold))
    with pytest.raises(ValueError, match="touch manual holdings"):
        execute_live_intent(**kwargs)
    assert not broker.calls
