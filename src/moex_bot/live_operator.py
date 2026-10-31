"""Operator CLI boundary: disabled by default, reviewed admission plus exact order confirmation."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from .domain import Instrument, OrderIntent, OrderStatus, Side
from .integrations.tinvest_live import TInvestLiveBroker
from .integrations.tinvest_sandbox import UrlLibJsonTransport, quotation_to_decimal
from .live_execution import (
    LivePermission,
    enroll_live_journal,
    execute_live_intent,
    intent_digest,
    policy_digest,
    reconcile_live_journal,
    registered_live_journal,
)
from .prod_long import ProdLongPolicy, load_prod_long_policy
from .runtime_config import load_runtime_config
from .sandbox_execution import decode_validated_intent
from .service_config import TInvestEnvironment
from .shadow import load_universe


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("live operator input must be a JSON object")
    return value


def load_live_settings(path: Path) -> dict[str, Any]:
    raw = _object(path)
    if raw.get("version") != 1 or not isinstance(raw.get("enabled"), bool):
        raise ValueError("live config requires version=1 and boolean enabled")
    if raw.get("account_id_env") != "T_INVEST_PROD_ACCOUNT_ID":
        raise ValueError("live execution must use the production account binding")
    for field in ("journal", "admission"):
        if not isinstance(raw.get(field), str) or not (
            Path(raw[field]).is_absolute() or PurePosixPath(raw[field]).is_absolute()
        ):
            raise ValueError(f"live {field} must be an absolute path")
    if Path(raw["journal"]).suffix != ".sqlite3" or raw["journal"] == raw["admission"]:
        raise ValueError("live journal must be a distinct sqlite3 file")
    return raw


def release_digest(policy_path: Path, universe_path: Path, settings_path: Path) -> str:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    for label, path in (
        ("policy", policy_path),
        ("universe", universe_path),
        ("settings", settings_path),
    ):
        digest.update(label.encode() + b"\0" + path.read_bytes())
    for path in sorted(policy_path.parent.glob("*.json")):
        if path.name != "prod-admission.json":
            digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def validate_review_node(uid: int, mode: int, *, symlink: bool, directory: bool) -> None:
    if (
        uid != 0
        or mode & 0o022
        or symlink
        or (directory and not stat.S_ISDIR(mode))
        or (not directory and not stat.S_ISREG(mode))
    ):
        raise ValueError("live admission and parents must be root-owned, non-writable, non-symlink")


def assert_root_review_path(path: Path) -> None:
    if os.name != "posix" or not path.is_absolute() or ".." in path.parts:
        raise ValueError("live admission requires an absolute root-controlled POSIX path")
    for node in (path, *path.parents):
        value = node.lstat()
        validate_review_node(
            value.st_uid, value.st_mode, symlink=node.is_symlink(), directory=node != path
        )


def require_live_admission(
    path: Path,
    *,
    account: str,
    policy: ProdLongPolicy,
    release: str,
    now: datetime,
) -> None:
    assert_root_review_path(path)
    raw = _object(path)
    if (
        raw.get("version") != 1
        or raw.get("decision") != "APPROVED_CANARY"
        or raw.get("account_id") != account
        or raw.get("release_sha256") != release
        or raw.get("policy_sha256") != policy_digest(policy)
    ):
        raise ValueError("independent admission does not match account/release/policy")
    reviewed = datetime.fromisoformat(str(raw.get("reviewed_at", "")))
    expiry = datetime.fromisoformat(str(raw.get("expires_at", "")))
    if (
        reviewed.tzinfo is None
        or expiry.tzinfo is None
        or not reviewed <= now < expiry
        or expiry - reviewed > timedelta(days=7)
    ):
        raise ValueError("live admission is invalid or expired")
    checks = raw.get("checks")
    for key in (
        "current_strategy_promotion_passed",
        "broker_reconciliation_passed",
        "protective_orders_tested",
        "restore_tested",
        "independent_code_review_passed",
    ):
        if not isinstance(checks, dict) or checks.get(key) is not True:
            raise ValueError(f"independent live admission missing: {key}")
    # These are explicit reviewer attestations, not fabricated metrics or an automatic backtest.


def read_live_plan(path: Path, index: int, now: datetime, expected_account: str) -> OrderIntent:
    raw = _object(path)
    if not isinstance(raw.get("quality"), dict) or raw["quality"].get("passed") is not True:
        raise ValueError("production plan quality must pass")
    portfolio = raw.get("portfolio_input")
    if not isinstance(portfolio, dict) or portfolio.get("source") != "t_invest_prod_observer":
        raise ValueError("sandbox/file plans cannot be submitted to production")
    if not expected_account or portfolio.get("account_id") != expected_account:
        raise ValueError("production plan was calculated for another or unidentified account")
    provenance = raw.get("portfolio_input_sha256")
    if (
        not isinstance(provenance, str)
        or len(provenance) != 64
        or any(c not in "0123456789abcdef" for c in provenance)
    ):
        raise ValueError("production plan broker-snapshot provenance is missing")
    origin = Path(str(raw.get("portfolio_input_path", "")))
    if not origin.is_absolute():
        raise ValueError("production plan snapshot path must be absolute")
    original_bytes = origin.read_bytes()
    if hashlib.sha256(original_bytes).hexdigest() != provenance:
        raise ValueError("production plan does not match its original broker snapshot")
    original = json.loads(original_bytes)
    if (
        not isinstance(original, dict)
        or original.get("account_id") != expected_account
        or original.get("source") != "t_invest_prod_observer"
    ):
        raise ValueError("original production snapshot account/source mismatch")
    run_id = str(raw.get("run_id", ""))
    if not run_id.startswith("shadow-"):
        raise ValueError("initial live executor accepts long shadow plans only")
    generated = datetime.fromisoformat(run_id.removeprefix("shadow-"))
    if generated.tzinfo is None or not timedelta(0) <= now - generated <= timedelta(minutes=5):
        raise ValueError("production plan expired or future-dated")
    orders = raw.get("orders")
    if not isinstance(orders, list) or isinstance(index, bool) or not 0 <= index < len(orders):
        raise ValueError("production order index is invalid")
    return decode_validated_intent(orders[index])


def live_operator(
    action: str,
    *,
    settings_path: Path,
    policy_path: Path,
    universe_path: Path,
    runtime_path: Path,
    plan_path: Path | None,
    order_index: int,
    output_path: Path | None,
    confirmation_path: Path | None,
    confirm_digest: str | None,
) -> int:
    try:
        settings = load_live_settings(settings_path)
        if action in {"execute", "enroll"} and settings["enabled"] is not True:
            raise ValueError("PROD execution is disabled in live config")
        runtime = load_runtime_config(runtime_path)
        if runtime.environment is not TInvestEnvironment.PROD or runtime.sandbox_orders_enabled:
            raise ValueError("live command requires a dedicated production runtime")
        policy = load_prod_long_policy(policy_path)
        account = os.getenv("T_INVEST_PROD_ACCOUNT_ID", "").strip()
        token = os.getenv("T_INVEST_PROD_TOKEN", "").strip()
        if not account or not token:
            raise ValueError("production token and account binding are required")
        now = datetime.now(UTC)
        release = release_digest(policy_path, universe_path, settings_path)
        if action in {"execute", "enroll"}:
            if policy.execution_stage != "canary":
                raise ValueError("production policy remains observer-only")
            require_live_admission(
                Path(settings["admission"]),
                account=account,
                policy=policy,
                release=release,
                now=now,
            )
        intent = (
            read_live_plan(plan_path, order_index, now, account) if plan_path is not None else None
        )
        instruments = {}
        for item in load_universe(universe_path):
            tick = (
                intent.instrument.tick_size
                if intent and item.t_invest_uid == intent.instrument.uid
                else Decimal(".01")
            )
            instruments[item.t_invest_uid] = Instrument(
                item.secid,
                item.t_invest_uid,
                item.board,
                item.lot_size_verified,
                tick,
                issuer_id=item.issuer_id,
                sector=item.sector,
                risk_cluster=item.risk_cluster,
                asset_class=item.asset_class,
                short_enabled=item.short_enabled_verified,
            )
        broker = TInvestLiveBroker(token, account, UrlLibJsonTransport(), instruments)
        journal = Path(settings["journal"])
        if action == "probe":
            # Technical reference only: prospective cost of one liquid share lot, never a signal.
            probe = next((x for x in instruments.values() if x.secid == "SBER"), None)
            if probe is None:
                raise ValueError("connectivity probe requires SBER in the verified universe")
            metadata = broker._post(
                "InstrumentsService/GetInstrumentBy",
                {"idType": "INSTRUMENT_ID_TYPE_UID", "id": probe.uid},
            )
            tick = quotation_to_decimal(metadata["instrument"]["minPriceIncrement"])
            probe = replace(probe, tick_size=tick)
            broker.instruments_by_uid[probe.uid] = probe
            book = broker._post(
                "MarketDataService/GetOrderBook", {"instrumentId": probe.uid, "depth": 1}
            )
            asks = book.get("asks", [])
            quote = asks[0]["price"] if asks else book.get("lastPrice")
            if not isinstance(quote, dict):
                raise ValueError("no exchange price available for read-only order-cost probe")
            price = quotation_to_decimal(quote)
            probe_intent = OrderIntent(
                str(uuid4()),
                probe,
                Side.BUY,
                1,
                price,
                price * probe.lot_size,
                "read-only connectivity probe",
            )
            state = broker.capture(probe_intent)
            print(
                f"READ_ONLY_PROBE: PROD …{account[-4:]}; type={state.account_type}; "
                f"capture={(state.finished_at - state.started_at).total_seconds():.2f}s; "
                f"market_tradable={state.trading_allowed}; no order submitted"
            )
            return 0
        if action == "reconcile":
            unresolved = reconcile_live_journal(journal, broker)
            print(
                f"{'BLOCKED' if unresolved else 'PASS'}: production unresolved={unresolved}; "
                "read-only"
            )
            return 2 if unresolved else 0
        if intent is None or plan_path is None:
            raise ValueError("a production plan is required")
        if action == "preview":
            state = broker.capture(intent)
            if output_path is None:
                raise ValueError("preview output path required")
            protected = {
                p.resolve()
                for p in (
                    settings_path,
                    policy_path,
                    universe_path,
                    runtime_path,
                    journal,
                    Path(settings["admission"]),
                    plan_path,
                )
                if p is not None
            }
            if output_path.suffix != ".json" or output_path.resolve() in protected:
                raise ValueError("preview output must be a separate new JSON file")
            report = {
                "mode": "prod",
                "account_id": account,
                "intent": asdict(intent),
                "intent_sha256": intent_digest(intent),
                "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                "policy_sha256": policy_digest(policy),
                "release_sha256": release,
                "confirmed_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "journal_uuid": registered_live_journal(journal, account)
                if journal.exists()
                else None,
                "broker_state": asdict(state),
                "order_submitted": False,
            }
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("x", encoding="utf-8") as stream:
                if os.name != "nt":
                    output_path.chmod(0o600)
                json.dump(report, stream, default=str, indent=2)
                stream.write("\n")
            print(
                f"PREVIEW_ONLY: PROD …{account[-4:]} {intent.side.value.upper()} "
                f"{intent.instrument.secid} {intent.lots} lots "
                f"({intent.lots * intent.instrument.lot_size} units) "
                f"LIMIT {intent.limit_price} RUB; notional={intent.notional} RUB; "
                f"estimated total={state.estimated_total} RUB; no real order sent"
            )
            print(f"intent_sha256={intent_digest(intent)}")
            return 0
        if action == "enroll":
            state = broker.capture(intent)
            identifier = enroll_live_journal(
                journal, policy=policy, state=state, now=datetime.now(UTC)
            )
            print(f"PASS: explicitly enrolled production ledger {identifier}; no real order sent")
            return 0
        if action != "execute" or confirmation_path is None:
            raise ValueError("exact order confirmation file required")
        approved = _object(confirmation_path)
        if approved.get("plan_sha256") != hashlib.sha256(plan_path.read_bytes()).hexdigest():
            raise ValueError("approved production plan content has changed")
        if (
            confirm_digest != intent_digest(intent)
            or approved.get("intent_sha256") != confirm_digest
        ):
            raise ValueError("explicit order digest does not match current production plan")
        permission = LivePermission(
            str(approved["account_id"]),
            str(approved["intent_sha256"]),
            str(approved["policy_sha256"]),
            str(approved["release_sha256"]),
            datetime.fromisoformat(approved["confirmed_at"]),
            datetime.fromisoformat(approved["expires_at"]),
            True,
            str(approved.get("journal_uuid", "")),
        )

        def final_gate() -> None:
            current = load_live_settings(settings_path)
            if current["enabled"] is not True or current != settings:
                raise ValueError("production kill-switch/settings changed before submit")
            current_release = release_digest(policy_path, universe_path, settings_path)
            current_policy = load_prod_long_policy(policy_path)
            if current_release != release or policy_digest(current_policy) != policy_digest(policy):
                raise ValueError("production code/config changed before submit")
            moment = datetime.now(UTC)
            require_live_admission(
                Path(current["admission"]),
                account=account,
                policy=current_policy,
                release=current_release,
                now=moment,
            )
            if hashlib.sha256(plan_path.read_bytes()).hexdigest() != approved[
                "plan_sha256"
            ] or intent_digest(
                read_live_plan(plan_path, order_index, moment, account)
            ) != intent_digest(intent):
                raise ValueError("approved production plan changed before submit")

        record = execute_live_intent(
            intent=intent,
            policy=policy,
            broker=broker,
            permission=permission,
            release_sha256=release,
            journal_path=journal,
            before_submit=final_gate,
        )
        if record is None:
            print("SKIP: already completed; no duplicate order sent")
            return 0
        print(
            f"PROD: {record.status.value}; filled={record.filled_lots}/{intent.lots}; "
            "state persisted in production execution journal"
        )
        return 0 if record.status is OrderStatus.FILLED else 2
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f"BLOCKED_OR_FAILED: production execution: {exc}")
        return 2
