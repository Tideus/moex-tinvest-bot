#!/usr/bin/env bash
set -Eeuo pipefail

PURGE=0
if [[ "${1:-}" == "--purge" ]]; then PURGE=1; fi
if [[ "${EUID}" -ne 0 ]]; then
  printf 'Run as root (sudo).\n' >&2
  exit 2
fi

systemctl disable --now moex-tinvest-shadow.timer moex-tinvest-health.timer 2>/dev/null || true
rm -f /etc/systemd/system/moex-tinvest-shadow.service \
  /etc/systemd/system/moex-tinvest-shadow.timer \
  /etc/systemd/system/moex-tinvest-health.service \
  /etc/systemd/system/moex-tinvest-health.timer \
  /etc/logrotate.d/moex-tinvest-bot
systemctl daemon-reload
rm -rf /opt/moex-tinvest-bot
if [[ "${PURGE}" -eq 1 ]]; then
  rm -rf /etc/moex-tinvest-bot /var/lib/moex-tinvest-bot \
    /var/log/moex-tinvest-bot /var/backups/moex-tinvest-bot
  userdel moexbot 2>/dev/null || true
  printf 'Uninstalled and purged configuration/state.\n'
else
  printf 'Uninstalled application; configuration and state were preserved.\n'
fi
