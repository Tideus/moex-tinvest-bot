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

restart_timers() {
  if [[ "${WAS_SHADOW_ACTIVE}" -eq 1 ]]; then
    systemctl start moex-tinvest-shadow.timer || true
  fi
  if [[ "${WAS_HEALTH_ACTIVE}" -eq 1 ]]; then
    systemctl start moex-tinvest-health.timer || true
  fi
  if [[ "${WAS_DAILY_ACTIVE}" -eq 1 ]]; then
    systemctl start moex-tinvest-daily-report.timer || true
  fi
}
WAS_SHADOW_ACTIVE=0
WAS_HEALTH_ACTIVE=0
WAS_DAILY_ACTIVE=0
systemctl is-active --quiet moex-tinvest-shadow.timer && WAS_SHADOW_ACTIVE=1
systemctl is-active --quiet moex-tinvest-health.timer && WAS_HEALTH_ACTIVE=1
systemctl is-active --quiet moex-tinvest-daily-report.timer && WAS_DAILY_ACTIVE=1
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
stop_timer_if_installed moex-tinvest-health.timer
stop_timer_if_installed moex-tinvest-daily-report.timer
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude '.env' --exclude 'artifacts/*' \
  --exclude 'logs/*' --exclude 'data' --exclude 'work' \
  "${PROJECT_DIR}/" "${APP_DIR}/"
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
"${APP_DIR}/.venv/bin/python" -m moex_bot.cli config-check --root "${APP_DIR}"
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
