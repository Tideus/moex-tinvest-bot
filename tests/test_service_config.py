import json
from pathlib import Path

import pytest

from moex_bot.service_config import (
    TInvestEnvironment,
    load_service_config,
    resolve_tinvest_runtime,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_official_service_config_is_accepted() -> None:
    config = load_service_config(PROJECT_ROOT / "config" / "services.json")
    assert config.t_invest.prod_grpc == "invest-public-api.tbank.ru:443"
    assert config.t_invest.sandbox_rest.startswith(
        "https://sandbox-invest-public-api.tbank.ru/"
    )


def test_unapproved_prod_host_is_rejected(tmp_path: Path) -> None:
    raw = json.loads((PROJECT_ROOT / "config" / "services.json").read_text(encoding="utf-8"))
    raw["t_invest"]["prod_rest"] = "https://evil.example/rest"
    path = tmp_path / "services.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="approved official host"):
        load_service_config(path)


def test_runtime_switch_selects_credentials_and_never_enables_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("T_INVEST_PROD_TOKEN", "secret")
    monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "account")
    config = load_service_config(PROJECT_ROOT / "config" / "services.json")
    runtime = resolve_tinvest_runtime(config, environment=TInvestEnvironment.PROD)
    assert runtime.grpc_endpoint == "invest-public-api.tbank.ru:443"
    assert runtime.token_env == "T_INVEST_PROD_TOKEN"
    assert runtime.live_orders_enabled is False
