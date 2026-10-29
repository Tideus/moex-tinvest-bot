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

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_path="${LOG_DIR}/prod-long-observer-${stamp}.log"
geo_path="${STATE_DIR}/artifacts/prod-geo-${stamp}.json"
broker_path="${STATE_DIR}/artifacts/prod-portfolio-${stamp}.json"
sleeve_path="${STATE_DIR}/artifacts/prod-sleeve-${stamp}.json"
shadow_path="${STATE_DIR}/artifacts/prod-long-plan-${stamp}.json"
outbox_path="${STATE_DIR}/data/notifications.sqlite3"

exec >>"${log_path}" 2>&1

geo_status=0
"${PYTHON_BIN}" -m moex_bot.cli geo-refresh \
  --sources "${APP_DIR}/config/geo_sources.json" \
  --output "${geo_path}" || geo_status=$?
if [[ ! -s "${geo_path}" ]]; then
  printf 'FAIL: geo collector did not produce a fail-closed payload (status=%s)\n' \
    "${geo_status}" >&2
  exit 2
fi

session_status=0
"${PYTHON_BIN}" -m moex_bot.cli session-check || session_status=$?
if [[ "${session_status}" -eq 3 ]]; then
  printf 'SKIP: outside conservative MOEX stock window\n'
  exit 0
fi
if [[ "${session_status}" -ne 0 ]]; then exit "${session_status}"; fi

"${PYTHON_BIN}" -m moex_bot.cli broker-portfolio-snapshot \
  --universe "${APP_DIR}/config/universe.json" \
  --runtime "${APP_DIR}/config/prod_runtime.json" \
  --services "${APP_DIR}/config/services.json" \
  --output "${broker_path}"

"${PYTHON_BIN}" -m moex_bot.cli prod-long-prepare \
  --policy "${APP_DIR}/config/prod_long.json" \
  --broker-snapshot "${broker_path}" \
  --output "${sleeve_path}"

"${PYTHON_BIN}" -m moex_bot.cli hourly-shadow \
  --config "${APP_DIR}/config/prod_shadow.json" \
  --universe "${APP_DIR}/config/universe.json" \
  --portfolio "${sleeve_path}" \
  --geo "${geo_path}" \
  --output "${shadow_path}" \
  --notifications "${APP_DIR}/config/notifications.json" \
  --report-environment "PROD OBSERVER" \
  --allowed-secids-policy "${APP_DIR}/config/prod_long.json" \
  --outbox "${outbox_path}"

telegram_status=0
"${PYTHON_BIN}" -m moex_bot.cli telegram-send \
  --outbox "${outbox_path}" || telegram_status=$?
if [[ "${telegram_status}" -ne 0 && "${telegram_status}" -ne 3 ]]; then
  printf 'WARN: Telegram delivery failed; outbox will retry\n'
fi

printf 'PASS: production IIS observation complete; real_orders=DISABLED\n'
