import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from urllib.error import URLError

import pytest

from moex_bot import news_observer as n
from moex_bot.monitoring_labels import check_description, compact_checks

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
SETTINGS = n.load_settings(ROOT / "config/news_observer.json")


def page(*rows):
    return json.dumps({"sitenews": {"columns": ["id", "title", "published_at"],
                                  "data": list(rows)}}).encode()


def rss(identity=1, title="Notice", published="Tue, 10 Nov 2026 14:00:00 +0300"):
    return (f'<rss><channel><item><title>{title}</title>'
            f'<link>https://www.moex.com/n{identity}</link><pubDate>{published}</pubDate>'
            '</item></channel></rss>').encode()


def feeds(url, timeout):
    if url == n.RSS:
        return rss()
    if url.endswith("start=0"):
        return page([1, "Notice", "2026-11-10 14:00:00"])
    return page()


def test_first_load_matches_reference_but_is_not_delivery_latency(tmp_path):
    result = n.observe(tmp_path, SETTINGS, fetch=feeds, clock=lambda: NOW)
    assert result["execution_allowed"] is False
    assert result["iss"]["window_covered"] is True
    assert [p["start"] for p in result["iss"]["pages"]] == [0, 1]
    assert result["comparison"]["status"] == "MATCH"
    assert result["comparison"]["reference_items"] == 1
    assert result["comparison"]["reference_records"][0]["id"] == 1
    assert result["news"][0]["published_at"] == "2026-11-10T11:00:00+00:00"
    assert result["discovery"]["baseline_new"] == 1
    assert result["discovery"]["median_seconds"] is None
    again = n.observe(tmp_path, SETTINGS, fetch=feeds, clock=lambda: NOW + timedelta(minutes=5))
    assert again["news"][0]["first_seen_at"] == result["news"][0]["first_seen_at"]
    assert again["discovery"]["forward_new"] == 0
    with sqlite3.connect(tmp_path / "news.sqlite3") as db:
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 2


def test_new_news_records_observed_delay_and_excludes_old_backfill(tmp_path):
    n.observe(tmp_path, SETTINGS, fetch=feeds, clock=lambda: NOW)
    def fetch(url, timeout):
        if url == n.RSS:
            raise URLError(TimeoutError("secret"))
        return page([2, "New", "2026-11-10 15:02:00"],
                    [1, "Notice", "2026-11-10 14:00:00"],
                    [3, "Old late arrival", "2026-11-10 13:00:00"]) if url.endswith(
                        "start=0") else page()
    result = n.observe(tmp_path, SETTINGS, fetch=fetch, clock=lambda: NOW + timedelta(minutes=5))
    assert result["iss"]["status"] == "OK"
    assert result["rss"] == {"status": "FAIL", "reason": "timeout"}
    assert result["comparison"]["status"] == "UNAVAILABLE"
    assert result["discovery"]["forward_new"] == 1
    assert result["discovery"]["delays_seconds"] == [180]
    assert result["discovery"]["historical_backfill_new"] == 1
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("fault", ["timeout", "schema", "repeated", "future", "page_limit"])
def test_incomplete_or_failed_iss_does_not_mark_partial_results_complete(tmp_path, fault):
    def fetch(url, timeout):
        if url == n.RSS:
            return rss()
        if fault == "timeout":
            raise TimeoutError("secret")
        if fault == "schema":
            return b'<html/>'
        if fault == "future":
            return page([1, "Future", "2026-11-11 15:00:00"])
        return page([1, "Notice", "2026-11-10 14:00:00"])
    result = n.observe(tmp_path, {**SETTINGS, "max_pages": 1 if fault == "page_limit" else 3},
                       fetch=fetch, clock=lambda: NOW)
    assert result["iss"]["status"] == "FAIL"
    assert result["iss"]["window_covered"] is False
    assert result["rss"]["status"] == "OK"
    assert result["comparison"]["status"] == "UNAVAILABLE"
    assert result["news"] == []
    with sqlite3.connect(tmp_path / "news.sqlite3") as db:
        assert db.execute('SELECT count(*) FROM news').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM meta').fetchone()[0] == 0


