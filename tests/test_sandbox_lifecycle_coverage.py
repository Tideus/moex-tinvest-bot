"""Explicit sandbox replacement rejects ambiguous state without retries."""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from test_sandbox_lifecycle import IDS, NOW, FakeService, _args

from moex_bot.sandbox_lifecycle import (
    _save,
    masked_preview,
    replace_sandbox_accounts,
    replacement_preview,
)


@pytest.mark.parametrize(
    "fault", ["naive", "config", "distinct", "closed", "state", "lists", "equity"]
)
def test_preview_validation(tmp_path: Path, fault: str) -> None:
    service = FakeService()
    if fault == "closed":
        service.known.pop(IDS["long"])
    original = service.account_state

    def account_state(account):
        state = original(account)
        if fault == "state":
            state["orders"] = None
        elif fault == "lists":
            state["orders"]["orders"] = {}
        elif fault == "equity":
            state["portfolio"].pop("totalAmountPortfolio")
        return state

    with patch.object(service, "account_state", side_effect=account_state):
        if fault == "equity":
            assert (
                replacement_preview(service, IDS, as_of=NOW)["accounts"][0]["equity_rub"]
                == "unknown"
            )
        else:
            with pytest.raises(ValueError):
                replacement_preview(
                    service,
                    {}
                    if fault == "config"
                    else {"long": "a", "intraday": "a"}
                    if fault == "distinct"
                    else IDS,
                    as_of=NOW.replace(tzinfo=None) if fault == "naive" else NOW,
                )


@pytest.mark.parametrize(
    "fault", ["lock", "naive", "path", "keys", "bindings", "rows", "expired", "occupied", "journal"]
)
def test_replace_validation(tmp_path: Path, fault: str) -> None:
    service = FakeService()
    kw = _args(tmp_path, service)
    if fault == "lock":
        kw["state_path"].with_suffix(".lock").mkdir()
    elif fault == "naive":
        kw["as_of"] = NOW.replace(tzinfo=None)
    elif fault == "path":
        kw["state_path"] = kw["env_path"]
    elif fault == "keys":
        kw["env_keys"] = {}
    elif fault == "bindings":
        kw["env_keys"] = {"long": "PROD", "intraday": "OTHER"}
    elif fault == "rows":
        kw["preview"]["accounts"] = []
    elif fault == "expired":
        kw["as_of"] += timedelta(hours=1)
    else:
        state = {
            "confirmation_hash": "wrong" if fault == "occupied" else kw["confirmation_hash"],
            "new": [],
        }
        kw["state_path"].write_text(json.dumps(state))
    with pytest.raises(ValueError):
        replace_sandbox_accounts(service, **kw)
    assert not service.calls
    with pytest.raises(ValueError):
        masked_preview({"accounts": None})


@pytest.mark.parametrize(
    "fault",
    [
        "ambiguous_create",
        "ambiguous_fund",
        "balance",
        "fundverify",
        "identity",
        "closepending",
        "closechanged",
        "closeincomplete",
        "closeverify",
        "missingold",
    ],
)
def test_resume_and_network_postconditions_are_fail_closed(tmp_path: Path, fault: str) -> None:
    service = FakeService()
    kw = _args(tmp_path, service)
    service.timeout_creation = True
    with pytest.raises(TimeoutError):
        replace_sandbox_accounts(service, **kw)
    state = json.loads(kw["state_path"].read_text())
    new_id = state["new"]["long"].get("id") or next(
        a.account_id for a in service.accounts() if a.name == state["new"]["long"]["name"]
    )
    if fault == "ambiguous_create":
        service.known.pop(new_id)
    elif fault in {"ambiguous_fund", "balance", "fundverify"}:
        state["new"]["long"].update(
            id=new_id, status="funding_pending" if fault == "ambiguous_fund" else "created"
        )
        if fault == "balance":
            service.balances[new_id] = Decimal(1)
        if fault == "fundverify":
            service.pay_in = lambda *args: Decimal(0)
    elif fault == "identity":
        state["new"] = {}
        service.open_account = lambda name: IDS["long"]
    else:
        for profile in ("long", "intraday"):
            state["new"][profile] = {"id": "new-" + profile, "status": "funded"}
            service.balances["new-" + profile] = Decimal(300000)
        state["bindings_updated"] = True
        if fault == "closepending":
            state["closed"]["long"] = "close_pending"
        elif fault == "closechanged":
            service.moved = True
        elif fault == "closeincomplete":
            service.account_state = lambda account: {"orders": None, "stop_orders": {}}
        elif fault == "closeverify":
            service.close_account = lambda account: None
        elif fault == "missingold":
            service.known.pop(IDS["long"])
            service.known.pop(IDS["intraday"])
    kw["state_path"].write_text(json.dumps(state))
    if fault == "missingold":
        assert replace_sandbox_accounts(service, **kw)["status"] == "EXECUTED_VERIFIED"
    else:
        with pytest.raises(ValueError):
            replace_sandbox_accounts(service, **kw)


@pytest.mark.parametrize("fail", [False, True])
def test_private_state_save_fsync_directory_and_cleanup(tmp_path: Path, fail: bool) -> None:
    fsync = Mock(side_effect=OSError("disk error") if fail else None)
    platform = SimpleNamespace(
        name="posix",
        fsync=fsync,
        open=Mock(return_value=123),
        close=Mock(),
        O_RDONLY=0,
        O_DIRECTORY=0,
    )
    path = tmp_path / "state.json"
    with patch("moex_bot.sandbox_lifecycle.os", platform):
        if fail:
            with pytest.raises(OSError):
                _save(path, {"test": True})
            assert not path.exists()
        else:
            _save(path, {"test": True})
            assert json.loads(path.read_text()) == {"test": True}
            platform.close.assert_called_once_with(123)
    assert not list(tmp_path.glob(".*.tmp"))


def test_resumed_accounts_must_still_be_distinct(tmp_path: Path) -> None:
    service = FakeService()
    kw = _args(tmp_path, service)
    state = {
        "confirmation_hash": kw["confirmation_hash"],
        "operation_id": "test",
        "old": IDS,
        "new": {profile: {"id": "same-new", "status": "funded"} for profile in IDS},
        "closed": {},
        "bindings_updated": False,
    }
    service.balances["same-new"] = Decimal(300000)
    kw["state_path"].write_text(json.dumps(state))
    with pytest.raises(ValueError, match="remain distinct"):
        replace_sandbox_accounts(service, **kw)


def test_windows_state_save_skips_posix_permissions_on_all_hosts(tmp_path: Path) -> None:
    from moex_bot import sandbox_lifecycle
    from moex_bot.env_file import upsert_env_values

    # Replace only each module's os binding, never the process-global os.name
    # (the latter would make pathlib select the wrong native Path implementation).
    native = sandbox_lifecycle.os
    platform = SimpleNamespace(**{name: getattr(native, name) for name in dir(native)})
    platform.name = "nt"
    with patch("moex_bot.sandbox_lifecycle.os", platform):
        path = tmp_path / "windows-state.json"
        _save(path, {"mode": "windows"})
        assert json.loads(path.read_text()) == {"mode": "windows"}
    with patch("moex_bot.env_file.os", platform):
        env = tmp_path / "windows.env"
        upsert_env_values(env, {"ACCOUNT_ID": "sandbox-only"})
        assert env.read_text() == "ACCOUNT_ID=sandbox-only\n"
