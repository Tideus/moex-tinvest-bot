"""Independent bases; no invented zero/BOD, cash flows or retrospective reset."""
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_live_execution import _setup

from moex_bot import daily_risk as risk
from moex_bot import daily_risk_reader as reader
from moex_bot.domain import Side
from moex_bot.live_execution import _authorize, execute_live_intent, policy_digest

BOD = datetime(2026, 10, 29, 21, 0, 5, tzinfo=UTC)
NOW = datetime(2026, 10, 30, 10, tzinfo=UTC)


def evidence(pnl='-1000', opening='160000', flows='0', now=NOW):
    return risk.AccountDailyRisk(risk.account_hash('real-iis'), BOD, now, D(opening),
                                 D(opening)+D(pnl)+D(flows), D(flows), D(pnl), 'a'*64)


def gate(**updates):
    data = evidence()
    args = dict(account='real-iis', equity=data.current_equity, now=NOW,
                account_risk=data, strategy_pnl=D(0), strategy_capital=D(40000),
                strategy_weight=D('.015'), account_weight=D('.015'))
    args.update(updates)
    risk.check_daily_limits(**args)


def test_manual_loss_is_not_long_loss_and_bod_denominator_is_fixed():
    gate()  # Manual -1000 exceeds LONG's 600 but not the IIS's 2400.
    for pnl in ('-600', '-2399.99'):
        data = evidence(pnl)
        gate(equity=data.current_equity, account_risk=data)
    data = evidence('-2400')
    with pytest.raises(ValueError, match='IIS daily loss.*2400'):
        gate(equity=data.current_equity, account_risk=data)
    with pytest.raises(ValueError, match='LONG daily loss.*600'):
        gate(strategy_pnl=D(-600))


@pytest.mark.parametrize('change', [
    {'account_risk': None}, {'strategy_pnl': None}, {'strategy_pnl': D('NaN')},
    {'account': 'foreign'}, {'equity': D(123)}, {'now': NOW+timedelta(seconds=21)},
    {'now': NOW-timedelta(seconds=1)}, {'now': NOW+timedelta(days=1)},
    {'strategy_weight': D('NaN')}, {'account_weight': D('.03')},
])
def test_unknown_or_inconsistent_never_becomes_zero(change):
    with pytest.raises(ValueError):
        gate(**change)


@pytest.mark.parametrize('change', [
    {'basis_at': NOW}, {'basis_at': BOD.replace(tzinfo=None)},
    {'pnl': D(0)}, {'opening_equity': D('Infinity')}, {'basis_sha256': 'x'*64},
])
def test_bad_evidence_fails(change):
    with pytest.raises(ValueError):
        gate(account_risk=replace(evidence(), **change))


def split_case(tmp_path):
    broker, kw, now = _setup(tmp_path)
    policy = kw['policy']
    kw['policy'] = replace(policy, canary=replace(policy.canary,
                                                 max_strategy_daily_loss_weight=D('.015')))
    kw['permission'] = replace(kw['permission'], policy_sha256=policy_digest(kw['policy']))
    data = evidence()
    broker.state = replace(broker.state, equity=data.current_equity, daily_pnl=D('-1000'),
                           account_daily_risk=data)
    return broker, kw, now


def test_first_entry_proves_zero_inside_registered_journal(tmp_path):
    broker, kw, now = split_case(tmp_path)
    result = execute_live_intent(**kw)
    assert result and len(broker.calls) == 1
    with closing(sqlite3.connect(kw['journal_path'])) as db:
        assert risk.unused_strategy_pnl(db, broker.account_id,
                                       kw['permission'].journal_uuid) is None
    assert execute_live_intent(**kw) is None  # no repeat even under new bases


def test_unenrolled_or_foreign_zero_is_not_trusted(tmp_path):
    broker, kw, _ = split_case(tmp_path)
    with closing(sqlite3.connect(kw['journal_path'])) as db:
        for account, identity in [('wrong', kw['permission'].journal_uuid),
                                  (broker.account_id, 'wrong')]:
            with pytest.raises(ValueError, match='enrollment'):
                risk.unused_strategy_pnl(db, account, identity)


def test_missing_bod_blocks_buy_before_dispatch(tmp_path):
    broker, kw, _ = split_case(tmp_path)
    broker.state = replace(broker.state, account_daily_risk=None)
    with pytest.raises(ValueError, match='IIS risk UNKNOWN'):
        execute_live_intent(**kw)
    assert not broker.calls


