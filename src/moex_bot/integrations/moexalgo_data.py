from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from importlib import import_module
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from ..domain import Instrument, MarketObservation

MOSCOW = ZoneInfo("Europe/Moscow")


class RecordsFrame(Protocol):
    def to_dict(self, orient: str) -> list[dict[str, Any]]: ...


class TickerClient(Protocol):
    def candles(
        self,
        *,
        start: str,
        end: str,
        period: str,
    ) -> RecordsFrame: ...


TickerFactory = Callable[[str, str], TickerClient]
MetadataFactory = Callable[[str, str], Mapping[str, Any]]


def _field(row: Mapping[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None:
            return value
    raise ValueError(f"required MOEX field is missing: {names[0]}")


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=MOSCOW)
    return result


def _volatility(closes: Sequence[Decimal]) -> Decimal:
    if len(closes) < 3:
        return Decimal("0")
    returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
    mean = sum(returns, start=Decimal("0")) / Decimal(len(returns))
    variance = sum(((item - mean) ** 2 for item in returns), start=Decimal("0")) / Decimal(
        len(returns)
    )
    return variance.sqrt()


class MoexAlgoReadOnlyAdapter:
    """Read-only completed-candle adapter; it contains no order methods."""

    def __init__(
        self,
        ticker_factory: TickerFactory,
        metadata_factory: MetadataFactory,
    ) -> None:
        self._ticker_factory = ticker_factory
        self._metadata_factory = metadata_factory

    @classmethod
    def from_environment(cls, *, require_token: bool = False) -> MoexAlgoReadOnlyAdapter:
        try:
            dotenv_module = import_module("dotenv")
            moexalgo_module = import_module("moexalgo")
        except ImportError as exc:  # pragma: no cover - exercised by preflight in minimal installs
            message = 'install optional integrations: pip install -e ".[integrations]"'
            raise RuntimeError(message) from exc

        load_dotenv = cast(Callable[[], object], dotenv_module.load_dotenv)
        ticker_constructor = cast(Callable[..., object], moexalgo_module.Ticker)
        market_constructor = cast(Callable[..., Any], moexalgo_module.Market)
        load_dotenv()
        token = os.getenv("MOEX_APIKEY", "").strip()
        if require_token and not token:
            raise RuntimeError("MOEX_APIKEY is required for entitled ALGOPACK data")
        if token:
            moexalgo_module.session.TOKEN = token

        def factory(secid: str, board: str) -> TickerClient:
            return cast(TickerClient, ticker_constructor(secid, board=board))

        def metadata(secid: str, board: str) -> Mapping[str, Any]:
            frame = market_constructor("EQ", board=board).tickers(
                "ticker", "board", "lotsize", "minstep"
            )
            rows = cast(RecordsFrame, frame).to_dict(orient="records")
            matches = [
                row
                for row in rows
                if str(_field(row, "ticker")) == secid and str(_field(row, "board")) == board
            ]
            if len(matches) != 1:
                raise ValueError(f"expected one securities row for {secid}/{board}")
            return matches[0]

        return cls(factory, metadata)

    def hourly_observation(
        self,
        *,
        secid: str,
        uid: str,
        board: str,
        as_of: datetime,
        trend_window: int = 20,
        momentum_window: int = 5,
        lookback_days: int = 30,
    ) -> MarketObservation:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if trend_window < 2 or momentum_window < 1:
            raise ValueError("invalid signal windows")
        ticker = self._ticker_factory(secid, board)
        info = self._metadata_factory(secid, board)
        instrument = Instrument(
            secid=secid,
            uid=uid,
            board=board,
            lot_size=int(_field(info, "lotsize", "lot_size")),
            tick_size=Decimal(str(_field(info, "minstep", "min_step"))),
        )
        local_as_of = as_of.astimezone(MOSCOW)
        start = (local_as_of - timedelta(days=lookback_days)).date().isoformat()
        end = local_as_of.date().isoformat()
        candle_rows = ticker.candles(start=start, end=end, period="1h").to_dict(orient="records")
        completed: list[tuple[datetime, Decimal]] = []
        for row in candle_rows:
            candle_end = _timestamp(_field(row, "end"))
            if candle_end <= as_of.astimezone(candle_end.tzinfo):
                completed.append((candle_end, Decimal(str(_field(row, "close")))))
        completed.sort(key=lambda item: item[0])
        minimum = max(trend_window, momentum_window + 1)
        if len(completed) < minimum:
            raise ValueError(
                f"insufficient completed candles for {secid}: {len(completed)} < {minimum}"
            )
        closes = [item[1] for item in completed]
        price = closes[-1]
        trend = sum(closes[-trend_window:], start=Decimal("0")) / Decimal(trend_window)
        momentum = price / closes[-(momentum_window + 1)] - Decimal("1")
        volatility_window = closes[-min(len(closes), trend_window + 1) :]
        return MarketObservation(
            instrument=instrument,
            price=price,
            trend=trend,
            momentum=momentum,
            volatility=_volatility(volatility_window),
            observed_at=completed[-1][0],
            complete=True,
            tradable=True,
        )
