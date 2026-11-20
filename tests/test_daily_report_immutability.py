"""Delayed update/restart must not replace a closed session with today's account."""
import argparse
import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_long_daily_report import DAY, NOW, fixture, point, write

from moex_bot import daily_report_store as store
from moex_bot import long_daily_report as long
from moex_bot import prod_once_observer as prod


def complete_long(tmp_path, monkeypatch):
    args = fixture(tmp_path, monkeypatch)
    write(args.output.parent / f"long-close-{DAY - timedelta(days=1)}.json", {
        **point(NOW - timedelta(days=1), 299900),
        "report_date": str(DAY - timedelta(days=1)),
        "account_binding": hashlib.sha256(b"long").hexdigest(),
    })
    assert long.generate(args, NOW) == 0
    return args


@pytest.mark.parametrize("hours", [0, 1, 12, 24, 72])
def test_long_repeat_never_reads_broker_or_changes_final(tmp_path, monkeypatch, hours):
    args = complete_long(tmp_path, monkeypatch)
    files = [args.output, args.output.with_suffix(".json"),
             args.output.parent / f"long-close-{DAY}.json"]
    original = [p.read_bytes() for p in files]
    service = Mock(side_effect=AssertionError("no live read for a completed date"))
    monkeypatch.setattr(long, "TInvestSandboxAccountService", service)
    assert long.generate(args, NOW + timedelta(hours=hours)) == 0
    assert original == [p.read_bytes() for p in files]
    service.assert_not_called()
    assert json.loads(files[1].read_bytes())["performance"]["pnl"] == "100"
    with sqlite3.connect(args.outbox) as db:
        assert db.execute('SELECT count(*) FROM notification_outbox').fetchone()[0] == 1
    assert json.loads(args.output.with_suffix('.attempt.json').read_bytes())['state'] == 'PRESERVED'


@pytest.mark.parametrize("existing", [False, True])
def test_late_missing_or_buggy_report_never_overwrites_or_queries(tmp_path, monkeypatch, existing):
    args = fixture(tmp_path, monkeypatch)
    if existing:
        write(args.output.with_suffix('.json'), {
            'report_date': str(DAY), 'observed_at': (NOW + timedelta(hours=12)).isoformat(),
            'performance': {'pnl': None},
        })
    original = args.output.with_suffix('.json').read_bytes() if existing else None
    service = Mock(side_effect=AssertionError('no current snapshot for a past close'))
    monkeypatch.setattr(long, 'TInvestSandboxAccountService', service)
    assert long.generate(args, NOW + timedelta(hours=12)) == 2
    service.assert_not_called()
    assert not args.output.exists() and not args.outbox.exists()
    if existing:
        assert args.output.with_suffix('.json').read_bytes() == original
    else:
        assert not args.output.with_suffix('.json').exists()


def test_legacy_final_and_foreign_scope(tmp_path, monkeypatch):
    args = complete_long(tmp_path, monkeypatch)
    path = args.output.with_suffix('.json')
    data = json.loads(path.read_bytes())
    del data['account_binding']
    del data['publication_final']
    write(path, data)
    original = path.read_bytes()
    assert long.generate(args, NOW + timedelta(hours=12)) == 0
    assert path.read_bytes() == original
    close = args.output.parent / f'long-close-{DAY}.json'
    data = json.loads(close.read_bytes())
    data['account_binding'] = 'foreign'
    write(close, data)
    with pytest.raises(ValueError, match='account mismatch'):
        long.generate(args, NOW + timedelta(hours=12))
    assert path.read_bytes() == original