def test_owned_sell_does_not_require_daily_risk_or_yield(tmp_path):
    from moex_bot.live_execution import LiveHolding
    broker, kw, now = split_case(tmp_path)
    intent = replace(kw['intent'], side=Side.SELL)
    state = replace(broker.state, daily_pnl=D('NaN'), account_daily_risk=None,
                    positions={intent.instrument.uid: LiveHolding(
                        intent.instrument, 50, 50, D(100))})
    _authorize(intent, kw['policy'], state, {intent.instrument.uid: 50}, 0, D(0), now)


def m(value):
    return {'currency': 'rub', 'units': str(value), 'nano': 0}


def post_fake(*, equity=160000, cash=40000, operations=()):
    def post(method, payload):
        if method.endswith('GetPortfolio'):
            return {'totalAmountPortfolio': m(equity), 'positions': [
                {'instrumentUid': 'manual', 'instrumentType': 'share',
                 'quantity': {'units': '10'}}]}
        if method.endswith('GetPositions'):
            return {'money': [m(cash)]}
        if method.endswith('GetOperationsByCursor'):
            return {'items': list(operations), 'hasNext': False}
        raise AssertionError('No mutation permitted')
    return post


def op(kind, payment, identity='op'):
    return {'id': identity, 'date': (NOW-timedelta(minutes=1)).isoformat(),
            'type': 'OPERATION_TYPE_'+kind, 'state': 'OPERATION_STATE_EXECUTED',
            'payment': m(payment)}


@pytest.mark.parametrize('operation,cash,equity,pnl', [
    (op('INPUT', 10000), 50000, 169000, '-1000'),
    (op('OUTPUT', -10000), 30000, 149000, '-1000'),
    (op('BROKER_FEE', -100), 39900, 158900, '-1100'),
    (op('WRITING_OFF_VARMARGIN', -1000), 39000, 159000, '-1000'),
    (op('DIVIDEND', 100), 40100, 159100, '-900'),
])
def test_external_flows_not_income_and_costs_stay_pnl(monkeypatch, operation,cash,equity,pnl):
    base = reader.baseline(post_fake(), 'real-iis', lambda: BOD)
    monkeypatch.setattr(reader, 'load_basis', lambda *args: base)
    result = reader.read_risk(post_fake(equity=equity, cash=cash, operations=[operation]),
                              'real-iis', lambda: NOW, Path('.'))
    assert result.pnl == D(pnl)


def test_late_deposit_cannot_hide_loss(monkeypatch):
    base = reader.baseline(post_fake(), 'real-iis', lambda: BOD)
    monkeypatch.setattr(reader, 'load_basis', lambda *args: base)
    with pytest.raises(ValueError, match='cash bridge mismatch'):
        reader.read_risk(post_fake(cash=50000, equity=169000), 'real-iis', lambda: NOW, Path('.'))


@pytest.mark.parametrize('operation', [op('BUY', -100), op('INPUT', -100),
    op('OUTPUT', 100), op('UNKNOWN', 0), op('INPUT', 100) | {'date': BOD.isoformat()},
    op('INPUT', 100) | {'state': 'OPERATION_STATE_CANCELED'},
    op('INPUT', 100) | {'payment': {'currency': 'usd', 'units': '100'}},
    op('INPUT', 100) | {'date': (NOW+timedelta(seconds=1)).isoformat()},
])
def test_ambiguous_operations_fail(operation):
    with pytest.raises(ValueError):
        reader.cash_bridge(post_fake(operations=[operation]), 'real-iis', BOD, NOW)


def test_cursor_cycle_and_conflicting_duplicates_fail():
    for responses in [
        [{'hasNext': True, 'nextCursor': 'a'}, {'hasNext': True, 'nextCursor': 'a'}],
        [{'items': [op('INPUT', 100), op('INPUT', 101)]}],
    ]:
        with pytest.raises(ValueError):
            reader.cash_bridge(Mock(side_effect=responses), 'real-iis', BOD, NOW)
    assert reader.cash_bridge(post_fake(operations=[op('INPUT',100)]*2),
                              'real-iis', BOD, NOW)[:2] == (D(100), D(100))


