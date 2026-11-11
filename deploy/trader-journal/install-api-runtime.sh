#!/usr/bin/env bash
# Separate from staging so the real runtime permissions can be regression-tested.
set -Eeuo pipefail
umask 027
[[ $EUID -eq 0 ]] || { echo 'Run as root'; exit 2; }
target="$(realpath -e "${1:?journal application directory}")"
[[ "$target" != / && -f "$target/requirements-api.txt" ]] || exit 2
[[ -x "$target/.venv/bin/python" ]] || /usr/bin/python3 -m venv "$target/.venv"
pip_args=(--disable-pip-version-check --require-hashes --only-binary=:all:)
if [[ -n "${JOURNAL_WHEELHOUSE:-}" ]]; then
  pip_args+=(--no-index --find-links "$JOURNAL_WHEELHOUSE")
fi
"$target/.venv/bin/python" -m pip install "${pip_args[@]}" -r "$target/requirements-api.txt"
# umask 027 creates root:root 0750 directories: the API's www-data group must
# traverse/read them, but must never write code. Do not follow interpreter links.
chown -R --no-dereference root:www-data "$target/.venv"
chmod -R u=rwX,g=rX,o= "$target/.venv"
