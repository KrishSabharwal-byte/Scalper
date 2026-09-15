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
                    logger.info(f"Loaded {len(db_trades)} trades from MongoDB Scalper for client '{self.client_id}'.")
            except Exception as e:
                logger.warning(f"Note loading MongoDB history for client '{self.client_id}': {e}")

        # 2. In-Memory Decrypted Broker Feed (Angel One)
        self.angel_feed = self._init_broker_feed()

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
        payload["astro_state"] = {
            "has_active_file": bool(active_astro_doc),
            "filename": active_astro_doc.get("filename") if active_astro_doc else None,
            "file_id": str(active_astro_doc.get("_id", "")) if active_astro_doc else None,
            "row_count": active_astro_doc.get("row_count", 0) if active_astro_doc else 0,
            "uploaded_at": active_astro_doc.get("uploaded_at") if active_astro_doc else None,
            "auto_trigger_by_slot": dict(self.astro_auto_trigger),
            "last_astro_direction": dict(self.last_astro_direction),
            "cluster_preview": self.last_astro_preview,
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
            # Build unified batch token request for all active slots and spot indices
            exchange_tokens: Dict[str, List[str]] = {
                "NSE": ["99926000"],
                "BSE": ["99919000"],
            }
            slot_contract_map: Dict[str, Dict[str, Any]] = {}

            if active_runs:
                for run in active_runs:
                    inst = run.config.instrument_name or "NIFTY"
                    contract_strike = getattr(run, "locked_strike", None) or run.config.strike
                    contract = self.angel_feed.resolve_contract(contract_strike, run.config.option_type, inst)
                    if contract:
                        run.config.contract_symbol = contract["symbol"]
                        run.config.contract_token = contract["token"]
                        run.locked_contract_symbol = contract["symbol"]
                        run.locked_contract_token = contract["token"]
                        exch = contract.get("exchange") or ("BFO" if ("SENSEX" in contract["symbol"] or "BSX" in contract["symbol"]) else "NFO")
                        tok = str(contract["token"])
                        exchange_tokens.setdefault(exch, []).append(tok)
                        slot_contract_map[run.run_id] = contract
            else:
                active_run = self.run_manager.get_run(self.run_manager.active_run_id) or next(iter(self.run_manager.runs.values()), None)
                if active_run:
                    inst = active_run.config.instrument_name or "NIFTY"
                    opt_type = active_run.config.option_type
                    spot = active_run.spot_ltp or self.angel_feed.latest_spot_by_inst.get(inst)
                    strike_src = getattr(active_run.config, "strike_source", "auto")
                    if strike_src == "manual" and active_run.config.strike:
                        strike = active_run.config.strike
                    else:
                        strike = calculate_nearest_strike(spot, inst) if spot else (active_run.config.strike or (81000 if inst == "SENSEX" else 24200))
                    contract = self.angel_feed.resolve_contract(strike, opt_type, inst)
                    if contract:
                        self.angel_feed.latest_option_contract = contract
                        exch = contract.get("exchange") or ("BFO" if ("SENSEX" in contract["symbol"] or "BSX" in contract["symbol"]) else "NFO")
                        tok = str(contract["token"])
                        exchange_tokens.setdefault(exch, []).append(tok)
                        slot_contract_map[active_run.run_id] = contract

            # Single unified batch fetch for both spots and all options!
            batch_ltps = await loop.run_in_executor(None, self.angel_feed.fetch_market_data_batch, exchange_tokens)

            # Update spot on all runs
            nifty_spot = batch_ltps.get("99926000") or self.angel_feed.latest_spot_by_inst.get("NIFTY")
            sensex_spot = batch_ltps.get("99919000") or self.angel_feed.latest_spot_by_inst.get("SENSEX")

            for r in self.run_manager.runs.values():
                inst = r.config.instrument_name or "NIFTY"
                sp = sensex_spot if inst == "SENSEX" else nifty_spot
                if sp is not None:
                    r.handle_spot_update(sp)

            # Update option LTP for active runs or idle run
            if active_runs:
                for run in active_runs:
                    contract = slot_contract_map.get(run.run_id)
                    if contract:
                        tok = str(contract["token"])
                        opt_ltp = batch_ltps.get(tok)
                        if opt_ltp is not None and opt_ltp > 0:
                            run.update_feed_health(True)
                            run.process_tick(opt_ltp)
                        else:
                            if run.last_ltp and run.last_ltp > 0:
                                run.update_feed_health(True)
                            else:
                                run.update_feed_health(False)
                    else:
                        run.update_feed_health(False)
                self.run_manager.save_state()
            else:
                active_run = self.run_manager.get_run(self.run_manager.active_run_id) or next(iter(self.run_manager.runs.values()), None)
                if active_run:
                    contract = slot_contract_map.get(active_run.run_id)
                    if contract:
                        tok = str(contract["token"])
                        opt_ltp = batch_ltps.get(tok)
                        if opt_ltp is not None and opt_ltp > 0:
                            active_run.last_ltp = opt_ltp
                            active_run.config.contract_symbol = contract["symbol"]
                            active_run.config.contract_token = contract["token"]
                            inst = active_run.config.instrument_name or "NIFTY"
                            spot = active_run.spot_ltp or (sensex_spot if inst == "SENSEX" else nifty_spot)
                            strike_src = getattr(active_run.config, "strike_source", "auto")
                            if strike_src != "manual" and spot:
                                old_strike = active_run.config.strike
                                new_strike = calculate_nearest_strike(spot, inst)
                                if new_strike != old_strike:
                                    active_run.config.strike = new_strike
                                    # Invalidate cached last_ltp because it belongs to the previous strike
                                    active_run.last_ltp = None

            # Check Astro preview & Auto-Trigger strictly for current active slot
            active_file = mongo_service.get_active_astro_file(self.client_id)
            if active_file and active_file.get("content"):
                try:
                    file_id_str = str(active_file.get("_id"))
                    if not self._parsed_astro_cache or self._parsed_astro_cache.get("file_id") != file_id_str:
                        df_parsed, _ = parse_astro_csv(active_file["content"])
                        self._parsed_astro_cache = {"file_id": file_id_str, "df": df_parsed}
                    else:
                        df_parsed = self._parsed_astro_cache["df"]

                    now_ist = datetime.datetime.now(IST)

                    # Target ONLY the slot where the user currently is (active_run_id / current slot tab)
                    active_slot_id = self.run_manager.active_run_id or "run01"
                    selected_run = self.run_manager.get_run(active_slot_id) or next(iter(self.run_manager.runs.values()), None)

                    if selected_run:
                        inst = (selected_run.config.instrument_name or selected_run.instrument_name or "NIFTY").upper()
                        last_dir = self.last_astro_direction.get(inst)
                        cutoff_t = getattr(selected_run.config, "cutoff_time_ist", "15:22") or "15:22"

                        has_active = bool(selected_run.is_active or len(selected_run.active_slices) > 0)
                        decision = astro_signal_engine.evaluate_cluster(
                            df=df_parsed,
                            current_dt=now_ist,
                            last_trade_direction=last_dir,
                            has_active_trade=has_active,
                            cutoff_time_ist=cutoff_t,
                            instrument=inst,
                        )
                        self.last_astro_preview = decision

                        is_auto_armed = bool(
                            self.astro_auto_trigger.get(active_slot_id, False)
                            or self.astro_auto_trigger.get("all", False)
                        )

                        if decision.get("status") == "APPROVED":
                            logger.info(
                                f"[Astro Auto-Trigger Check] client='{self.client_id}' slot='{active_slot_id}' is_auto_armed={is_auto_armed} decision='APPROVED' is_active={selected_run.is_active} slices={len(selected_run.active_slices)}"
                            )

                        if is_auto_armed and decision.get("status") == "APPROVED":
                            target_opt_type = decision["option_type"]  # "CE" or "PE"

                            # Only start if the current active slot is idle (no active ladder & no open slices)
                            if not selected_run.is_active and len(selected_run.active_slices) == 0:
                                run_id = selected_run.run_id
                                sp = sensex_spot if inst == "SENSEX" else nifty_spot
                                if sp is None or sp <= 0:
                                    sp = selected_run.spot_ltp or (81000.0 if inst == "SENSEX" else 24200.0)
                                atm_strike = calculate_nearest_strike(sp, inst)

                                opt_contract = self.angel_feed.resolve_contract(atm_strike, target_opt_type, inst)
                                opt_ltp = None
                                if opt_contract and opt_contract.get("token"):
                                    tok = str(opt_contract["token"])

                                    # --- Primary: check the already-fetched batch (covers the active-slot contract) ---
                                    opt_ltp = batch_ltps.get(tok)

                                    # --- Secondary: batch miss — the ATM contract token wasn't in the pre-batch.
                                    #     Force a FRESH direct API call (max_cache_age=0) so we NEVER reuse a
                                    #     stale cached price that belongs to a different (e.g. deep-OTM) strike.
                                    if opt_ltp is None or opt_ltp <= 0:
                                        sym = opt_contract.get("symbol", "")
                                        exch = opt_contract.get("exch_seg") or ("BFO" if ("SENSEX" in sym or "BSX" in sym) else "NFO")
                                        logger.info(
                                            f"[Astro Auto-Trigger] ATM token {tok} ({sym}) not in batch — "
                                            f"forcing direct LTP fetch (max_cache_age=0) to avoid stale OTM price contamination."
                                        )
                                        try:
                                            # max_cache_age=0 bypasses the per-symbol cache entirely, ensuring
                                            # we get the real market price for THIS specific ATM contract
                                            opt_ltp = await loop.run_in_executor(
                                                None,
                                                lambda: self.angel_feed.fetch_option_ltp(sym, tok, exch, max_cache_age=0),
                                            )
                                            if opt_ltp and opt_ltp > 0:
                                                logger.info(
                                                    f"[Astro Auto-Trigger] Verified live LTP for {inst} {atm_strike} "
                                                    f"{target_opt_type} ({sym}): ₹{opt_ltp}."
                                                )
                                            else:
                                                logger.warning(
                                                    f"[Astro Auto-Trigger] Direct fetch returned no usable LTP for "
                                                    f"{sym} ({tok}). Will postpone launch."
                                                )
                                                opt_ltp = None
                                        except Exception as e:
                                            logger.warning(f"[Astro Auto-Trigger] Failed to fetch direct LTP for {sym} ({tok}): {e}")
                                            opt_ltp = None

                                # --- Tertiary: strictly-guarded last_ltp fallback.
                                #     Only reuse last_ltp if it provably belongs to the SAME strike, option_type,
                                #     and instrument that we are about to launch.  A mismatch (e.g. last_ltp from
                                #     24300 CE being reused for 23300 CE) is silently discarded.
                                if (opt_ltp is None or opt_ltp <= 0) and selected_run:
                                    if (
                                        selected_run.config.strike == atm_strike
                                        and (selected_run.config.option_type or "").upper() == target_opt_type.upper()
                                        and (selected_run.config.instrument_name or "").upper() == inst
                                        and selected_run.last_ltp
                                        and selected_run.last_ltp > 0
                                    ):
                                        opt_ltp = selected_run.last_ltp
                                        logger.info(
                                            f"[Astro Auto-Trigger] Using verified last_ltp ₹{opt_ltp} for "
                                            f"{inst} {atm_strike} {target_opt_type} (strike/type/inst match confirmed)."
                                        )
                                    else:
                                        logger.warning(
                                            f"[Astro Auto-Trigger] Rejected last_ltp ₹{selected_run.last_ltp} — "
                                            f"belongs to slot config strike={selected_run.config.strike} "
                                            f"{selected_run.config.option_type} {selected_run.config.instrument_name}, "
                                            f"not the ATM contract {inst} {atm_strike} {target_opt_type}. Discarded."
                                        )

                                # --- Minimum LTP guard: block launch if the option is too cheap (deep OTM / illiquid).
                                #     An LTP below ₹25 means the contract has very little intrinsic value;
                                #     running a scalper grid on it risks phantom fills and runaway losses.
                                MIN_LAUNCH_LTP = 25.0
                                if opt_ltp is None or opt_ltp <= 0:
                                    logger.warning(
                                        f"[Astro Auto-Trigger] Postponing launch: unable to obtain verified market LTP for "
                                        f"{inst} {atm_strike} {target_opt_type} ({opt_contract.get('symbol') if opt_contract else 'No Contract'}). "
                                        f"Will retry on next tick cycle."
                                    )
                                elif opt_ltp < MIN_LAUNCH_LTP:
                                    logger.warning(
                                        f"[Astro Auto-Trigger] BLOCKED launch: LTP ₹{opt_ltp:.2f} is below minimum ₹{MIN_LAUNCH_LTP} "
                                        f"for {inst} {atm_strike} {target_opt_type}. "
                                        f"Option is too cheap/illiquid to safely run a scalper grid. Will skip this signal."
                                    )
                                else:
                                    is_sensex = (inst == "SENSEX")
                                    default_r_pts = 150.0 if is_sensex else 40.0
                                    default_profit = 30.0 if is_sensex else 8.0
                                    default_loss = 30.0 if is_sensex else 8.0

                                    if selected_run.config.instrument_name == inst:
                                        r_pts = getattr(selected_run.config, "range_points", None) or default_r_pts
                                        profit_val = selected_run.config.profit_point or default_profit
                                        loss_val = selected_run.config.loss_point or default_loss
                                    else:
                                        r_pts = default_r_pts
                                        profit_val = default_profit
                                        loss_val = default_loss

                                    s_cnt = getattr(selected_run.config, "slicer_count", None) or 5
                                    r_high = round(opt_ltp, 2)
                                    # Clamp r_low so no grid level is ever <= 0.
                                    # An option price cannot be negative, so any ladder level below 0 is
                                    # meaningless and can cause calculation errors downstream.
                                    r_low = max(round(r_high - r_pts, 2), 0.05)
                                    s_step = round(r_pts / s_cnt, 4)

                                    new_cfg = ScalperRunConfig(
                                        run_id=run_id,
                                        instrument_name=inst,
                                        option_type=target_opt_type,
                                        strike=atm_strike,
                                        range_points=r_pts,
                                        slicer_count=s_cnt,
                                        range_high=r_high,
                                        range_low=r_low,
                                        slice_interval=s_step,
                                        profit_point=profit_val,
                                        loss_point=loss_val,
                                        qty_per_slice_lots=selected_run.config.qty_per_slice_lots or 1,
                                        spot_ltp=sp,
                                        contract_symbol=f"{inst} {atm_strike} {target_opt_type}",
                                        contract_token=str(opt_contract.get("token")) if opt_contract else None,
                                        cutoff_time_ist=selected_run.config.cutoff_time_ist or "15:22",
                                        auto_eod_squareoff=selected_run.config.auto_eod_squareoff,
                                        trading_mode=self.trading_mode,
                                    )
                                    started_run = self.run_manager.start_run(new_cfg)
                                    self.last_astro_direction[inst] = target_opt_type
                                    logger.info(
                                        f"[Astro Auto-Trigger] Current Slot {run_id} launched {target_opt_type} trade on {inst} {atm_strike} @ LTP {opt_ltp}."
                                    )
                                    if opt_ltp and opt_ltp > 0:
                                        started_run.process_tick(opt_ltp)

                        # ── Astro Consensus Flip → Auto Slot Switch & Launch ──────────────────
                        # Determine the cluster's current consensus direction (CE / PE / None).
                        # We recognise a direction even when the signal is BLOCKED (e.g. an open
                        # trade on the current slot) so the flip is caught as soon as the cluster
                        # agrees on the new direction, regardless of the active-trade filter.
                        current_consensus: Optional[str] = None
                        if decision.get("option_type") in ("CE", "PE"):
                            current_consensus = decision["option_type"]

                        prev_consensus = self.last_astro_consensus

                        # Update stored consensus
                        if current_consensus is not None:
                            self.last_astro_consensus = current_consensus
                        elif decision.get("status") == "NO_SIGNAL" and decision.get("reason") in (
                            "cluster_mixed_or_neutral",
                            "empty_dataframe",
                            "before_first_report_row",
                            "insufficient_rows_remaining",
                        ):
                            # Hard reset: cluster is genuinely ambiguous → wipe memory so a
                            # future clean signal is treated as fresh, not as a spurious flip.
                            self.last_astro_consensus = None

                        flip_detected = (
                            prev_consensus is not None
                            and current_consensus is not None
                            and prev_consensus != current_consensus
                        )

                        if flip_detected:
                            # Slot layout (RunManager canonical defaults):
                            #   run01 = NIFTY  CE,  run02 = SENSEX CE
                            #   run03 = NIFTY  PE,  run04 = SENSEX PE
                            SLOT_DEFAULTS = {
                                "run01": ("NIFTY", "CE"),
                                "run02": ("SENSEX", "CE"),
                                "run03": ("NIFTY", "PE"),
                                "run04": ("SENSEX", "PE"),
                            }
                            target_slot_id = None
                            for slot_id, (slot_inst, slot_opt) in SLOT_DEFAULTS.items():
                                if slot_inst == inst and slot_opt == current_consensus:
                                    candidate = self.run_manager.get_run(slot_id)
                                    # Only switch to an idle slot — never disturb a live trade
                                    if candidate and not candidate.is_active and len(candidate.active_slices) == 0:
                                        target_slot_id = slot_id
                                        break

                            if target_slot_id:
                                logger.info(
                                    f"[Astro Consensus Flip] {prev_consensus} → {current_consensus} "
                                    f"detected on {inst}. Switching active slot: "
                                    f"{active_slot_id} → {target_slot_id}."
                                )
                                self.run_manager.active_run_id = target_slot_id

                                # Auto-launch the new slot if auto-trigger is armed for it
                                flip_auto_armed = bool(
                                    self.astro_auto_trigger.get(target_slot_id, False)
                                    or self.astro_auto_trigger.get("all", False)
                                )
                                flip_run = self.run_manager.get_run(target_slot_id)

                                if (
                                    flip_auto_armed
                                    and flip_run
                                    and not flip_run.is_active
                                    and len(flip_run.active_slices) == 0
                                ):
                                    sp_flip = sensex_spot if inst == "SENSEX" else nifty_spot
                                    if sp_flip is None or sp_flip <= 0:
                                        sp_flip = flip_run.spot_ltp or (81000.0 if inst == "SENSEX" else 24200.0)
                                    atm_strike_flip = calculate_nearest_strike(sp_flip, inst)

                                    opt_contract_flip = self.angel_feed.resolve_contract(
                                        atm_strike_flip, current_consensus, inst
                                    )
                                    opt_ltp_flip = None
                                    if opt_contract_flip and opt_contract_flip.get("token"):
                                        tok_flip = str(opt_contract_flip["token"])
                                        # Primary: already-fetched batch LTP
                                        opt_ltp_flip = batch_ltps.get(tok_flip)
                                        # Secondary: direct fresh fetch if not in batch
                                        if opt_ltp_flip is None or opt_ltp_flip <= 0:
                                            sym_flip = opt_contract_flip.get("symbol", "")
                                            exch_flip = opt_contract_flip.get("exch_seg") or (
                                                "BFO" if ("SENSEX" in sym_flip or "BSX" in sym_flip) else "NFO"
                                            )
                                            try:
                                                opt_ltp_flip = await loop.run_in_executor(
                                                    None,
                                                    lambda: self.angel_feed.fetch_option_ltp(
                                                        sym_flip, tok_flip, exch_flip, max_cache_age=0
                                                    ),
                                                )
                                            except Exception as e_flip:
                                                logger.warning(
                                                    f"[Astro Consensus Flip] Direct LTP fetch failed "
                                                    f"for {sym_flip}: {e_flip}"
                                                )
                                                opt_ltp_flip = None

                                    MIN_LAUNCH_LTP = 25.0
                                    if opt_ltp_flip is None or opt_ltp_flip <= 0:
                                        logger.warning(
                                            f"[Astro Consensus Flip] Cannot obtain LTP for "
                                            f"{inst} {atm_strike_flip} {current_consensus}. "
                                            f"Slot switched to {target_slot_id} but launch "
                                            f"deferred — will retry on next tick."
                                        )
                                    elif opt_ltp_flip < MIN_LAUNCH_LTP:
                                        logger.warning(
                                            f"[Astro Consensus Flip] LTP ₹{opt_ltp_flip:.2f} is below "
                                            f"₹{MIN_LAUNCH_LTP} minimum. Slot {target_slot_id} switched "
                                            f"but launch blocked (option too cheap/illiquid)."
                                        )
                                    else:
                                        is_sensex_flip = (inst == "SENSEX")
                                        default_r_pts_flip = 150.0 if is_sensex_flip else 40.0
                                        default_profit_flip = 30.0 if is_sensex_flip else 8.0
                                        default_loss_flip = 30.0 if is_sensex_flip else 8.0

                                        r_pts_flip = getattr(flip_run.config, "range_points", None) or default_r_pts_flip
                                        profit_flip = flip_run.config.profit_point or default_profit_flip
                                        loss_flip = flip_run.config.loss_point or default_loss_flip
                                        s_cnt_flip = getattr(flip_run.config, "slicer_count", None) or 5
                                        r_high_flip = round(opt_ltp_flip, 2)
                                        r_low_flip = max(round(r_high_flip - r_pts_flip, 2), 0.05)
                                        s_step_flip = round(r_pts_flip / s_cnt_flip, 4)

                                        flip_cfg = ScalperRunConfig(
                                            run_id=target_slot_id,
                                            instrument_name=inst,
                                            option_type=current_consensus,
                                            strike=atm_strike_flip,
                                            range_points=r_pts_flip,
                                            slicer_count=s_cnt_flip,
                                            range_high=r_high_flip,
                                            range_low=r_low_flip,
                                            slice_interval=s_step_flip,
                                            profit_point=profit_flip,
                                            loss_point=loss_flip,
                                            qty_per_slice_lots=flip_run.config.qty_per_slice_lots or 1,
                                            spot_ltp=sp_flip,
                                            contract_symbol=f"{inst} {atm_strike_flip} {current_consensus}",
                                            contract_token=(
                                                str(opt_contract_flip.get("token")) if opt_contract_flip else None
                                            ),
                                            cutoff_time_ist=flip_run.config.cutoff_time_ist or "15:22",
                                            auto_eod_squareoff=flip_run.config.auto_eod_squareoff,
                                            trading_mode=self.trading_mode,
                                        )
                                        started_flip = self.run_manager.start_run(flip_cfg)
                                        self.last_astro_direction[inst] = current_consensus
                                        logger.info(
                                            f"[Astro Consensus Flip] Auto-launched {target_slot_id} "
                                            f"({inst} {current_consensus}) @ strike {atm_strike_flip}, "
                                            f"LTP ₹{opt_ltp_flip}."
                                        )
                                        if opt_ltp_flip and opt_ltp_flip > 0:
                                            started_flip.process_tick(opt_ltp_flip)
                            else:
                                logger.info(
                                    f"[Astro Consensus Flip] {prev_consensus} → {current_consensus} "
                                    f"on {inst}: no idle slot available for {inst} {current_consensus}. "
                                    f"Flip noted but no switch performed (target slot has open trade)."
                                )
                except Exception as e:
                    logger.warning(f"Error evaluating astro auto-trigger: {e}")

        await self.broadcast_state()

    async def _live_feed_loop(self) -> None:
        """Background continuous loop for this client."""
        logger.info(f"Live feed task started for client '{self.client_id}'.")
        while self.live_feed_running:
            has_active_runs = any(r.is_active for r in self.run_manager.runs.values())
            has_subscribers = len(self.sse_subscribers) > 0

            # If no ladder is running and nobody is currently viewing the cockpit in their browser,
            # pause and sleep to preserve Angel One API rate limit quota for active users.
            if not has_active_runs and not has_subscribers:
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
