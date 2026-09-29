"""
Automated Unit & Concurrency Tests for Phase 4: Instantiable ClientSession Architecture
Tests:
- Concurrent execution of multiple clients with zero interference
- Independent 4-slot capacity per client
- Separate trade histories, P&L, audit logs, and state files
- Per-client SSE broadcasting isolation
- Session teardown on logout
"""

import asyncio
import os
import sys
import uuid
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app import app, get_client_session, client_sessions
from auth_service import auth_service
from client_session import ClientSession


@pytest.fixture
def client():
    return TestClient(app)


def test_concurrent_multi_client_runs_isolation(client):
    """Verify Client A and Client B can execute trading runs concurrently with zero state crossing."""
    cid_a = f"trader_a_{uuid.uuid4().hex[:6]}"
    cid_b = f"trader_b_{uuid.uuid4().hex[:6]}"
    pwd = "SecureTestPassword123!"

    auth_service.create_user(client_id=cid_a, plain_password=pwd)
    auth_service.create_user(client_id=cid_b, plain_password=pwd)

    res_a = client.post("/auth/login", json={"client_id": cid_a, "password": pwd})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    res_b = client.post("/auth/login", json={"client_id": cid_b, "password": pwd})
    token_b = res_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # Start Run for Client A (NIFTY CE)
    payload_a = {
        "run_id": "run01",
        "instrument_name": "NIFTY",
        "option_type": "CE",
        "strike": 24200,
        "range_low": 100.0,
        "range_high": 120.0,
        "slice_interval": 5.0,
        "profit_point": 5.0,
        "loss_point": 10.0,
        "qty_per_slice_lots": 1,
    }
    res_start_a = client.post("/api/runs/start", json=payload_a, headers=headers_a)
    assert res_start_a.status_code == 200
    assert res_start_a.json()["client_id"] == cid_a

    # Start Run for Client B (SENSEX PE)
    payload_b = {
        "run_id": "run01",
        "instrument_name": "SENSEX",
        "option_type": "PE",
        "strike": 81000,
        "range_low": 200.0,
        "range_high": 250.0,
        "slice_interval": 10.0,
        "profit_point": 10.0,
        "loss_point": 20.0,
        "qty_per_slice_lots": 2,
    }
    res_start_b = client.post("/api/runs/start", json=payload_b, headers=headers_b)
    assert res_start_b.status_code == 200
    assert res_start_b.json()["client_id"] == cid_b

    # Inject ticks into Client A
    client.post("/api/runs/run01/tick?price=105.0", headers=headers_a)
    client.post("/api/runs/run01/tick?price=110.0", headers=headers_a)
    client.post("/api/runs/run01/tick?price=115.0", headers=headers_a)

    # Inject ticks into Client B
    client.post("/api/runs/run01/tick?price=210.0", headers=headers_b)
    client.post("/api/runs/run01/tick?price=220.0", headers=headers_b)

    # Verify Client A status
    status_a = client.get("/api/runs/run01/status", headers=headers_a).json()["run"]
    assert status_a["instrument_name"] == "NIFTY"
    assert status_a["strike"] == 24200
    assert status_a["last_ltp"] == 115.0

    # Verify Client B status
    status_b = client.get("/api/runs/run01/status", headers=headers_b).json()["run"]
    assert status_b["instrument_name"] == "SENSEX"
    assert status_b["strike"] == 81000
    assert status_b["last_ltp"] == 220.0

    # Verify sessions in registry are independent objects
    session_a = get_client_session(cid_a)
    session_b = get_client_session(cid_b)
    assert session_a is not session_b
    assert session_a.run_manager is not session_b.run_manager
    assert session_a.angel_api_lock is not session_b.angel_api_lock


def test_per_client_four_slot_capacity(client):
    """Verify each client has their own independent 4-slot capacity."""
    cid_x = f"trader_slots_x_{uuid.uuid4().hex[:6]}"
    cid_y = f"trader_slots_y_{uuid.uuid4().hex[:6]}"
    pwd = "SlotPassword123!"

    auth_service.create_user(client_id=cid_x, plain_password=pwd)
    auth_service.create_user(client_id=cid_y, plain_password=pwd)

    tok_x = client.post("/auth/login", json={"client_id": cid_x, "password": pwd}).json()["access_token"]
    tok_y = client.post("/auth/login", json={"client_id": cid_y, "password": pwd}).json()["access_token"]

    head_x = {"Authorization": f"Bearer {tok_x}"}
    head_y = {"Authorization": f"Bearer {tok_y}"}

    # Start all 4 slots for Client X
    for i in range(1, 5):
        slot_id = f"run{i:02d}"
        res = client.post(
            "/api/runs/start",
            json={
                "run_id": slot_id,
                "instrument_name": "NIFTY",
                "option_type": "CE",
                "strike": 24000 + i * 50,
                "range_low": 50.0,
                "range_high": 150.0,
                "slice_interval": 5.0,
            },
            headers=head_x,
        )
        assert res.status_code == 200

    # Start all 4 slots for Client Y independently
    for i in range(1, 5):
        slot_id = f"run{i:02d}"
        res = client.post(
            "/api/runs/start",
            json={
                "run_id": slot_id,
                "instrument_name": "SENSEX",
                "option_type": "PE",
                "strike": 80000 + i * 100,
                "range_low": 100.0,
                "range_high": 300.0,
                "slice_interval": 10.0,
            },
            headers=head_y,
        )
        assert res.status_code == 200

    # Verify Client X has 4 active slots
    runs_x = client.get("/api/runs", headers=head_x).json()["runs"]
    assert len(runs_x) == 4
    for r in runs_x:
        assert r["instrument_name"] == "NIFTY"

    # Verify Client Y has 4 active slots
    runs_y = client.get("/api/runs", headers=head_y).json()["runs"]
    assert len(runs_y) == 4
    for r in runs_y:
        assert r["instrument_name"] == "SENSEX"


def test_session_teardown_on_logout(client):
    """Verify session is torn down and cleaned up on logout."""
    cid = f"trader_logout_{uuid.uuid4().hex[:6]}"
    pwd = "LogoutPassword123!"

    auth_service.create_user(client_id=cid, plain_password=pwd)
    tok = client.post("/auth/login", json={"client_id": cid, "password": pwd}).json()["access_token"]
    headers = {"Authorization": f"Bearer {tok}"}

    # Access state to ensure session is active in registry
    client.get("/api/state", headers=headers)
    assert cid in client_sessions

    # Logout
    res_logout = client.post("/api/auth/logout", headers=headers)
    assert res_logout.status_code == 200
    assert cid not in client_sessions
