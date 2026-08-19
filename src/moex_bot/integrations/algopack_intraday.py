from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
BASE_URL = "https://apim.moex.com/iss/datashop/algopack/eq"


class JsonGetTransport(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> object: ...


class UrlLibJsonGetTransport:
    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> object:
        request = Request(url, headers=dict(headers), method="GET")
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed host
            return json.loads(response.read().decode("utf-8"))


@dataclass(frozen=True, slots=True)
class IntradaySuperCandle:
    secid: str
    observed_at: datetime
    close: Decimal
    vwap: Decimal
    buy_value: Decimal
    sell_value: Decimal
    put_buy_value: Decimal
    put_sell_value: Decimal
    cancel_buy_value: Decimal
    cancel_sell_value: Decimal
    spread_bbo: Decimal
    book_imbalance: Decimal


class AlgoPackIntradayAdapter:
    """Three batched latest-row reads; ALGOPACK remains read-only."""

    def __init__(self, token: str, transport: JsonGetTransport, *, timeout: float = 15) -> None:
        if not token.strip():
            raise ValueError("MOEX_APIKEY is required for intraday ALGOPACK")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.token = token.strip()
        self.transport = transport
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> AlgoPackIntradayAdapter:
        return cls(os.getenv("MOEX_APIKEY", ""), UrlLibJsonGetTransport())

    def latest(self, *, secids: Sequence[str], as_of: datetime) -> tuple[IntradaySuperCandle, ...]:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        wanted = frozenset(secids)
        if not wanted:
            return ()
        local_day = as_of.astimezone(MOSCOW).date().isoformat()
        blocks = {
            name: self._latest_rows(name, local_day, wanted)
            for name in ("tradestats", "orderstats", "obstats")
        }
        complete: list[IntradaySuperCandle] = []
        common = (
            set(blocks["tradestats"])
            & set(blocks["orderstats"])
            & set(blocks["obstats"])
        )
        for key in sorted(common):
            trade = blocks["tradestats"][key]
            order = blocks["orderstats"][key]
            book = blocks["obstats"][key]
            complete.append(
                IntradaySuperCandle(
                    secid=key[0],
                    observed_at=key[1],
                    close=_decimal(trade, "pr_close"),
                    vwap=_decimal(trade, "pr_vwap"),
                    buy_value=_decimal(trade, "val_b"),
                    sell_value=_decimal(trade, "val_s"),
                    put_buy_value=_decimal(order, "put_val_b"),
                    put_sell_value=_decimal(order, "put_val_s"),
                    cancel_buy_value=_decimal(order, "cancel_val_b"),
                    cancel_sell_value=_decimal(order, "cancel_val_s"),
                    spread_bbo=abs(_decimal(book, "spread_bbo")),
                    book_imbalance=_decimal(book, "imbalance_val"),
                )
            )
        return tuple(complete)

    def _latest_rows(
        self, dataset: str, day: str, wanted: frozenset[str]
    ) -> dict[tuple[str, datetime], Mapping[str, object]]:
        query = urlencode({"date": day, "latest": 1})
        payload = self.transport.get(
            f"{BASE_URL}/{dataset}.json?{query}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout,
        )
        rows = _normalize(payload)
        result: dict[tuple[str, datetime], Mapping[str, object]] = {}
        for row in rows:
            secid = str(row.get("secid", "")).upper()
            if secid not in wanted:
                continue
            moment = _moment(row)
            result[(secid, moment)] = row
        return result


def _normalize(payload: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(payload, Mapping):
        raise ValueError("ALGOPACK response must be an object")
    block = payload.get("data")
    if not isinstance(block, Mapping):
        raise ValueError("ALGOPACK response has no data block")
    columns = block.get("columns")
    data = block.get("data")
    if not isinstance(columns, list) or not isinstance(data, list):
        raise ValueError("ALGOPACK data block is malformed")
    names = [str(item).lower() for item in columns]
    rows: list[Mapping[str, object]] = []
    for values in data:
        if not isinstance(values, list) or len(values) != len(names):
            raise ValueError("ALGOPACK row does not match columns")
        rows.append(dict(zip(names, values, strict=True)))
    return tuple(rows)


def _moment(row: Mapping[str, object]) -> datetime:
    value = f"{row.get('tradedate', '')}T{row.get('tradetime', '')}"
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=MOSCOW) if result.tzinfo is None else result.astimezone(MOSCOW)


def _decimal(row: Mapping[str, object], field: str) -> Decimal:
    value = row.get(field)
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"ALGOPACK {field} must be finite")
    return result
