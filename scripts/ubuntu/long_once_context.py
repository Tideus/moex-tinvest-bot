"""Local interlocks for the systemd runner. No API calls or broker reads."""

import os
import stat
import sys
from pathlib import Path

from moex_bot.account_profiles import load_account_registry
from moex_bot.runtime_config import load_runtime_config


def gate(runtime, accounts, env):
    settings = load_runtime_config(runtime)
    if settings.environment.value != "sandbox" or not settings.sandbox_orders_enabled:
        raise ValueError("operational runtime must explicitly enable sandbox orders")
    registry = load_account_registry(accounts)
    registry.sandbox_bindings(env)
    long, day = registry.by_id("long"), registry.by_id("intraday")
    if (
        long.purpose.value != "long"
        or long.environment.value != "sandbox"
        or not long.order_execution_enabled
        or long.account_id_env != "T_INVEST_SANDBOX_LONG_ACCOUNT_ID"
        or day.account_id_env != "T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID"
        or [(s.strategy_id, s.config_path) for s in long.strategies if s.enabled]
        != [("daily_long_momentum_v1", "config/shadow.json")]
    ):
        raise ValueError("unexpected sandbox LONG profile or risk strategy")
    ids = [env.get(k, "").strip() for k in (long.account_id_env, day.account_id_env)]
    if (
        not all(ids)
        or ids[0].casefold() == env.get("T_INVEST_PROD_ACCOUNT_ID", "").strip().casefold()
        or not env.get("T_INVEST_SANDBOX_TOKEN", "").strip()
    ):
        raise ValueError("distinct explicit long and intraday sandbox accounts required")


def ownership(marker):
    for parent in marker.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("unsafe LONG handoff parent")
    info = marker.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or info.st_size > 40
        or marker.read_bytes() != b"sandbox-long-once/v1\n"
    ):
        raise ValueError("missing/invalid root-owned LONG handoff")


if __name__ == "__main__":
    try:
        ownership(Path("/etc/moex-tinvest-bot/sandbox-long-once.owner"))
        gate(Path(sys.argv[1]), Path(sys.argv[2]), os.environ)
    except (OSError, ValueError, KeyError) as exc:
        sys.exit(f"FAIL: sandbox long interlock: {exc}")
