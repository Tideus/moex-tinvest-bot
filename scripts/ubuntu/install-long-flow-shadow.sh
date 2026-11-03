#!/usr/bin/env bash
# Install a separate research sidecar. Never update/start the trading application.
set -Eeuo pipefail
project="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
stage="${DESTDIR:-}"
if [[ -z "${stage}" && "${EUID}" -ne 0 ]]; then
  printf 'Run as root.\n' >&2; exit 2
fi
if [[ -L "${stage}/opt/moex-long-flow-shadow" || -L "${stage}/var/lib/moex-long-flow-shadow" ]]; then
  printf 'Refusing symlink research directories.\n' >&2; exit 2
fi
if [[ -z "${stage}" ]]; then
  for name in moex-long-flow-shadow moex-long-flow-shadow-report; do
    if systemctl cat "${name}.timer" >/dev/null 2>&1; then
      systemctl stop "${name}.timer" "${name}.service"
    fi
  done
fi
install -d -m 0755 "${stage}/opt/moex-long-flow-shadow" "${stage}/etc/systemd/system"
install -m 0644 "${project}/scripts/research/long_flow_forward.py" \
  "${stage}/opt/moex-long-flow-shadow/long_flow_forward.py"
sha256sum "${project}/scripts/research/long_flow_forward.py" | cut -d ' ' -f 1 \
  >"${stage}/opt/moex-long-flow-shadow/SOURCE_SHA256"
for kind in service timer; do
  for name in moex-long-flow-shadow moex-long-flow-shadow-report; do
    install -m 0644 "${project}/deploy/ubuntu/${name}.${kind}" \
      "${stage}/etc/systemd/system/${name}.${kind}"
  done
done
if [[ -n "${stage}" ]]; then exit 0; fi
chown -R root:root /opt/moex-long-flow-shadow
install -d -o moexbot -g moexbot -m 0700 /var/lib/moex-long-flow-shadow
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/moex-long-flow-shadow{,-report}.{service,timer}
printf 'INSTALLED_DISABLED: standalone shadow only; trading app/config/journals untouched.\n'
printf 'Enable: sudo systemctl enable --now moex-long-flow-shadow.timer moex-long-flow-shadow-report.timer\n'
