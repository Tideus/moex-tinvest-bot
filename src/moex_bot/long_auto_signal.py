"""Select one unowned cash equity from an actual long harness decision, never from a quote."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .config import load_config
from .domain import Instrument, Side
from .execution import stable_order_request_id
from .prod_long import load_prod_long_policy
from .scheduler import MOSCOW, is_conservative_stock_window
from .shadow import load_universe


@dataclass(frozen=True, slots=True)
class Candidate:
    account_id: str
    decision_sha256: str
    instrument: Instrument
    limit_price: Decimal
    stop_trigger: Decimal
    order_request_id: str
    stop_request_id: str
    valid_until: datetime
    lots: int = 1


def select_candidate(
    *,
    plan: Path,
    portfolio: Path,
    universe: Path,
    policy: Path,
    strategy_config: Path,
    account_id: str,
    now: datetime,
    environment: str,
) -> Candidate | None:
    sources = {"sandbox": "t_invest_sandbox", "prod-preview": "t_invest_prod_observer"}
    if environment not in sources or not account_id.strip() or now.tzinfo is None:
        raise ValueError("explicit environment, account and aware clock required")
    raw, snapshot = plan.read_bytes(), portfolio.read_bytes()
    p, s = json.loads(raw), json.loads(snapshot)
    digest = hashlib.sha256(raw).hexdigest()
    config = load_config(strategy_config)
    if (
        p["portfolio_input_sha256"] != hashlib.sha256(snapshot).hexdigest()
        or any(
            x["source"] != sources[environment] or x["account_id"] != account_id
            for x in (s, p["portfolio_input"])
        )
        or p["config_snapshot"] != json.loads(json.dumps(asdict(config), default=str))
        or config.mode.value != "shadow"
        or config.allow_margin
        or not config.strategy.flow_confirmation_enabled
    ):
        raise ValueError("long plan provenance, flow policy or account mismatch")
    run = p["run_id"]
    if not run.startswith("shadow-"):
        raise ValueError("expected genuine long shadow plan")
    decision = datetime.fromisoformat(run.removeprefix("shadow-"))
    expiry = decision + timedelta(minutes=5)
    if decision.tzinfo is None or not decision <= now < expiry:
        raise ValueError("stale or future long decision")
    if (
        not is_conservative_stock_window(now)
        or now.astimezone(MOSCOW).hour not in config.strategy.rebalance_hours_moscow
        or decision.astimezone(MOSCOW).hour not in config.strategy.rebalance_hours_moscow
        or p["quality"]["passed"] is not True
        or p["quality"]["errors"]
        or p["rebalance_allowed"] is not True
        or Decimal(p["geo"]["multiplier"]) <= 0
        or s["open_orders"] != 0
    ):
        return None
    rules = load_prod_long_policy(policy)
    entries = {i.secid: i for i in load_universe(universe)}
    eligible = {x["secid"] for x in p["signal_diagnostics"] if x["eligible"] is True}
    flow = {x["secid"] for x in p["flow_diagnostics"] if x["passed"] is True}
    targets = {x["secid"] for x in p["targets"] if Decimal(x["weight"]) > 0}
    for record in p["orders"]:
        o = record["intent"]
        label = o["instrument"]["secid"]
        if (
            record["status"] != "validated"
            or o["side"] != "buy"
            or o["order_type"] != "limit"
            or o["confirm_margin_trade"] is not False
            or label not in eligible & flow & targets
            or label not in rules.allowed_secids
            or label in rules.protected_secids
            or label in s["positions"]
        ):
            continue
        u = entries[label]
        i = Instrument(**(o["instrument"] | {"tick_size": Decimal(o["instrument"]["tick_size"])}))
        price = Decimal(o["limit_price"])
        n = o["lots"]
        one = price * i.lot_size
        if (
            i.uid != u.t_invest_uid
            or i.board != u.board
            or i.board != "TQBR"
            or i.lot_size != u.lot_size_verified
            or i.asset_class != u.asset_class
            or i.currency != "RUB"
            or type(n) is not int
            or n < 1
            or o["order_request_id"] != stable_order_request_id(run, label, Side.BUY, n)
            or not price.is_finite()
            or price <= 0
            or price % i.tick_size
            or Decimal(o["notional"]) != n * one
        ):
            raise ValueError("risk-gated intent identity, price or size mismatch")
        if one > min(Decimal(10000), rules.canary.max_order_notional_rub, Decimal(s["cash"])):
            continue
        stop = (price * Decimal(".98") / i.tick_size).to_integral_value(
            rounding=ROUND_FLOOR
        ) * i.tick_size
        if not 0 < stop < price:
            continue
        key = f"long-once/v1/{environment}/{account_id}/{digest}/{o['order_request_id']}"
        return Candidate(
            account_id,
            digest,
            i,
            price,
            stop,
            str(uuid5(NAMESPACE_URL, key)),
            str(uuid5(NAMESPACE_URL, key + "/stop")),
            expiry,
        )
    return None
