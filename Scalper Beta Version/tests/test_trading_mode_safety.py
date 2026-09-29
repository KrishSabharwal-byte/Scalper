"""
Unit and Integration Tests for Explicit Paper vs. Live Trading Mode.
Tests:
1. Default client trading mode is strictly "paper".
2. credential_service get_trading_mode and set_trading_mode persistence and validation.
3. Check 5 in order_safety_service fails closed on mode mismatch.
4. angel_one_service.place_order() hard-fails in live mode if unauthenticated (no silent fallback).
5. angel_one_service.place_order() simulates cleanly in paper mode without touching SmartAPI.
6. TradeRecord dataclass stamps mode correctly.
7. FastAPI endpoints GET/POST /api/client/trading_mode.
"""

import os
import sys
import uuid
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from credential_service import credential_service
from order_safety_service import validate_order_safety, OrderSafetyViolation
from angel_one_service import AngelOneFeed
from slice_trading_engine import TradeRecord, ScalperRunConfig, ScalperRun
from fastapi.testclient import TestClient
from app import app
from auth_service import auth_service


@pytest.fixture
def unique_client():
    cid = f"test_trader_{uuid.uuid4().hex[:6]}"
    auth_service.create_user(cid, "Password@123", role="trader")
    token = auth_service.create_access_token(cid)
    yield cid, token


def test_client_default_trading_mode_is_paper(unique_client):
    cid, _ = unique_client
    mode = credential_service.get_trading_mode(cid)
    assert mode == "paper", "Client must strictly default to 'paper' mode"


def test_set_and_get_trading_mode(unique_client):
    cid, _ = unique_client
    # Switch to live
    ok = credential_service.set_trading_mode(cid, "live")
    assert ok is True
    assert credential_service.get_trading_mode(cid) == "live"

    # Switch back to paper
    ok = credential_service.set_trading_mode(cid, "paper")
    assert ok is True
    assert credential_service.get_trading_mode(cid) == "paper"

    # Invalid mode should raise ValueError
    with pytest.raises(ValueError):
        credential_service.set_trading_mode(cid, "invalid_mode")


def test_order_safety_check_5_mismatch_fails_closed(unique_client):
    cid, _ = unique_client
    credential_service.set_trading_mode(cid, "paper")

    # Attempt to place "live" order while client is in "paper" mode
    order_params_live = {
        "symbol": "NIFTY 24250 CE",
        "token": "12345",
        "quantity": 65,
        "price": 100.0,
        "trading_mode": "live",
    }
    with pytest.raises(OrderSafetyViolation) as exc_info:
        validate_order_safety(
            client_id=cid,
            run_id="run01",
            run_owner_client_id=cid,
            credential_owner_client_id=cid,
            order_params=order_params_live,
        )
    assert "Mode mismatch" in str(exc_info.value)

    # Now switch client to "live" and attempt "paper" order -> must also fail closed!
    credential_service.set_trading_mode(cid, "live")
    order_params_paper = {
        "symbol": "NIFTY 24250 CE",
        "token": "12345",
        "quantity": 65,
        "price": 100.0,
        "trading_mode": "paper",
    }
    with pytest.raises(OrderSafetyViolation) as exc_info:
        validate_order_safety(
            client_id=cid,
            run_id="run01",
            run_owner_client_id=cid,
            credential_owner_client_id=cid,
            order_params=order_params_paper,
        )
    assert "Mode mismatch" in str(exc_info.value)

    # When order mode matches client mode ("live" == "live"), it passes Check 5
    result = validate_order_safety(
        client_id=cid,
        run_id="run01",
        run_owner_client_id=cid,
        credential_owner_client_id=cid,
        order_params=order_params_live,
    )
    assert result is True


def test_angel_one_place_order_live_unauthenticated_hard_fails(unique_client):
    """CRITICAL SECURITY TEST: Unauthenticated live orders MUST NOT fall back to simulation."""
    cid, _ = unique_client
    credential_service.set_trading_mode(cid, "live")

    feed = AngelOneFeed(owner_client_id=cid)
    feed.smart_api = None
    feed.is_authenticated = False

    result = feed.place_order(
        client_id=cid,
        run_id="run01",
        run_owner_client_id=cid,
        symbol="NIFTY 24250 CE",
        token="12345",
        quantity=65,
        price=105.0,
        trading_mode="live",
    )

    assert result["status"] == "error"
    assert result["mode"] == "live"
    assert "Live mode requires an authenticated AngelOne session" in result["message"]
    assert "order_id" not in result or not str(result.get("order_id", "")).startswith("sim_")


def test_angel_one_place_order_paper_mode_simulates_without_calling_broker(unique_client):
    """Paper mode should always simulate even if smart_api happens to be attached."""
    cid, _ = unique_client
    credential_service.set_trading_mode(cid, "paper")

    feed = AngelOneFeed(owner_client_id=cid)
    mock_smart_api = MagicMock()
    feed.smart_api = mock_smart_api
    feed.is_authenticated = True  # Even if authenticated!

    result = feed.place_order(
        client_id=cid,
        run_id="run01",
        run_owner_client_id=cid,
        symbol="NIFTY 24250 CE",
        token="12345",
        quantity=65,
        price=110.0,
        trading_mode="paper",
    )

    assert result["status"] == "success"
    assert result["mode"] == "paper"
    assert str(result["order_id"]).startswith("sim_")
    # Crucial: broker placeOrder must NEVER be called in paper mode
    mock_smart_api.placeOrder.assert_not_called()


def test_trade_record_stamps_mode():
    tr = TradeRecord(
        trade_id="TRD-12345",
        run_id="run01",
        label="A",
        level_price=100.0,
        fill_price=100.0,
        exit_price=108.0,
        quantity=65,
        pnl_points=8.0,
        pnl_rupees=520.0,
        exit_reason="PROFIT_TARGET",
        filled_at="2026-09-14T10:00:00",
        exited_at="2026-09-14T10:05:00",
        contract_symbol="NIFTY 24250 CE",
        mode="live",
    )
    assert tr.mode == "live"


def test_trading_mode_fastapi_endpoints(unique_client):
    cid, token = unique_client
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    # 1. GET /api/client/trading_mode -> defaults to paper
    res = client.get("/api/client/trading_mode", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["trading_mode"] == "paper"

    # 2. POST /api/client/trading_mode -> update to live
    res = client.post("/api/client/trading_mode", json={"mode": "live"}, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["trading_mode"] == "live"

    # Verify GET reflects update
    res = client.get("/api/client/trading_mode", headers=headers)
    assert res.json()["trading_mode"] == "live"

    # 3. POST invalid mode -> returns 400
    res = client.post("/api/client/trading_mode", json={"mode": "unreal"}, headers=headers)
    assert res.status_code == 400

    # 4. POST back to paper
    res = client.post("/api/client/trading_mode", json={"mode": "paper"}, headers=headers)
    assert res.status_code == 200
    assert res.json()["trading_mode"] == "paper"
