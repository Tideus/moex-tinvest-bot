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
    Target,
)
from .execution import build_order_intents
from .geo import assess_geo_risk
from .quality import QualityReport, validate_market
from .risk import evaluate_intent
from .strategy import calculate_targets


@dataclass(frozen=True, slots=True)
class HarnessResult:
    run_id: str
    quality: QualityReport
    geo: GeoRiskSnapshot
    targets: tuple[Target, ...]
    orders: tuple[OrderRecord, ...]
    rejected: tuple[dict[str, object], ...]


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
    ) -> HarnessResult:
        quality = validate_market(market, as_of, self.config.max_data_age_seconds)
        geo = assess_geo_risk(geo_events, news_stale=news_stale)
        self.audit.write(
            {"type": "run_started", "run_id": run_id, "as_of": as_of, "mode": self.config.mode}
        )
        if not quality.passed:
            self.audit.write({"type": "quality_block", "run_id": run_id, "errors": quality.errors})
            return HarnessResult(run_id, quality, geo, (), (), ())

        raw_targets = calculate_targets(market.values(), self.config.strategy)
        targets = tuple(
            Target(
                target.secid,
                min(target.weight, self.config.max_position_weight) * geo.multiplier,
                f"{target.rationale}; geo_multiplier={geo.multiplier}",
            )
            for target in raw_targets
            if target.secid not in geo.blocked_secids
        )
        intents = build_order_intents(
            run_id, targets, portfolio, market, self.config.min_trade_notional
        )
        equity = portfolio.equity(market)
        gross_exposure = sum(
            (
                position.units * market[secid].price
                for secid, position in portfolio.positions.items()
            ),
            start=Decimal("0"),
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
            )
            if not decision.allowed or decision.intent is None:
                item: dict[str, object] = {
                    "type": "risk_reject",
                    "run_id": run_id,
                    "order_request_id": intent.order_request_id,
                    "reasons": decision.reasons,
                }
                rejected.append(item)
                self.audit.write(item)
                continue
            record = self.execution.submit(decision.intent)
            orders.append(record)
            if decision.intent.side.value == "buy":
                projected_portfolio = replace(
                    projected_portfolio,
                    cash=projected_portfolio.cash - decision.intent.notional,
                    daily_turnover=(projected_portfolio.daily_turnover + decision.intent.notional),
                    open_orders=projected_portfolio.open_orders + 1,
                )
                projected_gross += decision.intent.notional
            else:
                projected_portfolio = replace(
                    projected_portfolio,
                    daily_turnover=(projected_portfolio.daily_turnover + decision.intent.notional),
                    open_orders=projected_portfolio.open_orders + 1,
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
            }
        )
        return HarnessResult(run_id, quality, geo, targets, tuple(orders), tuple(rejected))
