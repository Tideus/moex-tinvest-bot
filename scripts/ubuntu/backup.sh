#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

STATE_DIR="${MOEX_BOT_STATE_DIR:-/var/lib/moex-tinvest-bot}"
BACKUP_DIR="${MOEX_BOT_BACKUP_DIR:-/var/backups/moex-tinvest-bot}"
RETENTION_DAYS="${MOEX_BOT_BACKUP_RETENTION_DAYS:-30}"

mkdir -p "${BACKUP_DIR}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="${BACKUP_DIR}/moex-tinvest-${stamp}.tar.gz"
tar --create --gzip --file "${archive}" --directory "${STATE_DIR}" .
(cd "${BACKUP_DIR}" && sha256sum "$(basename "${archive}")") >"${archive}.sha256"
find "${BACKUP_DIR}" -maxdepth 1 -type f \
  \( -name 'moex-tinvest-*.tar.gz' -o -name 'moex-tinvest-*.tar.gz.sha256' \) \
  -mtime "+${RETENTION_DAYS}" -delete
printf '%s\n' "${archive}"
