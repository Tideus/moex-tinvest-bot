import hashlib
import subprocess
import tomllib
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


def test_trading_services_fail_fast_on_rejected_algopack_token() -> None:
    for unit in ("moex-tinvest-shadow.service",):
        text = _unit(unit)
        assert (
            "ExecStartPre=/opt/moex-tinvest-bot/.venv/bin/python "
            "-m moex_bot.cli algopack-health" in text
        )
    assert "algopack-health" not in _unit("moex-tinvest-intraday.service")


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


def test_prod_long_observer_is_read_only_and_separately_scheduled() -> None:
    timer = _unit("moex-tinvest-prod-long-observer.timer")
    service = _unit("moex-tinvest-prod-long-observer.service")
    runner = (PROJECT_ROOT / "scripts" / "ubuntu" / "run-prod-long-observer.sh").read_text(
        encoding="utf-8"
    )
    assert "Mon..Fri *-*-* 10:10:00 Europe/Moscow" in timer
    assert "Persistent=true" in timer
    assert "--require tinvest_prod" in service
    assert "prod_runtime.json" in service
    assert "broker-portfolio-snapshot" in runner
    assert "prod-long-prepare" in runner
    assert "--quality-block-is-safe" in runner
    assert '--report-environment "PROD OBSERVER"' in runner
    assert '--allowed-secids-policy "${APP_DIR}/config/prod_long.json"' in runner
    assert "sandbox-execute" not in runner
    assert "intraday-sandbox-execute" not in runner


def test_storage_maintenance_has_daily_root_timer_and_bounded_paths() -> None:
    timer = _unit("moex-tinvest-maintenance.timer")
    service = _unit("moex-tinvest-maintenance.service")
    assert "OnCalendar=*-*-* 03:40:00 Europe/Moscow" in timer
    assert "Persistent=true" in timer
    assert "User=root" in service
    assert "storage-maintenance" in service
    assert "ReadWritePaths=/var/lib/moex-tinvest-bot" in service
    assert "/var/log/moex-tinvest-bot /var/backups/moex-tinvest-bot" in service


def test_network_services_and_runners_use_system_ca_bundle() -> None:
    for unit_name in (
        "moex-tinvest-shadow.service",
        "moex-tinvest-intraday.service",
        "moex-tinvest-health.service",
        "moex-tinvest-daily-report.service",
    ):
        unit = _unit(unit_name)
        assert "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" in unit
        assert "REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt" in unit
    for script_name in (
        "run-shadow-cycle.sh",
        "run-intraday-cycle.sh",
        "run-daily-report.sh",
        "healthcheck.sh",
        "moex-botctl.sh",
    ):
        script = (PROJECT_ROOT / "scripts" / "ubuntu" / script_name).read_text(
            encoding="utf-8"
        )
        expected = (
            'export SSL_CERT_FILE="${SSL_CERT_FILE:-'
            '/etc/ssl/certs/ca-certificates.crt}"'
        )
        assert expected in script


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
        "write-build-metadata.sh",
        "reset-history.sh",
        "disaster-backup.sh",
        "restore-new-vm.sh",
        "install-kuma-local.sh",
        "run-prod-long-observer.sh",
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


def test_update_backs_up_and_only_fast_forwards_clean_checkout() -> None:
    text = (PROJECT_ROOT / "scripts" / "ubuntu" / "update.sh").read_text(
        encoding="utf-8"
    )
    assert '"${APP_DIR}/scripts/ubuntu/backup.sh"' in text
    assert "diff --quiet" in text
    assert "diff --cached --quiet" in text
    assert "pull --ff-only" in text


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
        "prod-observe",
        "decisions",
        "contour",
        "collect-report",
    ):
        assert f"{command})" in text
    assert "production orders remain technically unavailable" in text
    assert "timers не включены" in text
    assert "bot_env_included=no" in text
    assert "sha256sum" in text
    assert "T_INVEST_SANDBOX_LONG_ACCOUNT_ID" in text
    assert "T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID" in text


def test_control_stop_disables_all_timers_and_stops_current_cycles() -> None:
    text = (PROJECT_ROOT / "scripts" / "ubuntu" / "moex-botctl.sh").read_text(
        encoding="utf-8"
    )
    assert "systemctl disable --now" in text
    assert "moex-tinvest-shadow.timer" in text
    assert "moex-tinvest-health.timer" in text
    assert "moex-tinvest-daily-report.timer" in text
    assert "moex-tinvest-intraday.timer" in text
    assert "moex-tinvest-maintenance.timer" in text
    assert "moex-tinvest-shadow.service" in text
    assert "moex-tinvest-health.service" in text
    assert "moex-tinvest-daily-report.service" in text
    assert "moex-tinvest-intraday.service" in text
    assert "timers_are_stopped" in text
    assert "данные и конфиги сохранены" in text


