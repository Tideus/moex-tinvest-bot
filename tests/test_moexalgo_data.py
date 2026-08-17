from datetime import UTC, datetime, timedelta
from typing import Any

from moex_bot.integrations.moexalgo_data import MoexAlgoReadOnlyAdapter


class FakeFrame:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def to_dict(self, orient: str) -> list[dict[str, Any]]:
        assert orient == "records"
        return self.rows


class FakeTicker:
    def __init__(self, as_of: datetime) -> None:
        self.as_of = as_of

    def candles(self, *, start: str, end: str, period: str) -> FakeFrame:
        assert start < end
        assert period == "1h"
        rows: list[dict[str, Any]] = []
        for index in range(25):
            candle_end = self.as_of - timedelta(hours=25 - index)
            rows.append({"end": candle_end.isoformat(), "close": str(100 + index)})
        rows.append(
            {
                "end": (self.as_of + timedelta(minutes=30)).isoformat(),
                "close": "9999",
            }
        )
        return FakeFrame(rows)


def test_completed_hourly_candles_build_observation_without_lookahead() -> None:
    as_of = datetime(2026, 8, 14, 12, 30, tzinfo=UTC)
    adapter = MoexAlgoReadOnlyAdapter(
        lambda secid, board: FakeTicker(as_of),
        lambda secid, board: {
            "ticker": secid,
            "board": board,
            "lotsize": 10,
            "minstep": 0.01,
        },
    )
    observation = adapter.hourly_observation(
        secid="SBER",
        uid="e6123145-9665-43e0-8413-cd61b8aa9b13",
        board="TQBR",
        as_of=as_of,
    )
    assert observation.price == 124
    assert observation.price != 9999
    assert observation.complete
    assert observation.instrument.lot_size == 10
    assert observation.observed_at < as_of
