from __future__ import annotations

from collections.abc import Mapping
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
    if intent.instrument.secid in geo.blocked_secids:
        return None
    return intent


def evaluate_intent(
    intent: OrderIntent,
    portfolio: PortfolioSnapshot,
    config: BotConfig,
    geo: GeoRiskSnapshot,
    equity: Decimal,
    gross_exposure: Decimal,
    sector_exposure: Mapping[str, Decimal] | None = None,
    risk_cluster_exposure: Mapping[str, Decimal] | None = None,
    short_exposure: Decimal = Decimal("0"),
) -> RiskDecision:
    reasons: list[str] = []
    adjusted = apply_geo_multiplier(intent, geo)
    if adjusted is None:
        reasons.append("blocked by geopolitical risk policy")
        return RiskDecision(False, tuple(reasons), None)
    if adjusted.notional > config.max_order_notional:
        reasons.append("order notional exceeds limit")
    if portfolio.daily_turnover + adjusted.notional > config.max_daily_turnover:
        reasons.append("daily turnover limit exceeded")
    if portfolio.open_orders >= config.max_open_orders:
        reasons.append("open order limit reached")
    if portfolio.source.startswith("t_invest") and portfolio.open_orders > 0:
        reasons.append("active broker orders require position and exposure reconciliation")
    current_position = portfolio.positions.get(adjusted.instrument.secid)
    current_lots = 0 if current_position is None else current_position.lots
    lot_delta = adjusted.lots if adjusted.side is Side.BUY else -adjusted.lots
    resulting_lots = current_lots + lot_delta
    if current_lots * resulting_lots < 0:
        reasons.append("position reversal must close the current side before opening another")
    opening_short = resulting_lots < min(current_lots, 0)
    if opening_short:
        if not config.strategy.shorts_enabled or not config.allow_margin:
            reasons.append("sell requires an existing long position; shorting is disabled")
        if not adjusted.instrument.short_enabled:
            reasons.append("instrument is not verified as short-enabled")
        if not adjusted.confirm_margin_trade:
            reasons.append("short order requires explicit margin confirmation")
    elif adjusted.confirm_margin_trade:
        reasons.append("margin confirmation is forbidden for a non-short-increasing order")
    if current_position is not None and current_position.blocked_lots:
        if adjusted.side is Side.SELL and current_lots > 0:
            available_lots = max(0, current_lots - current_position.blocked_lots)
            if adjusted.lots > available_lots:
                reasons.append("sell quantity exceeds unblocked long position")
        if adjusted.side is Side.BUY and current_lots < 0:
            available_cover = max(0, abs(current_lots) - current_position.blocked_lots)
            if adjusted.lots > available_cover:
                reasons.append("cover quantity exceeds unblocked short position")
    reserve = max(Decimal("0"), equity * config.min_cash_reserve_weight)
    spendable_cash = max(Decimal("0"), portfolio.cash - reserve)
    if adjusted.side is Side.BUY and adjusted.notional > spendable_cash:
        reasons.append("insufficient spendable cash after mandatory reserve; margin forbidden")
    if equity <= 0:
        reasons.append("non-positive portfolio equity")
    else:
        current_notional = (
            Decimal(current_lots * adjusted.instrument.lot_size) * adjusted.limit_price
        )
        resulting_position = (
            Decimal(resulting_lots * adjusted.instrument.lot_size) * adjusted.limit_price
        )
        current_abs = abs(current_notional)
        resulting_abs = abs(resulting_position)
        exposure_delta = resulting_abs - current_abs
        if geo.level is GeoRiskLevel.CRITICAL and exposure_delta > 0:
            reasons.append("new exposure forbidden in CRITICAL mode")
        position_limit = (
            config.max_short_position_weight
            if resulting_position < 0
            else config.max_position_weight
        )
        if resulting_abs / equity > position_limit:
            reasons.append("resulting position exceeds position-weight limit")
        projected_gross = max(Decimal("0"), gross_exposure + exposure_delta)
        if projected_gross / equity > config.max_gross_exposure:
            reasons.append("projected gross exposure exceeds limit")
        current_short = current_abs if current_notional < 0 else Decimal("0")
        resulting_short = resulting_abs if resulting_position < 0 else Decimal("0")
        projected_short = max(
            Decimal("0"), short_exposure + resulting_short - current_short
        )
        if projected_short / equity > config.max_short_gross_exposure:
            reasons.append("projected short exposure exceeds limit")
        sector_current = (sector_exposure or {}).get(
            adjusted.instrument.sector, Decimal("0")
        )
        projected_sector = max(
            Decimal("0"), sector_current + exposure_delta
        )
        if projected_sector / equity > config.max_sector_weight:
            reasons.append("projected sector exposure exceeds limit")
        cluster_current = (risk_cluster_exposure or {}).get(
            adjusted.instrument.risk_cluster, Decimal("0")
        )
        projected_cluster = max(
            Decimal("0"), cluster_current + exposure_delta
        )
        if projected_cluster / equity > config.max_risk_cluster_weight:
            reasons.append("projected risk-cluster exposure exceeds limit")
    return RiskDecision(not reasons, tuple(reasons), adjusted if not reasons else None)
