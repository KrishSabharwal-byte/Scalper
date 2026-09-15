"""
Automated Security & Unit Tests for Phase 6: Cross-Client Order-Safety Validation
Tests:
- Fail-closed validation blocking mismatched client / credential order attempts
- Fail-closed validation blocking mismatched run slot order attempts
- Comprehensive audit trail recording (client_id, run_id, attempted_action, timestamp)
- Verified safe execution path for valid matching credentials and run slots
"""

import os
import sys
import uuid
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from order_safety_service import (
    validate_order_safety,
    OrderSafetyViolation,
    record_security_audit_event,
)
from client_session import ClientSession
from angel_one_service import AngelOneFeed


def test_validate_order_safety_matching_credentials_passes():
    """Verify order safety passes when client_id, run_owner, and cred_owner match 100%."""
    cid = f"trader_{uuid.uuid4().hex[:6]}"
    rid = "run01"
    order_params = {
        "symbol": "NIFTY 24250 CE",
        "token": "12345",
        "quantity": 65,
        "price": 105.5,
    }

    result = validate_order_safety(
        client_id=cid,
        run_id=rid,
        run_owner_client_id=cid,
        credential_owner_client_id=cid,
        order_params=order_params,
    )
    assert result is True


def test_validate_order_safety_mismatched_credentials_fails_closed():
    """Verify attempt to dispatch order using another client's credentials is blocked and logged."""
    cid_a = "client_attacker"
    cid_b = "client_victim"
    rid = "run01"
    order_params = {
        "symbol": "NIFTY 24250 CE",
        "token": "12345",
        "quantity": 65,
        "price": 105.5,
    }

    with pytest.raises(OrderSafetyViolation) as exc_info:
        validate_order_safety(
            client_id=cid_a,
            run_id=rid,
            run_owner_client_id=cid_a,
            credential_owner_client_id=cid_b,  # MISMATCH: Using victim's credentials
            order_params=order_params,
        )

    err = exc_info.value
    assert "Broker credentials belong to client 'client_victim', not 'client_attacker'" in err.message
    assert err.audit_data["event_type"] == "SECURITY_ORDER_SAFETY_VIOLATION"
    assert err.audit_data["severity"] == "CRITICAL"
    assert err.audit_data["client_id"] == cid_a


def test_validate_order_safety_mismatched_run_slot_fails_closed():
    """Verify attempt to dispatch order on another client's run slot is blocked and logged."""
    cid_a = "client_attacker"
    cid_b = "client_victim"
    rid = "run01"
    order_params = {
        "symbol": "SENSEX 81000 PE",
        "token": "99999",
        "quantity": 20,
        "price": 250.0,
    }

    with pytest.raises(OrderSafetyViolation) as exc_info:
        validate_order_safety(
            client_id=cid_a,
            run_id=rid,
            run_owner_client_id=cid_b,  # MISMATCH: Attacking victim's slot
            credential_owner_client_id=cid_a,
            order_params=order_params,
        )

    err = exc_info.value
    assert "Run slot 'run01' belongs to client 'client_victim', not 'client_attacker'" in err.message
    assert err.audit_data["event_type"] == "SECURITY_ORDER_SAFETY_VIOLATION"
    assert err.audit_data["client_id"] == cid_a


def test_validate_order_safety_anonymous_fails_closed():
    """Verify anonymous order attempts are rejected immediately."""
    with pytest.raises(OrderSafetyViolation) as exc_info:
        validate_order_safety(
            client_id="",
            run_id="run01",
            run_owner_client_id="",
            credential_owner_client_id="",
            order_params={"symbol": "NIFTY 24250 CE", "quantity": 65, "price": 100.0},
        )
    assert "Missing authenticated client_id" in exc_info.value.message


def test_client_session_place_order_safety_integration():
    """Verify ClientSession place_order prevents cross-client execution."""
    session_alpha = ClientSession(client_id="trader_alpha_sec")
    session_beta = ClientSession(client_id="trader_beta_sec")

    # Attempt to use session_alpha's feed with session_beta's run owner
    with pytest.raises(OrderSafetyViolation):
        session_alpha.angel_feed.place_order(
            client_id="trader_alpha_sec",
            run_id="run01",
            run_owner_client_id="trader_beta_sec",  # MISMATCH
            symbol="NIFTY 24200 CE",
            token="11111",
            quantity=65,
            price=120.0,
        )

    # Valid execution on session_alpha
    order_res = session_alpha.place_order(
        run_id="run01",
        symbol="NIFTY 24200 CE",
        token="11111",
        quantity=65,
        price=120.0,
    )
    assert order_res["status"] == "success"
    assert "order_id" in order_res

    # Cleanup
    session_alpha.close()
    session_beta.close()
