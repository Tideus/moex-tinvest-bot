from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from .observer_funding import number, verified_funding
from .protected_observer import ProtectedExternalInstrument

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SECID = re.compile(r"^[A-Z0-9]{1,16}$")


@dataclass(frozen=True, slots=True)
class ProdLongCanary:
    capital_limit_rub: Decimal
    max_order_notional_rub: Decimal
    max_daily_turnover_rub: Decimal
    max_orders_per_day: int
    max_account_daily_loss_weight: Decimal
    allow_margin: bool
    allowed_asset_classes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProdLongPolicy:
    version: int
    execution_stage: str
    account_id_env: str
    account_type: str
    manual_position_policy: str
    protected_secids: frozenset[str]
    managed_secids: frozenset[str]
    allowed_secids: frozenset[str]
    canary: ProdLongCanary
    protected_external_instruments: tuple[ProtectedExternalInstrument, ...] = ()


def load_prod_long_policy(path: Path) -> ProdLongPolicy:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("prod long policy must be an object")
    canary_raw = raw.get("canary")
    if not isinstance(canary_raw, dict):
        raise ValueError("prod long policy requires a canary object")
    policy = ProdLongPolicy(
        version=_integer(raw.get("version"), "version"),
        execution_stage=str(raw.get("execution_stage", "")),
        account_id_env=str(raw.get("account_id_env", "")),
        account_type=str(raw.get("account_type", "")),
        manual_position_policy=str(raw.get("manual_position_policy", "")),
        protected_secids=_secids(raw.get("protected_secids"), "protected_secids"),
        managed_secids=_secids(raw.get("managed_secids"), "managed_secids"),
        allowed_secids=_secids(raw.get("allowed_secids"), "allowed_secids"),
        protected_external_instruments=_external(raw.get("protected_external_instruments", [])),
        canary=ProdLongCanary(
            capital_limit_rub=_decimal(
                canary_raw.get("capital_limit_rub"), "capital_limit_rub"
            ),
            max_order_notional_rub=_decimal(
                canary_raw.get("max_order_notional_rub"), "max_order_notional_rub"
            ),
            max_daily_turnover_rub=_decimal(
                canary_raw.get("max_daily_turnover_rub"), "max_daily_turnover_rub"
            ),
            max_orders_per_day=_integer(
                canary_raw.get("max_orders_per_day"), "max_orders_per_day"
            ),
            max_account_daily_loss_weight=_decimal(
                canary_raw.get("max_account_daily_loss_weight"),
                "max_account_daily_loss_weight",
            ),
            allow_margin=_boolean(canary_raw.get("allow_margin"), "allow_margin"),
            allowed_asset_classes=_strings(
                canary_raw.get("allowed_asset_classes"), "allowed_asset_classes"
            ),
        ),
    )
    validate_prod_long_policy(policy)
    return policy


