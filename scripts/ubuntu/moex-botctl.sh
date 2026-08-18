#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${MOEX_BOT_APP_DIR:-/opt/moex-tinvest-bot}"
CONFIG_DIR="${MOEX_BOT_CONFIG_DIR:-/etc/moex-tinvest-bot}"
STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/moex-tinvest-bot}"
LOG_DIR="${MOEX_BOT_LOG_DIR:-/var/log/moex-tinvest-bot}"
BACKUP_DIR="${MOEX_BOT_BACKUP_DIR:-/var/backups/moex-tinvest-bot}"
SYSTEMD_DIR="${MOEX_BOT_SYSTEMD_DIR:-/etc/systemd/system}"
SERVICE_USER="${MOEX_BOT_USER:-moexbot}"
PYTHON_BIN="${MOEX_BOT_PYTHON:-${APP_DIR}/.venv/bin/python}"
ENV_FILE="${CONFIG_DIR}/bot.env"
RUNTIME_FILE="${CONFIG_DIR}/runtime.json"
SERVICES_FILE="${APP_DIR}/config/services.json"
FAILURES=0

usage() {
  cat <<'EOF'
Usage:
  sudo moex-botctl prelaunch
  sudo moex-botctl start
  sudo moex-botctl stop
  sudo moex-botctl diagnose [--once|--watch] [--interval SECONDS]
  sudo moex-botctl status
  sudo moex-botctl portfolio
  sudo moex-botctl decisions [SHADOW_JSON]
  sudo moex-botctl contour sandbox|prod
EOF
}

heading() {
  printf '\n\033[1;36m== %s ==\033[0m\n' "$1"
}

indent() {
  sed 's/^/    /'
}

check() {
  local label="$1"
  local comment="$2"
  shift 2
  local output=""
  local status=0
  output="$("$@" 2>&1)" || status=$?
  if [[ "${status}" -eq 0 ]]; then
    printf '\033[1;32m[PASS]\033[0m %s\n' "${label}"
  else
    printf '\033[1;31m[FAIL]\033[0m %s — %s (exit=%s)\n' \
      "${label}" "${comment}" "${status}"
    FAILURES=$((FAILURES + 1))
  fi
  if [[ -n "${output}" ]]; then printf '%s\n' "${output}" | indent; fi
  return 0
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    printf 'FAIL: run with sudo.\n' >&2
    exit 2
  fi
}

load_secrets() {
  if [[ ! -r "${ENV_FILE}" ]]; then return 2; fi
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
}

as_service() {
  runuser --user "${SERVICE_USER}" --preserve-environment -- "$@"
}

selected_requirement() {
  "${PYTHON_BIN}" -c \
    'import sys; from pathlib import Path; from moex_bot.runtime_config import load_runtime_config; print("tinvest_" + load_runtime_config(Path(sys.argv[1])).environment.value)' \
    "${RUNTIME_FILE}"
}

diagnostics_interval() {
  "${PYTHON_BIN}" -c \
    'import sys; from pathlib import Path; from moex_bot.runtime_config import load_runtime_config; print(load_runtime_config(Path(sys.argv[1])).schedule.diagnostics_interval_seconds)' \
    "${RUNTIME_FILE}"
}

check_paths() {
  local non_executable=""
  test -x "${PYTHON_BIN}"
  test -r "${ENV_FILE}"
  test -r "${RUNTIME_FILE}"
  test -r "${SERVICES_FILE}"
  non_executable="$(
    find "${APP_DIR}/scripts/ubuntu" -type f -name '*.sh' ! -perm -u+x -print -quit
  )"
  [[ -z "${non_executable}" ]]
}

require_permissions() {
  local expected="$1"
  local path="$2"
  local actual=""
  actual="$(stat -c '%a:%U:%G' "${path}")"
  if [[ "${actual}" != "${expected}" ]]; then
    printf 'FAIL: %s expected=%s actual=%s\n' "${path}" "${expected}" "${actual}" >&2
    return 2
  fi
}

