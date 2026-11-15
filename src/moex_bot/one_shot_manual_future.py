"""Reconcile the single approved, untouchable IIS future before an equity BUY."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .integrations.tinvest_sandbox import GET_INSTRUMENT_PATH, JsonPostTransport
from .observer_funding import number
from .protected_observer import ProtectedExternalInstrument
from .service_config import PROD_REST

KCG7 = ProtectedExternalInstrument("f870b159-01c1-4dd4-ab17-bbdeeff51717", "KCG7", "futures")
RAW_SOURCE = "t_invest_prod_long_once_reconciled"
SLEEVE_SOURCE = "t_invest_prod_observer_long_once_reconciled"


def _rows(response: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    rows = response.get(key, [])
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError(f"IIS: malformed {key}")
    return rows


def _integer(raw: object) -> int:
    # Protobuf int64 JSON: no implicit truncation, booleans or scientific notation.
    if type(raw) not in (int, str) or not re.fullmatch(r"-?[0-9]{1,19}", str(raw)):
        raise ValueError("IIS: invalid manual future contract count")
    return int(str(raw))


def _contracts(raw: object) -> int:
    if not isinstance(raw, Mapping) or not ({"units", "nano"} & raw.keys()):
        raise ValueError("IIS: missing manual future quantity")
    if _integer(raw.get("nano", 0)) != 0:
        raise ValueError("IIS: fractional manual future quantity")
    return _integer(raw.get("units", 0))


def require_collateral(funds: Mapping[str, object]) -> None:
    if number(funds.get("guarantee_rub")) <= 0:
        raise ValueError("IIS: open manual KCG7 requires positive RUB blockedGuarantee")


def verify_equity_turnover(row: Mapping[str, Any], trading_uids: frozenset[str]) -> None:
    # Futures payments may reflect margin rather than the notional of a share trade.
    # Keep non-trade entries (e.g. variation margin) in the account records.
    if row.get("type") in {
        "OPERATION_TYPE_BUY",
        "OPERATION_TYPE_SELL",
        "OPERATION_TYPE_BUY_CARD",
        "OPERATION_TYPE_SELL_CARD",
        "OPERATION_TYPE_BUY_MARGIN",
        "OPERATION_TYPE_SELL_MARGIN",
    } and (
        row.get("instrumentUid") not in trading_uids
        or row.get("instrumentType", "share") != "share"
    ):
        raise ValueError("IIS: cannot verify equity turnover after an external trade")


@dataclass
class ManualFutureCheck:
    account: str = field(repr=False)
    token: str = field(repr=False)
    transport: JsonPostTransport
    protected: tuple[ProtectedExternalInstrument, ...]
    trading_uids: frozenset[str]
    contracts: int = field(default=0, init=False)

    def verify_trade(self, row: Mapping[str, Any]) -> None:
        verify_equity_turnover(row, self.trading_uids)

    def equity_portfolio(
        self,
        portfolio: Mapping[str, Any],
        positions: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.contracts = 0
        if any(r.get("accountId", self.account) != self.account for r in (portfolio, positions)):
            raise ValueError("IIS: inconsistent broker account identity")
        if positions.get("limitsLoadingInProgress", False) is not False or _rows(
            positions, "options"
        ):
            raise ValueError("IIS: incomplete limits or unsupported options")
        for row in _rows(positions, "securities"):
            if row.get("instrumentUid") == KCG7.uid or row.get("ticker") == KCG7.secid:
                raise ValueError("IIS: manual future classified as security")
        shares, futures = [], []
        for row in _rows(portfolio, "positions"):
            kind = row.get("instrumentType")
            if kind == "futures":
                futures.append(row)
            elif (
                kind not in ("share", "currency")
                or row.get("instrumentUid") == KCG7.uid
                or row.get("ticker") == KCG7.secid
            ):
                raise ValueError("IIS: unsupported or inconsistent portfolio instrument type")
            else:
                shares.append(row)
        balances = _rows(positions, "futures")
        if not futures and not balances:
            if "totalAmountFutures" in portfolio:
                amount = portfolio["totalAmountFutures"]
                if (
                    not isinstance(amount, Mapping)
                    or not ({"units", "nano"} & amount.keys())
                    or number(amount.get("units", 0))
                    or number(amount.get("nano", 0))
                ):
                    raise ValueError("IIS: futures valuation without reconciled contracts")
            return portfolio
        if (
            len(futures) != 1
            or len(balances) != 1
            or KCG7 not in self.protected
            or KCG7.uid in self.trading_uids
        ):
            raise ValueError("IIS: unapproved, missing or duplicate manual future")
        p, b = futures[0], balances[0]
        if any(
            row.get("instrumentUid") != KCG7.uid or row.get("ticker", KCG7.secid) != KCG7.secid
            for row in (p, b)
        ):
            raise ValueError("IIS: unknown manual future identity")
        if any(k in p and k in b and (not p[k] or p[k] != b[k]) for k in ("figi", "positionUid")):
            raise ValueError("IIS: manual future position identifiers disagree")
        count = _contracts(p.get("quantity"))
        if (
            not count
            or count != _integer(b.get("balance"))
            or _integer(b.get("blocked", 0))
            or p.get("blocked", False) is not False
            or ("blockedLots" in p and _contracts(p["blockedLots"]) != 0)
        ):
            raise ValueError("IIS: KCG7 quantities disagree or contracts are blocked")
        raw = self.transport.post(
            PROD_REST + GET_INSTRUMENT_PATH,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            payload={"idType": "INSTRUMENT_ID_TYPE_UID", "id": KCG7.uid},
            timeout_seconds=10,
        ).get("instrument")
        if (
            not isinstance(raw, Mapping)
            or (raw.get("uid"), raw.get("ticker"), raw.get("instrumentType"))
            != (KCG7.uid, KCG7.secid, KCG7.instrument_type)
            or any(
                "classCode" in row
                and (not row["classCode"] or row["classCode"] != raw.get("classCode"))
                for row in (p, b)
            )
        ):
            raise ValueError("IIS: manual KCG7 broker metadata mismatch")
        self.contracts = count
        # Preserve account-wide NAV/P&L. Futures points are not cash or equity holdings.
        return {**portfolio, "positions": shares}
