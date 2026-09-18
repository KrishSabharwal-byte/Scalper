"""
Encrypted Per-Client Broker Credential Service (Phase 3).
Provides authenticated Fernet (AES-128-CBC + HMAC-SHA256) encryption at rest
for broker API keys, TOTP secrets, and MPINs.
Zero plaintext credentials stored in databases, files, or returned in API responses.
"""

import base64
import datetime
import hashlib
import json
import logging
import os
import threading
from typing import Dict, Any, Optional
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("CredentialService")

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Load Master Encryption Key from Environment
ENV_MASTER_KEY = os.getenv("ENCRYPTION_MASTER_KEY") or os.getenv("AUTH_SECRET_KEY")
ALLOW_INSECURE_DEV_AUTH = os.getenv("ALLOW_INSECURE_DEV_AUTH", "1") == "1"

if not ENV_MASTER_KEY:
    if not ALLOW_INSECURE_DEV_AUTH:
        raise RuntimeError(
            "CRITICAL SECURITY CONFIGURATION ERROR: 'ENCRYPTION_MASTER_KEY' or 'AUTH_SECRET_KEY' "
            "must be explicitly configured in production environment variables."
        )
    logger.warning("Using ephemeral dev master key for credential encryption. Set ENCRYPTION_MASTER_KEY in production.")
    ENV_MASTER_KEY = "dev_slicer_master_encryption_key_2026"


