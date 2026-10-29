#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

SOURCE_DIR="${1:?source checkout is required}"
TARGET_DIR="${2:?deployment target is required}"
ALLOW_DIRTY="${3:-0}"

git -C "${SOURCE_DIR}" rev-parse --is-inside-work-tree >/dev/null
revision="$(git -C "${SOURCE_DIR}" rev-parse HEAD)"
branch="$(git -C "${SOURCE_DIR}" symbolic-ref --quiet --short HEAD || printf detached)"
tree_state="clean"
if ! git -C "${SOURCE_DIR}" diff --quiet \
  || ! git -C "${SOURCE_DIR}" diff --cached --quiet \
  || [[ -n "$(git -C "${SOURCE_DIR}" ls-files --others --exclude-standard)" ]]; then
  tree_state="dirty"
fi
if [[ "${tree_state}" == "dirty" && "${ALLOW_DIRTY}" != "1" ]]; then
  printf 'FAIL: refusing to create production build metadata from a dirty checkout\n' >&2
  exit 2
fi

app_version="$(sed -n 's/^version = "\([^"]*\)"/\1/p' "${SOURCE_DIR}/pyproject.toml" | head -n 1)"
if [[ -z "${app_version}" ]]; then
  printf 'FAIL: application version is missing in pyproject.toml\n' >&2
  exit 2
fi

mkdir -p "${TARGET_DIR}"
printf '%s\n' "${revision}" >"${TARGET_DIR}/VERSION"
{
  printf 'app_version=%s\n' "${app_version}"
  printf 'git_revision=%s\n' "${revision}"
  printf 'git_branch=%s\n' "${branch}"
  printf 'tree_state=%s\n' "${tree_state}"
  printf 'deployed_at=%s\n' "$(date -u --iso-8601=seconds)"
  printf 'source_checkout=%s\n' "${SOURCE_DIR}"
} >"${TARGET_DIR}/BUILD_INFO"

(
  cd "${TARGET_DIR}"
  find . -type f \
    ! -path './.venv/*' \
    ! -path './artifacts/*' \
    ! -path './logs/*' \
    ! -path './data/*' \
    ! -path './work/*' \
    ! -name VERSION \
    ! -name BUILD_INFO \
    ! -name BUILD_MANIFEST.sha256 \
    -print0 | sort -z \
    | xargs -0 sha256sum
) >"${TARGET_DIR}/BUILD_MANIFEST.sha256"

chmod 0644 \
  "${TARGET_DIR}/VERSION" \
  "${TARGET_DIR}/BUILD_INFO" \
  "${TARGET_DIR}/BUILD_MANIFEST.sha256"
printf 'Build metadata: version=%s revision=%s branch=%s state=%s\n' \
  "${app_version}" "${revision}" "${branch}" "${tree_state}"
