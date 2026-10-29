#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_DIR="${MOEX_BOT_APP_DIR:-/opt/moex-tinvest-bot}"
CONFIG_DIR="${MOEX_BOT_CONFIG_DIR:-/etc/moex-tinvest-bot}"
STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/moex-tinvest-bot}"
LOG_DIR="${MOEX_BOT_LOG_DIR:-/var/log/moex-tinvest-bot}"
BACKUP_DIR="${MOEX_BOT_BACKUP_DIR:-/var/backups/moex-tinvest-bot}"
SERVICE_USER="${MOEX_BOT_USER:-moexbot}"
PYTHON_BIN="${MOEX_BOT_PYTHON:-${APP_DIR}/.venv/bin/python}"
ENV_FILE="${CONFIG_DIR}/bot.env"
RUNTIME_FILE="${CONFIG_DIR}/runtime.json"
SERVICES_FILE="${APP_DIR}/config/services.json"
ALLOW_NON_ROOT_TEST="${MOEX_BOT_ALLOW_NON_ROOT_TEST:-0}"

if [[ "${1:-}" != "--confirm-reset-history" || $# -ne 1 ]]; then
  printf 'Usage: sudo moex-botctl reset-history --confirm-reset-history\n' >&2
  exit 2
fi
if [[ "${EUID}" -ne 0 && "${ALLOW_NON_ROOT_TEST}" != "1" ]]; then
  printf 'Run as root (sudo).\n' >&2
  exit 2
fi
for path in "${STATE_DIR}" "${LOG_DIR}" "${BACKUP_DIR}"; do
  if [[ -z "${path}" || "${path}" == "/" ]]; then
    printf 'FAIL: unsafe reset path: %s\n' "${path:-empty}" >&2
    exit 2
  fi
done
if [[ "${ALLOW_NON_ROOT_TEST}" != "1" ]]; then
  if [[ "${STATE_DIR}" != "/var/lib/moex-tinvest-bot" \
     || "${LOG_DIR}" != "/var/log/moex-tinvest-bot" \
     || "${BACKUP_DIR}" != "/var/backups/moex-tinvest-bot" ]]; then
    printf 'FAIL: reset accepts only the installed MOEX bot state/log/backup directories.\n' >&2
    exit 2
  fi
fi
if [[ ! -r "${ENV_FILE}" ]]; then
  printf 'FAIL: secrets file is not readable: %s\n' "${ENV_FILE}" >&2
  exit 2
fi
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

timers=(
  moex-tinvest-shadow.timer
  moex-tinvest-intraday.timer
  moex-tinvest-health.timer
  moex-tinvest-daily-report.timer
)
services=(
  moex-tinvest-shadow.service
  moex-tinvest-intraday.service
  moex-tinvest-health.service
  moex-tinvest-daily-report.service
)
active_timers=()
stage_dir=""
partial_archive=""
snapshot_dir=""
reset_complete=0

as_service() {
  if [[ "${ALLOW_NON_ROOT_TEST}" == "1" ]]; then
    "$@"
  else
    runuser --user "${SERVICE_USER}" --preserve-environment -- "$@"
  fi
}

restore_timers() {
  local timer
  for timer in "${active_timers[@]}"; do
    systemctl start "${timer}" >/dev/null 2>&1 || true
  done
}

move_contents() {
  local source="$1"
  local target="$2"
  mkdir -p "${source}" "${target}"
  find "${source}" -mindepth 1 -maxdepth 1 -exec mv -t "${target}" -- {} +
}

rollback() {
  local status="$?"
  trap - EXIT
  if [[ "${reset_complete}" -eq 0 && -n "${stage_dir}" && -d "${stage_dir}" ]]; then
    move_contents "${stage_dir}/state" "${STATE_DIR}" || true
    move_contents "${stage_dir}/logs" "${LOG_DIR}" || true
  fi
  if [[ -n "${partial_archive}" ]]; then rm -f -- "${partial_archive}"; fi
  if [[ -n "${stage_dir}" && -d "${stage_dir}" ]]; then
    find "${stage_dir}" -depth -delete 2>/dev/null || true
  fi
  if [[ -n "${snapshot_dir}" && -d "${snapshot_dir}" ]]; then
    find "${snapshot_dir}" -depth -delete 2>/dev/null || true
  fi
  restore_timers
  exit "${status}"
}
trap rollback EXIT

mkdir -p "${STATE_DIR}" "${LOG_DIR}" "${BACKUP_DIR}"
for timer in "${timers[@]}"; do
  if systemctl is-active --quiet "${timer}"; then active_timers+=("${timer}"); fi
done
systemctl stop "${timers[@]}" "${services[@]}" 2>/dev/null || true

snapshot_dir="$(mktemp -d -t moex-reset-broker-state.XXXXXXXX)"
long_snapshot="${snapshot_dir}/long.json"
intraday_snapshot="${snapshot_dir}/intraday.json"
as_service "${PYTHON_BIN}" -m moex_bot.cli broker-portfolio-snapshot \
  --universe "${APP_DIR}/config/universe.json" \
  --runtime "${RUNTIME_FILE}" --services "${SERVICES_FILE}" \
  --account-id-env T_INVEST_SANDBOX_LONG_ACCOUNT_ID --output "${long_snapshot}"
as_service "${PYTHON_BIN}" -m moex_bot.cli broker-portfolio-snapshot \
  --universe "${APP_DIR}/config/universe.json" \
  --runtime "${RUNTIME_FILE}" --services "${SERVICES_FILE}" \
  --account-id-env T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID --output "${intraday_snapshot}"

"${PYTHON_BIN}" -m moex_bot.cli history-reset-safety \
  --runtime "${RUNTIME_FILE}" \
  --long-snapshot "${long_snapshot}" \
  --intraday-snapshot "${intraday_snapshot}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
stage_dir="$(mktemp -d "${BACKUP_DIR}/.history-reset-${stamp}.XXXXXXXX")"
mkdir -p "${stage_dir}/state" "${stage_dir}/logs" "${stage_dir}/broker-state-before-reset"
move_contents "${STATE_DIR}" "${stage_dir}/state"
move_contents "${LOG_DIR}" "${stage_dir}/logs"
cp -a "${long_snapshot}" "${stage_dir}/broker-state-before-reset/long.json"
cp -a "${intraday_snapshot}" "${stage_dir}/broker-state-before-reset/intraday.json"

archive="${BACKUP_DIR}/moex-history-reset-${stamp}.tar.gz"
partial_archive="${archive}.partial"
tar --create --gzip --file "${partial_archive}" --directory "${stage_dir}" .
sha256sum "${partial_archive}" >/dev/null
mv -- "${partial_archive}" "${archive}"
partial_archive=""
(cd "${BACKUP_DIR}" && sha256sum "$(basename "${archive}")") >"${archive}.sha256"
(cd "${BACKUP_DIR}" && sha256sum -c "$(basename "${archive}.sha256")") >/dev/null

if [[ "${ALLOW_NON_ROOT_TEST}" == "1" ]]; then
  mkdir -p "${STATE_DIR}/artifacts" "${STATE_DIR}/data" "${LOG_DIR}"
  chmod 0750 "${STATE_DIR}" "${STATE_DIR}/artifacts" "${STATE_DIR}/data" "${LOG_DIR}"
else
  install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 \
    "${STATE_DIR}" "${STATE_DIR}/artifacts" "${STATE_DIR}/data" "${LOG_DIR}"
fi
reset_complete=1
find "${stage_dir}" -depth -delete
stage_dir=""
find "${snapshot_dir}" -depth -delete
snapshot_dir=""
restore_timers
active_timers=()
trap - EXIT

printf 'RESET COMPLETE: local databases, artifacts and logs were cleared.\n'
printf 'Broker positions and configuration were not changed.\n'
printf 'Backup: %s\n' "${archive}"
printf 'Checksum: %s\n' "${archive}.sha256"