check_permissions() {
  require_permissions "755:root:root" "${APP_DIR}"
  require_permissions "750:root:${SERVICE_USER}" "${CONFIG_DIR}"
  require_permissions "640:root:${SERVICE_USER}" "${ENV_FILE}"
  require_permissions "644:root:${SERVICE_USER}" "${RUNTIME_FILE}"
  require_permissions "750:${SERVICE_USER}:${SERVICE_USER}" "${STATE_DIR}"
  require_permissions "750:${SERVICE_USER}:${SERVICE_USER}" "${LOG_DIR}"
  require_permissions "750:${SERVICE_USER}:${SERVICE_USER}" "${BACKUP_DIR}"
  while IFS= read -r script; do
    require_permissions "755:root:root" "${script}"
  done < <(find "${APP_DIR}/scripts/ubuntu" -type f -name '*.sh' -print)
}

prelaunch() {
  FAILURES=0
  heading "1/5 Файлы и права"
  check "Установочные файлы доступны" \
    "проверьте установку и выполните chmod 0755 для scripts/ubuntu/*.sh" check_paths
  check "Владельцы и режимы файлов безопасны" \
    "повторно выполните install/update для восстановления прав" check_permissions
  check "Секреты загружены" \
    "проверьте ${ENV_FILE}, владельца root:${SERVICE_USER} и режим 0640" load_secrets
  load_secrets || true

  heading "2/5 Конфигурация"
  check "Конфигурационные файлы валидны" \
    "исправьте JSON-файл, указанный в сообщении" \
    as_service "${PYTHON_BIN}" -m moex_bot.cli config-check --root "${APP_DIR}"
  check "Runtime, контур и расписание валидны" \
    "исправьте ${RUNTIME_FILE}" \
    as_service "${PYTHON_BIN}" -m moex_bot.cli environment-status \
      --runtime "${RUNTIME_FILE}" --services "${SERVICES_FILE}"
  check "Shadow risk-конфигурация валидна" \
    "исправьте config/shadow.json; торговые действия останутся заблокированы" \
    as_service "${PYTHON_BIN}" -m moex_bot.cli preflight \
      --config "${APP_DIR}/config/shadow.json"

  heading "3/5 Интеграции"
  local requirement="tinvest_sandbox"
  requirement="$(selected_requirement 2>/dev/null)" || true
  check "Обязательные credentials присутствуют" \
    "заполните bot.env; для sandbox сначала выполните sandbox-bootstrap" \
    as_service "${PYTHON_BIN}" -m moex_bot.cli integration-preflight \
      --services "${SERVICES_FILE}" --runtime "${RUNTIME_FILE}" \
      --require moex_algopack --require telegram \
      --require "${requirement}"

  heading "4/5 Systemd"
  check "Unit-файлы корректны" \
    "исправьте unit-файлы или повторно выполните install/update" \
    systemd-analyze verify \
      "${SYSTEMD_DIR}/moex-tinvest-shadow.service" \
      "${SYSTEMD_DIR}/moex-tinvest-shadow.timer" \
      "${SYSTEMD_DIR}/moex-tinvest-health.service" \
      "${SYSTEMD_DIR}/moex-tinvest-health.timer" \
      "${SYSTEMD_DIR}/moex-tinvest-daily-report.service" \
      "${SYSTEMD_DIR}/moex-tinvest-daily-report.timer"

  heading "5/5 Итог"
  if [[ "${FAILURES}" -eq 0 ]]; then
    printf '\033[1;32mREADY\033[0m: проверки пройдены; можно выполнить sudo moex-botctl start.\n'
    return 0
  fi
  printf '\033[1;31mBLOCKED\033[0m: ошибок: %s. Запуск не разрешён.\n' "${FAILURES}"
  return 2
}

