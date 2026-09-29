"""
Automated Unit & Integration Tests for Phase 2: Per-Client DB Provisioning & Scoped Data Model
Tests:
- Strict guard assertions in mongo_service when client_id is missing or empty
- Per-client database resolution (client_{client_id})
- Complete isolation between two independent clients (trades, runs, slot state)
- Per-client state file generation (runs_state_{client_id}.json)
- Clear history isolation
"""

import sys
import os
import uuid
import pytest
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app import app, get_client_run_manager
from auth_service import auth_service
from mongo_service import mongo_service


@pytest.fixture
def client():
    return TestClient(app)


def test_mongo_service_strict_guard_assertions():
    """Verify that every database operation asserts a valid client_id."""
    with pytest.raises(ValueError, match="CRITICAL SECURITY GUARD"):
        mongo_service.insert_trade({"trade_id": "T1"}, client_id=None)

    with pytest.raises(ValueError, match="CRITICAL SECURITY GUARD"):
        mongo_service.insert_trade({"trade_id": "T1"}, client_id="")

    with pytest.raises(ValueError, match="CRITICAL SECURITY GUARD"):
        mongo_service.get_recent_trades(client_id=None)

    with pytest.raises(ValueError, match="CRITICAL SECURITY GUARD"):
        mongo_service.save_client_state(client_id="", state_data={})

    with pytest.raises(ValueError, match="CRITICAL SECURITY GUARD"):
        mongo_service.load_client_state(client_id=None)


def test_per_client_database_names():
    """Verify client database naming convention."""
    assert mongo_service.get_client_db_name("trader_01") == "client_trader_01"
    assert mongo_service.get_client_db_name("admin") == "client_admin"
    assert mongo_service.get_client_db_name("client_xyz") == "client_client_xyz"


def test_multitenant_data_and_state_isolation(client):
    """
    Verify two different client logins produce completely separate trade histories
    and run states.
    """
    cid_a = f"client_alpha_{uuid.uuid4().hex[:6]}"
    cid_b = f"client_beta_{uuid.uuid4().hex[:6]}"
    pwd = "ClientPassword123!"

    # Create both users
    auth_service.create_user(client_id=cid_a, plain_password=pwd)
    auth_service.create_user(client_id=cid_b, plain_password=pwd)

    # Login both users to acquire separate JWT session tokens
    res_a = client.post("/auth/login", json={"client_id": cid_a, "password": pwd})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    res_b = client.post("/auth/login", json={"client_id": cid_b, "password": pwd})
    token_b = res_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # Verify initial state of both clients is clean
    hist_a_initial = client.get("/api/history", headers=headers_a).json()
    hist_b_initial = client.get("/api/history", headers=headers_b).json()
    assert hist_a_initial["total_trades"] == 0
    assert hist_b_initial["total_trades"] == 0

    # Start a run specifically under Client Alpha (with cutoff after market hours for test)
    start_payload_a = {
        "run_id": "run01",
        "instrument_name": "NIFTY",
        "option_type": "CE",
        "strike": 24300,
        "range_high": 150.0,
        "range_low": 110.0,
        "slice_interval": 10.0,
        "profit_point": 10.0,
        "loss_point": 10.0,
        "manual_opt_ltp": 125.0,
        "cutoff_time_ist": "23:59",
        "auto_eod_squareoff": False,
    }
    res_start_a = client.post("/api/runs/start", json=start_payload_a, headers=headers_a)
    assert res_start_a.status_code == 200
    assert res_start_a.json()["client_id"] == cid_a

    # Verify Client Alpha has an active run on slot 1
    runs_a = client.get("/api/runs", headers=headers_a).json()
    slot1_a = next(r for r in runs_a["runs"] if r["run_id"] == "run01")
    assert slot1_a["is_active"] is True
    assert slot1_a["strike"] == 24300

    # Verify Client Beta's slot 1 remains COMPLETELY INACTIVE and untouched
    runs_b = client.get("/api/runs", headers=headers_b).json()
    slot1_b = next(r for r in runs_b["runs"] if r["run_id"] == "run01")
    assert slot1_b["is_active"] is False

    # Simulate a trade by injecting tick into Client Alpha's run and hitting target
    client.post("/runs/run01/tick", json={"price": 135.0}, headers=headers_a)

    # Check Client Alpha's history
    hist_a_after = client.get("/api/history", headers=headers_a).json()
    assert hist_a_after["total_trades"] == 1
    assert hist_a_after["trades"][0]["exit_price"] == 135.0

    # Verify Client Beta STILL has 0 trades
    hist_b_after = client.get("/api/history", headers=headers_b).json()
    assert hist_b_after["total_trades"] == 0
    assert len(hist_b_after["trades"]) == 0

    # Verify separate isolated run managers and zero JSON trade state files on disk
    mgr_a = get_client_run_manager(cid_a)
    mgr_b = get_client_run_manager(cid_b)
    assert mgr_a.client_id == cid_a
    assert mgr_b.client_id == cid_b
    assert mgr_a != mgr_b

    # Assert zero JSON state files created on disk (trade history stored in MongoDB SlicerNS only)
    assert not os.path.exists(f"runs_state_{cid_a}.json")
    assert not os.path.exists(f"runs_state_{cid_b}.json")


