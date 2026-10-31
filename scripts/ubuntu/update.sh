#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${MOEX_BOT_APP_DIR:-/opt/moex-tinvest-bot}"
CONFIG_DIR="${MOEX_BOT_CONFIG_DIR:-/etc/moex-tinvest-bot}"
STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/moex-tinvest-bot}"
LOG_DIR="${MOEX_BOT_LOG_DIR:-/var/log/moex-tinvest-bot}"
BACKUP_DIR="${MOEX_BOT_BACKUP_DIR:-/var/backups/moex-tinvest-bot}"
SERVICE_USER="${MOEX_BOT_USER:-moexbot}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  printf 'Run as root (sudo).\n' >&2
  exit 2
fi
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  printf 'Existing installation is incomplete: %s\n' "${APP_DIR}" >&2
  exit 2
fi

# Stable root-owned inode shared with the whole BUY -> native-stop session.
# Acquire BEFORE backup/pull/rsync; never truncate or replace an existing lock.
CONTROL_DIR=/var/lib/moex-tinvest-control
CONTROL_LOCK="${CONTROL_DIR}/prod-session.lock"
for parent in / /var /var/lib; do
  mode="$(stat -c %a "${parent}")"
  if [[ -L "${parent}" || ! -d "${parent}" || "$(stat -c %u "${parent}")" != 0 ]] \
    || (( (8#${mode} & 0022) != 0 )); then
    printf 'FAIL: untrusted production control parent.\n' >&2; exit 2
  fi
done
if [[ -L "${CONTROL_DIR}" || ( -e "${CONTROL_DIR}" && \
  ( ! -d "${CONTROL_DIR}" || "$(stat -c %u "${CONTROL_DIR}")" != 0 ) ) ]]; then
  printf 'FAIL: untrusted production control directory.\n' >&2; exit 2
fi
if [[ -d "${CONTROL_DIR}" ]]; then
  mode="$(stat -c %a "${CONTROL_DIR}")"
  if (( (8#${mode} & 0022) != 0 )); then
    printf 'FAIL: writable production control directory.\n' >&2; exit 2
  fi
fi
install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONTROL_DIR}"
if [[ -L "${CONTROL_LOCK}" || ( -e "${CONTROL_LOCK}" && \
  ( ! -f "${CONTROL_LOCK}" || "$(stat -c %u "${CONTROL_LOCK}")" != 0 || \
    "$(stat -c %h "${CONTROL_LOCK}")" != 1 ) ) ]]; then
  printf 'FAIL: untrusted production control lock.\n' >&2; exit 2
fi
if [[ ! -e "${CONTROL_LOCK}" ]]; then
  (umask 007; set -o noclobber; : >"${CONTROL_LOCK}")
fi
chown root:"${SERVICE_USER}" "${CONTROL_LOCK}"
chmod 0660 "${CONTROL_LOCK}"
exec 9<>"${CONTROL_LOCK}"
if ! flock -n 9; then
  printf 'FAIL: production session is active; deployment made no app changes.\n' >&2
  exit 2
fi

if [[ -x "${APP_DIR}/scripts/ubuntu/backup.sh" ]]; then
  "${APP_DIR}/scripts/ubuntu/backup.sh"
fi
if git -C "${PROJECT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if ! git -C "${PROJECT_DIR}" diff --quiet \
    || ! git -C "${PROJECT_DIR}" diff --cached --quiet; then
    printf 'Source checkout has local changes; update aborted. Run git status and preserve them first.\n' >&2
    exit 2
  fi
  git -C "${PROJECT_DIR}" pull --ff-only
fi

restart_timers() {
  if [[ "${WAS_CALENDAR_ACTIVE:-0}" -eq 1 ]]; then
    systemctl start moex-tinvest-calendar.timer || true
  fi
  if [[ "${WAS_SHADOW_ACTIVE}" -eq 1 ]]; then
    systemctl start moex-tinvest-shadow.timer || true
  fi
  if [[ "${WAS_HEALTH_ACTIVE}" -eq 1 ]]; then
    systemctl start moex-tinvest-health.timer || true
  fi
  if [[ "${WAS_DAILY_ACTIVE}" -eq 1 ]]; then
    systemctl start moex-tinvest-daily-report.timer || true
  fi
  if [[ "${WAS_INTRADAY_ACTIVE}" -eq 1 ]]; then
    systemctl start moex-tinvest-intraday.timer || true
  fi
  if [[ "${WAS_MAINTENANCE_ACTIVE}" -eq 1 ]]; then
    systemctl start moex-tinvest-maintenance.timer || true
  fi
  if [[ "${WAS_PROD_OBSERVER_ACTIVE}" -eq 1 ]]; then
    systemctl start moex-tinvest-prod-long-observer.timer || true
  fi
}
WAS_SHADOW_ACTIVE=0
WAS_HEALTH_ACTIVE=0
WAS_DAILY_ACTIVE=0
WAS_INTRADAY_ACTIVE=0
WAS_MAINTENANCE_ACTIVE=0
WAS_PROD_OBSERVER_ACTIVE=0
WAS_CALENDAR_ACTIVE=0
systemctl is-active --quiet moex-tinvest-calendar.timer && WAS_CALENDAR_ACTIVE=1
systemctl is-active --quiet moex-tinvest-shadow.timer && WAS_SHADOW_ACTIVE=1
systemctl is-active --quiet moex-tinvest-health.timer && WAS_HEALTH_ACTIVE=1
systemctl is-active --quiet moex-tinvest-daily-report.timer && WAS_DAILY_ACTIVE=1
systemctl is-active --quiet moex-tinvest-intraday.timer && WAS_INTRADAY_ACTIVE=1
systemctl is-active --quiet moex-tinvest-maintenance.timer && WAS_MAINTENANCE_ACTIVE=1
systemctl is-active --quiet moex-tinvest-prod-long-observer.timer \
  && WAS_PROD_OBSERVER_ACTIVE=1
trap restart_timers EXIT

"${APP_DIR}/.venv/bin/python" -m moex_bot.cli preflight \
  --config "${APP_DIR}/config/shadow.json"
stop_timer_if_installed() {
  local unit="$1"
  if systemctl cat "${unit}" >/dev/null 2>&1; then
    systemctl stop "${unit}"
  fi
}
stop_timer_if_installed moex-tinvest-shadow.timer
stop_timer_if_installed moex-tinvest-calendar.timer
stop_timer_if_installed moex-tinvest-health.timer
stop_timer_if_installed moex-tinvest-daily-report.timer
stop_timer_if_installed moex-tinvest-intraday.timer
stop_timer_if_installed moex-tinvest-maintenance.timer
stop_timer_if_installed moex-tinvest-prod-long-observer.timer
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude '.env' --exclude 'artifacts/*' \
  --exclude 'logs/*' --exclude 'data' --exclude 'work' \
  "${PROJECT_DIR}/" "${APP_DIR}/"
bash "${PROJECT_DIR}/scripts/ubuntu/write-build-metadata.sh" \
  "${PROJECT_DIR}" "${APP_DIR}"
find "${APP_DIR}/scripts/ubuntu" -type f -name '*.sh' -exec chmod 0755 {} +
chown -R root:root "${APP_DIR}"
install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 \
  "${STATE_DIR}" "${LOG_DIR}" "${BACKUP_DIR}"
chown root:"${SERVICE_USER}" "${CONFIG_DIR}/bot.env"
chmod 0640 "${CONFIG_DIR}/bot.env"
bash "${PROJECT_DIR}/scripts/ubuntu/install-ca-certificates.sh"
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade "${APP_DIR}[server]"
"${APP_DIR}/.venv/bin/python" -m moex_bot.cli runtime-normalize \
  --runtime "${CONFIG_DIR}/runtime.json"
chown root:"${SERVICE_USER}" "${CONFIG_DIR}/runtime.json"
chmod 0644 "${CONFIG_DIR}/runtime.json"
if [[ ! -e "${CONFIG_DIR}/maintenance.json" ]]; then
  install -m 0644 -o root -g "${SERVICE_USER}" \
    "${APP_DIR}/config/maintenance.json" "${CONFIG_DIR}/maintenance.json"
fi
chown root:"${SERVICE_USER}" "${CONFIG_DIR}/maintenance.json"
chmod 0644 "${CONFIG_DIR}/maintenance.json"
for name in prod_live prod_long prod_experiment; do
  target="${CONFIG_DIR}/${name//_/-}.json"
  if [[ ! -e "${target}" ]]; then
    install -m 0640 -o root -g "${SERVICE_USER}" "${APP_DIR}/config/${name}.json" "${target}"
  fi
  chown root:"${SERVICE_USER}" "${target}"
  chmod 0640 "${target}"
done
"${APP_DIR}/.venv/bin/python" -m moex_bot.cli config-check --root "${APP_DIR}"
(cd "${APP_DIR}" && sha256sum --quiet -c BUILD_MANIFEST.sha256)
installed_version="$("${APP_DIR}/.venv/bin/python" -c \
  'from moex_bot import __version__; print(__version__)')"
expected_version="$(sed -n 's/^app_version=//p' "${APP_DIR}/BUILD_INFO")"
if [[ "${installed_version}" != "${expected_version}" ]]; then
  printf 'Installed package version mismatch: expected=%s actual=%s\n' \
    "${expected_version}" "${installed_version}" >&2
  exit 2
fi
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/"*.service /etc/systemd/system/
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/"*.timer /etc/systemd/system/
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-bot.logrotate" \
  /etc/logrotate.d/moex-tinvest-bot
install -m 0755 "${PROJECT_DIR}/scripts/ubuntu/moex-botctl.sh" \
  /usr/local/sbin/moex-botctl
systemctl daemon-reload
restart_timers
trap - EXIT
if [[ "${WAS_SHADOW_ACTIVE}" -eq 1 ]]; then
  systemctl start moex-tinvest-shadow.service
fi
printf 'Update complete.\n'
