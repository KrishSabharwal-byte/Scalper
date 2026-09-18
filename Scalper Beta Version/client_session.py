"""
ClientSession Multi-Tenant Container (Phase 4).
Encapsulates each client's independent trading engine, broker feed,
rate-limit mutex, SSE queues, and background tick streaming workers.
Zero shared global state between clients.
"""

import asyncio
import datetime
import json
import logging
import os
import threading
import time
from typing import Dict, Any, Optional, List

from slice_trading_engine import (
    RunManager,
    ScalperRunConfig,
    calculate_nearest_strike,
    INSTRUMENT_CONFIG,
    IST,
)
from angel_one_service import (
    AngelOneFeed,
    ANGEL_CLIENT_ID,
    ANGEL_API_KEY,
    ANGEL_TOTP_SECRET,
    ANGEL_MPIN,
)
from mongo_service import mongo_service
from credential_service import credential_service
from astro_signal_engine import astro_signal_engine, parse_astro_csv

logger = logging.getLogger("ClientSession")


class ClientSession:
    """
    Encapsulated per-client session container.
    Guarantees strict isolation for trades, runs, rate locks, SSE streams, and workers.
    """

    def __init__(self, client_id: str, state_dir: Optional[str] = None):
        if not client_id or not isinstance(client_id, str) or not client_id.strip():
            raise ValueError("CRITICAL SECURITY GUARD: client_id is required to create ClientSession.")

        self.client_id = client_id.strip()
        self.state_dir = state_dir or "."
        self.created_at = datetime.datetime.now(IST)
        self.last_activity = datetime.datetime.now(IST)
        self._lock = threading.Lock()

        # 0. Load Authoritative Trading Mode from credential_service (default: "paper")
        self.trading_mode: str = credential_service.get_trading_mode(self.client_id)

        # 1. Dedicated In-Memory RunManager (zero local JSON trade files, MongoDB Scalper only)
        def client_trade_callback(trade_data: Dict[str, Any], run_config: Optional[Dict[str, Any]] = None):
            trade_data["mode"] = getattr(self, "trading_mode", "paper") or "paper"
            mongo_service.insert_trade_async(trade_data, client_id=self.client_id, run_config=run_config)
            if hasattr(self, "run_manager") and self.run_manager:
                self.run_manager.master_trade_history.insert(0, trade_data)
                self.run_manager.sync_runs_from_history()
            try:
                slot_id = (run_config or {}).get("run_id") or trade_data.get("run_id") or "N-C"
                self.run_manager.record_slot_event(
                    slot_id=slot_id,
                    event_type="EXIT",
                    signal=trade_data.get("trigger_signal") or trade_data.get("signal") or "EXIT",
                    strike=trade_data.get("strike") or 0,
                    option_type=trade_data.get("option_type") or "CE",
                    price=trade_data.get("exit_price") or 0.0,
                    quantity=trade_data.get("quantity") or 0,
                    pnl_pts=trade_data.get("pnl_points"),
                    pnl_rupees=trade_data.get("pnl_rupees"),
                    details={
                        "reason": str(trade_data.get("exit_reason")),
                        "entry_price": trade_data.get("fill_price"),
                        "trade_id": trade_data.get("trade_id"),
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to record slot exit event: {e}")

        def client_order_callback(
            run_id: str,
            action: str,
            symbol: str,
            token: str,
            quantity: int,
            price: float,
            order_type: str = "MARKET",
            product_type: str = "INTRADAY",
            trading_mode: str = "paper",
        ) -> Dict[str, Any]:
            return self.place_order(
                run_id=run_id,
                symbol=symbol,
                token=token,
                quantity=quantity,
                price=price,
                transaction_type=action,
                order_type=order_type,
                product_type=product_type,
            )

        self.run_manager = RunManager(
            max_runs=4,
            state_file=":memory:",
            client_id=self.client_id,
            trade_callback=client_trade_callback,
            order_callback=client_order_callback,
        )

        # Synchronize trading mode across all initialized runs
        for r in self.run_manager.runs.values():
            r.config.trading_mode = self.trading_mode

        # Load historical trades strictly from MongoDB Scalper database
        if mongo_service.ensure_connection():
            try:
                db_trades = mongo_service.get_recent_trades(client_id=self.client_id, limit=200)
                if db_trades:
                    self.run_manager.master_trade_history = db_trades
                    self.run_manager.sync_runs_from_history()
                    logger.info(f"Loaded {len(db_trades)} trades from MongoDB Scalper for client '{self.client_id}'.")
            except Exception as e:
                logger.warning(f"Note loading MongoDB history for client '{self.client_id}': {e}")

        # 2. In-Memory Decrypted Broker Feed (Angel One)
        self.angel_feed = self._init_broker_feed()
        for r in self.run_manager.runs.values():
            r.contract_resolver = self.angel_feed.resolve_contract

        # 3. Dedicated Per-Client Rate Limit Mutex
        self.angel_api_lock = asyncio.Lock()

        # 4. Dedicated Per-Client SSE Subscribers
        self.sse_subscribers: List[asyncio.Queue] = []

        # 5. Dedicated Per-Client Live Feed Worker
        self.live_feed_running = False
        self.live_feed_task: Optional[asyncio.Task] = None

        # 6. Dedicated Astro Signal Engine state per client
        self.astro_auto_trigger: Dict[str, bool] = {
            "run01": False,
            "run02": False,
            "run03": False,
            "run04": False,
            "all": False,
        }
        self.last_astro_direction: Dict[str, Optional[str]] = {
            "NIFTY": None,
            "SENSEX": None,
        }
        self.last_astro_preview: Optional[Dict[str, Any]] = None
        self._parsed_astro_cache: Optional[Dict[str, Any]] = None
        # Tracks the last confirmed consensus direction ("CE", "PE", or None)
        # Used to detect consensus flips for cross-slot auto-launch
        self.last_astro_consensus: Optional[str] = None
        self.slot_signal_fingerprints: Dict[str, Optional[str]] = {
            "N-C": None,
            "N-P": None,
            "S-C": None,
            "S-P": None,
        }


    def _init_broker_feed(self) -> AngelOneFeed:
        """Initializes in-memory AngelOneFeed from decrypted credentials."""
        creds = credential_service.get_decrypted_broker_credentials(self.client_id, broker_name="angel_one")
        if creds and creds.get("client_id") and creds.get("api_key"):
            return AngelOneFeed(
                client_id=creds["client_id"],
                api_key=creds["api_key"],
                totp_secret=creds["totp_secret"],
                mpin=creds["mpin"],
                owner_client_id=self.client_id,
            )
        elif self.client_id == "admin" and ANGEL_CLIENT_ID:
            return AngelOneFeed(
                client_id=ANGEL_CLIENT_ID,
                api_key=ANGEL_API_KEY,
                totp_secret=ANGEL_TOTP_SECRET,
                mpin=ANGEL_MPIN,
                owner_client_id="admin",
            )
        else:
            return AngelOneFeed(
                client_id="",
                api_key="",
                totp_secret="",
                mpin="",
                owner_client_id=self.client_id,
            )

    def reload_broker_credentials(self) -> None:
        """Re-initializes broker feed in-memory after credential updates."""
        with self._lock:
            self.angel_feed = self._init_broker_feed()
            logger.info(f"Broker feed reloaded for client '{self.client_id}'.")

    def reload_trading_mode(self) -> str:
        """Reloads trading mode from credential_service and synchronizes all run configurations."""
        with self._lock:
            self.trading_mode = credential_service.get_trading_mode(self.client_id)
            for r in self.run_manager.runs.values():
                r.config.trading_mode = self.trading_mode
            logger.info(f"Synchronized trading mode to '{self.trading_mode}' for client '{self.client_id}'.")
            return self.trading_mode

    def touch(self) -> None:
        """Updates last activity timestamp."""
        self.last_activity = datetime.datetime.now(IST)

    async def broadcast_state(self) -> None:
        """Broadcasts state strictly to this client's open browser tabs."""
        self.touch()
        if not self.sse_subscribers:
            return

        active_run = self.run_manager.get_run(self.run_manager.active_run_id) or next(iter(self.run_manager.runs.values()), None)
        active_status = active_run.get_status() if active_run else {}

        instrument = (active_status.get("config", {}).get("instrument_name") if active_status else None) or (
            active_run.config.instrument_name if active_run else "NIFTY"
        )
        default_spot = 81000.0 if instrument == "SENSEX" else 24200.0

        raw_spot = active_status.get("spot_ltp")
        if raw_spot and ((instrument == "SENSEX" and raw_spot >= 50000) or (instrument == "NIFTY" and raw_spot < 50000)):
            spot_val = raw_spot
        else:
            spot_val = self.angel_feed.latest_spot_by_inst.get(instrument) or default_spot

        raw_strike = active_status.get("strike")
        if raw_strike and ((instrument == "SENSEX" and raw_strike >= 50000) or (instrument == "NIFTY" and raw_strike < 50000)):
            active_strike = raw_strike
        else:
            active_strike = self.angel_feed.latest_strike_by_inst.get(instrument) or calculate_nearest_strike(spot_val, instrument)

        last_ltp = active_status.get("last_ltp")
        contract_sym = active_status.get("contract_symbol") or f"{instrument} {active_strike} CE"

        db_trades = mongo_service.get_recent_trades(client_id=self.client_id, limit=200) if mongo_service.ensure_connection() else []
        mem_trades = self.run_manager.get_all_trade_history()
        all_trade_map = {}
        for t in (db_trades + mem_trades):
            if not t:
                continue
            tid = t.get("trade_id") or f"{t.get('run_id')}_{t.get('entry_time')}"
            all_trade_map[tid] = t
        combined_history = sorted(
            all_trade_map.values(),
            key=lambda x: str(x.get("exit_time") or x.get("exited_at") or x.get("entry_time") or x.get("created_at") or ""),
            reverse=True
        )

        payload = {
            "client_id": self.client_id,
            "trading_mode": getattr(self, "trading_mode", "paper"),
            "active_run_id": self.run_manager.active_run_id,
            "runs": self.run_manager.list_runs(),
            "active_run": active_status,
            "history": combined_history[:200],
            "angel_feed": self.angel_feed.get_status() if self.angel_feed else {},
            "broker_credential_status": credential_service.get_credential_status(self.client_id),
            "timestamp": datetime.datetime.now(IST).isoformat(),
            # Cockpit visualizer fields
            "slicer_running": active_run.is_active if active_run else False,
            "instrument": instrument,
            "spot_ltp": spot_val,
            "last_ltp": last_ltp,
            "feed_status": active_status.get("feed_status", "LIVE"),
            "active_strike": active_strike,
            "contract_symbol": contract_sym,
            "ladder_levels": active_status.get("grid_ladder", []),
            "open_slices": active_status.get("active_slices", []),
            "closed_slices": active_status.get("trade_history", []),
            "total_realized_pnl": active_status.get("accumulated_realized_pnl", 0.0),
            "unrealized_pnl": active_status.get("unrealized_pnl_rupees", 0.0),
            "total_pnl": active_status.get("total_pnl", 0.0),
            "active_loss_trigger": active_status.get("loss_trigger_price"),
            "ladder_cycle_id": active_status.get("cycle_count", 1),
            "config": active_status.get("config", {}),
            "recent_audit_logs": active_status.get("audit_events", []),
        }

        active_astro_doc = mongo_service.get_active_astro_file(self.client_id)
        active_preview = getattr(self, "last_astro_preview", {}) or {}
        sig_name = active_preview.get("signal_name") or "NEUTRAL"
        eligible_slots = active_preview.get("eligible_slots") or []

        # 4-Slot Status Matrix & Instrument P&L Rollups (Shared Helper)
        matrix_data = self.run_manager.get_slots_matrix(eligible_slots=eligible_slots)
        slots_matrix = matrix_data["slots_matrix"]
        instrument_pnl = matrix_data["instrument_pnl"]

        payload["slots_matrix"] = slots_matrix
        payload["instrument_pnl"] = instrument_pnl
        payload["active_signal"] = sig_name
        payload["basket_risk_scope"] = getattr(self.run_manager, "basket_risk_scope", "per_instrument")
        payload["slot_events"] = getattr(self.run_manager, "slot_events", [])[-50:]

        payload["astro_state"] = {
            "has_active_file": bool(active_astro_doc),
            "filename": active_astro_doc.get("filename") if active_astro_doc else None,
            "file_id": str(active_astro_doc.get("_id", "")) if active_astro_doc else None,
            "row_count": active_astro_doc.get("row_count", 0) if active_astro_doc else 0,
            "uploaded_at": active_astro_doc.get("uploaded_at") if active_astro_doc else None,
            "auto_trigger_by_slot": dict(self.astro_auto_trigger),
            "last_astro_direction": dict(self.last_astro_direction),
            "cluster_preview": self.last_astro_preview,
            "active_signal": sig_name,
            "eligible_slots": eligible_slots,
            "basket_risk_scope": getattr(self.run_manager, "basket_risk_scope", "per_instrument"),
            "slots_matrix": slots_matrix,
            "instrument_pnl": instrument_pnl,
        }

        message = f"data: {json.dumps(payload)}\n\n"
        dead_queues = []
        for queue in list(self.sse_subscribers):
            if queue.qsize() > 8:
                dead_queues.append(queue)
                continue
            try:
                queue.put_nowait(message)
            except Exception:
                dead_queues.append(queue)

        for dq in dead_queues:
            if dq in self.sse_subscribers:
                self.sse_subscribers.remove(dq)

    async def run_live_feed_cycle(self) -> None:
        """Executes one tick cycle for this client's active runs."""
        loop = asyncio.get_running_loop()

        if not self.angel_feed.is_authenticated and self.angel_feed.client_id and self.angel_feed.api_key:
            try:
                await loop.run_in_executor(None, self.angel_feed.login)
            except Exception as e:
                logger.warning(f"Error logging into Angel One for client '{self.client_id}': {e}")

        # Check EOD cutoff across all slots (active slots or slots with open slices)
        for r in self.run_manager.runs.values():
            if r.is_active or len(r.active_slices) > 0:
                if r.check_eod_cutoff():
                    logger.info(f"[EOD Squareoff] Slot {r.run_id} auto-squared off open trades at {r.config.cutoff_time_ist} IST for client '{self.client_id}'.")

        active_runs = [r for r in self.run_manager.runs.values() if r.is_active]

        async with self.angel_api_lock:
            # 1. Fetch spot market data for underlying indices (NIFTY & SENSEX) first
            spot_tokens: Dict[str, List[str]] = {
                "NSE": ["99926000"],
                "BSE": ["99919000"],
            }
            spot_ltps = await loop.run_in_executor(None, self.angel_feed.fetch_market_data_batch, spot_tokens)
            nifty_spot = spot_ltps.get("99926000") or self.angel_feed.latest_spot_by_inst.get("NIFTY")
            sensex_spot = spot_ltps.get("99919000") or self.angel_feed.latest_spot_by_inst.get("SENSEX")

            # 2. Update spot & evaluate strike locks / restrikes on all runs BEFORE resolving option contracts
            for r in self.run_manager.runs.values():
                r.contract_resolver = self.angel_feed.resolve_contract
                inst = r.config.instrument_name or "NIFTY"
                sp = sensex_spot if inst == "SENSEX" else nifty_spot
                if sp is not None:
                    r.handle_spot_update(sp, contract_resolver=self.angel_feed.resolve_contract)

            # 3. Now resolve exact option contracts & tokens for all slots based on the fresh strikes
            exchange_tokens: Dict[str, List[str]] = {}
            slot_contract_map: Dict[str, Dict[str, Any]] = {}

            active_runs = [r for r in self.run_manager.runs.values() if r.is_active]

            # 3a. Include contracts for active runs
            for run in active_runs:
                inst = run.config.instrument_name or "NIFTY"
                contract_strike = getattr(run, "locked_strike", None) or run.config.strike
                contract = self.angel_feed.resolve_contract(contract_strike, run.config.option_type, inst)
                if contract:
                    run.config.contract_symbol = contract["symbol"]
                    run.config.contract_token = contract["token"]
                    run.locked_contract_symbol = contract["symbol"]
                    run.locked_contract_token = str(contract["token"]) if contract.get("token") else None
                    exch = contract.get("exchange") or ("BFO" if ("SENSEX" in contract["symbol"] or "BSX" in contract["symbol"]) else "NFO")
                    tok = str(contract["token"])
                    if tok not in exchange_tokens.setdefault(exch, []):
                        exchange_tokens[exch].append(tok)
                    slot_contract_map[run.run_id] = contract
                    c_id = getattr(run, "slot_id", None)
                    if c_id:
                        slot_contract_map[c_id] = contract

            # 3b. Resolve option contracts for ALL 4 Fixed Slots (N-C, N-P, S-C, S-P)
            canonical_slot_defs = [
                ("N-C", "NIFTY", "CE"),
                ("N-P", "NIFTY", "PE"),
                ("S-C", "SENSEX", "CE"),
                ("S-P", "SENSEX", "PE"),
            ]
            for slot_code, inst, opt_type in canonical_slot_defs:
                slot_run = self.run_manager.get_run(slot_code)
                if not slot_run:
                    continue
                if slot_run.is_active or len(slot_run.active_slices) > 0:
                    continue  # Already batched in active_runs above

                spot = slot_run.spot_ltp or (sensex_spot if inst == "SENSEX" else nifty_spot)
                strike_src = getattr(slot_run.config, "strike_source", "auto")
                if strike_src == "manual" and slot_run.config.strike:
                    strike = slot_run.config.strike
                else:
                    strike = calculate_nearest_strike(spot, inst) if spot else (slot_run.config.strike or (81000 if inst == "SENSEX" else 24200))

                contract = self.angel_feed.resolve_contract(strike, opt_type, inst)
                if contract:
                    exch = contract.get("exchange") or ("BFO" if ("SENSEX" in contract["symbol"] or "BSX" in contract["symbol"]) else "NFO")
                    tok = str(contract["token"])
                    if tok not in exchange_tokens.setdefault(exch, []):
                        exchange_tokens[exch].append(tok)
                    slot_contract_map[slot_code] = contract
                    legacy_id = getattr(slot_run, "legacy_run_id", None)
                    if legacy_id:
                        slot_contract_map[legacy_id] = contract

            # 4. Batch fetch option LTPs for the resolved tokens
            batch_ltps: Dict[str, float] = {}
            if exchange_tokens:
                batch_ltps = await loop.run_in_executor(None, self.angel_feed.fetch_market_data_batch, exchange_tokens)
            if nifty_spot is not None:
                batch_ltps["99926000"] = nifty_spot
            if sensex_spot is not None:
                batch_ltps["99919000"] = sensex_spot

            # 5. Update live option LTP across all runs (both active trading runs and idle fixed slots)
            for r_id, r in list(self.run_manager.runs.items()):
                contract = (
                    slot_contract_map.get(r_id)
                    or slot_contract_map.get(getattr(r, "slot_id", ""))
                    or slot_contract_map.get(getattr(r, "legacy_run_id", ""))
                )
                if contract:
                    tok = str(contract["token"])
                    opt_ltp = batch_ltps.get(tok)
                    if opt_ltp is not None and opt_ltp > 0:
                        r.last_ltp = opt_ltp
                        r.config.contract_symbol = contract["symbol"]
                        r.config.contract_token = contract["token"]
                        r.locked_contract_symbol = contract["symbol"]
                        r.locked_contract_token = str(contract["token"]) if contract.get("token") else None
                        r.update_feed_health(True)
                        if r.is_active:
                            r.process_tick(opt_ltp)
                    else:
                        if r.last_ltp and r.last_ltp > 0:
                            r.update_feed_health(True)
                        else:
                            r.update_feed_health(False)
            self.run_manager.save_state()

            # ── 4-Slot Fixed Execution Layer (N-C, N-P, S-C, S-P) ───────────────────
            now_ist = datetime.datetime.now(IST)
            await self.evaluate_fixed_slots(
                current_dt=now_ist,
                batch_ltps=batch_ltps,
                nifty_spot=nifty_spot,
                sensex_spot=sensex_spot,
            )

        await self.broadcast_state()

    async def evaluate_fixed_slots(
        self,
        current_dt: Optional[datetime.datetime] = None,
        batch_ltps: Optional[Dict[str, float]] = None,
        nifty_spot: Optional[float] = None,
        sensex_spot: Optional[float] = None,
        active_astro_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Consumes directional signal from astro_signal_engine and executes
        into designated fixed slots (N-C, N-P, S-C, S-P).
        Returns execution evaluation summary.
        """
        if batch_ltps is None:
            batch_ltps = {}
        now_ist = current_dt or datetime.datetime.now(IST)

        content = active_astro_content
        if not content:
            active_file = mongo_service.get_active_astro_file(self.client_id)
            if active_file:
                content = active_file.get("content")

        if not content:
            return {"signal": "NEUTRAL", "status": "NO_FILE", "triggered_slots": []}

        try:
            if not self._parsed_astro_cache or self._parsed_astro_cache.get("content") != content:
                df_parsed, _ = parse_astro_csv(content)
                self._parsed_astro_cache = {"content": content, "df": df_parsed}
            else:
                df_parsed = self._parsed_astro_cache["df"]
        except Exception as e:
            logger.warning(f"Error parsing astro CSV: {e}")
            return {"signal": "NEUTRAL", "status": "PARSE_ERROR", "error": str(e), "triggered_slots": []}

        try:
            # 1. Consume existing 3-condition directional signal engine
            decision = astro_signal_engine.evaluate_cluster(
                df=df_parsed,
                current_dt=now_ist,
                has_active_trade=False,
                instrument="NIFTY",
            )

            signal_name = "NEUTRAL"
            if decision.get("status") == "APPROVED":
                if decision.get("option_type") == "CE":
                    signal_name = "UPSIDE"
                elif decision.get("option_type") == "PE":
                    signal_name = "DOWNSIDE"
            elif decision.get("filter") == "trading_hours":
                signal_name = "NEUTRAL"

            signal_fingerprint = decision.get("fingerprint")

            # Map signal to designated fixed slots
            if signal_name == "UPSIDE":
                target_slots = [
                    ("N-C", "NIFTY", "CE"),
                    ("S-C", "SENSEX", "CE"),
                ]
            elif signal_name == "DOWNSIDE":
                target_slots = [
                    ("N-P", "NIFTY", "PE"),
                    ("S-P", "SENSEX", "PE"),
                ]
            else:
                target_slots = []

            # Calculate which slots are currently eligible to fire
            eligible_slot_ids = []
            for s_id, _, _ in target_slots:
                s_run = self.run_manager.get_run(s_id)
                last_fp = getattr(s_run, "last_signal_fingerprint", None) or self.slot_signal_fingerprints.get(s_id)
                is_same_signal = bool(signal_fingerprint and last_fp == signal_fingerprint)
                if s_run and not s_run.is_active and len(s_run.active_slices) == 0 and not is_same_signal:
                    eligible_slot_ids.append(s_id)

            decision["signal_name"] = signal_name
            decision["eligible_slots"] = eligible_slot_ids
            decision["fingerprint"] = signal_fingerprint
            decision["slot_signal_fingerprints"] = dict(self.slot_signal_fingerprints)
            decision["basket_risk_scope"] = getattr(self.run_manager, "basket_risk_scope", "per_instrument")
            self.last_astro_preview = decision

            triggered_slots = []
            skipped_slots = []
            loop = asyncio.get_event_loop()

            for slot_id, inst, target_opt_type in target_slots:
                target_run = self.run_manager.get_run(slot_id)
                if not target_run:
                    continue

                # SLOT REUSE / RE-ENTRY RULE:
                # If slot already holds an open position, do NOT take new entry — must exit first
                if target_run.is_active or len(target_run.active_slices) > 0:
                    continue

                # SIGNAL OCCURRENCE DEDUP RULE:
                # If this slot was already opened on this specific signal occurrence (same cluster fingerprint),
                # do NOT auto-refire into this slot while the same signal persists, even if the slot is now closed/flat.
                last_fp = getattr(target_run, "last_signal_fingerprint", None) or self.slot_signal_fingerprints.get(slot_id)
                if signal_fingerprint and last_fp == signal_fingerprint:
                    continue

                is_armed = bool(
                    self.astro_auto_trigger.get(slot_id, False)
                    or self.astro_auto_trigger.get(getattr(target_run, "legacy_run_id", ""), False)
                    or self.astro_auto_trigger.get("all", False)
                )
                if not is_armed:
                    continue

                sp = sensex_spot if inst == "SENSEX" else nifty_spot
                if sp is None or sp <= 0:
                    sp = target_run.spot_ltp or (81000.0 if inst == "SENSEX" else 24200.0)

                strike_src = getattr(target_run.config, "strike_source", "auto")
                if strike_src == "manual" and target_run.config.strike:
                    atm_strike = target_run.config.strike
                else:
                    atm_strike = calculate_nearest_strike(sp, inst)

                opt_contract = self.angel_feed.resolve_contract(atm_strike, target_opt_type, inst)
                opt_ltp = None
                if opt_contract and opt_contract.get("token"):
                    tok = str(opt_contract["token"])
                    sym = opt_contract.get("symbol", "")
                    exch = opt_contract.get("exch_seg") or ("BFO" if ("SENSEX" in sym or "BSX" in sym) else "NFO")
                    opt_ltp = batch_ltps.get(tok)
                    if opt_ltp is None or opt_ltp <= 0:
                        try:
                            opt_ltp = await loop.run_in_executor(
                                None,
                                lambda: self.angel_feed.fetch_option_ltp(sym, tok, exch, max_cache_age=1.5),
                            )
                        except Exception as e:
                            logger.warning(f"[Fixed Slot Engine] Direct live LTP fetch failed for {sym}: {e}")

                if (opt_ltp is None or opt_ltp <= 0) and target_run.last_ltp and target_run.last_ltp > 0:
                    # Accept target_run.last_ltp ONLY if it strictly matches the current ATM strike & contract
                    if (
                        target_run.config.strike == atm_strike
                        and (target_run.config.option_type or "").upper() == target_opt_type.upper()
                        and (target_run.config.instrument_name or "").upper() == inst
                    ):
                        opt_ltp = target_run.last_ltp

                # In live market environment (feed is authenticated), NEVER use default/stale arbitrary prices!
                # Only in offline unit-test environments without broker connection do we allow mock test values.
                is_feed_live = bool(getattr(self.angel_feed, "is_authenticated", False))
                if (opt_ltp is None or opt_ltp <= 0) and not is_feed_live:
                    opt_ltp = target_run.last_ltp or (200.0 if inst == "SENSEX" else 120.0)

                MIN_LAUNCH_LTP = 15.0
                contract_label = opt_contract.get("symbol") if opt_contract else f"{inst} {atm_strike} {target_opt_type}"
                if opt_ltp is None or opt_ltp <= 0:
                    logger.warning(
                        f"[Fixed Slot Engine] [LTP_MISSING] Postponing launch for Slot {slot_id} ({contract_label}): "
                        f"live option premium LTP not yet received from Angel One. Waiting for next live tick..."
                    )
                    skipped_slots.append({
                        "slot_id": slot_id,
                        "contract": contract_label,
                        "reason": "LTP_MISSING",
                        "detail": f"Live option premium LTP not yet received from Angel One for {contract_label}.",
                    })
                    continue
                elif opt_ltp < MIN_LAUNCH_LTP:
                    logger.warning(
                        f"[Fixed Slot Engine] [LTP_BELOW_MIN] BLOCKED launch for Slot {slot_id} ({contract_label}): "
                        f"LTP ₹{opt_ltp:.2f} below minimum safety threshold ₹{MIN_LAUNCH_LTP:.2f}."
                    )
                    skipped_slots.append({
                        "slot_id": slot_id,
                        "contract": contract_label,
                        "reason": "LTP_BELOW_MIN",
                        "detail": f"LTP ₹{opt_ltp:.2f} below minimum safety threshold ₹{MIN_LAUNCH_LTP:.2f}.",
                    })
                    continue

                is_sensex = (inst == "SENSEX")
                default_r_pts = 150.0 if is_sensex else 40.0
                default_profit = 30.0 if is_sensex else 8.0
                default_loss = 30.0 if is_sensex else 8.0

                r_pts = getattr(target_run.config, "range_points", None) or default_r_pts
                profit_val = target_run.config.profit_point or default_profit
                loss_val = target_run.config.loss_point or default_loss
                s_count = getattr(target_run.config, "slicer_count", 5) or 5
                step_val = round(r_pts / s_count, 2)
                # Ladder starts at the current LTP as the top rung (first step), with the range stepping down below it
                high_val = round(opt_ltp, 2)
                low_val = round(high_val - r_pts, 2)

                lot_sz = 20 if is_sensex else 65
                qty = (getattr(target_run.config, "qty_per_slice_lots", 1) or 1) * lot_sz

                slot_cfg = ScalperRunConfig(
                    run_id=slot_id,
                    instrument_name=inst,
                    option_type=target_opt_type,
                    strike=atm_strike,
                    range_points=r_pts,
                    slicer_count=s_count,
                    range_high=high_val,
                    range_low=low_val,
                    slice_interval=step_val,
                    profit_point=profit_val,
                    loss_point=loss_val,
                    qty_per_slice_lots=getattr(target_run.config, "qty_per_slice_lots", 1) or 1,
                    contract_symbol=opt_contract.get("symbol") if opt_contract else f"{inst} {atm_strike} {target_opt_type}",
                    contract_token=str(opt_contract["token"]) if (opt_contract and opt_contract.get("token")) else None,
                    auto_eod_squareoff=True,
                    cutoff_time_ist="15:22",
                    trading_mode=self.trading_mode,
                )

                started_run = self.run_manager.start_run(slot_cfg)
                started_run.last_signal_fingerprint = signal_fingerprint
                self.slot_signal_fingerprints[slot_id] = signal_fingerprint
                started_run.last_ltp = opt_ltp
                started_run.spot_ltp = sp

                self.run_manager.record_slot_event(
                    slot_id=slot_id,
                    event_type="ENTRY",
                    signal=signal_name,
                    strike=atm_strike,
                    option_type=target_opt_type,
                    price=opt_ltp,
                    quantity=qty,
                    details={
                        "contract": opt_contract.get("symbol") if opt_contract else f"{inst} {atm_strike} {target_opt_type}",
                        "spot": sp,
                        "fingerprint": signal_fingerprint,
                    },
                )
                triggered_slots.append(slot_id)
                logger.info(
                    f"[Fixed Slot Engine] Fired entry into slot {slot_id} ({inst} {atm_strike} {target_opt_type}) "
                    f"@ LTP ₹{opt_ltp:.2f} on {signal_name} signal (Fingerprint: {signal_fingerprint})."
                )
                if opt_ltp and opt_ltp > 0:
                    started_run.process_tick(opt_ltp)

            # 3. Evaluate basket stop loss risk across active slots
            self.run_manager.check_basket_risk()

            decision["skipped_slots"] = skipped_slots
            return {
                "signal": signal_name,
                "target_slots": [s[0] for s in target_slots],
                "eligible_slots": eligible_slot_ids,
                "triggered_slots": triggered_slots,
                "skipped_slots": skipped_slots,
                "fingerprint": signal_fingerprint,
            }

        except Exception as e:
            logger.warning(f"Error evaluating fixed slots auto-trigger: {e}")
            return {"signal": "ERROR", "error": str(e), "triggered_slots": []}

    async def _live_feed_loop(self) -> None:
        """Background continuous loop for this client."""
        logger.info(f"Live feed task started for client '{self.client_id}'.")
        while self.live_feed_running:
            has_active_runs = any(r.is_active for r in self.run_manager.runs.values())
            has_subscribers = len(self.sse_subscribers) > 0
            is_any_armed = any(bool(v) for v in self.astro_auto_trigger.values())

            # If no ladder is running, nobody is currently viewing the cockpit in their browser,
            # and auto-trigger is not armed, pause and sleep to preserve Angel One API rate limit quota.
            if not has_active_runs and not has_subscribers and not is_any_armed:
                await asyncio.sleep(5.0)
                continue

            try:
                await self.run_live_feed_cycle()
            except Exception as e:
                logger.error(f"Error in client '{self.client_id}' live feed cycle: {e}")

            sleep_interval = 2.0 if has_active_runs else 3.5
            await asyncio.sleep(sleep_interval)

    def start_live_feed(self) -> None:
        """Starts background streaming task for this client if not already running."""
        with self._lock:
            if not self.live_feed_running:
                self.live_feed_running = True
                self.live_feed_task = asyncio.create_task(self._live_feed_loop())

    def stop_live_feed(self) -> None:
        """Stops background streaming task for this client."""
        with self._lock:
            self.live_feed_running = False
            if self.live_feed_task and not self.live_feed_task.done():
                self.live_feed_task.cancel()
                self.live_feed_task = None
            logger.info(f"Live feed task stopped for client '{self.client_id}'.")

    def place_order(
        self,
        run_id: str,
        symbol: str,
        token: str,
        quantity: int,
        price: float,
        transaction_type: str = "BUY",
        order_type: str = "LIMIT",
        product_type: str = "INTRADAY",
    ) -> Dict[str, Any]:
        """
        Secure order execution entrypoint for this client session.
        Executes order strictly via self.angel_feed with cross-client safety validation.
        """
        run = self.run_manager.get_run(run_id)
        run_owner = getattr(run, "client_id", self.client_id) if run else self.client_id

        clean_symbol = symbol
        clean_token = token
        if (not clean_token or " " in str(clean_symbol)) and run:
            inst = getattr(run.config, "instrument_name", "NIFTY")
            opt_type = getattr(run.config, "option_type", "CE")
            strike = getattr(run, "locked_strike", None) or getattr(run.config, "strike", None)
            if strike:
                contract = self.angel_feed.resolve_contract(int(strike), opt_type, inst)
                if contract:
                    clean_symbol = contract["symbol"]
                    clean_token = str(contract["token"])
                    run.config.contract_symbol = clean_symbol
                    run.config.contract_token = clean_token
                    run.locked_contract_symbol = clean_symbol
                    run.locked_contract_token = clean_token

        return self.angel_feed.place_order(
            client_id=self.client_id,
            run_id=run_id,
            run_owner_client_id=run_owner,
            symbol=clean_symbol,
            token=clean_token,
            quantity=quantity,
            price=price,
            transaction_type=transaction_type,
            order_type=order_type,
            product_type=product_type,
            trading_mode=getattr(self, "trading_mode", "paper"),
        )

    def close(self) -> None:
        """Tears down the session and flushes state."""
        self.stop_live_feed()
        self.run_manager.save_state()
        self.sse_subscribers.clear()
        logger.info(f"ClientSession closed for '{self.client_id}'.")
