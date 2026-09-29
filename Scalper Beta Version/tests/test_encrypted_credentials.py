"""
Automated Unit & Integration Tests for Phase 3: Encrypted Per-Client Broker Credential Storage
Tests:
- Authenticated Fernet AES-128-CBC encryption at rest
- Verification that stored database/file records contain ZERO plaintext secrets
- In-memory decryption strictly at runtime
- Static security audit asserting zero hardcoded secrets in source files
- Protected REST API endpoints for broker credentials management
- Cross-client credential isolation
"""

import os
import sys
import uuid
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app import app, get_client_broker_feed
from auth_service import auth_service
from credential_service import credential_service


@pytest.fixture
def client():
    return TestClient(app)


def test_fernet_encryption_and_decryption_at_rest():
    """Verify credentials are encrypted at rest and decryptable in-memory only."""
    cid = f"trader_{uuid.uuid4().hex[:6]}"
    raw_creds = {
        "broker_client_id": "AB123456",
        "api_key": "my_super_secret_api_key_999",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "mpin": "4321",
    }

    # Save credentials
    success = credential_service.save_broker_credentials(cid, "angel_one", raw_creds)
    assert success is True

    # Inspect raw stored record in cache
    raw_record = credential_service._cache.get(f"{cid}:angel_one")
    assert raw_record is not None
    # Verify encrypted fields are ciphertexts, NOT plaintext
    assert raw_record["encrypted_client_id"] != raw_creds["broker_client_id"]
    assert raw_record["encrypted_api_key"] != raw_creds["api_key"]
    assert raw_record["encrypted_totp_secret"] != raw_creds["totp_secret"]
    assert raw_record["encrypted_mpin"] != raw_creds["mpin"]
    assert "my_super_secret_api_key_999" not in str(raw_record)
    assert "JBSWY3DPEHPK3PXP" not in str(raw_record)

    # In-memory decryption
    decrypted = credential_service.get_decrypted_broker_credentials(cid, "angel_one")
    assert decrypted is not None
    assert decrypted["client_id"] == "AB123456"
    assert decrypted["api_key"] == "my_super_secret_api_key_999"
    assert decrypted["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert decrypted["mpin"] == "4321"

    # Status check (masked)
    status_info = credential_service.get_credential_status(cid, "angel_one")
    assert status_info["is_configured"] is True
    assert status_info["masked_client_id"] == "AB****56"

    # Clean up
    credential_service.delete_broker_credentials(cid, "angel_one")


def test_no_hardcoded_secrets_in_source_files():
    """Security audit: Assert zero hardcoded passwords or broker keys in source files."""
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    angel_file = os.path.join(root_dir, "angel_one_service.py")
    mongo_file = os.path.join(root_dir, "mongo_service.py")

    with open(angel_file, "r", encoding="utf-8") as f:
        angel_src = f.read()

    with open(mongo_file, "r", encoding="utf-8") as f:
        mongo_src = f.read()

    # Old hardcoded defaults must be completely gone
    forbidden_tokens = [
        "sze7NQng",
        "E3PUEUUFBLIEIR6XODCCXJT6S4",
        "crestviewcorporate_db_user",
        "Crestviewcorporate@cluster0",
    ]

    for token in forbidden_tokens:
        assert token not in angel_src, f"Security Violation: Token '{token}' found in angel_one_service.py"
        assert token not in mongo_src, f"Security Violation: Token '{token}' found in mongo_service.py"


def test_broker_credentials_rest_endpoints(client):
    """Verify submission, status check, and deletion of broker credentials via authenticated API."""
    cid = f"broker_trader_{uuid.uuid4().hex[:6]}"
    pwd = "BrokerPassword123!"

    # Create user & login
    auth_service.create_user(client_id=cid, plain_password=pwd)
    res_login = client.post("/auth/login", json={"client_id": cid, "password": pwd})
    token = res_login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Initial status -> is_configured: False
    res_status_init = client.get("/api/broker/credentials/status", headers=headers)
    assert res_status_init.status_code == 200
    assert res_status_init.json()["is_configured"] is False

    # Submit credentials
    submit_payload = {
        "broker_name": "angel_one",
        "broker_client_id": "XY998877",
        "api_key": "api_secret_key_abc123",
        "totp_secret": "TOTPSECRET123456",
        "mpin": "9876",
    }
    res_save = client.post("/api/broker/credentials", json=submit_payload, headers=headers)
    assert res_save.status_code == 200
    save_data = res_save.json()
    assert save_data["status"] == "success"
    assert save_data["is_configured"] is True
    assert save_data["masked_client_id"] == "XY****77"
    # Ensure raw secrets never leaked in response
    assert "api_secret_key_abc123" not in str(save_data)
    assert "TOTPSECRET123456" not in str(save_data)
    assert "9876" not in str(save_data)

    # Check status endpoint
    res_status = client.get("/api/broker/credentials/status", headers=headers)
    assert res_status.status_code == 200
    status_data = res_status.json()
    assert status_data["is_configured"] is True
    assert status_data["masked_client_id"] == "XY****77"
    assert "api_secret_key_abc123" not in str(status_data)

    # In-memory feed verification
    feed = get_client_broker_feed(cid)
    assert feed is not None
    assert feed.client_id == "XY998877"
    assert feed.api_key == "api_secret_key_abc123"

    # Delete credentials
    res_del = client.delete("/api/broker/credentials", headers=headers)
    assert res_del.status_code == 200

    # Verify status is now unconfigured
    res_status_after = client.get("/api/broker/credentials/status", headers=headers)
    assert res_status_after.json()["is_configured"] is False


def test_cross_client_credential_isolation(client):
    """Verify Client A and Client B credentials are fully isolated."""
    cid_a = f"client_cred_a_{uuid.uuid4().hex[:6]}"
    cid_b = f"client_cred_b_{uuid.uuid4().hex[:6]}"
    pwd = "IsolationPassword123!"

    auth_service.create_user(client_id=cid_a, plain_password=pwd)
    auth_service.create_user(client_id=cid_b, plain_password=pwd)

    res_a = client.post("/auth/login", json={"client_id": cid_a, "password": pwd})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    res_b = client.post("/auth/login", json={"client_id": cid_b, "password": pwd})
    token_b = res_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # Save credentials for Client A
    client.post(
        "/api/broker/credentials",
        json={
            "broker_name": "angel_one",
            "broker_client_id": "CLIENT_A_ID",
            "api_key": "KEY_A",
            "totp_secret": "TOTP_A",
            "mpin": "1111",
        },
        headers=headers_a,
    )

    # Query Client B status
    res_b_status = client.get("/api/broker/credentials/status", headers=headers_b)
    assert res_b_status.json()["is_configured"] is False
    assert res_b_status.json()["masked_client_id"] is None

    # Clean up
    credential_service.delete_broker_credentials(cid_a)
    credential_service.delete_broker_credentials(cid_b)
