"""
Cross-Client Order-Safety Validation Service (Phase 6).
Provides fail-closed security guards ensuring no broker order is dispatched
unless authenticated_client_id, run_client_id, and credential_owner_client_id match 100%.
"""

import datetime
import logging
from typing import Dict, Any, Optional

from slice_trading_engine import IST
from mongo_service import mongo_service

logger = logging.getLogger("OrderSafetyService")


class OrderSafetyViolation(Exception):
    """Raised when a cross-client or mismatched order attempt is detected."""
    def __init__(self, message: str, audit_data: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.audit_data = audit_data or {}


def record_security_audit_event(
    client_id: str,
    run_id: str,
    attempted_action: str,
    reason: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Logs and records a high-priority security violation audit event.
    Writes to MongoDB client audit collection and server logs.
    """
    event = {
        "event_type": "SECURITY_ORDER_SAFETY_VIOLATION",
        "severity": "CRITICAL",
        "client_id": client_id,
        "run_id": run_id,
        "attempted_action": attempted_action,
        "reason": reason,
        "details": details or {},
        "timestamp": datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp_iso": datetime.datetime.now(IST).isoformat(),
    }

    logger.critical(
        f"🚨 ORDER SAFETY VIOLATION BLOCKED: Client '{client_id}', Run '{run_id}', "
        f"Action '{attempted_action}' - Reason: {reason} | Details: {details}"
    )

    # Persist security event to client audit log in MongoDB
    try:
        if client_id and client_id.strip():
            mongo_service.insert_audit_log(event, client_id=client_id.strip())
    except Exception as e:
        logger.warning(f"Note writing security audit event to MongoDB: {e}")

    return event


def validate_order_safety(
    client_id: str,
    run_id: str,
    run_owner_client_id: str,
    credential_owner_client_id: str,
    order_params: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Hard validation step executed before any order is sent to a broker.
    Strict fail-closed check:
    1. client_id must be non-empty string.
    2. run_owner_client_id must match client_id.
    3. credential_owner_client_id must match client_id.
    4. order_params must contain valid trading contract and quantity.

    Raises OrderSafetyViolation if any check fails.
    """
    cid = (client_id or "").strip()
    rid = (run_id or "").strip()
    run_owner = (run_owner_client_id or "").strip()
    cred_owner = (credential_owner_client_id or "").strip()
    params = order_params or {}

    # Check 1: Non-empty authenticated client ID
    if not cid:
        audit = record_security_audit_event(
            client_id="ANONYMOUS",
            run_id=rid,
            attempted_action="DISPATCH_ORDER",
            reason="Missing authenticated client_id on order attempt.",
            details=params,
        )
        raise OrderSafetyViolation("Order blocked: Missing authenticated client_id.", audit)

    # Check 2: Run / Slot ownership match
    if run_owner != cid:
        audit = record_security_audit_event(
            client_id=cid,
            run_id=rid,
            attempted_action="DISPATCH_ORDER",
            reason=f"Cross-client run slot violation: Slot owned by '{run_owner}', attempted by '{cid}'.",
            details={"run_owner": run_owner, "client_id": cid, **params},
        )
        raise OrderSafetyViolation(
            f"Order blocked: Run slot '{rid}' belongs to client '{run_owner}', not '{cid}'.",
            audit,
        )

    # Check 3: Broker credential ownership match
    if cred_owner != cid:
        audit = record_security_audit_event(
            client_id=cid,
            run_id=rid,
            attempted_action="DISPATCH_ORDER",
            reason=f"Cross-client credential violation: Credentials owned by '{cred_owner}', attempted by '{cid}'.",
            details={"cred_owner": cred_owner, "client_id": cid, **params},
        )
        raise OrderSafetyViolation(
            f"Order blocked: Broker credentials belong to client '{cred_owner}', not '{cid}'.",
            audit,
        )

    # Check 4: Parameter sanity check
    symbol = params.get("symbol") or params.get("contract_symbol")
    qty = params.get("quantity") or params.get("qty")
    if not symbol or (qty is not None and qty <= 0):
        audit = record_security_audit_event(
            client_id=cid,
            run_id=rid,
            attempted_action="DISPATCH_ORDER",
            reason="Invalid order parameters: Symbol missing or non-positive quantity.",
            details=params,
        )
        raise OrderSafetyViolation("Order blocked: Invalid contract symbol or quantity.", audit)

    # Check 5: Authoritative Trading Mode Match (Defense-in-depth)
    # The trading_mode on the order must match the authoritative stored mode in credential_service.
    # Protects against stale UI toggles or race conditions attempting to trade live when client is in paper (or vice versa).
    from credential_service import credential_service
    stored_mode = credential_service.get_trading_mode(cid)
    order_mode = (params.get("trading_mode") or "paper").strip().lower()

    if order_mode != stored_mode:
        audit = record_security_audit_event(
            client_id=cid,
            run_id=rid,
            attempted_action="DISPATCH_ORDER",
            reason=f"Trading mode mismatch: Order specified '{order_mode}', but client '{cid}' is configured for '{stored_mode}'.",
            details={"order_mode": order_mode, "stored_mode": stored_mode, **params},
        )
        raise OrderSafetyViolation(
            f"Order blocked: Mode mismatch. Order mode is '{order_mode}', but client account is configured for '{stored_mode}'.",
            audit,
        )

    return True
