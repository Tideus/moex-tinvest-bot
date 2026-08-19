from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from moex_bot.integrations.algopack_intraday import AlgoPackIntradayAdapter

MOSCOW = ZoneInfo("Europe/Moscow")


class FakeGetTransport:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> object:
        self.urls.append(url)
        assert headers["Authorization"] == "Bearer token"
        common = ["tradedate", "tradetime", "secid"]
        if "/tradestats.json" in url:
            return _payload(
                common + ["pr_close", "pr_vwap", "val_b", "val_s"],
                ["2026-08-20", "10:25:00", "SBER", 100.5, 100.4, 700, 300],
            )
        if "/orderstats.json" in url:
            return _payload(
                common + ["put_val_b", "put_val_s", "cancel_val_b", "cancel_val_s"],
                ["2026-08-20", "10:25:00", "SBER", 900, 200, 100, 50],
            )
        return _payload(
            common + ["spread_bbo", "imbalance_val"],
            ["2026-08-20", "10:25:00", "SBER", 0.1, 0.4],
        )


def _payload(columns: list[str], row: list[Any]) -> dict[str, object]:
    return {"data": {"columns": columns, "data": [row]}}


def test_adapter_joins_three_latest_supercandle_datasets() -> None:
    transport = FakeGetTransport()
    adapter = AlgoPackIntradayAdapter("token", transport)
    bars = adapter.latest(
        secids=("SBER", "GAZP"),
        as_of=datetime(2026, 8, 20, 10, 30, tzinfo=MOSCOW),
    )
    assert len(bars) == 1
    assert bars[0].secid == "SBER"
    assert bars[0].buy_value == 700
    assert bars[0].put_buy_value == 900
    assert bars[0].book_imbalance == Decimal("0.4")
    assert len(transport.urls) == 3
    assert all("latest=1" in url for url in transport.urls)
