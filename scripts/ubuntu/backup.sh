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
  # The independently root-owned directory inode excludes ALL LONG mutations,
  # including a manual stop recovery. Reuse an updater's open file description.
  long_dir="${control_lock%/*}"
  if [[ ! -e /proc/self/fd/7 || ! "${long_dir}" -ef /proc/self/fd/7 ]]; then
    exec 7<"${long_dir}"
  fi
  if ! flock -xn 7; then
    printf 'FAIL: sandbox long writer active; backup not created.\n' >&2; exit 2
  fi
elif [[ -e "${STATE_DIR}/long-once" || -L "${STATE_DIR}/long-once" || \
        -e /etc/moex-tinvest-bot/sandbox-long-once.owner || -L /etc/moex-tinvest-bot/sandbox-long-once.owner || \
        -e "${control_lock%/*}" || -L "${control_lock%/*}" ]]; then
  printf 'FAIL: missing trusted production lock; no safe coordinated backup.\n' >&2; exit 2
fi

BACKUP_DIR="$(realpath -m -s -- "${BACKUP_DIR}")"
mkdir -p "${BACKUP_DIR}"
if [[ -L "${BACKUP_DIR}" || "$(realpath -e "${BACKUP_DIR}")" != "${BACKUP_DIR}" ]]; then
  printf 'FAIL: backup directory must not contain symlinks.\n' >&2; exit 2
fi
if [[ "${EUID}" -eq 0 ]]; then
  chown root:"${MOEX_BOT_USER:-moexbot}" "${BACKUP_DIR}"
  chmod 0750 "${BACKUP_DIR}"
fi
stage_dir="$(mktemp -d "${BACKUP_DIR}/.snapshot.XXXXXXXX")"
trap '[[ "${stage_dir}" == "${BACKUP_DIR}/.snapshot."* && -d "${stage_dir}" ]] && rm -rf -- "${stage_dir}"' EXIT
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${MOEX_BOT_PYTHON:-python3}" "${script_dir}/snapshot_state.py" "${STATE_DIR}" "${stage_dir}/state"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="${BACKUP_DIR}/moex-tinvest-${stamp}.tar.gz"
[[ ! -e "${archive}" ]] || { printf 'FAIL: backup name already exists.\n' >&2; exit 2; }
tar --create --gzip --file "${stage_dir}/state.tar.gz" --directory "${stage_dir}/state" .
# Independent anti-replay fences must travel WITH, not inside, a resettable state DB.
# Preserve all control evidence, but never auto-extract/re-enroll from this sidecar.
if [[ -d /var/lib/moex-tinvest-control/prod-long-once ]]; then
  control_archive="${archive%.tar.gz}.prod-control.tar.gz"
  tar --create --gzip --file "${control_archive}" \
    --directory /var/lib/moex-tinvest-control prod-long-once
  (cd "${BACKUP_DIR}" && sha256sum "$(basename "${control_archive}")") >"${control_archive}.sha256"
fi
if [[ -d /var/lib/moex-tinvest-control/long-once-sandbox ]]; then
  sandbox_control_archive="${archive%.tar.gz}.sandbox-control.tar.gz"
  sandbox_control_items=(long-once-sandbox)
  if [[ -e /var/lib/moex-tinvest-control/sandbox-long-retry-2.json ]]; then
    sandbox_control_items+=(sandbox-long-retry-2.json)
  fi
  tar --create --gzip --file "${sandbox_control_archive}" \
    --directory /var/lib/moex-tinvest-control "${sandbox_control_items[@]}"
  (cd "${BACKUP_DIR}" && sha256sum "$(basename "${sandbox_control_archive}")") \
    >"${sandbox_control_archive}.sha256"
fi
mv -- "${stage_dir}/state.tar.gz" "${archive}"
(cd "${BACKUP_DIR}" && sha256sum "$(basename "${archive}")") >"${stage_dir}/checksum"
mv -- "${stage_dir}/checksum" "${archive}.sha256"
printf '%s\n' "${archive}"
