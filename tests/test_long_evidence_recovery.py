"""Read-only recovery; no historical rewrite, re-entry, authority or notification storms."""
import json
import sqlite3
from datetime import timedelta

import pytest
from test_long_admission_watch import inputs as inputs
from test_long_admission_watch import receipt
from test_long_admission_watch import watch as watch
from test_long_auto_sandbox import case as case
from test_long_sandbox_verifier import saved as saved
from test_long_verification_diagnostics import running as running

from moex_bot import long_sandbox_verifier as verifier
from moex_bot.long_auto_once import run_sandbox_candidate
from moex_bot.notifications import SQLiteOutbox


def outage(now, **extra):
    return dict(version=1, environment='sandbox', state='UNAVAILABLE', read_only=True,
                observed_at=(now - timedelta(minutes=4)).isoformat(), **extra)


def recovery(proof, now, *, closed=False, seconds=30):
    p = proof | dict(version=2, request_started_at=(now - timedelta(seconds=seconds+1)).isoformat(),
                     observed_at=(now - timedelta(seconds=seconds)).isoformat(),
                     stop_evidence_verified=True)
    if closed:
        p.update(state='POSITION_CLOSED', stop_verified=False)
    return p


def evaluate(watch, review, now, *rows):
    return watch.evaluate(review, [receipt(watch, p) for p in rows], now)


@pytest.mark.parametrize('closed', [False, True])
def test_recovery_is_bound_readonly_and_history_preserved(watch, inputs, closed):
    review, proof, now = inputs
    failure = outage(now)
    latest = recovery(proof, now, closed=closed)
    rows = [receipt(watch, p) for p in (proof, failure, latest)]
    original = list(rows)
    p = watch.evaluate(review, rows, now)
    assert p['status'] == 'TECHNICAL_EVIDENCE_COMPLETE_PENDING_OPERATOR'
    assert p['sandbox_historical_proof_sha256'] == watch.digest(rows[0][0])
    assert p['sandbox_availability_episodes'][0]['failure_sha256'] == watch.digest(rows[1][0])
    assert p['sandbox_availability_episodes'][0]['recovery_sha256'] == watch.digest(rows[2][0])
    assert p['execution_allowed'] is False and p['decision'] == 'DRAFT_NOT_AUTHORIZED'
    assert rows == original and 'сбои сохранены' in watch.render(p)
    assert watch.evaluate(review, list(reversed(rows)), now) == p


def test_closure_failure_closure_and_latest_request_boundary(watch, inputs):
    review, proof, now = inputs
    first = recovery(proof, now, closed=True, seconds=300)
    last_failure = outage(now) | {'observed_at': (now-timedelta(seconds=45)).isoformat()}
    good = recovery(proof, now, closed=True)
    p = evaluate(watch, review, now, proof, first, outage(now), last_failure, good)
    assert p['status'].startswith('TECHNICAL_')
    assert p['sandbox_availability_episodes'][0]['last_failure_observed_at'] == last_failure[
        'observed_at']
    overlapping = good | {'request_started_at': (now-timedelta(seconds=100)).isoformat()}
    assert evaluate(watch, review, now, proof, outage(now), last_failure, overlapping)[
        'sandbox_evidence_reason'] == 'subsequent_readback_unavailable'


@pytest.mark.parametrize('offset', [0, -1])
def test_equal_or_overlapping_request_cannot_recover(watch, inputs, offset):
    review, proof, now = inputs
    failure = outage(now) | {'observed_at': (now-timedelta(seconds=60)).isoformat()}
    good = recovery(proof, now) | {
        'request_started_at': (now-timedelta(seconds=60)+timedelta(seconds=offset)).isoformat()}
    p = evaluate(watch, review, now, proof, failure, good)
    assert p['sandbox_evidence_reason'] == 'subsequent_readback_unavailable'
    assert p['sandbox_availability_episodes'][0]['recovery_sha256'] is None


