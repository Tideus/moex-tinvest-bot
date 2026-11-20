import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest


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
    proof = dict(environment='sandbox', observed_at='2026-11-09T07:20:00+00:00',
                 read_only=True, account_sha256='a' * 64, attempt=2,
                 state='FILLED_PROTECTED', ledger_state='FILLED_PROTECTED',
                 broker_status='filled', lots=1, filled_lots=1,
                 buy_verified=True, stop_verified=True)
    for k in ('decision_sha256', 'ledger_candidate_sha256', 'parent_id_sha256', 'stop_id_sha256'):
        proof[k] = 'b' * 64
    return review, proof, datetime(2026, 11, 9, 7, 26, tzinfo=UTC)


def receipt(watch, proof):
    facts = {k: v for k, v in proof.items() if k != 'observed_at'}
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
