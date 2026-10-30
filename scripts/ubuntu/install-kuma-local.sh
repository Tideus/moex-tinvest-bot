#!/usr/bin/env bash
# Independent monitoring stack; does not touch bot services or nginx.
set -Eeuo pipefail
if [[ "${EUID}" -ne 0 ]]; then printf 'Run as root.\n' >&2; exit 2; fi
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLATE="${1:-${PROJECT_DIR}/deploy/monitoring/compose.yaml}"
APP_DIR=/opt/moex-monitoring
IMAGE=louislam/uptime-kuma:2.5.8
if [[ -e "${APP_DIR}/compose.yaml" || -e "${APP_DIR}/data" ]]; then
  printf 'Existing monitoring installation found; refusing to overwrite data.\n' >&2
  exit 2
fi
if ss -ltn | awk '{print $4}' | grep -qE ':3001$'; then
  printf 'Port 3001 is already occupied.\n' >&2; exit 2
fi
docker compose version
docker pull "${IMAGE}"
DIGEST="$(docker image inspect --format '{{index .RepoDigests 0}}' "${IMAGE}")"
if [[ "${DIGEST}" != louislam/uptime-kuma@sha256:* ]]; then
  printf 'Missing immutable image digest; aborting.\n' >&2; exit 2
fi
install -d -m 0750 "${APP_DIR}" "${APP_DIR}/data"
install -m 0644 "${TEMPLATE}" "${APP_DIR}/compose.yaml"
umask 077
printf 'KUMA_IMAGE=%s\n' "${DIGEST}" >"${APP_DIR}/.env"
cd "${APP_DIR}"
docker compose config --quiet
docker compose up -d
printf 'Installed %s at localhost:3001. Complete setup using SSH tunnel.\n' "${DIGEST}"
