from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from moex_bot.domain import (
    Instrument,
    MarketObservation,
    OrderIntent,
    OrderRecord,
    OrderStatus,
    PortfolioSnapshot,
    Position,
    Side,
    Target,
)
from moex_bot.execution import build_order_intents, stable_order_request_id, transition_order


def _intent() -> OrderIntent:
    instrument = Instrument("SBER", "uid", "TQBR", 10, Decimal("0.01"))
    return OrderIntent("req", instrument, Side.BUY, 2, Decimal("300"), Decimal("6000"), "test")


def test_idempotency_key_is_stable() -> None:
    first = stable_order_request_id("run", "SBER", Side.BUY, 2)
    second = stable_order_request_id("run", "SBER", Side.BUY, 2)
    assert first == second
    assert str(UUID(first)) == first
    assert len(first) == 36


def test_order_state_machine_enforces_transitions() -> None:
    record = OrderRecord(_intent(), OrderStatus.PLANNED)
    record = transition_order(record, OrderStatus.VALIDATED)
    record = transition_order(record, OrderStatus.SUBMITTED)
    record = transition_order(record, OrderStatus.ACCEPTED, broker_order_id="broker")
    record = transition_order(record, OrderStatus.PARTIALLY_FILLED, filled_lots=1)
    record = transition_order(record, OrderStatus.FILLED, filled_lots=2)
    assert record.status is OrderStatus.FILLED
    with pytest.raises(ValueError):
        transition_order(record, OrderStatus.CANCELLED)


def test_order_intent_is_sliced_to_max_notional_for_gradual_rebalance() -> None:
    instrument = Instrument("SBER", "uid", "TQBR", 10, Decimal("0.01"))
    observation = MarketObservation(
        instrument,
        Decimal("300"),
        Decimal("290"),
        Decimal("0.1"),
        Decimal("0.2"),
        datetime.now(UTC),
        True,
        True,
    )
    intents = build_order_intents(
        "run",
        (Target("SBER", Decimal("0.50"), "test"),),
        PortfolioSnapshot(Decimal("300000")),
        {"SBER": observation},
        Decimal("500"),
        Decimal("10000"),
    )
    assert len(intents) == 1
    assert intents[0].lots == 3
    assert intents[0].notional == Decimal("9000")


def test_order_intents_prioritize_exits_then_preserve_strategy_rank() -> None:
    first = Instrument("ZZZ", "uid-z", "TQBR", 1, Decimal("0.01"))
    second = Instrument("AAA", "uid-a", "TQBR", 1, Decimal("0.01"))
    exiting = Instrument("BBB", "uid-b", "TQBR", 1, Decimal("0.01"))
    market = {
        item.secid: MarketObservation(
            item,
            Decimal("100"),
            Decimal("90"),
            Decimal("0.1"),
            Decimal("0.2"),
            datetime.now(UTC),
            True,
            True,
        )
        for item in (first, second, exiting)
    }
    intents = build_order_intents(
        "run",
        (Target("ZZZ", Decimal("0.10"), "rank-1"), Target("AAA", Decimal("0.10"), "rank-2")),
        PortfolioSnapshot(
            Decimal("9900"), positions={"BBB": Position(exiting, 1)}
        ),
        market,
        Decimal("100"),
        Decimal("10000"),
    )
    assert [item.instrument.secid for item in intents] == ["BBB", "ZZZ", "AAA"]


def test_negative_target_opens_short_with_explicit_margin_confirmation() -> None:
    instrument = Instrument(
        "AAA", "uid", "TQBR", 1, Decimal("0.01"), short_enabled=True
    )
    market = {
        "AAA": MarketObservation(
            instrument,
            Decimal("100"),
            Decimal("110"),
            Decimal("-0.1"),
            Decimal("0.02"),
            datetime.now(UTC),
            True,
            True,
        )
    }
    intents = build_order_intents(
        "run-short",
        (Target("AAA", Decimal("-0.10"), "direction=short"),),
        PortfolioSnapshot(Decimal("10000")),
        market,
        Decimal("100"),
        Decimal("10000"),
    )
    assert len(intents) == 1
    assert intents[0].side is Side.SELL
    assert intents[0].confirm_margin_trade


def test_reversal_closes_existing_long_before_opening_short() -> None:
    instrument = Instrument(
        "AAA", "uid", "TQBR", 1, Decimal("0.01"), short_enabled=True
    )
    market = {
        "AAA": MarketObservation(
            instrument,
            Decimal("100"),
            Decimal("110"),
            Decimal("-0.1"),
            Decimal("0.02"),
            datetime.now(UTC),
            True,
            True,
        )
    }
    intents = build_order_intents(
        "run-reverse",
        (Target("AAA", Decimal("-0.10"), "direction=short"),),
        PortfolioSnapshot(Decimal("9000"), {"AAA": Position(instrument, 10)}),
        market,
        Decimal("100"),
        Decimal("10000"),
    )
    assert len(intents) == 1
    assert intents[0].side is Side.SELL
    assert intents[0].lots == 10
    assert not intents[0].confirm_margin_trade
