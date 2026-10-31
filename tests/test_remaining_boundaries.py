import json
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_risk import _config

from moex_bot import daily_report as daily
from moex_bot import env_file, runtime_config, service_config, shadow, validation
from moex_bot.domain import (
    GeoRiskLevel,
    GeoRiskSnapshot,
    Instrument,
    OrderIntent,
    PortfolioSnapshot,
    Side,
)
from moex_bot.event_calendar import EventStore
from moex_bot.risk import evaluate_intent

ROOT=Path(__file__).resolve().parents[1]


def test_env_posix_preserves_mode_owner_and_cleans_failed_replace(tmp_path,monkeypatch):
    path=tmp_path/'bot.env'
    path.write_text('TOKEN=old\n')
    ownership=[]
    monkeypatch.setattr(env_file,'os',SimpleNamespace(name='posix',
        chown=lambda *args:ownership.append(args)))
    env_file.upsert_env_value(path,'TOKEN','new')
    assert ownership and path.read_text()=='TOKEN=new\n'
    env_file.upsert_env_value(tmp_path/'new.env','TOKEN','new')
    def denied(*args):
        raise PermissionError()
    monkeypatch.setattr(env_file.os,'chown',denied)
    env_file.upsert_env_value(path,'TOKEN','third')
    monkeypatch.setattr(Path,'replace',denied)
    with pytest.raises(PermissionError):
        env_file.upsert_env_value(path,'TOKEN','fourth')
    assert path.read_text()=='TOKEN=third\n'
    assert not list(tmp_path.glob('.*.tmp'))


def test_env_refuses_symlink(tmp_path,monkeypatch):
    path=tmp_path/'env'
    path.write_text('TOKEN=old')
    monkeypatch.setattr(Path,'is_symlink',lambda _:True)
    with pytest.raises(ValueError,match='symlink'):
        env_file.upsert_env_value(path,'TOKEN','new')


def test_report_filters_non_shadow_and_empty(tmp_path):
    path=tmp_path/'other.json'
    path.write_text('{"run_id":"intraday-test"}')
    result=daily.summarize_shadow_artifacts([path],report_date=date(2026,10,30),timezone='UTC')
    assert result.cycles==0
    assert 'нет прошедших' in daily.render_daily_shadow_report(result)


def test_calendar_invalid_rows_and_session_preservation(tmp_path):
    store=EventStore(tmp_path/'events.db')
    now=datetime(2026,10,30,tzinfo=UTC)
    with pytest.raises(ValueError,match='invalid calendar response'):
        store.record('report','uid','SBER',{'events':[None]},now)
    store.record('session','','',{'exchanges':[{'exchange':'MOEX'}]},now)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0]==1


def test_risk_excess_order_and_unconfirmed_short():
    instrument=Instrument('SBER','uid','TQBR',1,D('.01'))
    intent=OrderIntent('request',instrument,Side.SELL,1,D(1000000),D(1000000),'short',
                       confirm_margin_trade=False)
    geo=GeoRiskSnapshot(GeoRiskLevel.NORMAL,D(1),frozenset(),())
    result=evaluate_intent(intent,PortfolioSnapshot(D(300000),{}),_config(),geo,
                           equity=D(300000),gross_exposure=D(0))
    assert 'order notional exceeds limit' in result.reasons
    assert 'short order requires explicit margin confirmation' in result.reasons


def test_schedule_concurrent_replacement_guard(tmp_path,monkeypatch):
    path=tmp_path/'runtime.json'
    path.write_text('{"t_invest_environment":"sandbox"}')
    original=Path.read_text
    calls=0
    def read(file,*a,**k):
        nonlocal calls
        if file==path:
            calls+=1
            if calls==1:
                return '{"t_invest_environment":"sandbox","schedule":[]}'
        return original(file,*a,**k)
    monkeypatch.setattr(Path,'read_text',read)
    with pytest.raises(ValueError,match='schedule must'):
        runtime_config.materialize_runtime_defaults(path)


def test_endpoint_requires_exact_official_host():
    with pytest.raises(ValueError,match='approved official'):
        service_config._require_exact('evil.example','official.example','api')


def test_shadow_schema_and_position_mapping(tmp_path):
    path=tmp_path/'universe.json'
    path.write_text('[]')
    with pytest.raises(ValueError,match='non-empty'):
        shadow.load_universe(path)
    rows=json.loads((ROOT/'config/universe.json').read_text())
    rows.append(rows[0])
    path.write_text(json.dumps(rows))
    with pytest.raises(ValueError,match='duplicate SECID'):
        shadow.load_universe(path)
    portfolio=tmp_path/'portfolio.json'
    portfolio.write_text('{"cash":"1000","positions":{"SBER":{"lots":2,"blocked_lots":1}}}')
    universe={'SBER':SimpleNamespace(instrument=Instrument('SBER','uid','TQBR',1,D('.01')))}
    snapshot=shadow.load_portfolio(portfolio,universe)
    assert snapshot.positions['SBER'].blocked_lots==1


def test_prod_policy_cannot_reference_unverified_universe(monkeypatch):
    monkeypatch.setattr(validation,'load_universe',lambda _:(SimpleNamespace(secid='SBER'),))
    with pytest.raises(ValueError,match='verified universe'):
        validation.validate_project_configs(ROOT)
