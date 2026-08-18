#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_DIR="${MOEX_BOT_APP_DIR:-/opt/moex-tinvest-bot}"
STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/moex-tinvest-bot}"
LOG_DIR="${MOEX_BOT_LOG_DIR:-/var/log/moex-tinvest-bot}"
PYTHON_BIN="${MOEX_BOT_PYTHON:-${APP_DIR}/.venv/bin/python}"

mkdir -p "${STATE_DIR}/artifacts" "${STATE_DIR}/data" "${LOG_DIR}"
cd "${APP_DIR}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_path="${LOG_DIR}/shadow-${stamp}.log"
geo_path="${STATE_DIR}/artifacts/geo-${stamp}.json"
shadow_path="${STATE_DIR}/artifacts/shadow-${stamp}.json"
portfolio_path="${STATE_DIR}/artifacts/portfolio-${stamp}.json"
flow_path="${STATE_DIR}/artifacts/flow-${stamp}.json"
execution_path="${STATE_DIR}/artifacts/sandbox-execution-${stamp}.json"
outbox_path="${STATE_DIR}/data/notifications.sqlite3"

exec 3>&1 4>&2
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
if [[ "${session_status}" -ne 0 ]]; then
  exit "${session_status}"
fi

"${PYTHON_BIN}" -m moex_bot.cli broker-portfolio-snapshot \
  --universe "${APP_DIR}/config/universe.json" \
  --runtime "/etc/moex-tinvest-bot/runtime.json" \
  --services "${APP_DIR}/config/services.json" \
  --output "${portfolio_path}"

shadow_status=0
"${PYTHON_BIN}" -m moex_bot.cli hourly-shadow \
  --config "${APP_DIR}/config/shadow.json" \
  --universe "${APP_DIR}/config/universe.json" \
  --portfolio "${portfolio_path}" \
  --geo "${geo_path}" \
  --output "${shadow_path}" \
  --outbox "${outbox_path}" || shadow_status=$?

execution_status=0
if [[ "${shadow_status}" -eq 0 ]]; then
  "${PYTHON_BIN}" -m moex_bot.cli sandbox-execute \
    --shadow "${shadow_path}" \
    --portfolio "${portfolio_path}" \
    --runtime "/etc/moex-tinvest-bot/runtime.json" \
    --output "${execution_path}" \
    --outbox "${outbox_path}" || execution_status=$?
  if [[ "${execution_status}" -ne 0 && "${execution_status}" -ne 3 ]]; then
    shadow_status="${execution_status}"
  fi
fi

# Entitlement or network failures are visible in the log but do not replace shadow_status.
"${PYTHON_BIN}" -m moex_bot.cli algopack-flow \
  --secid SBER --futures-ticker SBERF \
  --output "${flow_path}" --outbox "${outbox_path}" || true

telegram_status=0
"${PYTHON_BIN}" -m moex_bot.cli telegram-send \
  --outbox "${outbox_path}" || telegram_status=$?
if [[ "${telegram_status}" -ne 0 && "${telegram_status}" -ne 3 ]]; then
  printf 'WARN: Telegram delivery failed; outbox will retry\n'
fi

exit "${shadow_status}"
