"""Durable native protection for one confirmed production entry; no entry/cancel API."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from .domain import Instrument
from .integrations.tinvest_sandbox import (
    JsonPostTransport,
    decimal_to_quotation,
    money_value_to_decimal,
)
from .live_execution import registered_live_journal
from .service_config import PROD_REST

PREFIX = "/tinkoff.public.invest.api.contract.v1."


@dataclass(frozen=True)
class ProtectionPlan:
    account_id: str
    entry_logical_id: str
    entry_request_id: str
    instrument: Instrument
    lots: int
    entry_price: Decimal
    stop_trigger: Decimal
    stop_limit: Decimal | None = None
    kind: str = "stop_loss_market"

    def validate(self) -> None:
        UUID(self.entry_logical_id)
        UUID(self.entry_request_id)
        if not self.account_id or type(self.lots) is not int or self.lots != 1:
            raise ValueError("one-lot production protection account required")
        if type(self.instrument.lot_size) is not int or self.instrument.lot_size <= 0:
            raise ValueError("instrument lot size must be a positive integer")
        if self.instrument.currency != "RUB" or self.instrument.asset_class != "share":
            raise ValueError("protection supports RUB shares only")
        prices = [self.entry_price, self.stop_trigger]
        if self.stop_limit is not None:
            prices.append(self.stop_limit)
        if any(not p.is_finite() or p <= 0 or p % self.instrument.tick_size for p in prices):
            raise ValueError("positive finite tick-aligned protection prices required")
        if any(money_value_to_decimal(decimal_to_quotation(p)) != p for p in prices):
            raise ValueError("protection prices must be exactly representable in broker nanos")
        if self.stop_trigger >= self.entry_price:
            raise ValueError("sell protection trigger must be below entry price")
        if self.kind not in {"stop_loss_market", "stop_limit"}:
            raise ValueError("unsupported protection kind")
        if (self.kind == "stop_limit") != (self.stop_limit is not None):
            raise ValueError("stop-limit price must match protection kind")
        if self.stop_limit is not None and self.stop_limit > self.stop_trigger:
            raise ValueError("sell stop-limit cannot exceed its trigger")

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), default=str, sort_keys=True).encode()
        ).hexdigest()

    @property
    def request_id(self) -> str:
        return str(
            uuid5(NAMESPACE_URL, "prod-protection:" + self.account_id + ":" + self.entry_request_id)
        )


class ProtectionBroker(Protocol):
    @property
    def account_id(self) -> str: ...

    @property
    def environment(self) -> str: ...

    def available_units(self, instrument: Instrument) -> int: ...
    def list_stops(self) -> tuple[Mapping[str, Any], ...]: ...
    def post_stop(self, plan: ProtectionPlan, request_id: str) -> Mapping[str, Any]: ...


class ProtectionJournal:
    def __init__(self, path: Path) -> None:
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                if db.execute("SELECT value FROM metadata WHERE key='format'").fetchone() != ("1",):
                    raise ValueError("invalid protection journal; restore, never recreate")
                db.execute(
                    "SELECT account,request_id,plan_hash,state,stop_id FROM protection LIMIT 0"
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT)")
            db.execute("INSERT OR IGNORE INTO metadata VALUES('format','1')")
            db.execute("""CREATE TABLE IF NOT EXISTS protection(
                account TEXT NOT NULL, request_id TEXT PRIMARY KEY, plan_hash TEXT NOT NULL,
                plan TEXT NOT NULL, live_uuid TEXT NOT NULL, state TEXT NOT NULL, stop_id TEXT)""")

    def prepare(self, plan: ProtectionPlan, entry_journal_path: Path) -> str:
        """Persist the immutable plan BEFORE entry BUY, without submitting anything."""
        plan.validate()
        identity = registered_live_journal(entry_journal_path, plan.account_id)
        if self.path.resolve() == entry_journal_path.resolve():
            raise ValueError("protection and entry journals must be separate")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT plan_hash,live_uuid FROM protection WHERE request_id=?", (plan.request_id,)
            ).fetchone()
            if existing:
                if existing[0] != plan.digest:
                    raise ValueError("protection plan changed; no replacement permitted")
                if existing[1] != identity:
                    raise ValueError("protection plan belongs to another enrollment")
                return plan.request_id
            if db.execute(
                "SELECT 1 FROM protection WHERE account=? LIMIT 1", (plan.account_id,)
            ).fetchone():
                raise ValueError("outstanding protection plan blocks another entry")
            db.execute(
                "INSERT INTO protection VALUES(?,?,?,?,?,'PLANNED',NULL)",
                (
                    plan.account_id,
                    plan.request_id,
                    plan.digest,
                    json.dumps(asdict(plan), default=str),
                    identity,
                ),
            )
        return plan.request_id

    def _row(self, plan: ProtectionPlan) -> tuple[str, str | None, str]:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT state,stop_id,live_uuid FROM protection "
                "WHERE account=? AND request_id=? AND plan_hash=?",
                (plan.account_id, plan.request_id, plan.digest),
            ).fetchone()
        if row is None:
            raise ValueError("persisted immutable protection plan missing")
        return str(row[0]), str(row[1]) if row[1] is not None else None, str(row[2])

    def _state(self, plan: ProtectionPlan, status: str, stop_id: str | None = None) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "UPDATE protection SET state=?,stop_id=? WHERE request_id=? AND plan_hash=?",
                (status, stop_id, plan.request_id, plan.digest),
            )

    def activate(
        self, plan: ProtectionPlan, entry_journal_path: Path, broker: ProtectionBroker
    ) -> str:
        plan.validate()
        status, stop_id, identity = self._row(plan)
        if broker.account_id != plan.account_id or broker.environment != "prod":
            raise ValueError("exact production protection broker required")
        if registered_live_journal(entry_journal_path, plan.account_id) != identity:
            raise ValueError("entry journal enrollment changed")
        if status != "PLANNED":
            return self.reconcile(plan, broker)
        with closing(
            sqlite3.connect(entry_journal_path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            parent = db.execute(
                "SELECT logical_id,state,record FROM actions WHERE account=? AND request_id=?",
                ("prod:" + plan.account_id, plan.entry_request_id),
            ).fetchone()
            rows = db.execute(
                "SELECT record FROM actions WHERE account=?", ("prod:" + plan.account_id,)
            ).fetchall()
        if parent is None:
            raise ValueError("persisted parent entry missing")
        record = json.loads(parent[2])
        entry = record["intent"]
        if (
            parent[0] != plan.entry_logical_id
            or parent[1] != "filled"
            or record["status"] != "filled"
            or record["filled_lots"] != 1
            or entry["lots"] != 1
            or entry["side"] != "buy"
            or entry["instrument"] != json.loads(json.dumps(asdict(plan.instrument), default=str))
            or Decimal(entry["limit_price"]) != plan.entry_price
        ):
            raise ValueError("parent must be the exact durable FILLED one-lot BUY")
        owned = 0
        for (encoded,) in rows:
            value = json.loads(encoded)
            if value["intent"]["instrument"]["uid"] == plan.instrument.uid:
                owned += (
                    (1 if value["intent"]["side"] == "buy" else -1)
                    * int(value["filled_lots"])
                    * int(value["intent"]["instrument"]["lot_size"])
                )
        units = plan.lots * plan.instrument.lot_size
        if owned < units or broker.available_units(plan.instrument) < units:
            raise ValueError("protection exceeds bot-owned or free broker units")
        if any(s.get("instrumentUid") == plan.instrument.uid for s in broker.list_stops()):
            raise ValueError("existing/manual stop requires reconciliation, never adoption")
        # Compare-and-swap is the account-independent single plan submission lock.
        with closing(sqlite3.connect(self.path)) as db, db:
            cursor = db.execute(
                "UPDATE protection SET state='POST_PENDING' WHERE request_id=? AND state='PLANNED'",
                (plan.request_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("protection submission already reserved")
        try:
            response = broker.post_stop(plan, plan.request_id)
        except Exception:
            self._state(plan, "UNKNOWN")
            raise
        returned = response.get("stopOrderId")
        if (
            not isinstance(returned, str)
            or not returned
            or response.get("orderRequestId") != plan.request_id
        ):
            self._state(
                plan, "UNKNOWN", returned if isinstance(returned, str) and returned else None
            )
            raise ValueError("stop POST response identity is incomplete")
        self._state(plan, "UNKNOWN", returned)
        return self.reconcile(plan, broker)

    def reconcile(self, plan: ProtectionPlan, broker: ProtectionBroker) -> str:
        status, stop_id, _ = self._row(plan)
        if broker.account_id != plan.account_id or broker.environment != "prod":
            raise ValueError("exact production protection broker required")
        if stop_id is None:
            return status  # UNKNOWN/PENDING never adopts a matching manual order or resubmits.
        self._state(plan, "UNKNOWN", stop_id)
        matches = [s for s in broker.list_stops() if s.get("stopOrderId") == stop_id]
        if len(matches) != 1 or not _verified_stop(plan, matches[0]):
            raise ValueError("known stop is absent or mismatched; protection is unverified")
        self._state(plan, "ACTIVE", stop_id)
        return "ACTIVE"


def _verified_stop(plan: ProtectionPlan, stop: Mapping[str, Any]) -> bool:
    expected = {
        "instrumentUid": plan.instrument.uid,
        "direction": "STOP_ORDER_DIRECTION_SELL",
        "currency": "rub",
        "orderType": "STOP_ORDER_TYPE_STOP_LOSS"
        if plan.kind == "stop_loss_market"
        else "STOP_ORDER_TYPE_STOP_LIMIT",
        "status": "STOP_ORDER_STATUS_ACTIVE",
    }
    if any(str(stop.get(k, "")).lower() != str(v).lower() for k, v in expected.items()):
        return False
    # StopOrder.exchange_order_type is documented as a take-profit child-order
    # field. STOP_LOSS / STOP_LIMIT already define market / limit semantics.
    child = stop.get("exchangeOrderType")
    expected_child = (
        "EXCHANGE_ORDER_TYPE_MARKET"
        if plan.kind == "stop_loss_market"
        else "EXCHANGE_ORDER_TYPE_LIMIT"
    )
    if child not in (None, "EXCHANGE_ORDER_TYPE_UNSPECIFIED", 0, "0", expected_child):
        return False
    if str(stop.get("lotsRequested")) != str(plan.lots):
        return False
    for name, expected_price in (("stopPrice", plan.stop_trigger), ("price", plan.stop_limit)):
        if expected_price is not None:
            value = stop.get(name)
            if (
                not isinstance(value, Mapping)
                or str(value.get("currency", "")).upper() != "RUB"
                or money_value_to_decimal(value) != expected_price
            ):
                return False
    return True


@dataclass
class NativeProtectionBroker:
    token: str
    account_id: str
    transport: JsonPostTransport
    timeout_seconds: float = 10
    environment: str = "prod"

    def __post_init__(self) -> None:
        if (
            not self.token.strip()
            or not self.account_id.strip()
            or self.timeout_seconds <= 0
            or self.environment != "prod"
        ):
            raise ValueError("fixed production protection credentials required")

    def _post(self, method: str, payload: Mapping[str, object]) -> Mapping[str, Any]:
        return self.transport.post(
            PROD_REST + PREFIX + method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )

    def available_units(self, instrument: Instrument) -> int:
        result = self._post("OperationsService/GetPositions", {"accountId": self.account_id})
        if result.get("limitsLoadingInProgress") is True:
            raise ValueError("position limits are loading")
        securities = result.get("securities", [])
        if not isinstance(securities, list) or any(not isinstance(s, Mapping) for s in securities):
            raise ValueError("invalid broker securities")
        matches = [s for s in securities if s.get("instrumentUid") == instrument.uid]
        if len(matches) != 1:
            raise ValueError("broker share position identity missing or duplicated")
        values = [matches[0].get("balance", 0), matches[0].get("blocked", 0)]
        if any(
            type(v) is not int
            and not (isinstance(v, str) and re.fullmatch(r"-?(0|[1-9][0-9]*)", v))
            for v in values
        ):
            raise ValueError("broker share quantities must be integers, not booleans/floats")
        units = int(values[0]) - int(values[1])
        if units < 0:
            raise ValueError("free broker units are negative")
        return units

    def list_stops(self) -> tuple[Mapping[str, Any], ...]:
        result = self._post(
            "StopOrdersService/GetStopOrders",
            {"accountId": self.account_id, "status": "STOP_ORDER_STATUS_ACTIVE"},
        )
        rows = result.get("stopOrders", [])
        if not isinstance(rows, list) or any(not isinstance(s, Mapping) for s in rows):
            raise ValueError("invalid broker stop orders")
        return tuple(rows)

    def post_stop(self, plan: ProtectionPlan, request_id: str) -> Mapping[str, Any]:
        plan.validate()
        if plan.account_id != self.account_id or request_id != plan.request_id:
            raise ValueError("stop submission account/idempotency mismatch")
        payload: dict[str, object] = {
            "accountId": self.account_id,
            "orderId": request_id,
            "instrumentId": plan.instrument.uid,
            "quantity": str(plan.lots),
            "direction": "STOP_ORDER_DIRECTION_SELL",
            "expirationType": "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL",
            "stopOrderType": "STOP_ORDER_TYPE_STOP_LOSS"
            if plan.kind == "stop_loss_market"
            else "STOP_ORDER_TYPE_STOP_LIMIT",
            "stopPrice": decimal_to_quotation(plan.stop_trigger),
            "priceType": "PRICE_TYPE_CURRENCY",
            "confirmMarginTrade": False,
        }
        if plan.stop_limit is not None:
            payload["price"] = decimal_to_quotation(plan.stop_limit)
        return self._post("StopOrdersService/PostStopOrder", payload)
