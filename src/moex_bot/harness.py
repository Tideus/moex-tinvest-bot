from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal

from .adapters import AuditPort, ExecutionPort, order_record_event
from .config import BotConfig
from .domain import (
    GeoEvent,
    GeoRiskSnapshot,
    MarketObservation,
    OrderRecord,
    PortfolioSnapshot,
    Position,
    Target,
)
from .execution import build_order_intents
from .geo import assess_geo_risk
from .quality import QualityReport, validate_market
from .risk import evaluate_intent
from .strategy import calculate_targets


@dataclass(frozen=True, slots=True)
class SignalDiagnostic:
    secid: str
    momentum: Decimal
    momentum_threshold: Decimal
    price: Decimal
    trend: Decimal
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HarnessResult:
    run_id: str
    quality: QualityReport
    geo: GeoRiskSnapshot
    targets: tuple[Target, ...]
    orders: tuple[OrderRecord, ...]
    rejected: tuple[dict[str, object], ...]
    portfolio: PortfolioSnapshot | None = None
    rebalance_allowed: bool = True
    rebalance_hours_moscow: tuple[int, ...] = ()
    signal_diagnostics: tuple[SignalDiagnostic, ...] = ()


class TradingHarness:
    def __init__(self, config: BotConfig, execution: ExecutionPort, audit: AuditPort) -> None:
        self.config = config
        self.execution = execution
        self.audit = audit

    def run(
        self,
        *,
        run_id: str,
        as_of: datetime,
        market: Mapping[str, MarketObservation],
        portfolio: PortfolioSnapshot,
        geo_events: tuple[GeoEvent, ...],
        news_stale: bool = False,
        allow_rebalance: bool = True,
    ) -> HarnessResult:
        quality = validate_market(market, as_of, self.config.max_data_age_seconds)
        geo = assess_geo_risk(geo_events, news_stale=news_stale)
        self.audit.write(
            {"type": "run_started", "run_id": run_id, "as_of": as_of, "mode": self.config.mode}
        )
        if not quality.passed:
            self.audit.write({"type": "quality_block", "run_id": run_id, "errors": quality.errors})
            return HarnessResult(
                run_id,
                quality,
                geo,
                (),
                (),
                (),
                portfolio,
                allow_rebalance,
                self.config.strategy.rebalance_hours_moscow,
            )

        raw_targets = calculate_targets(
            market.values(),
            self.config.strategy,
            held_secids=frozenset(
                secid for secid, position in portfolio.positions.items() if position.lots > 0
            ),
            held_short_secids=frozenset(
                secid for secid, position in portfolio.positions.items() if position.lots < 0
            ),
        )
        targets = tuple(
            Target(
                target.secid,
                (
                    min(target.weight, self.config.max_position_weight)
                    if target.weight >= 0
                    else -min(abs(target.weight), self.config.max_short_position_weight)
                )
                * geo.multiplier,
                f"{target.rationale}; geo_multiplier={geo.multiplier}",
            )
            for target in raw_targets
            if target.secid not in geo.blocked_secids
        )
        signal_diagnostics = tuple(
            SignalDiagnostic(
                secid=item.instrument.secid,
                momentum=item.momentum,
                momentum_threshold=self.config.strategy.min_momentum,
                price=item.price,
                trend=item.trend,
                eligible=(
                    item.complete
                    and item.tradable
                    and item.momentum > self.config.strategy.min_momentum
                    and (
                        not self.config.strategy.require_above_trend
                        or item.price > item.trend
                    )
                    and item.instrument.secid not in geo.blocked_secids
                ),
                reasons=tuple(
                    reason
                    for condition, reason in (
                        (not item.complete, "incomplete candle"),
                        (not item.tradable, "instrument is not tradable"),
                        (
                            item.momentum <= self.config.strategy.min_momentum,
                            "momentum threshold not passed",
                        ),
                        (
                            self.config.strategy.require_above_trend
                            and item.price <= item.trend,
                            "price is not above trend",
                        ),
                        (
                            item.instrument.secid in geo.blocked_secids,
                            "blocked by geopolitical risk policy",
                        ),
                    )
                    if condition
                ),
            )
            for item in sorted(
                market.values(),
                key=lambda observation: (
                    -observation.momentum,
                    observation.instrument.secid,
                ),
            )
        )
        intents = (
            build_order_intents(
                run_id,
                targets,
                portfolio,
                market,
                self.config.min_trade_notional,
                self.config.max_order_notional,
            )
            if allow_rebalance
            else ()
        )
        equity = portfolio.equity(market)
        gross_exposure = sum(
            (
                abs(position.units * market[secid].price)
                for secid, position in portfolio.positions.items()
            ),
            start=Decimal("0"),
        )
        short_exposure = sum(
            (
                abs(position.units * market[secid].price)
                for secid, position in portfolio.positions.items()
                if position.lots < 0
            ),
            start=Decimal("0"),
        )
        sector_exposure: dict[str, Decimal] = {}
        cluster_exposure: dict[str, Decimal] = {}
        for secid, position in portfolio.positions.items():
            exposure = abs(Decimal(position.units) * market[secid].price)
            instrument = position.instrument
            sector_exposure[instrument.sector] = (
                sector_exposure.get(instrument.sector, Decimal("0")) + exposure
            )
            cluster_exposure[instrument.risk_cluster] = (
                cluster_exposure.get(instrument.risk_cluster, Decimal("0")) + exposure
            )
        orders: list[OrderRecord] = []
        rejected: list[dict[str, object]] = []
        projected_portfolio = portfolio
        projected_gross = gross_exposure
        for intent in intents:
            decision = evaluate_intent(
                intent,
                projected_portfolio,
                self.config,
                geo,
                equity,
                projected_gross,
                sector_exposure,
                cluster_exposure,
                short_exposure,
            )
            if not decision.allowed or decision.intent is None:
                item: dict[str, object] = {
                    "type": "risk_reject",
                    "run_id": run_id,
                    "order_request_id": intent.order_request_id,
                    "secid": intent.instrument.secid,
                    "side": intent.side.value,
                    "lots": intent.lots,
                    "notional": intent.notional,
                    "reasons": decision.reasons,
                }
                rejected.append(item)
                self.audit.write(item)
                continue
            record = self.execution.submit(decision.intent)
            orders.append(record)
            projected_source = projected_portfolio.source
            if projected_source.startswith("t_invest"):
                projected_source = f"projected:{projected_source}"
            instrument = decision.intent.instrument
            current = projected_portfolio.positions.get(instrument.secid)
            current_lots = 0 if current is None else current.lots
            lot_delta = (
                decision.intent.lots
                if decision.intent.side.value == "buy"
                else -decision.intent.lots
            )
            resulting_lots = current_lots + lot_delta
            next_positions = dict(projected_portfolio.positions)
            if resulting_lots == 0:
                next_positions.pop(instrument.secid, None)
            else:
                next_positions[instrument.secid] = replace(
                    current or Position(instrument, 0), lots=resulting_lots
                )
            current_abs = (
                abs(Decimal(current_lots * instrument.lot_size))
                * decision.intent.limit_price
            )
            resulting_abs = (
                abs(Decimal(resulting_lots * instrument.lot_size))
                * decision.intent.limit_price
            )
            exposure_delta = resulting_abs - current_abs
            projected_gross = max(Decimal("0"), projected_gross + exposure_delta)
            current_short = current_abs if current_lots < 0 else Decimal("0")
            resulting_short = resulting_abs if resulting_lots < 0 else Decimal("0")
            short_exposure = max(
                Decimal("0"), short_exposure + resulting_short - current_short
            )
            projected_portfolio = replace(
                projected_portfolio,
                cash=(
                    projected_portfolio.cash - decision.intent.notional
                    if decision.intent.side.value == "buy"
                    else projected_portfolio.cash + decision.intent.notional
                ),
                positions=next_positions,
                daily_turnover=(projected_portfolio.daily_turnover + decision.intent.notional),
                open_orders=projected_portfolio.open_orders + 1,
                source=projected_source,
            )
            sector = decision.intent.instrument.sector
            cluster = decision.intent.instrument.risk_cluster
            sector_exposure[sector] = max(
                Decimal("0"),
                sector_exposure.get(sector, Decimal("0"))
                + exposure_delta,
            )
            cluster_exposure[cluster] = max(
                Decimal("0"),
                cluster_exposure.get(cluster, Decimal("0"))
                + exposure_delta,
            )
            event = order_record_event(record)
            event["run_id"] = run_id
            self.audit.write(event)
        self.audit.write(
            {
                "type": "run_finished",
                "run_id": run_id,
                "targets": [asdict(item) for item in targets],
                "orders": len(orders),
                "rejected": len(rejected),
                "rebalance_allowed": allow_rebalance,
            }
        )
        return HarnessResult(
            run_id,
            quality,
            geo,
            targets,
            tuple(orders),
            tuple(rejected),
            portfolio,
            allow_rebalance,
            self.config.strategy.rebalance_hours_moscow,
            signal_diagnostics,
        )
