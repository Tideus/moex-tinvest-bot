#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  printf 'Run as root (sudo).\n' >&2
  exit 2
fi

systemctl daemon-reload
if ! systemctl start moex-tinvest-shadow.service; then
  journalctl -u moex-tinvest-shadow.service -n 100 --no-pager >&2
  printf 'Activation blocked: the first shadow cycle failed.\n' >&2
  exit 2
fi
systemctl start moex-tinvest-health.service
systemctl enable --now moex-tinvest-shadow.timer moex-tinvest-health.timer
printf 'Activated shadow and health timers. Live orders remain disabled.\n'