def prepare_prod_long_sleeve(
    *,
    policy: ProdLongPolicy,
    broker_snapshot: Mapping[str, object],
    expected_account_id: str,
) -> dict[str, object]:
    if policy.execution_stage != "observer":
        raise ValueError("this build permits prod long observer stage only")
    if broker_snapshot.get("source") not in {"t_invest_prod", "t_invest_prod_observer_raw"}:
        raise ValueError("prod long observer requires a production broker snapshot")
    external = _external_snapshot(policy, broker_snapshot)
    account_id = str(broker_snapshot.get("account_id", "")).strip()
    if not account_id or account_id != expected_account_id.strip():
        raise ValueError("production snapshot account does not match configured IIS")
    raw_positions = broker_snapshot.get("positions")
    if not isinstance(raw_positions, Mapping):
        raise ValueError("production snapshot positions must be an object")
    held = {
        str(secid)
        for secid, value in raw_positions.items()
        if _position_lots(value) != 0
    }
    classified = policy.protected_secids | policy.managed_secids
    unknown = held - classified
    if unknown:
        raise ValueError(
            f"production positions require explicit protected/managed classification: "
            f"{sorted(unknown)}"
        )
    cash = _decimal(broker_snapshot.get("cash"), "snapshot cash")
    funding = verified_funding(
        broker_snapshot.get("observer_funding"), account_id, datetime.now(UTC),
    )
    available = max(Decimal(0), min(cash, number(funding["withdraw_conservative_rub"])))
    sleeve_cash = min(available, policy.canary.capital_limit_rub)
    funding.update({"positions_money_rub": str(cash),
                    "positions_blocked_rub": str(broker_snapshot.get("blocked_cash", "0")),
                    "available_cash_rub": str(available),
                    "capital_limit_rub": str(policy.canary.capital_limit_rub)})
    managed_positions = {
        str(secid): value
        for secid, value in raw_positions.items()
        if str(secid) in policy.managed_secids and _position_lots(value) != 0
    }
    result: dict[str, object] = {
        "source": "t_invest_prod_observer",
        "account_id": account_id,
        "cash": str(sleeve_cash),
        "blocked_cash": str(broker_snapshot.get("blocked_cash", "0")),
        "reported_equity": str(sleeve_cash),
        "positions": managed_positions,
        "observer_funding": funding,
        "daily_turnover": str(broker_snapshot.get("daily_turnover", "0")),
        "open_orders": _coerce_int(
            broker_snapshot.get("open_orders", 0), "snapshot open_orders"
        ),
        "observer": {
            "execution_stage": policy.execution_stage,
            "real_orders_enabled": False,
            "account_type": policy.account_type,
            "capital_limit_rub": str(policy.canary.capital_limit_rub),
            "protected_secids": sorted(policy.protected_secids & held),
            "managed_secids": sorted(policy.managed_secids & held),
            "allowed_secids": sorted(policy.allowed_secids),
        },
    }
    if external:
        result["source"] = "t_invest_prod_observer_external"
        result["protected_external"] = external
        result["account_reported_equity"] = broker_snapshot.get("reported_equity")
        result["execution_allowed"] = False
    return result


