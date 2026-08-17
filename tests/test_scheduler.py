from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from moex_bot.scheduler import is_conservative_stock_window, next_hourly_run


def test_next_hourly_run_uses_moscow_hh05() -> None:
    now = datetime(2026, 8, 14, 10, 6, tzinfo=ZoneInfo("Europe/Moscow"))
    result = next_hourly_run(now)
    assert result.isoformat() == "2026-08-14T11:05:00+03:00"


def test_scheduler_rejects_naive_time() -> None:
    with pytest.raises(ValueError):
        next_hourly_run(datetime(2026, 8, 14, 10, 0))


def test_conservative_stock_window_skips_after_close_and_weekends() -> None:
    moscow = ZoneInfo("Europe/Moscow")
    assert is_conservative_stock_window(datetime(2026, 8, 14, 10, 5, tzinfo=moscow))
    assert not is_conservative_stock_window(datetime(2026, 8, 14, 23, 6, tzinfo=moscow))
    assert not is_conservative_stock_window(datetime(2026, 8, 15, 10, 5, tzinfo=moscow))
