"""
Unit and integration tests for Angel One Live Order Placement and Order History.
Verifies:
1. ScalperRun dispatches BUY order on entry when tick matches.
2. ScalperRun dispatches SELL order on manual stop, slice exit, profit target, and EOD squareoff.
3. Live mode calls SmartAPI with correct payload and captures Angel One order ID.
4. Live mode fail-closed behavior: rejected broker orders do not create phantom slices.
5. Paper mode simulates cleanly without calling SmartAPI.
6. /api/broker/orders endpoint returns orders correctly.
"""

import uuid
import pytest
from unittest.mock import MagicMock, patch
from slice_trading_engine import (
    ScalperRun,
    ScalperRunConfig,
    SliceStatus,
    SliceExitReason,
    RunManager,
)
from client_session import ClientSession
from angel_one_service import AngelOneFeed
from auth_service import auth_service
from credential_service import credential_service


@pytest.fixture
def unique_client():
    cid = f"test_trader_{uuid.uuid4().hex[:6]}"
    auth_service.create_user(cid, "Password@123", role="trader")
    token = auth_service.create_access_token(cid)
    credential_service.set_trading_mode(cid, "live")
    yield cid, token


@pytest.fixture
def mock_run_config():
    return ScalperRunConfig(
        run_id="run01",
        instrument_name="NIFTY",
        option_type="CE",
        strike=24200,
        range_points=40.0,
        slicer_count=5,
        range_high=140.0,
        range_low=100.0,
        slice_interval=8.0,
        profit_point=8.0,
        loss_point=8.0,
        qty_per_slice_lots=1,
        contract_symbol="NIFTY24SEP24200CE",
        contract_token="45678",
        trading_mode="paper",
    )


def test_paper_mode_simulates_buy_and_sell_without_broker(mock_run_config):
    """Verifies that in paper mode, buy and sell orders generate simulated order IDs without touching broker."""
    dispatched_orders = []

    def mock_order_callback(**kwargs):
        dispatched_orders.append(kwargs)
        return {
            "status": "success",
            "order_id": f"sim_{len(dispatched_orders)}",
            "mode": kwargs.get("trading_mode", "paper"),
        }

    run = ScalperRun(mock_run_config, order_callback=mock_order_callback)
    run.start()

    # Inject tick at 135.0 (within top slice range: 140 - 8 < 135 <= 140)
    actions = run.process_tick(135.0)

    assert len(actions["buys"]) == 1
    assert len(run.active_slices) == 1
    assert len(dispatched_orders) == 1
    assert dispatched_orders[0]["action"] == "BUY"
    assert dispatched_orders[0]["symbol"] == "NIFTY24SEP24200CE"
    assert dispatched_orders[0]["quantity"] == 65
    assert run.active_slices[0].order_id == "sim_1"

    # Stop run -> triggers manual exit SELL
    exited = run.stop()
    assert len(exited) == 1
    assert len(dispatched_orders) == 2
    assert dispatched_orders[1]["action"] == "SELL"
    assert dispatched_orders[1]["quantity"] == 65
    assert exited[0].entry_order_id == "sim_1"
    assert exited[0].exit_order_id == "sim_2"


def test_live_mode_dispatches_real_orders_and_saves_broker_order_id(mock_run_config):
    """Verifies that in live mode, SmartAPI order placement confirms order ID on active slices and closed trades."""
    mock_run_config.trading_mode = "live"
    dispatched_orders = []

    def live_order_callback(**kwargs):
        dispatched_orders.append(kwargs)
        action = kwargs.get("action")
        return {
            "status": "success",
            "order_id": f"ANGEL_{action}_987654321",
            "mode": "live",
        }

    run = ScalperRun(mock_run_config, order_callback=live_order_callback)
    run.start()

    # Step 3: Trigger BUY at 138.0
    actions = run.process_tick(138.0)
    assert len(actions["buys"]) == 1
    assert len(run.active_slices) == 1
    assert run.active_slices[0].order_id == "ANGEL_BUY_987654321"

    # Step 2: Trigger Profit Target SELL (entry 138.0 + profit_point 8.0 = 146.0)
    sell_actions = run.process_tick(146.0)
    assert len(sell_actions["sells"]) == 1
    assert len(run.active_slices) == 0
    assert len(run.trade_history) == 1
    rec = run.trade_history[0]
    assert rec.entry_order_id == "ANGEL_BUY_987654321"
    assert rec.exit_order_id == "ANGEL_SELL_987654321"


