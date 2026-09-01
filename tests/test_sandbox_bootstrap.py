from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from moex_bot.cli import sandbox_bootstrap, sandbox_bootstrap_profiles
from moex_bot.domain import Instrument
from moex_bot.env_file import upsert_env_value
from moex_bot.integrations.tinvest_sandbox import (
    SandboxAccountBootstrap,
    TInvestSandboxAccountService,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def test_broker_snapshot_reads_cash_positions_and_active_orders() -> None:
    transport = SequencedTransport(
        [
            {
                "totalAmountPortfolio": {
                    "currency": "rub",
                    "units": "120000",
                    "nano": 0,
                },
                "positions": [
                    {
                        "instrumentUid": "uid-sber",
                        "instrumentType": "share",
                        "quantity": {"units": "30", "nano": 0},
                        "blockedLots": {"units": "2", "nano": 0},
                        "currentPrice": {"units": "310", "nano": 0},
                    }
                ],
            },
            {
                "money": [{"currency": "rub", "units": "80000", "nano": 0}],
                "blocked": [{"currency": "rub", "units": "5000", "nano": 0}],
                "limitsLoadingInProgress": False,
            },
            {"orders": [{"orderId": "one"}]},
        ]
    )
    service = TInvestSandboxAccountService("token", transport)
    instrument = Instrument("SBER", "uid-sber", "TQBR", 10, Decimal("0.01"))
    snapshot = service.broker_snapshot("sandbox-1", {instrument.uid: instrument})

    assert snapshot.cash_available == Decimal("80000")
    assert snapshot.cash_blocked == Decimal("5000")
    assert snapshot.reported_equity == Decimal("120000")
    assert snapshot.positions_lots == {"SBER": 3}
    assert snapshot.blocked_lots == {"SBER": 2}
    assert snapshot.position_values == {"SBER": Decimal("9300")}
    assert snapshot.as_portfolio_payload()["positions"]["SBER"]["current_value"] == "9300"
    assert snapshot.open_orders == 1
    assert transport.calls[0]["url"].endswith("/GetSandboxPortfolio")
    assert transport.calls[1]["url"].endswith("/GetSandboxPositions")
    assert transport.calls[2]["url"].endswith("/GetSandboxOrders")


def test_broker_snapshot_fails_closed_for_position_outside_universe() -> None:
    transport = SequencedTransport(
        [
            {
                "totalAmountPortfolio": {"currency": "rub", "units": "1", "nano": 0},
                "positions": [
                    {
                        "instrumentUid": "unknown",
                        "instrumentType": "share",
                        "quantity": {"units": "1", "nano": 0},
                    }
                ],
            },
            {"money": [], "blocked": [], "limitsLoadingInProgress": False},
            {"orders": []},
        ]
    )
    service = TInvestSandboxAccountService("token", transport)
    with pytest.raises(ValueError, match="outside verified universe"):
        service.broker_snapshot("sandbox-1", {})


def test_operations_follow_cursor_and_normalize_trade() -> None:
    transport = SequencedTransport(
        [
            {
                "items": [
                    {
                        "id": "operation-1",
                        "date": "2026-08-18T08:00:00Z",
                        "type": "OPERATION_TYPE_BUY",
                        "instrumentUid": "uid-sber",
                        "commission": {"units": "1", "nano": 500000000},
                        "tradesInfo": {
                            "trades": [
                                {
                                    "quantity": "10",
                                    "price": {"units": "300", "nano": 0},
                                }
                            ]
                        },
                    }
                ],
                "hasNext": True,
                "nextCursor": "page-2",
            },
            {"items": [], "hasNext": False},
        ]
    )
    service = TInvestSandboxAccountService("token", transport)
    instrument = Instrument("SBER", "uid-sber", "TQBR", 10, Decimal("0.01"))
    operations = service.operations(
        "sandbox-1",
        {instrument.uid: instrument},
        from_time=datetime(2026, 8, 18, tzinfo=UTC),
        to_time=datetime(2026, 8, 19, tzinfo=UTC),
    )
    assert len(operations) == 1
    assert operations[0].secid == "SBER"
    assert operations[0].side == "BUY"
    assert operations[0].gross == Decimal("3000")
    assert operations[0].commission == Decimal("1.5")
    assert transport.calls[1]["payload"]["cursor"] == "page-2"


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


class FakeProfileSandboxService:
    def __init__(self) -> None:
        self.ids = iter(("long-id", "intraday-id"))
        self.funded: list[tuple[str, Decimal]] = []

    def ensure_account(
        self, configured_account_id: str | None, *, account_name: str
    ) -> SandboxAccountBootstrap:
        assert configured_account_id == ""
        assert account_name in {
            "moex-tinvest-bot-long",
            "moex-tinvest-bot-intraday",
        }
        return SandboxAccountBootstrap(next(self.ids), created=True)

    def available_rub_balance(self, account_id: str) -> Decimal:
        return Decimal("100000") if account_id == "long-id" else Decimal("250000")

    def pay_in(self, account_id: str, amount: Decimal) -> Decimal:
        self.funded.append((account_id, amount))
        return Decimal("300000")


def test_profile_bootstrap_creates_and_funds_two_separate_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("T_INVEST_SANDBOX_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("T_INVEST_SANDBOX_LONG_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("T_INVEST_SANDBOX_TOKEN=secret\n", encoding="utf-8")
    service = FakeProfileSandboxService()
    status = sandbox_bootstrap_profiles(
        env_path=env_path,
        accounts_path=PROJECT_ROOT / "config" / "accounts.json",
        fund_targets=True,
        service=service,  # type: ignore[arg-type]
    )
    assert status == 0
    assert service.funded == [
        ("long-id", Decimal("200000")),
        ("intraday-id", Decimal("50000")),
    ]
    content = env_path.read_text(encoding="utf-8")
    assert "T_INVEST_SANDBOX_LONG_ACCOUNT_ID=long-id" in content
    assert "T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID=intraday-id" in content
    assert "T_INVEST_SANDBOX_ACCOUNT_ID=long-id" in content


def test_profile_bootstrap_reuses_legacy_long_account_during_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str | None, str]] = []

    class MigrationService(FakeProfileSandboxService):
        def ensure_account(
            self, configured_account_id: str | None, *, account_name: str
        ) -> SandboxAccountBootstrap:
            seen.append((configured_account_id, account_name))
            if account_name.endswith("-long"):
                return SandboxAccountBootstrap("legacy-long", created=False)
            return SandboxAccountBootstrap("intraday-id", created=True)

    monkeypatch.setenv("T_INVEST_SANDBOX_ACCOUNT_ID", "legacy-long")
    monkeypatch.delenv("T_INVEST_SANDBOX_LONG_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("T_INVEST_SANDBOX_TOKEN=secret\n", encoding="utf-8")
    status = sandbox_bootstrap_profiles(
        env_path=env_path,
        accounts_path=PROJECT_ROOT / "config" / "accounts.json",
        fund_targets=False,
        service=MigrationService(),  # type: ignore[arg-type]
    )
    assert status == 0
    assert seen[0][0] == "legacy-long"
    assert "T_INVEST_SANDBOX_LONG_ACCOUNT_ID=legacy-long" in env_path.read_text(
        encoding="utf-8"
    )


def test_profile_bootstrap_refuses_conflicting_long_account_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("T_INVEST_SANDBOX_ACCOUNT_ID", "old-long")
    monkeypatch.setenv("T_INVEST_SANDBOX_LONG_ACCOUNT_ID", "new-long")
    monkeypatch.delenv("T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID", raising=False)
    status = sandbox_bootstrap_profiles(
        env_path=tmp_path / ".env",
        accounts_path=PROJECT_ROOT / "config" / "accounts.json",
        fund_targets=False,
        service=FakeProfileSandboxService(),  # type: ignore[arg-type]
    )
    assert status == 2