@pytest.mark.parametrize("reference,status", [
    (rss(identity=5), "DIFFERENCES"), (rss(title="Changed"), "DIFFERENCES"),
    (rss(published="Tue, 10 Nov 2026 13:00:00 +0300"), "DIFFERENCES"),
    (b'<rss><channel/></rss>', "NO_REFERENCE_ITEMS"),
])
def test_comparison_is_not_vacuous_success(reference, status):
    news = n.parse_iss(page([1, "Notice", "2026-11-10 14:00:00"]))
    assert n.compare_rss(reference, news, NOW-timedelta(days=2), NOW)["status"] == status


@pytest.mark.parametrize("content", [b'broken', b'<html/>',
                                     b'<rss><channel><item><title>Missing date</title>'
                                     b'</item></channel></rss>'])
def test_bad_rss_cannot_make_iss_bad_or_completeness_green(tmp_path, content):
    result = n.observe(tmp_path, SETTINGS,
                       fetch=lambda u,t: content if u == n.RSS else feeds(u,t), clock=lambda: NOW)
    assert result["iss"]["status"] == "OK"
    assert result["rss"]["status"] == "FAIL"
    assert result["comparison"]["status"] == "UNAVAILABLE"


def test_monitoring_distinguishes_rss_and_iss_and_stale_observer(tmp_path):
    assert n.evidence(tmp_path, NOW)["api:news_iss_observer"].startswith("FAIL")
    n.observe(tmp_path, SETTINGS, fetch=feeds, clock=lambda: NOW)
    assert n.evidence(tmp_path, NOW)["api:news_iss_observer"] == "OK"
    assert n.evidence(tmp_path, NOW+timedelta(minutes=16))[
        "api:news_iss_observer"].startswith("FAIL")
    checks = {"api:geo_moex_risk": "FAIL: probe timeout (10.0s)",
              "api:news_iss_observer": "OK"}
    assert 'api:geo_moex_risk' in compact_checks(checks)
    assert 'ISS' in check_description('api:news_iss_observer')
    assert 'RSS' in check_description('api:geo_moex_risk')


@pytest.mark.parametrize("key,value", [("execution_allowed", True), ("mode", "live"),
                                      ("max_pages", 0), ("timeout_seconds", True)])
def test_config_forbids_execution_and_unbounded_collection(tmp_path, key, value):
    path=tmp_path/'config.json'
    path.write_text(json.dumps({**SETTINGS, key:value}))
    with pytest.raises(ValueError):
        n.load_settings(path)


def test_coverage_boundary_and_expired_budget():
    cutoff = NOW-timedelta(days=2)
    rows,pages = n.collect_iss(SETTINGS, cutoff,
        fetch=lambda u,t: page([1,'old','2026-11-07 15:00:00']),
        clock=lambda: NOW, deadline=monotonic()+10)
    assert rows == [] and len(pages) == 1
    with pytest.raises(TimeoutError):
        n.collect_iss(SETTINGS, cutoff, fetch=feeds, clock=lambda: NOW, deadline=0)


def test_service_has_no_broker_credentials_or_trading_writes():
    unit=(ROOT/'deploy/ubuntu/moex-news-observer.service').read_text()
    assert 'EnvironmentFile=' not in unit
    assert 'InaccessiblePaths=/etc/moex-tinvest-bot /var/lib/moex-tinvest-bot' in unit
    assert 'StateDirectory=moex-news-observer' in unit
    for file in ('geo_feed.py','shadow.py','intraday.py','live_execution.py'):
        assert 'news_observer' not in (ROOT/'src/moex_bot'/file).read_text()


def test_dedicated_rss_kuma_panel_does_not_duplicate_geo_notifications():
    spec=importlib.util.spec_from_file_location('relay_news',ROOT/'scripts/ubuntu/kuma-telegram-relay.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.transition_owned_elsewhere('MOEX Trading / RSS [api:geo_moex_risk]',0)
    assert not module.transition_owned_elsewhere('MOEX Trading / ISS [api:news_iss_observer]',0)
    assert not module.transition_owned_elsewhere('MOEX Trading / RSS [api:geo_moex_risk]',None)
