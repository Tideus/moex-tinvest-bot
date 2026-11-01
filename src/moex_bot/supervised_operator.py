"""Explicit, paired-preview CLI for the first supervised production experiment.

No timer, automatic admission or strategy profitability assertion is created here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from .domain import Instrument, OrderIntent, Side
from .integrations.tinvest_sandbox import UrlLibJsonTransport, quotation_to_decimal
from .live_audit import AuditJsonTransport
from .live_execution import (
    LivePermission,
    enroll_live_journal,
    intent_digest,
    policy_digest,
    registered_live_journal,
)
from .live_operator import _object, assert_root_review_path, release_digest
from .live_protection import NativeProtectionBroker, ProtectionJournal, ProtectionPlan
from .prod_experiment import load_experiment_settings
from .prod_long import ProdLongPolicy, load_prod_long_policy, validate_prod_long_policy
from .prod_session import production_session
from .sandbox_execution import decode_validated_intent
from .shadow import load_universe
from .supervised_execution import SupervisedTInvestBroker, run_supervised_entry
from .supervised_notifications import notify_experiment
from .take_profit import (
    NativeExitBroker,
    active_stop,
    exit_intent,
    exit_permission,
    load_plan,
    precheck,
    preview_report,
    run_exit,
    write_preview,
)


def experiment_policy(path: Path, settings: dict[str, Any]) -> ProdLongPolicy:
    policy = load_prod_long_policy(path)
    capital = min(policy.canary.capital_limit_rub, Decimal(settings["capital_limit_rub"]))
    policy = replace(
        policy,
        execution_stage="canary",
        canary=replace(
            policy.canary,
            capital_limit_rub=capital,
            max_order_notional_rub=min(
                policy.canary.max_order_notional_rub, Decimal(settings["max_order_notional_rub"])
            ),
            max_daily_turnover_rub=min(
                policy.canary.max_daily_turnover_rub, Decimal(settings["max_daily_turnover_rub"])
            ),
            max_orders_per_day=min(
                policy.canary.max_orders_per_day, settings["max_attempts_per_day"]
            ),
            max_account_daily_loss_weight=min(
                policy.canary.max_account_daily_loss_weight,
                Decimal(settings["max_daily_loss_rub"]) / capital,
            ),
        ),
    )
    validate_prod_long_policy(policy)
    return policy


def require_experiment_review(
    path: Path,
    account: str,
    policy: ProdLongPolicy,
    release: str,
    now: datetime,
    min_remaining_seconds: int = 0,
) -> None:
    assert_root_review_path(path)
    raw = _object(path)
    if (
        raw.get("version") != 1
        or raw.get("decision") != "APPROVED_SUPERVISED_EXPERIMENT"
        or raw.get("scope") != "ONE_LOT_TECHNICAL_TEST"
        or raw.get("strategy_profitability") != "NOT_ESTABLISHED"
        or raw.get("account_id") != account
        or raw.get("release_sha256") != release
        or raw.get("policy_sha256") != policy_digest(policy)
    ):
        raise ValueError("supervised experiment review identity/scope does not match")
    reviewed = datetime.fromisoformat(str(raw.get("reviewed_at", "")))
    expiry = datetime.fromisoformat(str(raw.get("expires_at", "")))
    if (
        reviewed.tzinfo is None
        or expiry.tzinfo is None
        or now.tzinfo is None
        or not reviewed <= now < expiry
        or expiry - reviewed > timedelta(hours=1)
        or expiry - now < timedelta(seconds=min_remaining_seconds)
    ):
        raise ValueError("supervised experiment review expired/invalid")
    checks = raw.get("checks", {})
    for key in (
        "experimental_risk_acknowledged",
        "broker_readonly_preflight_passed",
        "protection_contract_tests_passed",
        "restore_drill_passed",
        "independent_code_review_passed",
        "supervised_session_active",
    ):
        if not isinstance(checks, dict) or checks.get(key) is not True:
            raise ValueError(f"supervised experiment evidence missing: {key}")


def decode_preview(
    raw: dict[str, Any], account: str, release: str, policy: ProdLongPolicy, now: datetime
) -> tuple[OrderIntent, ProtectionPlan]:
    created = datetime.fromisoformat(str(raw.get("created_at", "")))
    expires = datetime.fromisoformat(str(raw.get("expires_at", "")))
    if (
        raw.get("format") != 1
        or raw.get("account_id") != account
        or raw.get("release_sha256") != release
        or raw.get("policy_sha256") != policy_digest(policy)
        or raw.get("cancel_unfilled_after_seconds") != 30
        or created.tzinfo is None
        or expires.tzinfo is None
        or now.tzinfo is None
        or not created <= now < expires
        or expires - created > timedelta(minutes=5)
    ):
        raise ValueError("complete paired preview is changed, stale or invalid")
    intent = decode_validated_intent({"status": "validated", "intent": raw["intent"]})
    item = raw["protection"]
    protection = ProtectionPlan(
        account,
        str(item["entry_logical_id"]),
        str(item["entry_request_id"]),
        intent.instrument,
        item["lots"],
        Decimal(str(item["entry_price"])),
        Decimal(str(item["stop_trigger"])),
        None if item["stop_limit"] is None else Decimal(str(item["stop_limit"])),
        str(item["kind"]),
        None if item.get("take_profit") is None else Decimal(str(item["take_profit"])),
    )
    protection.validate()
    if (
        intent.side is not Side.BUY
        or intent.lots != 1
        or json.loads(json.dumps(asdict(protection), default=str)) != item
        or protection.entry_logical_id != intent.order_request_id
        or protection.entry_request_id
        != str(uuid5(NAMESPACE_URL, "prod:" + account + ":" + intent.order_request_id))
        or protection.entry_price != intent.limit_price
        or intent.instrument.secid not in policy.allowed_secids
        or intent.instrument.secid in policy.protected_secids
        or intent.notional > policy.canary.max_order_notional_rub
    ):
        raise ValueError("exact paired one-lot entry/protection identity does not match")
    return intent, protection


def require_planned_risk(
    intent: OrderIntent, protection: ProtectionPlan, settings: dict[str, Any]
) -> None:
    reference = protection.stop_limit or protection.stop_trigger
    planned = (intent.limit_price - reference) * intent.instrument.lot_size
    planned += intent.notional * Decimal(".005")  # Conservative transaction-cost allowance.
    if planned > Decimal(settings["max_daily_loss_rub"]):
        raise ValueError("planned stop distance plus costs exceeds the experiment loss budget")


def operate(args: argparse.Namespace) -> int:
    settings = load_experiment_settings(args.settings)
    policy = experiment_policy(args.policy, settings)
    release = release_digest(args.policy, args.universe, args.settings)
    account = os.getenv("T_INVEST_PROD_ACCOUNT_ID", "").strip()
    token = os.getenv("T_INVEST_PROD_TOKEN", "").strip()
    if not account or not token:
        raise ValueError("configured PROD token/account binding required")
    instruments = {
        x.t_invest_uid: Instrument(
            x.secid,
            x.t_invest_uid,
            x.board,
            x.lot_size_verified,
            Decimal(".01"),
            issuer_id=x.issuer_id,
            sector=x.sector,
            risk_cluster=x.risk_cluster,
            asset_class=x.asset_class,
            short_enabled=x.short_enabled_verified,
        )
        for x in load_universe(args.universe)
    }
    broker = SupervisedTInvestBroker(token, account, UrlLibJsonTransport(), instruments)
    journal = Path(settings["journal"])
    now = datetime.now(UTC)
    if args.action in {"exit-preview", "exit-execute"}:
        return operate_exit(args, settings, policy, release, account, token, broker, instruments)
    if args.action == "preview":
        instrument = next((x for x in instruments.values() if x.secid == args.ticker), None)
        if instrument is None or instrument.secid not in policy.allowed_secids:
            raise ValueError("preview instrument must belong to the production allowlist")
        metadata = broker._post(
            "InstrumentsService/GetInstrumentBy",
            {
                "idType": "INSTRUMENT_ID_TYPE_UID",
                "id": instrument.uid,
            },
        )
        instrument = replace(
            instrument, tick_size=quotation_to_decimal(metadata["instrument"]["minPriceIncrement"])
        )
        instruments[instrument.uid] = instrument
        price = Decimal(args.limit_price)
        intent = OrderIntent(
            str(uuid4()),
            instrument,
            Side.BUY,
            1,
            price,
            price * instrument.lot_size,
            "supervised technical experiment",
        )
        protection = ProtectionPlan(
            account,
            intent.order_request_id,
            str(uuid5(NAMESPACE_URL, "prod:" + account + ":" + intent.order_request_id)),
            instrument,
            1,
            price,
            Decimal(args.stop_trigger),
            Decimal(args.stop_limit) if args.stop_limit is not None else None,
            args.kind,
            Decimal(args.take_profit) if getattr(args, "take_profit", None) is not None else None,
        )
        protection.validate()
        require_planned_risk(intent, protection, settings)
        if (
            instrument.secid in policy.protected_secids
            or intent.notional > policy.canary.max_order_notional_rub
        ):
            raise ValueError("protected position or excessive preview notional")
        state = broker.capture(intent)
        if state.account_id != account or state.account_type != policy.account_type:
            raise ValueError("preview account is not the configured IIS")
        report = {
            "format": 1,
            "account_id": account,
            "intent": asdict(intent),
            "protection": asdict(protection),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "release_sha256": release,
            "policy_sha256": policy_digest(policy),
            "cancel_unfilled_after_seconds": 30,
            "journal_uuid": registered_live_journal(journal, account) if journal.exists() else None,
            "broker_state": asdict(state),
            "order_submitted": False,
        }
        if args.output is None or args.output.suffix != ".json":
            raise ValueError("new private JSON preview output required")
        if args.output.resolve() in {
            p.resolve()
            for p in (
                args.settings,
                args.policy,
                args.universe,
                journal,
                Path(settings["protection_journal"]),
                Path(settings["admission"]),
            )
        }:
            raise ValueError("preview cannot overwrite config, review or journals")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            if os.name != "nt":
                args.output.chmod(0o600)
            json.dump(report, stream, default=str, indent=2)
        print(
            f"PREVIEW_ONLY: IIS …{account[-4:]}; BUY {instrument.secid} 1 lot "
            f"({instrument.lot_size} shares), LIMIT {price}; amount {intent.notional} RUB; "
            f"then SELL protection {protection.kind}, trigger {protection.stop_trigger}, "
            f"limit {protection.stop_limit}; synthetic take-profit {protection.take_profit}; "
            "cancel unfilled new entry after 30s"
        )
        print("preview_sha256=" + hashlib.sha256(args.output.read_bytes()).hexdigest())
        return 0
    preview_bytes = args.preview.read_bytes() if args.preview is not None else b""
    if (
        args.preview is None
        or args.confirm_preview_sha256 != hashlib.sha256(preview_bytes).hexdigest()
    ):
        raise ValueError("exact complete preview SHA256 confirmation required")
    raw = json.loads(preview_bytes)
    if not isinstance(raw, dict):
        raise ValueError("complete paired preview must be a JSON object")
    intent, protection = decode_preview(raw, account, release, policy, now)
    require_planned_risk(intent, protection, settings)
    canonical = instruments.get(intent.instrument.uid)
    if canonical is None or replace(canonical, tick_size=intent.instrument.tick_size) != (
        intent.instrument
    ):
        raise ValueError("preview risk metadata differs from the trusted instrument universe")
    instruments[intent.instrument.uid] = intent.instrument
    if args.action == "enroll":
        identifier = enroll_live_journal(
            journal, policy=policy, state=broker.capture(intent), now=datetime.now(UTC)
        )
        print(f"PREPARED_LOCAL_ONLY: IIS …{account[-4:]}; ledger={identifier}; no order submitted")
        return 0
    if settings["enabled"] is not True or settings["stage"] != "supervised_canary":
        raise ValueError("supervised experiment is disabled; no order submitted")
    if raw["journal_uuid"] != registered_live_journal(journal, account):
        raise ValueError("paired preview belongs to another/missing production ledger")
    permission = LivePermission(
        account,
        intent_digest(intent),
        policy_digest(policy),
        release,
        datetime.fromisoformat(raw["created_at"]),
        datetime.fromisoformat(raw["expires_at"]),
        True,
        raw["journal_uuid"],
    )

    def gate() -> None:
        current = load_experiment_settings(args.settings)
        if current != settings or current["enabled"] is not True:
            raise ValueError("experiment kill-switch/settings changed")
        if release_digest(args.policy, args.universe, args.settings) != release:
            raise ValueError("experiment code/config changed")
        if hashlib.sha256(args.preview.read_bytes()).hexdigest() != args.confirm_preview_sha256:
            raise ValueError("complete paired preview changed")
        require_experiment_review(
            Path(settings["admission"]), account, policy, release, datetime.now(UTC)
        )

    gate()
    audit = AuditJsonTransport(
        UrlLibJsonTransport(), journal.parent / "audit" / (protection.entry_request_id + ".jsonl")
    )
    broker.transport = audit
    result = run_supervised_entry(
        intent=intent,
        policy=policy,
        permission=permission,
        release=release,
        journal_path=journal,
        protection_path=Path(settings["protection_journal"]),
        protection=protection,
        confirmed_protection_sha256=protection.digest,
        broker=broker,
        stop_broker=NativeProtectionBroker(token, account, audit),
        approval_gate=gate,
        entry_approval_gate=lambda: require_experiment_review(
            Path(settings["admission"]),
            account,
            policy,
            release,
            datetime.now(UTC),
            min_remaining_seconds=180,
        ),
    )
    print(json.dumps(result))
    notify_experiment(
        f"PROD · SUPERVISED TEST · IIS …{account[-4:]}\n"
        f"{intent.instrument.secid} · filled={result['filled_lots']} lot · "
        f"{result['state']}\nProtection: {result['protection']}",
        dedupe_key=protection.request_id + ":" + str(result["state"]),
        critical=False,
        now=datetime.now(UTC),
    )
    return 0 if result["state"] == "FILLED_PROTECTED" else 2


def operate_exit(
    args: argparse.Namespace, settings: dict[str, Any], policy: ProdLongPolicy,
    release: str, account: str, token: str, broker: SupervisedTInvestBroker,
    instruments: dict[str, Instrument],
) -> int:
    entry_path = Path(settings["journal"])
    protection_path = Path(settings["protection_journal"])
    plan = load_plan(protection_path, account, entry_path)
    canonical = instruments.get(plan.instrument.uid)
    if canonical is None or replace(canonical, tick_size=plan.instrument.tick_size) != (
        plan.instrument
    ):
        raise ValueError("stored protection instrument differs from trusted universe")
    instruments[plan.instrument.uid] = plan.instrument
    journal = ProtectionJournal(protection_path)
    transport = AuditJsonTransport(
        UrlLibJsonTransport(), entry_path.parent / "audit" / (plan.entry_request_id + ".jsonl")
    )
    broker.transport = transport
    stops = NativeExitBroker(token, account, transport)
    stop_id = active_stop(plan, journal, stops)
    identity = registered_live_journal(entry_path, account)
    if args.action == "exit-preview":
        precheck(plan, exit_intent(plan), policy, broker, entry_path, lambda: datetime.now(UTC))
        if args.output is None or args.output.resolve() in {
            p.resolve() for p in (entry_path, protection_path, args.settings, args.policy,
                                  args.universe, Path(settings["admission"]))
        }:
            raise ValueError("separate new exit preview output required")
        digest = write_preview(args.output, preview_report(
            plan, stop_id, release, policy, identity, datetime.now(UTC)
        ))
        print(
            f"PREVIEW_ONLY: IIS …{account[-4:]}; cancel known stop {stop_id}; "
            f"then SELL {plan.instrument.secid} 1 lot LIMIT {plan.take_profit} RUB. "
            "Protection gap: unfilled/failed sale requires operator intervention."
        )
        print("preview_sha256=" + digest)
        return 0
    content = args.preview.read_bytes() if args.preview is not None else b""
    if args.confirm_preview_sha256 != hashlib.sha256(content).hexdigest():
        raise ValueError("exact fresh exit preview confirmation required")
    permission = exit_permission(
        json.loads(content), plan, stop_id, release, policy, identity, datetime.now(UTC)
    )

    def gate() -> None:
        permission.validate(account, exit_intent(plan), policy, release, datetime.now(UTC))
        if (load_experiment_settings(args.settings) != settings or settings["enabled"] is not True
                or settings["stage"] != "supervised_canary"
                or release_digest(args.policy, args.universe, args.settings) != release
                or hashlib.sha256(args.preview.read_bytes()).hexdigest()
                != args.confirm_preview_sha256):
            raise ValueError("exit config/confirmation changed or disabled")
        require_experiment_review(
            Path(settings["admission"]), account, policy, release, datetime.now(UTC),
            min_remaining_seconds=120,
        )

    result = run_exit(
        plan=plan, journal=journal, entry_path=entry_path, policy=policy, broker=broker,
        stops=stops, permission=permission, release=release, gate=gate,
        clock=lambda: datetime.now(UTC),
    )
    print(json.dumps(result))
    notify_experiment(
        f"PROD · IIS …{account[-4:]} · {plan.instrument.secid} · CLOSED_TAKE_PROFIT; "
        "native stop cancellation verified", dedupe_key=plan.request_id + ":closed",
        critical=False, now=datetime.now(UTC),
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "enroll", "execute", "exit-preview",
                                          "exit-execute"))
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--confirm-preview-sha256")
    parser.add_argument("--ticker", default="SBER")
    parser.add_argument("--limit-price")
    parser.add_argument("--stop-trigger")
    parser.add_argument("--stop-limit")
    parser.add_argument("--take-profit", help="Synthetic target; no second broker sell is placed")
    parser.add_argument(
        "--kind", choices=("stop_loss_market", "stop_limit"), default="stop_loss_market"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "preview":
            return operate(args)
        with production_session():
            return operate(args)
    except (Exception, KeyboardInterrupt):
        # Never print raw broker/URL/credential-bearing exception text.
        print(
            "BLOCKED_OR_FAILED: supervised PROD experiment; inspect private journals; "
            "do not retry unknown orders"
        )
        if args.action in {"execute", "exit-execute"}:
            print(
                "CRITICAL_CHECK_ACCOUNT: entry/exit may be unverified or protection cancelled; "
                "check orders, stops and holdings at the broker immediately"
            )
            notify_experiment(
                "CRITICAL · PROD supervised test needs operator review. "
                "Entry/exit may be unverified or native protection cancelled. "
                "Check orders, stops and holdings; never retry blindly.",
                dedupe_key="supervised-failure:" + str(uuid4()),
                critical=True,
                now=datetime.now(UTC),
            )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
