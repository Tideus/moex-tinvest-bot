#!/usr/bin/env bash
# Two dated sessions only. Run from the separate root-owned study bundle, NOT installed bot.
set -Eeuo pipefail
umask 077
root="$(pwd -P)"
[[ "${root}" =~ ^/opt/moex-pr6-shadow\.[a-zA-Z0-9]{8}$ && "$(stat -c %u "${root}")" = 0 ]]
base=moex-pr6-20261102
envfile="/run/${base}.env"
state="/var/lib/${base}"
common=(-p DynamicUser=yes -p User=moex-pr6-watch -p "StateDirectory=${base}"
        -p StateDirectoryMode=0700 -p UMask=0077 -p ProtectSystem=strict -p ProtectHome=yes
        -p PrivateTmp=yes -p PrivateDevices=yes -p NoNewPrivileges=yes -p CapabilityBoundingSet=
        -p "InaccessiblePaths=-/etc/moex-tinvest-bot -/opt/moex-tinvest-bot -/var/lib/moex-tinvest-bot -/var/lib/moex-tinvest-control -/var/backups/moex-tinvest-bot"
        -p "WorkingDirectory=${root}" -p "Environment=PYTHONPATH=${root}/src"
        -p "Environment=PYTHONDONTWRITEBYTECODE=1" -p TimeoutStartSec=270 -p MemoryMax=600M)
py="${root}/venv/bin/python"
obs="${root}/scripts/research/pr6_observe.py"
rep="${root}/scripts/research/pr6_report.py"
account=(-p "EnvironmentFile=${envfile}")
case "${1:-}" in
  prepare)
    # Select only these five values server-side; no shell evaluation or user/home environment.
    "${py}" - "${envfile}" <<'PY'
import os, re, shlex, sys
from pathlib import Path
keys = {"MOEX_APIKEY", "T_INVEST_SANDBOX_TOKEN", "T_INVEST_SANDBOX_LONG_ACCOUNT_ID",
        "T_INVEST_SANDBOX_INTRADAY_ACCOUNT_ID", "T_INVEST_SANDBOX_ACCOUNT_ID"}
values = {}
for line in Path("/etc/moex-tinvest-bot/bot.env").read_text().splitlines():
    k, _, v = line.strip().removeprefix("export ").partition("=")
    if k not in keys: continue
    parts = shlex.split(v, comments=True)
    if not parts: continue
    if k in values or len(parts)!=1 or not re.fullmatch(r"[A-Za-z0-9_.=+:/-]+", parts[0]):
        raise SystemExit("invalid or duplicate PR6 environment value")
    values[k] = parts[0]
from moex_bot.account_profiles import load_account_registry
load_account_registry(Path("config/accounts.json")).sandbox_bindings(values)
if keys - {"T_INVEST_SANDBOX_ACCOUNT_ID"} - values.keys(): raise SystemExit("missing sandbox key")
with os.fdopen(os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), "w") as f:
    for k,v in values.items(): f.write(f"{k}={v}\n")
PY
    ;;
  precheck)
    [[ "${2:-}" = long || "${2:-}" = intraday ]]
    systemd-run --quiet --wait --pipe --collect "${common[@]}" "${account[@]}" \
        "${py}" "${obs}" "${2}" --precheck
    ;;
  reports)
    systemd-run --quiet --wait --pipe --collect "${common[@]}" "${py}" "${rep}"
    ;;
  start)
    [[ -s "${envfile}" ]]
    for profile in long intraday; do
      test -s "${state}/${profile}.binding"
      # Require a successful broker read on both accounts before scheduling anything.
      "${py}" -c 'import json,pathlib,sys
p=sorted(pathlib.Path(sys.argv[1]).glob("cycle-*.json"))
assert p and "equity" in json.loads(p[-1].read_bytes())' "${state}/precheck/${profile}"
    done
    for profile in long intraday report; do
      when='10..18:05:10'
      execargs=("${account[@]}" "${py}" "${obs}" "${profile}")
      if [[ "${profile}" = intraday ]]; then when='10..18:00/5:25'; fi
      if [[ "${profile}" = report ]]; then
        when='11..19:12:00'; execargs=("${py}" "${rep}")
      fi
      systemd-run --quiet --unit "${base}-${profile}" \
          --on-calendar="2026-11-02,03 ${when} Europe/Moscow" --timer-property=AccuracySec=1s \
          "${common[@]}" "${execargs[@]}"
    done
    # The only credential copy is root-only tmpfs. Remove it after the last collection.
    systemd-run --quiet --unit "${base}-expire" \
        --on-calendar="2026-11-03 20:00:00 Europe/Moscow" --timer-property=AccuracySec=1s \
        /usr/bin/unlink "${envfile}"
    ;;
  *) exit 2 ;;
esac
