from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from .adapters import DryRunExecutionAdapter, JsonlAuditLog
from .config import BotConfig
from .domain import (
    ExecutionMode,
    GeoEvent,
    MarketObservation,
    PortfolioSnapshot,
    Position,
)
from .harness import HarnessResult, TradingHarness


class HourlyMarketDataPort(Protocol):
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
    ) -> MarketObservation: ...


@dataclass(frozen=True, slots=True)
class UniverseEntry:
    secid: str
    board: str
    t_invest_uid: str
    lot_size_verified: int
    api_trade_available: bool

    def validate(self) -> None:
        UUID(self.t_invest_uid)
        if not self.secid or not self.board:
            raise ValueError("universe identity must be complete")
        if self.lot_size_verified <= 0:
            raise ValueError("verified lot must be positive")
        if not self.api_trade_available:
            raise ValueError(f"instrument is not API-tradeable: {self.secid}")


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def load_universe(path: Path) -> tuple[UniverseEntry, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("universe must be a non-empty list")
    entries = tuple(
        UniverseEntry(
            secid=str(item["secid"]),
            board=str(item["board"]),
            t_invest_uid=str(item["t_invest_uid"]),
            lot_size_verified=int(item["lot_size_verified"]),
            api_trade_available=bool(item["api_trade_available"]),
        )
        for item in raw
    )
    for entry in entries:
        entry.validate()
    if len({entry.secid for entry in entries}) != len(entries):
        raise ValueError("universe contains duplicate SECID")
    return entries


def load_geo_feed(
    path: Path,
    *,
    as_of: datetime,
    max_age_seconds: int = 7200,
) -> tuple[tuple[GeoEvent, ...], bool]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    observed_raw = raw.get("feed_observed_at")
    news_stale = observed_raw is None
    if observed_raw is not None:
        observed_at = datetime.fromisoformat(str(observed_raw))
        if observed_at.tzinfo is None:
            raise ValueError("geo feed timestamp must be timezone-aware")
        age = (as_of - observed_at).total_seconds()
        if age < 0:
            raise ValueError("geo feed timestamp is in the future")
        news_stale = age > max_age_seconds
    events = tuple(
        GeoEvent(
            event_id=str(item["event_id"]),
            severity=int(item["severity"]),
            confidence=_decimal(item["confidence"]),
            source_tier=str(item["source_tier"]),
            confirmed=bool(item["confirmed"]),
            affected_secids=frozenset(item.get("affected_secids", [])),
            observed_at=datetime.fromisoformat(str(item["observed_at"])),
        )
        for item in raw.get("events", [])
    )
    return events, news_stale


def load_portfolio(
    path: Path,
    market: Mapping[str, MarketObservation],
) -> PortfolioSnapshot:
    raw = json.loads(path.read_text(encoding="utf-8"))
    unknown = set(raw.get("positions", {})) - set(market)
    if unknown:
        raise ValueError(f"positions outside verified universe: {sorted(unknown)}")
    positions = {
        secid: Position(market[secid].instrument, int(lots))
        for secid, lots in raw.get("positions", {}).items()
    }
    return PortfolioSnapshot(
        cash=_decimal(raw["cash"]),
        positions=positions,
        daily_turnover=_decimal(raw.get("daily_turnover", "0")),
        open_orders=int(raw.get("open_orders", 0)),
    )


def run_hourly_shadow(
    *,
    config: BotConfig,
    universe: tuple[UniverseEntry, ...],
    portfolio_path: Path,
    geo_path: Path,
    output_path: Path,
    as_of: datetime,
    market_data: HourlyMarketDataPort,
) -> HarnessResult:
    if config.mode is not ExecutionMode.SHADOW:
        raise ValueError("hourly shadow requires mode=shadow")
    market: dict[str, MarketObservation] = {}
    for entry in universe:
        observation = market_data.hourly_observation(
            secid=entry.secid,
            uid=entry.t_invest_uid,
            board=entry.board,
            as_of=as_of,
        )
        if observation.instrument.lot_size != entry.lot_size_verified:
            raise ValueError(f"lot mismatch for {entry.secid}")
        market[entry.secid] = observation
    portfolio = load_portfolio(portfolio_path, market)
    geo_events, news_stale = load_geo_feed(geo_path, as_of=as_of)
    audit_path = output_path.with_suffix(".audit.jsonl")
    run_id = f"shadow-{as_of.isoformat()}"
    result = TradingHarness(
        config,
        DryRunExecutionAdapter(config.mode),
        JsonlAuditLog(audit_path),
    ).run(
        run_id=run_id,
        as_of=as_of,
        market=market,
        portfolio=portfolio,
        geo_events=geo_events,
        news_stale=news_stale,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(asdict(result), ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )
    return result
