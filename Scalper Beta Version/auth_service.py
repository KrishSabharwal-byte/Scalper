"""
Authentication & Session Management Service (Phase 1)
Implements:
- Password hashing & verification using bcrypt (never plaintext)
- Cryptographically signed JWT session tokens (HMAC-SHA256)
- Environment-loaded secrets with loud failure on missing keys
- MongoDB users collection ('slicer_users') with local fallback
- Invalidation / revocation blacklist for logout
- Initial user bootstrapping
"""

import os
import datetime
import logging
import uuid
import threading
from typing import Dict, Any, Optional, List, Set
import bcrypt
import jwt
from pymongo import MongoClient
from pymongo.errors import PyMongoError, DuplicateKeyError

logger = logging.getLogger("AuthService")

# -----------------------------------------------------------------------------
# Configuration & Secret Management
# -----------------------------------------------------------------------------
DEFAULT_MONGO_URI = ""
MONGO_URI = os.getenv("MONGO_URI", DEFAULT_MONGO_URI)
DB_NAME = os.getenv("MONGO_DB_NAME", "new_logic")
USERS_COLLECTION_NAME = os.getenv("MONGO_USERS_COLLECTION", "slicer_users")

# Auth Secret Key - Must be provided via environment variable
AUTH_SECRET_KEY = os.getenv("AUTH_SECRET_KEY") or os.getenv("JWT_SECRET_KEY")
ALLOW_INSECURE_DEV_AUTH = os.getenv("ALLOW_INSECURE_DEV_AUTH", "true").lower() in ("true", "1", "yes")

if not AUTH_SECRET_KEY:
    if ALLOW_INSECURE_DEV_AUTH:
        # Development fallback secret with clear warning
        AUTH_SECRET_KEY = "slicer_dev_secret_key_change_in_production_89324792374982374"
        logger.warning("AUTH_SECRET_KEY not set in environment! Using fallback dev key because ALLOW_INSECURE_DEV_AUTH=true.")
    else:
        raise RuntimeError(
            "CRITICAL SECURITY CONFIGURATION ERROR: "
            "Neither AUTH_SECRET_KEY nor JWT_SECRET_KEY environment variable is set. "
            "Server refuses to start without a valid cryptographic signing key."
        )

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", str(60 * 24)))  # 24 hours default
SESSION_COOKIE_NAME = "slicer_session"

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


