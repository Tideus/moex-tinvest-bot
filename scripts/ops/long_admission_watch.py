"""Collect evidence only. No credentials, broker calls, grant or trading configuration writes."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moex_bot.admission_diagnostics import explanation, failure
from moex_bot.notifications import SQLiteOutbox

BASE = Path('/var/lib/moex-long-readiness-20261108')
APP = Path('/opt/moex-tinvest-bot')
SETTINGS = Path('/etc/moex-tinvest-bot/prod-long-once.json')
RECEIPTS = Path('/var/lib/moex-tinvest-bot/artifacts/long-sandbox-verification')
OUTBOX = Path('/var/lib/moex-tinvest-bot/data/notifications.sqlite3')
KINDS = {'tests', 'independent_audit', 'restore_drill', 'monitoring_reporting'}
PENDING = ['fresh_iis_readonly_preflight', 'fresh_production_monitoring_and_delivery',
           'fresh_off_host_control_head', 'operator_recovery_readiness',
           'separate_explicit_operator_authorization']


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError('unsafe or oversized evidence')
    return path.read_bytes()


def evaluate(review: dict[str, Any], receipts: list[tuple[bytes, str]],
             now: datetime) -> dict[str, Any]:
    """A successful proof only updates a non-authorizing technical evidence packet."""
    start = datetime.fromisoformat(review['sandbox_not_before'])
    if now.tzinfo is None or start.tzinfo is None or start > now:
        raise ValueError('invalid time boundary')
    matches = []
    for raw, name in receipts:
        p = json.loads(raw)
        if (p.get('account_sha256') != review['sandbox_account_sha256']
                or p.get('attempt') != review['sandbox_attempt']):
            continue
        at = datetime.fromisoformat(p['observed_at'])
        if at.tzinfo is None or not start <= at <= now:
            continue
        facts = {k: v for k, v in p.items() if k != 'observed_at'}
        expected = digest(json.dumps(facts, sort_keys=True).encode())
        if name != expected + '.json':
            raise ValueError('receipt content hash differs')
        matches.append((at, p, digest(raw)))
    state, proof = 'WAITING_SANDBOX', None
    if matches:
        _, p, sha = max(matches, key=lambda x: x[0])
        exact = (p.get('environment') == 'sandbox' and p.get('read_only') is True
                 and p.get('state') == p.get('ledger_state') == 'FILLED_PROTECTED'
                 and p.get('broker_status') == 'filled'
                 and type(p.get('filled_lots')) is int and p['filled_lots'] == 1
                 and type(p.get('lots')) is int and p['lots'] == 1
                 and p.get('buy_verified') is True and p.get('stop_verified') is True)
        hashes = ('decision_sha256', 'ledger_candidate_sha256',
                  'parent_id_sha256', 'stop_id_sha256')
        exact = exact and all(isinstance(p.get(k), str) and len(p[k]) == 64
                              and all(c in '0123456789abcdef' for c in p[k]) for k in hashes)
        if exact:
            state, proof = 'TECHNICAL_EVIDENCE_COMPLETE_PENDING_OPERATOR', sha
        else:
            state = 'SANDBOX_NOT_CONFIRMED'
    return {'version': 1, 'decision': 'DRAFT_NOT_AUTHORIZED', 'execution_allowed': False,
            'revision': review['revision'], 'status': state,
            'technical_evidence': review['evidence'], 'sandbox_receipt_sha256': proof,
            'missing': PENDING + ([] if proof else ['sandbox_fill_and_native_stop']),
            'note': 'No trading admission. Historical sandbox proof is not current IIS protection.'}


def root_check_succeeded() -> bool:
    result = subprocess.run(
        ['systemctl', 'show', 'moex-long-admission-integrity.service', '-p', 'Result', '--value'],
        check=True, capture_output=True, text=True, timeout=10)
    return result.stdout.strip() == 'success'


def check_inputs() -> dict[str, Any]:
    raw_review = read(BASE / 'review.json')
    review: dict[str, Any] = json.loads(raw_review)
    info = dict(line.split('=', 1) for line in read(APP / 'BUILD_INFO').decode().splitlines()
                if '=' in line)
    if info.get('git_revision') != review['revision']:
        raise ValueError('release changed; re-audit required')
    manifest = read(APP / 'BUILD_MANIFEST.sha256')
    if digest(manifest) != review['build_manifest_sha256']:
        raise ValueError('manifest changed')
    if not root_check_succeeded():
        raise ValueError('root integrity service failed')
    integrity = json.loads(read(BASE / 'integrity/status.json'))
    checked = datetime.fromisoformat(integrity['checked_at'])
    if (checked.tzinfo is None or not 0 <= (datetime.now(UTC) - checked).total_seconds() <= 120
            or integrity.get('passed') is not True
            or integrity.get('review_sha256') != digest(raw_review)):
        raise ValueError('fresh root deployment integrity proof required')
    settings = read(SETTINGS)
    if (digest(settings) != review['settings_sha256']
            or json.loads(settings)['enabled'] is not False):
        raise ValueError('review only while unchanged PROD config disabled')
    if set(review['evidence']) != KINDS:
        raise ValueError('missing technical evidence')
    for kind, sha in review['evidence'].items():
        raw = read(BASE / (kind + '.json'))
        p = json.loads(raw)
        if (digest(raw) != sha or p.get('kind') != kind or p.get('result') != 'PASS'
                or p.get('revision') != review['revision'] or not p.get('scope')):
            raise ValueError('unreviewed or changed technical evidence')
    return review


def render(p: dict[str, Any]) -> str:
    names = {'WAITING_SANDBOX': 'Ожидается реальный BUY → подтверждённый стоп в sandbox.',
             'SANDBOX_NOT_CONFIRMED': 'Sandbox-покупка и активный стоп ещё не подтверждены.',
             'TECHNICAL_EVIDENCE_COMPLETE_PENDING_OPERATOR':
                 'Sandbox BUY и активный стоп подтверждены: технический пакет дополнен.',
             'REVIEW_INVALID': 'Проверка заблокирована: входные данные изменились или недоступны.'}
    detail = '\n' + explanation(p) if p['status'] == 'REVIEW_INVALID' else ''
    return ('📋 LONG · подготовка допуска на ИИС\n' + names[p['status']] + detail
            + '\n\nЭтот сборщик не включает PROD. Это НЕ разрешение на торговлю.'
            + '\nОстаются: свежая сверка ИИС, проверка контроля/доставки перед включением,'
              ' готовность оператора к восстановлению и отдельное подтверждение.'
            + '\nПрибыльность и P&L этим пакетом не подтверждаются.')


def publish(output: Path, raw: bytes) -> None:
    if not output.exists():
        fd, name = tempfile.mkstemp(prefix='.pending-', dir=output.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            with suppress(FileExistsError):
                if os.name == 'nt':
                    os.rename(temporary, output)  # Windows refuses an existing destination.
                else:
                    os.link(temporary, output)  # Atomic publication, never overwrite evidence.
        finally:
            temporary.unlink(missing_ok=True)
        if os.name != 'nt':
            fd = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    if read(output) != raw:
        raise ValueError('existing report differs')


def main() -> int:
    now = datetime.now(UTC)
    stage = 'review_inputs'
    try:
        review = check_inputs()
        stage = 'sandbox_receipts'
        evidence = [(read(p), p.name) for p in RECEIPTS.glob('*.json')]
        stage = 'sandbox_evaluate'
        packet = evaluate(review, evidence, now)
    except Exception as exc:
        packet = {'version': 1, 'decision': 'DRAFT_NOT_AUTHORIZED', 'execution_allowed': False,
                  'status': 'REVIEW_INVALID', **failure(exc, stage)}
    raw = json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2).encode()
    sha = digest(raw)
    output = BASE / 'results' / (sha + '.json')
    # Immutable, private report. It is never placed at the live admission path.
    publish(output, raw)
    if not OUTBOX.is_file():
        raise ValueError('shared outbox missing')
    SQLiteOutbox(OUTBOX).enqueue(kind='long_admission_review',
                               dedupe_key='long-admission-review:' + sha,
                               body=render(packet), now=now)
    diagnostic = (f" stage={packet['failure_stage']} reason={packet['failure_reason']}"
                  if packet['status'] == 'REVIEW_INVALID' else '')
    print(packet['status'] + diagnostic + '; execution_allowed=false')
    return 2 if packet['status'] == 'REVIEW_INVALID' else 0


if __name__ == '__main__':
    raise SystemExit(main())