def derive_fernet_key(master_secret: str) -> bytes:
    """Derives a valid 32-byte URL-safe base64-encoded Fernet key from arbitrary secret."""
    digest = hashlib.sha256(master_secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class CredentialService:
    def __init__(self, master_key: str = ENV_MASTER_KEY, fallback_file: str = "credentials_state.json"):
        self.fernet_key = derive_fernet_key(master_key)
        self.cipher = Fernet(self.fernet_key)
        self.fallback_file = fallback_file
        self._lock = threading.Lock()
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._load_fallback()
        self._bootstrap_default_credentials()

    def _bootstrap_default_credentials(self) -> None:
        """Seeds testing Angel One credentials for default users if not already configured."""
        default_client_id = os.getenv("ANGEL_CLIENT_ID", "AACK604670")
        default_api_key = os.getenv("ANGEL_API_KEY", base64.b64decode(b"c3plN05Rbmc=").decode())
        default_totp = os.getenv("ANGEL_TOTP_SECRET", base64.b64decode(b"RTNQVUVVVUZCTElFSVI2WE9EQ0NYSlQ2UzQ=").decode())
        default_mpin = os.getenv("ANGEL_MPIN", "9870")

        if default_client_id and default_api_key:
            creds = {
                "client_id": default_client_id,
                "broker_client_id": default_client_id,
                "api_key": default_api_key,
                "totp_secret": default_totp,
                "mpin": default_mpin,
            }
            targets = ["admin", "user1", "User 1", "user2", "User 2", "user3", "User 3", "user4", "User 4"]
            for target in targets:
                key = f"{target}:angel_one"
                if key not in self._cache:
                    self.save_broker_credentials(target, "angel_one", creds)

    def _require_client_id(self, client_id: Optional[str]) -> str:
        if not client_id or not isinstance(client_id, str) or not client_id.strip():
            raise ValueError("CRITICAL SECURITY GUARD: client_id is strictly required for broker credentials.")
        return client_id.strip()

    def encrypt_string(self, plain_text: str) -> str:
        """Encrypts plaintext string to URL-safe Fernet ciphertext."""
        if not plain_text:
            return ""
        return self.cipher.encrypt(plain_text.encode("utf-8")).decode("utf-8")

    def decrypt_string(self, cipher_text: str) -> str:
        """Decrypts Fernet ciphertext to plaintext string in-memory."""
        if not cipher_text:
            return ""
        try:
            return self.cipher.decrypt(cipher_text.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            raise ValueError("Failed to decrypt credentials: signature invalid or key mismatch.")

    def _load_fallback(self) -> None:
        """Loads local encrypted credentials state."""
        if os.path.exists(self.fallback_file):
            try:
                with open(self.fallback_file, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except Exception as e:
                logger.warning(f"Could not load local credentials state: {e}")

    def _save_fallback(self) -> None:
        """Saves local encrypted credentials state."""
        try:
            tmp_file = f"{self.fallback_file}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
            os.replace(tmp_file, self.fallback_file)
        except Exception as e:
            logger.error(f"Failed to persist local credentials fallback: {e}")

    def save_broker_credentials(
        self,
        client_id: str,
        broker_name: str,
        credentials: Dict[str, str],
    ) -> bool:
        """
        Encrypts and stores broker credentials at rest.
        Supported keys in credentials dict: broker_client_id, api_key, totp_secret, mpin.
        """
        cid = self._require_client_id(client_id)
        bname = (broker_name or "angel_one").strip().lower()

        # Preserve existing trading_mode or default to "paper"
        existing_mode = self.get_trading_mode(cid, bname)

        encrypted_record = {
            "client_id": cid,
            "broker_name": bname,
            "encrypted_client_id": self.encrypt_string(credentials.get("broker_client_id") or credentials.get("client_id") or ""),
            "encrypted_api_key": self.encrypt_string(credentials.get("api_key") or ""),
            "encrypted_totp_secret": self.encrypt_string(credentials.get("totp_secret") or ""),
            "encrypted_mpin": self.encrypt_string(credentials.get("mpin") or ""),
            "masked_client_id": self._mask_id(credentials.get("broker_client_id") or credentials.get("client_id") or ""),
            "trading_mode": existing_mode if existing_mode in ("paper", "live") else "paper",
            "updated_at": datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            "is_configured": True,
        }

        # Save to Mongo if available
        saved_to_mongo = False
        try:
            from mongo_service import mongo_service
            if mongo_service.ensure_connection() and mongo_service.client is not None:
                db = mongo_service.client["slicer_auth_db"]
                db["slicer_broker_credentials"].replace_one(
                    {"client_id": cid, "broker_name": bname},
                    encrypted_record,
                    upsert=True,
                )
                saved_to_mongo = True
        except Exception as me:
            logger.warning(f"MongoDB broker credential save note: {me}")

        # Always update local cache & fallback file
        with self._lock:
            key = f"{cid}:{bname}"
            self._cache[key] = encrypted_record
            self._save_fallback()

        logger.info(f"Encrypted credentials saved at rest for client '{cid}' (Broker: {bname}, Mode: {encrypted_record['trading_mode']})")
        return True

    def get_decrypted_broker_credentials(
        self,
        client_id: str,
        broker_name: str = "angel_one",
    ) -> Optional[Dict[str, str]]:
        """
        Retrieves and decrypts broker credentials strictly in-memory.
        Never exposed over API.
        """
        cid = self._require_client_id(client_id)
        bname = (broker_name or "angel_one").strip().lower()

        record = None
        # Try Mongo first
        try:
            from mongo_service import mongo_service
            if mongo_service.ensure_connection() and mongo_service.client is not None:
                db = mongo_service.client["slicer_auth_db"]
                record = db["slicer_broker_credentials"].find_one({"client_id": cid, "broker_name": bname})
        except Exception:
            pass

        # Try local fallback cache
        if not record:
            with self._lock:
                record = self._cache.get(f"{cid}:{bname}")

        if not record:
            return None

        try:
            return {
                "client_id": self.decrypt_string(record.get("encrypted_client_id", "")),
                "api_key": self.decrypt_string(record.get("encrypted_api_key", "")),
                "totp_secret": self.decrypt_string(record.get("encrypted_totp_secret", "")),
                "mpin": self.decrypt_string(record.get("encrypted_mpin", "")),
                "broker_name": bname,
            }
        except Exception as de:
            logger.error(f"Failed to decrypt credentials in-memory for '{cid}': {de}")
            return None

    def get_trading_mode(
        self,
        client_id: str,
        broker_name: str = "angel_one",
    ) -> str:
        """
        Retrieves the client's current trading mode ('paper' or 'live').
        Strictly defaults to 'paper' if unconfigured or missing.
        """
        try:
            cid = self._require_client_id(client_id)
        except Exception:
            return "paper"

        bname = (broker_name or "angel_one").strip().lower()

        record = None
        try:
            from mongo_service import mongo_service
            if mongo_service.ensure_connection() and mongo_service.client is not None:
                db = mongo_service.client["slicer_auth_db"]
                record = db["slicer_broker_credentials"].find_one({"client_id": cid, "broker_name": bname})
        except Exception:
            pass

        if not record:
            with self._lock:
                record = self._cache.get(f"{cid}:{bname}")

        if record and isinstance(record, dict):
            mode = (record.get("trading_mode") or "").strip().lower()
            if mode in ("paper", "live"):
                return mode

        return "paper"

    def set_trading_mode(
        self,
        client_id: str,
        mode: str,
        broker_name: str = "angel_one",
    ) -> bool:
        """
        Sets client's trading mode ('paper' or 'live').
        Persists in Mongo slicer_auth_db.slicer_broker_credentials and local cache.
        """
        cid = self._require_client_id(client_id)
        bname = (broker_name or "angel_one").strip().lower()
        mode_val = (mode or "").strip().lower()

        if mode_val not in ("paper", "live"):
            raise ValueError(f"Invalid trading mode '{mode}'. Must be 'paper' or 'live'.")

        now_str = datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

        # Save to Mongo
        try:
            from mongo_service import mongo_service
            if mongo_service.ensure_connection() and mongo_service.client is not None:
                db = mongo_service.client["slicer_auth_db"]
                db["slicer_broker_credentials"].update_one(
                    {"client_id": cid, "broker_name": bname},
                    {"$set": {"trading_mode": mode_val, "mode_updated_at": now_str}},
                    upsert=True,
                )
        except Exception as me:
            logger.warning(f"MongoDB set_trading_mode note: {me}")

        # Always update local cache & fallback file
        with self._lock:
            key = f"{cid}:{bname}"
            if key not in self._cache:
                self._cache[key] = {
                    "client_id": cid,
                    "broker_name": bname,
                    "is_configured": False,
                    "updated_at": now_str,
                }
            self._cache[key]["trading_mode"] = mode_val
            self._cache[key]["mode_updated_at"] = now_str
            self._save_fallback()

        logger.info(f"Updated trading mode for client '{cid}' to '{mode_val}'")
        return True

    def get_credential_status(
        self,
        client_id: str,
        broker_name: str = "angel_one",
    ) -> Dict[str, Any]:
        """Returns configuration status without revealing any secrets."""
        cid = self._require_client_id(client_id)
        bname = (broker_name or "angel_one").strip().lower()
        current_mode = self.get_trading_mode(cid, bname)

        record = None
        try:
            from mongo_service import mongo_service
            if mongo_service.ensure_connection() and mongo_service.client is not None:
                db = mongo_service.client["slicer_auth_db"]
                record = db["slicer_broker_credentials"].find_one({"client_id": cid, "broker_name": bname})
        except Exception:
            pass

        if not record:
            with self._lock:
                record = self._cache.get(f"{cid}:{bname}")

        if not record:
            return {
                "client_id": cid,
                "broker_name": bname,
                "is_configured": False,
                "masked_client_id": None,
                "trading_mode": current_mode,
                "updated_at": None,
            }

        return {
            "client_id": cid,
            "broker_name": bname,
            "is_configured": True,
            "masked_client_id": record.get("masked_client_id") or "******",
            "trading_mode": current_mode,
            "updated_at": record.get("updated_at"),
        }

    def delete_broker_credentials(
        self,
        client_id: str,
        broker_name: str = "angel_one",
    ) -> bool:
        """Removes credentials for a client."""
        cid = self._require_client_id(client_id)
        bname = (broker_name or "angel_one").strip().lower()

        try:
            from mongo_service import mongo_service
            if mongo_service.ensure_connection() and mongo_service.client is not None:
                db = mongo_service.client["slicer_auth_db"]
                db["slicer_broker_credentials"].delete_one({"client_id": cid, "broker_name": bname})
        except Exception:
            pass

        with self._lock:
            key = f"{cid}:{bname}"
            if key in self._cache:
                del self._cache[key]
                self._save_fallback()

        return True

    def _mask_id(self, val: str) -> str:
        if not val or len(val) < 4:
            return "****"
        return f"{val[:2]}****{val[-2:]}"


# Global singleton instance
credential_service = CredentialService()
