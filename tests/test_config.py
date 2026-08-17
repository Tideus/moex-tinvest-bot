from decimal import Decimal

from moex_bot.config import BotConfig, StrategyConfig
from moex_bot.domain import ExecutionMode


def test_live_mode_is_rejected_by_scaffold() -> None:
    config = BotConfig(
        ExecutionMode.LIVE,
        "RUB",
        3600,
        Decimal("0.1"),
        Decimal("10000"),
        Decimal("0.8"),
        Decimal("50000"),
        Decimal("500"),
        5,
        False,
        False,
        StrategyConfig(5, Decimal("0"), True),
    )
    assert "live mode" in " ".join(config.validate())
