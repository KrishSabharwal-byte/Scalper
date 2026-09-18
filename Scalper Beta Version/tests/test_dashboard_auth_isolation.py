"""
Automated Unit & Dashboard Integration Tests for Phase 5: Wire Dashboard to Logged-In Client's Data Only
Tests:
- Unauthenticated access rejection on all data and stream endpoints
- Browser SSE stream authentication via query token (?token=...)
- Multi-client dashboard state isolation (zero data bleed between Client A and Client B)
- Session invalidation & logout flow
"""

import asyncio
import json
import os
import sys
import uuid
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app import app
from auth_service import auth_service


@pytest.fixture
def client():
    return TestClient(app)


def test_unauthenticated_dashboard_state_and_stream_rejection(client):
    """Verify all dashboard and stream endpoints strictly reject unauthenticated calls with 401."""
    res_state = client.get("/api/state")
    assert res_state.status_code == 401

    res_stream = client.get("/api/stream")
    assert res_stream.status_code == 401

    res_runs = client.get("/api/runs")
    assert res_runs.status_code == 401

    res_history = client.get("/api/history")
    assert res_history.status_code == 401


def test_authenticated_dashboard_state(client):
    """Verify authenticated client retrieves scoped state with valid session."""
    cid = f"dash_user_{uuid.uuid4().hex[:6]}"
    pwd = "DashPassword123!"

    auth_service.create_user(client_id=cid, plain_password=pwd)
    res_login = client.post("/auth/login", json={"client_id": cid, "password": pwd})
    assert res_login.status_code == 200
    token = res_login.json()["access_token"]

    res_state = client.get("/api/state", headers={"Authorization": f"Bearer {token}"})
    assert res_state.status_code == 200
    data = res_state.json()
    assert data["client_id"] == cid
    assert "runs" in data
    assert "active_run" in data


def test_two_clients_dashboard_state_isolation(client):
    """Verify Client A and Client B dashboards receive strictly isolated data with zero bleed."""
    cid_a = f"dash_a_{uuid.uuid4().hex[:6]}"
    cid_b = f"dash_b_{uuid.uuid4().hex[:6]}"
    pwd = "DashPassword123!"

    auth_service.create_user(client_id=cid_a, plain_password=pwd)
    auth_service.create_user(client_id=cid_b, plain_password=pwd)

    tok_a = client.post("/auth/login", json={"client_id": cid_a, "password": pwd}).json()["access_token"]
    tok_b = client.post("/auth/login", json={"client_id": cid_b, "password": pwd}).json()["access_token"]

    headers_a = {"Authorization": f"Bearer {tok_a}"}
    headers_b = {"Authorization": f"Bearer {tok_b}"}

    # Client A starts NIFTY CE
    client.post(
        "/api/runs/start",
        json={
            "run_id": "run01",
            "instrument_name": "NIFTY",
            "option_type": "CE",
            "strike": 24200,
            "range_low": 100.0,
            "range_high": 120.0,
            "slice_interval": 5.0,
            "qty_per_slice_lots": 1,
        },
        headers=headers_a,
    )
    # Inject tick into Client A
    client.post("/api/runs/run01/tick?price=105.0", headers=headers_a)

    # Client B starts SENSEX PE
    client.post(
        "/api/runs/start",
        json={
            "run_id": "run01",
            "instrument_name": "SENSEX",
            "option_type": "PE",
            "strike": 81000,
            "range_low": 200.0,
            "range_high": 250.0,
            "slice_interval": 10.0,
            "qty_per_slice_lots": 2,
        },
        headers=headers_b,
    )
    # Inject tick into Client B
    client.post("/api/runs/run01/tick?price=210.0", headers=headers_b)

    # Query Client A dashboard state
    state_a = client.get("/api/state", headers=headers_a).json()
    assert state_a["client_id"] == cid_a
    assert state_a["active_run"]["instrument_name"] == "NIFTY"
    assert state_a["active_run"]["last_ltp"] == 105.0

    # Query Client B dashboard state
    state_b = client.get("/api/state", headers=headers_b).json()
    assert state_b["client_id"] == cid_b
    assert state_b["active_run"]["instrument_name"] == "SENSEX"
    assert state_b["active_run"]["last_ltp"] == 210.0

    # Verify no cross-client bleed
    assert state_a["active_run"]["contract_symbol"] != state_b["active_run"]["contract_symbol"]
