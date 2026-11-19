import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_cold_journal_rebuild_has_headroom_without_relaxing_resource_isolation():
    unit = (ROOT / "deploy/trader-journal/moex-trader-journal.service").read_text()
    assert "TimeoutStartSec=600" in unit
    assert "CPUQuota=40%" in unit and "MemoryMax=300M" in unit
    timer = (ROOT / "deploy/trader-journal/moex-trader-journal.timer").read_text()
    assert "OnUnitInactiveSec=5min" in timer


@pytest.mark.parametrize("script", ["install.sh", "update.sh"])
def test_service_import_guard_uses_installed_package_not_root_pythonpath(script):
    text = (ROOT / "scripts/ubuntu" / script).read_text()
    end = text.index("# END SERVICE PYTHON INSTALL")
    guard = text.index("-I -c 'import moex_bot.cli'", end)
    assert end < guard < text.index("systemctl daemon-reload", guard)
    assert 'runuser -u "${SERVICE_USER}"' in text[end:guard]
    assert "exit 2" in text[guard:guard+230]


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX umask regression")
@pytest.mark.parametrize("script,expected_calls", [("install.sh", 3), ("update.sh", 1)])
def test_private_umask_does_not_leak_into_installed_python_code(tmp_path, script, expected_calls):
    text = (ROOT / "scripts/ubuntu" / script).read_text()
    block = text.split("# BEGIN SERVICE PYTHON INSTALL\n", 1)[1].split(
        "# END SERVICE PYTHON INSTALL", 1)[0]
    app = tmp_path / "app"
    binary = app / ".venv/bin"
    binary.mkdir(parents=True)
    # No pip/network: execute the actual install block with recording fake binaries.
    stub = '#!/bin/sh\numask >> "$MASK_LOG"\nprintf code > "$CODE_FILE"\n'
    for path in (binary / "python", tmp_path / "python3.12"):
        path.write_text(stub)
        path.chmod(0o755)
    log, code = tmp_path / "masks", tmp_path / "code.py"
    env = dict(os.environ, APP_DIR=str(app), MASK_LOG=str(log), CODE_FILE=str(code),
               PATH=str(tmp_path)+os.pathsep+os.environ.get("PATH", ""))
    result = subprocess.run(["bash", "-c", "set -e; umask 077\n"+block+"\numask"],
                            env=env, text=True, capture_output=True, check=True)
    assert log.read_text().splitlines() == ["0022"]*expected_calls
    assert result.stdout.strip() == "0077"  # Secrets outside the subshell stay private.
    assert stat.S_IMODE(code.stat().st_mode) == 0o644


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX control shell")
def test_prelaunch_stops_before_api_checks_on_python_failure(tmp_path):
    text = (ROOT / "scripts/ubuntu/moex-botctl.sh").read_text()
    function = text.split("prelaunch() {", 1)[1].split("\nstart_bot()", 1)[0]
    # Execute actual prelaunch function with check doubles, no secrets/services/API.
    prelude = '''
set -e
FAILURES=0
CONFIG_DIR=/nonexistent
ENV_FILE=/nonexistent/bot.env
SERVICE_USER=test
PYTHON_BIN=/unreadable/python
heading() { :; }
load_secrets() { :; }
check() {
  case "$1" in
    *"Python CLI"*) FAILURES=$((FAILURES+1));;
    *"credentials"*|*"токен"*) echo API_CALLED; return 99;;
  esac
}
'''
    result = subprocess.run(["bash", "-c", prelude+"prelaunch() {"+function+"\nprelaunch"],
                            text=True, capture_output=True)
    assert result.returncode == 2 and "API_CALLED" not in result.stdout
    assert "проверки API не выполнялись" in result.stdout
