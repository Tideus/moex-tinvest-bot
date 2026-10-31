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

# Existing installations must exclude PROD BEFORE apt/useradd or any app mutation.
# A fresh install without a control inode cannot have an active production_session.
if [[ -z "${DESTDIR}" ]]; then
  early_control_dir=/var/lib/moex-tinvest-control
  early_control_lock="${early_control_dir}/prod-session.lock"
  for parent in / /var /var/lib "${early_control_dir}"; do
    if [[ -e "${parent}" || -L "${parent}" ]]; then
      if [[ -L "${parent}" || ! -d "${parent}" || "$(stat -c %u "${parent}")" != 0 ]]; then
        printf 'FAIL: untrusted production control parent before installation.\n' >&2; exit 2
      fi
      mode="$(stat -c %a "${parent}")"
      if (( (8#${mode} & 0022) != 0 )); then
        printf 'FAIL: writable production control parent before installation.\n' >&2; exit 2
      fi
    fi
  done
  if [[ -e "${early_control_lock}" || -L "${early_control_lock}" ]]; then
    if [[ -L "${early_control_lock}" || ! -f "${early_control_lock}" || \
      "$(stat -c %u "${early_control_lock}")" != 0 || \
      "$(stat -c %h "${early_control_lock}")" != 1 ]]; then
      printf 'FAIL: untrusted production lock before installation.\n' >&2; exit 2
    fi
    mode="$(stat -c %a "${early_control_lock}")"
    if (( (8#${mode} & 0007) != 0 )); then
      printf 'FAIL: insecure production lock before installation.\n' >&2; exit 2
    fi
    exec 9<>"${early_control_lock}"
    if ! flock -n 9; then
      printf 'FAIL: production session active; installation made no changes.\n' >&2; exit 2
    fi
  fi
fi

if [[ -z "${DESTDIR}" && "${INSTALL_PACKAGES}" -eq 1 ]]; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3.12 python3.12-venv ca-certificates openssl curl logrotate rsync util-linux
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

# Keep the control inode outside all bot-writable state directories.
control_dir="$(root_path /var/lib/moex-tinvest-control)"
control_lock="${control_dir}/prod-session.lock"
if [[ -L "${control_dir}" || -L "${control_lock}" ]]; then
  printf 'FAIL: symlinked production control path.\n' >&2; exit 2
fi
if [[ -n "${DESTDIR}" ]]; then
  mkdir -p "${control_dir}"
  chmod 0750 "${control_dir}"
else
  for parent in / /var /var/lib; do
    mode="$(stat -c %a "${parent}")"
    if [[ -L "${parent}" || ! -d "${parent}" || "$(stat -c %u "${parent}")" != 0 ]] \
      || (( (8#${mode} & 0022) != 0 )); then
      printf 'FAIL: untrusted production control parent.\n' >&2; exit 2
    fi
  done
  if [[ -e "${control_dir}" && ( ! -d "${control_dir}" || \
    "$(stat -c %u "${control_dir}")" != 0 ) ]]; then
    printf 'FAIL: untrusted production control directory.\n' >&2; exit 2
  fi
  if [[ -d "${control_dir}" ]]; then
    mode="$(stat -c %a "${control_dir}")"
    if (( (8#${mode} & 0022) != 0 )); then
      printf 'FAIL: writable production control directory.\n' >&2; exit 2
    fi
  fi
  install -d -o root -g "${SERVICE_USER}" -m 0750 "${control_dir}"
fi
if [[ -e "${control_lock}" && ( ! -f "${control_lock}" || \
  "$(stat -c %h "${control_lock}")" != 1 ) ]]; then
  printf 'FAIL: production control lock is not a single regular file.\n' >&2; exit 2
fi
if [[ -z "${DESTDIR}" && -e "${control_lock}" && \
  "$(stat -c %u "${control_lock}")" != 0 ]]; then
  printf 'FAIL: production control lock is not root-owned.\n' >&2; exit 2
fi
if [[ ! -e "${control_lock}" ]]; then
  (umask 007; set -o noclobber; : >"${control_lock}")
fi
chmod 0660 "${control_lock}"
if [[ -z "${DESTDIR}" ]]; then
  chown root:"${SERVICE_USER}" "${control_lock}"
  if [[ ! -e /proc/self/fd/9 || ! "${control_lock}" -ef /proc/self/fd/9 ]]; then
    exec 9<>"${control_lock}"
  fi
  if ! flock -n 9; then
    printf 'FAIL: production session is active; installation blocked.\n' >&2; exit 2
  fi
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

allow_dirty=0
if [[ -n "${DESTDIR}" ]]; then allow_dirty=1; fi
bash "${PROJECT_DIR}/scripts/ubuntu/write-build-metadata.sh" \
  "${PROJECT_DIR}" "$(root_path "${APP_DIR}")" "${allow_dirty}"

install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-calendar.service" \
  "$(root_path /etc/systemd/system/moex-tinvest-calendar.service)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-calendar.timer" \
  "$(root_path /etc/systemd/system/moex-tinvest-calendar.timer)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-shadow.service" \
  "$(root_path /etc/systemd/system/moex-tinvest-shadow.service)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-shadow.timer" \
  "$(root_path /etc/systemd/system/moex-tinvest-shadow.timer)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-prod-long-observer.service" \
  "$(root_path /etc/systemd/system/moex-tinvest-prod-long-observer.service)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-prod-long-observer.timer" \
  "$(root_path /etc/systemd/system/moex-tinvest-prod-long-observer.timer)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-health.service" \
  "$(root_path /etc/systemd/system/moex-tinvest-health.service)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-health.timer" \
  "$(root_path /etc/systemd/system/moex-tinvest-health.timer)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-daily-report.service" \
  "$(root_path /etc/systemd/system/moex-tinvest-daily-report.service)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-daily-report.timer" \
  "$(root_path /etc/systemd/system/moex-tinvest-daily-report.timer)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-intraday.service" \
  "$(root_path /etc/systemd/system/moex-tinvest-intraday.service)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-intraday.timer" \
  "$(root_path /etc/systemd/system/moex-tinvest-intraday.timer)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-maintenance.service" \
  "$(root_path /etc/systemd/system/moex-tinvest-maintenance.service)"
install -m 0644 "${PROJECT_DIR}/deploy/ubuntu/moex-tinvest-maintenance.timer" \
  "$(root_path /etc/systemd/system/moex-tinvest-maintenance.timer)"
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
maintenance_path="$(root_path "${CONFIG_DIR}/maintenance.json")"
if [[ ! -e "${maintenance_path}" ]]; then
  install -m 0644 "${PROJECT_DIR}/config/maintenance.json" "${maintenance_path}"
fi

for name in prod_live prod_long prod_experiment; do
  target="$(root_path "${CONFIG_DIR}/${name//_/-}.json")"
  if [[ ! -e "${target}" ]]; then
    install -m 0640 "${PROJECT_DIR}/config/${name}.json" "${target}"
  fi
done

find "$(root_path "${APP_DIR}")/scripts/ubuntu" -type f -name '*.sh' -exec chmod 0755 {} +

if [[ -n "${DESTDIR}" ]]; then
  printf 'Staged installation at %s\n' "${DESTDIR}"
  exit 0
fi

chown -R root:root "${APP_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${STATE_DIR}" "${LOG_DIR}" "${BACKUP_DIR}"
chown root:"${SERVICE_USER}" "${CONFIG_DIR}" "${CONFIG_DIR}/bot.env" \
  "${CONFIG_DIR}/runtime.json" "${CONFIG_DIR}/maintenance.json"
chmod 0750 "${CONFIG_DIR}"
chmod 0640 "${CONFIG_DIR}/bot.env"
chmod 0644 "${CONFIG_DIR}/maintenance.json"
chown root:"${SERVICE_USER}" "${CONFIG_DIR}/prod-live.json" "${CONFIG_DIR}/prod-long.json" \
  "${CONFIG_DIR}/prod-experiment.json"
chmod 0640 "${CONFIG_DIR}/prod-live.json" "${CONFIG_DIR}/prod-long.json" \
  "${CONFIG_DIR}/prod-experiment.json"

python3.12 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${APP_DIR}/.venv/bin/python" -m pip install "${APP_DIR}[server]"
(cd "${APP_DIR}" && sha256sum --quiet -c BUILD_MANIFEST.sha256)
installed_version="$("${APP_DIR}/.venv/bin/python" -c \
  'from moex_bot import __version__; print(__version__)')"
expected_version="$(sed -n 's/^app_version=//p' "${APP_DIR}/BUILD_INFO")"
if [[ "${installed_version}" != "${expected_version}" ]]; then
  printf 'Installed package version mismatch: expected=%s actual=%s\n' \
    "${expected_version}" "${installed_version}" >&2
  exit 2
fi
"${APP_DIR}/.venv/bin/python" -m moex_bot.cli runtime-normalize \
  --runtime "${CONFIG_DIR}/runtime.json"
chown root:"${SERVICE_USER}" "${CONFIG_DIR}/runtime.json"
chmod 0644 "${CONFIG_DIR}/runtime.json"
"${APP_DIR}/.venv/bin/python" -m moex_bot.cli config-check --root "${APP_DIR}"
"${APP_DIR}/.venv/bin/python" -m moex_bot.cli preflight \
  --config "${APP_DIR}/config/shadow.json"
"${APP_DIR}/.venv/bin/python" -m moex_bot.cli integration-preflight \
  --services "${APP_DIR}/config/services.json" \
  --runtime "${CONFIG_DIR}/runtime.json"

systemctl daemon-reload
if [[ "${ENABLE_TIMER}" -eq 1 ]]; then
  "${APP_DIR}/scripts/ubuntu/activate.sh"
fi
printf 'Installed. Edit %s, then run scripts/ubuntu/activate.sh\n' \
  "${CONFIG_DIR}/bot.env"
