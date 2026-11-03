"""Operator-controlled PROD one-shot boundary, disabled until reviewed local enrollment.

No command creates an admission, initializes a journal, arms a timer, or changes
an account/profile. The sandbox and supervised operator contracts stay unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .integrations.tinvest_sandbox import JsonPostTransport
from .live_audit import AuditJsonTransport
from .live_execution import registered_live_journal
from .live_operator import assert_root_review_path, release_digest
from .live_protection import NativeProtectionBroker
from .long_auto_signal import select_candidate
from .prod_long import load_prod_long_policy
from .prod_long_once import run_once
from .prod_session import production_session
from .service_config import PROD_REST
from .shadow import load_universe
from .supervised_execution import SupervisedTInvestBroker
from .supervised_notifications import notify_experiment

SETTINGS = Path("/etc/moex-tinvest-bot/prod-long-once.json")
CONFIG = Path("/opt/moex-tinvest-bot/config")
BASE = Path("/var/lib/moex-tinvest-bot/prod-long-once")
FIXED: dict[str, object] = {
    "version": 1,
    "mode": "prod_long_once",
    "account_id_env": "T_INVEST_PROD_ACCOUNT_ID",
    "entry_lots": 1,
    "max_total_with_fees_rub": "10000",
    "max_attempts_total": 1,
    "protect_manual_positions": True,
    "native_stop_required": True,
    "journal": "/var/lib/moex-tinvest-bot/execution/prod-orders.sqlite3",
    "protection_journal": "/var/lib/moex-tinvest-bot/execution/prod-protection.sqlite3",
    "claim_directory": "/var/lib/moex-tinvest-control/prod-long-once",
    "authorization": "/etc/moex-tinvest-bot/prod-long-once-admission.json",
}
EVIDENCE = ("tests", "independent_audit", "restore_drill", "sandbox_fill_and_native_stop")


def load_settings(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_bytes())
    if (
        not isinstance(raw, dict)
        or set(raw) != set(FIXED) | {"enabled"}
        or type(raw["enabled"]) is not bool
    ):
        raise ValueError("invalid one-shot settings schema")
    if any(type(raw[k]) is not type(v) or raw[k] != v for k, v in FIXED.items()):
        raise ValueError("one-shot hard limits or canonical paths changed")
    return raw


def validate_grant(
    raw: dict[str, Any],
    *,
    account: str,
    release: str,
    journal_uuid: str,
    now: datetime,
) -> None:
    expected = {
        "version": 1,
        "decision": "OPERATOR_AUTHORIZED_PROD_LONG_ONCE",
        "account_sha256": hashlib.sha256(account.encode()).hexdigest(),
        "release_sha256": release,
        "journal_uuid": journal_uuid,
        "entry_lots": 1,
        "max_attempts_total": 1,
        "max_total_with_fees_rub": "10000",
        "native_stop_required": True,
        "protect_manual_positions": True,
    }
    if any(type(raw.get(k)) is not type(v) or raw.get(k) != v for k, v in expected.items()):
        raise ValueError("operator grant identity/limits mismatch")
    reviewed, expiry = (
        datetime.fromisoformat(str(raw.get(k, ""))) for k in ("reviewed_at", "expires_at")
    )
    if (
        any(t.tzinfo is None for t in (reviewed, expiry, now))
        or not reviewed <= now < expiry
        or expiry - reviewed > timedelta(hours=24)
    ):
        raise ValueError("grant expired, not yet valid or insufficient time for protection")
    evidence = raw.get("evidence_sha256")
    if not isinstance(evidence, dict) or set(evidence) != set(EVIDENCE):
        raise ValueError("complete operator-reviewed evidence is required")
    for key in EVIDENCE:
        digest = evidence[key]
        if (
            not isinstance(digest, str)
            or re.fullmatch("[0-9a-f]{64}", digest) is None
            or len(set(digest)) == 1
        ):
            raise ValueError("real evidence digests required, not placeholders")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise HTTPError(req.full_url, code, "redirect refused", headers, fp)


class ProductionTransport:
    """Separate fixed PROD transport; never reuse a sandbox host or follow redirects."""

    def post(self, url: str, *, headers: Any, payload: Any, timeout_seconds: float) -> Any:
        prefix = PROD_REST + "/tinkoff.public.invest.api.contract.v1."
        allowed = {
            "UsersService/GetAccounts",
            "InstrumentsService/GetInstrumentBy",
            "OperationsService/GetPortfolio",
            "OperationsService/GetPositions",
            "OperationsService/GetWithdrawLimits",
            "OperationsService/GetOperationsByCursor",
            "OrdersService/GetOrders",
            "OrdersService/GetOrderPrice",
            "OrdersService/GetOrderState",
            "OrdersService/PostOrder",
            "OrdersService/CancelOrder",
            "StopOrdersService/GetStopOrders",
            "StopOrdersService/PostStopOrder",
            "MarketDataService/GetOrderBook",
            "MarketDataService/GetTradingStatus",
        }
        if not url.startswith(prefix) or url.removeprefix(prefix) not in allowed:
            raise ValueError("unsupported production method/host")
        request = Request(
            url, data=json.dumps(payload).encode(), headers=dict(headers), method="POST"
        )
        with build_opener(NoRedirect()).open(request, timeout=timeout_seconds) as response:
            result = json.loads(response.read())
        if not isinstance(result, dict):
            raise ValueError("invalid production response")
        return result


def operate(args: argparse.Namespace) -> dict[str, str | int]:
    from .cli import _verified_instruments

    settings = load_settings(args.settings)
    if args.action == "config-check":
        return {"state": "CONFIG_VALID", "enabled": str(settings["enabled"])}
    if settings["enabled"] is not True:
        return {"state": "DISABLED"}  # Before reading credentials, plan, claim or broker.
    if args.settings != SETTINGS:
        raise ValueError("execution requires the canonical operator-owned settings")
    assert_root_review_path(SETTINGS)
    account = os.environ["T_INVEST_PROD_ACCOUNT_ID"].strip()
    sandbox_accounts = {
        os.getenv(k, "").strip()
        for k in (
            "T_INVEST_SANDBOX_ACCOUNT_ID",
            "T_INVEST_SANDBOX_LONG_ACCOUNT_ID",
            "T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID",
        )
    }
    if not account or account in sandbox_accounts:
        raise ValueError("explicit distinct IIS account required")
    journal = Path(settings["journal"])
    identity = registered_live_journal(journal, account)
    release = release_digest(CONFIG / "prod_long.json", CONFIG / "universe.json", SETTINGS)
    grant_path = Path(settings["authorization"])
    assert_root_review_path(grant_path)
    grant_bytes = grant_path.read_bytes()
    grant = json.loads(grant_bytes)

    def authority() -> None:
        assert_root_review_path(SETTINGS)
        assert_root_review_path(grant_path)
        if (
            load_settings(SETTINGS) != settings
            or grant_path.read_bytes() != grant_bytes
            or release_digest(CONFIG / "prod_long.json", CONFIG / "universe.json", SETTINGS)
            != release
        ):
            raise ValueError("operator authority/code/config changed")
        validate_grant(
            grant, account=account, release=release, journal_uuid=identity, now=datetime.now(UTC)
        )

    authority()
    if datetime.fromisoformat(grant["expires_at"]) - datetime.now(UTC) < timedelta(minutes=5):
        raise ValueError("at least five minutes of operator authority must remain")
    claim_dir = Path(settings["claim_directory"])
    # Explicit setup only: missing directories or symlinks never get repaired by runner.
    if (
        not claim_dir.is_dir()
        or claim_dir.resolve() != claim_dir
        or claim_dir.stat().st_mode & 0o077
    ):
        raise ValueError("independently enrolled private claim directory missing/unsafe")
    if (claim_dir / "claim.json").exists() or (claim_dir / "claim.json").is_symlink():
        return {"state": "ALREADY_CLAIMED", "protection": "NOT_RECHECKED"}
    if args.action == "status":
        return {"state": "ARMED_WAITING", "broker_checked": "false"}
    if (
        args.plan is None
        or args.portfolio is None
        or args.plan.resolve() != args.plan
        or args.plan.parent.parent != BASE
        or not re.fullmatch(r"run-[0-9]{8}T[0-9]{6}Z\.[A-Za-z0-9]{8}", args.plan.parent.name)
        or args.plan.name != "plan.json"
        or args.portfolio != args.plan.with_name("sleeve.json")
    ):
        raise ValueError("only a fresh same-cycle IIS strategy plan is accepted")
    candidate = select_candidate(
        plan=args.plan,
        portfolio=args.portfolio,
        universe=CONFIG / "universe.json",
        policy=CONFIG / "prod_long.json",
        strategy_config=CONFIG / "prod_shadow.json",
        account_id=account,
        now=datetime.now(UTC),
        environment="prod-preview",
    )
    if candidate is None:
        return {"state": "WAITING_NO_SIGNAL"}
    # Stage change is confined to this capability. Never change global runtime or intraday.
    policy = replace(load_prod_long_policy(CONFIG / "prod_long.json"), execution_stage="canary")
    token = os.environ["T_INVEST_PROD_TOKEN"]
    instruments = _verified_instruments(load_universe(CONFIG / "universe.json"))
    if candidate.instrument.uid not in instruments:
        raise ValueError("candidate outside deployed universe")
    instruments[candidate.instrument.uid] = candidate.instrument
    raw_transport: JsonPostTransport = ProductionTransport()
    transport = AuditJsonTransport(
        raw_transport, journal.parent / "audit" / (candidate.order_request_id + ".jsonl")
    )
    broker = SupervisedTInvestBroker(token, account, transport, instruments)
    return run_once(
        candidate=candidate,
        policy=policy,
        release=release,
        journal=journal,
        protection_journal=Path(settings["protection_journal"]),
        claim_directory=claim_dir,
        broker=broker,
        stops=NativeProtectionBroker(token, account, transport),
        authority=authority,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["config-check", "status", "run"])
    parser.add_argument("--settings", type=Path, default=SETTINGS)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--portfolio", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "run":
            with production_session():
                result = operate(args)
        else:
            result = operate(args)
        print(json.dumps(result, ensure_ascii=False))
        if args.action == "run" and result["state"] not in {
            "DISABLED",
            "WAITING_NO_SIGNAL",
            "ALREADY_CLAIMED",
        }:
            notify_experiment(
                "PROD · LONG · одноразовый вход\n"
                + str(result["state"])
                + "\nЗащита: "
                + str(result.get("protection", "не подтверждена")),
                dedupe_key="long-once:" + str(result["state"]),
                critical=False,
                now=datetime.now(UTC),
            )
        if args.action == "status" and result["state"] != "ARMED_WAITING":
            return 3
        return 0
    except Exception:
        print("BLOCKED: PROD LONG; inspect private evidence, no automatic retry")
        if args.action == "run":
            notify_experiment(
                "PROD LONG: цикл остановлен. Проверьте журнал, родительскую заявку и стоп; "
                "не повторяйте вход и не очищайте журнал.",
                dedupe_key="long-once-failure:" + datetime.now(UTC).isoformat(),
                critical=True,
                now=datetime.now(UTC),
            )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
