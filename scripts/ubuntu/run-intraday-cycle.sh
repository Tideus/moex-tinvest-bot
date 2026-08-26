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
cd "${APP_DIR}"

weekday="$(TZ=Europe/Moscow date +%u)"
clock="$(TZ=Europe/Moscow date +%H%M)"
if (( weekday > 5 || 10#${clock} < 1000 || 10#${clock} > 1859 )); then
  printf 'SKIP: outside configured intraday monitoring window\n'
  exit 0
fi

if [[ -z "${T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID:-}" ]]; then
  printf 'FAIL: T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID is missing\n' >&2
  exit 2
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_path="${LOG_DIR}/intraday-${stamp}.log"
portfolio_path="${STATE_DIR}/artifacts/intraday-portfolio-${stamp}.json"
plan_path="${STATE_DIR}/artifacts/intraday-plan-${stamp}.json"
execution_path="${STATE_DIR}/artifacts/intraday-execution-${stamp}.json"
state_path="${STATE_DIR}/data/intraday.sqlite3"
outbox_path="${STATE_DIR}/data/notifications.sqlite3"

exec >>"${log_path}" 2>&1

"${PYTHON_BIN}" -m moex_bot.cli intraday-reconcile \
  --accounts "${APP_DIR}/config/accounts.json"

"${PYTHON_BIN}" -m moex_bot.cli broker-portfolio-snapshot \
  --universe "${APP_DIR}/config/universe.json" \
  --runtime "/etc/moex-tinvest-bot/runtime.json" \
  --services "${APP_DIR}/config/services.json" \
  --account-id-env T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID \
  --output "${portfolio_path}"

plan_status=0
"${PYTHON_BIN}" -m moex_bot.cli intraday-plan \
  --config "${APP_DIR}/config/intraday.json" \
  --universe "${APP_DIR}/config/universe.json" \
  --portfolio "${portfolio_path}" \
  --state "${state_path}" \
  --output "${plan_path}" || plan_status=$?

if [[ "${plan_status}" -eq 0 ]]; then
  "${PYTHON_BIN}" -m moex_bot.cli intraday-sandbox-execute \
    --accounts "${APP_DIR}/config/accounts.json" \
    --config "${APP_DIR}/config/intraday.json" \
    --plan "${plan_path}" \
    --portfolio "${portfolio_path}" \
    --output "${execution_path}"
fi

"${PYTHON_BIN}" -m moex_bot.cli intraday-trade-notifications \
  --accounts "${APP_DIR}/config/accounts.json" \
  --universe "${APP_DIR}/config/universe.json" \
  --notifications "${APP_DIR}/config/notifications.json" \
  --outbox "${outbox_path}"

"${PYTHON_BIN}" -m moex_bot.cli telegram-send --outbox "${outbox_path}" || true
exit "${plan_status}"
