#!/usr/bin/env bash
# Isolated sandbox long-only, single-use ledger. Never use a "latest" plan.
set -Eeuo pipefail
umask 077
app=/opt/moex-tinvest-bot
base=/var/lib/moex-tinvest-bot/long-once
ledger="${base}/sandbox.sqlite3"
runtime=/etc/moex-tinvest-bot/runtime.json
python="${app}/.venv/bin/python"
source "${app}/scripts/ubuntu/lock-long-writer.sh"
lock_long_writer
cd "${app}"
if [[ ! -s "${ledger}" || -L "${ledger}" || -L "${base}" ]]; then
  printf 'FAIL: explicitly initialized sandbox ledger missing or unsafe; never recreate\n' >&2
  exit 2
fi
context() {
  "${python}" -I -B "${app}/scripts/ubuntu/long_once_context.py" \
    "${runtime}" "${app}/config/accounts.json"
}
context
status=0
"${python}" -I -B -m moex_bot.long_auto_operator status --state "${ledger}" --waiting-only || status=$?
if [[ "${status}" == 3 ]]; then exit 0; fi
if [[ "${status}" != 0 ]]; then exit "${status}"; fi
"${python}" -I -B -m moex_bot.cli session-check || status=$?
if [[ "${status}" == 3 ]]; then exit 0; fi
if [[ "${status}" != 0 ]]; then exit "${status}"; fi
run="$(mktemp -d "${base}/run-$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX")"
"${python}" -I -B -m moex_bot.cli geo-refresh --sources "${app}/config/geo_sources.json" \
  --output "${run}/geo.json"
# The explicit account-id flag is sandbox-only; the runtime parser refuses it
# in PROD. Snapshot is from this cycle and hourly-shadow binds its exact bytes.
"${python}" -I -B -m moex_bot.cli broker-portfolio-snapshot \
  --universe "${app}/config/universe.json" --runtime "${runtime}" \
  --services "${app}/config/services.json" --account-id-env T_INVEST_SANDBOX_LONG_ACCOUNT_ID \
  --output "${run}/portfolio.json"
"${python}" -I -B -m moex_bot.cli hourly-shadow \
  --config "${app}/config/shadow.json" --universe "${app}/config/universe.json" \
  --portfolio "${run}/portfolio.json" --geo "${run}/geo.json" \
  --require-token --report-environment SANDBOX --distinguish-missing-flow \
  --output "${run}/plan.json" || status=$?
# Completed no-entry decision, not a failed service. Never fall through to sandbox-cycle.
if [[ "${status}" == 4 ]]; then exit 0; fi
if [[ "${status}" != 0 ]]; then exit "${status}"; fi
context
"${python}" -I -B -m moex_bot.cli session-check
exec "${python}" -I -B -m moex_bot.long_auto_operator sandbox-cycle \
  --state "${ledger}" --plan "${run}/plan.json" --portfolio "${run}/portfolio.json" \
  --universe "${app}/config/universe.json" --policy "${app}/config/prod_long.json" \
  --strategy-config "${app}/config/shadow.json"
