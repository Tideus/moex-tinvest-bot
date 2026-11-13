#!/usr/bin/env bash
# Deploy only the standalone read-only research observer, never trading services.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
DEST=/opt/moex-universe24-a95e285-r2
STAGE=""
if [[ $# == 2 && "$1" == --staging ]]; then
  STAGE="$2"
  [[ "${STAGE}" == /* && ! -L "${STAGE}" && "${STAGE}" != / ]] || exit 2
elif [[ $# != 0 || "${EUID}" != 0 ]]; then
  printf 'Usage: sudo install.sh OR install.sh --staging ABSOLUTE_DIR\n' >&2
  exit 2
fi
TARGET="${STAGE}${DEST}"
[[ ! -e "${TARGET}" ]] || { printf 'Immutable research package exists; no overwrite.\n' >&2; exit 2; }
install -d -m 0755 "${TARGET}" "${STAGE}/etc/systemd/system"
for file in universe24.json universe_capture.py universe_history.py universe_watch.py universe_launch.py universe_service.py; do
  install -m 0644 "${ROOT}/scripts/research/${file}" "${TARGET}/${file}"
done
install -m 0644 "${ROOT}/config/universe.json" "${TARGET}/universe.json"
printf '%s\n' 'base_revision=a95e28572fa97e4b2acf106992f91c3c5fb50461' \
  'protocol=universe24-server-shadow-v1' 'execution_allowed=false' >"${TARGET}/BUILD_INFO"
(
  cd "${TARGET}"
  sha256sum BUILD_INFO universe*.py universe*.json >BUNDLE_MANIFEST.sha256
  sha256sum --quiet -c BUNDLE_MANIFEST.sha256
)
for unit in moex-research-universe24.service moex-research-universe24-report.service moex-research-universe24-report.timer; do
  install -m 0644 "${ROOT}/deploy/research-universe24/${unit}" "${STAGE}/etc/systemd/system/${unit}"
done
if [[ -n "${STAGE}" ]]; then
  printf 'PASS: standalone staging complete: %s\n' "${TARGET}"
  exit 0
fi
chown -R root:root "${TARGET}"
for unit in moex-research-universe24.service; do
  install -d -m 0755 "/etc/systemd/system/${unit}.d"
  {
    printf '[Service]\nUnsetEnvironment='
    # Names only; values never enter files, argv or logs. Launcher also has a
    # positive allowlist, covering credentials added after this installation.
    sed -n 's/^[[:space:]]*\([A-Za-z_][A-Za-z_0-9]*\)=.*/\1/p' /etc/moex-tinvest-bot/bot.env \
      | awk '$0!="MOEX_APIKEY" && $0!="T_INVEST_READONLY_TOKEN" {printf "%s ",$0}'
    printf '\n'
  } >"/etc/systemd/system/${unit}.d/readonly.conf"
done
systemd-analyze verify /etc/systemd/system/moex-research-universe24.service \
  /etc/systemd/system/moex-research-universe24-report.service \
  /etc/systemd/system/moex-research-universe24-report.timer
systemctl daemon-reload
systemctl enable --now moex-research-universe24.service moex-research-universe24-report.timer
printf 'PASS: research-only collector and report timer enabled. Trading units untouched.\n'
