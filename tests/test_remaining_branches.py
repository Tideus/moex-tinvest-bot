import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

from test_risk import _config

from moex_bot import (
    env_file,
    geo_feed,
    intraday,
    performance,
    reporting,
    storage_maintenance,
    validation,
)
from moex_bot.domain import (
    GeoRiskLevel,
    GeoRiskSnapshot,
    Instrument,
    OrderIntent,
    PortfolioSnapshot,
    Position,
    Side,
    Target,
)
from moex_bot.harness import HarnessResult
from moex_bot.integrations.algopack_intraday import AlgoPackIntradayAdapter
from moex_bot.quality import QualityReport
from moex_bot.risk import evaluate_intent

NOW=datetime(2026,10,30,10,tzinfo=UTC)


def test_env_without_chown_and_zero_positions(tmp_path,monkeypatch):
    path=tmp_path/'env'
    path.write_text('X=old')
    monkeypatch.setattr(env_file,'os',SimpleNamespace(name='posix'))
    env_file.upsert_env_value(path,'X','new')
    assert path.read_text()=='X=new\n'
    assert intraday._positions({'positions':{'SBER':{'lots':0}}})=={}
    instrument=Instrument('SBER','uid','TQBR',1,D('.01'))
    assert intraday._broker_position_prices({'positions':{'SBER':{'current_value':'0'}}},
        {'SBER':instrument},{'SBER':1})=={}
    assert performance._max_drawdown((D(0),D(-1)))==0


def test_blocked_lots_do_not_block_small_reductions():
    instrument=Instrument('SBER','uid','TQBR',1,D('.01'))
    geo=GeoRiskSnapshot(GeoRiskLevel.NORMAL,D(1),frozenset(),())
    for lots,side in [(10,Side.SELL),(-10,Side.BUY)]:
        portfolio=PortfolioSnapshot(D(300000),{'SBER':Position(instrument,lots,blocked_lots=1)})
        intent=OrderIntent('reduce',instrument,side,1,D(100),D(100),'exit')
        result=evaluate_intent(intent,portfolio,_config(),geo,
            equity=D(300000),gross_exposure=D(1000))
        assert result.allowed


def test_empty_reference_and_optional_reports():
    equity=reporting.EquityFlowSnapshot('SBER',NOW,NOW,D(0),D(0),0)
    assert 'HI2' not in reporting.render_flow_report(equity,None,None)
    result=HarnessResult('x',QualityReport(True,(),()),
        GeoRiskSnapshot(GeoRiskLevel.NORMAL,D(1),frozenset(),()),
        (Target('SBER',D('.1'),'no structured rationale'),),(),(),rebalance_allowed=False)
    report=reporting.render_shadow_report(result,NOW)
    assert 'Следующее разрешённое окно' not in report


def test_old_backup_without_checksum(tmp_path):
    path=tmp_path/'moex-old.tar.gz'
    path.write_bytes(b'backup')
    old=(NOW-timedelta(days=60)).timestamp()
    os.utime(path,(old,old))
    assert storage_maintenance._old_backups(tmp_path,NOW,30,0)==[path]


def test_geo_discards_outside_lookback():
    source=geo_feed.GeoSource('cbr','https://cbr.ru/rss','primary')
    body=b'<rss><channel><item><title>old</title><link>https://cbr.ru/old</link>' \
        b'<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item></channel></rss>'
    payload,stale=geo_feed.build_geo_payload((source,),as_of=NOW,fetch=lambda _:body)
    assert payload['events']==[]


def test_zero_entry_basis_is_not_assumed_profit(tmp_path):
    from test_intraday import PROJECT_ROOT, _instrument

    from moex_bot.intraday_config import load_intraday_config
    orders,notes=intraday._position_exit_orders(
        config=load_intraday_config(PROJECT_ROOT/'config/intraday.json'),
        instruments={'SBER':_instrument()},positions={'SBER':1},prices={'SBER':D(100)},
        signals=(),store=intraday.IntradayStateStore(tmp_path/'state.db'),
        trade_day=NOW.date().isoformat(),as_of=NOW,
        fill_entries={'SBER':(NOW,Side.BUY,D(0),1)})
    assert orders==() and notes==()


def test_aligned_performance_reference_is_rendered(tmp_path):
    from test_performance import _artifact
    paths=[tmp_path/'a.json',tmp_path/'b.json']
    for path,hour,equity in zip(paths,(8,10),('1000','1100'),strict=True):
        _artifact(path,f'2026-10-30T{hour:02}:00:00Z',equity,'100',1,
                  price_observed_at='2026-10-30T00:00:00Z')
    summary=performance.summarize_performance(paths,[],start_date=NOW.date(),
        end_date=NOW.date(),timezone='UTC',label='test')
    assert summary.benchmark_aligned
    assert 'Разница к нему' in performance.render_performance_report(summary,weekly=False)


def test_valid_ownership_rows_complete_validation(tmp_path):
    source=Path(__file__).resolve().parents[1]
    shutil.copytree(source/'config',tmp_path/'config')
    shutil.copytree(source/'examples',tmp_path/'examples')
    item=dict(secid='SBER',holder_name='Fund',holder_type='fund',report_date='2026-01-01',
        published_at='2026-01-02T00:00:00Z',source_url='https://example.org/filing',
        source_kind='issuer',verified=True)
    (tmp_path/'config/ownership_disclosures.json').write_text(json.dumps([item]))
    assert 'config/ownership_disclosures.json' in validation.validate_project_configs(tmp_path)


def test_timeout_transient_retries_then_succeeds():
    calls=[]
    def get(*a,**k):
        calls.append(1)
        if len(calls)==1:
            raise TimeoutError()
        return {'data':{}}
    adapter=AlgoPackIntradayAdapter('test',SimpleNamespace(get=get),sleeper=lambda _:None)
    assert adapter._get_with_retry('https://example.test')=={'data':{}}


def test_latest_supercandle_ignores_older_common_windows(monkeypatch):
    adapter=AlgoPackIntradayAdapter('test',None)
    # Realistic full-session history: all three datasets share several completed bars.
    row={key:'1' for key in ('pr_close','pr_vwap','val_b','val_s','put_val_b','put_val_s',
         'cancel_val_b','cancel_val_s','spread_bbo','imbalance_val')}
    points={('SBER',NOW-timedelta(minutes=offset*5)):row for offset in range(20)}
    monkeypatch.setattr(adapter,'_day_rows',lambda *args:points)
    candles=adapter.latest(secids=['SBER'],as_of=NOW)
    assert len(candles)==1 and candles[0].observed_at==NOW
