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
LINKS = ('decision_sha256', 'ledger_candidate_sha256', 'parent_id_sha256')
STATIC = ('secid', 'lot_size', 'limit_price', 'stop_trigger')


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError('unsafe or oversized evidence')
    return path.read_bytes()


def valid_hash(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in '0123456789abcdef' for c in value))


def confirmed(p: dict[str, Any], *, closed: bool = False) -> bool:
    return (type(p.get('version')) is int and p['version'] in (1, 2)
            and p.get('environment') == 'sandbox' and p.get('read_only') is True
            and p.get('ledger_state') == 'FILLED_PROTECTED'
            and p.get('state') == ('POSITION_CLOSED' if closed else 'FILLED_PROTECTED')
            and p.get('broker_status') == 'filled'
            and type(p.get('lots')) is int and p['lots'] == 1
            and type(p.get('filled_lots')) is int and p['filled_lots'] == 1
            and p.get('buy_verified') is True and p.get('stop_verified') is (not closed)
            and all(valid_hash(p.get(k)) for k in LINKS)
            and (closed or valid_hash(p.get('stop_id_sha256')))
            and (p['version'] == 1 or (p.get('stop_evidence_verified') is True
                                      and valid_hash(p.get('stop_id_sha256'))))
            and isinstance(p.get('secid'), str) and bool(p['secid'])
            and type(p.get('lot_size')) is int and p['lot_size'] > 0
            and all(isinstance(p.get(k), str) and p[k] for k in ('limit_price', 'stop_trigger')))


def recoverable(p: dict[str, Any]) -> bool:
    """Unavailability is not a contradiction. Known local/integrity faults remain sticky."""
    if p.get('read_only') is not True or type(p.get('version')) is not int:
        return False
    if p['version'] == 1 and 'failure' not in p:
        return True  # Original v1 had no error details; never infer a network cause.
    f = p.get('failure')
    return (p['version'] in (1, 2) and isinstance(f, dict)
            and f.get('source') == 't_invest_sandbox'
            and (f.get('category') in {'timeout', 'dns', 'tls', 'connection'}
                 or (f.get('category') == 'http' and type(f.get('http_status')) is int
                     and (f['http_status'] in {408, 429} or 500 <= f['http_status'] <= 599))))


