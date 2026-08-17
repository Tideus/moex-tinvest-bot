#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAGE_DIR="${MOEX_BOT_TEST_STAGE:-work/ubuntu-deployment-test}"

rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}"

bash -n "${PROJECT_DIR}"/scripts/ubuntu/*.sh
DESTDIR="${STAGE_DIR}" bash "${PROJECT_DIR}/scripts/ubuntu/install.sh" \
  --no-packages

required=(
  "opt/moex-tinvest-bot/pyproject.toml"
  "opt/moex-tinvest-bot/scripts/ubuntu/run-shadow-cycle.sh"
  "opt/moex-tinvest-bot/scripts/ubuntu/activate.sh"
  "etc/moex-tinvest-bot/bot.env"
  "etc/moex-tinvest-bot/runtime.json"
  "etc/systemd/system/moex-tinvest-shadow.service"
  "etc/systemd/system/moex-tinvest-shadow.timer"
  "etc/systemd/system/moex-tinvest-health.service"
  "etc/systemd/system/moex-tinvest-health.timer"
  "etc/logrotate.d/moex-tinvest-bot"
)
for path in "${required[@]}"; do
  [[ -e "${STAGE_DIR}/${path}" ]] || {
    printf 'FAIL: staged path missing: %s\n' "${path}" >&2
    exit 2
  }
done

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

MOEX_BOT_APP_DIR="${PROJECT_DIR}" \
MOEX_BOT_STATE_DIR="${RUNTIME_DIR}/state" \
MOEX_BOT_LOG_DIR="${RUNTIME_DIR}/logs" \
MOEX_BOT_PYTHON="${FAKE_PYTHON}" \
  bash "${PROJECT_DIR}/scripts/ubuntu/run-shadow-cycle.sh"

find "${RUNTIME_DIR}/state/artifacts" -name 'shadow-*.json' -print -quit | grep -q .
MOEX_BOT_APP_DIR="${PROJECT_DIR}" \
MOEX_BOT_CONFIG_DIR="${STAGE_DIR}/etc/moex-tinvest-bot" \
MOEX_BOT_STATE_DIR="${RUNTIME_DIR}/state" \
MOEX_BOT_PYTHON="${FAKE_PYTHON}" \
  bash "${PROJECT_DIR}/scripts/ubuntu/healthcheck.sh"

MOEX_BOT_STATE_DIR="${RUNTIME_DIR}/state" \
MOEX_BOT_BACKUP_DIR="${RUNTIME_DIR}/backups" \
  bash "${PROJECT_DIR}/scripts/ubuntu/backup.sh" >/dev/null
checksum="$(find "${RUNTIME_DIR}/backups" -name '*.sha256' -print -quit)"
[[ -n "${checksum}" ]]
(cd "$(dirname "${checksum}")" && sha256sum -c "$(basename "${checksum}")") >/dev/null

printf 'PASS: Ubuntu deployment staging, runner, health and backup contracts\n'
