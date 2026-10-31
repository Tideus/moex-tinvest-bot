from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from moex_bot import reporting as r
from moex_bot.domain import (
    GeoRiskLevel,
    GeoRiskSnapshot,
    Instrument,
    OrderIntent,
    OrderRecord,
    OrderStatus,
    PortfolioSnapshot,
    Side,
    Target,
)
from moex_bot.harness import FlowDiagnostic, HarnessResult
from moex_bot.quality import QualityReport

NOW=datetime(2026,10,30,10,tzinfo=UTC)


def test_flow_and_window():
    for buy,sell,state in [(0,0,r.FlowState.BALANCED),(10,90,r.FlowState.SELL_DOMINANT),
                           (90,10,r.FlowState.BUY_DOMINANT)]:
        equity=r.EquityFlowSnapshot('SBER',NOW,NOW,D(buy),D(sell),1)
        assert equity.state==state
        futoi=r.FutoiSnapshot('Si',NOW,(r.FutoiGroupSnapshot('YUR',D(1),D(2),D(1),1,1),))
        assert 'YUR' in r.render_flow_report(equity,futoi,
            r.ConcentrationSnapshot('SBER',NOW,{'hhi':D('.5')}))
        assert 'нет доступных метрик' in r.render_flow_report(equity,None,
            r.ConcentrationSnapshot('SBER',NOW,{}))
    with pytest.raises(ValueError,match='timezone'):
        r.completed_window(NOW.replace(tzinfo=None),5)
    with pytest.raises(ValueError,match='positive'):
        r.completed_window(NOW,0)
    assert r.completed_window(NOW.replace(minute=3),5)[1].minute==0
    assert r.completed_window(NOW,5)[1].minute==55
    assert r.normalized_groups(futoi.groups)==futoi.groups
    assert r._rationale_metrics('momentum=bad;price=1>trend=1') is None
    assert r._rationale_metrics('malformed') is None
    assert r._next_rebalance_window(NOW,()) is None


def test_shadow_variants():
    instrument=Instrument('SBER','uid','TQBR',1,D('.01'))
    result=HarnessResult('test',QualityReport(False,('bad data',),()),
        GeoRiskSnapshot(GeoRiskLevel.NORMAL,D(1),frozenset(),()),(),(),())
    assert 'Причины блокировки' in r.render_shadow_report(result,NOW)
    portfolio=PortfolioSnapshot(cash=D(1000),positions={},reported_equity=None)
    flow=FlowDiagnostic('SBER',NOW,D('.5'),D('.4'),D('.3'),D(1),False,('book blocked',))
    intents=tuple(OrderRecord(OrderIntent(str(i),instrument,side,1,D(100),D(100),why,
                    confirm_margin_trade=margin),OrderStatus.VALIDATED)
        for i,(side,why,margin) in enumerate([(Side.SELL,'short',True),
            (Side.BUY,'direction=short',False),(Side.SELL,'exit target',False)]))
    result=replace(result,quality=QualityReport(True,(),()),portfolio=portfolio,
        flow_diagnostics=(flow,),targets=(Target('SBER',D('-.1'),
            'direction=short;momentum=-.01;price=100<trend=110'),),orders=intents,
        rejected=({'side':'buy','secid':'SBER','reasons':'other'},))
    report=r.render_shadow_report(result,NOW,environment_label='PROD OBSERVER')
    assert 'OPEN SHORT' in report and 'COVER SHORT' in report and 'CLOSE LONG' in report
    assert 'не передан' in report and 'book blocked' in report
    result=replace(result,orders=())
    assert 'Нет сделок, прошедших' in r.render_shadow_report(result,NOW)
    huge=replace(result,rejected=({'reasons':'x'*10000},))
    assert len(r.render_shadow_report(huge,NOW))<4096


@pytest.mark.parametrize('updates,message',[
    ({'quality':None},'quality/geo'),({'targets':None},'decision sections'),
    ({'targets':[1]},'target entry'),({'market':{'invalid':True}},'market section'),
    ({'market':[1]},'market entry'),({'orders':[1]},'order entry'),
    ({'orders':[{'intent':{}}]},'no instrument'),({'rejected':[1]},'rejected entry')])
def test_persisted_invalid_decisions(updates,message):
    raw={'quality':{},'geo':{},'targets':[],'orders':[],'rejected':[],**updates}
    with pytest.raises(ValueError,match=message):
        r.render_persisted_shadow_decisions(raw)


def test_persisted_empty_and_string_rejection():
    report=r.render_persisted_shadow_decisions({'quality':{},'geo':{},'targets':[],
        'orders':[],'rejected':[{'secid':'SBER','reasons':'not enough cash'}]})
    assert 'не выбрала' in report and 'not enough cash' in report
    assert 'ОТКЛОНЕНО' in r.render_persisted_shadow_decisions({'quality':{},'geo':{},
        'targets':[],'orders':[],'rejected':[]})


@pytest.mark.parametrize('hours',[(),(0,),(10,),(23,),(14,7,19),(10,10)])
@pytest.mark.parametrize('hour',[0,7,10,19,23])
def test_next_window_matches_reference(hours,hour):
    for zone in (UTC,r.MOSCOW):
        now=NOW.replace(hour=hour,tzinfo=zone)
        local=now.astimezone(r.MOSCOW)
        expected=None
        for offset in range(2):
            for target in sorted(hours):
                candidate=datetime.combine((local+timedelta(days=offset)).date(),
                    datetime.min.time().replace(hour=target),tzinfo=r.MOSCOW)
                if candidate>local:
                    expected=candidate.strftime('%d.%m.%Y · %H:00–%H:59 МСК')
                    break
            if expected is not None:
                break
        assert r._next_rebalance_window(now,hours)==expected


def test_invalid_schedule_hour_rejected():
    with pytest.raises(ValueError):
        r._next_rebalance_window(NOW,(25,))
