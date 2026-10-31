"""Deterministic probes: real temporary databases, mocked network and systemd."""
import io
import json
import os
import runpy
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from moex_bot import monitoring
from moex_bot import monitoring_probes as p
from moex_bot.monitoring_config import load_settings

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 30, 10, tzinfo=UTC)


@pytest.fixture
def settings():
    return load_settings(ROOT / "config/monitoring.json")


@pytest.mark.parametrize("raw,expected", [
    (b'{}', 'OK'), (b'[]', 'response schema'), (b'{"ok":false}', 'rejected'),
    (b'invalid', 'transport/schema'), (b'<rss/>', 'OK'),
    (b'<feed/>', 'OK'), (b'<html/>', 'RSS schema'),
])
def test_network_schema(monkeypatch, raw, expected):
    monkeypatch.setattr(p, "urlopen", lambda *a, **k: io.BytesIO(raw))
    assert expected in p.request_probe("https://example.test", token="secret", payload={},
                                       xml=raw.startswith(b'<'))
    if raw == b'{}':
        assert "response schema" in p.request_probe("https://example.test", block="data")


@pytest.mark.parametrize("value", ["output", "returncode", "oserror", "timeout"])
def test_systemd(monkeypatch, value):
    def run(*a, **k):
        if value == 'oserror':
            raise OSError()
        if value == 'timeout':
            raise subprocess.TimeoutExpired('systemctl', 10)
        return SimpleNamespace(returncode=int(value == 'returncode'), stdout=' active\n')
    monkeypatch.setattr(p.subprocess, 'run', run)
    assert p.systemd('unit', 'prop') == ('active' if value == 'output' else 'unavailable')


@pytest.mark.parametrize("record,expected", [
    (None, 'no refresh'), ((NOW.isoformat(), 0), 'OK'),
    (((NOW-timedelta(days=2)).isoformat(), 0), 'stale/incomplete'),
    ((NOW.isoformat(), 1), 'stale/incomplete'),
])
def test_calendar_database(tmp_path, record, expected):
    path = tmp_path/'calendar.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE refreshes(received_at TEXT, failures INTEGER)')
        if record:
            db.execute('INSERT INTO refreshes VALUES(?,?)', record)
    assert expected in p.read_calendar_health(path, NOW)


def test_outbox_database(tmp_path):
    path = tmp_path/'outbox.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE notification_outbox(status TEXT, available_at TEXT)')
        db.executemany('INSERT INTO notification_outbox VALUES(?,?)',
                       [('pending', NOW.isoformat()), ('dead', NOW.isoformat())])
    assert p.read_outbox_health(path, NOW) == {'pending_due': 1, 'dead': 1}
    with pytest.raises(ValueError, match='timezone'):
        p.read_outbox_health(path, NOW.replace(tzinfo=None))


@pytest.mark.parametrize("variant", ['healthy', 'low', 'zero_inodes', 'unsupported'])
def test_resources(tmp_path, monkeypatch, settings, variant):
    monkeypatch.setattr(Path, 'read_text', lambda *a, **k:
                        'MemAvailable: '+ ('1' if variant == 'low' else '90') +
                        ' kB\nMemTotal: 100 kB\n')
    monkeypatch.setattr(p.shutil, 'disk_usage', lambda _: SimpleNamespace(total=100*2**30,
                                                                      free=50*2**30))
    monkeypatch.setattr(p.os, 'statvfs', None if variant == 'unsupported' else lambda _:
                        SimpleNamespace(f_favail=1 if variant == 'low' else 50,
                                        f_files=0 if variant == 'zero_inodes' else 100),
                        raising=False)
    if variant == 'unsupported':
        with pytest.raises(RuntimeError, match='POSIX'):
            p.collect_resources(tmp_path, tmp_path, settings, NOW)
    else:
        checks = p.collect_resources(tmp_path, tmp_path, settings, NOW)
        assert checks['memory:available'].startswith('WARNING' if variant == 'low' else 'OK')
        assert checks['inodes:/'].startswith('CRITICAL' if variant == 'low' else 'OK')


@pytest.mark.parametrize("variant", ['healthy', 'failed', 'stuck', 'recent', 'bad_time'])
def test_service_probes(tmp_path, monkeypatch, settings, variant):
    def unit(name, prop):
        if prop == 'ActiveState':
            if name.endswith('.service'):
                return 'activating' if variant in {'stuck','recent','bad_time'} else 'inactive'
            return 'inactive' if variant == 'failed' else 'active'
        if prop == 'UnitFileState':
            return 'enabled'
        if prop == 'Result':
            return 'failed' if variant == 'failed' else 'success'
        if prop == 'StateChangeTimestampMonotonic':
            return 'bad' if variant == 'bad_time' else '100000000'
        return '100'
    monkeypatch.setattr(p, 'systemd', unit)
    monkeypatch.setattr(Path, 'read_text', lambda *a, **k: '200' if variant=='recent' else '2000')
    checks = p.collect_services(tmp_path, tmp_path, settings, NOW)
    assert ('FAIL' in checks['timer:shadow']) == (variant == 'failed')
    assert ('FAIL' in checks['service:shadow']) == (variant in {'failed','stuck','bad_time'})
    assert 'timer:calendar' in checks
    settings['calendar_required'] = False
    assert 'timer:calendar' not in p.collect_services(tmp_path,tmp_path,settings,NOW)


@pytest.mark.parametrize("variant", [
    'healthy', 'missing', 'no_alert', 'calendar_error', 'optional'])