@pytest.mark.parametrize('during', [True, False])
def test_net_zero_late_cash_postings_never_imply_zero_pnl(monkeypatch, during):
    base = reader.baseline(post_fake(), 'real-iis', lambda: BOD)
    monkeypatch.setattr(reader, 'load_basis', lambda *args: base)
    regular = post_fake()
    calls = 0

    def post(method, payload):
        nonlocal calls
        if method.endswith('GetOperationsByCursor'):
            calls += 1
            if calls == 2:
                at = NOW if during else NOW-timedelta(minutes=1)
                return {'items': [op('INPUT', 10000) | {'date': at.isoformat()},
                                  op('TAX', -10000, 'tax') | {'date': at.isoformat()}]}
        return regular(method, payload)

    with pytest.raises(ValueError, match='during NAV|operations changed'):
        reader.read_risk(post, 'real-iis', lambda: NOW, Path('.'))


def test_baseline_never_backdated_and_integrity_checked():
    with pytest.raises(ValueError, match='start-of-day'):
        reader.baseline(post_fake(), 'real-iis', lambda: NOW)
    base = reader.baseline(post_fake(), 'real-iis', lambda: BOD)
    for update in [{'equity': '200000'}, {'account_sha256': 'f'*64},
                   {'observed_at': (BOD+timedelta(days=1)).isoformat()}]:
        with pytest.raises(ValueError):
            risk.validate_basis(base | update, 'real-iis', NOW)


def test_baseline_changed_cash_or_inventory_rejected():
    a = reader.frame(post_fake(), 'real-iis')
    for changed in [a | {'rub_cash': '40001'}, a | {'inventory': {}},
                    a | {'currencies': {'USD': '1'}}]:
        with pytest.raises(ValueError, match='changed'):
            reader.stable(a, changed)


def test_store_exclusive_and_reload_validates_integrity(tmp_path, monkeypatch):
    # Permission checks tested separately: temp pytest parents intentionally aren't root-only.
    monkeypatch.setattr(risk, 'trusted_path', lambda *args: tmp_path/'basis.json')
    if os.name != 'nt':
        monkeypatch.setattr(os, 'fchown', lambda *args: None)
    base = reader.baseline(post_fake(), 'real-iis', lambda: BOD)
    path = risk.store_basis(tmp_path, 'real-iis', base, BOD)
    with pytest.raises(FileExistsError):
        risk.store_basis(tmp_path, 'real-iis', base, BOD)
    # load's permission trust deliberately remains tested against the actual owner.
    if os.name == 'nt' or os.geteuid() == 0:
        assert risk.load_basis(tmp_path, 'real-iis', NOW) == base
        path.write_text(json.dumps(base | {'sha256': '0'*64}))
        with pytest.raises(ValueError):
            risk.load_basis(tmp_path, 'real-iis', NOW)


def test_basis_hash_and_trusted_directory(tmp_path):
    base = reader.baseline(post_fake(), 'real-iis', lambda: BOD)
    assert base['sha256'] == hashlib.sha256(risk.canonical(
        {k:v for k,v in base.items() if k != 'sha256'})).hexdigest()
    if os.name != 'nt':
        with pytest.raises(ValueError, match='untrusted'):
            risk.trusted_path(tmp_path, 'real-iis', NOW)


@pytest.mark.parametrize('raw', [
    {'units': '1.5'}, {'units': True}, {'units': 'NaN'}, {'nano': 1000000000},
    {'units': '1', 'nano': -1}, {'units': str(2**63)}, {}, {'units': None},
])
def test_malformed_broker_quotation_never_normalized(raw):
    with pytest.raises(ValueError):
        reader.quotation(raw)


@pytest.mark.parametrize('raw', [{'units': '10'}, {'currency': 'usd', 'units': '10'},
                                {'currency': 'rub', 'units': '1.5'}])
def test_money_requires_explicit_rub_and_valid_components(raw):
    with pytest.raises(ValueError):
        reader.money(raw)


def test_adapter_failure_is_unknown_not_zero_and_sell_skips_reader(monkeypatch, tmp_path):
    from test_tinvest_live import Transport, _broker, _intent

    from moex_bot.integrations import tinvest_live
    transport = Transport()
    broker = _broker(transport)
    broker.daily_risk_directory = tmp_path
    failing = Mock(side_effect=ValueError('cash bridge mismatch'))
    monkeypatch.setattr(tinvest_live, 'read_risk', failing)
    state = broker.capture(_intent())
    assert state.account_daily_risk is None and state.daily_pnl.is_nan()
    assert state.account_risk_reason == 'cash bridge mismatch'
    transport.responses['GetInstrumentBy']['instrument']['sellAvailableFlag'] = True
    broker.capture(replace(_intent(), side=Side.SELL))
    assert failing.call_count == 1
