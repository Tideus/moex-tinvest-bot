#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAGE_DIR="${MOEX_BOT_TEST_STAGE:-work/ubuntu-deployment-test}"

STAGE_DIR="$(realpath -m -- "${STAGE_DIR}")"
if [[ "${STAGE_DIR}" != "${PROJECT_DIR}/work/"* ]]; then
  printf 'FAIL: deployment test stage must resolve below %s/work/\n' "${PROJECT_DIR}" >&2
  exit 2
fi
rm -rf -- "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}"

for script in "${PROJECT_DIR}"/scripts/ubuntu/*.sh; do
  bash -n "${script}"
done
DESTDIR="${STAGE_DIR}" bash "${PROJECT_DIR}/scripts/ubuntu/install.sh" \
  --no-packages

required=(
  "opt/moex-tinvest-bot/pyproject.toml"
  "opt/moex-tinvest-bot/config/geo_sources.json"
  "opt/moex-tinvest-bot/config/accounts.json"
  "opt/moex-tinvest-bot/config/intraday.json"
  "opt/moex-tinvest-bot/config/maintenance.json"
  "opt/moex-tinvest-bot/config/prod_long.json"
  "opt/moex-tinvest-bot/config/prod_live.json"
  "opt/moex-tinvest-bot/config/prod_experiment.json"
  "opt/moex-tinvest-bot/config/prod_runtime.json"
  "opt/moex-tinvest-bot/config/prod_shadow.json"
  "opt/moex-tinvest-bot/config/ownership_disclosures.json"
  "opt/moex-tinvest-bot/config/replay.json"
  "opt/moex-tinvest-bot/config/runtime.json"
  "opt/moex-tinvest-bot/config/services.json"
  "opt/moex-tinvest-bot/config/shadow.json"
  "opt/moex-tinvest-bot/config/universe.json"
  "opt/moex-tinvest-bot/scripts/ubuntu/run-shadow-cycle.sh"
  "opt/moex-tinvest-bot/scripts/ubuntu/run-intraday-cycle.sh"
  "opt/moex-tinvest-bot/scripts/ubuntu/run-prod-long-observer.sh"
  "opt/moex-tinvest-bot/scripts/ubuntu/run-daily-report.sh"
  "opt/moex-tinvest-bot/scripts/ubuntu/activate.sh"
  "opt/moex-tinvest-bot/scripts/ubuntu/install-ca-certificates.sh"
  "opt/moex-tinvest-bot/deploy/ubuntu/certificates/SHA256SUMS"
  "etc/moex-tinvest-bot/bot.env"
  "etc/moex-tinvest-bot/runtime.json"
  "etc/moex-tinvest-bot/maintenance.json"
  "etc/moex-tinvest-bot/prod-live.json"
  "etc/moex-tinvest-bot/prod-experiment.json"
  "etc/moex-tinvest-bot/prod-long.json"
  "etc/systemd/system/moex-tinvest-shadow.service"
  "etc/systemd/system/moex-tinvest-shadow.timer"
  "etc/systemd/system/moex-tinvest-health.service"
  "etc/systemd/system/moex-tinvest-health.timer"
  "etc/systemd/system/moex-tinvest-daily-report.service"
  "etc/systemd/system/moex-tinvest-daily-report.timer"
  "etc/systemd/system/moex-tinvest-intraday.service"
  "etc/systemd/system/moex-tinvest-intraday.timer"
  "etc/systemd/system/moex-tinvest-maintenance.service"
  "etc/systemd/system/moex-tinvest-maintenance.timer"
  "etc/systemd/system/moex-tinvest-prod-long-observer.service"
  "etc/systemd/system/moex-tinvest-prod-long-observer.timer"
  "etc/logrotate.d/moex-tinvest-bot"
  "usr/local/sbin/moex-botctl"
  "usr/local/share/ca-certificates/moex-tinvest-russian_trusted_root_ca.crt"
  "usr/local/share/ca-certificates/moex-tinvest-russian_trusted_root_ca_gost_2025.crt"
  "usr/local/share/ca-certificates/moex-tinvest-russian_trusted_sub_ca.crt"
  "usr/local/share/ca-certificates/moex-tinvest-russian_trusted_sub_ca_2024.crt"
  "usr/local/share/ca-certificates/moex-tinvest-russian_trusted_sub_ca_gost_2025.crt"
)
for path in "${required[@]}"; do
  [[ -e "${STAGE_DIR}/${path}" ]] || {
    printf 'FAIL: staged path missing: %s\n' "${path}" >&2
    exit 2
  }
done

for script in "${STAGE_DIR}"/opt/moex-tinvest-bot/scripts/ubuntu/*.sh; do
  [[ -x "${script}" ]] || {
    printf 'FAIL: staged script is not executable: %s\n' "${script}" >&2
    exit 2
  }
done
[[ -x "${STAGE_DIR}/usr/local/sbin/moex-botctl" ]] || {
  printf 'FAIL: staged moex-botctl is not executable\n' >&2
  exit 2
}
for config in "${PROJECT_DIR}"/config/*.json; do
  cmp "${config}" "${STAGE_DIR}/opt/moex-tinvest-bot/config/$(basename "${config}")"
done
cmp "${PROJECT_DIR}/config/runtime.json" \
  "${STAGE_DIR}/etc/moex-tinvest-bot/runtime.json"
cmp "${PROJECT_DIR}/config/maintenance.json" \
  "${STAGE_DIR}/etc/moex-tinvest-bot/maintenance.json"

(cd "${PROJECT_DIR}/deploy/ubuntu/certificates" && \
  sha256sum --check --strict SHA256SUMS)
grep -q 'install-ca-certificates.sh' "${PROJECT_DIR}/scripts/ubuntu/install.sh"
grep -q 'install-ca-certificates.sh' "${PROJECT_DIR}/scripts/ubuntu/update.sh"

grep -q '^User=moexbot$' \
  "${STAGE_DIR}/etc/systemd/system/moex-tinvest-shadow.service"
grep -q '^OnCalendar=\*-\*-\* \*:05:00 Europe/Moscow$' \
  "${STAGE_DIR}/etc/systemd/system/moex-tinvest-shadow.timer"
grep -q '^Persistent=true$' \
  "${STAGE_DIR}/etc/systemd/system/moex-tinvest-shadow.timer"
if grep -Ev '^(#.*|[A-Z0-9_]+=$|[[:space:]]*)$' \
  "${STAGE_DIR}/etc/moex-tinvest-bot/bot.env"; then
  printf 'FAIL: staged env template contains a value\n' >&2
  exit 2
fi

RUNTIME_DIR="work/ubuntu-runtime-smoke"
rm -rf "${RUNTIME_DIR}"
mkdir -p "${RUNTIME_DIR}/state" "${RUNTIME_DIR}/logs"
FAKE_PYTHON="${RUNTIME_DIR}/fake-python"
cat >"${FAKE_PYTHON}" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
output=""
previous=""
for argument in "$@"; do
  if [[ "${previous}" == "--output" ]]; then output="${argument}"; fi
  previous="${argument}"
done
if [[ -n "${output}" ]]; then
  mkdir -p "$(dirname "${output}")"
  printf '{"smoke":true}\n' >"${output}"
fi
printf 'fake-python %s\n' "$*"
exit 0
EOF
chmod 0755 "${FAKE_PYTHON}"
mkdir -p "${RUNTIME_DIR}/bin"
cat >"${RUNTIME_DIR}/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "is-failed" ]]; then exit 1; fi
exit 0
EOF
chmod 0755 "${RUNTIME_DIR}/bin/systemctl"

MOEX_BOT_APP_DIR="${PROJECT_DIR}" \
MOEX_BOT_STATE_DIR="${RUNTIME_DIR}/state" \
MOEX_BOT_LOG_DIR="${RUNTIME_DIR}/logs" \
MOEX_BOT_PYTHON="${FAKE_PYTHON}" \
  bash "${PROJECT_DIR}/scripts/ubuntu/run-shadow-cycle.sh"

find "${RUNTIME_DIR}/state/artifacts" -name 'shadow-*.json' -print -quit | grep -q .
MOEX_BOT_APP_DIR="${PROJECT_DIR}" \
MOEX_BOT_STATE_DIR="${RUNTIME_DIR}/state" \
MOEX_BOT_LOG_DIR="${RUNTIME_DIR}/logs" \
MOEX_BOT_PYTHON="${FAKE_PYTHON}" \
  bash "${PROJECT_DIR}/scripts/ubuntu/run-prod-long-observer.sh"
find "${RUNTIME_DIR}/state/artifacts" -name 'prod-long-plan-*.json' -print -quit \
  | grep -q .
MOEX_BOT_APP_DIR="${PROJECT_DIR}" \
MOEX_BOT_STATE_DIR="${RUNTIME_DIR}/state" \
MOEX_BOT_LOG_DIR="${RUNTIME_DIR}/logs" \
MOEX_BOT_PYTHON="${FAKE_PYTHON}" \
MOEX_BOT_REPORT_DATE="2026-08-18" \
  bash "${PROJECT_DIR}/scripts/ubuntu/run-daily-report.sh"
find "${RUNTIME_DIR}/state/artifacts" -name 'daily-performance-*.txt' -print -quit | grep -q .
MOEX_BOT_APP_DIR="${PROJECT_DIR}" \
MOEX_BOT_CONFIG_DIR="${STAGE_DIR}/etc/moex-tinvest-bot" \
MOEX_BOT_STATE_DIR="${RUNTIME_DIR}/state" \
MOEX_BOT_PYTHON="${FAKE_PYTHON}" \
PATH="${RUNTIME_DIR}/bin:${PATH}" \
  bash "${PROJECT_DIR}/scripts/ubuntu/healthcheck.sh"

MOEX_BOT_STATE_DIR="${RUNTIME_DIR}/state" \
MOEX_BOT_BACKUP_DIR="${RUNTIME_DIR}/backups" \
  bash "${PROJECT_DIR}/scripts/ubuntu/backup.sh" >/dev/null
checksum="$(find "${RUNTIME_DIR}/backups" -name '*.sha256' -print -quit)"
[[ -n "${checksum}" ]]
(cd "$(dirname "${checksum}")" && sha256sum -c "$(basename "${checksum}")") >/dev/null

printf 'history-before-reset\n' >"${RUNTIME_DIR}/state/data/history-marker.txt"
printf 'log-before-reset\n' >"${RUNTIME_DIR}/logs/history-marker.log"
MOEX_BOT_ALLOW_NON_ROOT_TEST=1 \
MOEX_BOT_APP_DIR="${PROJECT_DIR}" \
MOEX_BOT_CONFIG_DIR="${STAGE_DIR}/etc/moex-tinvest-bot" \
MOEX_BOT_STATE_DIR="${RUNTIME_DIR}/state" \
MOEX_BOT_LOG_DIR="${RUNTIME_DIR}/logs" \
MOEX_BOT_BACKUP_DIR="${RUNTIME_DIR}/reset-backups" \
MOEX_BOT_PYTHON="${FAKE_PYTHON}" \
PATH="${RUNTIME_DIR}/bin:${PATH}" \
  bash "${PROJECT_DIR}/scripts/ubuntu/reset-history.sh" --confirm-reset-history
[[ ! -e "${RUNTIME_DIR}/state/data/history-marker.txt" ]]
[[ ! -e "${RUNTIME_DIR}/logs/history-marker.log" ]]
[[ -d "${RUNTIME_DIR}/state/artifacts" && -d "${RUNTIME_DIR}/state/data" ]]
reset_archive="$(find "${RUNTIME_DIR}/reset-backups" -name 'moex-history-reset-*.tar.gz' -print -quit)"
[[ -n "${reset_archive}" && -f "${reset_archive}.sha256" ]]
(cd "$(dirname "${reset_archive}")" && sha256sum -c "$(basename "${reset_archive}.sha256")") \
  >/dev/null
tar -tzf "${reset_archive}" | grep -q 'state/data/history-marker.txt'
tar -tzf "${reset_archive}" | grep -q 'logs/history-marker.log'

printf 'PASS: Ubuntu deployment staging, runner, health and backup contracts\n'
