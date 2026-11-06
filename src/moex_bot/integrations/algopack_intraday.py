from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from http.client import IncompleteRead, RemoteDisconnected
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
BASE_URL = "https://apim.moex.com/iss/datashop/algopack/eq"


class AlgoPackReadError(RuntimeError):
    """Sanitized source/stage evidence after a failed read."""


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
    """Paginated read-only joins; latest for LONG, bounded history for intraday."""

    def __init__(
        self,
        token: str,
        transport: JsonGetTransport,
        *,
        timeout: float = 15,
        retry_attempts: int = 3,
        retry_backoff: float = 0.5,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token.strip():
            raise ValueError("MOEX_APIKEY is required for intraday ALGOPACK")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if not 1 <= retry_attempts <= 5 or retry_backoff < 0:
            raise ValueError("ALGOPACK retry policy is invalid")
        self.token = token.strip()
        self.transport = transport
        self.timeout = timeout
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff
        self.sleeper = sleeper

    @classmethod
    def from_environment(cls) -> AlgoPackIntradayAdapter:
        return cls(os.getenv("MOEX_APIKEY", ""), UrlLibJsonGetTransport())

    def latest(self, *, secids: Sequence[str], as_of: datetime) -> tuple[IntradaySuperCandle, ...]:
        return self.history(secids=secids, as_of=as_of, bars_per_secid=1)

    def history(
        self, *, secids: Sequence[str], as_of: datetime, bars_per_secid: int,
        invalid: list[InvalidBar] | None = None,
    ) -> tuple[IntradaySuperCandle, ...]:
        """Recover only a current-day decision window, never synthesize missing bars."""
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if type(bars_per_secid) is not int or not 1 <= bars_per_secid <= 288:
            raise ValueError("bars_per_secid must be an integer in [1, 288]")
        wanted = frozenset(secids)
        if not wanted:
            return ()
        local_day = as_of.astimezone(MOSCOW).date().isoformat()
        blocks = {
            name: self._day_rows(name, local_day, wanted)
            for name in ("tradestats", "orderstats", "obstats")
        }
        complete: list[IntradaySuperCandle] = []
        common = set(blocks["tradestats"]) & set(blocks["orderstats"]) & set(blocks["obstats"])
        keys_by_secid: dict[str, list[tuple[str, datetime]]] = {}
        for key in sorted(common):
            secid, observed_at = key
            if (observed_at > as_of.astimezone(MOSCOW)
                    or observed_at.astimezone(MOSCOW).date().isoformat() != local_day):
                continue
            keys_by_secid.setdefault(secid, []).append(key)
        keys = [key for group in keys_by_secid.values() for key in group[-bars_per_secid:]]
        for key in sorted(keys):
            trade = blocks["tradestats"][key]
            order = blocks["orderstats"][key]
            book = blocks["obstats"][key]
            try:
                bar = IntradaySuperCandle(
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
            except NumericFieldError as exc:
                if invalid is None:
                    raise
                dataset = ("obstats" if exc.field in {"spread_bbo", "imbalance_val"}
                           else "orderstats" if exc.field.startswith(("put_", "cancel_"))
                           else "tradestats")
                invalid.append(InvalidBar(key[0], key[1], dataset, exc.field, exc.reason))
                continue
            complete.append(bar)
        return tuple(complete)

    def _day_rows(
        self, dataset: str, day: str, wanted: frozenset[str]
    ) -> dict[tuple[str, datetime], Mapping[str, object]]:
        result: dict[tuple[str, datetime], Mapping[str, object]] = {}
        start = 0
        page_size = 1000
        while True:
            query = urlencode({"date": day, "start": start, "limit": page_size})
            try:
                payload = self._get_with_retry(f"{BASE_URL}/{dataset}.json?{query}")
            except (OSError, IncompleteRead) as exc:
                # No URL, response fragment or credential may enter logs/alerts.
                raise AlgoPackReadError(
                    f"api=ALGOPACK/shared stage={dataset} page={start} "
                    f"error={type(exc).__name__}; read failed; entries blocked"
                ) from None
            rows = _normalize(payload)
            if not rows:
                break
            for row in rows:
                secid = str(row.get("secid", "")).upper()
                if secid not in wanted:
                    continue
                moment = _moment(row)
                result[(secid, moment)] = row
            start += len(rows)
            if start > 100_000:
                raise ValueError("ALGOPACK pagination exceeded the safety limit")
        return result

    def _get_with_retry(self, url: str) -> object:
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return self.transport.get(
                    url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=self.timeout,
                )
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == self.retry_attempts:
                    raise
            except (TimeoutError, URLError, ConnectionError, IncompleteRead, RemoteDisconnected):
                if attempt == self.retry_attempts:
                    raise
            self.sleeper(self.retry_backoff * attempt)
        raise RuntimeError("unreachable ALGOPACK retry state")


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


@dataclass(frozen=True, slots=True)
class InvalidBar:
    secid: str
    observed_at: datetime
    dataset: str
    field: str
    reason: str


class NumericFieldError(ValueError):
    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"ALGOPACK {field}: {reason}")
        self.field = field
        self.reason = reason


def _decimal(row: Mapping[str, object], field: str) -> Decimal:
    value = row.get(field)
    if value is None:
        raise NumericFieldError(field, "null" if field in row else "missing")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise NumericFieldError(field, "not_numeric") from None
    if not result.is_finite():
        raise NumericFieldError(field, "nonfinite")
    return result
