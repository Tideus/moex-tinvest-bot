from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from moex_bot.cli import sandbox_bootstrap
from moex_bot.env_file import upsert_env_value
from moex_bot.integrations.tinvest_sandbox import (
    SandboxAccountBootstrap,
    TInvestSandboxAccountService,
)


class SequencedTransport:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        return self.responses.pop(0)


def test_existing_open_account_is_not_duplicated() -> None:
    transport = SequencedTransport(
        [
            {
                "accounts": [
                    {
                        "id": "sandbox-1",
                        "name": "moex-tinvest-bot",
                        "status": "ACCOUNT_STATUS_OPEN",
                    }
                ]
            }
        ]
    )
    service = TInvestSandboxAccountService("token", transport)
    result = service.ensure_account("sandbox-1", account_name="moex-tinvest-bot")
    assert result == SandboxAccountBootstrap("sandbox-1", created=False)
    assert len(transport.calls) == 1
    assert transport.calls[0]["url"].endswith("/GetSandboxAccounts")


def test_missing_account_is_created_on_fixed_sandbox_host() -> None:
    transport = SequencedTransport([{"accounts": []}, {"accountId": "sandbox-new"}])
    service = TInvestSandboxAccountService("token", transport)
    result = service.ensure_account("expired", account_name="moex-tinvest-bot")
    assert result == SandboxAccountBootstrap("sandbox-new", created=True)
    assert "sandbox-invest-public-api.tbank.ru" in transport.calls[1]["url"]
    assert transport.calls[1]["payload"] == {"name": "moex-tinvest-bot"}


def test_available_rub_balance_ignores_other_currencies() -> None:
    transport = SequencedTransport(
        [
            {
                "money": [
                    {"currency": "rub", "units": "125000", "nano": 500000000},
                    {"currency": "usd", "units": "50", "nano": 0},
                ]
            }
        ]
    )
    service = TInvestSandboxAccountService("token", transport)
    assert service.available_rub_balance("sandbox-1") == Decimal("125000.5")
    assert transport.calls[0]["payload"] == {"accountId": "sandbox-1"}


def test_pay_in_uses_money_value_and_returns_current_balance() -> None:
    transport = SequencedTransport(
        [{"balance": {"currency": "rub", "units": "300000", "nano": 250000000}}]
    )
    service = TInvestSandboxAccountService("token", transport)
    balance = service.pay_in("sandbox-1", Decimal("150000.25"))
    assert balance == Decimal("300000.25")
    assert transport.calls[0]["payload"] == {
        "accountId": "sandbox-1",
        "amount": {"currency": "rub", "units": "150000", "nano": 250000000},
    }


def test_env_upsert_is_idempotent_and_removes_duplicate_keys(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "TOKEN=secret\nT_INVEST_SANDBOX_ACCOUNT_ID=old\n"
        "T_INVEST_SANDBOX_ACCOUNT_ID=duplicate\n",
        encoding="utf-8",
    )
    upsert_env_value(env_path, "T_INVEST_SANDBOX_ACCOUNT_ID", "sandbox-new")
    content = env_path.read_text(encoding="utf-8")
    assert "TOKEN=secret" in content
    assert content.count("T_INVEST_SANDBOX_ACCOUNT_ID=") == 1
    assert "T_INVEST_SANDBOX_ACCOUNT_ID=sandbox-new" in content


class FakeSandboxService:
    def __init__(self) -> None:
        self.top_up: Decimal | None = None

    def ensure_account(
        self, configured_account_id: str | None, *, account_name: str
    ) -> SandboxAccountBootstrap:
        assert configured_account_id == ""
        assert account_name == "moex-tinvest-bot"
        return SandboxAccountBootstrap("sandbox-new", created=True)

    def available_rub_balance(self, account_id: str) -> Decimal:
        assert account_id == "sandbox-new"
        return Decimal("100000")

    def pay_in(self, account_id: str, amount: Decimal) -> Decimal:
        assert account_id == "sandbox-new"
        self.top_up = amount
        return Decimal("150000")


def test_cli_bootstrap_saves_id_prompts_and_tops_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("T_INVEST_SANDBOX_ACCOUNT_ID", raising=False)
    monkeypatch.setattr("builtins.input", lambda _: "50 000,00")
    env_path = tmp_path / ".env"
    env_path.write_text("T_INVEST_SANDBOX_TOKEN=secret\n", encoding="utf-8")
    service = FakeSandboxService()

    status = sandbox_bootstrap(
        env_path=env_path,
        account_name="moex-tinvest-bot",
        top_up=None,
        no_prompt=False,
        service=service,  # type: ignore[arg-type]
    )

    assert status == 0
    assert service.top_up == Decimal("50000.00")
    assert "T_INVEST_SANDBOX_ACCOUNT_ID=sandbox-new" in env_path.read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert "Available sandbox balance: 100 000.00 RUB" in output
    assert "new sandbox balance: 150 000.00 RUB" in output


def test_noninteractive_bootstrap_never_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("T_INVEST_SANDBOX_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(
        "builtins.input", lambda _: pytest.fail("input must not be called in noninteractive mode")
    )
    service = FakeSandboxService()
    assert (
        sandbox_bootstrap(
            env_path=tmp_path / ".env",
            account_name="moex-tinvest-bot",
            top_up=None,
            no_prompt=True,
            service=service,  # type: ignore[arg-type]
        )
        == 0
    )
    assert service.top_up is None
