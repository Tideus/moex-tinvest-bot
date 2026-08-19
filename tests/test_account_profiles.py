import json
from pathlib import Path

import pytest

from moex_bot.account_profiles import AccountPurpose, load_account_registry
from moex_bot.intraday_config import load_intraday_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_two_sandbox_accounts_have_separate_capital_and_strategies() -> None:
    registry = load_account_registry(PROJECT_ROOT / "config" / "accounts.json")
    long = registry.by_id("long")
    intraday = registry.by_id("intraday")
    assert long.purpose is AccountPurpose.LONG
    assert intraday.purpose is AccountPurpose.INTRADAY
    assert long.target_balance_rub == intraday.target_balance_rub == 300_000
    assert long.account_id_env != intraday.account_id_env
    assert long.strategies != intraday.strategies
    assert not long.order_execution_enabled
    assert intraday.order_execution_enabled


def test_intraday_defaults_allow_only_sandbox_and_never_overnight() -> None:
    config = load_intraday_config(PROJECT_ROOT / "config" / "intraday.json")
    assert config.execution_stage == "sandbox"
    assert config.candle_minutes == 5
    assert not config.allow_overnight
    assert config.enabled_strategies == ("intraday_momentum_v1",)


def test_account_registry_rejects_shared_account_id_env(tmp_path: Path) -> None:
    raw = json.loads((PROJECT_ROOT / "config" / "accounts.json").read_text())
    raw["profiles"][1]["account_id_env"] = raw["profiles"][0]["account_id_env"]
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="valid and unique"):
        load_account_registry(path)


def test_intraday_config_rejects_production_stage(tmp_path: Path) -> None:
    raw = json.loads((PROJECT_ROOT / "config" / "intraday.json").read_text())
    raw["execution_stage"] = "production"
    path = tmp_path / "intraday.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="research_only or sandbox"):
        load_intraday_config(path)