def write_prod_long_sleeve(
    *,
    policy_path: Path,
    broker_snapshot_path: Path,
    output_path: Path,
    expected_account_id: str,
) -> dict[str, object]:
    raw = json.loads(broker_snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("production broker snapshot must be an object")
    result = prepare_prod_long_sleeve(
        policy=load_prod_long_policy(policy_path),
        broker_snapshot=raw,
        expected_account_id=expected_account_id,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def validate_prod_long_policy(policy: ProdLongPolicy) -> None:
    external = policy.protected_external_instruments
    if len({item.uid for item in external}) != len(external) or len(
        {item.secid for item in external}
    ) != len(external):
        raise ValueError("protected external instruments must be unique")
    for item in external:
        if item.secid not in policy.protected_secids or item.instrument_type != "futures":
            raise ValueError("external futures must be explicitly protected, never tradable")
    if policy.version != 1:
        raise ValueError("prod long policy version must be 1")
    if policy.execution_stage not in {"observer", "canary"}:
        raise ValueError("production execution is not enabled in this release")
    if not _ENV_NAME.fullmatch(policy.account_id_env):
        raise ValueError("account_id_env must be a valid environment variable")
    if policy.account_id_env != "T_INVEST_PROD_ACCOUNT_ID":
        raise ValueError("prod long observer must use T_INVEST_PROD_ACCOUNT_ID")
    if policy.account_type != "ACCOUNT_TYPE_TINKOFF_IIS":
        raise ValueError("prod long observer is restricted to the reviewed IIS account")
    if policy.manual_position_policy != "protect":
        raise ValueError("manual positions must remain protected")
    if policy.protected_secids & policy.managed_secids:
        raise ValueError("protected and managed SECIDs must be disjoint")
    if policy.protected_secids & policy.allowed_secids:
        raise ValueError("protected and allowed SECIDs must be disjoint")
    if not policy.managed_secids <= policy.allowed_secids:
        raise ValueError("managed SECIDs must belong to the production allowlist")
    if len(policy.allowed_secids) != 20:
        raise ValueError("production allowlist must contain exactly 20 SECIDs")
    canary = policy.canary
    if policy.execution_stage == "canary" and (
        canary.capital_limit_rub > Decimal("40000")
        or canary.max_order_notional_rub > Decimal("10000")
        or canary.max_daily_turnover_rub > Decimal("20000")
        or canary.max_orders_per_day > 2
    ):
        raise ValueError("initial live canary hard limits are 40000/10000/20000 RUB and 2 attempts")
    for label, value in (
        ("capital_limit_rub", canary.capital_limit_rub),
        ("max_order_notional_rub", canary.max_order_notional_rub),
        ("max_daily_turnover_rub", canary.max_daily_turnover_rub),
    ):
        if not value.is_finite() or value <= 0:
            raise ValueError(f"{label} must be positive and finite")
    if canary.max_order_notional_rub > canary.capital_limit_rub:
        raise ValueError("max order cannot exceed canary capital")
    if canary.max_order_notional_rub > canary.max_daily_turnover_rub:
        raise ValueError("max order cannot exceed daily turnover")
    if not 1 <= canary.max_orders_per_day <= 4:
        raise ValueError("production canary permits from one to four orders per day")
    if not Decimal("0") < canary.max_account_daily_loss_weight <= Decimal("0.02"):
        raise ValueError("account daily loss gate must be in (0, 0.02]")
    if canary.allow_margin:
        raise ValueError("margin is forbidden for the initial production canary")
    if canary.allowed_asset_classes != ("share",):
        raise ValueError("initial production canary supports shares only")


def _secids(value: object, label: str) -> frozenset[str]:
    values = _strings(value, label)
    if len(set(values)) != len(values) or any(not _SECID.fullmatch(item) for item in values):
        raise ValueError(f"{label} must contain unique uppercase SECIDs")
    return frozenset(values)


def _external(value: object) -> tuple[ProtectedExternalInstrument, ...]:
    if not isinstance(value, list):
        raise ValueError("protected_external_instruments must be a list")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"uid", "secid", "instrument_type"}:
            raise ValueError("invalid protected external instrument descriptor")
        uid = str(item["uid"])
        if str(UUID(uid)) != uid or not _SECID.fullmatch(str(item["secid"])):
            raise ValueError("invalid protected external identity")
        result.append(ProtectedExternalInstrument(uid, str(item["secid"]),
                                                  str(item["instrument_type"])))
    return tuple(result)


def _external_snapshot(policy: ProdLongPolicy, snapshot: Mapping[str, object]) -> dict[str, object]:
    if snapshot.get("source") != "t_invest_prod_observer_raw":
        if (snapshot.get("protected_external_positions")
                or snapshot.get("protected_external_operations")):
            raise ValueError("external holdings require observer-only snapshot")
        return {}
    if snapshot.get("execution_allowed") is not False:
        raise ValueError("external holdings cannot be execution input")
    allowed = {(item.uid, item.secid, item.instrument_type)
               for item in policy.protected_external_instruments}
    result: dict[str, object] = {}
    for key in ("protected_external_positions", "protected_external_operations"):
        rows = snapshot.get(key)
        if not isinstance(rows, list):
            raise ValueError("protected external snapshot records missing")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("unclassified external instrument in observer snapshot")
            if ((
                row.get("instrument_uid"), row.get("secid"), row.get("instrument_type")
            ) not in allowed or row.get("protected") is not True
                    or row.get("execution_allowed") is not False):
                raise ValueError("unclassified external instrument in observer snapshot")
        result[key] = rows
    return result


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a string array")
    return tuple(value)


def _decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{label} must be decimal") from exc
    return result


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a JSON boolean")
    return value


def _position_lots(value: object) -> int:
    if isinstance(value, Mapping):
        return _coerce_int(value.get("lots", 0), "position lots")
    return _coerce_int(value, "position lots")


def _coerce_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    return parsed
