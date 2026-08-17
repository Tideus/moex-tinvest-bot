from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from .domain import (
    MarketObservation,
    OrderIntent,
    OrderRecord,
    OrderStatus,
    PortfolioSnapshot,
    Side,
    Target,
)


def stable_order_request_id(run_id: str, secid: str, side: Side, lots: int) -> str:
    raw = f"moex-tinvest-bot:{run_id}|{secid}|{side.value}|{lots}"
    return str(uuid5(NAMESPACE_URL, raw))


def build_order_intents(
    run_id: str,
    targets: tuple[Target, ...],
    portfolio: PortfolioSnapshot,
    market: Mapping[str, MarketObservation],
    min_trade_notional: Decimal,
) -> tuple[OrderIntent, ...]:
    equity = portfolio.equity(market)
    target_map = {target.secid: target for target in targets}
    secids = sorted(set(target_map) | set(portfolio.positions))
    intents: list[OrderIntent] = []
    for secid in secids:
        observation = market.get(secid)
        if observation is None:
            continue
        instrument = observation.instrument
        current_position = portfolio.positions.get(secid)
        target = target_map.get(secid)
        current_lots = current_position.lots if current_position is not None else 0
        target_weight = target.weight if target is not None else Decimal("0")
        target_value = equity * target_weight
        lot_value = Decimal(instrument.lot_size) * observation.price
        target_lots = int(target_value // lot_value)
        delta = target_lots - current_lots
        if delta == 0:
            continue
        side = Side.BUY if delta > 0 else Side.SELL
        lots = abs(delta)
        price = instrument.round_price(observation.price)
        notional = Decimal(lots * instrument.lot_size) * price
        if notional < min_trade_notional:
            continue
        rationale = target.rationale if target is not None else "exit target"
        intents.append(
            OrderIntent(
                order_request_id=stable_order_request_id(run_id, secid, side, lots),
                instrument=instrument,
                side=side,
                lots=lots,
                limit_price=price,
                notional=notional,
                rationale=rationale,
            )
        )
    return tuple(intents)


_ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PLANNED: frozenset({OrderStatus.VALIDATED, OrderStatus.REJECTED}),
    OrderStatus.VALIDATED: frozenset({OrderStatus.SUBMITTED, OrderStatus.REJECTED}),
    OrderStatus.SUBMITTED: frozenset(
        {OrderStatus.ACCEPTED, OrderStatus.REJECTED, OrderStatus.UNKNOWN}
    ),
    OrderStatus.ACCEPTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.UNKNOWN: frozenset(
        {
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


def transition_order(
    record: OrderRecord,
    new_status: OrderStatus,
    *,
    filled_lots: int | None = None,
    broker_order_id: str | None = None,
) -> OrderRecord:
    if new_status not in _ALLOWED_TRANSITIONS[record.status]:
        raise ValueError(f"invalid order transition {record.status}->{new_status}")
    next_filled = record.filled_lots if filled_lots is None else filled_lots
    if next_filled < record.filled_lots or next_filled > record.intent.lots:
        raise ValueError("filled lots must be monotonic and within requested lots")
    if new_status is OrderStatus.FILLED and next_filled != record.intent.lots:
        raise ValueError("filled status requires all lots filled")
    return replace(
        record,
        status=new_status,
        filled_lots=next_filled,
        broker_order_id=broker_order_id or record.broker_order_id,
    )
