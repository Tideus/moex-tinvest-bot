from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .config import BotConfig
from .domain import (
    GeoRiskLevel,
    GeoRiskSnapshot,
    OrderIntent,
    PortfolioSnapshot,
    RiskDecision,
    Side,
)


def apply_geo_multiplier(intent: OrderIntent, geo: GeoRiskSnapshot) -> OrderIntent | None:
    if intent.instrument.secid in geo.blocked_secids and intent.side is Side.BUY:
        return None
    if intent.side is Side.SELL:
        return intent
    lots = int(Decimal(intent.lots) * geo.multiplier)
    if lots <= 0:
        return None
    units = lots * intent.instrument.lot_size
    return replace(intent, lots=lots, notional=Decimal(units) * intent.limit_price)


def evaluate_intent(
    intent: OrderIntent,
    portfolio: PortfolioSnapshot,
    config: BotConfig,
    geo: GeoRiskSnapshot,
    equity: Decimal,
    gross_exposure: Decimal,
) -> RiskDecision:
    reasons: list[str] = []
    adjusted = apply_geo_multiplier(intent, geo)
    if adjusted is None:
        reasons.append("blocked by geopolitical risk policy")
        return RiskDecision(False, tuple(reasons), None)
    if geo.level is GeoRiskLevel.CRITICAL and adjusted.side is Side.BUY:
        reasons.append("new exposure forbidden in CRITICAL mode")
    if adjusted.notional > config.max_order_notional:
        reasons.append("order notional exceeds limit")
    if portfolio.daily_turnover + adjusted.notional > config.max_daily_turnover:
        reasons.append("daily turnover limit exceeded")
    if portfolio.open_orders >= config.max_open_orders:
        reasons.append("open order limit reached")
    if adjusted.side is Side.BUY and adjusted.notional > portfolio.cash:
        reasons.append("insufficient cash; margin forbidden")
    if equity <= 0:
        reasons.append("non-positive portfolio equity")
    else:
        current_position = portfolio.positions.get(adjusted.instrument.secid)
        current_notional = Decimal("0")
        if current_position is not None:
            current_notional = Decimal(current_position.units) * adjusted.limit_price
        resulting_position = (
            current_notional + adjusted.notional
            if adjusted.side is Side.BUY
            else max(Decimal("0"), current_notional - adjusted.notional)
        )
        if resulting_position / equity > config.max_position_weight:
            reasons.append("resulting position exceeds position-weight limit")
        projected_gross = (
            gross_exposure + adjusted.notional if adjusted.side is Side.BUY else gross_exposure
        )
        if projected_gross / equity > config.max_gross_exposure:
            reasons.append("projected gross exposure exceeds limit")
    return RiskDecision(not reasons, tuple(reasons), adjusted if not reasons else None)