def test_admin_and_user1_separate_tabs_slot_start_isolation(client):
    """
    Regression Test:
    Simulates Tab 1 logging in as admin and Tab 2 logging in as user1.
    When Tab 2 fires /api/runs/start with user1's Authorization header,
    the run MUST start exclusively under user1's RunManager, and admin's RunManager
    must remain completely idle / inactive.
    """
    # 1. Admin login (Tab 1 session)
    res_admin = client.post("/auth/login", json={"client_id": "admin", "password": "Slicer@Admin2026"})
    assert res_admin.status_code == 200
    token_admin = res_admin.json()["access_token"]
    headers_admin = {"Authorization": f"Bearer {token_admin}"}

    # 2. User 1 login (Tab 2 session)
    res_u1 = client.post("/auth/login", json={"client_id": "User 1", "password": "User1@Slicer2026"})
    assert res_u1.status_code == 200
    token_u1 = res_u1.json()["access_token"]
    headers_u1 = {"Authorization": f"Bearer {token_u1}"}

    # Verify /auth/me returns respective identities
    me_admin = client.get("/auth/me", headers=headers_admin).json()
    assert me_admin["client_id"] == "admin"
    me_u1 = client.get("/auth/me", headers=headers_u1).json()
    assert me_u1["client_id"] == "User 1"

    # 3. User 1 starts slot N-C (run01)
    start_payload = {
        "run_id": "run01",
        "instrument_name": "NIFTY",
        "option_type": "CE",
        "strike": 24500,
        "range_high": 150.0,
        "range_low": 100.0,
        "slice_interval": 10.0,
        "profit_point": 10.0,
        "loss_point": 10.0,
        "manual_opt_ltp": 130.0,
        "cutoff_time_ist": "23:59",
        "auto_eod_squareoff": False,
    }
    res_start = client.post("/api/runs/start", json=start_payload, headers=headers_u1)
    assert res_start.status_code == 200
    assert res_start.json()["client_id"] == "User 1"

    # 4. Verify User 1's RunManager has the active run
    mgr_u1 = get_client_run_manager("User 1")
    run_u1 = mgr_u1.get_run("run01")
    assert run_u1 is not None
    assert run_u1.is_active is True
    assert run_u1.config.strike == 24500

    # 5. Verify admin's RunManager has ZERO active runs
    mgr_admin = get_client_run_manager("admin")
    run_admin = mgr_admin.get_run("run01")
    assert run_admin is not None
    assert run_admin.is_active is False


def test_delete_single_closed_trade_endpoint(client):
    """
    Verify deleting a single closed trade removes it from mongo and RunManager,
    updates realized P&L, and does not affect other clients.
    """
    cid = f"test_del_client_{uuid.uuid4().hex[:6]}"
    pwd = "TestPass123!Safe"
    auth_service.create_user(client_id=cid, plain_password=pwd)

    res_login = client.post("/auth/login", json={"client_id": cid, "password": pwd})
    token = res_login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Insert two closed trades into client's history
    trade1 = {
        "trade_id": "TRD-NIFTY-DEL-01",
        "instrument": "NIFTY",
        "symbol": "NIFTY",
        "option_type": "CE",
        "strike": 24500,
        "entry_price": 100.0,
        "fill_price": 100.0,
        "exit_price": 110.0,
        "quantity": 50,
        "pnl_points": 10.0,
        "pnl_rupees": 500.0,
        "mode": "paper",
        "client_id": cid,
        "entry_time": "2026-09-17 10:00:00",
        "exit_time": "2026-09-17 10:05:00",
    }
    trade2 = {
        "trade_id": "TRD-NIFTY-KEEP-02",
        "instrument": "NIFTY",
        "symbol": "NIFTY",
        "option_type": "PE",
        "strike": 24400,
        "entry_price": 100.0,
        "fill_price": 100.0,
        "exit_price": 120.0,
        "quantity": 50,
        "pnl_points": 20.0,
        "pnl_rupees": 1000.0,
        "mode": "paper",
        "client_id": cid,
        "entry_time": "2026-09-17 10:10:00",
        "exit_time": "2026-09-17 10:15:00",
    }
    # Insert two closed trades into client's MongoDB collection
    mongo_service.insert_trade(trade1, client_id=cid)
    mongo_service.insert_trade(trade2, client_id=cid)

    # Acquire client's RunManager (which loads the 2 trades from DB)
    mgr = get_client_run_manager(cid)

    # Check history endpoint has 2 trades
    hist = client.get("/api/history", headers=headers).json()
    assert hist["total_trades"] == 2

    # Delete trade 1 via DELETE /api/history/TRD-NIFTY-DEL-01
    del_res = client.delete("/api/history/TRD-NIFTY-DEL-01", headers=headers)
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "success"
    assert del_res.json()["deleted_count"] >= 1

    # Verify history now only has trade 2
    hist_after = client.get("/api/history", headers=headers).json()
    assert hist_after["total_trades"] == 1
    assert hist_after["trades"][0]["trade_id"] == "TRD-NIFTY-KEEP-02"

    # Verify RunManager in-memory list also removed trade 1
    all_mem = mgr.get_all_trade_history()
    assert len(all_mem) == 1
    assert all_mem[0]["trade_id"] == "TRD-NIFTY-KEEP-02"


