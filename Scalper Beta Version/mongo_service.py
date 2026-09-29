import datetime
import logging
import os
import threading
from typing import Dict, Any, Optional, List
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from bson import ObjectId

logger = logging.getLogger("MongoService")

try:
    from auth_service import DEFAULT_MONGO_URI
except ImportError:
    DEFAULT_MONGO_URI = None

MONGO_URI = os.getenv("MONGO_URI", DEFAULT_MONGO_URI)
DEFAULT_DB_NAME = os.getenv("MONGO_DB_NAME", "Scalper")
DB_PREFIX = os.getenv("MONGO_CLIENT_DB_PREFIX", "client_")
RUNS_STATE_COLLECTION = "runs_state"

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


class MongoService:
    """
    Multi-Tenant Scoped MongoDB Service for Scalper Database.
    Routes user trade history into designated user collections,
    and Astro signal files into 'Astro.{client_id}' collections (e.g. 'Astro.admin').
    All read/write operations strictly assert a resolved client_id.
    """

    def __init__(
        self,
        uri: Optional[str] = MONGO_URI,
        db_name: str = DEFAULT_DB_NAME,
        db_prefix: str = DB_PREFIX,
    ):
        self.uri = uri
        self.db_name = db_name
        self.db_prefix = db_prefix
        self.client: Optional[MongoClient] = None
        self.is_connected = False
        self._last_connect_time = 0.0
        self._lock = threading.Lock()
        self._in_memory_astro_files: Dict[str, List[Dict[str, Any]]] = {}
        self._active_file_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self._deleted_trades: Dict[str, Set[str]] = {}
        self._load_tombstones()
        self._connect()

    def _connect(self) -> bool:
        with self._lock:
            import time
            now = time.time()
            if now - getattr(self, "_last_connect_time", 0.0) < 30.0:
                return False
            self._last_connect_time = now
            if not self.uri:
                logger.info("No 'MONGO_URI' provided in environment. Operating in resilient offline fallback mode.")
                self.is_connected = False
                return False

            candidate_uris = [
                self.uri,
                "mongodb://crestviewcorporate_db_user:Crestviewcorporate@ac-kjmgfzp-shard-00-00.zfk4ahy.mongodb.net:27017/Scalper?ssl=true&authSource=admin&directConnection=true",
                "mongodb://crestviewcorporate_db_user:Crestviewcorporate@ac-kjmgfzp-shard-00-01.zfk4ahy.mongodb.net:27017/Scalper?ssl=true&authSource=admin&directConnection=true",
            ]

            for u in candidate_uris:
                if not u:
                    continue
                try:
                    self.client = MongoClient(u, serverSelectionTimeoutMS=2000, connectTimeoutMS=2000)
                    self.client.admin.command('ping')
                    self.is_connected = True
                    logger.info(f"MongoDB connected successfully to cluster. Database: {self.db_name}")
                    return True
                except Exception as e:
                    self.is_connected = False
                    logger.debug(f"MongoDB candidate connection note: {e}")

            logger.warning(f"All MongoDB connection candidates failed. Running in offline fallback mode.")
            return False

    def ensure_connection(self) -> bool:
        if self.is_connected and self.client is not None:
            return True
        return self._connect()

    def _require_client_id(self, client_id: Optional[str]) -> str:
        """Strict guard assertion ensuring client_id is resolved and non-empty."""
        if not client_id or not isinstance(client_id, str) or not client_id.strip():
            raise ValueError(
                "CRITICAL SECURITY GUARD: Operation rejected. A valid, non-empty 'client_id' "
                "is strictly required for per-client database operations."
            )
        return client_id.strip()

    def get_client_collection_name(self, client_id: str) -> str:
        """
        Maps client_id to its designated collection in Scalper:
          - 'admin' / 'Administrator' -> 'Admin'
          - 'user1' / 'User 1' / 'user_1' / 'trader_01' -> 'User 1'
          - 'user2' / 'User 2' / 'user_2' / 'trader_02' -> 'User 2'
          - 'user3' / 'User 3' / 'user_3' / 'trader_03' -> 'User 3'
        """
        cid = self._require_client_id(client_id)
        cid_clean = cid.strip()
        norm = cid_clean.lower().replace(" ", "").replace("_", "").replace("-", "")

        # 1. Direct explicit mapping for requested Scalper collections
        if norm in ("admin", "administrator"):
            return "Admin"
        elif norm in ("user1", "user01", "trader1", "trader01", "trader_01"):
            return "User 1"
        elif norm in ("user2", "user02", "trader2", "trader02", "trader_02"):
            return "User 2"
        elif norm in ("user3", "user03", "trader3", "trader03", "trader_03"):
            return "User 3"
        elif norm in ("user4", "user04", "trader4", "trader04", "trader_04"):
            return "User 4"

        # 2. Check if a collection already exists in database Scalper matching name
        if self.ensure_connection() and self.client is not None:
            try:
                existing_cols = self.client[self.db_name].list_collection_names()
                if cid_clean in existing_cols:
                    return cid_clean
                for col in existing_cols:
                    if col.lower().replace(" ", "").replace("_", "").replace("-", "") == norm:
                        return col
            except Exception:
                pass

        # 3. Dynamic formatting for userX / traderX
        if norm.startswith("user") and norm[4:].isdigit():
            return f"User {int(norm[4:])}"
        elif norm.startswith("trader") and norm[6:].isdigit():
            return f"User {int(norm[6:])}"

        return cid_clean

    def get_database(self):
        """Returns the primary Scalper MongoDB database object."""
        if not self.ensure_connection() or self.client is None:
            return None
        return self.client[self.db_name]

    def get_client_collection(self, client_id: str):
        """Returns the specific collection for client_id within Scalper."""
        db = self.get_database()
        if db is None:
            return None
        col_name = self.get_client_collection_name(client_id)
        return db[col_name]

    def get_client_db_name(self, client_id: str) -> str:
        """Maintains backward compatibility with legacy db_prefix checks."""
        cid = self._require_client_id(client_id)
        return f"{self.db_prefix}{cid}"

    def get_client_db(self, client_id: str):
        """Returns the database for client operations."""
        return self.get_database()

    def insert_trade(self, trade_data: Dict[str, Any], client_id: str, run_config: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """
        Inserts a closed trade into the client's dedicated collection in Scalper
        (e.g., Scalper['Admin'], Scalper['User 1'], Scalper['User 2']).
        """
        cid = self._require_client_id(client_id)
        try:
            col = self.get_client_collection(cid)
            if col is None:
                return None

            col_name = self.get_client_collection_name(cid)
            now_ist = datetime.datetime.now(IST)
            now_utc = datetime.datetime.now(datetime.timezone.utc)

            cfg = run_config or {}
            inst = cfg.get("instrument_name", "NIFTY")
            opt_type = cfg.get("option_type", "CE")
            mode = (
                trade_data.get("mode")
                or (cfg.get("trading_mode") if cfg else None)
                or "paper"
            ).strip().lower()
            if mode not in ("paper", "live"):
                mode = "paper"

            doc = {
                "type": "trade",
                "client_id": cid,
                "collection": col_name,
                "trade_id": trade_data.get("trade_id"),
                "run_id": trade_data.get("run_id"),
                "run_number": trade_data.get("run_number") or cfg.get("run_number", 1),
                "symbol": inst,
                "instrument": inst,
                "option_type": opt_type,
                "side": "CALL" if opt_type == "CE" else "PUT",
                "strike": cfg.get("strike"),
                "contract_symbol": trade_data.get("contract_symbol") or cfg.get("contract_symbol"),
                "label": trade_data.get("label"),
                "level_price": trade_data.get("level_price"),
                "entry_price": trade_data.get("fill_price") or trade_data.get("entry_price"),
                "fill_price": trade_data.get("fill_price"),
                "exit_price": trade_data.get("exit_price"),
                "quantity": trade_data.get("quantity"),
                "pnl_points": trade_data.get("pnl_points"),
                "pnl_rupees": trade_data.get("pnl_rupees"),
                "exit_reason": trade_data.get("exit_reason"),
                "entry_time": trade_data.get("entry_time") or trade_data.get("filled_at"),
                "exit_time": trade_data.get("exit_time") or trade_data.get("exited_at"),
                "mode": mode,
                "created_at": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
                "timestamp_utc": now_utc.isoformat(),
                "status": "CLOSED",
            }

            res = col.insert_one(doc)
            logger.info(f"Trade {trade_data.get('trade_id')} [{mode.upper()}] saved to MongoDB [{self.db_name}.{col_name}] [ID: {res.inserted_id}]")
            return str(res.inserted_id)
        except Exception as e:
            logger.error(f"Failed to insert trade into client '{cid}' collection: {e}")
            return None

    def insert_trade_async(self, trade_data: Dict[str, Any], client_id: str, run_config: Optional[Dict[str, Any]] = None) -> None:
        """Non-blocking background insert scoped to client_id."""
        self._require_client_id(client_id)
        threading.Thread(target=self.insert_trade, args=(trade_data, client_id, run_config), daemon=True).start()

    # -------------------------------------------------------------------------
    # Tombstone / Deletion Cache Management
    # -------------------------------------------------------------------------
    def _load_tombstones(self) -> None:
        try:
            import json, glob
            for fpath in glob.glob("deleted_trades_*.json"):
                cid_part = os.path.basename(fpath).replace("deleted_trades_", "").replace(".json", "")
                with open(fpath, "r", encoding="utf-8") as f:
                    self._deleted_trades[cid_part] = set(json.load(f))
        except Exception:
            pass

    def _save_tombstones(self, client_id: str) -> None:
        try:
            import json
            norm_cid = self.get_client_collection_name(client_id).lower().replace(" ", "_")
            fpath = f"deleted_trades_{norm_cid}.json"
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(list(self._deleted_trades.get(norm_cid, set())), f)
        except Exception as e:
            logger.debug(f"Note saving deleted trades tombstone: {e}")

    def _is_tombstoned(self, client_id: str, doc: Dict[str, Any]) -> bool:
        norm_cid = self.get_client_collection_name(client_id).lower().replace(" ", "_")
        deleted_set = self._deleted_trades.get(norm_cid)
        if not deleted_set:
            return False
        identifiers = [
            str(doc.get("_id") or "").strip(),
            str(doc.get("trade_id") or "").strip(),
            str(doc.get("id") or "").strip(),
            str(doc.get("entry_time") or "").strip(),
            str(doc.get("exit_time") or "").strip(),
            f"{doc.get('run_id')}_{doc.get('entry_time')}".strip(),
            str(doc.get("entry_order_id") or "").strip(),
            str(doc.get("exit_order_id") or "").strip(),
        ]
        return any(x and x in deleted_set for x in identifiers)

    def get_recent_trades(self, client_id: str, limit: int = 50, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetches recent trades strictly from client's dedicated collection in Scalper with optional mode filter."""
        cid = self._require_client_id(client_id)
        try:
            col = self.get_client_collection(cid)
            if col is None:
                return []
            base_query: Dict[str, Any] = {"$or": [{"type": "trade"}, {"trade_id": {"$exists": True}}]}
            if mode and mode.strip().lower() in ("paper", "live"):
                query: Dict[str, Any] = {"$and": [base_query, {"mode": mode.strip().lower()}]}
            else:
                query = base_query
            cursor = col.find(query).sort("_id", -1).limit(limit + 50)
            results = []
            for doc in cursor:
                doc["_id"] = str(doc["_id"])
                if not self._is_tombstoned(cid, doc):
                    results.append(doc)
                if len(results) >= limit:
                    break
            return results
        except Exception as e:
            logger.error(f"Failed to fetch trades for client '{cid}': {e}")
            return []

    def clear_trades(self, client_id: str, instrument: Optional[str] = None) -> bool:
        """Clears trade history only within client's dedicated collection in Scalper."""
        cid = self._require_client_id(client_id)
        norm_cid = self.get_client_collection_name(cid).lower().replace(" ", "_")
        try:
            # Mark all existing trades as tombstoned
            existing = self.get_recent_trades(cid, limit=500)
            del_set = self._deleted_trades.setdefault(norm_cid, set())
            for t in existing:
                for k in ["_id", "trade_id", "id", "entry_time", "exit_time"]:
                    val = str(t.get(k) or "").strip()
                    if val: del_set.add(val)
                comp = f"{t.get('run_id')}_{t.get('entry_time')}".strip()
                if comp: del_set.add(comp)
            self._save_tombstones(cid)

            col = self.get_client_collection(cid)
            if col is not None:
                query: Dict[str, Any] = {"$or": [{"type": "trade"}, {"trade_id": {"$exists": True}}]}
                if instrument:
                    query = {"$and": [query, {"instrument": instrument.upper()}]}
                try:
                    col.delete_many(query)
                except Exception as de:
                    logger.warning(f"MongoDB write delete note during clear_trades: {de}")
            return True
        except Exception as e:
            logger.error(f"Failed to clear trades for client '{cid}': {e}")
            return True

    def delete_trade(self, client_id: str, trade_id: str) -> bool:
        """Deletes a single trade by trade_id, _id, id, entry_time, or order_id from client's dedicated collection."""
        cid = self._require_client_id(client_id)
        if not trade_id:
            return False
        tid_str = str(trade_id).strip()
        norm_cid = self.get_client_collection_name(cid).lower().replace(" ", "_")
        
        # 1. Always record in persistent tombstone blacklist
        del_set = self._deleted_trades.setdefault(norm_cid, set())
        del_set.add(tid_str)
        self._save_tombstones(cid)

        # 2. Attempt direct MongoDB deletion
        try:
            col = self.get_client_collection(cid)
            if col is not None:
                from bson import ObjectId
                or_clauses: List[Dict[str, Any]] = [
                    {"trade_id": tid_str},
                    {"_id": tid_str},
                    {"id": tid_str},
                    {"entry_time": tid_str},
                    {"exit_time": tid_str},
                    {"entry_order_id": tid_str},
                    {"exit_order_id": tid_str},
                ]
                if "_" in tid_str:
                    parts = tid_str.split("_", 1)
                    or_clauses.append({"$and": [{"run_id": parts[0]}, {"entry_time": parts[1]}]})
                try:
                    or_clauses.append({"_id": ObjectId(tid_str)})
                except Exception:
                    pass
                res = col.delete_one({"$or": or_clauses})
                if res.deleted_count > 0:
                    logger.info(f"Deleted trade '{trade_id}' from client '{cid}' collection.")
        except Exception as e:
            logger.warning(f"MongoDB write delete note for '{trade_id}' (tombstoned locally): {e}")

        return True

    def insert_audit_log(self, audit_data: Dict[str, Any], client_id: str) -> Optional[str]:
        """Inserts an audit log record into client's dedicated collection in SlicerNS."""
        cid = self._require_client_id(client_id)
        try:
            col = self.get_client_collection(cid)
            if col is None:
                return None
            doc = dict(audit_data)
            doc["type"] = "audit_log"
            doc["client_id"] = cid
            res = col.insert_one(doc)
            return str(res.inserted_id)
        except Exception as e:
            logger.error(f"Failed to insert audit log for client '{cid}': {e}")
            return None

    def save_client_state(self, client_id: str, state_data: Dict[str, Any]) -> bool:
        """Persists multi-slot run state document in SlicerNS['runs_state']."""
        cid = self._require_client_id(client_id)
        try:
            db = self.get_database()
            if db is None:
                return False
            now_ist = datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            state_doc = {
                "client_id": cid,
                "collection": self.get_client_collection_name(cid),
                "updated_at": now_ist,
                "state": state_data,
            }
            db[RUNS_STATE_COLLECTION].replace_one({"client_id": cid}, state_doc, upsert=True)
            return True
        except Exception as e:
            logger.error(f"Failed to save state in DB for client '{cid}': {e}")
            return False

    def load_client_state(self, client_id: str) -> Optional[Dict[str, Any]]:
        """Loads multi-slot run state document from SlicerNS['runs_state']."""
        cid = self._require_client_id(client_id)
        try:
            db = self.get_database()
            if db is None:
                return None
            doc = db[RUNS_STATE_COLLECTION].find_one({"client_id": cid})
            if doc and "state" in doc:
                return doc["state"]
            return None
        except Exception as e:
            logger.error(f"Failed to load state from DB for client '{cid}': {e}")
            return False

    # -------------------------------------------------------------------
    # Astro Report File Storage (Collection: Astro.{client_id})
    # -------------------------------------------------------------------
    def get_astro_collection_name(self, client_id: str) -> str:
        """Returns the Astro collection name in Scalper database, e.g. Astro.admin, Astro.user1."""
        cid = self._require_client_id(client_id)
        norm = cid.strip().lower().replace(" ", "").replace("_", "").replace("-", "")
        return f"Astro.{norm}"

    def get_astro_collection(self, client_id: str):
        """Returns MongoDB collection object for client's Astro reports."""
        db = self.get_database()
        if db is None:
            return None
        col_name = self.get_astro_collection_name(client_id)
        return db[col_name]

    def save_astro_file(
        self,
        client_id: str,
        filename: str,
        content: str,
        row_count: int,
        is_active: bool = True,
    ) -> str:
        """
        Stores an uploaded Astro CSV in Scalper['Astro.{client_id}'] with active flag.
        Maintains an in-memory fallback for offline test environments.
        """
        cid = self._require_client_id(client_id)
        now_ist = datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

        doc = {
            "type": "astro_file",
            "client_id": cid,
            "filename": filename,
            "content": content,
            "row_count": row_count,
            "uploaded_at": now_ist,
            "active": is_active,
        }

        # 1. MongoDB collection insert
        file_id = None
        try:
            col = self.get_astro_collection(cid)
            if col is not None:
                if is_active:
                    col.update_many({"type": "astro_file"}, {"$set": {"active": False}})
                res = col.insert_one(doc)
                file_id = str(res.inserted_id)
                doc["_id"] = file_id
        except Exception as e:
            logger.warning(f"Failed to insert astro file into MongoDB: {e}")

        # 2. In-memory fallback
        if not file_id:
            import uuid
            file_id = f"astro-{uuid.uuid4().hex[:12]}"
            doc["_id"] = file_id

        user_files = self._in_memory_astro_files.setdefault(cid, [])
        if is_active:
            for f in user_files:
                f["active"] = False
            self._active_file_cache[cid] = dict(doc)
        user_files.insert(0, doc)
        return file_id

    def list_astro_files(self, client_id: str) -> List[Dict[str, Any]]:
        """Returns list of uploaded Astro reports for client, sorted newest first."""
        cid = self._require_client_id(client_id)
        try:
            col = self.get_astro_collection(cid)
            if col is not None:
                cursor = col.find({"type": "astro_file"}, {"content": 0}).sort("uploaded_at", -1)
                results = []
                for doc in cursor:
                    doc["_id"] = str(doc["_id"])
                    doc["file_id"] = doc["_id"]
                    results.append(doc)
                if results:
                    return results
        except Exception as e:
            logger.warning(f"Note listing astro files from DB: {e}")

        # Fallback to in-memory
        mem_files = self._in_memory_astro_files.get(cid, [])
        res_mem = []
        for f in mem_files:
            item = {k: v for k, v in f.items() if k != "content"}
            item["file_id"] = str(item.get("_id", ""))
            res_mem.append(item)
        return res_mem

    def get_active_astro_file(self, client_id: str, use_cache: bool = True) -> Optional[Dict[str, Any]]:
        """
        Returns the active Astro file document. Uses in-memory cache to guarantee zero
        flicker or drops during rapid ticks, auto-selecting most recent if none marked active.
        """
        cid = self._require_client_id(client_id)
        if not hasattr(self, "_active_file_cache"):
            self._active_file_cache = {}

        if use_cache and cid in self._active_file_cache and self._active_file_cache[cid] is not None:
            return dict(self._active_file_cache[cid])

        doc = None
        try:
            col = self.get_astro_collection(cid)
            if col is not None:
                # First check active
                doc = col.find_one({"type": "astro_file", "active": True})
                if not doc:
                    # Auto-select most recent
                    doc = col.find_one({"type": "astro_file"}, sort=[("uploaded_at", -1)])
                    if doc:
                        col.update_one({"_id": doc["_id"]}, {"$set": {"active": True}})
                        doc["active"] = True
                if doc:
                    doc["_id"] = str(doc["_id"])
                    self._active_file_cache[cid] = dict(doc)
                    return doc
        except Exception as e:
            logger.warning(f"Note getting active astro file from DB: {e}")

        # In-memory fallback
        mem_files = self._in_memory_astro_files.get(cid, [])
        active_doc = next((f for f in mem_files if f.get("active")), None)
        if not active_doc and mem_files:
            active_doc = mem_files[0]
            active_doc["active"] = True
        if active_doc:
            self._active_file_cache[cid] = dict(active_doc)
            return dict(active_doc)

        self._active_file_cache[cid] = None
        return None

    def set_active_astro_file(self, client_id: str, file_id: str) -> bool:
        """Sets the specified file as active and unsets others."""
        cid = self._require_client_id(client_id)
        if not hasattr(self, "_active_file_cache"):
            self._active_file_cache = {}
        self._active_file_cache.pop(cid, None)

        matched = False
        try:
            col = self.get_astro_collection(cid)
            if col is not None:
                col.update_many({"type": "astro_file"}, {"$set": {"active": False}})
                try:
                    obj_id = ObjectId(file_id)
                    res = col.update_one({"_id": obj_id}, {"$set": {"active": True}})
                    matched = res.matched_count > 0
                except Exception:
                    res = col.update_one({"_id": file_id}, {"$set": {"active": True}})
                    matched = res.matched_count > 0
        except Exception as e:
            logger.warning(f"Note setting active astro file in DB: {e}")

        mem_files = self._in_memory_astro_files.get(cid, [])
        for f in mem_files:
            if str(f.get("_id")) == str(file_id):
                f["active"] = True
                matched = True
            else:
                f["active"] = False
        return matched

    def delete_astro_file(self, client_id: str, file_id: str) -> bool:
        """Deletes an Astro report. If active, selects the next most recent as active."""
        cid = self._require_client_id(client_id)
        if not hasattr(self, "_active_file_cache"):
            self._active_file_cache = {}
        self._active_file_cache.pop(cid, None)

        deleted = False
        try:
            col = self.get_astro_collection(cid)
            if col is not None:
                try:
                    obj_id = ObjectId(file_id)
                    res = col.delete_one({"_id": obj_id})
                    deleted = res.deleted_count > 0
                except Exception:
                    res = col.delete_one({"_id": file_id})
                    deleted = res.deleted_count > 0

                # If deleted, ensure an active file exists if any remain
                if deleted:
                    active_remains = col.find_one({"type": "astro_file", "active": True})
                    if not active_remains:
                        recent = col.find_one({"type": "astro_file"}, sort=[("uploaded_at", -1)])
                        if recent:
                            col.update_one({"_id": recent["_id"]}, {"$set": {"active": True}})
                            recent["_id"] = str(recent["_id"])
                            self._active_file_cache[cid] = recent
        except Exception as e:
            logger.warning(f"Note deleting astro file from DB: {e}")

        mem_files = self._in_memory_astro_files.get(cid, [])
        idx = next((i for i, f in enumerate(mem_files) if str(f.get("_id")) == str(file_id)), None)
        if idx is not None:
            was_active = mem_files[idx].get("active", False)
            mem_files.pop(idx)
            deleted = True
            if was_active and mem_files:
                mem_files[0]["active"] = True
                self._active_file_cache[cid] = dict(mem_files[0])

        return deleted

    def get_pnl_summary(
        self,
        client_id: str,
        period: str,
        ref_date: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Computes gross P&L summary grouped by instrument for a given time period.

        Args:
            client_id: The client whose collection to query.
            period: One of "day", "week", "month", "custom".
            ref_date: ISO date string (YYYY-MM-DD) as the reference date; defaults to today IST.
            mode: Optional "paper" or "live" filter.

        Returns:
            A dict with keys: range, by_instrument (list), total (dict).
        """
        cid = self._require_client_id(client_id)

        # ── Resolve reference date in IST ─────────────────────────────────────
        now_ist = datetime.datetime.now(IST)
        if ref_date:
            try:
                ref_naive = datetime.datetime.strptime(ref_date.strip()[:10], "%Y-%m-%d")
                ref = ref_naive.replace(tzinfo=IST)
            except ValueError:
                ref = now_ist
        else:
            ref = now_ist

        ref_date_only = ref.date()

        # ── Compute IST date range [range_start, range_end) ───────────────────
        if period == "day":
            range_start = datetime.datetime(ref_date_only.year, ref_date_only.month, ref_date_only.day, 0, 0, 0, tzinfo=IST)
            range_end   = range_start + datetime.timedelta(days=1)
        elif period == "week":
            # Mon=0 … Sun=6 → Monday of the week containing ref
            monday = ref_date_only - datetime.timedelta(days=ref_date_only.weekday())
            range_start = datetime.datetime(monday.year, monday.month, monday.day, 0, 0, 0, tzinfo=IST)
            range_end   = range_start + datetime.timedelta(days=7)
        elif period == "month":
            range_start = datetime.datetime(ref_date_only.year, ref_date_only.month, 1, 0, 0, 0, tzinfo=IST)
            # First day of next month
            if ref_date_only.month == 12:
                range_end = datetime.datetime(ref_date_only.year + 1, 1, 1, 0, 0, 0, tzinfo=IST)
            else:
                range_end = datetime.datetime(ref_date_only.year, ref_date_only.month + 1, 1, 0, 0, 0, tzinfo=IST)
        else:
            # "custom" — treat same as "day" for ref_date
            range_start = datetime.datetime(ref_date_only.year, ref_date_only.month, ref_date_only.day, 0, 0, 0, tzinfo=IST)
            range_end   = range_start + datetime.timedelta(days=1)

        range_from_str = range_start.strftime("%Y-%m-%d %H:%M:%S")
        range_to_str   = (range_end - datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")

        # ── Accumulator per instrument ─────────────────────────────────────────
        def _empty_bucket():
            return {"gross_profit": 0.0, "gross_loss": 0.0, "net_pnl": 0.0,
                    "trade_count": 0, "win_count": 0, "loss_count": 0}

        buckets: Dict[str, Dict[str, Any]] = {}

        # ── Early return if offline ────────────────────────────────────────────
        if not self.ensure_connection():
            return {
                "range": {"from": range_from_str, "to": range_to_str},
                "by_instrument": [],
                "total": _empty_bucket(),
            }

        try:
            col = self.get_client_collection(cid)
            if col is None:
                return {
                    "range": {"from": range_from_str, "to": range_to_str},
                    "by_instrument": [],
                    "total": _empty_bucket(),
                }

            # Same base query pattern used by get_recent_trades / clear_trades
            base_query: Dict[str, Any] = {"$or": [{"type": "trade"}, {"trade_id": {"$exists": True}}]}
            if mode and mode.strip().lower() in ("paper", "live"):
                base_query = {"$and": [base_query, {"mode": mode.strip().lower()}]}

            cursor = col.find(base_query, {
                "instrument": 1, "pnl_rupees": 1,
                "exit_time": 1, "entry_time": 1, "created_at": 1,
            })

            for doc in cursor:
                # ── Resolve timestamp field ─────────────────────────────────
                ts_raw = doc.get("exit_time") or doc.get("entry_time") or doc.get("created_at")
                if not ts_raw:
                    continue
                try:
                    ts_str = str(ts_raw).strip()
                    # Timestamps are stored as IST strings "YYYY-MM-DD HH:MM:SS"
                    # or ISO with offset; parse defensively
                    if "T" in ts_str:
                        if ts_str.endswith("Z"):
                            ts_dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(IST)
                        elif "+" in ts_str[10:] or (ts_str.count("-") > 2):
                            ts_dt = datetime.datetime.fromisoformat(ts_str).astimezone(IST)
                        else:
                            ts_dt = datetime.datetime.fromisoformat(ts_str).replace(tzinfo=IST)
                    else:
                        ts_dt = datetime.datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                except (ValueError, TypeError):
                    continue

                # ── Check if within range ──────────────────────────────────
                if not (range_start <= ts_dt < range_end):
                    continue

                # ── Instrument key ─────────────────────────────────────────
                inst = str(doc.get("instrument") or "UNKNOWN").upper().strip()

                pnl = float(doc.get("pnl_rupees") or 0)

                if inst not in buckets:
                    buckets[inst] = _empty_bucket()

                b = buckets[inst]
                b["trade_count"] += 1
                if pnl > 0:
                    b["gross_profit"] += pnl
                    b["win_count"] += 1
                elif pnl < 0:
                    b["gross_loss"] += pnl
                    b["loss_count"] += 1

        except Exception as e:
            logger.error(f"Failed to compute P&L summary for client '{cid}': {e}")
            return {
                "range": {"from": range_from_str, "to": range_to_str},
                "by_instrument": [],
                "total": _empty_bucket(),
            }

        # ── Finalise net_pnl and build output list ─────────────────────────
        by_instrument = []
        total = _empty_bucket()

        for inst, b in sorted(buckets.items()):
            b["net_pnl"] = round(b["gross_profit"] + b["gross_loss"], 2)
            b["gross_profit"] = round(b["gross_profit"], 2)
            b["gross_loss"]   = round(b["gross_loss"], 2)
            row = {"instrument": inst, **b}
            by_instrument.append(row)

            total["gross_profit"] += b["gross_profit"]
            total["gross_loss"]   += b["gross_loss"]
            total["trade_count"]  += b["trade_count"]
            total["win_count"]    += b["win_count"]
            total["loss_count"]   += b["loss_count"]

        total["net_pnl"]      = round(total["gross_profit"] + total["gross_loss"], 2)
        total["gross_profit"] = round(total["gross_profit"], 2)
        total["gross_loss"]   = round(total["gross_loss"], 2)

        return {
            "range": {"from": range_from_str, "to": range_to_str},
            "by_instrument": by_instrument,
            "total": total,
        }

    def get_status(self, client_id: Optional[str] = None) -> Dict[str, Any]:
        col_name = self.get_client_collection_name(client_id) if client_id else None
        return {
            "is_connected": self.is_connected,
            "database": self.db_name,
            "scoped_collection": col_name or "unscoped",
            "scoped_database": self.db_name,
            "db_prefix": self.db_prefix,
        }


# Global singleton instance
mongo_service = MongoService()
