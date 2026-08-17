#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${MOEX_BOT_APP_DIR:-/opt/moex-tinvest-bot}"
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
}
WAS_SHADOW_ACTIVE=0
WAS_HEALTH_ACTIVE=0
systemctl is-active --quiet moex-tinvest-shadow.timer && WAS_SHADOW_ACTIVE=1
systemctl is-active --quiet moex-tinvest-health.timer && WAS_HEALTH_ACTIVE=1
trap restart_timers EXIT

"${APP_DIR}/.venv/bin/python" -m moex_bot.cli preflight \
  --config "${APP_DIR}/config/shadow.json"
systemctl stop moex-tinvest-shadow.timer moex-tinvest-health.timer
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude '.env' --exclude 'artifacts/*' \
  --exclude 'logs/*' --exclude 'data' --exclude 'work' \
  "${PROJECT_DIR}/" "${APP_DIR}/"
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade "${APP_DIR}[server]"
"${APP_DIR}/.venv/bin/python" -m moex_bot.cli config-check --root "${APP_DIR}"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/"*.service /etc/systemd/system/
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/"*.timer /etc/systemd/system/
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-bot.logrotate" \
  /etc/logrotate.d/moex-tinvest-bot
systemctl daemon-reload
restart_timers
trap - EXIT
if [[ "${WAS_SHADOW_ACTIVE}" -eq 1 ]]; then
  systemctl start moex-tinvest-shadow.service
fi
printf 'Update complete.\n'
