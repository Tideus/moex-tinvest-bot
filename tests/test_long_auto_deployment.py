"""Offline unit contracts plus an actual shell harness with a non-network CLI double."""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts/ubuntu"
DEPLOY = ROOT / "deploy/ubuntu"
UNIT = "moex-tinvest-long-once-sandbox"
spec = importlib.util.spec_from_file_location("context_gate", SCRIPTS / "long_once_context.py")
ctx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctx)


@pytest.mark.parametrize("fault", ["none", "prod", "disabled", "aliases", "missing", "profile"])
def test_scope_guard_uses_actual_account_profile(tmp_path, fault):
    env = {
        "T_INVEST_SANDBOX_LONG_ACCOUNT_ID": "dedicated",
        "T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID": "other",
        "T_INVEST_SANDBOX_TOKEN": "fixture",
    }
    runtime = json.loads((ROOT / "config/runtime.json").read_bytes())
    registry = json.loads((ROOT / "config/accounts.json").read_bytes())
    if fault == "prod":
        runtime["t_invest_environment"] = "prod"
    elif fault == "disabled":
        runtime["sandbox_orders_enabled"] = False
    elif fault == "aliases":
        env["T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID"] = "DEDICATED"
    elif fault == "missing":
        del env["T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID"]
    elif fault == "profile":
        registry["profiles"][0]["strategies"][0]["enabled"] = False
    rcfg, acfg = tmp_path / "runtime", tmp_path / "accounts"
    rcfg.write_text(json.dumps(runtime))
    acfg.write_text(json.dumps(registry))
    if fault == "none":
        ctx.gate(rcfg, acfg, env)
    else:
        with pytest.raises(ValueError):
            ctx.gate(rcfg, acfg, env)


def test_disabled_by_default_and_excludes_legacy_and_deployment():
    service, timer = [(DEPLOY / (UNIT + ext)).read_text() for ext in (".service", ".timer")]
    assert "Persistent=false" in timer and "10:05:00 Europe/Moscow" in timer
    assert "ConditionPathExists=/etc/moex-tinvest-bot/sandbox-long-once.owner" in service
    assert "[Install]" not in service and "Restart=" not in service
    assert "UnsetEnvironment=T_INVEST_PROD_TOKEN" in service
    assert (
        "ReadWritePaths=/var/lib/moex-tinvest-bot/long-once "
        "/var/lib/moex-tinvest-control/long-once-sandbox" in service
    )
    install, update, legacy, control = [
        (SCRIPTS / f).read_text(encoding="utf-8")
        for f in ("install.sh", "update.sh", "run-shadow-cycle.sh", "moex-botctl.sh")
    ]
    assert "ensure_long_anchor_dir" in install and "ensure_long_anchor_dir" in update
    assert "systemctl enable" not in install
    assert f"systemctl enable {UNIT}" not in control and f"start {UNIT}.service" not in update
    assert 'if [[ "${WAS_LONG_ONCE_ACTIVE}" == 1 ]]' in update
    assert (
        legacy.index("lock_long_writer")
        < legacy.index("sandbox-long-once.owner")
        < legacy.index("sandbox-reconcile")
    )
    assert "entry.claim" in legacy and "lock_long_writer" in update
    assert (DEPLOY / "sandbox-long-once.owner.example").read_bytes() == b"sandbox-long-once/v1\n"


@pytest.mark.parametrize(
    "fail",
    [
        "",
        "status",
        "consumed",
        "session-check",
        "geo-refresh",
        "hourly-shadow",
        "broker-portfolio-snapshot",
    ],
)
def test_shell_computes_same_run_plan_or_aborts(tmp_path, fail):
    bash = (
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Git/bin/bash.exe"
        if os.name == "nt"
        else Path(shutil.which("bash") or "/not-installed")
    )
    if not bash.is_file():
        pytest.skip("bash not installed")
    # All fixed paths redirected only in this temporary test COPY, not via a production override.
    app, base = (tmp_path / n for n in ("app", "local-state"))
    for d in (app / ".venv/bin", app / "scripts/ubuntu", base):
        d.mkdir(parents=True)
    (app / "scripts/ubuntu/lock-long-writer.sh").write_text("lock_long_writer() { :; }\n")
    (base / "sandbox.sqlite3").write_bytes(b"fixture")
    cli = app / ".venv/bin/python"
    cli.write_bytes(b"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> calls
if [[ "${FAIL_AT:-}" == consumed && " $* " == *" --waiting-only "* ]]; then exit 3; fi
if [[ -n "${FAIL_AT:-}" && " $* " == *" ${FAIL_AT} "* ]]; then exit 2; fi
while [[ $# -gt 1 ]]; do
  if [[ "$1" == "--output" ]]; then printf '{}\\n' > "$2"; break; fi
  shift
done
""")
    cli.chmod(0o755)
    runner = tmp_path / "run.sh"
    runner.write_bytes(
        (SCRIPTS / "run-long-once-sandbox.sh")
        .read_bytes()
        .replace(b"app=/opt/moex-tinvest-bot", b'app="$PWD/app"')
        .replace(b"base=/var/lib/moex-tinvest-bot/long-once", b'base="$PWD/local-state"')
    )
    result = subprocess.run(
        [str(bash), "run.sh"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=25,
        env={**os.environ, "FAIL_AT": fail},
    )
    calls = (app / "calls").read_text() if (app / "calls").exists() else ""
    if not calls:
        if os.name == "nt" and "NtCreateDirectoryObject" in result.stderr:
            pytest.skip("Git Bash kernel IPC not available in restricted Windows environment")
        pytest.fail(f"isolated bash fixture did not run: {result.stderr}")
    assert "sandbox-execute" not in calls and "sandbox-reconcile" not in calls
    if fail == "consumed":
        assert (
            result.returncode == 0
            and "portfolio-snapshot" not in calls
            and "geo-refresh" not in calls
        )
        assert "sandbox-cycle" not in calls
    elif fail:
        assert result.returncode != 0 and "sandbox-cycle" not in calls
    else:
        assert result.returncode == 0, result.stderr
        assert (
            calls.index("geo-refresh")
            < calls.index("broker-portfolio-snapshot")
            < calls.index("hourly-shadow")
            < calls.index("sandbox-cycle")
        )
        (run,) = base.glob("run-*")
        for name in ("portfolio.json", "plan.json", "geo.json"):
            assert (run / name).is_file() and (run.name + "/" + name) in calls
        assert "--account-id-env T_INVEST_SANDBOX_LONG_ACCOUNT_ID" in calls
        assert "--strategy-config " in calls and "init-sandbox" not in calls
