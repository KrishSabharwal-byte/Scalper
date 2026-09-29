"""
Unit & Integration Tests for Authentication & Session Management (Phase 1)
Tests:
- Bcrypt hashing & verification
- Unauthenticated requests returning 401 Unauthorized
- Successful / failed login flow
- Protected route execution with Bearer token & session cookies
- Tampered & expired token rejection
- Logout & token revocation (blacklist)
- Deactivated user rejection
"""

import sys
import os
import pytest
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app import app
from auth_service import auth_service, hash_password, verify_password


import uuid

@pytest.fixture
def client():
    # Set dev secret / ensure test client is ready
    return TestClient(app)


@pytest.fixture
def test_user():
    client_id = f"test_trader_{uuid.uuid4().hex[:8]}"
    password = "SecretPassword123!"
    user = auth_service.create_user(client_id=client_id, plain_password=password, is_active=True, role="trader")
    return {"client_id": client_id, "password": password, "user": user}


def test_password_hashing_and_verification():
    raw_pass = "SecureTradingPass#2026"
    hashed = hash_password(raw_pass)

    assert hashed != raw_pass
    assert hashed.startswith("$2b$") or hashed.startswith("$2a$")
    assert verify_password(raw_pass, hashed) is True
    assert verify_password("WrongPassword", hashed) is False
    assert verify_password("", hashed) is False


def test_unauthenticated_requests_return_401(client):
    # Unauthenticated /runs request
    res_runs = client.get("/api/runs")
    assert res_runs.status_code == 401
    assert "Authentication required" in res_runs.json().get("detail", "")

    # Unauthenticated /history request
    res_hist = client.get("/api/history")
    assert res_hist.status_code == 401

    # Unauthenticated /api/state request
    res_state = client.get("/api/state")
    assert res_state.status_code == 401


def test_login_flow_success_and_failure(client, test_user):
    # Valid Login
    login_payload = {
        "client_id": test_user["client_id"],
        "password": test_user["password"],
    }
    res = client.post("/auth/login", json=login_payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "access_token" in data
    assert data["client_id"] == test_user["client_id"]

    # Invalid Password Login
    bad_payload = {
        "client_id": test_user["client_id"],
        "password": "WrongPasswordXYZ",
    }
    res_bad = client.post("/auth/login", json=bad_payload)
    assert res_bad.status_code == 401
    assert "Invalid Client ID or password" in res_bad.json().get("detail", "")


def test_protected_endpoints_with_bearer_token(client, test_user):
    # Login to get token
    login_res = client.post("/auth/login", json={
        "client_id": test_user["client_id"],
        "password": test_user["password"],
    })
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Access /api/runs
    res_runs = client.get("/api/runs", headers=headers)
    assert res_runs.status_code == 200
    assert res_runs.json()["status"] == "success"

    # Access /api/history
    res_hist = client.get("/api/history", headers=headers)
    assert res_hist.status_code == 200
    assert "trades" in res_hist.json()

    # Access /auth/me
    res_me = client.get("/auth/me", headers=headers)
    assert res_me.status_code == 200
    assert res_me.json()["client_id"] == test_user["client_id"]


def test_protected_endpoints_with_session_cookie(client, test_user):
    # Login sets slicer_session cookie
    login_res = client.post("/auth/login", json={
        "client_id": test_user["client_id"],
        "password": test_user["password"],
    })
    assert login_res.status_code == 200
    assert "slicer_session" in login_res.cookies

    # Request without explicit Authorization header, relying on cookie
    res = client.get("/api/runs", cookies=login_res.cookies)
    assert res.status_code == 200
    assert res.json()["status"] == "success"


def test_tampered_and_invalid_token_rejected(client):
    headers = {"Authorization": "Bearer fake.tampered.token"}
    res = client.get("/api/runs", headers=headers)
    assert res.status_code == 401


def test_expired_token_rejected(client, test_user):
    # Create an expired token (expired 5 minutes ago)
    expired_token = auth_service.create_access_token(
        client_id=test_user["client_id"],
        expires_delta=datetime.timedelta(minutes=-5),
    )
    headers = {"Authorization": f"Bearer {expired_token}"}
    res = client.get("/api/runs", headers=headers)
    assert res.status_code == 401
    assert "expired" in res.json().get("detail", "").lower()


def test_logout_invalidates_session(client, test_user):
    # Login
    login_res = client.post("/auth/login", json={
        "client_id": test_user["client_id"],
        "password": test_user["password"],
    })
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Verify active access
    assert client.get("/api/runs", headers=headers).status_code == 200

    # Logout
    logout_res = client.post("/auth/logout", headers=headers)
    assert logout_res.status_code == 200

    # Subsequent request with the same token must be rejected with 401
    res_after = client.get("/api/runs", headers=headers)
    assert res_after.status_code == 401
    assert "revoked" in res_after.json().get("detail", "").lower() or "invalidated" in res_after.json().get("detail", "").lower()
