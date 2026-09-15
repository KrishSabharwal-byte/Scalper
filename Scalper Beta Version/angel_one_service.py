"""
Angel One SmartAPI Live Feed Integration for Slicer Nifty
Handles authentication, TOTP generation, instrument lookup, nearest weekly expiry resolution,
and continuous live polling of Spot Index & Option LTP.
"""

import asyncio
import datetime
import json
import logging
import math
import os
import re
import threading
import time
import urllib.request
from typing import Dict, Any, Optional, List
import pyotp
from SmartApi import SmartConnect

logger = logging.getLogger("AngelOneService")
logging.basicConfig(level=logging.INFO)

ANGEL_CLIENT_ID = os.environ.get("ANGEL_CLIENT_ID")
ANGEL_API_KEY = os.environ.get("ANGEL_API_KEY")
ANGEL_TOTP_SECRET = os.environ.get("ANGEL_TOTP_SECRET")
ANGEL_MPIN = os.environ.get("ANGEL_MPIN")

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

INSTRUMENT_CONFIG: Dict[str, Dict[str, Any]] = {
    "NIFTY": {
        "name": "NIFTY",
        "symbol_prefix": "NIFTY",
        "strike_step": 50,
        "lot_size": 65,
        "spot_exchange": "NSE",
        "spot_symbol": "Nifty 50",
        "spot_token": "99926000",
        "opt_exchange": "NFO",
        "opt_names": ["NIFTY"],
    },
    "SENSEX": {
        "name": "SENSEX",
        "symbol_prefix": "SENSEX",
        "strike_step": 100,
        "lot_size": 20,
        "spot_exchange": "BSE",
        "spot_symbol": "SENSEX",
        "spot_token": "99919000",
        "opt_exchange": "BFO",
        "opt_names": ["SENSEX", "BSX"],
    },
}


def calculate_nearest_strike(spot_price: float, instrument: str = "NIFTY") -> int:
    """
    Calculates the nearest strike price based on the instrument's native strike step
    (NIFTY = 50 pts, SENSEX = 100 pts) using standard half-up rounding.
    """
    cfg = INSTRUMENT_CONFIG.get(instrument.upper(), INSTRUMENT_CONFIG["NIFTY"])
    step = float(cfg["strike_step"])
    return int(math.floor((spot_price + (step / 2.0)) / step) * int(step))


def calculate_nearest_50_strike(spot_price: float) -> int:
    """Calculates the nearest 50 strike price (NIFTY default)."""
    return calculate_nearest_strike(spot_price, "NIFTY")


