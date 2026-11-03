#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_DIR="${MOEX_BOT_APP_DIR:-/opt/moex-tinvest-bot}"
STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/moex-tinvest-bot}"
LOG_DIR="${MOEX_BOT_LOG_DIR:-/var/log/moex-tinvest-bot}"
PYTHON_BIN="${MOEX_BOT_PYTHON:-${APP_DIR}/.venv/bin/python}"
CONFIG_DIR="${MOEX_BOT_CONFIG_DIR:-/etc/moex-tinvest-bot}"
export SSL_CERT_FILE="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"
export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-${SSL_CERT_FILE}}"

mkdir -p "${STATE_DIR}/artifacts" "${STATE_DIR}/data" "${LOG_DIR}"
date_args=()
if [[ -n "${MOEX_BOT_REPORT_DATE:-}" ]]; then
  date_args=(--report-date "${MOEX_BOT_REPORT_DATE}")
fi
# A delayed invocation after midnight still describes the closed previous session.
# An explicit future/unclosed date fails before any broker read or notification.
report_date="$("${PYTHON_BIN}" -m moex_bot.long_daily_report --date-only "${date_args[@]}")"
weekday="$(date --date="${report_date}" +%u)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
output_path="${STATE_DIR}/artifacts/daily-performance-${report_date}.txt"
intraday_output_path="${STATE_DIR}/artifacts/intraday-daily-performance-${report_date}.txt"
outbox_path="${STATE_DIR}/data/notifications.sqlite3"
log_path="${LOG_DIR}/daily-report-${stamp}.log"

exec >>"${log_path}" 2>&1
report_status=0
if [[ -e "${CONFIG_DIR}/sandbox-long-once.owner" || -e "${STATE_DIR}/long-once/sandbox.sqlite3" ]]; then
  "${PYTHON_BIN}" -m moex_bot.long_daily_report \
    --report-date "${report_date}" --base "${STATE_DIR}/long-once" \
    --accounts "${APP_DIR}/config/accounts.json" \
    --universe "${APP_DIR}/config/universe.json" \
    --notifications "${APP_DIR}/config/notifications.json" \
    --output "${STATE_DIR}/artifacts/long-daily-performance-${report_date}.txt" \
    --outbox "${outbox_path}" || report_status=$?
else
"${PYTHON_BIN}" -m moex_bot.cli account-performance-report \
  --artifacts "${STATE_DIR}/artifacts" \
  --start-date "${report_date}" \
  --end-date "${report_date}" \
  --timezone Europe/Moscow \
  --universe "${APP_DIR}/config/universe.json" \
  --runtime "${CONFIG_DIR}/runtime.json" \
  --services "${APP_DIR}/config/services.json" \
  --notifications "${APP_DIR}/config/notifications.json" \
  --output "${output_path}" \
  --outbox "${outbox_path}" || report_status=$?
fi

prod_status=0
"${PYTHON_BIN}" -m moex_bot.prod_once_observer \
  --settings "${CONFIG_DIR}/prod-long-once.json" --report-date "${report_date}" \
  --output "${STATE_DIR}/artifacts/prod-long-daily-control-${report_date}.txt" \
  --outbox "${outbox_path}" || prod_status=$?

intraday_status=0
"${PYTHON_BIN}" -m moex_bot.cli intraday-performance-report \
  --accounts "${APP_DIR}/config/accounts.json" \
  --universe "${APP_DIR}/config/universe.json" \
  --notifications "${APP_DIR}/config/notifications.json" \
  --artifacts "${STATE_DIR}/artifacts" \
  --report-date "${report_date}" \
  --output "${intraday_output_path}" \
  --outbox "${outbox_path}" || intraday_status=$?
# Legacy weekly aggregation cannot consume one-shot LONG evidence. Do not report it
# as zero or hide a missing current LONG daily report behind a successful skip.
if [[ "${weekday}" -eq 5 && ! -e "${CONFIG_DIR}/sandbox-long-once.owner" && ! -e "${STATE_DIR}/long-once/sandbox.sqlite3" ]]; then
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
if [[ "${report_status}" -ne 0 && "${report_status}" -ne 3 ]]; then exit "${report_status}"; fi
if [[ "${intraday_status}" -ne 0 && "${intraday_status}" -ne 3 ]]; then exit "${intraday_status}"; fi
if [[ "${prod_status}" -ne 0 ]]; then exit "${prod_status}"; fi
exit 0
