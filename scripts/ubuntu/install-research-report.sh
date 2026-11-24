#!/usr/bin/env bash
# Report-only repair. Never edit the frozen bundle, collector or trading units.
set -Eeuo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
BUNDLE=/opt/moex-research-forward-20261112-r2
UNIT=/etc/systemd/system/moex-research-forward-report.service
if [[ ! -f "$UNIT" || ! -f "$BUNDLE/BUNDLE_MANIFEST.sha256" ]]; then
  echo 'SKIP: separate frozen research report is not installed'
  exit 0
fi
[[ "$EUID" -eq 0 ]] || { echo 'ERROR: root required' >&2; exit 1; }
# Validation only; the bundle's registered SHA is not replaced.
(cd "$BUNDLE" && sha256sum --check --status BUNDLE_MANIFEST.sha256)
install -d -m 0755 /usr/local/lib/moex-research-report
install -m 0644 "$PROJECT_DIR/scripts/research/forward_report.py" \
  /usr/local/lib/moex-research-report/forward_report.py
install -m 0644 "$PROJECT_DIR/scripts/research/forward_diagnostics.py" \
  /usr/local/lib/moex-research-report/forward_diagnostics.py
install -d -m 0755 /etc/systemd/system/moex-research-forward-report.service.d
install -m 0644 "$PROJECT_DIR/deploy/research-report/report.conf" \
  /etc/systemd/system/moex-research-forward-report.service.d/report.conf
echo 'PASS: report-only helper installed; collector/model/registry unchanged'