def evaluate(review: dict[str, Any], receipts: list[tuple[bytes, str]],
             now: datetime) -> dict[str, Any]:
    """A successful proof only updates a non-authorizing technical evidence packet."""
    start = datetime.fromisoformat(review['sandbox_not_before'])
    if now.tzinfo is None or start.tzinfo is None or start > now:
        raise ValueError('invalid time boundary')
    if (type(review.get('sandbox_attempt')) is not int or review['sandbox_attempt'] < 1
            or not valid_hash(review.get('sandbox_account_sha256'))):
        raise ValueError('invalid sandbox review binding')
    matches = []
    for raw, name in receipts:
        p = json.loads(raw)
        unavailable = p.get('state') == 'UNAVAILABLE' and p.get('environment') == 'sandbox'
        if not unavailable and (p.get('account_sha256') != review['sandbox_account_sha256']
                or p.get('attempt') != review['sandbox_attempt']):
            continue
        at = datetime.fromisoformat(p['observed_at'])
        if at.tzinfo is None:
            raise ValueError('invalid time boundary')
        if type(p.get('version')) is int and p['version'] == 2:
            begun = datetime.fromisoformat(p['request_started_at'])
            if (begun.tzinfo is None or not begun <= at <= now
                    or not 0 <= (at - begun).total_seconds() <= 180):
                raise ValueError('invalid verification interval')
        if not start <= at <= now:
            continue
        # Producer hashes UNAVAILABLE including time; successful readbacks dedupe by facts.
        facts = p if unavailable or p.get('version') == 2 else {
            k: v for k, v in p.items() if k != 'observed_at'}
        expected = digest(json.dumps(facts, sort_keys=True).encode())
        if name != expected + '.json':
            raise ValueError('receipt content hash differs')
        matches.append((at, p, digest(raw)))
    matches.sort(key=lambda row: (row[0], row[2]))
    historical = next((row for row in matches if confirmed(row[1])
                       and type(row[1].get('attempt')) is int), None)
    state, proof, reason = 'WAITING_SANDBOX', None, 'no_receipt'
    if matches:
        state, reason = 'SANDBOX_NOT_CONFIRMED', 'no_historical_protection'
    episodes: list[dict[str, Any]] = []
    pending: datetime | None = None
    latest_fresh: datetime | None = None
    if historical:
        good_at, good, good_sha = historical
        reason = 'historical_protection_confirmed'
        closed_seen = False
        ambiguous = any(left[0] == right[0] and left[1] != right[1]
                        for left, right in zip(matches, matches[1:], strict=False))
        for at, p, sha in matches:
            if ambiguous:
                reason = 'ambiguous_same_time'
                break
            if p.get('state') == 'UNAVAILABLE':
                if at >= good_at:
                    if not recoverable(p):
                        reason = 'nonrecoverable_readback_error'
                        break
                    if pending is None:
                        episodes.append({'failure_sha256': sha,
                                         'failure_observed_at': at.isoformat(),
                                         'recovery_sha256': None, 'recovery_observed_at': None})
                    pending = at
                    episodes[-1].update(last_failure_sha256=sha,
                                        last_failure_observed_at=at.isoformat())
                    reason = 'subsequent_readback_unavailable'
                continue
            if (type(p.get('attempt')) is not int
                    or type(p.get('version')) is not int or p['version'] not in (1, 2)
                    or p.get('environment') != 'sandbox' or p.get('read_only') is not True
                    or type(p.get('lot_size')) is not int
                    or any(p.get(k) != good[k] for k in (*LINKS, *STATIC))
                    or ('stop_id_sha256' in p and p['stop_id_sha256'] != good['stop_id_sha256'])):
                reason = 'conflicting_binding'
                break
            if at < good_at:
                if p.get('state') != 'REQUIRES_REVIEW':
                    reason = 'closed_before_protection'
                    break
                continue
            if confirmed(p, closed=True) and at > good_at:
                closed_seen = True
            elif not confirmed(p) or closed_seen:
                reason = 'subsequent_readback_conflict'
                break
            if p['version'] == 2:
                begun = datetime.fromisoformat(p['request_started_at'])
                if pending is not None and begun > pending:
                    episodes[-1].update(recovery_sha256=sha, recovery_observed_at=at.isoformat())
                    pending = None
                if pending is None:
                    latest_fresh = at
        else:
            if pending is not None:
                reason = 'subsequent_readback_unavailable'
            elif episodes and (latest_fresh is None or (now - latest_fresh).total_seconds() > 300):
                reason = 'recovered_readback_stale'
            else:
                state, proof = 'TECHNICAL_EVIDENCE_COMPLETE_PENDING_OPERATOR', good_sha
                reason = ('historical_protection_then_closed' if closed_seen
                          else 'historical_protection_confirmed')
    latest = matches[-1] if matches else None
    return {'version': 2, 'decision': 'DRAFT_NOT_AUTHORIZED', 'execution_allowed': False,
            'revision': review['revision'], 'status': state,
            'technical_evidence': review['evidence'], 'sandbox_receipt_sha256': proof,
            'sandbox_historical_proof_sha256': historical[2] if historical else None,
            'sandbox_historical_observed_at': historical[1]['observed_at'] if historical else None,
            'sandbox_latest_receipt_sha256': latest[2] if latest else None,
            'sandbox_latest_observed_at': latest[1]['observed_at'] if latest else None,
            'sandbox_latest_state': latest[1].get('state') if latest else None,
            'sandbox_evidence_reason': reason,
            'sandbox_availability_episodes': episodes,
            'sandbox_recovery_latest_fresh_at': latest_fresh.isoformat() if episodes
                and latest_fresh is not None else None,
            'missing': PENDING + ([] if proof else [
                'sandbox_latest_readback_or_conflict_review' if historical
                else 'sandbox_fill_and_native_stop']),
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
             'SANDBOX_NOT_CONFIRMED': 'Sandbox-доказательства недостаточны или противоречивы.',
             'TECHNICAL_EVIDENCE_COMPLETE_PENDING_OPERATOR':
                 'Исторический sandbox BUY → защитный стоп подтверждён.',
             'REVIEW_INVALID': 'Проверка заблокирована: входные данные изменились или недоступны.'}
    detail = '\n' + explanation(p) if p['status'] == 'REVIEW_INVALID' else ''
    reasons = {
        'historical_protection_then_closed':
            'Позднее позиция закрыта. Причина закрытия и P&L этим не установлены.',
        'subsequent_readback_unavailable':
            'После подтверждения проверка была недоступна; требуется свежая сверка.',
        'nonrecoverable_readback_error':
            'Ошибка журнала/привязки или непроверенная причина: '
            'автоматическое восстановление запрещено.',
        'recovered_readback_stale':
            'Сверка после сбоя устарела (старше 5 минут); '
            'нужна новая проверка, не повторная покупка.',
        'conflicting_binding': 'Конфликт привязок попытки, покупки или стопа; нужна проверка.',
        'ambiguous_same_time': 'Противоречивые квитанции с одинаковым временем.',
        'closed_before_protection': 'Нарушена последовательность закрытия и защиты.',
        'subsequent_readback_conflict': 'Последующее состояние не подтверждает безопасную цепочку.',
        'no_historical_protection': 'Нет полного исторического подтверждения покупки и стопа.',
    }
    if p.get('sandbox_evidence_reason') in reasons:
        detail += '\n' + reasons[p['sandbox_evidence_reason']]
    if p.get('sandbox_historical_proof_sha256') and p['status'] == 'SANDBOX_NOT_CONFIRMED':
        detail += '\nИсторическое подтверждение сохранено; повторная покупка для него не нужна.'
    episodes = p.get('sandbox_availability_episodes', [])
    if episodes and episodes[-1]['recovery_sha256'] and p['status'].startswith('TECHNICAL_'):
        detail += ('\nСвязь после недоступности восстановлена свежей полной сверкой той же покупки '
                   'и исходной защиты. Все сбои сохранены в истории.')
    return ('📋 LONG · подготовка допуска на ИИС\n' + names[p['status']] + detail
            + '\nКвитанции не подтверждают текущую защиту ИИС и не дают повторный вход.'
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
                               dedupe_key='long-admission-review:' + notification_key(packet),
                               body=render(packet), now=now)
    diagnostic = (f" stage={packet['failure_stage']} reason={packet['failure_reason']}"
                  if packet['status'] == 'REVIEW_INVALID' else '')
    print(packet['status'] + diagnostic + '; execution_allowed=false')
    return 2 if packet['status'] == 'REVIEW_INVALID' else 0


def notification_key(packet: dict[str, Any]) -> str:
    """Alert on episode/status transitions, not on each two-minute receipt/freshness tick."""
    ignored = {'sandbox_latest_receipt_sha256', 'sandbox_latest_observed_at',
               'sandbox_recovery_latest_fresh_at', 'sandbox_availability_episodes'}
    stable = {k: v for k, v in packet.items() if k not in ignored}
    stable['episodes'] = [{k: ep[k] for k in ('failure_sha256', 'recovery_sha256')}
                          for ep in packet.get('sandbox_availability_episodes', [])]
    return digest(json.dumps(stable, sort_keys=True).encode())


if __name__ == '__main__':
    raise SystemExit(main())
