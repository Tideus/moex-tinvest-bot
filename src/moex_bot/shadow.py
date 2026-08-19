from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

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
        period: str = "1h",
        momentum_windows: tuple[int, ...] | None = None,
        volatility_window: int | None = None,
    ) -> MarketObservation: ...


@dataclass(frozen=True, slots=True)
class UniverseEntry:
    secid: str
    board: str
    t_invest_uid: str
    lot_size_verified: int
    api_trade_available: bool
    issuer_id: str
    sector: str
    risk_cluster: str
    asset_class: str
    short_enabled_verified: bool = False

    def validate(self) -> None:
        UUID(self.t_invest_uid)
        if not self.secid or not self.board:
            raise ValueError("universe identity must be complete")
        if self.lot_size_verified <= 0:
            raise ValueError("verified lot must be positive")
        if not self.api_trade_available:
            raise ValueError(f"instrument is not API-tradeable: {self.secid}")
        if not self.issuer_id or not self.sector or not self.risk_cluster:
            raise ValueError(f"diversification identity is incomplete: {self.secid}")
        if self.asset_class != "share":
            raise ValueError(f"only shares are supported by the current strategy: {self.secid}")


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def load_universe(path: Path) -> tuple[UniverseEntry, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("universe must be a non-empty list")
    entries_list: list[UniverseEntry] = []
    for item in raw:
        trade_available = item["api_trade_available"]
        if not isinstance(trade_available, bool):
            raise ValueError("universe api_trade_available must be a JSON boolean")
        short_enabled = item.get("short_enabled_verified", False)
        if not isinstance(short_enabled, bool):
            raise ValueError("universe short_enabled_verified must be a JSON boolean")
        entries_list.append(
            UniverseEntry(
                secid=str(item["secid"]),
                board=str(item["board"]),
                t_invest_uid=str(item["t_invest_uid"]),
                lot_size_verified=int(item["lot_size_verified"]),
                api_trade_available=trade_available,
                issuer_id=str(item["issuer_id"]),
                sector=str(item["sector"]),
                risk_cluster=str(item["risk_cluster"]),
                asset_class=str(item.get("asset_class", "share")),
                short_enabled_verified=short_enabled,
            )
        )
    entries = tuple(entries_list)
    for entry in entries:
        entry.validate()
    if len({entry.secid for entry in entries}) != len(entries):
        raise ValueError("universe contains duplicate SECID")
    if len({entry.t_invest_uid for entry in entries}) != len(entries):
        raise ValueError("universe contains duplicate T-Invest UID")
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
    positions: dict[str, Position] = {}
    for secid, position_raw in raw.get("positions", {}).items():
        if isinstance(position_raw, dict):
            lots = int(position_raw["lots"])
            blocked_lots = int(position_raw.get("blocked_lots", 0))
        else:
            lots = int(position_raw)
            blocked_lots = 0
        positions[secid] = Position(market[secid].instrument, lots, blocked_lots)
    return PortfolioSnapshot(
        cash=_decimal(raw["cash"]),
        positions=positions,
        daily_turnover=_decimal(raw.get("daily_turnover", "0")),
        open_orders=int(raw.get("open_orders", 0)),
        blocked_cash=_decimal(raw.get("blocked_cash", "0")),
        reported_equity=(
            None if raw.get("reported_equity") is None else _decimal(raw["reported_equity"])
        ),
        source=str(raw.get("source", "file")),
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
            trend_window=config.strategy.trend_window,
            momentum_windows=config.strategy.momentum_windows,
            volatility_window=config.strategy.volatility_window,
            lookback_days=max(120, config.strategy.trend_window * 3),
            period=config.strategy.candle_period,
        )
        if observation.instrument.lot_size != entry.lot_size_verified:
            raise ValueError(f"lot mismatch for {entry.secid}")
        observation = replace(
            observation,
            instrument=replace(
                observation.instrument,
                issuer_id=entry.issuer_id,
                sector=entry.sector,
                risk_cluster=entry.risk_cluster,
                asset_class=entry.asset_class,
                short_enabled=entry.short_enabled_verified,
            ),
        )
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
        allow_rebalance=(
            as_of.astimezone(ZoneInfo("Europe/Moscow")).hour
            in config.strategy.rebalance_hours_moscow
        ),
    )
    persisted = asdict(result)
    persisted["market"] = [asdict(market[secid]) for secid in sorted(market)]
    persisted["portfolio_input"] = asdict(portfolio)
    persisted["config_snapshot"] = asdict(config)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(persisted, ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )
    return result
