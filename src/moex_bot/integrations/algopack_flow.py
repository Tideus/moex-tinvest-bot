from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from importlib import import_module
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from ..reporting import (
    ConcentrationSnapshot,
    EquityFlowSnapshot,
    FutoiGroupSnapshot,
    FutoiSnapshot,
    completed_window,
    normalized_groups,
)

MOSCOW = ZoneInfo("Europe/Moscow")


class RecordsFrame(Protocol):
    def to_dict(self, orient: str) -> list[dict[str, Any]]: ...


class AlgoPackTicker(Protocol):
    def tradestats(self, *, start: str, end: str, latest: bool = False) -> RecordsFrame: ...

    def futoi(self, *, start: str, end: str) -> RecordsFrame: ...

    def hi2(self, *, start: str, end: str, latest: bool = False) -> RecordsFrame: ...


TickerFactory = Callable[[str], AlgoPackTicker]


def _decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    return Decimal("0") if value is None else Decimal(str(value))


def _moment(row: Mapping[str, Any]) -> datetime:
    value = f"{row['tradedate']}T{row['tradetime']}"
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=MOSCOW) if result.tzinfo is None else result


class AlgoPackFlowAdapter:
    """Entitled read-only ALGOPACK adapter; no broker methods by design."""

    def __init__(self, ticker_factory: TickerFactory) -> None:
        self._ticker_factory = ticker_factory

    @classmethod
    def from_environment(cls) -> AlgoPackFlowAdapter:
        try:
            dotenv_module = import_module("dotenv")
            moexalgo_module = import_module("moexalgo")
        except ImportError as exc:  # pragma: no cover
            message = 'install optional integrations: pip install -e ".[integrations]"'
            raise RuntimeError(message) from exc
        cast(Callable[[], object], dotenv_module.load_dotenv)()
        token = os.getenv("MOEX_APIKEY", "").strip()
        if not token:
            raise RuntimeError("MOEX_APIKEY is required for ALGOPACK flow data")
        moexalgo_module.session.TOKEN = token
        constructor = cast(Callable[[str], AlgoPackTicker], moexalgo_module.Ticker)
        return cls(constructor)

    def equity_flow(
        self, *, secid: str, as_of: datetime, window_minutes: int = 60
    ) -> EquityFlowSnapshot:
        start_at, end_at = completed_window(as_of.astimezone(MOSCOW), window_minutes)
        rows = self._ticker_factory(secid).tradestats(
            start=start_at.date().isoformat(), end=end_at.date().isoformat()
        ).to_dict(orient="records")
        selected = [row for row in rows if start_at <= _moment(row) <= end_at]
        if not selected:
            raise ValueError(f"no completed TradeStats rows for {secid}")
        return EquityFlowSnapshot(
            secid=secid,
            window_start=start_at,
            window_end=end_at,
            buy_value=sum((_decimal(row, "val_b") for row in selected), Decimal("0")),
            sell_value=sum((_decimal(row, "val_s") for row in selected), Decimal("0")),
            intervals=len(selected),
        )

    def futoi(self, *, ticker: str, as_of: datetime) -> FutoiSnapshot:
        local = as_of.astimezone(MOSCOW)
        rows = self._ticker_factory(ticker).futoi(
            start=local.date().isoformat(), end=local.date().isoformat()
        ).to_dict(orient="records")
        if not rows:
            raise ValueError(f"no FUTOI rows for {ticker}")
        latest = max(_moment(row) for row in rows)
        selected = [row for row in rows if _moment(row) == latest]
        groups = []
        for row in selected:
            raw_short = _decimal(row, "pos_short")
            groups.append(
                FutoiGroupSnapshot(
                    group=str(row["clgroup"]),
                    net_contracts=_decimal(row, "pos"),
                    gross_long=abs(_decimal(row, "pos_long")),
                    gross_short=abs(raw_short),
                    long_participants=int(_decimal(row, "pos_long_num")),
                    short_participants=int(_decimal(row, "pos_short_num")),
                )
            )
        return FutoiSnapshot(ticker=ticker, observed_at=latest, groups=normalized_groups(groups))

    def concentration(self, *, secid: str, as_of: datetime) -> ConcentrationSnapshot:
        local = as_of.astimezone(MOSCOW)
        rows = self._ticker_factory(secid).hi2(
            start=local.date().isoformat(), end=local.date().isoformat(), latest=True
        ).to_dict(orient="records")
        if not rows:
            raise ValueError(f"no HI2 rows for {secid}")
        latest = max(_moment(row) for row in rows)
        metrics = {
            str(row["metric"]): _decimal(row, "value")
            for row in rows
            if _moment(row) == latest
        }
        return ConcentrationSnapshot(secid=secid, observed_at=latest, metrics=metrics)
