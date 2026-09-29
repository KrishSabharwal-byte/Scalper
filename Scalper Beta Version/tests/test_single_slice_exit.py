"""
Tests for exiting individual active slice positions in ScalperRun and via REST API.
"""

import pytest
from fastapi.testclient import TestClient
from slice_trading_engine import (
    ScalperRun,
    ScalperRunConfig,
    MultiSlotManager,
    SliceStatus,
    SliceExitReason,
)
from app import app
from auth_service import auth_service


@pytest.fixture
def run_config():
    return ScalperRunConfig(
        run_id="test_run_single_exit",
        instrument_name="NIFTY",
        strike=24250,
        range_high=140.0,
        range_low=100.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=10.0,
        qty_per_slice_lots=1,
    )


def test_scalper_run_exit_single_slice_by_label(run_config):
    """Verifies exiting a single slice by its label leaves other active slices untouched and run active."""
    run = ScalperRun(run_config)
    run.start()

    # Step down: Buy Slice A at 140, Slice B at 130
    run.process_tick(140.0)
    assert len(run.active_slices) == 1
    assert run.active_slices[0].label == "A"

    run.process_tick(130.0)
    assert len(run.active_slices) == 2
    assert run.active_slices[1].label == "B"

    # Manually exit Slice A at 135.0
    trade = run.exit_slice("A", exit_price=135.0)

    assert trade is not None
    assert trade.label == "A"
    assert trade.exit_price == 135.0
    assert trade.fill_price == 140.0
    assert trade.pnl_points == -5.0
    assert trade.pnl_rupees == -5.0 * 65
    assert trade.exit_reason == SliceExitReason.MANUAL.value

    # Run should remain active!
    assert run.is_active is True

    # Only Slice B should remain active
    assert len(run.active_slices) == 1
    assert run.active_slices[0].label == "B"

    # Ladder level 140 should be marked USED
    level_140 = next(s for s in run.grid_ladder if s.level_price == 140.0)
    assert level_140.status == SliceStatus.USED

    # Trade history should contain trade for Slice A
    assert len(run.trade_history) == 1
    assert run.trade_history[0].label == "A"

    # Audit events should record MANUAL_SLICE_EXIT
    assert any(e["event_type"] == "MANUAL_SLICE_EXIT" for e in run.audit_events)


def test_scalper_run_exit_slice_by_order_id_and_level_price(run_config):
    """Verifies slice lookup works by order_id or level_price float."""
    run = ScalperRun(run_config)
    run.start()

    run.process_tick(140.0)
    run.process_tick(130.0)
    assert len(run.active_slices) == 2

    slice_a = run.active_slices[0]
    slice_b = run.active_slices[1]

    # Exit slice A by order_id
    trade_a = run.exit_slice(slice_a.order_id, exit_price=145.0)
    assert trade_a is not None
    assert trade_a.label == "A"
    assert trade_a.pnl_points == 5.0

    # Exit slice B by level_price
    trade_b = run.exit_slice("130.0", exit_price=132.0)
    assert trade_b is not None
    assert trade_b.label == "B"
    assert trade_b.pnl_points == 2.0

    assert len(run.active_slices) == 0


def test_scalper_run_exit_slice_invalid_identifier(run_config):
    """Returns None when attempting to exit a non-existent slice or empty ladder."""
    run = ScalperRun(run_config)
    run.start()

    assert run.exit_slice("NONEXISTENT") is None

    run.process_tick(140.0)
    assert run.exit_slice("Z") is None
    assert len(run.active_slices) == 1


def test_multi_slot_manager_exit_slice(run_config):
    """Verifies MultiSlotManager exit_slice delegates properly and saves state."""
    mgr = MultiSlotManager(state_file="scratch_test_state.json")
    try:
        run = mgr.start_run(run_config)
        run.process_tick(140.0)
        assert len(run.active_slices) == 1

        trade = mgr.exit_slice(run_config.run_id, "A", exit_price=142.0)
        assert trade is not None
        assert trade.label == "A"
        assert len(run.active_slices) == 0
    finally:
        import os
        if os.path.exists("scratch_test_state.json"):
            os.remove("scratch_test_state.json")


def test_api_exit_slice_endpoint():
    """Verifies the REST API endpoint /runs/{run_id}/slices/exit works with auth."""
    import uuid
    client_id = f"test_trader_{uuid.uuid4().hex[:8]}"
    password = "SecretPassword123!"
    auth_service.create_user(client_id=client_id, plain_password=password, is_active=True, role="trader")

    client = TestClient(app)
    login_res = client.post("/auth/login", json={"client_id": client_id, "password": password})
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Start slicer run01 if not running
    start_resp = client.post(
        "/runs/start",
        json={
            "run_id": "run01",
            "instrument": "NIFTY",
            "range_high": 140.0,
            "range_low": 100.0,
            "slice_interval": 10.0,
            "profit_point": 10.0,
            "loss_point": 10.0,
            "qty_per_slice_lots": 1,
            "spot_ltp": 24250.0,
            "auto_eod_squareoff": False,
        },
        headers=headers,
    )
    assert start_resp.status_code == 200

    # Inject tick to fill slice at 140.0
    tick_resp = client.post(
        "/runs/run01/tick",
        json={"price": 140.0},
        headers=headers,
    )
    assert tick_resp.status_code == 200

    # Exit the slice via API
    exit_resp = client.post(
        "/runs/run01/slices/exit",
        json={"slice_id": "A", "price": 139.0},
        headers=headers,
    )
    assert exit_resp.status_code == 200
    data = exit_resp.json()
    assert data["status"] == "success"
    assert data["trade"]["label"] == "A"
    assert data["trade"]["exit_price"] == 139.0

    # Attempting to exit again should return 404
    exit_again = client.post(
        "/runs/run01/slices/exit",
        json={"slice_id": "A"},
        headers=headers,
    )
    assert exit_again.status_code == 404
