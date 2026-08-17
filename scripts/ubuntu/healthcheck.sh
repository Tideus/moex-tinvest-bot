#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${MOEX_BOT_APP_DIR:-/opt/moex-tinvest-bot}"
STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/moex-tinvest-bot}"
CONFIG_DIR="${MOEX_BOT_CONFIG_DIR:-/etc/moex-tinvest-bot}"
MAX_AGE_SECONDS="${MOEX_BOT_HEALTH_MAX_AGE_SECONDS:-7200}"
PYTHON_BIN="${MOEX_BOT_PYTHON:-${APP_DIR}/.venv/bin/python}"

cd "${APP_DIR}"
"${PYTHON_BIN}" -m moex_bot.cli config-check --root "${APP_DIR}"
"${PYTHON_BIN}" -m moex_bot.cli preflight --config "${APP_DIR}/config/shadow.json"
"${PYTHON_BIN}" -m moex_bot.cli integration-preflight \
  --services "${APP_DIR}/config/services.json" \
  --require moex_algopack --require telegram
"${PYTHON_BIN}" -m moex_bot.cli environment-status \
  --runtime "${CONFIG_DIR}/runtime.json" \
  --services "${APP_DIR}/config/services.json"
"${PYTHON_BIN}" -m moex_bot.cli outbox-health \
  --outbox "${STATE_DIR}/data/notifications.sqlite3" --max-pending-due 20

latest="$(find "${STATE_DIR}/artifacts" -maxdepth 1 -type f -name 'shadow-*.json' \
  -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2-)"
session_status=0
"${PYTHON_BIN}" -m moex_bot.cli session-check || session_status=$?
if [[ "${session_status}" -eq 3 ]]; then
  printf 'PASS: outside conservative market window; artifact freshness not enforced\n'
  exit 0
fi
if [[ "${session_status}" -ne 0 ]]; then exit "${session_status}"; fi
if [[ -z "${latest}" ]]; then
  printf 'FAIL: no shadow artifact found during market window\n' >&2
  exit 2
fi

now="$(date +%s)"
modified="$(stat -c %Y "${latest}")"
age="$((now - modified))"
if (( age < 0 || age > MAX_AGE_SECONDS )); then
  printf 'FAIL: latest shadow artifact is stale: %ss\n' "${age}" >&2
  exit 2
fi
printf 'PASS: latest shadow artifact age=%ss path=%s\n' "${age}" "${latest}"