@pytest.mark.parametrize('fault', ['date', 'binding', 'pnl', 'end', 'corrupt'])
def test_invalid_final_never_silently_regenerates(tmp_path, monkeypatch, fault):
    args = complete_long(tmp_path, monkeypatch)
    path = args.output.with_suffix('.json')
    data = json.loads(path.read_bytes())
    if fault == 'date':
        data['report_date'] = '2026-01-01'
    if fault == 'binding':
        data['account_binding'] = 'foreign'
    if fault == 'pnl':
        data['performance']['pnl'] = 'NaN'
    if fault == 'end':
        data['performance']['period_ended_at'] = (NOW + timedelta(hours=12)).isoformat()
    write(path, data)
    if fault == 'corrupt':
        path.write_text('invalid')
    original = path.read_bytes()
    with pytest.raises(ValueError):
        long.generate(args, NOW + timedelta(hours=12))
    assert path.read_bytes() == original


def test_interrupted_text_publish_recovers_from_final_not_broker(tmp_path, monkeypatch):
    args = complete_long(tmp_path, monkeypatch)
    original = args.output.read_bytes()
    args.output.unlink()
    monkeypatch.setattr(long, 'TInvestSandboxAccountService', Mock(side_effect=AssertionError))
    assert long.generate(args, NOW + timedelta(hours=12)) == 0
    assert args.output.read_bytes() == original


def test_close_without_final_requires_evidence_recovery(tmp_path, monkeypatch):
    args = complete_long(tmp_path, monkeypatch)
    args.output.with_suffix('.json').unlink()
    monkeypatch.setattr(long, 'TInvestSandboxAccountService', Mock(side_effect=AssertionError))
    assert long.generate(args, NOW) == 2
    assert not args.output.with_suffix('.json').exists()


def prod_args(tmp_path):
    return argparse.Namespace(output=tmp_path/'prod.txt', outbox=tmp_path/'outbox.sqlite3',
                              settings=tmp_path/'unused')


@pytest.mark.parametrize('legacy', [False, True])
def test_prod_final_is_preserved_on_daytime_restart(tmp_path, monkeypatch, legacy):
    args = prod_args(tmp_path)
    read = Mock(return_value={'environment': 'prod', 'observed_at': NOW.isoformat(),
                             'state': 'DISABLED', 'check': 'OK'})
    monkeypatch.setattr(prod, 'safe_observe', read)
    monkeypatch.setenv('T_INVEST_PROD_ACCOUNT_ID', 'iis')
    assert prod.daily_report(args, DAY, NOW) == 0
    path = args.output.with_suffix('.json')
    if legacy:
        data = json.loads(path.read_bytes())
        del data['report_date']
        del data['account_binding']
        write(path, data)
    original = path.read_bytes(), args.output.read_bytes()
    assert prod.daily_report(args, DAY, NOW + timedelta(hours=12)) == 0
    read.assert_called_once()
    assert original == (path.read_bytes(), args.output.read_bytes())
    with sqlite3.connect(args.outbox) as db:
        assert db.execute('SELECT count(*) FROM notification_outbox').fetchone()[0] == 1


def test_prod_late_no_report_does_not_read_current_account(tmp_path, monkeypatch):
    args = prod_args(tmp_path)
    monkeypatch.setattr(prod, 'safe_observe', Mock(side_effect=AssertionError))
    assert prod.daily_report(args, DAY, NOW + timedelta(hours=12)) == 2
    assert not args.output.exists() and not args.outbox.exists()


def test_atomic_write_failure_preserves_previous_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path/'result.json'
    target.write_text('old')
    monkeypatch.setattr(Path, 'replace', Mock(side_effect=OSError('disk error')))
    with pytest.raises(OSError):
        store.atomic_text(target, 'new')
    assert target.read_text() == 'old'
    assert list(tmp_path.glob('*.tmp')) == []


def test_concurrent_publisher_is_rejected_and_lock_recovers(tmp_path):
    output = tmp_path/'report.txt'
    with store.report_lock(output), pytest.raises(OSError), store.report_lock(output):
        pytest.fail('second writer admitted')
    with store.report_lock(output):
        pass


def test_naive_close_rejected():
    with pytest.raises(ValueError, match='aware'):
        store.closing_window(DAY, NOW.replace(tzinfo=None))
