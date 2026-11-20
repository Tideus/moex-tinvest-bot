#!/usr/bin/env bash
# Upgrade diagnostics only. Never replace review/evidence, admission, settings or units.
set -Eeuo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
TARGET=/usr/local/lib/moex-long-readiness-20261108
if [[ ! -f /etc/systemd/system/moex-long-admission-review.service || ! -d "$TARGET" ]]; then
  echo 'SKIP: separate LONG readiness observer is not installed'
  exit 0
fi
[[ "$EUID" -eq 0 ]] || { echo 'ERROR: root required' >&2; exit 1; }
[[ ! -L "$TARGET" && "$(stat -c %u "$TARGET")" == 0 ]]
sources=(long_admission_watch.py long_admission_integrity.py)
targets=(report.py verify-deployment.py)
previous=(0a30f078b1fd712447272252dc22df854aeb3cab4c29ecd14e7e159ef721ef4c ecae5ac51eeeffeda5b6a229ccbabb1542f715d7df6b4e75d91282a8e5d2006b)
# Validate both files before changing either. Refuse to overwrite unknown local edits.
for index in 0 1; do
  file="$TARGET/${targets[$index]}"
  source="$PROJECT_DIR/scripts/ops/${sources[$index]}"
  [[ -f "$file" && ! -L "$file" && "$(stat -c %u "$file")" == 0 ]]
  old="$(sha256sum "$file" | cut -d ' ' -f 1)"
  new="$(sha256sum "$source" | cut -d ' ' -f 1)"
  if [[ "$old" != "${previous[$index]}" && "$old" != "$new" ]]; then
    echo 'FAIL: custom readiness diagnostics; review before replacement' >&2
    exit 2
  fi
done
for index in 0 1; do
  install -m 0644 "$PROJECT_DIR/scripts/ops/${sources[$index]}" "$TARGET/${targets[$index]}"
done
echo 'PASS: LONG diagnostics installed; pinned evidence and trading authority unchanged'
