import hashlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = PROJECT_ROOT / "deploy" / "ubuntu"


def _unit(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def test_shadow_systemd_unit_has_security_and_absolute_paths() -> None:
    unit = _unit("moex-tinvest-shadow.service")
    assert "User=moexbot" in unit
    assert "EnvironmentFile=/etc/moex-tinvest-bot/bot.env" in unit
    assert "ExecStartPre=" in unit
    assert "--require moex_algopack --require telegram" in unit
    assert (
        "ExecStart=/usr/bin/bash /opt/moex-tinvest-bot/scripts/ubuntu/run-shadow-cycle.sh"
        in unit
    )
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=/var/lib/moex-tinvest-bot /var/log/moex-tinvest-bot" in unit


def test_hourly_timer_uses_explicit_moscow_timezone() -> None:
    timer = _unit("moex-tinvest-shadow.timer")
    assert "OnCalendar=*-*-* *:05:00 Europe/Moscow" in timer
    assert "Persistent=true" in timer
    assert "Unit=moex-tinvest-shadow.service" in timer


def test_health_timer_and_service_contract_match() -> None:
    timer = _unit("moex-tinvest-health.timer")
    service = _unit("moex-tinvest-health.service")
    assert "Unit=moex-tinvest-health.service" in timer
    assert "OnUnitActiveSec=15min" in timer
    assert "ExecStart=/usr/bin/bash /opt/moex-tinvest-bot/scripts/ubuntu/healthcheck.sh" in service


def test_env_template_contains_names_only() -> None:
    lines = [
        line for line in _unit("bot.env.example").splitlines()
        if line and not line.startswith("#")
    ]
    assert lines
    assert all(line.endswith("=") for line in lines)
    assert any(line.startswith("T_INVEST_PROD_TOKEN=") for line in lines)


def test_all_deployment_shell_scripts_use_strict_mode() -> None:
    scripts = sorted((PROJECT_ROOT / "scripts" / "ubuntu").glob("*.sh"))
    assert {item.name for item in scripts} == {
        "activate.sh", "backup.sh", "healthcheck.sh", "install-ca-certificates.sh",
        "install.sh", "moex-botctl.sh", "run-shadow-cycle.sh", "test-deployment.sh",
        "uninstall.sh", "update.sh",
    }
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash\n")
        assert "set -Eeuo pipefail" in text


def test_certificate_manifest_pins_all_deployment_certificates() -> None:
    directory = DEPLOY / "certificates"
    manifest: dict[str, str] = {}
    for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split()
        manifest[name] = digest
    assert set(manifest) == {
        "russian_trusted_root_ca.crt",
        "russian_trusted_root_ca_gost_2025.crt",
        "russian_trusted_sub_ca.crt",
        "russian_trusted_sub_ca_2024.crt",
        "russian_trusted_sub_ca_gost_2025.crt",
    }
    for name, expected_digest in manifest.items():
        payload = (directory / name).read_bytes()
        assert payload.startswith(b"-----BEGIN CERTIFICATE-----\n")
        assert payload.endswith(b"-----END CERTIFICATE-----\n")
        assert hashlib.sha256(payload).hexdigest() == expected_digest


def test_install_and_update_both_refresh_system_ca_store() -> None:
    for name in ("install.sh", "update.sh"):
        text = (PROJECT_ROOT / "scripts" / "ubuntu" / name).read_text(encoding="utf-8")
        assert "install-ca-certificates.sh" in text


def test_install_and_update_restore_script_execute_permissions() -> None:
    for name in ("install.sh", "update.sh"):
        text = (PROJECT_ROOT / "scripts" / "ubuntu" / name).read_text(encoding="utf-8")
        assert "-exec chmod 0755 {} +" in text


def test_install_and_update_publish_single_command_control_tool() -> None:
    for name in ("install.sh", "update.sh"):
        text = (PROJECT_ROOT / "scripts" / "ubuntu" / name).read_text(encoding="utf-8")
        assert "/usr/local/sbin/moex-botctl" in text


def test_install_and_update_materialize_full_runtime_config() -> None:
    for name in ("install.sh", "update.sh"):
        text = (PROJECT_ROOT / "scripts" / "ubuntu" / name).read_text(encoding="utf-8")
        assert "runtime-normalize" in text


def test_control_tool_exposes_safe_operator_workflow() -> None:
    text = (PROJECT_ROOT / "scripts" / "ubuntu" / "moex-botctl.sh").read_text(
        encoding="utf-8"
    )
    for command in ("prelaunch", "start", "diagnose", "status", "contour"):
        assert f"{command})" in text
    assert "timers не включены" in text
