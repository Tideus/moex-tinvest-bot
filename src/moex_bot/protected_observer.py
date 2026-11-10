"""Read-only projection of explicitly protected manual futures, never execution input.

Keep broker quantities/prices/payments verbatim: futures quotes are not RUB equity.
Unknown instruments still reach the strict portfolio parser and block observation.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .integrations.tinvest_sandbox import (
    GET_INSTRUMENT_PATH,
    GET_OPERATIONS_BY_CURSOR_PATH,
    GET_ORDERS_PATH,
    GET_PORTFOLIO_PATH,
    GET_POSITIONS_PATH,
    JsonPostTransport,
)
from .service_config import PROD_REST


@dataclass(frozen=True)
class ProtectedExternalInstrument:
    uid: str
    secid: str
    instrument_type: str


@dataclass
class ProtectedObserverTransport:
    delegate: JsonPostTransport
    instruments: tuple[ProtectedExternalInstrument, ...]
    account_id: str
    positions: list[dict[str, Any]] = field(default_factory=list)
    operations: list[dict[str, Any]] = field(default_factory=list)
    _verified: bool = False

    def post(self, url: str, *, headers: Mapping[str, str],
             payload: Mapping[str, object], timeout_seconds: float) -> Mapping[str, Any]:
        allowed = {PROD_REST + path for path in (
            GET_PORTFOLIO_PATH, GET_POSITIONS_PATH, GET_ORDERS_PATH,
            GET_OPERATIONS_BY_CURSOR_PATH,
        )}
        if url not in allowed or payload.get("accountId") != self.account_id:
            raise ValueError("protected observer permits only bound production account reads")
        if not self._verified:
            for item in self.instruments:
                response = self.delegate.post(
                    PROD_REST + GET_INSTRUMENT_PATH, headers=headers,
                    payload={"idType": "INSTRUMENT_ID_TYPE_UID", "id": item.uid},
                    timeout_seconds=timeout_seconds,
                )
                instrument = response.get("instrument", {})
                if not isinstance(instrument, Mapping) or (
                    instrument.get("uid"), instrument.get("ticker"),
                    instrument.get("instrumentType"),
                ) != (item.uid, item.secid, item.instrument_type):
                    raise ValueError("protected external instrument identity mismatch")
            self._verified = True
        raw = self.delegate.post(url, headers=headers, payload=payload,
                                 timeout_seconds=timeout_seconds)
        if url == PROD_REST + GET_PORTFOLIO_PATH:
            return self._partition(raw, "positions", self.positions)
        if url == PROD_REST + GET_OPERATIONS_BY_CURSOR_PATH:
            return self._partition(raw, "items", self.operations)
        return raw  # cash, blocked money and *all* open orders remain unchanged

    def _partition(self, raw: Mapping[str, Any], key: str,
                   captured: list[dict[str, Any]]) -> Mapping[str, Any]:
        entries = raw.get(key, [])
        if not isinstance(entries, list):
            raise ValueError("protected observer response collection must be a list")
        by_uid = {item.uid: item for item in self.instruments}
        retained: list[Mapping[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError("protected observer response entry must be an object")
            item = by_uid.get(str(entry.get("instrumentUid", "")))
            if item is None:
                retained.append(entry)
                continue
            if entry.get("instrumentType", item.instrument_type) != item.instrument_type:
                raise ValueError("protected external response type mismatch")
            captured.append({"secid": item.secid, "instrument_uid": item.uid,
                             "instrument_type": item.instrument_type,
                             "protected": True, "execution_allowed": False,
                             "broker_record": dict(entry)})
        return {**raw, key: retained}

    def annotate(self, payload: dict[str, object]) -> None:
        payload.update({
            "source": "t_invest_prod_observer_raw", "execution_allowed": False,
            "protected_external_positions": self.positions,
            "protected_external_operations": self.operations,
            "turnover_scope": "verified_universe_only",
        })
