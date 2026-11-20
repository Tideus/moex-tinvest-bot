import pytest

from moex_bot.admission_diagnostics import REASONS, explanation, failure


@pytest.mark.parametrize('message,expected', REASONS.items())
def test_fixed_reasons_have_explicit_stages(message, expected):
    packet = failure(ValueError(message), 'review_inputs')
    assert (packet['failure_stage'], packet['failure_reason']) == expected[:2]
    assert expected[2] in explanation(packet)


def test_unknown_errors_and_packet_fields_never_leak():
    class SecretError(Exception):
        pass

    packet = failure(SecretError('TOKEN=secret'), 'account=secret')
    assert packet == {'failure_stage': 'unknown', 'failure_reason': 'unclassified_error',
                      'error_type': 'Exception'}
    assert 'secret' not in explanation({'failure_stage': 'account=secret'})
    assert failure(PermissionError('private path'), 'sandbox_receipts')[
        'failure_stage'] == 'sandbox_receipts'
    # A subclass carrying arbitrary content cannot masquerade as our fixed validation error.
    assert failure(KeyError('manifest changed'), 'integrity_inputs')[
        'failure_reason'] == 'unclassified_error'
