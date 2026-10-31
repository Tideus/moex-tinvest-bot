import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from moex_bot import intraday_performance as ip
from moex_bot import performance as p


def save(tmp_path, name, raw):
    path=tmp_path/name
    path.write_text(json.dumps(raw),encoding='utf-8')
    return path


def test_intraday_roundtrip_reports_and_filters(tmp_path):
    day=date(2026,10,30)
    start=datetime(2026,10,30,8,tzinfo=UTC)
    snapshots=[save(tmp_path,'p1.json',{'observed_at':start.isoformat(),
        'reported_equity':'1000','positions':{}}),
        save(tmp_path,'p2.json',{'observed_at':(start+timedelta(hours=5)).isoformat(),
        'reported_equity':'1108','positions':{'SBER':{'lots':1}}})]
    operations=[p.TradeOperation('1',start,'SBER','BUY',1,D(100),D(1)),
        p.TradeOperation('2',start,'SBER','SELL',1,D(110),D(1)),
        p.TradeOperation('3',start,None,None,0,D(0),D(0),D(100))]
    plans=[save(tmp_path,'plan.json',{'run_id':'intraday-'+start.isoformat(),
        'signals':[{}],'orders':[{}],'phase':'force_flat','quality':{'passed':True}}),
        save(tmp_path,'skip.json',{'run_id':'other'}),
        save(tmp_path,'old.json',{'run_id':'intraday-'+(start-timedelta(days=1)).isoformat()}),
        save(tmp_path,'bad-list.json',{'run_id':'intraday-'+start.isoformat(),
                                     'signals':None,'orders':None,'quality':None})]
    result=ip.summarize_intraday_performance(snapshots,plans,operations,
        report_date=day,timezone='Europe/Moscow')
    assert result.pnl==D(8) and result.rows[0].cashflow_pnl==D(8)
    assert result.plan_cycles==2 and result.quality_failures==1 and result.forced_flat_cycles==1
    assert 'остаток 1' in ip.render_intraday_performance_report(result)
    assert 'Сверка' in ip.render_intraday_performance_report(replace(result,unattributed_pnl=D(1)))
    assert 'не было' in ip.render_intraday_performance_report(replace(result,rows=()))
    many=replace(result,rows=tuple(replace(result.rows[0],secid='X'*90) for _ in range(100)))
    assert len(ip.render_intraday_performance_report(many))<4096
    with pytest.raises(ValueError,match='two'):
        ip.summarize_intraday_performance([],[],[],report_date=day,timezone='UTC')
    zero=[save(tmp_path,'z1.json',{'observed_at':start.isoformat()}),
          save(tmp_path,'z2.json',{'observed_at':(start+timedelta(hours=1)).isoformat()})]
    summary=ip.summarize_intraday_performance(zero,[],[],report_date=day,timezone='UTC')
    assert summary.return_pct==0


@pytest.mark.parametrize('raw,message',[
    ([], 'JSON object'), ({'observed_at':'2026-10-30T10:00:00'},'timezone-aware'),
    ({'observed_at':'2026-10-30T10:00:00Z','positions':[]},'positions are invalid'),
    ({'observed_at':'2026-10-30T10:00:00Z','positions':{'SBER':1}},'position is invalid')])
def test_bad_intraday_snapshot(tmp_path,raw,message):
    with pytest.raises(ValueError,match=message):
        ip._portfolio_point(save(tmp_path,'bad.json',raw))


@pytest.mark.parametrize('raw,message',[(None,'invalid shadow artifact'),
    ({'run_id':'bad'},'invalid shadow run id'),
    ({'run_id':'shadow-2026-10-30T10:00:00Z'},'no portfolio/market'),
    ({'run_id':'shadow-2026-10-30T10:00:00Z','portfolio_input':{},'market':[None]},
     'invalid market item'),
    ({'run_id':'shadow-2026-10-30T10:00:00Z','portfolio_input':{'positions':[]},'market':[]},
     'invalid portfolio positions'),
    ({'run_id':'shadow-2026-10-30T10:00:00Z','portfolio_input':{'positions':{'X':1}},'market':[]},
     'invalid position')])
def test_bad_performance_snapshot(tmp_path,raw,message):
    with pytest.raises(ValueError,match=message):
        p._point(save(tmp_path,'bad.json',raw))


@pytest.mark.parametrize('raw,message',[(None,'invalid shadow artifact'),
    ({'run_id':'bad'},'invalid shadow run id'),
    ({'run_id':'shadow-2026-10-30T10:00:00'},'timezone-aware')])
def test_bad_performance_time(tmp_path,raw,message):
    with pytest.raises(ValueError,match=message):
        p._artifact_observed_at(save(tmp_path,'bad.json',raw))


@pytest.mark.parametrize('args,expected',[
    ((30,10,1,D(0),D(1)),'REVIEW_DATA'),
    ((30,10,0,D(3),D(1)),'REVIEW_RISK'),
    ((30,10,0,D(0),D(-1)),'OBSERVE'),
    ((30,10,0,D(0),D(1)),'CONTINUE_OOS')])
def test_review_verdicts(args,expected):
    assert p._verdict(*args).startswith(expected)


def test_account_boundaries_and_executed_costs(tmp_path):
    now=datetime(2026,10,30,8,tzinfo=UTC)
    raw={'run_id':'shadow-'+now.isoformat(),'portfolio_input':{
        'account_id':'one','reported_equity':'1000','positions':{'SBER':{'lots':1}}},
        'market':[{'instrument':{'secid':'SBER','lot_size':1},'price':'100'}],
        'quality':{'passed':True}}
    first=save(tmp_path,'first.json',raw)
    raw['run_id']='shadow-'+(now+timedelta(hours=2)).isoformat()
    raw['portfolio_input']['reported_equity']='1008'
    last=save(tmp_path,'last.json',raw)
    operations=[p.TradeOperation('1',now,'SBER','BUY',1,D(100),D(1)),
        p.TradeOperation('2',now,'SBER','SELL',1,D(110),D(1)),
        p.TradeOperation('deposit',now,None,None,0,D(0),D(0))]
    kwargs={'start_date':now.date(),'end_date':now.date(),'timezone':'UTC','label':'test'}
    summary=p.summarize_performance([first,last],operations,**kwargs)
    assert summary.rows[0].pnl==D(8)
    report=p.render_performance_report(replace(summary,rows=(),attribution_reliable=False),weekly=False)
    assert 'позиции и исполненные сделки отсутствуют' in report
    raw['portfolio_input']['account_id']='two'
    last=save(tmp_path,'last.json',raw)
    with pytest.raises(ValueError,match='different broker'):
        p.summarize_performance([first,last],[],**kwargs)
    with pytest.raises(ValueError,match='at least two'):
        p.summarize_performance([],[],**kwargs)
