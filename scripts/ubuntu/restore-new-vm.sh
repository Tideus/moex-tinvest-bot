#!/usr/bin/env bash
# Offline only. No credentials, installer, live destination or activation mode.
set -Eeuo pipefail
umask 077
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -I -B "${script_dir}/dr_journals.py" rehearse "$@"