start_bot() {
  prelaunch || return 2
  heading "Применение расписания"
  check "Systemd drop-in создан" "проверьте runtime.json и права /etc/systemd/system" \
    "${PYTHON_BIN}" -m moex_bot.cli runtime-render-systemd \
      --runtime "${RUNTIME_FILE}" --output-dir "${SYSTEMD_DIR}"
  check "Systemd перечитал конфигурацию" "выполните systemctl daemon-reload" \
    systemctl daemon-reload
  check "Применённое расписание корректно" \
    "проверьте поля schedule в runtime.json" \
    systemd-analyze verify \
      "${SYSTEMD_DIR}/moex-tinvest-shadow.timer" \
      "${SYSTEMD_DIR}/moex-tinvest-health.timer" \
      "${SYSTEMD_DIR}/moex-tinvest-daily-report.timer"
  if [[ "${FAILURES}" -ne 0 ]]; then
    printf '\033[1;31mBLOCKED\033[0m: расписание не применено; сервисы не запускались.\n'
    return 2
  fi
  check "Первый shadow-цикл завершён" \
    "посмотрите journalctl -u moex-tinvest-shadow.service" \
    systemctl start moex-tinvest-shadow.service
  if [[ "${FAILURES}" -ne 0 ]]; then
    printf '\033[1;31mBLOCKED\033[0m: первый shadow-цикл не пройден; timers не включены.\n'
    return 2
  fi
  check "Health-check завершён" \
    "посмотрите journalctl -u moex-tinvest-health.service" \
    systemctl start moex-tinvest-health.service
  if [[ "${FAILURES}" -ne 0 ]]; then
    printf '\033[1;31mBLOCKED\033[0m: health-check не пройден; timers не включены.\n'
    return 2
  fi
  check "Таймеры включены" "проверьте systemctl status таймеров" \
    systemctl enable --now moex-tinvest-shadow.timer moex-tinvest-health.timer \
      moex-tinvest-daily-report.timer
  heading "Фактическое расписание"
  systemctl list-timers 'moex-tinvest-*' --no-pager || true
  if [[ "${FAILURES}" -ne 0 ]]; then
    printf '\033[1;31mPARTIAL/FAILED\033[0m: ошибок при запуске: %s.\n' "${FAILURES}"
    return 2
  fi
  printf '\033[1;32mSTARTED\033[0m: shadow, daily-report и health timers активны; live orders отключены.\n'
}

timers_are_stopped() {
  local unit=""
  for unit in \
    moex-tinvest-shadow.timer \
    moex-tinvest-health.timer \
    moex-tinvest-daily-report.timer; do
    if systemctl is-active --quiet "${unit}"; then
      printf 'FAIL: timer remains active: %s\n' "${unit}" >&2
      return 2
    fi
    if systemctl is-enabled --quiet "${unit}"; then
      printf 'FAIL: timer remains enabled: %s\n' "${unit}" >&2
      return 2
    fi
  done
}

stop_bot() {
  FAILURES=0
  heading "Остановка расписания"
  check "Таймеры отключены" "проверьте systemctl status moex-tinvest-*.timer" \
    systemctl disable --now \
      moex-tinvest-shadow.timer \
      moex-tinvest-health.timer \
      moex-tinvest-daily-report.timer
  check "Текущие циклы остановлены" "проверьте journalctl сервисов" \
    systemctl stop \
      moex-tinvest-shadow.service \
      moex-tinvest-health.service \
      moex-tinvest-daily-report.service
  check "Автозапуск действительно выключен" \
    "один или несколько timers остались active/enabled" timers_are_stopped
  if [[ "${FAILURES}" -ne 0 ]]; then
    printf '\033[1;31mPARTIAL/FAILED\033[0m: ошибок при остановке: %s.\n' "${FAILURES}"
    return 2
  fi
  printf '\033[1;32mSTOPPED\033[0m: расписание отключено, активные циклы завершены; данные и конфиги сохранены.\n'
}

diagnose_once() {
  FAILURES=0
  local requirement="tinvest_sandbox"
  requirement="$(selected_requirement 2>/dev/null)" || true
  heading "Диагностика $(date --iso-8601=seconds)"
  check "Окружение" "проверьте runtime.json и bot.env" \
    as_service "${PYTHON_BIN}" -m moex_bot.cli environment-status \
      --runtime "${RUNTIME_FILE}" --services "${SERVICES_FILE}"
  check "Интеграции" "проверьте credentials в bot.env" \
    as_service "${PYTHON_BIN}" -m moex_bot.cli integration-preflight \
      --services "${SERVICES_FILE}" --runtime "${RUNTIME_FILE}" \
      --require moex_algopack --require telegram \
      --require "${requirement}"
  check "Shadow timer активен" "выполните sudo moex-botctl start" \
    systemctl is-active --quiet moex-tinvest-shadow.timer
  check "Health timer активен" "выполните sudo moex-botctl start" \
    systemctl is-active --quiet moex-tinvest-health.timer
  check "Daily report timer активен" "выполните sudo moex-botctl start" \
    systemctl is-active --quiet moex-tinvest-daily-report.timer
  check "Outbox Telegram здоров" "проверьте очередь и доступность Telegram" \
    as_service "${PYTHON_BIN}" -m moex_bot.cli outbox-health \
      --outbox "${STATE_DIR}/data/notifications.sqlite3" --max-pending-due 20
  check "Последний цикл свежий" \
    "проверьте journalctl сервисов и доступность MOEX/ALGOPACK" \
    as_service /usr/bin/bash "${APP_DIR}/scripts/ubuntu/healthcheck.sh"
  if [[ "${FAILURES}" -eq 0 ]]; then
    printf '\033[1;32mHEALTHY\033[0m: диагностический цикл пройден.\n'
    return 0
  fi
  printf '\033[1;31mUNHEALTHY\033[0m: ошибок: %s. Последние сообщения сервисов:\n' "${FAILURES}"
  journalctl -u moex-tinvest-shadow.service -u moex-tinvest-health.service \
    -n 30 --no-pager 2>/dev/null | indent || true
  return 2
}

