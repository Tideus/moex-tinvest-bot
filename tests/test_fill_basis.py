from decimal import Decimal

from moex_bot.intraday import executed_entries


def test_partial_fills_reduce_and_dedupe():
    def fill(identifier, side, quantity, gross, hour):
        return {"operation_id": identifier, "secid": "T", "side": side,
                "quantity": quantity, "gross": gross,
                "occurred_at": f"2026-10-30T{hour}:00:00+03:00"}

    first = fill("1", "BUY", 2, "200", "10")
    second = fill("2", "BUY", 2, "240", "11")
    reduction = fill("3", "SELL", 1, "130", "12")
    entries = executed_entries({"executed_operations": [first, second, first, reduction]})
    at, side, price, quantity = entries["T"]
    assert price == Decimal("110")
    assert quantity == 3
    assert at.hour == 10
    assert side.value == "buy"
    assert not executed_entries({"executed_operations": [
        first, fill("4", "SELL", 2, "250", "12")]})
