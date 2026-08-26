#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_DIR="${MOEX_BOT_APP_DIR:-/opt/moex-tinvest-bot}"
STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/moex-tinvest-bot}"
LOG_DIR="${MOEX_BOT_LOG_DIR:-/var/log/moex-tinvest-bot}"
PYTHON_BIN="${MOEX_BOT_PYTHON:-${APP_DIR}/.venv/bin/python}"
export SSL_CERT_FILE="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"
export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-${SSL_CERT_FILE}}"

mkdir -p "${STATE_DIR}/artifacts" "${STATE_DIR}/data" "${LOG_DIR}"
report_date="${MOEX_BOT_REPORT_DATE:-$(date --date='TZ="Europe/Moscow" now' +%F)}"
weekday="$(date --date="${report_date}" +%u)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
output_path="${STATE_DIR}/artifacts/daily-performance-${report_date}.txt"
intraday_output_path="${STATE_DIR}/artifacts/intraday-daily-performance-${report_date}.txt"
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
  --notifications "${APP_DIR}/config/notifications.json" \
  --output "${output_path}" \
  --outbox "${outbox_path}" || report_status=$?
if [[ "${report_status}" -ne 0 && "${report_status}" -ne 3 ]]; then
  exit "${report_status}"
fi

intraday_status=0
"${PYTHON_BIN}" -m moex_bot.cli intraday-performance-report \
  --accounts "${APP_DIR}/config/accounts.json" \
  --universe "${APP_DIR}/config/universe.json" \
  --notifications "${APP_DIR}/config/notifications.json" \
  --artifacts "${STATE_DIR}/artifacts" \
  --report-date "${report_date}" \
  --output "${intraday_output_path}" \
  --outbox "${outbox_path}" || intraday_status=$?
if [[ "${intraday_status}" -ne 0 && "${intraday_status}" -ne 3 ]]; then
  exit "${intraday_status}"
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
    --notifications "${APP_DIR}/config/notifications.json" \
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
