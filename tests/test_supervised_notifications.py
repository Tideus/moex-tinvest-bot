import sqlite3
from datetime import UTC, datetime

import pytest

from moex_bot import supervised_notifications as m
from moex_bot.notifications import SQLiteOutbox

NOW=datetime(2026,10,31,tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(m,'TRADE_OUTBOX',tmp_path/'trade.db')
    monkeypatch.setattr(m,'ALERT_OUTBOX',tmp_path/'relay.db')
    for key in ('TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID','TELEGRAM_ALERT_CHAT_ID',
                'T_INVEST_PROD_TOKEN','T_INVEST_PROD_ACCOUNT_ID'):
        monkeypatch.delenv(key,raising=False)


def test_critical_only_queues_independent_relay(monkeypatch):
    monkeypatch.setattr(m,'TelegramBotApiSender',lambda _:pytest.fail('no critical network'))
    result=m.notify_experiment('CRITICAL check account',dedupe_key='critical',critical=True,now=NOW)
    assert result=={'queued':True,'sent':0,'failed':0}
    with sqlite3.connect(m.ALERT_OUTBOX) as db:
        row=db.execute('SELECT kind,status FROM notification_outbox').fetchone()
        assert row==('monitoring','pending')
    assert not m.TRADE_OUTBOX.exists()


def test_normal_without_credentials_stays_queued_and_deduplicates():
    kwargs=dict(dedupe_key='normal',critical=False,now=NOW)
    assert m.notify_experiment('event',**kwargs)['queued'] is True
    assert m.notify_experiment('event',**kwargs)['queued'] is False
    assert SQLiteOutbox(m.TRADE_OUTBOX).counts()=={'pending':1}


@pytest.mark.parametrize('fail',[False,True])
def test_normal_delivery_is_mocked_and_errors_safe(monkeypatch,fail):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','PRIVATE_TOKEN')
    monkeypatch.setenv('TELEGRAM_CHAT_ID','chat')
    monkeypatch.setenv('TELEGRAM_ALERT_CHAT_ID','alerts')
    class Sender:
        def send_text(self,chat,text):
            assert chat=='chat'
            assert 'PRIVATE_TOKEN' not in text
            if fail:
                raise OSError('PRIVATE_TOKEN https://secret')
    monkeypatch.setattr(m,'TelegramBotApiSender',lambda _:Sender())
    result=m.notify_experiment('done PRIVATE_TOKEN',dedupe_key='send',critical=False,now=NOW)
    assert result=={'queued':True,'sent':0 if fail else 1,'failed':1 if fail else 0}
    assert 'PRIVATE_TOKEN' not in repr(result)
    with sqlite3.connect(m.TRADE_OUTBOX) as db:
        body,error=db.execute('SELECT body,last_error FROM notification_outbox').fetchone()
        assert 'PRIVATE_TOKEN' not in body and 'PRIVATE_TOKEN' not in str(error)


def test_sender_construction_error_does_not_erase_queue(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','secret')
    monkeypatch.setenv('TELEGRAM_CHAT_ID','chat')
    def broken(_):
        raise RuntimeError('secret')
    monkeypatch.setattr(m,'TelegramBotApiSender',broken)
    assert m.notify_experiment('ok',dedupe_key='x',critical=False,now=NOW)=={
        'queued':True,'sent':0,'failed':1}
    assert SQLiteOutbox(m.TRADE_OUTBOX).counts()=={'pending':1}


def test_storage_error_is_isolated(monkeypatch):
    def broken(_):
        raise sqlite3.OperationalError('private-path')
    monkeypatch.setattr(m,'SQLiteOutbox',broken)
    assert m.notify_experiment('ok',dedupe_key='x',critical=True,now=NOW)=={
        'queued':False,'sent':0,'failed':1}


@pytest.mark.parametrize('body,key,now',[
    ('','key',NOW),('event','',NOW),('event','key',NOW.replace(tzinfo=None))])
def test_invalid_event_never_raises(body,key,now):
    assert m.notify_experiment(body,dedupe_key=key,critical=False,now=now)['failed']==1


def test_long_body_bounded_and_account_redacted(monkeypatch):
    monkeypatch.setenv('T_INVEST_PROD_ACCOUNT_ID','full-private-account')
    m.notify_experiment('full-private-account'+'x'*5000,dedupe_key='long',critical=True,now=NOW)
    event=SQLiteOutbox(m.ALERT_OUTBOX).claim(now=NOW)[0]
    assert len(event.body)==4096 and 'full-private-account' not in event.body
