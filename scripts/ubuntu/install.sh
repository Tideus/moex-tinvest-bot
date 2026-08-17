#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

APP_NAME="moex-tinvest-bot"
SERVICE_USER="${MOEX_BOT_USER:-moexbot}"
APP_DIR="${MOEX_BOT_APP_DIR:-/opt/${APP_NAME}}"
CONFIG_DIR="${MOEX_BOT_CONFIG_DIR:-/etc/${APP_NAME}}"
STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/${APP_NAME}}"
LOG_DIR="${MOEX_BOT_LOG_DIR:-/var/log/${APP_NAME}}"
BACKUP_DIR="${MOEX_BOT_BACKUP_DIR:-/var/backups/${APP_NAME}}"
DESTDIR="${DESTDIR:-}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENABLE_TIMER=0
INSTALL_PACKAGES=1

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/ubuntu/install.sh [--enable] [--no-packages]

Environment:
  DESTDIR=/tmp/root   Install into a staging root without user/systemd mutations.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --enable) ENABLE_TIMER=1 ;;
    --no-enable) ENABLE_TIMER=0 ;;
    --no-packages) INSTALL_PACKAGES=0 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

root_path() {
  local path="${1#/}"
  if [[ -n "${DESTDIR}" ]]; then
    printf '%s/%s' "${DESTDIR%/}" "${path}"
  else
    printf '/%s' "${path}"
  fi
}

if [[ -z "${DESTDIR}" && "${EUID}" -ne 0 ]]; then
  printf 'Run as root (sudo) or set DESTDIR for a staging install.\n' >&2
  exit 2
fi

if [[ -z "${DESTDIR}" && "$(uname -s)" != "Linux" ]]; then
  printf 'This installer supports Linux only.\n' >&2
  exit 2
fi

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ -z "${DESTDIR}" && "${ID:-}" != "ubuntu" ]]; then
    printf 'Expected Ubuntu; found %s.\n' "${ID:-unknown}" >&2
    exit 2
  fi
fi

if [[ -z "${DESTDIR}" && "${INSTALL_PACKAGES}" -eq 1 ]]; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3.12 python3.12-venv ca-certificates openssl curl logrotate rsync
fi

if [[ -z "${DESTDIR}" ]] && ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${STATE_DIR}" --create-home \
    --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

if [[ -n "${DESTDIR}" ]]; then
  mkdir -p "$(root_path "${APP_DIR}")" "$(root_path "${CONFIG_DIR}")" \
    "$(root_path "${STATE_DIR}")" "$(root_path "${LOG_DIR}")" \
    "$(root_path "${BACKUP_DIR}")" "$(root_path /etc/systemd/system)" \
    "$(root_path /etc/logrotate.d)" "$(root_path /usr/local/sbin)"
else
  install -d -m 0755 "$(root_path "${APP_DIR}")"
  install -d -m 0750 "$(root_path "${CONFIG_DIR}")"
  install -d -m 0750 "$(root_path "${STATE_DIR}")" "$(root_path "${LOG_DIR}")" \
    "$(root_path "${BACKUP_DIR}")"
  install -d -m 0755 "$(root_path /etc/systemd/system)" "$(root_path /etc/logrotate.d)"
  install -d -m 0755 "$(root_path /usr/local/sbin)"
fi

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '.env' --exclude 'artifacts/*' \
    --exclude 'logs/*' --exclude 'data' --exclude 'work' \
    "${PROJECT_DIR}/" "$(root_path "${APP_DIR}")/"
else
  target="$(root_path "${APP_DIR}")"
  for item in .github config deploy docs examples scripts src tests \
    .env.example .gitignore Makefile README.md pyproject.toml; do
    cp -a "${PROJECT_DIR}/${item}" "${target}/"
  done
  mkdir -p "${target}/artifacts" "${target}/logs"
  : >"${target}/artifacts/.gitkeep"
  : >"${target}/logs/.gitkeep"
fi

install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-shadow.service" \
  "$(root_path /etc/systemd/system/moex-tinvest-shadow.service)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-shadow.timer" \
  "$(root_path /etc/systemd/system/moex-tinvest-shadow.timer)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-health.service" \
  "$(root_path /etc/systemd/system/moex-tinvest-health.service)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-health.timer" \
  "$(root_path /etc/systemd/system/moex-tinvest-health.timer)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-bot.logrotate" \
  "$(root_path /etc/logrotate.d/moex-tinvest-bot)"
install -m 0755 "${PROJECT_DIR}/scripts/ubuntu/moex-botctl.sh" \
  "$(root_path /usr/local/sbin/moex-botctl)"

DESTDIR="${DESTDIR}" bash "${PROJECT_DIR}/scripts/ubuntu/install-ca-certificates.sh"

env_path="$(root_path "${CONFIG_DIR}/bot.env")"
if [[ ! -e "${env_path}" ]]; then
  install -m 0640 "${PROJECT_DIR}/deploy/ubuntu/bot.env.example" "${env_path}"
fi
runtime_path="$(root_path "${CONFIG_DIR}/runtime.json")"
if [[ ! -e "${runtime_path}" ]]; then
  install -m 0644 "${PROJECT_DIR}/config/runtime.json" "${runtime_path}"
fi

find "$(root_path "${APP_DIR}")/scripts/ubuntu" -type f -name '*.sh' -exec chmod 0755 {} +

if [[ -n "${DESTDIR}" ]]; then
  printf 'Staged installation at %s\n' "${DESTDIR}"
  exit 0
fi

chown -R root:root "${APP_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${STATE_DIR}" "${LOG_DIR}" "${BACKUP_DIR}"
chown root:"${SERVICE_USER}" "${CONFIG_DIR}" "${CONFIG_DIR}/bot.env" \
  "${CONFIG_DIR}/runtime.json"
chmod 0750 "${CONFIG_DIR}"
chmod 0640 "${CONFIG_DIR}/bot.env"

python3.12 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${APP_DIR}/.venv/bin/python" -m pip install "${APP_DIR}[server]"
"${APP_DIR}/.venv/bin/python" -m moex_bot.cli config-check --root "${APP_DIR}"
"${APP_DIR}/.venv/bin/python" -m moex_bot.cli preflight \
  --config "${APP_DIR}/config/shadow.json"
"${APP_DIR}/.venv/bin/python" -m moex_bot.cli integration-preflight \
  --services "${APP_DIR}/config/services.json"

systemctl daemon-reload
if [[ "${ENABLE_TIMER}" -eq 1 ]]; then
  "${APP_DIR}/scripts/ubuntu/activate.sh"
fi
printf 'Installed. Edit %s, then run scripts/ubuntu/activate.sh\n' \
  "${CONFIG_DIR}/bot.env"