def test_v1_cannot_recover_or_refresh(watch, inputs):
    review, proof, now = inputs
    legacy = proof | {'observed_at': (now-timedelta(seconds=10)).isoformat()}
    assert evaluate(watch, review, now, proof, outage(now), legacy)[
        'sandbox_evidence_reason'] == 'subsequent_readback_unavailable'
    later = now+timedelta(minutes=6)
    p = evaluate(watch, review, later, proof, outage(now), recovery(proof, now),
                 legacy | {'observed_at': later.isoformat()})
    assert p['sandbox_evidence_reason'] == 'recovered_readback_stale'


def test_recovery_history_and_current_freshness_are_separate(watch, inputs):
    review, proof, now = inputs
    first = recovery(proof, now)
    later = now+timedelta(minutes=6)
    old = evaluate(watch, review, later, proof, outage(now), first)
    assert old['sandbox_evidence_reason'] == 'recovered_readback_stale'
    fresh = evaluate(watch, review, later, proof, outage(now), first, recovery(proof, later))
    assert fresh['status'].startswith('TECHNICAL_')
    assert fresh['sandbox_availability_episodes'] == old['sandbox_availability_episodes']
    assert fresh['sandbox_recovery_latest_fresh_at'] != old['sandbox_recovery_latest_fresh_at']


@pytest.mark.parametrize('field,value', [
    ('decision_sha256', 'c'*64), ('ledger_candidate_sha256', 'c'*64),
    ('parent_id_sha256', 'c'*64), ('stop_id_sha256', 'c'*64),
    ('stop_evidence_verified', False), ('stop_id_sha256', None), ('version', 3),
    ('lots', True), ('filled_lots', 2), ('secid', 'T'), ('stop_trigger', '290'),
    ('state', 'REQUIRES_REVIEW'), ('environment', 'prod'),
    ('account_sha256', 'c'*64), ('attempt', 3),
])
def test_bad_binding_never_clears_outage(watch, inputs, field, value):
    review, proof, now = inputs
    bad = recovery(proof, now, closed=True) | {field: value}
    assert evaluate(watch, review, now, proof, outage(now), bad)[
        'status'] == 'SANDBOX_NOT_CONFIRMED'


@pytest.mark.parametrize('bad', ['conflict', 'reopen', 'local', 'unknown'])
def test_known_conflict_stays_sticky_after_later_success(watch, inputs, bad):
    review, proof, now = inputs
    middle = recovery(proof, now, seconds=120)
    if bad == 'conflict':
        middle['state'] = 'REQUIRES_REVIEW'
    elif bad == 'reopen':
        middle.update(state='POSITION_CLOSED', stop_verified=False)
    else:
        middle.update(state='UNAVAILABLE', failure={'source': 'local', 'category':
                      'validation' if bad == 'local' else 'unexpected'})
    p = evaluate(watch, review, now, proof, outage(now), middle, recovery(proof, now))
    assert p['status'] == 'SANDBOX_NOT_CONFIRMED' and p['sandbox_receipt_sha256'] is None


@pytest.mark.parametrize('field,value', [
    ('request_started_at', '2026-11-09T07:25:29'),
    ('request_started_at', '2026-11-09T07:25:31+00:00'),
    ('request_started_at', '2026-11-09T07:20:00+00:00'),
    ('observed_at', '2026-11-09T07:26:01+00:00'),
])
def test_v2_invalid_intervals_fail_closed(watch, inputs, field, value):
    review, proof, now = inputs
    with pytest.raises(ValueError, match='interval'):
        evaluate(watch, review, now, proof, outage(now), recovery(proof, now) | {field:value})


