"""Single-order production canary. Explicit per-order approval, no autonomous activation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from .domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from .execution_journal import ExecutionJournal
from .prod_long import ProdLongPolicy, validate_prod_long_policy

MOSCOW = ZoneInfo("Europe/Moscow")


def intent_digest(intent: OrderIntent) -> str:
    return hashlib.sha256(
        json.dumps(asdict(intent), default=str, sort_keys=True).encode()
    ).hexdigest()


def policy_digest(policy: ProdLongPolicy) -> str:
    return hashlib.sha256(
        json.dumps(
            asdict(policy),
            default=lambda x: sorted(x) if isinstance(x, frozenset) else str(x),
            sort_keys=True,
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class LivePermission:
    account_id: str
    intent_sha256: str
    policy_sha256: str
    release_sha256: str
    confirmed_at: datetime
    expires_at: datetime
    strategy_admission_passed: bool
    journal_uuid: str = ""

    def validate(
        self, account: str, intent: OrderIntent, policy: ProdLongPolicy, release: str, now: datetime
    ) -> None:
        if self.strategy_admission_passed is not True:
            raise ValueError("strategy admission has not passed")
        if self.account_id != account or self.intent_sha256 != intent_digest(intent):
            raise ValueError("exact account/order confirmation does not match")
        if self.policy_sha256 != policy_digest(policy) or self.release_sha256 != release:
            raise ValueError("approved policy/release changed")
        if (
            len(release) != 64
            or any(c not in "0123456789abcdef" for c in release)
            or self.confirmed_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or now.tzinfo is None
            or not self.confirmed_at <= now < self.expires_at
            or self.expires_at - self.confirmed_at > timedelta(minutes=5)
        ):
            raise ValueError("live confirmation is invalid or expired")


@dataclass(frozen=True)
class LiveHolding:
    instrument: Instrument
    units: int
    free_units: int
    price: Decimal


@dataclass(frozen=True)
class LiveState:
    account_id: str
    account_type: str
    account_status: str
    access_level: str
    started_at: datetime
    finished_at: datetime
    instrument: Instrument
    trading_allowed: bool
    book_at: datetime
    bid: Decimal
    ask: Decimal
    cash_available: Decimal
    equity: Decimal
    daily_pnl: Decimal
    daily_turnover: Decimal
    estimated_total: Decimal
    positions: dict[str, LiveHolding]
    active_orders: int
    stop_uids: frozenset[str]


class LiveBrokerPort(Protocol):
    account_id: str

    @property
    def environment(self) -> str: ...

    def capture(self, intent: OrderIntent) -> LiveState: ...

    def submit(self, intent: OrderIntent) -> OrderRecord: ...

    def lookup(self, record: OrderRecord) -> OrderRecord: ...


def enroll_live_journal(
    path: Path,
    *,
    policy: ProdLongPolicy,
    state: LiveState,
    now: datetime,
) -> str:
    """Explicit first enrollment only; never used by execute, reconcile, reset or update."""
    validate_prod_long_policy(policy)
    if path.exists():
        raise ValueError("production journal already exists; never overwrite/re-enroll it")
    if (
        state.account_type != policy.account_type
        or state.account_status != "ACCOUNT_STATUS_OPEN"
        or state.access_level != "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"
        or state.active_orders
        or state.started_at.tzinfo is None
        or now.tzinfo is None
        or not timedelta(0) <= now - state.started_at <= timedelta(seconds=20)
    ):
        raise ValueError("first enrollment requires eligible fresh broker state with no orders")
    if any(
        h.units < 0 or (h.units and h.instrument.secid not in policy.protected_secids)
        for h in state.positions.values()
    ):
        raise ValueError("all pre-existing holdings must be explicitly protected at enrollment")
    journal = ExecutionJournal(path, migration_required=True)
    identifier = str(uuid4())
    with closing(sqlite3.connect(journal.path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO metadata VALUES('live_account_id',?)", (state.account_id,))
        db.execute("INSERT INTO metadata VALUES('live_journal_uuid',?)", (identifier,))
        db.execute("INSERT INTO metadata VALUES('live_enrolled_at',?)", (now.isoformat(),))
        db.execute("UPDATE metadata SET value='ready' WHERE key='migration_state'")
    return identifier


def registered_live_journal(path: Path, account_id: str) -> str:
    if not ExecutionJournal.is_initialized(path):
        raise ValueError("production journal missing/corrupt; restore it, do not recreate")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("SELECT value FROM metadata WHERE key='live_account_id'").fetchone() != (
            account_id,
        ):
            raise ValueError("production journal is not enrolled for this account")
        identity = db.execute("SELECT value FROM metadata WHERE key='live_journal_uuid'").fetchone()
        if not identity:
            raise ValueError("production journal enrollment identity missing")
        return str(identity[0])


def _ledger_summary(
    db: sqlite3.Connection, account: str, now: datetime
) -> tuple[dict[str, int], int, Decimal]:
    owned: dict[str, int] = {}
    count = 0
    turnover = Decimal("0")
    today = now.astimezone(MOSCOW).date()
    for snapshot, encoded in db.execute(
        "SELECT snapshot_at,record FROM actions WHERE account=?", (account,)
    ):
        raw = json.loads(encoded)
        value = raw["intent"]
        uid = value["instrument"]["uid"]
        sign = 1 if value["side"] == "buy" else -1
        owned[uid] = owned.get(uid, 0) + sign * raw["filled_lots"] * value["instrument"]["lot_size"]
        if datetime.fromisoformat(snapshot).astimezone(MOSCOW).date() == today:
            count += 1  # Includes rejected/cancelled attempts: conservative daily attempt budget.
            turnover += Decimal(value["notional"])
    if any(units < 0 for units in owned.values()):
        raise ValueError("bot ownership ledger contains short exposure")
    return owned, count, turnover


def _authorize(
    intent: OrderIntent,
    policy: ProdLongPolicy,
    state: LiveState,
    owned: dict[str, int],
    count: int,
    reserved_turnover: Decimal,
    now: datetime,
) -> None:
    if (
        state.account_type != policy.account_type
        or state.account_status != "ACCOUNT_STATUS_OPEN"
        or state.access_level != "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"
    ):
        raise ValueError("broker account type/status/access is not eligible")
    if (
        any(x.tzinfo is None for x in (state.started_at, state.finished_at, state.book_at))
        or not timedelta(0) <= state.finished_at - state.started_at <= timedelta(seconds=20)
        or not timedelta(0) <= now - state.started_at <= timedelta(seconds=20)
        or not timedelta(0) <= now - state.book_at <= timedelta(seconds=10)
    ):
        raise ValueError("live broker/book state is stale or incoherent")
    if state.finished_at > now:
        raise ValueError("broker snapshot is from the future")
    if state.instrument != intent.instrument:
        raise ValueError("live instrument UID/lot/tick/metadata changed")
    instrument = intent.instrument
    if (
        instrument.asset_class != "share"
        or instrument.currency != "RUB"
        or intent.order_type != "limit"
        or intent.confirm_margin_trade
        or policy.canary.allow_margin
    ):
        raise ValueError("initial live canary permits RUB share limit orders without margin only")
    if instrument.secid not in policy.allowed_secids or instrument.secid in policy.protected_secids:
        raise ValueError("manual/protected or non-allowlisted instrument")
    if not intent.limit_price.is_finite() or intent.limit_price % instrument.tick_size:
        raise ValueError("limit price does not match tick size")
    notional = intent.limit_price * intent.lots * instrument.lot_size
    if intent.notional != notional or notional > policy.canary.max_order_notional_rub:
        raise ValueError("live order notional is inconsistent or exceeds limit")
    if (
        not state.trading_allowed
        or not all(x.is_finite() and x > 0 for x in (state.bid, state.ask))
        or state.ask < state.bid
        or (state.ask / state.bid - 1) > Decimal("0.005")
    ):
        raise ValueError("trading status or order book spread is unacceptable")
    mid = (state.ask + state.bid) / 2
    if abs(intent.limit_price / mid - 1) > Decimal("0.01"):
        raise ValueError("confirmed limit price is too far from current order book")
    if state.active_orders or instrument.uid in state.stop_uids:
        raise ValueError("active regular/protective orders require reconciliation")
    if count >= policy.canary.max_orders_per_day:
        raise ValueError("persistent daily order-attempt limit reached")
    if (
        max(state.daily_turnover, reserved_turnover) + notional
        > policy.canary.max_daily_turnover_rub
    ):
        raise ValueError("persistent daily turnover limit reached")
    numeric = (
        state.cash_available,
        state.equity,
        state.daily_pnl,
        state.daily_turnover,
        state.estimated_total,
    )
    if any(not x.is_finite() for x in numeric) or state.equity <= 0 or state.daily_turnover < 0:
        raise ValueError("live monetary state is invalid")
    gross = Decimal("0")
    for uid, units in owned.items():
        if units == 0:
            continue
        holding = state.positions.get(uid)
        if (
            holding is None
            or holding.units < units
            or holding.free_units < units
            or not holding.price.is_finite()
            or holding.price <= 0
        ):
            raise ValueError("bot-owned holdings disagree with broker positions")
        gross += units * holding.price
    for uid, holding in state.positions.items():
        if holding.units < 0:
            raise ValueError("existing account short exposure is unsupported by canary")
        if (
            holding.units
            and holding.instrument.secid not in policy.protected_secids
            and not owned.get(uid)
        ):
            raise ValueError("pre-existing manual position requires explicit protection")
    units = intent.lots * instrument.lot_size
    if intent.side is Side.SELL:
        holding = state.positions.get(instrument.uid)
        if units > owned.get(instrument.uid, 0) or holding is None or units > holding.free_units:
            raise ValueError("sell would touch manual holdings or create a short")
    else:
        # Whole-account loss can block new risk; the canary-sized cap makes this conservative.
        loss_budget = min(state.equity, policy.canary.capital_limit_rub) * (
            policy.canary.max_account_daily_loss_weight
        )
        if state.daily_pnl <= -loss_budget:
            raise ValueError("daily loss gate blocks new exposure")
        if state.estimated_total < notional or state.estimated_total > notional * Decimal("1.005"):
            raise ValueError("broker fee estimate is missing or exceeds reserve")
        if state.estimated_total > state.cash_available:
            raise ValueError("insufficient free broker cash including estimated fees")
        if gross + state.estimated_total > policy.canary.capital_limit_rub:
            raise ValueError("40000 RUB canary capital limit exceeded")
        if instrument.sector == "unknown" or instrument.risk_cluster == "unknown":
            raise ValueError("live diversification metadata is incomplete")
        for label, field, weight in (
            ("issuer", "issuer_id", Decimal("0.25")),
            ("sector", "sector", Decimal("0.50")),
            ("risk cluster", "risk_cluster", Decimal("0.50")),
        ):
            exposure = sum(
                (
                    owned.get(uid, 0) * holding.price
                    for uid, holding in state.positions.items()
                    if getattr(holding.instrument, field) == getattr(instrument, field)
                ),
                Decimal("0"),
            )
            if exposure + notional > policy.canary.capital_limit_rub * weight:
                raise ValueError(f"projected {label} concentration exceeds canary limit")


def execute_live_intent(
    *,
    intent: OrderIntent,
    policy: ProdLongPolicy,
    broker: LiveBrokerPort,
    permission: LivePermission,
    release_sha256: str,
    journal_path: Path,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    before_submit: Callable[[], None] | None = None,
) -> OrderRecord | None:
    if broker.environment != "prod" or policy.execution_stage != "canary":
        raise ValueError("live canary stage and production broker are required")
    validate_prod_long_policy(policy)
    permission.validate(broker.account_id, intent, policy, release_sha256, clock())
    journal_uuid = registered_live_journal(journal_path, broker.account_id)
    if permission.journal_uuid != journal_uuid:
        raise ValueError("exact order confirmation belongs to another production ledger")
    journal = ExecutionJournal(journal_path, clock=clock)
    scope = "prod:" + broker.account_id

    captured: list[LiveState] = []

    def authorize(db: sqlite3.Connection) -> datetime:
        state = broker.capture(intent)
        now = clock()
        permission.validate(broker.account_id, intent, policy, release_sha256, now)
        if state.account_id != broker.account_id:
            raise ValueError("fresh broker state belongs to another account")
        owned, count, turnover = _ledger_summary(db, scope, now)
        _authorize(intent, policy, state, owned, count, turnover, now)
        captured.append(state)
        return state.started_at

    request_id = journal.reserve(
        scope,
        intent,
        intent.order_request_id,
        clock(),
        residual_exit=False,
        authorize=authorize,
        broker_request_id=str(uuid5(NAMESPACE_URL, scope + ":" + intent.order_request_id)),
    )
    if request_id is None:
        return None
    sent = replace(intent, order_request_id=request_id)
    try:
        if before_submit is not None:
            before_submit()
        now = clock()
        permission.validate(broker.account_id, intent, policy, release_sha256, now)
        state = captured[0]
        if not timedelta(0) <= now - state.started_at <= timedelta(seconds=20) or not timedelta(
            0
        ) <= now - state.book_at <= timedelta(seconds=10):
            raise ValueError("snapshot expired before submit; reserved order was not submitted")
    except (OSError, ValueError) as exc:
        # This branch is strictly before the first submit call, so non-delivery is provable.
        journal.record(
            scope, OrderRecord(sent, OrderStatus.REJECTED, status_detail=f"not_submitted:{exc}")
        )
        raise
    record = broker.submit(sent)
    journal.record(scope, record)
    return record  # Accepted/partial/UNKNOWN remain a durable account-wide blocker.


def reconcile_live_journal(journal_path: Path, broker: LiveBrokerPort) -> int:
    from .sandbox_execution import decode_validated_intent

    scope = "prod:" + broker.account_id
    if broker.environment != "prod":
        raise ValueError("production reconciliation requires a production broker")
    registered_live_journal(journal_path, broker.account_id)
    journal = ExecutionJournal(journal_path)
    for raw in journal.unresolved(scope):
        intent = decode_validated_intent({"status": "validated", "intent": raw["intent"]})
        record = OrderRecord(
            intent,
            OrderStatus(str(raw["status"])),
            int(str(raw["filled_lots"])),
            str(raw["broker_order_id"]) if raw["broker_order_id"] else None,
        )
        journal.record(scope, broker.lookup(record))
    return len(journal.unresolved(scope))
