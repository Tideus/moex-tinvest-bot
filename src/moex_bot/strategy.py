from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from .config import StrategyConfig
from .domain import MarketObservation, Target


def calculate_targets(
    observations: Iterable[MarketObservation],
    config: StrategyConfig,
    *,
    held_secids: frozenset[str] = frozenset(),
) -> tuple[Target, ...]:
    eligible = [
        item
        for item in observations
        if item.tradable
        and item.complete
        and item.momentum > config.min_momentum
        and (not config.require_above_trend or item.price > item.trend)
    ]
    ranked = sorted(eligible, key=lambda item: (-item.momentum, item.instrument.secid))
    entrants = ranked[: config.top_n]
    retained = [
        item
        for rank, item in enumerate(ranked, start=1)
        if item.instrument.secid in held_secids
        and rank <= config.top_n + config.exit_rank_buffer
    ]
    selected_by_secid = {item.instrument.secid: item for item in (*entrants, *retained)}
    selected = tuple(
        sorted(selected_by_secid.values(), key=lambda item: (-item.momentum, item.instrument.secid))
    )
    if not selected:
        return ()
    if config.inverse_volatility_weights:
        risk_units = {
            item.instrument.secid: Decimal("1") / max(
                item.volatility, config.volatility_floor
            )
            for item in selected
        }
    else:
        risk_units = {item.instrument.secid: Decimal("1") for item in selected}
    total_risk_units = sum(risk_units.values(), Decimal("0"))
    return tuple(
        Target(
            secid=item.instrument.secid,
            weight=risk_units[item.instrument.secid] / total_risk_units,
            rationale=(
                f"momentum={item.momentum}; price={item.price}>trend={item.trend}; "
                f"volatility={item.volatility}; weighting="
                f"{'inverse_volatility' if config.inverse_volatility_weights else 'equal'}"
            ),
        )
        for item in selected
    )
