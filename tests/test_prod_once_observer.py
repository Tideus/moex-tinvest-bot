"""No network: monitor cannot transact or turn missing evidence into healthy protection."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_prod_long_once import case as case
from test_prod_long_once import execute

from moex_bot import prod_once_observer as observer
from moex_bot.prod_long_once import paired
from moex_bot.prod_long_once_operator import FIXED
from moex_bot.supervised_execution import read_parent


@pytest.fixture
def observed(case, monkeypatch, tmp_path):
    execute(case)
    settings = dict(FIXED) | {"enabled": False}
    settings.update(claim_directory=str(case["claim_directory"]), journal=str(case["journal"]),
                    protection_journal=str(case["protection_journal"]))
    monkeypatch.setattr(observer, "FIXED", settings)
    monkeypatch.setattr(observer, "load_settings", lambda _: settings)
    account = case["candidate"].account_id
    monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", account)
    monkeypatch.setenv("T_INVEST_PROD_TOKEN", "never-print-this")
    _, plan = paired(case["candidate"])
    parent = read_parent(case["journal"], plan)
    rows = {
        "UsersService/GetAccounts": {"accounts": [{"id": account,
            "type": "ACCOUNT_TYPE_TINKOFF_IIS"}]},
        "OperationsService/GetPositions": {"securities": [{"instrumentUid": plan.instrument.uid,
            "balance": "10", "blocked": "0"}]},
        "OrdersService/GetOrders": {"orders": []},
        "OrdersService/GetOrderState": {
            "instrumentUid": plan.instrument.uid, "orderRequestId": plan.entry_request_id,
            "orderId": parent.broker_order_id, "direction": "ORDER_DIRECTION_BUY",
            "lotsRequested": "1", "lotsExecuted": "1",
            "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
        },
        "StopOrdersService/GetStopOrders": {"stopOrders": list(case["stops"].list_stops())},
    }
    transport = Mock()
    transport.post.side_effect = lambda url, **_: rows[url.removeprefix(observer.PREFIX)]
    path = tmp_path / "settings.json"
    path.write_text("{}")
    return path, rows, transport, case


def observe(fixture):
    path, _, transport, case = fixture
    return observer.observe(path, case["clock"](), transport=transport)


def test_disabled_does_not_hide_existing_protection(observed):
    result = observe(observed)
    assert result["state"] == "PROTECTED" and result["check"] == "OK"
    assert "never-print-this" not in json.dumps(result)
    assert observed[3]["candidate"].account_id not in json.dumps(result)
    assert all(c.args[0].removeprefix(observer.PREFIX) in observer.READS
               for c in observed[2].post.call_args_list)


@pytest.mark.parametrize("mutation", ["missing_stop", "foreign_stop", "extra_stop",
                                      "wrong_quantity", "blocked", "pending", "trigger"])
def test_unprotected_and_manual_conflicts_fail(observed, mutation):
    rows = observed[1]
    stops = rows["StopOrdersService/GetStopOrders"]["stopOrders"]
    holding = rows["OperationsService/GetPositions"]["securities"][0]
    if mutation == "missing_stop":
        stops.clear()
    elif mutation == "foreign_stop":
        stops[0]["stopOrderId"] = "other"
    elif mutation == "extra_stop":
        stops.append(dict(stops[0]))
    elif mutation == "wrong_quantity":
        holding["balance"] = "20"
    elif mutation == "blocked":
        holding["blocked"] = "10"
    elif mutation == "pending":
        rows["OrdersService/GetOrders"]["orders"].append(
            {"instrumentUid": holding["instrumentUid"]})
    else:
        stops[0]["stopPrice"]["units"] = "1"
    assert observe(observed)["check"] != "OK"


def test_flat_is_not_claimed_to_be_stop_execution_or_pnl(observed):
    observed[1]["OperationsService/GetPositions"]["securities"] = []
    observed[1]["StopOrdersService/GetStopOrders"]["stopOrders"] = []
    result = observe(observed)
    assert result["state"] == "FLAT_EXIT_UNATTRIBUTED"
    body = observer.render(result, observed[3]["clock"]().date())
    assert "P&L и просадка: н/д" in body
    assert "причина выхода не установлена" in body


def test_flat_with_orphan_stop_alerts(observed):
    observed[1]["OperationsService/GetPositions"]["securities"] = []
    assert observe(observed)["check"] != "OK"


def test_protobuf_omitted_empty_lists(observed):
    for name in ("OperationsService/GetPositions", "OrdersService/GetOrders",
                 "StopOrdersService/GetStopOrders"):
        observed[1][name] = {}
    assert observe(observed)["state"] == "FLAT_EXIT_UNATTRIBUTED"


@pytest.mark.parametrize("value", [None, {}, "", [None], [1]])
def test_invalid_repeated_fields_rejected(value):
    with pytest.raises(ValueError):
        observer.rows({"rows": value}, "rows")


def test_orphan_fence_never_healthy(observed):
    (observed[3]["claim_directory"] / "claim.json").unlink()
    with pytest.raises(ValueError, match="control evidence lost|orphan"):
        observe(observed)


@pytest.mark.parametrize("lost_settings", [False, True])
def test_total_control_loss_cannot_hide_filled_buy(observed, lost_settings):
    for path in observed[3]["claim_directory"].iterdir():
        path.unlink()
    if lost_settings:
        observed[0].unlink()
    with pytest.raises(ValueError, match="control evidence lost"):
        observe(observed)
    observed[2].post.assert_not_called()


def test_foreign_account_never_reads_broker(observed, monkeypatch):
    monkeypatch.setenv("T_INVEST_PROD_ACCOUNT_ID", "other")
    with pytest.raises(ValueError, match="account mismatch"):
        observe(observed)
    observed[2].post.assert_not_called()


@pytest.mark.parametrize("method", ["OrdersService/PostOrder", "OrdersService/CancelOrder",
                                    "StopOrdersService/PostStopOrder",
                                    "StopOrdersService/CancelStopOrder"])
def test_readonly_transport_refuses_all_writes(method):
    with pytest.raises(ValueError, match="cannot mutate"):
        observer.ReadOnlyTransport().post(observer.PREFIX + method, headers={}, payload={},
                                          timeout_seconds=1)


def test_api_errors_never_leak_credentials(monkeypatch):
    monkeypatch.setattr(observer, "observe", Mock(side_effect=OSError("secret-account-token")))
    result = observer.safe_observe(Path("unused"), datetime.now(UTC))
    assert result["state"] == "UNKNOWN" and result["check"] != "OK"
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("elapsed", [0, 6])
def test_filled_buy_gets_bound_stop_after_entry_revocation(case, elapsed):
    original = case["broker"].submit
    start = case["clock"]()

    def fill(intent):
        result = original(intent)
        case["authority"].side_effect = ValueError("entry revoked")
        return result

    case["broker"].submit = fill
    case["clock"] = lambda: start + timedelta(minutes=elapsed if case["broker"].calls else 0)
    assert execute(case)["state"] == "FILLED_PROTECTED"
    assert len(case["broker"].calls) == 1 and case["stops"].post_stop.call_count == 1
    with pytest.raises(ValueError, match="entry revoked"):
        execute(case)


def test_protection_continuation_is_not_unlimited(case):
    start = case["clock"]()
    case["clock"] = lambda: start + timedelta(minutes=11 if case["broker"].calls else 0)
    with pytest.raises(ValueError, match="continuation expired"):
        execute(case)
    assert len(case["broker"].calls) == 1
    case["stops"].post_stop.assert_not_called()
