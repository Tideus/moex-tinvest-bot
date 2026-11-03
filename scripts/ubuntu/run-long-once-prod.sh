#!/usr/bin/env bash
# Operator-enrolled single attempt. No account creation or automatic admission.
set -Eeuo pipefail
umask 077
app=/opt/moex-tinvest-bot
base=/var/lib/moex-tinvest-bot/prod-long-once
python="${app}/.venv/bin/python"
cd "${app}"
status=0
"${python}" -I -B -m moex_bot.prod_long_once_operator status || status=$?
if [[ "${status}" == 3 ]]; then exit 0; fi
if [[ "${status}" != 0 ]]; then exit "${status}"; fi
"${python}" -I -B -m moex_bot.cli session-check || status=$?
if [[ "${status}" == 3 ]]; then exit 0; fi
if [[ "${status}" != 0 ]]; then exit "${status}"; fi
# Must be explicitly created by operator deployment, not silently recreated here.
[[ -d "${base}" && ! -L "${base}" ]] || exit 2
run="$(mktemp -d "${base}/run-$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX")"
"${python}" -I -B -m moex_bot.cli geo-refresh \
  --sources "${app}/config/geo_sources.json" --output "${run}/geo.json"
"${python}" -I -B -m moex_bot.cli broker-portfolio-snapshot \
  --universe "${app}/config/universe.json" --runtime "${app}/config/prod_runtime.json" \
  --services "${app}/config/services.json" --output "${run}/portfolio.json"
"${python}" -I -B -m moex_bot.cli prod-long-prepare \
  --policy "${app}/config/prod_long.json" --broker-snapshot "${run}/portfolio.json" \
  --output "${run}/sleeve.json"
"${python}" -I -B -m moex_bot.cli hourly-shadow \
  --config "${app}/config/prod_shadow.json" --universe "${app}/config/universe.json" \
  --portfolio "${run}/sleeve.json" --geo "${run}/geo.json" --require-token \
  --report-environment "PROD ONE-SHOT PLAN" --output "${run}/plan.json" \
  --allowed-secids-policy "${app}/config/prod_long.json"
"${python}" -I -B -m moex_bot.cli session-check
exec "${python}" -I -B -m moex_bot.prod_long_once_operator run \
  --plan "${run}/plan.json" --portfolio "${run}/sleeve.json"
