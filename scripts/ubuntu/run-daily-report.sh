#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_DIR="${MOEX_BOT_APP_DIR:-/opt/moex-tinvest-bot}"
STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/moex-tinvest-bot}"
LOG_DIR="${MOEX_BOT_LOG_DIR:-/var/log/moex-tinvest-bot}"
PYTHON_BIN="${MOEX_BOT_PYTHON:-${APP_DIR}/.venv/bin/python}"

mkdir -p "${STATE_DIR}/artifacts" "${STATE_DIR}/data" "${LOG_DIR}"
report_date="$(date --date='TZ="Europe/Moscow" now' +%F)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
output_path="${STATE_DIR}/artifacts/daily-trades-${report_date}.txt"
outbox_path="${STATE_DIR}/data/notifications.sqlite3"
log_path="${LOG_DIR}/daily-report-${stamp}.log"

exec >>"${log_path}" 2>&1
"${PYTHON_BIN}" -m moex_bot.cli daily-trade-report \
  --artifacts "${STATE_DIR}/artifacts" \
  --date "${report_date}" \
  --timezone Europe/Moscow \
  --output "${output_path}" \
  --outbox "${outbox_path}"

telegram_status=0
"${PYTHON_BIN}" -m moex_bot.cli telegram-send \
  --outbox "${outbox_path}" || telegram_status=$?
if [[ "${telegram_status}" -ne 0 && "${telegram_status}" -ne 3 ]]; then
  printf 'WARN: daily Telegram delivery failed; outbox will retry\n'
fi
exit 0
