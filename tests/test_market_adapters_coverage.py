from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from moex_bot.integrations import algopack_flow, moexalgo_data
from moex_bot.integrations.algopack_flow import AlgoPackFlowAdapter
from moex_bot.integrations.moexalgo_data import HistoricalCandle, MoexAlgoReadOnlyAdapter

D = Decimal
NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


class Frame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


class Ticker:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def candles(self, **kwargs):
        self.calls.append(kwargs)
        return Frame(self.rows)

    def tradestats(self, **kwargs):
        return Frame(self.rows)

    def futoi(self, **kwargs):
        return Frame(self.rows)

    def hi2(self, **kwargs):
        return Frame(self.rows)


@pytest.mark.parametrize(
    "values",
    [
        (0, 2, 1, 1, 1),
        (2, 1, 1, 2, 1),
        (1, 2, 2, 1, 1),
        (1, 2, 1, 1, -1),
    ],
)
def test_historical_candle_validation(values):
    with pytest.raises(ValueError):
        HistoricalCandle(date(2026, 1, 1), *(D(v) for v in values))


def test_field_missing_and_short_volatility():
    with pytest.raises(ValueError, match="missing"):
        moexalgo_data._field({"ignored": 1}, "open")
    assert moexalgo_data._field({"OPEN": None, "CLOSE": 3}, "open", "close") == 3
    assert moexalgo_data._volatility([D(1), D(2)]) == 0
    assert moexalgo_data._timestamp(NOW) is NOW
    assert moexalgo_data._timestamp("2026-01-01T10:00:00").utcoffset() == timedelta(hours=3)


@pytest.mark.parametrize(
    "module,adapter",
    [
        (moexalgo_data, MoexAlgoReadOnlyAdapter),
        (algopack_flow, AlgoPackFlowAdapter),
    ],
)
def test_missing_optional_dependency_is_actionable(monkeypatch, module, adapter):
    def missing(name):
        raise ImportError(name)

    monkeypatch.setattr(module, "import_module", missing)
    with pytest.raises(RuntimeError, match="install optional integrations"):
        adapter.from_environment()


def test_moexalgo_environment_factory_and_metadata(monkeypatch):
    ticker = Ticker()
    rows = [{"ticker": "SBER", "board": "TQBR", "lotsize": 10, "minstep": "0.01"}]
    module = SimpleNamespace(
        session=SimpleNamespace(TOKEN=""),
        Ticker=lambda secid, board: ticker,
        Market=lambda *a, **k: SimpleNamespace(tickers=lambda *a: Frame(rows)),
    )
    monkeypatch.setattr(
        moexalgo_data,
        "import_module",
        lambda name: SimpleNamespace(load_dotenv=lambda: None) if name == "dotenv" else module,
    )
    monkeypatch.setenv("MOEX_APIKEY", "test")
    adapter = MoexAlgoReadOnlyAdapter.from_environment(require_token=True)
    assert module.session.TOKEN == "test"
    assert adapter._ticker_factory("SBER", "TQBR") is ticker
    assert adapter._metadata_factory("SBER", "TQBR")["lotsize"] == 10
    with pytest.raises(ValueError, match="one securities row"):
        adapter._metadata_factory("OTHER", "TQBR")
    monkeypatch.delenv("MOEX_APIKEY")
    with pytest.raises(RuntimeError, match="MOEX_APIKEY"):
        MoexAlgoReadOnlyAdapter.from_environment(require_token=True)
    MoexAlgoReadOnlyAdapter.from_environment(require_token=False)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"as_of": datetime(2026, 1, 1)},
        {"trend_window": 1},
        {"momentum_windows": (0,)},
        {"volatility_window": 1},
        {},
    ],
)
def test_observation_rejects_missing_or_invalid_history(kwargs):
    adapter = MoexAlgoReadOnlyAdapter(
        lambda *args: Ticker(), lambda *args: {"lotsize": 10, "minstep": "0.01"}
    )
    with pytest.raises(ValueError):
        adapter.hourly_observation(
            **(dict(secid="SBER", uid="uid", board="TQBR", as_of=NOW) | kwargs)
        )


def test_historical_candles_chunk_and_sort_without_duplicates():
    ticker = Ticker([dict(begin="2025-01-02T10:00:00", open=1, high=2, low=1, close=2, volume=100)])
    adapter = MoexAlgoReadOnlyAdapter(lambda *args: ticker, lambda *args: {})
    result = adapter.historical_daily_candles(
        secid="SBER", board="TQBR", start=date(2025, 1, 1), end=date(2026, 1, 1)
    )
    assert len(ticker.calls) == 2
    assert len(result) == 1
    assert result[0].trading_date == date(2025, 1, 2)
    with pytest.raises(ValueError, match="start must not"):
        adapter.historical_daily_candles(
            secid="SBER", board="TQBR", start=date(2026, 1, 1), end=date(2025, 1, 1)
        )


def test_algopack_flow_environment(monkeypatch):
    module = SimpleNamespace(session=SimpleNamespace(TOKEN=""), Ticker=Ticker)
    monkeypatch.setattr(
        algopack_flow,
        "import_module",
        lambda name: SimpleNamespace(load_dotenv=lambda: None) if name == "dotenv" else module,
    )
    monkeypatch.delenv("MOEX_APIKEY", raising=False)
    with pytest.raises(RuntimeError, match="MOEX_APIKEY"):
        AlgoPackFlowAdapter.from_environment()
    monkeypatch.setenv("MOEX_APIKEY", "test")
    assert isinstance(AlgoPackFlowAdapter.from_environment(), AlgoPackFlowAdapter)
    assert module.session.TOKEN == "test"


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("equity_flow", {"secid": "SBER"}),
        ("futoi", {"ticker": "Si"}),
        ("concentration", {"secid": "SBER"}),
    ],
)
def test_algopack_flow_missing_rows(method, kwargs):
    adapter = AlgoPackFlowAdapter(lambda *args: Ticker())
    with pytest.raises(ValueError, match="no "):
        getattr(adapter, method)(as_of=NOW, **kwargs)
