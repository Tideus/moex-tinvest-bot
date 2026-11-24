import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_long_auto_sandbox import case as case
from test_long_sandbox_verifier import saved as saved


@pytest.fixture
def watch():
    path = Path(__file__).resolve().parents[1] / 'scripts/ops/long_admission_watch.py'
    spec = importlib.util.spec_from_file_location('admission_watch', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def integrity():
    path = Path(__file__).resolve().parents[1] / 'scripts/ops/long_admission_integrity.py'
    spec = importlib.util.spec_from_file_location('admission_integrity', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def inputs():
    review = dict(revision='reviewed-release', sandbox_not_before='2026-11-08T16:00:00+00:00',
                  sandbox_account_sha256='a' * 64, sandbox_attempt=2, evidence={})
    proof = dict(version=1, environment='sandbox', observed_at='2026-11-09T07:20:00+00:00',
                 read_only=True, account_sha256='a' * 64, attempt=2,
                 secid='SBER', lot_size=10, limit_price='300', stop_trigger='294',
                 state='FILLED_PROTECTED', ledger_state='FILLED_PROTECTED',
                 broker_status='filled', lots=1, filled_lots=1,
                 buy_verified=True, stop_verified=True)
    for k in ('decision_sha256', 'ledger_candidate_sha256', 'parent_id_sha256', 'stop_id_sha256'):
        proof[k] = 'b' * 64
    return review, proof, datetime(2026, 11, 9, 7, 26, tzinfo=UTC)


def receipt(watch, proof):
    facts = (proof if proof.get('state') == 'UNAVAILABLE' or proof.get('version') == 2 else
             {k: v for k, v in proof.items() if k != 'observed_at'})
    name = watch.digest(json.dumps(facts, sort_keys=True).encode()) + '.json'
    return json.dumps(proof).encode(), name


def test_success_is_never_authority(watch, inputs):
    review, proof, now = inputs
    packet = watch.evaluate(review, [receipt(watch, proof)], now)
    assert packet['status'] == 'TECHNICAL_EVIDENCE_COMPLETE_PENDING_OPERATOR'
    assert packet['decision'] == 'DRAFT_NOT_AUTHORIZED'
    assert packet['execution_allowed'] is False
    assert 'fresh_iis_readonly_preflight' in packet['missing']
    assert 'НЕ разрешение' in watch.render(packet)


@pytest.mark.parametrize('field,value', [('attempt', 1), ('account_sha256', 'c'*64),
                                        ('observed_at', '2026-11-08T15:59:00+00:00'),
                                        ('observed_at', '2026-11-10T07:20:00+00:00')])
def test_foreign_old_future_evidence_ignored(watch, inputs, field, value):
    review, proof, now = inputs
    proof[field] = value
    assert watch.evaluate(review, [receipt(watch, proof)], now)['status'] == 'WAITING_SANDBOX'


@pytest.mark.parametrize('field,value', [('buy_verified', False), ('stop_verified', False),
                                        ('filled_lots', True), ('filled_lots', '1'),
                                        ('lots', 2), ('state', 'POSITION_CLOSED'),
                                        ('environment', 'prod'), ('read_only', False),
                                        ('stop_id_sha256', ''), ('ledger_state', 'STOP_PENDING')])
def test_incomplete_confirmation_not_success(watch, inputs, field, value):
    review, proof, now = inputs
    proof[field] = value
    assert watch.evaluate(review, [receipt(watch, proof)], now)['status'] == 'SANDBOX_NOT_CONFIRMED'


def test_corrupt_receipt_rejected(watch, inputs):
    review, proof, now = inputs
    raw, name = receipt(watch, proof)
    with pytest.raises(ValueError, match='hash differs'):
        watch.evaluate(review, [(raw, 'wrong.json')], now)


def test_later_adverse_result_blocks_success(watch, inputs):
    review, proof, now = inputs
    later = proof | dict(observed_at='2026-11-09T07:25:00+00:00', state='REQUIRES_REVIEW',
                         stop_verified=False)
    assert watch.evaluate(review, [receipt(watch, proof), receipt(watch, later)], now)[
        'status'] == 'SANDBOX_NOT_CONFIRMED'


def closed(proof, **changes):
    result = proof | dict(observed_at='2026-11-09T07:25:00+00:00',
                          state='POSITION_CLOSED', stop_verified=False)
    del result['stop_id_sha256']  # The actual verifier never supplies a stop ID after closure.
    return result | changes


def test_confirmed_then_closed_preserves_historical_proof_not_current_protection(watch, inputs):
    review, proof, now = inputs
    success, closure = receipt(watch, proof), receipt(watch, closed(proof))
    packet = watch.evaluate(review, [closure, success], now)
    assert packet == watch.evaluate(review, [success, closure], now)
    assert packet['sandbox_receipt_sha256'] == watch.digest(success[0])
    assert packet['sandbox_historical_proof_sha256'] == watch.digest(success[0])
    assert packet['sandbox_latest_receipt_sha256'] == watch.digest(closure[0])
    assert packet['sandbox_latest_state'] == 'POSITION_CLOSED'
    assert packet['sandbox_evidence_reason'] == 'historical_protection_then_closed'
    assert packet['status'] == 'TECHNICAL_EVIDENCE_COMPLETE_PENDING_OPERATOR'
    assert packet['execution_allowed'] is False
    assert 'fresh_production_monitoring_and_delivery' in packet['missing']
    text = watch.render(packet)
    assert 'Исторический' in text and 'Причина закрытия и P&L этим не установлены' in text
    assert 'не дают повторный вход' in text and 'активный стоп подтверждён' not in text


def test_closed_without_proof_or_before_proof_cannot_confirm(watch, inputs):
    review, proof, now = inputs
    closure = closed(proof)
    for evidence in ([closure], [closure | {'observed_at': '2026-11-09T07:19:00+00:00'}, proof]):
        packet = watch.evaluate(review, [receipt(watch, p) for p in evidence], now)
        assert packet['status'] == 'SANDBOX_NOT_CONFIRMED'
        assert packet['sandbox_receipt_sha256'] is None


@pytest.mark.parametrize('field,value', [
    ('decision_sha256', 'c'*64), ('ledger_candidate_sha256', 'c'*64),
    ('parent_id_sha256', 'c'*64), ('stop_id_sha256', 'c'*64),
    ('secid', 'T'), ('lot_size', 1), ('limit_price', '301'), ('stop_trigger', '295'),
    ('version', True), ('version', '1'), ('lots', True), ('lots', '1'),
    ('filled_lots', True), ('filled_lots', 2), ('stop_verified', True),
    ('buy_verified', False), ('ledger_state', 'UNKNOWN'), ('broker_status', 'unknown'),
    ('read_only', False), ('environment', 'prod'),
    ('state', 'REQUIRES_REVIEW'), ('state', 'UNKNOWN'),
])
def test_conflicting_closure_never_reuses_success(watch, inputs, field, value):
    review, proof, now = inputs
    packet = watch.evaluate(review, [receipt(watch, proof),
                                    receipt(watch, closed(proof, **{field: value}))], now)
    assert packet['status'] == 'SANDBOX_NOT_CONFIRMED'
    assert packet['sandbox_historical_proof_sha256'] is not None
    assert packet['sandbox_receipt_sha256'] is None and packet['execution_allowed'] is False


@pytest.mark.parametrize('changes', [
    {'state': 'POSITION_CLOSED', 'stop_verified': False}, {'stop_id_sha256': 'c'*64},
])
def test_simultaneous_conflicts_are_order_independent(watch, inputs, changes):
    review, proof, now = inputs
    rows = [receipt(watch, proof), receipt(watch, proof | changes)]
    left, right = (watch.evaluate(review, r, now) for r in (rows, list(reversed(rows))))
    assert left == right and left['status'] == 'SANDBOX_NOT_CONFIRMED'
    assert left['sandbox_evidence_reason'] == 'ambiguous_same_time'


def test_changed_stop_and_reopened_position_block(watch, inputs):
    review, proof, now = inputs
    for middle in (closed(proof, observed_at='2026-11-09T07:22:00+00:00'),
                   proof | {'observed_at': '2026-11-09T07:22:00+00:00',
                            'stop_id_sha256': 'c'*64}):
        rows = [proof, middle, proof | {'observed_at': '2026-11-09T07:25:00+00:00'}]
        assert watch.evaluate(review, [receipt(watch, p) for p in rows], now)[
            'status'] == 'SANDBOX_NOT_CONFIRMED'


def test_unbound_unavailable_is_visible_and_cannot_be_cleared_by_old_success(watch, inputs):
    review, proof, now = inputs
    error = dict(version=1, state='UNAVAILABLE', environment='sandbox', read_only=True,
                 observed_at='2026-11-09T07:25:00+00:00',
                 failure={'source': 't_invest_sandbox', 'category': 'connection'})
    packet = watch.evaluate(review, [receipt(watch, error), receipt(watch, proof)], now)
    assert packet['sandbox_historical_proof_sha256'] is not None
    assert packet['sandbox_evidence_reason'] == 'subsequent_readback_unavailable'
    assert packet['sandbox_latest_state'] == 'UNAVAILABLE'
    assert packet['status'] == 'SANDBOX_NOT_CONFIRMED'
    assert 'требуется свежая сверка' in watch.render(packet)
    assert 'повторная покупка для него не нужна' in watch.render(packet)
    assert 'sandbox_latest_readback_or_conflict_review' in packet['missing']
    assert 'sandbox_fill_and_native_stop' not in packet['missing']
    # An earlier failure does not erase a later complete readback.
    error['observed_at'] = '2026-11-09T07:19:00+00:00'
    assert watch.evaluate(review, [receipt(watch, error), receipt(watch, proof)], now)[
        'status'] == 'TECHNICAL_EVIDENCE_COMPLETE_PENDING_OPERATOR'


@pytest.mark.parametrize('field,value', [('version', True), ('version', '1'),
                                        ('lot_size', True), ('lot_size', '10')])
def test_schema_types_are_exact(watch, inputs, field, value):
    review, proof, now = inputs
    assert watch.evaluate(review, [receipt(watch, proof | {field: value})], now)[
        'status'] == 'SANDBOX_NOT_CONFIRMED'


def test_boolean_attempt_and_naive_time_do_not_authorize(watch, inputs):
    review, proof, now = inputs
    review['sandbox_attempt'] = 1
    proof['attempt'] = True
    assert watch.evaluate(review, [receipt(watch, proof)], now)[
        'status'] == 'SANDBOX_NOT_CONFIRMED'
    proof.update(attempt=1, observed_at='2026-11-09T07:20:00')
    with pytest.raises(ValueError, match='invalid time boundary'):
        watch.evaluate(review, [receipt(watch, proof)], now)


def test_producer_readbacks_and_publication_match_evaluator(watch, saved, tmp_path, monkeypatch):
    from moex_bot import long_sandbox_verifier as verifier
    from moex_bot.long_auto_once import run_sandbox_candidate

    candidate, state, anchor, broker, transport, now = saved
    run_sandbox_candidate(candidate, state, broker, clock=lambda: now, anchor=anchor)
    before = state.read_bytes(), anchor.read_bytes()
    proof = verifier.verify(state, anchor, candidate.account_id, broker, now)
    assert proof['state'] == 'FILLED_PROTECTED'
    original = transport.post

    def flat(url, **kwargs):
        if url.endswith('/GetSandboxPositions'):
            return {'securities': []}
        if url.endswith('/GetSandboxStopOrders'):
            return {'stopOrders': []}
        return original(url, **kwargs)

    monkeypatch.setattr(transport, 'post', flat)
    later = now + timedelta(minutes=5)
    closure = verifier.verify(state, anchor, candidate.account_id, broker, later)
    assert closure['state'] == 'POSITION_CLOSED'
    assert closure['stop_id_sha256'] == proof['stop_id_sha256']
    assert closure['stop_evidence_verified'] is True and closure['stop_verified'] is False
    folder, outbox = tmp_path/'receipts', tmp_path/'outbox.sqlite3'
    for p in (proof, closure):
        verifier.publish(p, folder, outbox, later)
    originals = {p.name: p.read_bytes() for p in folder.glob('*.json')}
    review = dict(revision='checked', evidence={}, sandbox_account_sha256=proof['account_sha256'],
                  sandbox_attempt=proof['attempt'], sandbox_not_before=now.isoformat())
    monkeypatch.setattr(watch, 'BASE', tmp_path)
    monkeypatch.setattr(watch, 'RECEIPTS', folder)
    monkeypatch.setattr(watch, 'OUTBOX', outbox)
    monkeypatch.setattr(watch, 'check_inputs', lambda: review)
    (tmp_path/'results').mkdir()
    assert watch.main() == watch.main() == 0
    packet = json.loads(next((tmp_path/'results').glob('*.json')).read_bytes())
    assert packet['status'] == 'TECHNICAL_EVIDENCE_COMPLETE_PENDING_OPERATOR'
    assert packet['sandbox_evidence_reason'] == 'historical_protection_then_closed'
    assert packet['execution_allowed'] is False
    assert {p.name: p.read_bytes() for p in folder.glob('*.json')} == originals
    assert (state.read_bytes(), anchor.read_bytes()) == before


def test_diagnostics_upgrade_pins_the_previous_shipped_blobs():
    import hashlib
    import subprocess

    root = Path(__file__).resolve().parents[1]
    installer = (root/'scripts/ubuntu/install-admission-diagnostics.sh').read_text()
    # No Git needed for ordinary tests; these are the independently reviewed LF bytes.
    expected = ('cd8ea80a200a947c88a1b30fdff70a924330f03361caf97bc26c5c5eb75bb448',
                '0d2f4fcbe36c76fd127dafca873ca39dc59b6ee55656e464168833e7b1653bfc')
    assert all(sha in installer for sha in expected)
    assert '"$old" != "${reviewed[$index]}"' in installer
    # Where the release object exists, verify the allowlist against Git, not deployed files.
    revision = '645342ec04880b4cd2b33c0b9831236af8fe2aa0'
    for name, sha in zip(('long_admission_watch.py', 'long_admission_integrity.py'),
                         expected, strict=True):
        result = subprocess.run(['git', '-C', str(root), 'show',
                                 f'{revision}:scripts/ops/{name}'], capture_output=True)
        if result.returncode == 0:
            assert hashlib.sha256(result.stdout).hexdigest() == sha


def test_deployment_and_evidence_pins(watch, integrity, inputs, tmp_path, monkeypatch):
    review, _, _ = inputs
    app, base = tmp_path/'app', tmp_path/'review'
    app.mkdir()
    base.mkdir()
    settings = tmp_path/'settings.json'
    settings.write_text('{"enabled": false}')
    (app/'BUILD_INFO').write_text('git_revision=reviewed-release\n')
    code = app/'safe.py'
    code.write_text('pass\n')
    manifest = app/'BUILD_MANIFEST.sha256'
    manifest.write_text(watch.digest(code.read_bytes())+'  ./safe.py\n')
    review.update(build_manifest_sha256=watch.digest(manifest.read_bytes()),
                  settings_sha256=watch.digest(settings.read_bytes()))
    for kind in watch.KINDS:
        raw = json.dumps(dict(kind=kind, result='PASS', revision='reviewed-release',
                              scope='offline only')).encode()
        (base/(kind+'.json')).write_bytes(raw)
        review['evidence'][kind] = watch.digest(raw)
    (base/'review.json').write_text(json.dumps(review))
    monkeypatch.setattr(watch, 'APP', app)
    monkeypatch.setattr(watch, 'BASE', base)
    monkeypatch.setattr(watch, 'SETTINGS', settings)
    monkeypatch.setattr(watch, 'root_check_succeeded', lambda: True)
    monkeypatch.setattr(integrity, 'APP', app)
    monkeypatch.setattr(integrity, 'BASE', base)
    (base/'integrity').mkdir()
    assert integrity.main() == 0
    assert watch.check_inputs()['revision'] == 'reviewed-release'
    code.write_text('changed\n')
    assert integrity.main() == 0  # Failure is published as data; reporter must still run.
    with pytest.raises(ValueError, match='integrity proof required'):
        watch.check_inputs()
    code.write_text('pass\n')
    assert integrity.main() == 0
    settings.write_text('{"enabled": true}')
    with pytest.raises(ValueError, match='disabled'):
        watch.check_inputs()
    settings.write_text('{"enabled": false}')
    proof_path = base/'integrity/status.json'
    proof = json.loads(proof_path.read_text())
    proof['checked_at'] = '2000-01-01T00:00:00+00:00'
    proof_path.write_text(json.dumps(proof))
    with pytest.raises(ValueError, match='integrity proof required'):
        watch.check_inputs()


def test_durable_report_deduplicates(watch, inputs, tmp_path, monkeypatch):
    from moex_bot.notifications import SQLiteOutbox
    review, _, _ = inputs
    monkeypatch.setattr(watch, 'BASE', tmp_path)
    monkeypatch.setattr(watch, 'OUTBOX', tmp_path/'outbox.sqlite3')
    monkeypatch.setattr(watch, 'RECEIPTS', tmp_path/'receipts')
    monkeypatch.setattr(watch, 'check_inputs', lambda: review)
    (tmp_path/'results').mkdir()
    # No broker transport is constructed; only an existing durable outbox is used.
    box = SQLiteOutbox(watch.OUTBOX)
    assert watch.main() == watch.main() == 0
    assert box.health(now=datetime.now(UTC))['pending_due'] == 1
    assert len(list((tmp_path/'results').glob('*.json'))) == 1


def test_interrupted_report_can_be_retried(watch, tmp_path, monkeypatch):
    path = tmp_path/'report.json'
    operation = 'rename' if watch.os.name == 'nt' else 'link'
    original = getattr(watch.os, operation)
    def interrupted(*args):
        raise OSError('simulated crash before atomic publication')
    monkeypatch.setattr(watch.os, operation, interrupted)
    with pytest.raises(OSError):
        watch.publish(path, b'{"execution_allowed":false}')
    assert not path.exists()
    assert not list(tmp_path.glob('.pending-*'))
    monkeypatch.setattr(watch.os, operation, original)
    watch.publish(path, b'{"execution_allowed":false}')
    assert json.loads(path.read_bytes())['execution_allowed'] is False
    with pytest.raises(ValueError, match='differs'):
        watch.publish(path, b'changed')


@pytest.mark.parametrize('message,stage,reason', [
    ('release changed; re-audit required', 'deployment_revision', 'release_changed'),
    ('manifest changed', 'deployment_manifest', 'manifest_changed'),
    ('receipt content hash differs', 'sandbox_evidence', 'receipt_hash_mismatch'),
    ('account=secret TOKEN=never-publish', 'review_inputs', 'unclassified_error'),
])
def test_failure_reason_is_safe_durable_and_deduplicated(
        watch, tmp_path, monkeypatch, capsys, message, stage, reason):
    from moex_bot.notifications import SQLiteOutbox

    monkeypatch.setattr(watch, 'BASE', tmp_path)
    monkeypatch.setattr(watch, 'OUTBOX', tmp_path/'outbox.sqlite3')
    (tmp_path/'results').mkdir()
    box = SQLiteOutbox(watch.OUTBOX)

    def fail():
        raise ValueError(message)

    monkeypatch.setattr(watch, 'check_inputs', fail)
    assert watch.main() == watch.main() == 2
    assert box.health(now=datetime.now(UTC))['pending_due'] == 1
    paths = list((tmp_path/'results').glob('*.json'))
    assert len(paths) == 1
    packet = json.loads(paths[0].read_bytes())
    assert packet['failure_stage'] == stage and packet['failure_reason'] == reason
    assert packet['execution_allowed'] is False
    assert packet['decision'] == 'DRAFT_NOT_AUTHORIZED'
    text = watch.render(packet) + capsys.readouterr().out + paths[0].read_text()
    assert 'never-publish' not in text and 'account=secret' not in text
    assert 'stage=' in text and 'reason=' in text
    assert 'НЕ разрешение' in text


def test_changed_release_remains_blocked_and_review_is_not_rewritten(watch, tmp_path, monkeypatch):
    app = tmp_path/'app'
    app.mkdir()
    (tmp_path/'review.json').write_text('{"revision":"old"}')
    (app/'BUILD_INFO').write_text('git_revision=new\n')
    monkeypatch.setattr(watch, 'BASE', tmp_path)
    monkeypatch.setattr(watch, 'APP', app)
    before = (tmp_path/'review.json').read_bytes()
    with pytest.raises(ValueError, match='release changed; re-audit required'):
        watch.check_inputs()
    assert (tmp_path/'review.json').read_bytes() == before


def test_integrity_failure_retains_stage_and_never_echoes_private_text(
        integrity, tmp_path, monkeypatch):
    (tmp_path/'review.json').write_text('{"build_manifest_sha256":"old"}')
    (tmp_path/'BUILD_MANIFEST.sha256').write_text('new')
    (tmp_path/'integrity').mkdir()
    monkeypatch.setattr(integrity, 'BASE', tmp_path)
    monkeypatch.setattr(integrity, 'APP', tmp_path)
    assert integrity.main() == 0
    result = json.loads((tmp_path/'integrity/status.json').read_bytes())
    assert result['passed'] is False
    assert result['failure_stage'] == 'deployment_manifest'
    assert result['failure_reason'] == 'manifest_changed'


def test_diagnostic_installer_does_not_reissue_authority_or_modify_units():
    root = Path(__file__).resolve().parents[1]
    script = (root/'scripts/ubuntu/install-admission-diagnostics.sh').read_text()
    assert 'Validate both files before changing either' in script
    assert 'sha256sum' in script and 'custom readiness diagnostics' in script
    assert 'review.json' not in script and 'settings.json' not in script
    assert 'systemctl' not in script and 'bot.env' not in script
    assert 'enable' not in script and 'rm ' not in script
    update = (root/'scripts/ubuntu/update.sh').read_text()
    assert 'bash "${PROJECT_DIR}/scripts/ubuntu/install-admission-diagnostics.sh"' in update
