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
    held_short_secids: frozenset[str] = frozenset(),
) -> tuple[Target, ...]:
    observations = tuple(observations)
    long_eligible = [
        item
        for item in observations
        if item.tradable
        and item.complete
        and item.momentum > config.min_momentum
        and (not config.require_above_trend or item.price > item.trend)
    ]
    long_ranked = sorted(
        long_eligible, key=lambda item: (-item.momentum, item.instrument.secid)
    )
    entrants = long_ranked[: config.top_n]
    retained = [
        item
        for rank, item in enumerate(long_ranked, start=1)
        if item.instrument.secid in held_secids
        and rank <= config.top_n + config.exit_rank_buffer
    ]
    long_by_secid = {item.instrument.secid: item for item in (*entrants, *retained)}
    selected_long = tuple(
        sorted(long_by_secid.values(), key=lambda item: (-item.momentum, item.instrument.secid))
    )
    short_eligible = (
        [
            item
            for item in observations
            if item.tradable
            and item.complete
            and item.instrument.short_enabled
            and item.momentum < config.max_short_momentum
            and (
                not config.require_below_trend_for_short or item.price < item.trend
            )
        ]
        if config.shorts_enabled
        else []
    )
    short_ranked = sorted(
        short_eligible, key=lambda item: (item.momentum, item.instrument.secid)
    )
    short_entrants = short_ranked[: config.short_top_n]
    short_retained = [
        item
        for rank, item in enumerate(short_ranked, start=1)
        if item.instrument.secid in held_short_secids
        and rank <= config.short_top_n + config.exit_rank_buffer
    ]
    short_by_secid = {
        item.instrument.secid: item for item in (*short_entrants, *short_retained)
    }
    selected_short = tuple(
        sorted(short_by_secid.values(), key=lambda item: (item.momentum, item.instrument.secid))
    )
    return (
        *_weighted_targets(selected_long, config, config.long_target_gross, "long"),
        *_weighted_targets(selected_short, config, config.short_target_gross, "short"),
    )


def _weighted_targets(
    selected: tuple[MarketObservation, ...],
    config: StrategyConfig,
    gross_target: Decimal,
    direction: str,
) -> tuple[Target, ...]:
    if not selected or gross_target == 0:
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
    sign = Decimal("1") if direction == "long" else Decimal("-1")
    return tuple(
        Target(
            secid=item.instrument.secid,
            weight=(
                sign * gross_target * risk_units[item.instrument.secid] / total_risk_units
            ),
            rationale=(
                f"direction={direction}; momentum={item.momentum}; "
                f"price={item.price}{'>trend=' if direction == 'long' else '<trend='}"
                f"{item.trend}; "
                f"volatility={item.volatility}; weighting="
                f"{'inverse_volatility' if config.inverse_volatility_weights else 'equal'}"
            ),
        )
        for item in selected
    )