def test_health_checks_authenticated_algopack_and_future_timer_elapses() -> None:
    health = (PROJECT_ROOT / "scripts" / "ubuntu" / "healthcheck.sh").read_text(
        encoding="utf-8"
    )
    control = (PROJECT_ROOT / "scripts" / "ubuntu" / "moex-botctl.sh").read_text(
        encoding="utf-8"
    )
    assert "algopack-health" in health
    assert "ALGOPACK authenticated read-only probe" in health
    assert "health-alert" in health
    assert "telegram-send" in health
    assert "timer_has_next_elapse" in control
    assert "NextElapseUSecRealtime" in control
    assert "NextElapseUSecMonotonic" in control
    assert "ALGOPACK принимает токен" in control


def test_intraday_notification_timeout_does_not_abort_trading_cycle() -> None:
    runner = (PROJECT_ROOT / "scripts" / "ubuntu" / "run-intraday-cycle.sh").read_text(
        encoding="utf-8"
    )
    assert "notification_status=0" in runner
    assert "the next cycle will retry" in runner


def test_shadow_runner_uses_selected_broker_snapshot_not_empty_example() -> None:
    text = (PROJECT_ROOT / "scripts" / "ubuntu" / "run-shadow-cycle.sh").read_text(
        encoding="utf-8"
    )
    assert "broker-portfolio-snapshot" in text
    assert '--runtime "/etc/moex-tinvest-bot/runtime.json"' in text
    assert '--portfolio "${portfolio_path}"' in text
    assert "portfolio_empty.json" not in text
    assert "sandbox-reconcile" in text
    assert "--profile long" in text
    assert "T_INVEST_SANDBOX_LONG_ACCOUNT_ID" in text
    assert '--outbox "${outbox_path}"' in text


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
    assert "algopack-flow" not in shadow
    assert "--secid SBER --futures-ticker SBERF" not in shadow
    assert 'intraday-trade-notifications \\' in intraday
    assert 'intraday-performance-report \\' in daily
    assert 'intraday-daily-performance-${report_date}.txt' in daily


def test_install_update_and_report_preserve_verifiable_build_metadata() -> None:
    install = (PROJECT_ROOT / "scripts" / "ubuntu" / "install.sh").read_text(
        encoding="utf-8"
    )
    update = (PROJECT_ROOT / "scripts" / "ubuntu" / "update.sh").read_text(
        encoding="utf-8"
    )
    control = (PROJECT_ROOT / "scripts" / "ubuntu" / "moex-botctl.sh").read_text(
        encoding="utf-8"
    )
    metadata = (PROJECT_ROOT / "scripts" / "ubuntu" / "write-build-metadata.sh").read_text(
        encoding="utf-8"
    )
    assert "write-build-metadata.sh" in install
    assert "write-build-metadata.sh" in update
    assert "BUILD_MANIFEST.sha256" in install and "BUILD_MANIFEST.sha256" in update
    assert "rev-parse HEAD" in metadata
    assert "git_revision=" in metadata
    assert "deployed_at=" in metadata
    assert '[[ -r "${APP_DIR}/VERSION" ]]' in control
    assert "version_status" in control
    assert "ls-remote --exit-code origin" in control


def test_package_and_runtime_versions_are_identical() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    from moex_bot import __version__

    assert project["project"]["version"] == __version__


def test_history_reset_is_confirmed_backed_up_and_broker_safe() -> None:
    reset = (PROJECT_ROOT / "scripts" / "ubuntu" / "reset-history.sh").read_text(
        encoding="utf-8"
    )
    control = (PROJECT_ROOT / "scripts" / "ubuntu" / "moex-botctl.sh").read_text(
        encoding="utf-8"
    )
    cli = (PROJECT_ROOT / "src" / "moex_bot" / "cli.py").read_text(encoding="utf-8")
    assert "--confirm-reset-history" in reset
    assert "history-reset-safety" in reset
    assert "history reset is allowed only in the sandbox contour" in cli
    assert "intraday account is not flat" in cli
    assert "active broker orders" in cli
    assert "moex-history-reset-" in reset
    assert "sha256sum -c" in reset
    assert "reset-history)" in control


def test_disaster_recovery_is_portable_verified_and_keeps_secrets_separate() -> None:
    backup = (PROJECT_ROOT / "scripts" / "ubuntu" / "disaster-backup.sh").read_text(
        encoding="utf-8"
    )
    restore = (PROJECT_ROOT / "scripts" / "ubuntu" / "restore-new-vm.sh").read_text(
        encoding="utf-8"
    )
    control = (PROJECT_ROOT / "scripts" / "ubuntu" / "moex-botctl.sh").read_text(
        encoding="utf-8"
    )
    assert "secrets_included=no" in backup
    assert "runtime.json" in backup and "maintenance.json" in backup
    assert '"${LOG_DIR}/." "${stage_dir}/logs/"' in backup
    assert '"${stage_dir}/logs/" "${LOG_DIR}/"' in restore
    assert "sha256sum -c" in backup and "sha256sum -c" in restore
    assert "unsafe archive path" in restore
    assert "--env-file" in restore
    assert "moex-botctl prelaunch" in restore
    assert "disaster-backup)" in control
