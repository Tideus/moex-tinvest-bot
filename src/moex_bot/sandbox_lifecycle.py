"""Explicit account replacement previews, separate from trading execution."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .env_file import upsert_env_values
from .integrations.tinvest_sandbox import TInvestSandboxAccountService, money_value_to_decimal


def replacement_preview(
    service: TInvestSandboxAccountService,
    configured: dict[str, str],
    *,
    as_of: datetime,
) -> dict[str, object]:
    if as_of.tzinfo is None:
        raise ValueError("preview timestamp requires timezone")
    if set(configured) != {"long", "intraday"} or any(not x for x in configured.values()):
        raise ValueError("exactly two configured sandbox accounts required")
    if len(set(configured.values())) != 2:
        raise ValueError("account profiles must be distinct")
    accounts = {account.account_id: account for account in service.accounts()}
    preview: list[dict[str, object]] = []
    for profile, account_id in configured.items():
        account = accounts.get(account_id)
        if account is None or not account.is_open:
            raise ValueError(f"configured {profile} sandbox account is not open")
        state = service.account_state(account_id)
        portfolio = state["portfolio"]
        positions = state["positions"]
        orders = state["orders"]
        stops = state["stop_orders"]
        if not all(isinstance(x, dict) for x in (portfolio, positions, orders, stops)):
            raise ValueError("account state is incomplete")
        assert isinstance(portfolio, dict) and isinstance(positions, dict)
        assert isinstance(orders, dict) and isinstance(stops, dict)
        active = orders.get("orders", [])
        active_stops = stops.get("stopOrders", [])
        holdings = portfolio.get("positions", [])
        if not all(isinstance(x, list) for x in (active, active_stops, holdings)):
            raise ValueError("account state lists are invalid")
        total = portfolio.get("totalAmountPortfolio")
        equity = money_value_to_decimal(total) if isinstance(total, dict) else None
        preview.append(
            {
                "profile": profile,
                "account_id": account_id,
                "account_name": account.name,
                "masked_id": "…" + account_id[-8:],
                "status": account.status,
                "equity_rub": str(equity) if equity is not None else "unknown",
                "holdings": holdings,
                "positions": positions,
                "active_orders": active,
                "active_stop_orders": active_stops,
                "new_balance_rub": str(Decimal("300000")),
            }
        )
    digest = hashlib.sha256(json.dumps(preview, sort_keys=True, default=str).encode()).hexdigest()
    return {
        "version": 1,
        "mode": "sandbox",
        "as_of": as_of.isoformat(),
        "confirmation_hash": digest,
        "accounts": preview,
        "action": "replace_two_configured_sandbox_accounts",
        "real_money": False,
    }


def masked_preview(preview: dict[str, object]) -> str:
    rows = preview["accounts"]
    if not isinstance(rows, list):
        raise ValueError("invalid replacement preview")
    lines = ["PREVIEW_ONLY: sandbox account replacement (irreversible old-account closure)"]
    for row in rows:
        lines.append(
            f"- {row['profile']}: {row['account_name']} {row['masked_id']} "
            f"equity={row['equity_rub']} RUB; "
            f"orders={len(row['active_orders'])}; stops={len(row['active_stop_orders'])}; "
            "replacement=300000 RUB"
        )
    lines.append(f"confirmation_hash={preview['confirmation_hash']}")
    return "\n".join(lines)


def _save(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            if os.name != "nt":
                temporary.chmod(0o600)
            json.dump(value, stream, default=str, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def _fingerprint(rows: list[dict[str, object]]) -> str:
    # Portfolio market prices may change without changing the approved holdings.
    material = [
        {
            key: row[key]
            for key in (
                "profile",
                "account_id",
                "account_name",
                "status",
                "positions",
                "active_orders",
                "active_stop_orders",
            )
        }
        for row in rows
    ]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def replace_sandbox_accounts(
    service: TInvestSandboxAccountService,
    *,
    preview: dict[str, object],
    confirmation_hash: str,
    configured: dict[str, str],
    env_keys: dict[str, str],
    env_path: Path,
    state_path: Path,
    as_of: datetime,
) -> dict[str, object]:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock = state_path.with_suffix(".lock")
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise ValueError("replacement already running or requires crash-lock recovery") from exc
    try:
        return _replace_sandbox_accounts(
            service,
            preview=preview,
            confirmation_hash=confirmation_hash,
            configured=configured,
            env_keys=env_keys,
            env_path=env_path,
            state_path=state_path,
            as_of=as_of,
        )
    finally:
        lock.rmdir()


def _replace_sandbox_accounts(
    service: TInvestSandboxAccountService,
    *,
    preview: dict[str, object],
    confirmation_hash: str,
    configured: dict[str, str],
    env_keys: dict[str, str],
    env_path: Path,
    state_path: Path,
    as_of: datetime,
) -> dict[str, object]:
    """Confirmed replacement only. Durable pending states prohibit blind RPC retries."""
    if as_of.tzinfo is None or preview.get("mode") != "sandbox" or preview.get("version") != 1:
        raise ValueError("sandbox replacement requires a valid preview")
    if state_path.resolve() == env_path.resolve() or state_path.suffix != ".json":
        raise ValueError("replacement state must be a separate JSON file")
    if set(env_keys) != {"long", "intraday"}:
        raise ValueError("two sandbox env bindings required")
    if set(env_keys.values()) != {
        "T_INVEST_SANDBOX_LONG_ACCOUNT_ID",
        "T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID",
    }:
        raise ValueError("only the two fixed sandbox env bindings may change")
    rows = preview.get("accounts")
    if not isinstance(rows, list) or len(rows) != 2 or not all(isinstance(x, dict) for x in rows):
        raise ValueError("invalid replacement account list")
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
    if preview.get("confirmation_hash") != digest or confirmation_hash != digest:
        raise ValueError("replacement confirmation does not match preview")
    old = {row["profile"]: row["account_id"] for row in rows}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("confirmation_hash") != digest:
            raise ValueError("another sandbox replacement operation already occupies this state")
    else:
        preview_at = datetime.fromisoformat(str(preview.get("as_of", "")))
        if preview_at.tzinfo is None or not timedelta(0) <= as_of - preview_at <= timedelta(
            minutes=30
        ):
            raise ValueError("replacement preview expired")
        if configured != old:
            raise ValueError("configured accounts changed since preview")
        refreshed = replacement_preview(service, configured, as_of=as_of)
        if _fingerprint(refreshed["accounts"]) != _fingerprint(rows):  # type: ignore[arg-type]
            raise ValueError("sandbox state materially changed; confirmation must be renewed")
        state = {
            "version": 1,
            "confirmation_hash": digest,
            "operation_id": uuid4().hex,
            "new": {},
            "closed": {},
            "old": old,
            "bindings_updated": False,
        }
        _save(state_path, state)
    new = state["new"]
    if not isinstance(new, dict):
        raise ValueError("invalid replacement journal")
    for profile in ("long", "intraday"):
        item = new.setdefault(
            profile,
            {
                "name": f"moex-tinvest-bot-{profile}-{state['operation_id']}",
                "status": "not_started",
            },
        )
        if item["status"] == "not_started":
            previous_ids = {a.account_id for a in service.accounts()}
            item["status"] = "create_pending"
            _save(state_path, state)
            account_id = service.open_account(item["name"])
            if account_id in previous_ids or any(
                value.get("id") == account_id for value in new.values()
            ):
                raise ValueError("new sandbox account identity is not distinct")
            item.update(id=account_id, status="created")
            _save(state_path, state)
        elif item["status"] == "create_pending":
            matches = [a for a in service.accounts() if a.name == item["name"] and a.is_open]
            if len(matches) != 1 or matches[0].account_id in old.values():
                raise ValueError("ambiguous account creation; no blind retry allowed")
            item.update(id=matches[0].account_id, status="created")
            _save(state_path, state)
        balance = service.available_rub_balance(item["id"])
        if balance == Decimal("300000"):
            item["status"] = "funded"
        elif item["status"] == "funding_pending":
            raise ValueError("ambiguous funding; no blind pay-in retry allowed")
        elif item["status"] == "created" and balance == 0:
            item["status"] = "funding_pending"
            _save(state_path, state)
            service.pay_in(item["id"], Decimal("300000"))
            if service.available_rub_balance(item["id"]) != Decimal("300000"):
                raise ValueError("new sandbox funding is not verified")
            item["status"] = "funded"
        else:
            raise ValueError("unexpected balance on replacement account")
        _save(state_path, state)
    if len({new[p]["id"] for p in ("long", "intraday")}) != 2:
        raise ValueError("replacement identities must remain distinct")
    if not state["bindings_updated"]:
        upsert_env_values(
            env_path,
            {
                env_keys["long"]: new["long"]["id"],
                env_keys["intraday"]: new["intraday"]["id"],
                "T_INVEST_SANDBOX_ACCOUNT_ID": new["long"]["id"],
            },
        )
        state["bindings_updated"] = True
        _save(state_path, state)
    for profile, account_id in old.items():
        if state["closed"].get(profile) == "verified":
            continue
        present = any(a.account_id == account_id and a.is_open for a in service.accounts())
        if not present:
            state["closed"][profile] = "verified"
            _save(state_path, state)
            continue
        if state["closed"].get(profile) == "close_pending":
            raise ValueError("ambiguous account closure; no blind retry allowed")
        expected = next(row for row in rows if row["profile"] == profile)
        current = service.account_state(account_id)
        current_orders, current_stops = current["orders"], current["stop_orders"]
        if not isinstance(current_orders, dict) or not isinstance(current_stops, dict):
            raise ValueError("old account state is incomplete before closure")
        observed = {
            **expected,
            "positions": current["positions"],
            "active_orders": current_orders.get("orders", []),
            "active_stop_orders": current_stops.get("stopOrders", []),
        }
        if _fingerprint([observed]) != _fingerprint([expected]):
            raise ValueError("old account changed before closure; new confirmation required")
        state["closed"][profile] = "close_pending"
        _save(state_path, state)
        service.close_account(account_id)
        if any(a.account_id == account_id and a.is_open for a in service.accounts()):
            raise ValueError("old sandbox account closure is not verified")
        state["closed"][profile] = "verified"
        _save(state_path, state)
    return {
        "status": "EXECUTED_VERIFIED",
        "old_accounts_closed": 2,
        "new_accounts": {
            p: {"masked_id": "…" + new[p]["id"][-8:], "balance_rub": "300000"}
            for p in ("long", "intraday")
        },
        "real_money": False,
    }
