from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path, PurePosixPath

from .service_config import TInvestEnvironment

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class AccountPurpose(StrEnum):
    LONG = "long"
    INTRADAY = "intraday"


@dataclass(frozen=True, slots=True)
class StrategyAssignment:
    strategy_id: str
    config_path: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class AccountProfile:
    profile_id: str
    purpose: AccountPurpose
    environment: TInvestEnvironment
    account_id_env: str
    target_balance_rub: Decimal
    order_execution_enabled: bool
    strategies: tuple[StrategyAssignment, ...]


@dataclass(frozen=True, slots=True)
class AccountRegistry:
    version: int
    base_currency: str
    profiles: tuple[AccountProfile, ...]

    def by_id(self, profile_id: str) -> AccountProfile:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        raise KeyError(profile_id)


def load_account_registry(path: Path) -> AccountRegistry:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), list):
        raise ValueError("account registry must contain a profiles array")
    profiles = tuple(_profile(item) for item in raw["profiles"])
    registry = AccountRegistry(
        version=_integer(raw.get("version"), "version"),
        base_currency=str(raw.get("base_currency", "")),
        profiles=profiles,
    )
    _validate(registry)
    return registry


def _profile(value: object) -> AccountProfile:
    if not isinstance(value, dict) or not isinstance(value.get("strategies"), list):
        raise ValueError("each account profile must contain a strategies array")
    return AccountProfile(
        profile_id=str(value.get("profile_id", "")).strip(),
        purpose=AccountPurpose(str(value.get("purpose", ""))),
        environment=TInvestEnvironment(str(value.get("environment", ""))),
        account_id_env=str(value.get("account_id_env", "")).strip(),
        target_balance_rub=Decimal(str(value.get("target_balance_rub", "0"))),
        order_execution_enabled=_boolean(
            value.get("order_execution_enabled"), "order_execution_enabled"
        ),
        strategies=tuple(_strategy(item) for item in value["strategies"]),
    )


def _strategy(value: object) -> StrategyAssignment:
    if not isinstance(value, dict):
        raise ValueError("strategy assignment must be an object")
    return StrategyAssignment(
        strategy_id=str(value.get("strategy_id", "")).strip(),
        config_path=str(value.get("config_path", "")).strip(),
        enabled=_boolean(value.get("enabled"), "strategy.enabled"),
    )


def _validate(registry: AccountRegistry) -> None:
    if registry.version != 1:
        raise ValueError("account registry version must be 1")
    if registry.base_currency != "RUB":
        raise ValueError("account registry base_currency must be RUB")
    if len(registry.profiles) != 2:
        raise ValueError("exactly two account profiles are required")
    ids = [item.profile_id for item in registry.profiles]
    if len(set(ids)) != len(ids) or any(not item for item in ids):
        raise ValueError("account profile ids must be non-empty and unique")
    if {item.purpose for item in registry.profiles} != {
        AccountPurpose.LONG,
        AccountPurpose.INTRADAY,
    }:
        raise ValueError("one long and one intraday account profile are required")
    env_names = [item.account_id_env for item in registry.profiles]
    if len(set(env_names)) != len(env_names) or any(
        not _ENV_NAME.fullmatch(item) for item in env_names
    ):
        raise ValueError("account_id_env values must be valid and unique env names")
    for profile in registry.profiles:
        if profile.environment is not TInvestEnvironment.SANDBOX:
            raise ValueError("multi-account rollout is sandbox-only")
        if not profile.target_balance_rub.is_finite() or profile.target_balance_rub <= 0:
            raise ValueError("target_balance_rub must be positive and finite")
        if not profile.strategies or not any(item.enabled for item in profile.strategies):
            raise ValueError("each account profile requires an enabled strategy")
        strategy_ids = [item.strategy_id for item in profile.strategies]
        if len(set(strategy_ids)) != len(strategy_ids) or any(
            not item for item in strategy_ids
        ):
            raise ValueError("strategy ids must be non-empty and unique per profile")
        for strategy in profile.strategies:
            candidate = PurePosixPath(strategy.config_path)
            if (
                candidate.is_absolute()
                or ".." in candidate.parts
                or not strategy.config_path.startswith("config/")
            ):
                raise ValueError("strategy config_path must stay inside config/")


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a JSON boolean")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value

