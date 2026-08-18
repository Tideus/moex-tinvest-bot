from __future__ import annotations

from collections.abc import Mapping
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
    sector_exposure: Mapping[str, Decimal] | None = None,
    risk_cluster_exposure: Mapping[str, Decimal] | None = None,
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
    if portfolio.source.startswith("t_invest") and portfolio.open_orders > 0:
        reasons.append("active broker orders require position and exposure reconciliation")
    current_position = portfolio.positions.get(adjusted.instrument.secid)
    if adjusted.side is Side.SELL:
        if current_position is None:
            reasons.append("sell requires an existing long position; shorting forbidden")
        else:
            available_lots = max(0, current_position.lots - current_position.blocked_lots)
            if adjusted.lots > available_lots:
                reasons.append("sell quantity exceeds unblocked long position; shorting forbidden")
    reserve = max(Decimal("0"), equity * config.min_cash_reserve_weight)
    spendable_cash = max(Decimal("0"), portfolio.cash - reserve)
    if adjusted.side is Side.BUY and adjusted.notional > spendable_cash:
        reasons.append("insufficient spendable cash after mandatory reserve; margin forbidden")
    if equity <= 0:
        reasons.append("non-positive portfolio equity")
    else:
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
            gross_exposure + adjusted.notional
            if adjusted.side is Side.BUY
            else max(Decimal("0"), gross_exposure - adjusted.notional)
        )
        if projected_gross / equity > config.max_gross_exposure:
            reasons.append("projected gross exposure exceeds limit")
        direction = Decimal("1") if adjusted.side is Side.BUY else Decimal("-1")
        sector_current = (sector_exposure or {}).get(
            adjusted.instrument.sector, Decimal("0")
        )
        projected_sector = max(
            Decimal("0"), sector_current + direction * adjusted.notional
        )
        if projected_sector / equity > config.max_sector_weight:
            reasons.append("projected sector exposure exceeds limit")
        cluster_current = (risk_cluster_exposure or {}).get(
            adjusted.instrument.risk_cluster, Decimal("0")
        )
        projected_cluster = max(
            Decimal("0"), cluster_current + direction * adjusted.notional
        )
        if projected_cluster / equity > config.max_risk_cluster_weight:
            reasons.append("projected risk-cluster exposure exceeds limit")
    return RiskDecision(not reasons, tuple(reasons), adjusted if not reasons else None)
