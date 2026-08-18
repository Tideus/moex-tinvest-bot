#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_DIR="${MOEX_BOT_APP_DIR:-/opt/moex-tinvest-bot}"
STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/moex-tinvest-bot}"
LOG_DIR="${MOEX_BOT_LOG_DIR:-/var/log/moex-tinvest-bot}"
PYTHON_BIN="${MOEX_BOT_PYTHON:-${APP_DIR}/.venv/bin/python}"

mkdir -p "${STATE_DIR}/artifacts" "${STATE_DIR}/data" "${LOG_DIR}"
report_date="${MOEX_BOT_REPORT_DATE:-$(date --date='TZ="Europe/Moscow" now' +%F)}"
weekday="$(date --date="${report_date}" +%u)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
output_path="${STATE_DIR}/artifacts/daily-performance-${report_date}.txt"
outbox_path="${STATE_DIR}/data/notifications.sqlite3"
log_path="${LOG_DIR}/daily-report-${stamp}.log"

exec >>"${log_path}" 2>&1
if [[ "${weekday}" -gt 5 ]]; then
  printf 'SKIP: daily performance report is not scheduled on weekends\n'
  exit 0
fi

report_status=0
"${PYTHON_BIN}" -m moex_bot.cli account-performance-report \
  --artifacts "${STATE_DIR}/artifacts" \
  --start-date "${report_date}" \
  --end-date "${report_date}" \
  --timezone Europe/Moscow \
  --universe "${APP_DIR}/config/universe.json" \
  --runtime /etc/moex-tinvest-bot/runtime.json \
  --services "${APP_DIR}/config/services.json" \
  --output "${output_path}" \
  --outbox "${outbox_path}" || report_status=$?
if [[ "${report_status}" -ne 0 && "${report_status}" -ne 3 ]]; then
  exit "${report_status}"
fi

if [[ "${weekday}" -eq 5 ]]; then
  week_start="$(date --date="${report_date} -4 days" +%F)"
  weekly_output="${STATE_DIR}/artifacts/weekly-performance-${week_start}-${report_date}.txt"
  weekly_status=0
  "${PYTHON_BIN}" -m moex_bot.cli account-performance-report \
    --artifacts "${STATE_DIR}/artifacts" \
    --start-date "${week_start}" \
    --end-date "${report_date}" \
    --timezone Europe/Moscow \
    --universe "${APP_DIR}/config/universe.json" \
    --runtime /etc/moex-tinvest-bot/runtime.json \
    --services "${APP_DIR}/config/services.json" \
    --output "${weekly_output}" \
    --outbox "${outbox_path}" \
    --weekly || weekly_status=$?
  if [[ "${weekly_status}" -ne 0 && "${weekly_status}" -ne 3 ]]; then
    exit "${weekly_status}"
  fi
fi

telegram_status=0
"${PYTHON_BIN}" -m moex_bot.cli telegram-send \
  --outbox "${outbox_path}" || telegram_status=$?
if [[ "${telegram_status}" -ne 0 && "${telegram_status}" -ne 3 ]]; then
  printf 'WARN: daily Telegram delivery failed; outbox will retry\n'
fi
exit 0
