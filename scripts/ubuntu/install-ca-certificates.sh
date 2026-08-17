#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE_DIR="${PROJECT_DIR}/deploy/ubuntu/certificates"
DESTDIR="${DESTDIR:-}"

root_path() {
  local path="${1#/}"
  if [[ -n "${DESTDIR}" ]]; then
    printf '%s/%s' "${DESTDIR%/}" "${path}"
  else
    printf '/%s' "${path}"
  fi
}

if [[ -z "${DESTDIR}" && "${EUID}" -ne 0 ]]; then
  printf 'Run as root (sudo) or set DESTDIR for a staging install.\n' >&2
  exit 2
fi
for command in sha256sum install grep awk cmp openssl; do
  command -v "${command}" >/dev/null 2>&1 || {
    printf 'Required command is missing: %s\n' "${command}" >&2
    exit 2
  }
done
[[ -r "${SOURCE_DIR}/SHA256SUMS" ]] || {
  printf 'Certificate checksum manifest is missing.\n' >&2
  exit 2
}

(cd "${SOURCE_DIR}" && sha256sum --check --strict SHA256SUMS)
mapfile -t certificates < <(awk '{print $2}' "${SOURCE_DIR}/SHA256SUMS")
[[ "${#certificates[@]}" -gt 0 ]] || {
  printf 'Certificate manifest is empty.\n' >&2
  exit 2
}

target_dir="$(root_path /usr/local/share/ca-certificates)"
install -d -m 0755 "${target_dir}"
for certificate in "${certificates[@]}"; do
  [[ "${certificate}" =~ ^[a-z0-9_]+\.crt$ ]] || {
    printf 'Unsafe certificate filename: %s\n' "${certificate}" >&2
    exit 2
  }
  grep -qx -- '-----BEGIN CERTIFICATE-----' "${SOURCE_DIR}/${certificate}"
  grep -qx -- '-----END CERTIFICATE-----' "${SOURCE_DIR}/${certificate}"
  openssl x509 -in "${SOURCE_DIR}/${certificate}" -noout -checkend 0
  install -m 0644 "${SOURCE_DIR}/${certificate}" \
    "${target_dir}/moex-tinvest-${certificate}"
  cmp -s "${SOURCE_DIR}/${certificate}" \
    "${target_dir}/moex-tinvest-${certificate}"
done

if [[ -n "${DESTDIR}" ]]; then
  printf 'Staged %s verified CA certificates in %s\n' \
    "${#certificates[@]}" "${target_dir}"
  exit 0
fi
command -v update-ca-certificates >/dev/null 2>&1 || {
  printf 'update-ca-certificates is missing; install the ca-certificates package.\n' >&2
  exit 2
}
update-ca-certificates
printf 'Installed %s verified CA certificates into the Ubuntu trust store.\n' \
  "${#certificates[@]}"
