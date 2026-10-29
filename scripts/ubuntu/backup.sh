#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/moex-tinvest-bot}"
BACKUP_DIR="${MOEX_BOT_BACKUP_DIR:-/var/backups/moex-tinvest-bot}"

mkdir -p "${BACKUP_DIR}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="${BACKUP_DIR}/moex-tinvest-${stamp}.tar.gz"
tar --create --gzip --file "${archive}" --directory "${STATE_DIR}" .
(cd "${BACKUP_DIR}" && sha256sum "$(basename "${archive}")") >"${archive}.sha256"
printf '%s\n' "${archive}"
