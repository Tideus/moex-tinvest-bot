from datetime import UTC, datetime
from decimal import Decimal

from moex_bot.integrations.algopack_flow import AlgoPackFlowAdapter
from moex_bot.reporting import FlowState, render_flow_report

AS_OF = datetime(2026, 8, 14, 9, 7, tzinfo=UTC)


class Frame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self.rows


class Ticker:
    def tradestats(self, *, start: str, end: str, latest: bool = False) -> Frame:
        return Frame(
            [
                {"tradedate": "2026-08-14", "tradetime": "11:05:00", "val_b": 70, "val_s": 30},
                {"tradedate": "2026-08-14", "tradetime": "12:05:00", "val_b": 60, "val_s": 40},
            ]
        )

    def futoi(self, *, start: str, end: str) -> Frame:
        return Frame(
            [
                {
                    "tradedate": "2026-08-14", "tradetime": "14:00:00", "clgroup": "FIZ",
                    "pos": -20, "pos_long": 80, "pos_short": -100,
                    "pos_long_num": 8, "pos_short_num": 10,
                },
                {
                    "tradedate": "2026-08-14", "tradetime": "14:00:00", "clgroup": "YUR",
                    "pos": 20, "pos_long": 100, "pos_short": -80,
                    "pos_long_num": 4, "pos_short_num": 3,
                },
            ]
        )

    def hi2(self, *, start: str, end: str, latest: bool = False) -> Frame:
        return Frame(
            [{
                "tradedate": "2026-08-14", "tradetime": "14:00:00",
                "metric": "hhi_buy", "value": 1700,
            }]
        )


def test_flow_aggregates_sums_and_does_not_call_sales_shorts() -> None:
    adapter = AlgoPackFlowAdapter(lambda ticker: Ticker())
    equity = adapter.equity_flow(secid="SBER", as_of=AS_OF)
    assert equity.buy_value == Decimal("130")
    assert equity.sell_value == Decimal("70")
    assert equity.imbalance == Decimal("0.3")
    assert equity.state is FlowState.BUY_DOMINANT
    futoi = adapter.futoi(ticker="SBERF", as_of=AS_OF)
    assert futoi.groups[0].gross_short == Decimal("100")
    concentration = adapter.concentration(secid="SBER", as_of=AS_OF)
    report = render_flow_report(equity, futoi, concentration)
    assert "не число открытых шортов" in report
    assert "YUR" in report
