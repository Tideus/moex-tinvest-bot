import json
from decimal import Decimal
from pathlib import Path

import pytest

from moex_bot.config import BotConfig, StrategyConfig, load_config
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


def test_loader_rejects_string_instead_of_json_boolean(tmp_path: Path) -> None:
    payload = {
        "mode": "shadow",
        "base_currency": "RUB",
        "max_data_age_seconds": 3600,
        "max_position_weight": "0.1",
        "max_order_notional": "10000",
        "max_gross_exposure": "0.8",
        "max_daily_turnover": "50000",
        "min_trade_notional": "500",
        "max_open_orders": 5,
        "allow_margin": "false",
        "live_interlock": False,
        "strategy": {
            "top_n": 5,
            "min_momentum": "0",
            "require_above_trend": True,
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON boolean"):
        load_config(path)


def test_config_rejects_inconsistent_notional_limits() -> None:
    config = BotConfig(
        ExecutionMode.SHADOW,
        "RUB",
        3600,
        Decimal("0.1"),
        Decimal("10000"),
        Decimal("0.8"),
        Decimal("5000"),
        Decimal("20000"),
        5,
        False,
        False,
        StrategyConfig(5, Decimal("0"), True),
    )
    errors = " ".join(config.validate())
    assert "min_trade_notional" in errors
    assert "max_order_notional" in errors
