#!/usr/bin/env bash
set -Eeuo pipefail
# Invoke only in a new candidate directory with: unshare --mount --net bash ...
mount --make-rprivate /
mkdir -p work/control-fixture
chmod 0777 work work/control-fixture
mount --bind "$PWD/work/control-fixture" /var/lib/moex-tinvest-control
for path in /etc/moex-tinvest-bot /var/lib/moex-tinvest-bot /opt/moex-tinvest-bot; do
  mount -t tmpfs tmpfs "$path"
done
ip link set lo up
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/src:$PWD/scripts/research:/var/tmp/moex-rc9-test.ndrJt5Ty/.test-venv/lib/python3.12/site-packages:/opt/moex-pr6-shadow.HG5iukXr/venv/lib/python3.12/site-packages"
export MYPY_CACHE_DIR="$PWD/work/mypy"
export RUFF_CACHE_DIR="$PWD/work/ruff"
runuser -u nobody -- git init -q
runuser -u nobody -- chmod +x scripts/ubuntu/*.sh
runuser -u nobody -- git add .
runuser -u nobody -- git update-index --chmod=+x scripts/ubuntu/*.sh
runuser -u nobody -- git -c user.name=Isolated-Test -c user.email=test@example.invalid -c commit.gpgsign=false commit -qm candidate
runuser -u nobody -- python3 -m ruff check src tests scripts
runuser -u nobody -- python3 -m mypy --strict src/moex_bot
runuser -u nobody -- python3 -m pytest -q -o addopts='' -p no:cacheprovider --basetemp=work/pytest --tb=short
python3 -m pytest -q -o addopts='' -p no:cacheprovider --basetemp=work/pytest-root tests/test_prod_backup_lock.py::test_backup_busy_and_inherited_updater_descriptor tests/test_long_sandbox_retry.py::test_root_receipt_and_restart
chown root:root work/control-fixture
chmod 0755 work/control-fixture
install -o root -g nogroup -m 0660 /dev/null work/control-fixture/prod-session.lock
runuser -u nobody -- bash scripts/ubuntu/test-deployment.sh
systemd-analyze verify deploy/trader-journal/moex-trader-journal.service deploy/trader-journal/moex-trader-journal.timer
DESTDIR="$PWD/work/journal-stage" bash deploy/trader-journal/install.sh
[[ $(stat -c %a work/journal-stage/etc/moex-trader-journal/access.txt) == 600 ]]
[[ $(stat -c %a work/journal-stage/etc/moex-trader-journal/htpasswd) == 640 ]]
[[ $(stat -c %a work/journal-stage/var/lib/moex-trader-journal/public) == 2750 ]]
before_config=$(sha256sum work/journal-stage/etc/moex-trader-journal/{config.json,htpasswd})
DESTDIR="$PWD/work/journal-stage" bash deploy/trader-journal/install.sh
[[ "$before_config" == "$(sha256sum work/journal-stage/etc/moex-trader-journal/{config.json,htpasswd})" ]]
python3 deploy/trader-journal/test-nginx.py
echo 'PASS: isolated journal deployment, private permissions and idempotent reinstallation'