class AngelOneFeed:
    # Class-level rate limiter: Angel One enforces a maximum of 3 requests/sec per API key.
    # Enforcing a minimum interval of 0.65s guarantees <= 1.54 req/s across all concurrent threads.
    _pacer_lock = threading.Lock()
    _last_api_call_ts: Dict[str, float] = {}
    _min_call_interval: float = 0.65
    _rate_limit_cooldown_until: float = 0.0

    @classmethod
    def set_rate_limit_cooldown(cls, seconds: float = 3.0) -> None:
        """Sets a global silent cooldown when a rate limit is detected to allow Angel One's rolling window to reset."""
        with cls._pacer_lock:
            cls._rate_limit_cooldown_until = max(cls._rate_limit_cooldown_until, time.monotonic() + seconds)
            logger.warning(f"Angel One rate limit detected. Enforcing {seconds:.1f}s cooldown across all calls.")

    @classmethod
    def is_in_cooldown(cls) -> bool:
        """Checks if calls are currently suppressed due to active rate-limit cooldown."""
        with cls._pacer_lock:
            return time.monotonic() < cls._rate_limit_cooldown_until

    def __init__(
        self,
        client_id: Optional[str] = None,
        api_key: Optional[str] = None,
        totp_secret: Optional[str] = None,
        mpin: Optional[str] = None,
        owner_client_id: Optional[str] = None,
    ):
        self.client_id = client_id or ANGEL_CLIENT_ID
        self.api_key = api_key or ANGEL_API_KEY
        self.totp_secret = totp_secret or ANGEL_TOTP_SECRET
        self.mpin = mpin or ANGEL_MPIN
        self.owner_client_id = (owner_client_id or "").strip()

        self.smart_api: Optional[SmartConnect] = None
        self.is_authenticated = False
        self.session_data: Dict[str, Any] = {}
        self.last_auth_time: Optional[datetime.datetime] = None

        # In-memory fast caches to avoid redundant API hits within high-frequency poll loops
        self._spot_cache: Dict[str, Dict[str, Any]] = {}
        self._option_cache: Dict[str, Dict[str, Any]] = {}

        # Multi-instrument cache: {"NIFTY": [...], "SENSEX": [...]}
        self.options_cache: Dict[str, List[Dict[str, Any]]] = {"NIFTY": [], "SENSEX": []}
        self.last_cache_time: Optional[datetime.datetime] = None

        # Live Feed Status
        self.is_streaming = False
        self.stream_task: Optional[asyncio.Task] = None
        self.poll_interval_sec = 3.0

        self.latest_spot_ltp: Optional[float] = None
        self.latest_active_strike: Optional[int] = None
        self.latest_option_contract: Optional[Dict[str, Any]] = None
        self.latest_option_ltp: Optional[float] = None
        self.last_tick_time: Optional[str] = None
        self.error_message: Optional[str] = None

        # Per-instrument tracking
        self.latest_spot_by_inst: Dict[str, float] = {}
        self.latest_strike_by_inst: Dict[str, int] = {}

    def _pace_api_call(self) -> None:
        """
        Thread-safe pacer enforcing a minimum interval between calls for this API key.
        Respects active cooldowns and guarantees <= 1.54 req/s to prevent HTTP 429 rate limit.
        """
        key = self.api_key or self.client_id or "default"
        with AngelOneFeed._pacer_lock:
            now = time.monotonic()
            if now < AngelOneFeed._rate_limit_cooldown_until:
                wait_cd = AngelOneFeed._rate_limit_cooldown_until - now
                if wait_cd > 0:
                    time.sleep(wait_cd)
                now = time.monotonic()

            last_ts = AngelOneFeed._last_api_call_ts.get(key, 0.0)
            elapsed = now - last_ts
            wait_time = AngelOneFeed._min_call_interval - elapsed
            if wait_time > 0:
                time.sleep(wait_time)
            AngelOneFeed._last_api_call_ts[key] = time.monotonic()

    @property
    def nifty_options_cache(self) -> List[Dict[str, Any]]:
        return self.options_cache.get("NIFTY", [])

    @nifty_options_cache.setter
    def nifty_options_cache(self, val: List[Dict[str, Any]]) -> None:
        self.options_cache["NIFTY"] = val

    def login(self) -> bool:
        """
        Authenticates with Angel One using SmartAPI and TOTP.
        """
        if not (self.client_id and self.api_key and self.totp_secret and self.mpin):
            self.error_message = "Angel One credentials missing or incomplete. Please submit broker credentials."
            self.is_authenticated = False
            return False

        for attempt in range(2):
            try:
                self._pace_api_call()
                totp = pyotp.TOTP(self.totp_secret).now()
                self.smart_api = SmartConnect(api_key=self.api_key)
                session = self.smart_api.generateSession(self.client_id, self.mpin, totp)
                
                if session and session.get("status") is True:
                    self.session_data = session.get("data", {})
                    self.is_authenticated = True
                    self.last_auth_time = datetime.datetime.now()
                    self.error_message = None
                    logger.info(f"Angel One Login Successful for Client: {self.client_id}")
                    self._load_instruments()
                    return True
                else:
                    msg = session.get("message", "Login failed") if session else "Unknown login failure"
                    self.error_message = msg
                    self.is_authenticated = False
                    logger.error(f"Angel One Login failed: {msg}")
                    return False
            except Exception as e:
                err_str = str(e)
                if "exceeding access rate" in err_str or "Access denied" in err_str:
                    logger.warning(f"Rate limited on Angel One login (attempt {attempt+1}), backing off 0.5s...")
                    time.sleep(0.5)
                    continue
                self.error_message = str(e)
                self.is_authenticated = False
                logger.error(f"Angel One Login Exception: {e}", exc_info=True)
                return False
        return False

    def _load_instruments(self):
        """
        Loads and caches both NIFTY and SENSEX options instruments from Angel One OpenAPI Scrip Master.
        Persists to a local cache file for fast offline/startup loading.
        """
        cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrips_instruments.json")
        legacy_nifty_cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrips_nifty.json")
        today_date = datetime.date.today()
        
        # Check if fresh multi-instrument cache exists
        if os.path.exists(cache_file):
            try:
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(cache_file)).date()
                if mtime == today_date:
                    with open(cache_file, "r") as f:
                        cached_data = json.load(f)
                    self.options_cache["NIFTY"] = self._parse_scrip_items(cached_data.get("NIFTY", []))
                    self.options_cache["SENSEX"] = self._parse_scrip_items(cached_data.get("SENSEX", []))
                    self.last_cache_time = datetime.datetime.now()
                    logger.info(
                        f"Loaded {len(self.options_cache['NIFTY'])} NIFTY and {len(self.options_cache['SENSEX'])} SENSEX option contracts from local cache."
                    )
                    return
            except Exception as e:
                logger.warning(f"Error reading local scrips cache: {e}")

        # Download fresh OpenAPI Scrip Master
        try:
            logger.info("Downloading official Angel One OpenAPI Scrip Master...")
            url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                
                nifty_items = [
                    i for i in raw
                    if i.get("exch_seg") == "NFO" and i.get("name") == "NIFTY" and i.get("instrumenttype") == "OPTIDX"
                ]
                sensex_items = [
                    i for i in raw
                    if i.get("exch_seg") == "BFO" and i.get("name") in ["SENSEX", "BSX"] and i.get("instrumenttype") == "OPTIDX"
                ]

                with open(cache_file, "w") as f:
                    json.dump({"NIFTY": nifty_items, "SENSEX": sensex_items}, f)

                self.options_cache["NIFTY"] = self._parse_scrip_items(nifty_items)
                self.options_cache["SENSEX"] = self._parse_scrip_items(sensex_items)
                self.last_cache_time = datetime.datetime.now()
                logger.info(
                    f"Cached {len(self.options_cache['NIFTY'])} NIFTY and {len(self.options_cache['SENSEX'])} SENSEX option contracts from Scrip Master."
                )
                return
        except Exception as e:
            logger.error(f"Error downloading OpenAPIScripMaster: {e}")

        # Fallback to legacy cache if available
        if os.path.exists(legacy_nifty_cache):
            try:
                with open(legacy_nifty_cache, "r") as f:
                    cached_items = json.load(f)
                self.options_cache["NIFTY"] = self._parse_scrip_items(cached_items)
                logger.info(f"Fallback loaded {len(self.options_cache['NIFTY'])} NIFTY contracts from legacy cache.")
            except Exception as e:
                logger.error(f"Error loading legacy cache: {e}")

    def _parse_scrip_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        parsed = []
        for i in items:
            sym = i.get("symbol", "")
            token = str(i.get("token", ""))
            strike_raw = i.get("strike", 0)
            exp_str = i.get("expiry", "")
            try:
                strike_val = int(float(strike_raw) / 100) if float(strike_raw) > 100000 else int(float(strike_raw))
            except Exception:
                continue

            otype = "CE" if sym.endswith("CE") else ("PE" if sym.endswith("PE") else None)
            if not otype:
                continue

            try:
                if len(exp_str) == 9:
                    exp_date = datetime.datetime.strptime(exp_str, "%d%b%Y").date()
                else:
                    exp_date = datetime.datetime.strptime(exp_str, "%d%b%y").date()
            except Exception:
                continue

            parsed.append({
                "symbol": sym,
                "token": token,
                "expiry_str": exp_str,
                "expiry_date": exp_date,
                "strike": strike_val,
                "option_type": otype,
                "exch_seg": i.get("exch_seg", "NFO"),
            })
        return parsed

    def resolve_contract(self, strike: int, option_type: str, instrument: str = "NIFTY") -> Optional[Dict[str, Any]]:
        """
        Finds the exact nearest weekly expiry contract matching the strike, option type (CE/PE), and instrument (NIFTY/SENSEX).
        """
        inst_key = instrument.upper()
        cache = self.options_cache.get(inst_key, [])
        if not cache:
            self._load_instruments()
            cache = self.options_cache.get(inst_key, [])

        otype = option_type.upper()
        if otype in ["CALL", "C"]:
            otype = "CE"
        elif otype in ["PUT", "P"]:
            otype = "PE"

        today = datetime.date.today()
        matched = [
            o for o in cache
            if o["strike"] == strike and o["option_type"] == otype and o["expiry_date"] >= today
        ]

        if matched:
            matched.sort(key=lambda x: x["expiry_date"])
            best = matched[0]
            logger.info(f"Resolved {inst_key} nearest weekly contract for strike {strike} {otype}: {best['symbol']} (token: {best['token']}, expiry: {best['expiry_str']})")
            return best

        # Fallback if no contract has expiry >= today
        all_strike = [
            o for o in cache
            if o["strike"] == strike and o["option_type"] == otype
        ]
        if all_strike:
            all_strike.sort(key=lambda x: abs((x["expiry_date"] - today).days))
            best = all_strike[0]
            logger.info(f"Resolved {inst_key} fallback contract for strike {strike} {otype}: {best['symbol']} (token: {best['token']}, expiry: {best['expiry_str']})")
            return best

        return None

    def get_available_strikes(self, spot_price: float, instrument: str = "NIFTY", count_each_side: int = 15) -> List[int]:
        """
        Returns a sorted list of unique strike prices available around the spot_price (ATM ± count_each_side strikes).
        Uses instrument master data when available, falling back to native step (50 for NIFTY, 100 for SENSEX).
        """
        inst_key = instrument.upper()
        cache = self.options_cache.get(inst_key, [])
        if not cache:
            self._load_instruments()
            cache = self.options_cache.get(inst_key, [])

        step = 100 if inst_key == "SENSEX" else 50
        atm_strike = calculate_nearest_strike(spot_price, inst_key)

        cached_strikes = set()
        if cache:
            for o in cache:
                stk = o.get("strike")
                if stk and isinstance(stk, (int, float)) and stk > 0:
                    cached_strikes.add(int(stk))

        if cached_strikes:
            lower_bound = atm_strike - (count_each_side * step)
            upper_bound = atm_strike + (count_each_side * step)
            strikes = sorted([s for s in cached_strikes if lower_bound <= s <= upper_bound])
            if atm_strike not in strikes:
                strikes.append(atm_strike)
                strikes.sort()
            if len(strikes) >= 5:
                return strikes

        # Fallback to pure arithmetic step calculation
        return [atm_strike + (i * step) for i in range(-count_each_side, count_each_side + 1)]

    def fetch_market_data_batch(
        self,
        exchange_tokens: Dict[str, List[str]],
        max_cache_age: float = 1.8,
    ) -> Dict[str, float]:
        """
        Fetches live LTP for multiple instruments and options in a single unified API call
        via SmartAPI getMarketData(mode="LTP", exchangeTokens=...).
        Drastically slashes outbound requests by up to 80%, completely preventing HTTP 429 rate limiting.
        """
        results: Dict[str, float] = {}
        tokens_needed: Dict[str, List[str]] = {}

        now = time.monotonic()
        # 1. Check in-memory fast caches first
        for exch, tokens in exchange_tokens.items():
            for t in tokens:
                t_str = str(t)
                cached_opt = self._option_cache.get(t_str)
                if cached_opt and (now - cached_opt.get("time", 0.0)) < max_cache_age:
                    results[t_str] = cached_opt["ltp"]
                elif t_str == "99926000" and self._spot_cache.get("NIFTY") and (now - self._spot_cache["NIFTY"].get("time", 0.0)) < max_cache_age:
                    results[t_str] = self._spot_cache["NIFTY"]["ltp"]
                elif t_str == "99919000" and self._spot_cache.get("SENSEX") and (now - self._spot_cache["SENSEX"].get("time", 0.0)) < max_cache_age:
                    results[t_str] = self._spot_cache["SENSEX"]["ltp"]
                else:
                    tokens_needed.setdefault(exch, []).append(t_str)

        if not tokens_needed:
            return results

        if AngelOneFeed.is_in_cooldown():
            # Return cached data during cooldown
            for exch, tokens in tokens_needed.items():
                for t in tokens:
                    t_str = str(t)
                    if t_str == "99926000" and "NIFTY" in self.latest_spot_by_inst:
                        results[t_str] = self.latest_spot_by_inst["NIFTY"]
                    elif t_str == "99919000" and "SENSEX" in self.latest_spot_by_inst:
                        results[t_str] = self.latest_spot_by_inst["SENSEX"]
                    elif t_str in self._option_cache:
                        results[t_str] = self._option_cache[t_str]["ltp"]
            return results

        if not self.smart_api or not self.is_authenticated:
            if not self.login():
                return results

        try:
            self._pace_api_call()
            resp = self.smart_api.getMarketData("LTP", tokens_needed)
            if resp and resp.get("status") and resp.get("data"):
                fetched = resp["data"].get("fetched", [])
                for item in fetched:
                    t_str = str(item.get("symbolToken", ""))
                    ltp_val = float(item.get("ltp", 0.0))
                    sym = item.get("tradingSymbol", "")
                    if ltp_val > 0:
                        results[t_str] = ltp_val
                        now_ts = time.monotonic()
                        if t_str == "99926000":
                            self.latest_spot_ltp = ltp_val
                            self.latest_spot_by_inst["NIFTY"] = ltp_val
                            self._spot_cache["NIFTY"] = {"ltp": ltp_val, "time": now_ts}
                            stk = calculate_nearest_strike(ltp_val, "NIFTY")
                            self.latest_active_strike = stk
                            self.latest_strike_by_inst["NIFTY"] = stk
                        elif t_str == "99919000":
                            self.latest_spot_by_inst["SENSEX"] = ltp_val
                            self._spot_cache["SENSEX"] = {"ltp": ltp_val, "time": now_ts}
                            self.latest_strike_by_inst["SENSEX"] = calculate_nearest_strike(ltp_val, "SENSEX")
                        else:
                            self.latest_option_ltp = ltp_val
                            self._option_cache[t_str] = {"ltp": ltp_val, "time": now_ts}
                            if sym:
                                self._option_cache[f"{sym}:{t_str}"] = {"ltp": ltp_val, "time": now_ts}
                            self.last_tick_time = datetime.datetime.now(IST).strftime("%H:%M:%S")
                return results
            elif resp and not resp.get("status"):
                msg = str(resp.get("message", ""))
                errcode = str(resp.get("errorcode", ""))
                if "exceeding access rate" in msg.lower() or "rate" in msg.lower():
                    AngelOneFeed.set_rate_limit_cooldown(3.0)
                elif "token" in msg.lower() or "session" in msg.lower() or errcode in ("AG8001", "AB1010"):
                    self.login()
        except Exception as e:
            err_str = str(e)
            if "exceeding access rate" in err_str or "Access denied" in err_str:
                AngelOneFeed.set_rate_limit_cooldown(3.0)
            else:
                logger.error(f"Error in fetch_market_data_batch: {e}")

        # Fallback to last known cached prices so trading engine doesn't stutter
        for exch, tokens in tokens_needed.items():
            for t in tokens:
                t_str = str(t)
                if t_str not in results:
                    if t_str == "99926000" and "NIFTY" in self.latest_spot_by_inst:
                        results[t_str] = self.latest_spot_by_inst["NIFTY"]
                    elif t_str == "99919000" and "SENSEX" in self.latest_spot_by_inst:
                        results[t_str] = self.latest_spot_by_inst["SENSEX"]
                    elif t_str in self._option_cache:
                        results[t_str] = self._option_cache[t_str]["ltp"]

        return results

    def fetch_spot_ltp(self, instrument: str = "NIFTY", max_cache_age: float = 1.8) -> Optional[float]:
        """
        Fetches the live Spot Index LTP for NIFTY (NSE) or SENSEX (BSE) with pacing, caching, and auto-authentication.
        """
        inst_key = instrument.upper()

        # 1. High-frequency cache hit: return if cached within max_cache_age
        cached = self._spot_cache.get(inst_key)
        if cached and (time.monotonic() - cached.get("time", 0.0)) < max_cache_age:
            return cached.get("ltp")

        if AngelOneFeed.is_in_cooldown():
            return self.latest_spot_by_inst.get(inst_key)

        inst_cfg = INSTRUMENT_CONFIG.get(inst_key, INSTRUMENT_CONFIG["NIFTY"])
        exchange = inst_cfg["spot_exchange"]
        symbol = inst_cfg["spot_symbol"]
        token = inst_cfg["spot_token"]

        if not self.smart_api or not self.is_authenticated:
            logger.info(f"Angel One not authenticated on spot fetch ({inst_key}). Attempting automatic login...")
            if not self.login():
                return self.latest_spot_by_inst.get(inst_key)

        try:
            self._pace_api_call()
            resp = self.smart_api.ltpData(exchange, symbol, token)
            if resp and resp.get("status") and resp.get("data"):
                ltp = float(resp["data"]["ltp"])
                self.latest_spot_ltp = ltp
                self.latest_spot_by_inst[inst_key] = ltp
                self._spot_cache[inst_key] = {"ltp": ltp, "time": time.monotonic()}
                nearest = calculate_nearest_strike(ltp, inst_key)
                self.latest_active_strike = nearest
                self.latest_strike_by_inst[inst_key] = nearest
                return ltp
            elif resp and not resp.get("status"):
                msg = str(resp.get("message", ""))
                errorcode = str(resp.get("errorcode", ""))
                if "token" in msg.lower() or "session" in msg.lower() or "invalid" in msg.lower() or errorcode in ("AG8001", "AB1010"):
                    logger.warning(f"Angel One session expired ({msg}). Re-authenticating...")
                    self.login()
                elif "exceeding access rate" in msg.lower() or "rate" in msg.lower():
                    AngelOneFeed.set_rate_limit_cooldown(3.0)
        except Exception as e:
            err_str = str(e)
            if "exceeding access rate" in err_str or "Access denied" in err_str:
                AngelOneFeed.set_rate_limit_cooldown(3.0)
            else:
                logger.error(f"Error fetching {inst_key} spot LTP: {e}")

        # Fallback to last known spot price so UI doesn't freeze or drop
        return self.latest_spot_by_inst.get(inst_key)

    def fetch_option_ltp(self, symbol: str, token: str, exchange: Optional[str] = None, max_cache_age: float = 1.8) -> Optional[float]:
        """
        Fetches the live Option LTP for a given symbol and token with pacing, caching, and auto-authentication.

        When max_cache_age=0 (forced-fresh mode, used by Astro auto-trigger to avoid cross-contract
        price contamination), the per-symbol cache is bypassed entirely.  If the live API call also
        fails, this method returns None rather than falling back to self.latest_option_ltp, which is
        a global that could belong to a completely different strike/contract.
        """
        cache_key = f"{symbol}:{token}"
        cached = self._option_cache.get(cache_key) or self._option_cache.get(str(token))
        # When max_cache_age=0 the caller explicitly wants a live price — skip the cache check.
        if max_cache_age > 0 and cached and (time.monotonic() - cached.get("time", 0.0)) < max_cache_age:
            return cached.get("ltp")

        if AngelOneFeed.is_in_cooldown():
            # During a rate-limit cooldown, return the per-symbol cache if available;
            # never return the global latest_option_ltp when forced-fresh (max_cache_age=0).
            if cached and max_cache_age > 0:
                return cached.get("ltp")
            return None

        if not exchange:
            exchange = "BFO" if ("SENSEX" in symbol or "BSX" in symbol) else "NFO"

        if not self.smart_api or not self.is_authenticated:
            logger.info(f"Angel One not authenticated on option fetch {symbol}. Attempting automatic login...")
            if not self.login():
                # Same rule: don't bleed the global when forced-fresh.
                if cached and max_cache_age > 0:
                    return cached.get("ltp")
                return None

        try:
            self._pace_api_call()
            resp = self.smart_api.ltpData(exchange, symbol, token)
            if resp and resp.get("status") and resp.get("data"):
                ltp = float(resp["data"]["ltp"])
                self.latest_option_ltp = ltp
                self._option_cache[cache_key] = {"ltp": ltp, "time": time.monotonic()}
                self._option_cache[str(token)] = {"ltp": ltp, "time": time.monotonic()}
                return ltp
            elif resp and not resp.get("status"):
                msg = str(resp.get("message", ""))
                errorcode = str(resp.get("errorcode", ""))
                if "token" in msg.lower() or "session" in msg.lower() or "invalid" in msg.lower() or errorcode in ("AG8001", "AB1010"):
                    logger.warning(f"Angel One session expired on option fetch ({msg}). Re-authenticating...")
                    self.login()
                elif "exceeding access rate" in msg.lower() or "rate" in msg.lower():
                    AngelOneFeed.set_rate_limit_cooldown(3.0)
        except Exception as e:
            err_str = str(e)
            if "exceeding access rate" in err_str or "Access denied" in err_str:
                AngelOneFeed.set_rate_limit_cooldown(3.0)
            else:
                logger.error(f"Error fetching option LTP for {symbol}: {e}")

        # Fallback to the per-symbol cache if available.
        # IMPORTANT: when max_cache_age=0 (forced-fresh / Astro auto-trigger), do NOT fall back to
        # self.latest_option_ltp — that global belongs to whatever contract was last fetched anywhere
        # in the system and could be a completely different strike (e.g. deep-OTM 24300 CE @ ₹0.70
        # contaminating an ATM 23300 CE entry).  Return None so callers can postpone safely.
        if cached and max_cache_age > 0:
            return cached.get("ltp")
        return None

    async def poll_cycle(self, active_option_type: str = "CE", instrument: str = "NIFTY") -> Dict[str, Any]:
        """
        Performs one single poll cycle with rate limiting protection:
        1. Fetches spot LTP
        2. Calculates nearest 50 strike
        3. Resolves current option contract
        4. Fetches live option LTP
        """
        loop = asyncio.get_running_loop()
        
        if not self.is_authenticated:
            logged_in = await loop.run_in_executor(None, self.login)
            if not logged_in:
                return {"status": "error", "message": self.error_message or "Authentication failed"}

        # 1. Fetch spot LTP
        spot_ltp = await loop.run_in_executor(None, self.fetch_spot_ltp)
        if spot_ltp is None:
            return {"status": "error", "message": "Failed to fetch spot LTP"}

        self.latest_spot_ltp = spot_ltp
        strike = calculate_nearest_50_strike(spot_ltp)
        self.latest_active_strike = strike

        # 2. Resolve option contract
        contract = self.resolve_contract(strike, active_option_type)
        if not contract:
            return {
                "status": "partial",
                "spot_ltp": spot_ltp,
                "strike": strike,
                "message": f"No active contract found for strike {strike} {active_option_type}",
            }

        self.latest_option_contract = contract

        # 1.2s pause between requests to strictly respect Angel One rate limits
        await asyncio.sleep(1.2)

        # 3. Fetch Option LTP
        opt_ltp = await loop.run_in_executor(
            None, self.fetch_option_ltp, contract["symbol"], contract["token"]
        )
        self.latest_option_ltp = opt_ltp
        self.last_tick_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

        return {
            "status": "success",
            "spot_ltp": spot_ltp,
            "strike": strike,
            "contract": contract["symbol"],
            "contract_token": contract["token"],
            "expiry": contract["expiry_str"],
            "option_type": contract["option_type"],
            "option_ltp": opt_ltp,
            "timestamp": self.last_tick_time,
        }

    def get_status(self) -> Dict[str, Any]:
        return {
            "authenticated": self.is_authenticated,
            "streaming": self.is_streaming,
            "client_id": self.client_id,
            "owner_client_id": self.owner_client_id,
            "spot_ltp": self.latest_spot_ltp,
            "spot_by_inst": dict(self.latest_spot_by_inst),
            "active_strike": self.latest_active_strike,
            "active_contract": self.latest_option_contract.get("symbol") if self.latest_option_contract else None,
            "contract_token": self.latest_option_contract.get("token") if self.latest_option_contract else None,
            "option_ltp": self.latest_option_ltp,
            "last_tick_time": self.last_tick_time,
            "error": self.error_message,
        }

    def place_order(
        self,
        client_id: str,
        run_id: str,
        run_owner_client_id: str,
        symbol: str,
        token: str,
        quantity: int,
        price: float,
        transaction_type: str = "BUY",
        order_type: str = "LIMIT",
        product_type: str = "INTRADAY",
        trading_mode: str = "paper",
    ) -> Dict[str, Any]:
        """
        Executes an order on Angel One with strict fail-closed safety validation.
        trading_mode must be explicitly passed ('paper' or 'live').
        Validates client_id == run_owner_client_id == self.owner_client_id and validates mode match.
        """
        import uuid
        from order_safety_service import validate_order_safety

        mode = (trading_mode or "paper").strip().lower()
        if mode not in ("paper", "live"):
            mode = "paper"

        order_params = {
            "symbol": symbol,
            "token": token,
            "quantity": quantity,
            "price": price,
            "transaction_type": transaction_type,
            "order_type": order_type,
            "product_type": product_type,
            "trading_mode": mode,
        }

        # Hard fail-closed security validation (including Check 5: trading_mode match)
        validate_order_safety(
            client_id=client_id,
            run_id=run_id,
            run_owner_client_id=run_owner_client_id,
            credential_owner_client_id=self.owner_client_id or client_id,
            order_params=order_params,
        )

        logger.info(
            f"Order safety validation passed: Client '{client_id}', Run '{run_id}', Mode '{mode}', "
            f"{transaction_type} {quantity}x {symbol} @ ₹{price}"
        )

        if mode == "live":
            # Attempt auto-login if credentials exist and session is not yet active
            if (not self.smart_api or not self.is_authenticated) and self.client_id and self.api_key and self.totp_secret and self.mpin:
                try:
                    logger.info(f"Attempting live broker login for client '{client_id}' prior to order placement...")
                    self.login()
                except Exception as login_err:
                    logger.warning(f"Auto-login attempt failed: {login_err}")

            # Hard requirement: SmartAPI must be instantiated and authenticated
            if not self.smart_api or not self.is_authenticated:
                err_msg = "Live mode requires an authenticated AngelOne session. Order rejected."
                logger.error(f"LIVE ORDER REJECTED for client '{client_id}': {err_msg}")
                return {
                    "status": "error",
                    "message": err_msg,
                    "mode": "live",
                    "order_params": order_params,
                }

            try:
                # Format price correctly for exchange: 0 for MARKET, tick-size (0.05) multiple for LIMIT
                clean_ordertype = order_type.upper()
                if clean_ordertype == "MARKET":
                    price_str = "0"
                else:
                    # Round to nearest 0.05 tick size
                    tick = 0.05
                    aligned_price = round(round(float(price) / tick) * tick, 2)
                    price_str = f"{aligned_price:.2f}"

                exchange = "BFO" if ("SENSEX" in symbol.upper() or "BSX" in symbol.upper()) else "NFO"

                order_payload = {
                    "variety": "NORMAL",
                    "tradingsymbol": str(symbol),
                    "symboltoken": str(token),
                    "transactiontype": transaction_type.upper(),
                    "exchange": exchange,
                    "ordertype": clean_ordertype,
                    "producttype": product_type.upper(),
                    "duration": "DAY",
                    "price": price_str,
                    "squareoff": "0",
                    "stoploss": "0",
                    "quantity": str(int(quantity)),
                }

                self._pace_api_call()
                order_id_str = None
                api_err_msg = None

                # Primary: query API and capture detailed status/error dictionary
                if hasattr(self.smart_api, "_postRequest"):
                    try:
                        api_res = self.smart_api._postRequest("api.order.place", order_payload)
                        if api_res and isinstance(api_res, dict) and api_res.get("status"):
                            data = api_res.get("data") or {}
                            order_id_str = str(data.get("orderid", "")) if isinstance(data, dict) else str(data)
                        elif api_res and isinstance(api_res, dict):
                            api_err_msg = api_res.get("message") or api_res.get("errorcode")
                    except Exception as req_err:
                        logger.warning(f"Note calling SmartAPI _postRequest: {req_err}")

                # Fallback to standard placeOrder method if needed
                if not order_id_str and self.smart_api:
                    # If token was invalid or expired (AB1007), refresh login session and retry
                    if api_err_msg in ("Invalid Token", "Token Exception") or "AB1007" in str(api_err_msg):
                        logger.info(f"Angel One session expired or invalid token for client '{client_id}'. Auto-refreshing session...")
                        if self.login() and self.smart_api:
                            try:
                                api_res = self.smart_api._postRequest("api.order.place", order_payload)
                                if api_res and isinstance(api_res, dict) and api_res.get("status"):
                                    data = api_res.get("data") or {}
                                    order_id_str = str(data.get("orderid", "")) if isinstance(data, dict) else str(data)
                                elif api_res and isinstance(api_res, dict):
                                    api_err_msg = api_res.get("message") or api_res.get("errorcode")
                            except Exception as retry_err:
                                logger.warning(f"Error during retried order place: {retry_err}")

                    if not order_id_str:
                        try:
                            res = self.smart_api.placeOrder(order_payload)
                            if res:
                                order_id_str = str(res)
                        except Exception as pe_err:
                            logger.warning(f"Note calling SmartAPI placeOrder: {pe_err}")

                if not order_id_str:
                    err_msg = api_err_msg or f"Angel One rejected {transaction_type} order for {symbol} ({quantity} qty @ {price_str}). Check margin, market hours, or contract."
                    logger.error(f"LIVE ORDER FAILED for client '{client_id}': {err_msg}")
                    return {
                        "status": "error",
                        "message": err_msg,
                        "mode": "live",
                        "order_params": order_params,
                    }

                logger.info(f"LIVE ORDER CONFIRMED: Client '{client_id}', Order ID: {order_id_str}, {transaction_type} {quantity}x {symbol} @ ₹{price_str}")
                return {
                    "status": "success",
                    "order_id": order_id_str,
                    "mode": "live",
                    "order_params": order_params,
                }
            except Exception as e:
                logger.error(f"Error placing live order with SmartAPI: {e}", exc_info=True)
                return {
                    "status": "error",
                    "message": str(e),
                    "mode": "live",
                    "order_params": order_params,
                }

        # Paper Mode: Never call smart_api.placeOrder() even if authenticated
        fill_price = price if price and price > 0 else (self.latest_option_ltp or 0.0)
        return {
            "status": "success",
            "order_id": f"sim_{uuid.uuid4().hex[:8]}",
            "mode": "paper",
            "fill_price": fill_price,
            "order_params": order_params,
        }

    def get_order_book(self) -> List[Dict[str, Any]]:
        """
        Retrieves today's complete order book from Angel One via SmartAPI.
        Returns parsed list of normalized order records.
        """
        if not self.smart_api or not self.is_authenticated:
            return []

        try:
            self._pace_api_call()
            res = self.smart_api.orderBook()
            if not res or not isinstance(res, dict) or not res.get("status"):
                return []

            raw_orders = res.get("data") or []
            if not isinstance(raw_orders, list):
                return []

            orders = []
            for o in raw_orders:
                orders.append({
                    "order_id": str(o.get("orderid", "")),
                    "tradingsymbol": o.get("tradingsymbol", ""),
                    "symboltoken": str(o.get("symboltoken", "")),
                    "transaction_type": o.get("transactiontype", "BUY"),
                    "order_type": o.get("ordertype", "LIMIT"),
                    "product_type": o.get("producttype", "INTRADAY"),
                    "price": float(o.get("price", 0.0) or 0.0),
                    "quantity": int(o.get("quantity", 0) or 0),
                    "filled_quantity": int(o.get("filledshares", 0) or 0),
                    "unfilled_quantity": int(o.get("unfilledshares", 0) or 0),
                    "order_status": (o.get("orderstatus") or o.get("status") or "UNKNOWN").upper(),
                    "updatetime": o.get("updatetime", ""),
                    "exchange": o.get("exchange", ""),
                    "rejection_reason": o.get("text", "") or o.get("rejectionreason", ""),
                })
            return orders
        except Exception as e:
            logger.warning(f"Error fetching order book from Angel One: {e}")
            return []

