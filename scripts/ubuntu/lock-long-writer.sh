#!/usr/bin/env bash
set -Eeuo pipefail
# Sourced by both sandbox LONG writers and by deployment. The directory inode is
# root-owned, not the replaceable bot-owned SQLite file. PROD locks a different inode.
lock_long_writer() {
  local p mode dir=/var/lib/moex-tinvest-control
  for p in / /var /var/lib "${dir}"; do
    if [[ -L "${p}" || ! -d "${p}" || "$(stat -c %u "${p}")" != 0 ]]; then
      printf 'FAIL: untrusted sandbox writer lock parent\n' >&2; return 2
    fi
    mode="$(stat -c %a "${p}")"
    if (( (8#${mode} & 0022) != 0 )); then
      printf 'FAIL: writable sandbox writer lock parent\n' >&2; return 2
    fi
  done
  exec 7<"${dir}"
  if [[ ! "${dir}" -ef /proc/self/fd/7 ]] || ! flock -xn 7; then
    printf 'FAIL: sandbox long writer/deployment already active\n' >&2; return 2
  fi
}

# Never clear an existing claim or recreate a lost anchor on an enrolled installation.
ensure_long_anchor_dir() {
  local parent="$1" service_user="$2" owner_file="$3" ledger="$4"
  local dir="${parent}/long-once-sandbox"
  if [[ -L "${dir}" ]]; then return 2; fi
  if [[ ! -e "${dir}" ]]; then
    if [[ -e "${owner_file}" || -L "${owner_file}" || -e "${ledger}" || -L "${ledger}" ]]; then
      printf 'FAIL: enrolled sandbox rollback anchor directory missing; preserve evidence\n' >&2; return 2
    fi
    install -d -o "${service_user}" -g "${service_user}" -m 0700 "${dir}"
  fi
  if [[ ! -d "${dir}" || "$(stat -c %u "${dir}")" != "$(id -u "${service_user}")" || \
        "$(stat -c %a "${dir}")" != 700 ]]; then
    printf 'FAIL: unsafe sandbox rollback anchor directory\n' >&2; return 2
  fi
}
