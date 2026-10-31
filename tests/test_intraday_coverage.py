from collections import UserDict
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_intraday import MOSCOW, PROJECT_ROOT, _bars, _instrument, _portfolio

from moex_bot import intraday as m
from moex_bot.domain import Side
from moex_bot.intraday_config import load_intraday_config


@pytest.fixture
def setup(tmp_path):
    from datetime import datetime
    now=datetime(2026,8,20,10,25,tzinfo=MOSCOW)
    config=load_intraday_config(PROJECT_ROOT/'config/intraday.json')
    store=m.IntradayStateStore(tmp_path/'state.db')
    return now,config,store


def test_fill_reconstruction_validation_and_lifecycle():
    base={'operation_id':'1','secid':'SBER','side':'BUY','quantity':10,'gross':'1000',
          'occurred_at':'2026-08-20T10:00:00+03:00'}
    with pytest.raises(ValueError,match='list'):
        m.executed_entries({'executed_operations':{}})
    with pytest.raises(ValueError,match='invalid executed'):
        m.executed_entries({'executed_operations':[UserDict(base)]})
    for update in ({'quantity':0},{'gross':'0'},{'occurred_at':'2026-08-20T10:00:00'}):
        with pytest.raises(ValueError,match='fill'):
            m.executed_entries({'executed_operations':[{**base,**update}]})
    items=[base,base,{**base,'operation_id':'ignore','side':'DIVIDEND'}]
    assert m.executed_entries({'executed_operations':items})['SBER'][3]==10
    items.append({**base,'operation_id':'2'})
    assert m.executed_entries({'executed_operations':items})['SBER'][3]==20
    items.append({**base,'operation_id':'3','side':'SELL'})
    assert m.executed_entries({'executed_operations':items})['SBER'][3]==10
    items.append({**base,'operation_id':'4','side':'SELL','quantity':20,'gross':'2200'})
    assert m.executed_entries({'executed_operations':items})['SBER'][3]==-10
    items.append({**base,'operation_id':'5'})
    assert m.executed_entries({'executed_operations':items})=={}


def test_invalid_position_inputs():
    for raw in ([],{'SBER':1}):
        with pytest.raises(ValueError,match='object'):
            m._positions({'positions':raw})
    with pytest.raises(ValueError,match='object'):
        m._broker_position_prices({'positions':[]},{},{})
    for raw in (True,1.2,None):
        with pytest.raises(ValueError,match='integer'):
            m._integer_value(raw,'open_orders')
    assert m._flatten_orders({}, {'SBER':1}, {}, __import__('datetime').datetime.now())==()


def test_signal_quality(setup):
    now,config,store=setup
    bars=_bars(now-timedelta(minutes=10))
    assert m._signal(config,'SBER',bars,now)
    broken=(bars[0], replace(bars[1],observed_at=bars[1].observed_at+timedelta(minutes=1)),bars[2])
    assert m._signal(config,'SBER',broken,now) is None
    assert m._signal(config,'SBER',bars,now+timedelta(hours=1)) is None
    wide=(*bars[:-1],replace(bars[-1],book_imbalance=D('-1'),spread_bbo=D('5')))
    assert m._signal(config,'SBER',wide,now) is None
    signal=m._signal(config,'SBER',bars,now)
    assert signal
    store.mark_signal(signal)
    assert store.latest_signal(now.date().isoformat(),'SBER')[2]==signal.price


def test_build_validation_phases_and_risk(setup,tmp_path):
    now,config,store=setup
    instruments={'SBER':_instrument()}
    bars=_bars(now-timedelta(minutes=10))
    args=dict(config=config,instruments=instruments,portfolio=_portfolio(),store=store,
              bars=bars,as_of=now)
    for update,message in [({'as_of':now.replace(tzinfo=None)},'timezone'),
        ({'portfolio':{**_portfolio(),'reported_equity':'0'}},'equity'),
        ({'portfolio':{**_portfolio(),'open_orders':1}},None),
        ({'portfolio':_portfolio(positions={'SBER':{'lots':1}}),'bars':()},'without broker')]:
        if message:
            with pytest.raises(ValueError,match=message):
                m.build_intraday_plan(**{**args,**update})
        else:
            assert not m.build_intraday_plan(**{**args,**update}).quality_passed
    for hour in (9,19):
        plan=m.build_intraday_plan(**{**args,'as_of':now.replace(hour=hour),'bars':()})
        assert plan.quality_passed
        assert 'Заявок нет' in m.render_intraday_report(plan)
    with pytest.raises(ValueError,match='flatten'):
        m.build_intraday_plan(**{**args,'as_of':now.replace(hour=19),'bars':(),
             'portfolio':_portfolio(positions={'SBER':{'lots':1}})})
    low={**_portfolio(),'reported_equity':'290000'}
    assert m.build_intraday_plan(**{**args,'portfolio':low}).phase=='loss_limit_flat'
    shortbars=_bars(now-timedelta(minutes=10),falling=True)
    assert not m.build_intraday_plan(**{**args,'bars':shortbars,
        'store':m.IntradayStateStore(tmp_path/'short.db'),
        'config':replace(config,allow_short=False)}).orders


def test_managed_exit_variants(setup):
    now,config,store=setup
    instruments={'SBER':_instrument()}
    args=dict(config=config,instruments=instruments,positions={'SBER':1},
        prices={'SBER':D(102)},signals=(),store=store,trade_day=str(now.date()),as_of=now,
        fill_entries={'SBER':(now.replace(tzinfo=None),Side.BUY,D(100),1)})
    assert 'take_profit' in m._position_exit_orders(**args)[1][0]
    signal=m.IntradaySignal('SBER',Side.SELL,now,D(100),D('-.01'),D('-.5'),D('-.5'),D('-.5'),D(1))
    args.update(prices={'SBER':D(100)},signals=(signal,))
    assert 'signal_reversal' in m._position_exit_orders(**args)[1][0]
    args.update(positions={'SBER':0})
    assert m._position_exit_orders(**args)==((),())