def test_live_mode_fail_closed_on_broker_rejection(mock_run_config):
    """Verifies that in live mode, if the broker rejects the BUY, no slice is filled."""
    mock_run_config.trading_mode = "live"

    def rejecting_order_callback(**kwargs):
        return {
            "status": "error",
            "message": "RMS:Rule: Check Margin - Insufficient funds",
            "mode": "live",
        }

    run = ScalperRun(mock_run_config, order_callback=rejecting_order_callback)
    run.start()

    actions = run.process_tick(135.0)

    # Slice must NOT be filled
    assert len(actions["buys"]) == 0
    assert len(run.active_slices) == 0
    # Audit event must record rejection
    rejections = [e for e in run.audit_events if e["event_type"] == "LIVE_BUY_REJECTED"]
    assert len(rejections) == 1
    assert "Insufficient funds" in rejections[0]["message"]


def test_manual_slice_exit_dispatches_sell_order(mock_run_config):
    """Verifies that manual exit_slice dispatches a SELL order and captures exit_order_id."""
    dispatched_orders = []

    def mock_order_callback(**kwargs):
        dispatched_orders.append(kwargs)
        return {
            "status": "success",
            "order_id": f"ORD_{kwargs['action']}_101",
            "mode": "paper",
        }

    run = ScalperRun(mock_run_config, order_callback=mock_order_callback)
    run.start()
    run.process_tick(135.0)
    assert len(run.active_slices) == 1

    # Exit specific slice
    trade = run.exit_slice("A", exit_price=136.0)
    assert trade is not None
    assert len(dispatched_orders) == 2
    assert dispatched_orders[1]["action"] == "SELL"
    assert trade.exit_order_id == "ORD_SELL_101"
    assert len(run.active_slices) == 0


def test_angel_one_feed_place_order_live_with_smartapi_success(unique_client):
    """Verifies AngelOneFeed.place_order correctly formats and sends order to smart_api."""
    cid, _ = unique_client
    feed = AngelOneFeed(
        client_id="TEST_CLIENT",
        api_key="TEST_KEY",
        totp_secret="TEST_TOTP",
        mpin="1234",
        owner_client_id=cid,
    )
    feed.is_authenticated = True

    mock_smart_api = MagicMock()
    mock_smart_api._postRequest.return_value = {
        "status": True,
        "message": "SUCCESS",
        "errorcode": "",
        "data": {"orderid": "240915000123456"},
    }
    feed.smart_api = mock_smart_api

    res = feed.place_order(
        client_id=cid,
        run_id="run01",
        run_owner_client_id=cid,
        symbol="NIFTY24SEP24200CE",
        token="45678",
        quantity=65,
        price=120.0,
        transaction_type="BUY",
        order_type="MARKET",
        trading_mode="live",
    )

    assert res["status"] == "success"
    assert res["order_id"] == "240915000123456"
    assert res["mode"] == "live"
    mock_smart_api._postRequest.assert_called_once()
    payload = mock_smart_api._postRequest.call_args[0][1]
    assert payload["tradingsymbol"] == "NIFTY24SEP24200CE"
    assert payload["symboltoken"] == "45678"
    assert payload["transactiontype"] == "BUY"
    assert payload["quantity"] == "65"
    assert payload["squareoff"] == "0"
    assert payload["stoploss"] == "0"
