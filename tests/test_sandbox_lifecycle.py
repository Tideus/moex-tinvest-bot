from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from moex_bot.integrations.tinvest_sandbox import SandboxAccount
from moex_bot.sandbox_lifecycle import masked_preview, replace_sandbox_accounts, replacement_preview

NOW = datetime(2026, 10, 30, 15, tzinfo=UTC)
IDS = {"long": "old-long", "intraday": "old-intraday"}
KEYS = {
    "long": "T_INVEST_SANDBOX_LONG_ACCOUNT_ID",
    "intraday": "T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID",
}


class FakeService:
    def __init__(self) -> None:
        self.known = {
            value: SandboxAccount(value, "bot-" + key, "ACCOUNT_STATUS_OPEN")
            for key, value in IDS.items()
        }
        self.known["unrelated"] = SandboxAccount("unrelated", "manual", "ACCOUNT_STATUS_OPEN")
        self.balances: dict[str, Decimal] = {key: Decimal("0") for key in self.known}
        self.calls: list[str] = []
        self.timeout_creation = False
        self.timeout_funding = False
        self.moved = False

    def accounts(self) -> tuple[SandboxAccount, ...]:
        return tuple(self.known.values())

    def account_state(self, account_id: str) -> dict[str, object]:
        return {
            "portfolio": {"positions": [], "totalAmountPortfolio": {"units": "300000"}},
            "positions": {"securities": [{"balance": "11" if self.moved else "10"}]},
            "orders": {"orders": []},
            "stop_orders": {"stopOrders": []},
        }

    def open_account(self, name: str) -> str:
        account_id = f"new-{len(self.calls)}"
        self.calls.append("open")
        self.known[account_id] = SandboxAccount(account_id, name, "ACCOUNT_STATUS_OPEN")
        self.balances[account_id] = Decimal("0")
        if self.timeout_creation:
            self.timeout_creation = False
            raise TimeoutError("creation response lost")
        return account_id

    def available_rub_balance(self, account_id: str) -> Decimal:
        return self.balances[account_id]

    def pay_in(self, account_id: str, amount: Decimal) -> Decimal:
        self.calls.append("pay_in")
        self.balances[account_id] += amount
        if self.timeout_funding:
            self.timeout_funding = False
            raise TimeoutError("funding response lost")
        return self.balances[account_id]

    def close_account(self, account_id: str) -> None:
        self.calls.append("close:" + account_id)
        del self.known[account_id]


def _args(tmp_path: Path, service: FakeService) -> dict:
    env = tmp_path / "bot.env"
    env.write_text("T_INVEST_PROD_ACCOUNT_ID=keep-iis\nTELEGRAM_CHAT_ID=keep-chat\n")
    preview = replacement_preview(service, IDS, as_of=NOW)  # type: ignore[arg-type]
    return {
        "preview": preview,
        "confirmation_hash": preview["confirmation_hash"],
        "configured": IDS,
        "env_keys": KEYS,
        "env_path": env,
        "state_path": tmp_path / "replacement.json",
        "as_of": NOW,
    }


def test_confirmed_replacement_is_verified_and_leaves_other_accounts_and_env_intact(
    tmp_path: Path,
) -> None:
    service = FakeService()
    kwargs = _args(tmp_path, service)
    result = replace_sandbox_accounts(service, **kwargs)  # type: ignore[arg-type]
    assert result["status"] == "EXECUTED_VERIFIED"
    assert "unrelated" in service.known
    assert not (set(IDS.values()) & set(service.known))
    assert service.calls[:4] == ["open", "pay_in", "open", "pay_in"]
    assert service.calls[4:] == ["close:old-long", "close:old-intraday"]
    text = kwargs["env_path"].read_text()
    assert "T_INVEST_PROD_ACCOUNT_ID=keep-iis" in text and "TELEGRAM_CHAT_ID=keep-chat" in text
    before = list(service.calls)
    replace_sandbox_accounts(service, **kwargs)  # type: ignore[arg-type]
    assert service.calls == before


@pytest.mark.parametrize("fault", ["confirmation", "changed_holdings", "binding", "tamper"])
def test_replacement_does_not_mutate_without_matching_fresh_preview(
    tmp_path: Path,
    fault: str,
) -> None:
    service = FakeService()
    kwargs = _args(tmp_path, service)
    if fault == "confirmation":
        kwargs["confirmation_hash"] = "wrong"
    if fault == "changed_holdings":
        service.moved = True
    if fault == "binding":
        kwargs["configured"] = {"long": "unrelated", "intraday": "old-intraday"}
    if fault == "tamper":
        kwargs["preview"] = deepcopy(kwargs["preview"])
        kwargs["preview"]["accounts"][0]["account_id"] = "unrelated"
    with pytest.raises(ValueError):
        replace_sandbox_accounts(service, **kwargs)  # type: ignore[arg-type]
    assert not service.calls


@pytest.mark.parametrize("fault", ["timeout_creation", "timeout_funding"])
def test_lost_response_does_not_duplicate_accounts_or_virtual_payments(
    tmp_path: Path,
    fault: str,
) -> None:
    service = FakeService()
    kwargs = _args(tmp_path, service)
    setattr(service, fault, True)
    with pytest.raises(TimeoutError):
        replace_sandbox_accounts(service, **kwargs)  # type: ignore[arg-type]
    result = replace_sandbox_accounts(service, **kwargs)  # type: ignore[arg-type]
    assert result["status"] == "EXECUTED_VERIFIED"
    assert service.calls.count("open") == 2 and service.calls.count("pay_in") == 2


def test_masked_preview_never_prints_full_account_id(tmp_path: Path) -> None:
    service = FakeService()
    kwargs = _args(tmp_path, service)
    assert "old-intraday" not in masked_preview(kwargs["preview"])
