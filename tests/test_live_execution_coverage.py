"""Boundary and recovery coverage for the manual production canary (no network)."""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_live_execution import _setup

from moex_bot.domain import OrderRecord, OrderStatus, Side
from moex_bot.execution_journal import ExecutionJournal
from moex_bot.live_execution import (
    LiveHolding,
    _authorize,
    _ledger_summary,
    enroll_live_journal,
    execute_live_intent,
    reconcile_live_journal,
    registered_live_journal,
)


def test_enrollment_refuses_overwrite_ineligible_or_unprotected(tmp_path: Path) -> None:
    broker, kw, now = _setup(tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        enroll_live_journal(kw["journal_path"], policy=kw["policy"], state=broker.state, now=now)
    with pytest.raises(ValueError, match="eligible fresh"):
        enroll_live_journal(
            tmp_path / "bad",
            policy=kw["policy"],
            state=replace(broker.state, active_orders=1),
            now=now,
        )
    holding = LiveHolding(kw["intent"].instrument, 10, 10, Decimal("100"))
    with pytest.raises(ValueError, match="explicitly protected"):
        enroll_live_journal(
            tmp_path / "manual",
            policy=kw["policy"],
            state=replace(broker.state, positions={"uid-sber": holding}),
            now=now,
        )
    with sqlite3.connect(kw["journal_path"]) as db:
        db.execute("DELETE FROM metadata WHERE key='live_journal_uuid'")
    with pytest.raises(ValueError, match="identity missing"):
        registered_live_journal(kw["journal_path"], broker.account_id)


@pytest.mark.parametrize(
    "fault",
    [
        "future",
        "identity",
        "distance",
        "capital",
        "metadata",
        "short",
        "manual",
        "owned_missing",
        "fee",
        "issuer",
        "sector",
        "risk cluster",
        "zero_owned",
        "sell",
        "old",
    ],
)
def test_authorization_boundaries(tmp_path: Path, fault: str) -> None:
    broker, kw, now = _setup(tmp_path)
    state, intent, policy = broker.state, kw["intent"], kw["policy"]
    owned = {}
    if fault == "future":
        state = replace(state, finished_at=now + timedelta(seconds=1))
    elif fault == "identity":
        state = replace(state, instrument=replace(intent.instrument, uid="changed"))
    elif fault == "distance":
        state = replace(state, bid=Decimal("110"), ask=Decimal("110.1"))
    elif fault == "fee":
        state = replace(state, estimated_total=Decimal("4999"))
    elif fault == "metadata":
        intent = replace(intent, instrument=replace(intent.instrument, sector="unknown"))
        state = replace(state, instrument=intent.instrument)
    elif fault in {"capital", "issuer", "sector", "risk cluster"}:
        units = 360 if fault == "capital" else (60 if fault == "issuer" else 160)
        instrument = intent.instrument
        if fault in {"sector", "risk cluster"}:
            instrument = replace(instrument, issuer_id="other")
        if fault == "risk cluster":
            instrument = replace(instrument, sector="other")
        holding = LiveHolding(instrument, units, units, Decimal("100"))
        state = replace(state, positions={instrument.uid: holding})
        owned = {instrument.uid: units}
    elif fault in {"short", "manual"}:
        holding = LiveHolding(
            intent.instrument, -10 if fault == "short" else 10, 10, Decimal("100")
        )
        state = replace(state, positions={intent.instrument.uid: holding})
    elif fault == "owned_missing":
        owned = {intent.instrument.uid: 10}
    elif fault == "zero_owned":
        owned = {intent.instrument.uid: 0}
    elif fault == "sell":
        intent = replace(intent, side=Side.SELL)
    elif fault == "old":
        state = replace(state, started_at=now - timedelta(seconds=21))
    if fault == "zero_owned":
        _authorize(intent, policy, state, owned, 0, Decimal(0), now)
    else:
        with pytest.raises(ValueError):
            _authorize(intent, policy, state, owned, 0, Decimal(0), now)


def test_reconcile_only_reads_existing_pending_and_rejects_sandbox(tmp_path: Path) -> None:
    broker, kw, _ = _setup(tmp_path)
    broker.status = OrderStatus.ACCEPTED
    execute_live_intent(**kw)
    assert reconcile_live_journal(kw["journal_path"], broker) == 1
    assert len(broker.calls) == 1
    broker.environment = "sandbox"
    with pytest.raises(ValueError, match="production broker"):
        reconcile_live_journal(kw["journal_path"], broker)


def test_journal_binding_and_short_ownership_ledger(tmp_path: Path) -> None:
    broker, kw, now = _setup(tmp_path)
    kw["permission"] = replace(kw["permission"], journal_uuid="wrong")
    with pytest.raises(ValueError, match="another production ledger"):
        execute_live_intent(**kw)
    journal = ExecutionJournal(kw["journal_path"], clock=lambda: now)
    intent = replace(kw["intent"], side=Side.SELL)
    key = journal.reserve("prod:real-iis", intent, "sell", now, residual_exit=False)
    journal.record(
        "prod:real-iis",
        OrderRecord(replace(intent, order_request_id=key), OrderStatus.FILLED, intent.lots),
    )
    with sqlite3.connect(journal.path) as db, pytest.raises(ValueError, match="short exposure"):
        _ledger_summary(db, "prod:real-iis", now)


def test_owned_units_can_be_sold_and_previous_day_is_not_daily_turnover(tmp_path: Path) -> None:
    broker, kw, now = _setup(tmp_path)
    intent = kw["intent"]
    owned = {intent.instrument.uid: 50}
    holding = LiveHolding(intent.instrument, 50, 50, Decimal("100"))
    state = replace(broker.state, positions={intent.instrument.uid: holding})
    _authorize(replace(intent, side=Side.SELL), kw["policy"], state, owned, 0, Decimal(0), now)
    journal = ExecutionJournal(kw["journal_path"], clock=lambda: now)
    key = journal.reserve("prod:real-iis", intent, "buy", now, residual_exit=False)
    journal.record(
        "prod:real-iis",
        OrderRecord(replace(intent, order_request_id=key), OrderStatus.FILLED, intent.lots),
    )
    with sqlite3.connect(journal.path) as db:
        assert _ledger_summary(db, "prod:real-iis", now + timedelta(days=1)) == (
            owned,
            0,
            Decimal(0),
        )
