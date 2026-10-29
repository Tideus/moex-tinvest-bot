#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_NAME="moex-tinvest-bot"
SERVICE_USER="${MOEX_BOT_USER:-moexbot}"
APP_DIR="${MOEX_BOT_APP_DIR:-/opt/${APP_NAME}}"
CONFIG_DIR="${MOEX_BOT_CONFIG_DIR:-/etc/${APP_NAME}}"
STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/${APP_NAME}}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  sudo ./scripts/ubuntu/restore-new-vm.sh \
    --backup /path/moex-disaster-YYYYmmddTHHMMSSZ.tar.gz \
    --env-file /path/bot.env [--enable] [--no-packages]

The adjacent .sha256 file is required. The recovery archive never contains secrets.
EOF
}

backup=""
env_file=""
enable=0
install_packages=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup) shift; backup="${1:-}" ;;
    --env-file) shift; env_file="${1:-}" ;;
    --enable) enable=1 ;;
    --no-packages) install_packages=0 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ "${EUID}" -ne 0 ]]; then
  printf 'Run as root (sudo).\n' >&2
  exit 2
fi
if [[ -z "${backup}" || -z "${env_file}" ]]; then
  usage >&2
  exit 2
fi
backup="$(realpath "${backup}")"
env_file="$(realpath "${env_file}")"
checksum="${backup}.sha256"
for required in "${backup}" "${checksum}" "${env_file}"; do
  if [[ ! -r "${required}" ]]; then
    printf 'FAIL: required recovery file is not readable: %s\n' "${required}" >&2
    exit 2
  fi
done

(cd "$(dirname "${backup}")" && sha256sum -c "$(basename "${checksum}")")

python3 - "${backup}" <<'PY'
import sys
import tarfile
from pathlib import PurePosixPath

archive = sys.argv[1]
with tarfile.open(archive, "r:gz") as payload:
    names = set()
    for member in payload.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"FAIL: unsafe archive path: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            raise SystemExit(f"FAIL: unsafe archive member: {member.name}")
        names.add(member.name.removeprefix("./"))
required = {"RECOVERY_MANIFEST.txt", "config/runtime.json", "config/maintenance.json"}
missing = required - names
if missing:
    raise SystemExit(f"FAIL: incomplete disaster backup: {sorted(missing)}")
PY

stage_dir="$(mktemp -d -t moex-disaster-restore.XXXXXXXX)"
cleanup() { find "${stage_dir}" -depth -delete 2>/dev/null || true; }
trap cleanup EXIT
tar --extract --gzip --file "${backup}" --directory "${stage_dir}" \
  --no-same-owner --no-same-permissions

install_args=(--no-enable)
if [[ "${install_packages}" -eq 0 ]]; then install_args+=(--no-packages); fi
bash "${PROJECT_DIR}/scripts/ubuntu/install.sh" "${install_args[@]}"

install -o root -g "${SERVICE_USER}" -m 0640 "${env_file}" \
  "${CONFIG_DIR}/bot.env"
install -o root -g "${SERVICE_USER}" -m 0644 \
  "${stage_dir}/config/runtime.json" "${CONFIG_DIR}/runtime.json"
install -o root -g "${SERVICE_USER}" -m 0644 \
  "${stage_dir}/config/maintenance.json" "${CONFIG_DIR}/maintenance.json"
rsync -a --delete "${stage_dir}/state/" "${STATE_DIR}/"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${STATE_DIR}"
chmod 0750 "${STATE_DIR}"

/usr/local/sbin/moex-botctl prelaunch
if [[ "${enable}" -eq 1 ]]; then
  /usr/local/sbin/moex-botctl start
fi

printf 'RESTORE COMPLETE: code, configuration and state were restored.\n'
if [[ "${enable}" -eq 0 ]]; then
  printf 'Timers remain disabled. Start with: sudo moex-botctl start\n'
fi