def test_outage_episodes_and_notifications(watch, inputs):
    review, proof, now = inputs
    f1, r1 = outage(now), recovery(proof, now, seconds=120)
    f2 = outage(now) | {'observed_at': (now-timedelta(seconds=60)).isoformat()}
    r2 = recovery(proof, now)
    rows = [proof, f1]
    failed = evaluate(watch, review, now, *rows)
    repeated = evaluate(watch, review, now, *rows,
                        f1 | {'observed_at': (now-timedelta(seconds=180)).isoformat()})
    assert watch.notification_key(failed) == watch.notification_key(repeated)
    recovered = evaluate(watch, review, now, *rows, r1)
    more = evaluate(watch, review, now, *rows, r1, r2)
    assert watch.notification_key(recovered) == watch.notification_key(more)
    second = evaluate(watch, review, now, *rows, r1, f2)
    assert second['status'] == 'SANDBOX_NOT_CONFIRMED'
    done = evaluate(watch, review, now, *rows, r1, f2, r2)
    assert done['status'].startswith('TECHNICAL_')
    assert len(done['sandbox_availability_episodes']) == 2
    assert len({watch.notification_key(p) for p in (failed,recovered,second,done)}) == 4


@pytest.mark.parametrize('fault', ['missing_fence', 'stop_id', 'request_id', 'verified', 'fence'])
def test_closed_readback_requires_original_durable_protection(saved, monkeypatch, fault):
    c, state, anchor, broker, transport, now = saved
    run_sandbox_candidate(c, state, broker, clock=lambda: now, anchor=anchor)
    original = transport.post
    def flat(url, **kwargs):
        if url.endswith('/GetSandboxPositions'):
            return {'securities': []}
        if url.endswith('/GetSandboxStopOrders'):
            return {'stopOrders': []}
        return original(url, **kwargs)
    monkeypatch.setattr(transport, 'post', flat)
    fence = anchor.with_suffix(anchor.suffix+'.stop-dispatch')
    if fault == 'missing_fence':
        fence.unlink()
    elif fault == 'fence':
        fence.write_text('{}')
    else:
        with sqlite3.connect(state) as db:
            detail = json.loads(db.execute('SELECT detail FROM attempt').fetchone()[0])
            detail[{'stop_id':'stopOrderId','request_id':'orderRequestId','verified':'verified'}[
                fault]] = None
            db.execute('UPDATE attempt SET detail=?', (json.dumps(detail),))
    before = state.read_bytes(), anchor.read_bytes()
    with pytest.raises(ValueError):
        verifier.verify(state, anchor, c.account_id, broker, now)
    assert before == (state.read_bytes(), anchor.read_bytes())


def test_main_publishes_v2_every_time_without_notification_spam(running):
    _, state, anchor, _, transport, _ = running
    before = state.read_bytes(), anchor.read_bytes()
    assert verifier.main() == verifier.main() == 0
    paths = list(verifier.OUTPUT.glob('*.json'))
    assert len(paths) == 2
    for path in paths:
        p = json.loads(path.read_bytes())
        assert p['version'] == 2 and p['request_started_at'] <= p['observed_at']
        assert p['stop_evidence_verified'] is True
    assert all(method in verifier.READS for method, _ in transport.posts)
    assert before == (state.read_bytes(), anchor.read_bytes())
    assert SQLiteOutbox(verifier.OUTBOX).health(now=verifier.datetime.now(verifier.UTC))[
        'pending_due'] == 1


def test_v2_publication_never_overwrites_or_collapses_timestamps(tmp_path, inputs):
    _, proof, now = inputs
    output, outbox = tmp_path/'evidence', tmp_path/'outbox.sqlite3'
    p = recovery(proof, now)
    verifier.publish(p, output, outbox, now)
    original = {f.name:f.read_bytes() for f in output.glob('*.json')}
    verifier.publish(p, output, outbox, now)
    verifier.publish(recovery(proof, now, seconds=10), output, outbox, now)
    assert len(list(output.glob('*.json'))) == 2
    assert all((output/name).read_bytes() == raw for name,raw in original.items())
    path = output/next(iter(original))
    path.write_text(json.dumps(p | {'observed_at': now.isoformat()}))
    with pytest.raises(ValueError, match='never silently replace'):
        verifier.publish(p, output, outbox, now)
