import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from moex_bot import backtest_reporting as r


def test_gates_file_and_boolean_validation(tmp_path):
    path=tmp_path/'gates.json'
    path.write_text('[]')
    with pytest.raises(ValueError,match='JSON object'):
        r.load_promotion_gates(path)
    source=Path(__file__).resolve().parents[1]/'config/promotion_gates.json'
    raw=json.loads(source.read_text())
    path.write_text(json.dumps(raw))
    assert r.load_promotion_gates(path).min_oos_sessions>0
    raw['require_dividends']='true'
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError,match='JSON boolean'):
        r.load_promotion_gates(path)


def test_html_chart_without_benchmark_does_not_invent_values():
    points=tuple(SimpleNamespace(trading_date=date(2026,10,30),strategy_equity=Decimal(100),
                                 benchmark_equity=None) for _ in range(2))
    chart=r._html_chart(SimpleNamespace(equity_curve=points))
    assert 'points=""' in chart
    assert '100.0' in chart
