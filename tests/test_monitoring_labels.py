import pytest

from moex_bot.monitoring_labels import (
    API_LABELS,
    check_action,
    check_description,
    monitor_description,
)


@pytest.mark.parametrize("key", API_LABELS)
def test_api_labels_identify_exact_source_without_secret_urls(key):
    title, host = API_LABELS[key]
    assert title in check_description(key)
    assert host in check_description(key)
    rendered = monitor_description("MOEX / " + key)
    assert title in rendered and host in rendered
    assert "Действие" in rendered
    assert "https://" not in rendered


@pytest.mark.parametrize("key,expected", [
    ("disk:/", "SERVER"), ("inodes:/", "SERVER"), ("memory:available", "SERVER"),
    ("collector:resources", "SERVER"), ("api:geo_cbr_press", "cbr_press"),
    ("timer:intraday", "Планировщик"), ("service:shadow", "Планировщик"),
    ("data:calendar", "Данные"),
])
def test_check_category(key, expected):
    assert expected in check_description(key)


@pytest.mark.parametrize("key,expected", [
    ("api:tinvest_sandbox", "SANDBOX"), ("api:tinvest_prod", "PROD"),
    ("api:moex_iss", "HTTP"), ("disk:/", "df -h"),
    ("inodes:/", "df -i"), ("memory:available", "free -h"),
    ("timer:shadow", "systemctl"), ("service:intraday", "journalctl"),
    ("data:calendar", "diagnose"),
])
def test_action_is_specific(key, expected):
    assert expected in check_action(key)


@pytest.mark.parametrize("name,expected", [
    ("MOEX / Resources", "SERVER"), ("Ресурсы", "RAM"),
    ("Sync / HTTPS", "Syncthing"), ("Minio health", "MinIO S3"),
    ("S3 public", "INFRA"), ("MOEX / API integrations", "legacy"),
    ("MOEX / Trading data", "свежесть"), ("MOEX / Timers and services", "systemd"),
    ("MOEX / Observer heartbeat", "Push"), ("Unknown", "dashboard"),
])
def test_kuma_category(name, expected):
    assert expected in monitor_description(name)


def test_safe_failure_details(tmp_path):
    import json
    from datetime import UTC, datetime

    from moex_bot.monitoring_labels import safe_failure_details
    now=datetime(2026,10,31,tzinfo=UTC)
    path=tmp_path/'checks.json'
    checks={
        'api:geo_cbr_press':'FAIL: HTTP 503',
        'api:geo_moex_risk':'FAIL: HTTP 502',
        'api:tinvest_prod':'FAIL: HTTP 401',
        'api:tinvest_sandbox':'FAIL: missing token',
        'api:calendar':'FAIL: stale',
        'data:long':'FAIL: storage/read error',
        'quality:long':'FAIL: quality gate blocked',
        'timer:intraday':'FAIL: active=inactive, no schedule',
        'disk:/':'CRITICAL: free=1.00 GiB (1.0%)',
        'api:moex_iss':'FAIL: SECRET https://botTOKEN',
        'data:intraday':'OK',
        'api:SECRET_KEY':'FAIL: SECRET',
    }
    path.write_text(json.dumps({'observed_at':now.isoformat(),'checks':checks}))
    details=safe_failure_details('MOEX Trading / All API sources',path,now)
    assert 'HTTP 401' in details and 'токен отсутствует' in details
    assert 'api:geo_cbr_press: HTTP 503' in details
    assert 'api:geo_moex_risk: HTTP 502' in details
    assert 'SECRET' not in details and 'TOKEN' not in details
    assert 'timer:intraday' in safe_failure_details('MOEX Trading / Timers',path,now)
    assert 'disk:/' in safe_failure_details('Server / VM disk',path,now)
    assert 'quality:long' in safe_failure_details('MOEX Trading / Data freshness',path,now)
    checks.update({f'{kind}:{unit}':'FAIL: active=inactive, no schedule'
        for kind in ('timer','service') for unit in
        ('shadow','intraday','health','daily-report','maintenance','calendar')})
    checks['data:calendar']='FAIL: storage/read error'
    path.write_text(json.dumps({'observed_at':now.isoformat(),'checks':checks}))
    data_details=safe_failure_details('MOEX Trading / Data freshness',path,now)
    assert 'data:calendar' in data_details
    assert 'api:' not in data_details and 'timer:' not in data_details
    assert 'api:tinvest_prod' not in safe_failure_details(
        'MOEX Trading / T-Invest [api:tinvest_sandbox]',path,now)
    assert safe_failure_details('Unknown',tmp_path/'missing',now)==''
    path.write_text('{}')
    assert safe_failure_details('Unknown',path,now)==''
    path.write_text('{"observed_at":"2020-01-01T00:00:00Z","checks":{}}')
    assert safe_failure_details('Unknown',path,now)==''