diagnose() {
  local watch=0
  local interval=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --once) watch=0 ;;
      --watch) watch=1 ;;
      --interval)
        if [[ $# -lt 2 ]]; then
          printf 'FAIL: --interval requires seconds.\n' >&2
          return 2
        fi
        shift
        interval="$1"
        ;;
      *) printf 'Unknown diagnose option: %s\n' "$1" >&2; return 2 ;;
    esac
    shift
  done
  load_secrets || { printf 'FAIL: cannot load %s\n' "${ENV_FILE}" >&2; return 2; }
  if [[ -z "${interval}" ]]; then interval="$(diagnostics_interval)"; fi
  [[ "${interval}" =~ ^[1-9][0-9]*$ ]] || {
    printf 'FAIL: diagnostic interval must be integer seconds.\n' >&2
    return 2
  }
  if [[ "${watch}" -eq 0 ]]; then diagnose_once; return $?; fi
  printf 'Continuous diagnostics every %ss; Ctrl+C to stop.\n' "${interval}"
  while true; do
    diagnose_once || true
    sleep "${interval}"
  done
}

status_bot() {
  load_secrets || true
  diagnose_once || true
  systemctl list-timers 'moex-tinvest-*' --no-pager || true
}

show_decisions() {
  local input="${1:-}"
  if [[ -z "${input}" ]]; then
    input="$(
      find "${STATE_DIR}/artifacts" -maxdepth 1 -type f -name 'shadow-*.json' \
        -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2-
    )"
  fi
  if [[ -z "${input}" || ! -r "${input}" ]]; then
    printf 'FAIL: shadow artifact not found or not readable: %s\n' "${input:-latest}" >&2
    return 2
  fi
  as_service "${PYTHON_BIN}" -m moex_bot.cli shadow-decisions --input "${input}"
}

show_portfolio() {
  load_secrets || { printf 'FAIL: cannot load %s\n' "${ENV_FILE}" >&2; return 2; }
  local stamp=""
  local output=""
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  output="${STATE_DIR}/artifacts/portfolio-${stamp}.json"
  as_service mkdir -p "${STATE_DIR}/artifacts"
  as_service "${PYTHON_BIN}" -m moex_bot.cli broker-portfolio-snapshot \
    --universe "${APP_DIR}/config/universe.json" \
    --runtime "${RUNTIME_FILE}" \
    --services "${SERVICES_FILE}" \
    --output "${output}"
}

set_contour() {
  local environment="${1:-}"
  case "${environment}" in
    sandbox|prod) ;;
    *) printf 'FAIL: contour must be sandbox or prod.\n' >&2; return 2 ;;
  esac
  "${PYTHON_BIN}" -m moex_bot.cli environment-set \
    --environment "${environment}" --runtime "${RUNTIME_FILE}"
  chown root:"${SERVICE_USER}" "${RUNTIME_FILE}"
  chmod 0644 "${RUNTIME_FILE}"
  printf 'Contour saved. Run sudo moex-botctl prelaunch, then start.\n'
}

require_root
command="${1:-}"
if [[ $# -gt 0 ]]; then shift; fi
case "${command}" in
  prelaunch) prelaunch ;;
  start) start_bot ;;
  stop) stop_bot ;;
  diagnose) diagnose "$@" ;;
  status) status_bot ;;
  portfolio) show_portfolio ;;
  decisions) show_decisions "$@" ;;
  contour) set_contour "$@" ;;
  -h|--help|help) usage ;;
  *) usage >&2; exit 2 ;;
esac
