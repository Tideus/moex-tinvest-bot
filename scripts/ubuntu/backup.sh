#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/moex-tinvest-bot}"
BACKUP_DIR="${MOEX_BOT_BACKUP_DIR:-/var/backups/moex-tinvest-bot}"

# Legacy installations without production control have no manual PROD sessions.
# Once enrolled, every backup excludes the whole entry -> stop interval.
control_lock=/var/lib/moex-tinvest-control/prod-session.lock
if [[ -e "${control_lock}" || -L "${control_lock}" ]]; then
  for parent in / /var /var/lib /var/lib/moex-tinvest-control; do
    if [[ -L "${parent}" || ! -d "${parent}" || "$(stat -c %u "${parent}")" != 0 ]]; then
      printf 'FAIL: untrusted production control parent; backup not created.\n' >&2; exit 2
    fi
    mode="$(stat -c %a "${parent}")"
    if (( (8#${mode} & 0022) != 0 )); then
      printf 'FAIL: writable production control parent; backup not created.\n' >&2; exit 2
    fi
  done
  if [[ -L "${control_lock}" || ! -f "${control_lock}" || \
    "$(stat -c %u "${control_lock}")" != 0 || "$(stat -c %h "${control_lock}")" != 1 ]]; then
    printf 'FAIL: untrusted production control lock; backup not created.\n' >&2; exit 2
  fi
  mode="$(stat -c %a "${control_lock}")"
  if (( (8#${mode} & 0007) != 0 )); then
    printf 'FAIL: insecure production control lock; backup not created.\n' >&2; exit 2
  fi
  if [[ -e /proc/self/fd/9 && "${control_lock}" -ef /proc/self/fd/9 ]]; then
    # update.sh's FD9 is the SAME open file description, not an environment bypass.
    if ! flock -n 9; then
      printf 'FAIL: production session active; backup not created.\n' >&2; exit 2
    fi
  else
    exec 8<>"${control_lock}"
    if ! flock -n 8; then
      printf 'FAIL: production session active; backup not created.\n' >&2; exit 2
    fi
  fi
fi

mkdir -p "${BACKUP_DIR}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="${BACKUP_DIR}/moex-tinvest-${stamp}.tar.gz"
tar --create --gzip --file "${archive}" --directory "${STATE_DIR}" .
(cd "${BACKUP_DIR}" && sha256sum "$(basename "${archive}")") >"${archive}.sha256"
printf '%s\n' "${archive}"
