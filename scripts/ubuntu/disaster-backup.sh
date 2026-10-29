#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_DIR="${MOEX_BOT_APP_DIR:-/opt/moex-tinvest-bot}"
CONFIG_DIR="${MOEX_BOT_CONFIG_DIR:-/etc/moex-tinvest-bot}"
STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/moex-tinvest-bot}"
LOG_DIR="${MOEX_BOT_LOG_DIR:-/var/log/moex-tinvest-bot}"
BACKUP_DIR="${MOEX_BOT_BACKUP_DIR:-/var/backups/moex-tinvest-bot}"

if [[ "${EUID}" -ne 0 ]]; then
  printf 'Run as root (sudo).\n' >&2
  exit 2
fi

output_dir="${BACKUP_DIR}"
if [[ $# -gt 0 ]]; then
  if [[ "${1:-}" != "--output-dir" || $# -ne 2 ]]; then
    printf 'Usage: sudo moex-botctl disaster-backup [--output-dir DIR]\n' >&2
    exit 2
  fi
  output_dir="$2"
fi

for required in "${STATE_DIR}" "${LOG_DIR}" "${CONFIG_DIR}/runtime.json" \
  "${CONFIG_DIR}/maintenance.json"; do
  if [[ ! -e "${required}" ]]; then
    printf 'FAIL: required recovery source is missing: %s\n' "${required}" >&2
    exit 2
  fi
done

timers=(
  moex-tinvest-shadow.timer
  moex-tinvest-intraday.timer
  moex-tinvest-health.timer
  moex-tinvest-daily-report.timer
  moex-tinvest-maintenance.timer
  moex-tinvest-prod-long-observer.timer
)
services=(
  moex-tinvest-shadow.service
  moex-tinvest-intraday.service
  moex-tinvest-health.service
  moex-tinvest-daily-report.service
  moex-tinvest-maintenance.service
  moex-tinvest-prod-long-observer.service
)
active_timers=()
stage_dir=""

restore_timers() {
  local timer
  for timer in "${active_timers[@]}"; do
    systemctl start "${timer}" >/dev/null 2>&1 || true
  done
}

cleanup() {
  local status="$?"
  trap - EXIT
  if [[ -n "${stage_dir}" && -d "${stage_dir}" ]]; then
    find "${stage_dir}" -depth -delete 2>/dev/null || true
  fi
  restore_timers
  exit "${status}"
}
trap cleanup EXIT

for timer in "${timers[@]}"; do
  if systemctl is-active --quiet "${timer}"; then active_timers+=("${timer}"); fi
done
systemctl stop "${timers[@]}" "${services[@]}" 2>/dev/null || true

mkdir -p "${output_dir}"
stage_dir="$(mktemp -d -t moex-disaster-backup.XXXXXXXX)"
mkdir -p "${stage_dir}/state" "${stage_dir}/logs" \
  "${stage_dir}/config" "${stage_dir}/metadata"
cp -a "${STATE_DIR}/." "${stage_dir}/state/"
cp -a "${LOG_DIR}/." "${stage_dir}/logs/"
install -m 0644 "${CONFIG_DIR}/runtime.json" "${stage_dir}/config/runtime.json"
install -m 0644 "${CONFIG_DIR}/maintenance.json" \
  "${stage_dir}/config/maintenance.json"

for name in BUILD_INFO BUILD_MANIFEST.sha256 VERSION; do
  if [[ -r "${APP_DIR}/${name}" ]]; then
    cp -a "${APP_DIR}/${name}" "${stage_dir}/metadata/${name}"
  fi
done
{
  printf 'format_version=1\n'
  printf 'created_at=%s\n' "$(date --iso-8601=seconds)"
  printf 'hostname=%s\n' "$(hostname)"
  printf 'secrets_included=no\n'
  printf 'restore_script=scripts/ubuntu/restore-new-vm.sh\n'
} >"${stage_dir}/RECOVERY_MANIFEST.txt"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="${output_dir}/moex-disaster-${stamp}.tar.gz"
tar --create --gzip --file "${archive}" --directory "${stage_dir}" .
(cd "${output_dir}" && sha256sum "$(basename "${archive}")") \
  >"${archive}.sha256"
(cd "${output_dir}" && sha256sum -c "$(basename "${archive}.sha256")") \
  >/dev/null

find "${stage_dir}" -depth -delete
stage_dir=""
restore_timers
active_timers=()
trap - EXIT

printf 'DISASTER BACKUP READY: %s\n' "${archive}"
printf 'CHECKSUM: %s\n' "${archive}.sha256"
printf 'IMPORTANT: bot.env is not included; keep an encrypted copy separately.\n'
