import hashlib
import subprocess
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


def test_daily_report_timer_and_service_send_at_moscow_eod() -> None:
    timer = _unit("moex-tinvest-daily-report.timer")
    service = _unit("moex-tinvest-daily-report.service")
    assert "OnCalendar=*-*-* 23:20:00 Europe/Moscow" in timer
    assert "Persistent=true" in timer
    assert "run-daily-report.sh" in service
    assert "EnvironmentFile=/etc/moex-tinvest-bot/bot.env" in service


def test_intraday_timer_and_service_are_isolated_and_five_minute() -> None:
    timer = _unit("moex-tinvest-intraday.timer")
    service = _unit("moex-tinvest-intraday.service")
    assert "10..18:00/5:00 Europe/Moscow" in timer
    assert "Persistent=false" in timer
    assert "Unit=moex-tinvest-intraday.service" in timer
    assert "run-intraday-cycle.sh" in service
    assert "User=moexbot" in service
    assert "ReadWritePaths=/var/lib/moex-tinvest-bot /var/log/moex-tinvest-bot" in service


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
        "run-daily-report.sh", "run-intraday-cycle.sh", "uninstall.sh", "update.sh",
    }
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash\n")
        assert "set -Eeuo pipefail" in text


def test_git_index_preserves_shell_script_execute_bits() -> None:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "scripts/ubuntu/*.sh"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    rows = [line for line in result.stdout.splitlines() if line]
    assert rows
    assert all(line.startswith("100755 ") for line in rows)


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


def test_update_reasserts_runtime_owners_and_modes() -> None:
    text = (PROJECT_ROOT / "scripts" / "ubuntu" / "update.sh").read_text(
        encoding="utf-8"
    )
    assert 'chown -R root:root "${APP_DIR}"' in text
    assert 'chmod 0640 "${CONFIG_DIR}/bot.env"' in text
    assert 'install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"' in text
    assert '"${STATE_DIR}" "${LOG_DIR}" "${BACKUP_DIR}"' in text


def test_prelaunch_checks_runtime_owners_and_modes() -> None:
    text = (PROJECT_ROOT / "scripts" / "ubuntu" / "moex-botctl.sh").read_text(
        encoding="utf-8"
    )
    assert "check_permissions" in text
    assert 'require_permissions "640:root:${SERVICE_USER}" "${ENV_FILE}"' in text
    assert 'require_permissions "755:root:root" "${script}"' in text


def test_install_and_update_publish_single_command_control_tool() -> None:
    for name in ("install.sh", "update.sh"):
        text = (PROJECT_ROOT / "scripts" / "ubuntu" / name).read_text(encoding="utf-8")
        assert "/usr/local/sbin/moex-botctl" in text


def test_install_and_update_materialize_full_runtime_config() -> None:
    for name in ("install.sh", "update.sh"):
        text = (PROJECT_ROOT / "scripts" / "ubuntu" / name).read_text(encoding="utf-8")
        assert "runtime-normalize" in text


def test_update_tolerates_a_new_timer_not_installed_yet() -> None:
    text = (PROJECT_ROOT / "scripts" / "ubuntu" / "update.sh").read_text(
        encoding="utf-8"
    )
    assert "stop_timer_if_installed" in text
    assert 'systemctl cat "${unit}"' in text
    assert (
        "systemctl stop moex-tinvest-shadow.timer moex-tinvest-health.timer"
        not in text
    )


def test_control_tool_exposes_safe_operator_workflow() -> None:
    text = (PROJECT_ROOT / "scripts" / "ubuntu" / "moex-botctl.sh").read_text(
        encoding="utf-8"
    )
    for command in (
        "prelaunch",
        "start",
        "stop",
        "diagnose",
        "status",
        "portfolio",
        "decisions",
        "contour",
    ):
        assert f"{command})" in text
    assert "timers не включены" in text


def test_control_stop_disables_all_timers_and_stops_current_cycles() -> None:
    text = (PROJECT_ROOT / "scripts" / "ubuntu" / "moex-botctl.sh").read_text(
        encoding="utf-8"
    )
    assert "systemctl disable --now" in text
    assert "moex-tinvest-shadow.timer" in text
    assert "moex-tinvest-health.timer" in text
    assert "moex-tinvest-daily-report.timer" in text
    assert "moex-tinvest-intraday.timer" in text
    assert "moex-tinvest-shadow.service" in text
    assert "moex-tinvest-health.service" in text
    assert "moex-tinvest-daily-report.service" in text
    assert "moex-tinvest-intraday.service" in text
    assert "timers_are_stopped" in text
    assert "данные и конфиги сохранены" in text


def test_shadow_runner_uses_selected_broker_snapshot_not_empty_example() -> None:
    text = (PROJECT_ROOT / "scripts" / "ubuntu" / "run-shadow-cycle.sh").read_text(
        encoding="utf-8"
    )
    assert "broker-portfolio-snapshot" in text
    assert '--runtime "/etc/moex-tinvest-bot/runtime.json"' in text
    assert '--portfolio "${portfolio_path}"' in text
    assert "portfolio_empty.json" not in text


def test_runners_apply_compact_telegram_policy_without_losing_artifacts() -> None:
    shadow = (PROJECT_ROOT / "scripts" / "ubuntu" / "run-shadow-cycle.sh").read_text(
        encoding="utf-8"
    )
    intraday = (
        PROJECT_ROOT / "scripts" / "ubuntu" / "run-intraday-cycle.sh"
    ).read_text(encoding="utf-8")
    daily = (PROJECT_ROOT / "scripts" / "ubuntu" / "run-daily-report.sh").read_text(
        encoding="utf-8"
    )
    assert '--notifications "${APP_DIR}/config/notifications.json"' in shadow
    assert '--output "${flow_path}" || true' in shadow
    assert 'intraday-trade-notifications \\' in intraday
    assert 'intraday-performance-report \\' in daily
    assert 'intraday-daily-performance-${report_date}.txt' in daily