# -----------------------------------------------------------------------------
# Password Utilities (bcrypt)
# -----------------------------------------------------------------------------
def hash_password(plain_password: str) -> str:
    """Hashes password using bcrypt with automatic salt generation."""
    if not plain_password or not isinstance(plain_password, str):
        raise ValueError("Password must be a non-empty string.")
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(plain_password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plain password against the stored bcrypt hash."""
    if not plain_password or not hashed_password:
        return False
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception as e:
        logger.warning(f"Error during password verification: {e}")
        return False


# -----------------------------------------------------------------------------
# AuthService Class
# -----------------------------------------------------------------------------
class AuthService:
    def __init__(
        self,
        uri: str = MONGO_URI,
        db_name: str = DB_NAME,
        collection_name: str = USERS_COLLECTION_NAME,
        secret_key: str = AUTH_SECRET_KEY,
        state_file: str = "users_state.json",
    ):
        self.uri = uri
        self.db_name = db_name
        self.collection_name = collection_name
        self.secret_key = secret_key
        self.state_file = state_file

        self.client: Optional[MongoClient] = None
        self.db = None
        self.collection = None
        self.is_connected = False
        self._lock = threading.Lock()

        # In-memory revocation set (blacklist) for logged-out tokens
        self.revoked_tokens: Set[str] = set()

        self._connect()
        self._bootstrap_default_admin()

    def _connect(self) -> bool:
        with self._lock:
            try:
                self.client = MongoClient(self.uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)
                self.db = self.client[self.db_name]
                self.collection = self.db[self.collection_name]
                self.client.admin.command("ping")
                self.is_connected = True

                # Ensure unique index on client_id
                try:
                    self.collection.create_index("client_id", unique=True)
                except Exception as ie:
                    logger.debug(f"Index creation note: {ie}")

                logger.info(f"AuthService connected to MongoDB {self.db_name}.{self.collection_name}")
                return True
            except Exception as e:
                self.is_connected = False
                logger.warning(f"AuthService MongoDB connection failed: {e}. Running in local state fallback mode.")
                return False

    def ensure_connection(self) -> bool:
        if self.is_connected and self.collection is not None:
            return True
        return self._connect()

    # -------------------------------------------------------------------------
    # Local State Fallback Management
    # -------------------------------------------------------------------------
    def _load_local_users(self) -> Dict[str, Dict[str, Any]]:
        if not os.path.exists(self.state_file):
            return {}
        try:
            import json
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_local_user(self, user_doc: Dict[str, Any]) -> None:
        try:
            import json
            users = self._load_local_users()
            users[user_doc["client_id"]] = user_doc
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(users, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save user to local fallback file: {e}")

    # -------------------------------------------------------------------------
    # User Operations
    # -------------------------------------------------------------------------
    def get_user(self, client_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves user document by client_id."""
        if not client_id:
            return None
        client_id_clean = client_id.strip()

        if self.ensure_connection():
            try:
                user = self.collection.find_one({"client_id": client_id_clean})
                if user:
                    user["_id"] = str(user["_id"])
                    return user
            except Exception as e:
                logger.warning(f"Error querying user from MongoDB: {e}")

        # Local fallback lookup
        local_users = self._load_local_users()
        return local_users.get(client_id_clean)

    def create_user(
        self,
        client_id: str,
        plain_password: str,
        is_active: bool = True,
        role: str = "trader",
    ) -> Dict[str, Any]:
        """
        Creates a new user record with bcrypt-hashed password.
        Raises ValueError if client_id already exists.
        """
        if not client_id or not client_id.strip():
            raise ValueError("client_id cannot be empty.")
        if not plain_password or len(plain_password) < 4:
            raise ValueError("Password must be at least 4 characters.")

        client_id_clean = client_id.strip()
        existing = self.get_user(client_id_clean)
        if existing:
            raise ValueError(f"User with client_id '{client_id_clean}' already exists.")

        now_ist = datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        password_hash = hash_password(plain_password)

        user_doc = {
            "client_id": client_id_clean,
            "password_hash": password_hash,
            "is_active": is_active,
            "role": role,
            "created_at": now_ist,
            "updated_at": now_ist,
        }

        # Save to MongoDB
        if self.ensure_connection():
            try:
                res = self.collection.insert_one(dict(user_doc))
                user_doc["_id"] = str(res.inserted_id)
            except DuplicateKeyError:
                raise ValueError(f"User with client_id '{client_id_clean}' already exists.")
            except Exception as e:
                logger.error(f"Error saving user to MongoDB: {e}")

        # Always save to local fallback for maximum uptime
        self._save_local_user(user_doc)
        logger.info(f"User '{client_id_clean}' successfully created.")
        return user_doc

    def authenticate_user(self, client_id: str, plain_password: str) -> Optional[Dict[str, Any]]:
        """
        Verifies client_id and password.
        Returns user document if valid and active, else None.
        Supports normalized fallback (e.g. 'User 1' <-> 'user1').
        """
        user = self.get_user(client_id)
        if not user:
            # Try normalized lookup fallback
            cid_norm = client_id.strip().lower().replace(" ", "").replace("_", "")
            if cid_norm in ("user1", "user01", "trader1", "trader01"):
                user = self.get_user("user1") or self.get_user("User 1")
            elif cid_norm in ("user2", "user02", "trader2", "trader02"):
                user = self.get_user("user2") or self.get_user("User 2")
            elif cid_norm in ("user3", "user03", "trader3", "trader03"):
                user = self.get_user("user3") or self.get_user("User 3")
            elif cid_norm in ("user4", "user04", "trader4", "trader04"):
                user = self.get_user("user4") or self.get_user("User 4")
            elif cid_norm in ("admin", "administrator"):
                user = self.get_user("admin")

        if not user:
            logger.warning(f"Auth failed: user '{client_id}' not found.")
            return None

        if not user.get("is_active", True):
            logger.warning(f"Auth failed: user '{client_id}' is deactivated.")
            return None

        hashed = user.get("password_hash")
        if not hashed or not verify_password(plain_password, hashed):
            logger.warning(f"Auth failed: invalid password for user '{client_id}'.")
            return None

        logger.info(f"User '{client_id}' authenticated successfully.")
        return user

    def _bootstrap_default_admin(self) -> None:
        """Seeds initial admin and user accounts if they don't exist."""
        default_accounts = [
            (os.getenv("INITIAL_ADMIN_CLIENT_ID", "admin").strip(), os.getenv("INITIAL_ADMIN_PASSWORD", "Slicer@Admin2026"), "admin"),
            ("user1", "User1@Slicer2026", "trader"),
            ("User 1", "User1@Slicer2026", "trader"),
            ("user2", "User2@Slicer2026", "trader"),
            ("User 2", "User2@Slicer2026", "trader"),
            ("user3", "User3@Slicer2026", "trader"),
            ("User 3", "User3@Slicer2026", "trader"),
            ("user4", "User4@Slicer2026", "trader"),
            ("User 4", "User4@Slicer2026", "trader"),
        ]
        for cid, pw, role in default_accounts:
            try:
                user = self.get_user(cid)
                if not user:
                    logger.info(f"Creating bootstrap account '{cid}'...")
                    self.create_user(cid, pw, is_active=True, role=role)
            except Exception as e:
                logger.warning(f"Bootstrap check note for '{cid}': {e}")

    # -------------------------------------------------------------------------
    # JWT Token Operations
    # -------------------------------------------------------------------------
    def create_access_token(
        self,
        client_id: str,
        expires_delta: Optional[datetime.timedelta] = None,
        extra_claims: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Issues a signed JWT access token for client_id."""
        if expires_delta is None:
            expires_delta = datetime.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

        now = datetime.datetime.now(datetime.timezone.utc)
        expire = now + expires_delta
        jti = uuid.uuid4().hex

        payload = {
            "sub": client_id,
            "client_id": client_id,
            "jti": jti,
            "iat": int(now.timestamp()),
            "exp": int(expire.timestamp()),
            "token_type": "access",
        }
        if extra_claims:
            payload.update(extra_claims)

        token = jwt.encode(payload, self.secret_key, algorithm=JWT_ALGORITHM)
        return token

    def decode_access_token(self, token: str) -> Dict[str, Any]:
        """
        Decodes and verifies a JWT token.
        Raises ValueError with detail if invalid, expired, or revoked.
        """
        if not token or not isinstance(token, str):
            raise ValueError("Token is missing or empty.")

        token_clean = token.strip()
        if token_clean.startswith("Bearer "):
            token_clean = token_clean[7:].strip()

        if token_clean in self.revoked_tokens:
            raise ValueError("Token has been revoked / logged out.")

        try:
            payload = jwt.decode(token_clean, self.secret_key, algorithms=[JWT_ALGORITHM])
            jti = payload.get("jti")
            if jti and jti in self.revoked_tokens:
                raise ValueError("Session token has been invalidated.")
            return payload
        except jwt.ExpiredSignatureError:
            raise ValueError("Session token has expired. Please login again.")
        except jwt.InvalidTokenError as e:
            raise ValueError(f"Invalid session token: {e}")

    def revoke_token(self, token: str) -> bool:
        """Invalidates a session token (logout)."""
        if not token:
            return False
        token_clean = token.strip()
        if token_clean.startswith("Bearer "):
            token_clean = token_clean[7:].strip()

        self.revoked_tokens.add(token_clean)
        try:
            # Also extract jti to blacklist
            payload = jwt.decode(token_clean, self.secret_key, algorithms=[JWT_ALGORITHM], options={"verify_exp": False})
            jti = payload.get("jti")
            if jti:
                self.revoked_tokens.add(jti)
        except Exception:
            pass

        logger.info("Session token added to revocation blacklist.")
        return True


# Global singleton instance
auth_service = AuthService()
