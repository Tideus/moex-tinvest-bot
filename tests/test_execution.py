from decimal import Decimal
from uuid import UUID

import pytest

from moex_bot.domain import Instrument, OrderIntent, OrderRecord, OrderStatus, Side
from moex_bot.execution import stable_order_request_id, transition_order


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