def test_integrations(tmp_path, monkeypatch, settings, variant):
    calls = []
    def probe(url, **kwargs):
        calls.append((url, kwargs))
        return 'OK'
    monkeypatch.setattr(p, 'request_probe', probe)
    def calendar(*a):
        if variant == 'calendar_error':
            raise sqlite3.OperationalError('secret')
        return 'OK'
    monkeypatch.setattr(p, 'read_calendar_health', calendar)
    for key in ('MOEX_APIKEY', 'T_INVEST_SANDBOX_TOKEN', 'T_INVEST_PROD_TOKEN',
                'T_INVEST_READONLY_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_ALERT_CHAT_ID'):
        monkeypatch.setenv(key, '' if variant == 'missing' else 'test')
    if variant == 'no_alert':
        monkeypatch.delenv('TELEGRAM_ALERT_CHAT_ID')
    if variant == 'optional':
        settings['required_tinvest_environments'] = []
        settings['calendar_required'] = False
    checks = p.collect_integrations(ROOT, tmp_path, settings, NOW)
    assert checks['api:algopack_obstats'] == ('FAIL: missing token' if variant=='missing' else 'OK')
    assert ('api:tinvest_prod' in checks) == (variant != 'optional')
    if variant == 'calendar_error':
        assert checks['data:calendar'] == 'FAIL: storage/read error'
    if variant == 'no_alert':
        assert 'api:telegram_alert_chat' not in checks
    assert calls


@pytest.mark.parametrize("variant", ['quality', 'bad_schema', 'blocked', 'empty', 'positions'])
def test_artifact_probes(tmp_path, monkeypatch, settings, variant):
    artifacts = tmp_path/'artifacts'
    artifacts.mkdir()
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(p,'datetime',Clock)
    def health(*a):
        if variant == 'bad_schema':
            raise OSError()
        return {'dead': 1 if variant=='blocked' else 0, 'pending_due': 0}
    monkeypatch.setattr(p,'read_outbox_health',health)
    backup = tmp_path/'moex-tinvest-test.tar.gz'
    backup.write_bytes(b'backup')
    os.utime(backup, (NOW.timestamp(), NOW.timestamp()))
    original = Path.glob
    def glob(path, pattern):
        if str(path).replace('\\','/') == '/var/backups/moex-tinvest-bot':
            return iter([backup])
        return original(path,pattern)
    monkeypatch.setattr(Path,'glob',glob)
    if variant != 'empty':
        for prefix in ('intraday-plan-', 'shadow-'):
            f = artifacts/(prefix+'test.json')
            f.write_text('bad' if variant=='bad_schema' else json.dumps(
                {'quality': {'passed': variant != 'blocked'}}))
            os.utime(f,(NOW.timestamp(),NOW.timestamp()))
    checks = p.collect_artifacts(ROOT,tmp_path,settings,NOW)
    assert checks['backup:age'] == 'OK'
    if variant != 'empty':
        assert (checks['quality:long'] == 'OK') == (variant not in {'bad_schema','blocked'})
    position = artifacts/'intraday-portfolio-test.json'
    for raw, expected in [('bad','read error'), ('{"positions":{}}','OK'),
                          ('{"positions":{"SBER":1}}','has positions')]:
        position.write_text(raw)
        late = NOW.replace(hour=17)
        result = p.collect_artifacts(ROOT, tmp_path, settings, late)
        assert expected in result['intraday:overnight_exposure']


@pytest.mark.parametrize("field,value", [('disk_warn_pct',True),('timeout_seconds','bad'),
    ('repeat_hours',0),('disk_warn_pct',1),('calendar_required',1),
    ('required_tinvest_environments','prod'),('required_tinvest_environments',['bad'])])
def test_settings_validation(tmp_path,settings,field,value):
    settings[field]=value
    path=tmp_path/'settings.json'
    path.write_text(json.dumps(settings))
    with pytest.raises(ValueError):
        load_settings(path)


@pytest.mark.parametrize("variant", ['test', 'conflict', 'error', 'notify'])
def test_monitoring_cli(tmp_path,monkeypatch,variant):
    args=['monitoring','--app',str(ROOT),'--state',str(tmp_path)]
    if variant in {'test','conflict'}:
        args.append('--test-alert')
    if variant == 'conflict':
        args.append('--collect-only')
    monkeypatch.setattr(sys,'argv',args)
    def collect(*a):
        if variant=='error':
            raise OSError('sensitive')
        return {'timer:test':'OK'}
    monkeypatch.setattr(monitoring,'collect',collect)
    if variant=='conflict':
        with pytest.raises(SystemExit):
            monitoring.main()
    else:
        assert monitoring.main() == (2 if variant=='error' else 0)
        if variant=='error':
            assert 'sensitive' not in (tmp_path/'artifacts/monitoring-latest.json').read_text()


def test_zero_disk(settings):
    assert p.disk_status(0,0,settings).startswith('CRITICAL')


def test_module_entrypoint(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['monitoring', '--app', str(ROOT), '--state',
                                     str(tmp_path), '--collect-only'])
    monkeypatch.setattr(p, 'collect', lambda *a: {'probe': 'OK'})
    monkeypatch.delitem(sys.modules, 'moex_bot.monitoring', raising=False)
    with pytest.raises(SystemExit) as result:
        runpy.run_module('moex_bot.monitoring', run_name='__main__')
    assert result.value.code == 0
