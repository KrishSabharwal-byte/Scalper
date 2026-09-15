"""
Slicing Scalper Simulation Engine - Hardened Core Engine
Step-Down Slicing Option Scalper System implementing:
- Exact 3-step state machine (Loss Backstop -> Profit Sell -> Grid Buy)
- Float precision elimination using integer tick arithmetic
- Configurable gap fill policies (all_crossed | single | skip_and_log)
- Strike locking on open positions & optional flat-restrike
- Loss-exit ladder recentering anchored to current LTP
- Feed staleness tracking & paused buy entries
- Session cutoffs & EOD squareoff
- State persistence & crash recovery
- Monotonic sequence deduplication
- Multi-slot isolation (up to 3 concurrent slots)
"""

import datetime
import json
import math
import os
import re
import uuid
from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


class SliceStatus(str, Enum):
    UNUSED = "UNUSED"
    USED = "USED"
    FILLED = "FILLED"
    EXITED = "EXITED"
    CANCELLED = "CANCELLED"
    PENDING = "UNUSED"  # Backward compatibility alias

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str) and value.upper() == "PENDING":
            return cls.UNUSED
        return super()._missing_(value)


class SliceExitReason(str, Enum):
    PROFIT_TARGET = "PROFIT_TARGET"
    LOSS_EXIT = "LOSS_EXIT"
    EOD_SQUAREOFF = "EOD_SQUAREOFF"
    MANUAL = "MANUAL"


class GapFillMode(str, Enum):
    ALL_CROSSED = "all_crossed"
    SINGLE = "single"
    SKIP_AND_LOG = "skip_and_log"


def get_sequential_label(index: int) -> str:
    """
    Converts 0-based integer to spreadsheet-style alphabetical label:
    0 -> A, 1 -> B, ... 25 -> Z, 26 -> AA, 27 -> AB, etc.
    """
    label = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        label = chr(65 + remainder) + label
    return label


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
        "default_spot": 24200.0,
        "default_range_points": 40.0,
        "default_slicer_count": 5,
        "default_range_high": 140.0,
        "default_range_low": 100.0,
        "default_step": 8.0,
        "default_profit_point": 8.0,
        "default_loss_point": 8.0,
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
        "default_spot": 81000.0,
        "default_range_points": 150.0,
        "default_slicer_count": 5,
        "default_range_high": 250.0,
        "default_range_low": 100.0,
        "default_step": 30.0,
        "default_profit_point": 30.0,
        "default_loss_point": 30.0,
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


def calculate_level_for_price(
    price: float, range_high: float, range_low: float, step: float
) -> Optional[float]:
    """
    Calculates the exact grid rung L for a given price based on price-range membership:
    L is the level such that: L - step < price <= L
    Formula: L = range_high - floor((range_high - price) / step) * step
    Returns the level price if within grid bounds [range_low, range_high], else None.
    """
    if step <= 0 or price > range_high + 1e-4:
        return None

    k = math.floor((range_high - price + 1e-7) / step)
    level_price = round(range_high - (k * step), 4)

    if level_price < range_low - 1e-4:
        return None

    return level_price


def get_lot_size(instrument: str) -> int:
    """Returns standard lot units for instruments."""
    inst = instrument.upper()
    if "SENSEX" in inst or "BSX" in inst:
        return 20
    elif "BANKNIFTY" in inst:
        return 15
    elif "FINNIFTY" in inst:
        return 25
    return 65  # NIFTY default


def recenter_range(
    current_price: float,
    original_span: float,
    slice_interval: float,
    original_range_high: Optional[float] = None,
    original_range_low: Optional[float] = None,
    range_points: Optional[float] = None,
    slicer_count: Optional[int] = None,
) -> Tuple[float, float]:
    """
    Re-anchors the ladder band around current_price after a loss-exit.
    If current_price is already within [original_range_low, original_range_high], keeps the original band.
    Otherwise, centers the band on current_price and aligns range_high to the nearest slice_interval step.
    When range_points and slicer_count are supplied, dynamically re-derives the band magnitude and step.
    """
    span = range_points if (range_points is not None and range_points > 0) else original_span
    step = (
        round(range_points / slicer_count, 4)
        if (range_points is not None and slicer_count is not None and slicer_count >= 1)
        else slice_interval
    )

    if (
        original_range_high is not None
        and original_range_low is not None
        and original_range_low <= current_price <= original_range_high
    ):
        return round(original_range_high, 2), round(original_range_low, 2)

    half_span = span / 2.0
    raw_high = current_price + half_span

    # Align raw_high to nearest slice_interval tick using integer arithmetic
    step_int = int(round(step * 100))
    if step_int <= 0:
        step_int = 100
    raw_high_int = int(round(raw_high * 100))
    aligned_high_int = int(round(raw_high_int / step_int)) * step_int

    new_range_high = round(aligned_high_int / 100.0, 2)
    new_range_low = round(new_range_high - span, 2)
    return new_range_high, new_range_low


def validate_config(cfg: "ScalperRunConfig") -> None:
    """Strict configuration validator. Fails fast with clear ValueError."""
    if cfg.range_low >= cfg.range_high:
        raise ValueError(f"range_low ({cfg.range_low}) must be strictly less than range_high ({cfg.range_high}).")
    if cfg.range_points is not None and cfg.range_points <= 0:
        raise ValueError(f"range_points ({cfg.range_points}) must be strictly greater than 0.")
    if cfg.slicer_count is not None and cfg.slicer_count < 1:
        raise ValueError(f"slicer_count ({cfg.slicer_count}) must be an integer >= 1.")
    if cfg.slice_interval <= 0:
        raise ValueError(f"slice_interval ({cfg.slice_interval}) must be greater than 0.")
    if cfg.profit_point <= 0:
        raise ValueError(f"profit_point ({cfg.profit_point}) must be greater than 0.")
    if cfg.loss_point <= 0:
        raise ValueError(f"loss_point ({cfg.loss_point}) must be greater than 0.")
    if cfg.qty_per_slice_lots <= 0:
        raise ValueError(f"qty_per_slice_lots ({cfg.qty_per_slice_lots}) must be at least 1.")

    span = round(cfg.range_high - cfg.range_low, 4)
    step = round(cfg.slice_interval, 4)
    remainder = round(span % step, 4)
    if remainder > 1e-4 and round(step - remainder, 4) > 1e-4:
        raise ValueError(
            f"Range span ({span}) must be evenly divisible by slice_interval ({step}). Remainder: {remainder}"
        )


@dataclass
class Slice:
    level_price: float
    status: SliceStatus = SliceStatus.UNUSED
    label: Optional[str] = None
    quantity: int = 65
    profit_point: float = 8.0
    profit_target: Optional[float] = None
    sl_price: Optional[float] = None
    loss_trigger: Optional[float] = None
    fill_price: Optional[float] = None
    filled_at: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[SliceExitReason] = None
    exited_at: Optional[str] = None
    order_id: Optional[str] = None
    pnl_points: Optional[float] = None
    pnl_rupees: Optional[float] = None

    def mark_used(self) -> None:
        """
        Marks grid level as USED after a position has been closed / exited,
        ensuring one-trade-per-level rule so it will not be re-bought within the same run/cycle.
        """
        self.status = SliceStatus.USED
        self.order_id = None

    def reset_to_unused(self) -> None:
        """
        Resets slice state back to UNUSED on ladder re-centering or reset.
        """
        self.status = SliceStatus.UNUSED
        self.label = None
        self.fill_price = None
        self.filled_at = None
        self.profit_target = None
        self.exit_price = None
        self.exit_reason = None
        self.exited_at = None
        self.order_id = None
        self.pnl_points = None
        self.pnl_rupees = None

    def reset_to_pending(self) -> None:
        """Backward compatibility alias for reset_to_unused."""
        self.reset_to_unused()


@dataclass
class ScalperRunConfig:
    run_id: str = "run01"
    instrument_name: str = "NIFTY"
    option_type: str = "CE"
    strike: int = 24250
    expiry: str = "25AUG2026"
    range_points: Optional[float] = None
    slicer_count: Optional[int] = None
    slice_interval: float = 8.0
    range_high: float = 140.0
    range_low: float = 100.0
    qty_per_slice_lots: int = 1
    profit_point: float = 8.0
    loss_point: float = 8.0
    poll_interval_seconds: float = 2.0
    spot_ltp: Optional[float] = 24250.0
    contract_symbol: Optional[str] = None
    contract_token: Optional[str] = None
    lock_strike_on_entry: bool = True
    auto_restrike: bool = False
    flicker_guard_ticks: int = 3
    flicker_guard_seconds: float = 2.0
    gap_fill_mode: str = "all_crossed"  # "all_crossed" | "single" | "skip_and_log"
    cutoff_time_ist: str = "15:22"
    auto_eod_squareoff: bool = False
    strike_source: str = "auto"  # "auto" | "manual"
    trading_mode: str = "paper"  # "paper" | "live"

    def __post_init__(self):
        if self.contract_symbol is None:
            self.contract_symbol = f"{self.instrument_name} {self.strike} {self.option_type}"
        if self.range_points is not None and self.slicer_count is not None:
            if self.range_points <= 0:
                raise ValueError(f"range_points ({self.range_points}) must be strictly greater than 0.")
            if self.slicer_count < 1:
                raise ValueError(f"slicer_count ({self.slicer_count}) must be an integer >= 1.")
            self.slicer_count = int(self.slicer_count)
            self.slice_interval = round(self.range_points / self.slicer_count, 4)
            if self.range_high is not None:
                self.range_low = round(self.range_high - self.range_points, 4)
        else:
            self.range_points = round(self.range_high - self.range_low, 4)
            if self.slice_interval > 0:
                self.slicer_count = max(1, int(round((self.range_high - self.range_low) / self.slice_interval)))
            else:
                self.slicer_count = 1
        validate_config(self)

    @property
    def lot_size(self) -> int:
        return get_lot_size(self.instrument_name)

    @property
    def total_slice_quantity(self) -> int:
        return self.qty_per_slice_lots * self.lot_size

    @property
    def original_span(self) -> float:
        return round(self.range_high - self.range_low, 4)


@dataclass
class TradeRecord:
    trade_id: str
    run_id: str
    label: str
    level_price: float
    fill_price: float
    exit_price: float
    quantity: int
    pnl_points: float
    pnl_rupees: float
    exit_reason: str
    filled_at: str
    exited_at: str
    contract_symbol: str
    entry_time: Optional[str] = None
    exit_time: Optional[str] = None
    instrument: Optional[str] = None
    symbol: Optional[str] = None
    run_number: int = 1
    mode: str = "paper"
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None

    def __post_init__(self):
        if self.entry_time is None:
            self.entry_time = self.filled_at
        if self.exit_time is None:
            self.exit_time = self.exited_at
        if self.instrument is None:
            if self.contract_symbol and "SENSEX" in self.contract_symbol.upper():
                self.instrument = "SENSEX"
            else:
                self.instrument = "NIFTY"
        if self.symbol is None:
            self.symbol = self.instrument


class ScalperRun:
    """
    Independent Scalper Run instance executing the 3-step state machine.
    Zero shared mutable globals. Fully isolated per run_id.
    """

    def __init__(
        self,
        config: ScalperRunConfig,
        trade_callback: Optional[Any] = None,
        order_callback: Optional[Any] = None,
    ):
        self.config = config
        self.run_id = config.run_id
        self.trade_callback = trade_callback
        self.order_callback = order_callback
        self.is_active = False
        self.run_number = 1
        self.cycle_count = 1
        self.label_counter = 0
        self.last_ltp: Optional[float] = None
        self.last_processed_seq: Optional[int] = None
        self.spot_ltp: float = config.spot_ltp or 24250.0
        self.accumulated_realized_pnl: float = 0.0
        self.active_slices: List[Slice] = []
        self.grid_ladder: List[Slice] = []
        self.trade_history: List[TradeRecord] = []
        self.audit_events: List[Dict[str, Any]] = []
        self.feed_status: str = "LIVE"  # "LIVE" | "STALE"
        self.stale_polls_count: int = 0
        self.stale_threshold_polls: int = 5
        self.eod_squared_off: bool = False
        self.last_filled_level: Optional[float] = None

        # Strike locking and flicker guard state per slot
        self.locked_strike: int = config.strike
        self.locked_contract_symbol: Optional[str] = config.contract_symbol
        self.locked_contract_token: Optional[str] = config.contract_token
        self.candidate_strike: Optional[int] = None
        self.candidate_strike_ticks: int = 0
        self.candidate_strike_first_seen: Optional[float] = None

        self._generate_grid()
        self._record_event(
            "INITIAL_RESOLVE",
            0.0,
            f"Run {self.run_id} initialized with contract {self.config.contract_symbol} (Strike: {self.config.strike}, Option: {self.config.option_type})",
            {"strike": self.config.strike, "option_type": self.config.option_type, "contract": self.config.contract_symbol},
        )

    def _dispatch_order(
        self,
        action: str,
        quantity: int,
        price: float,
        order_type: str = "MARKET",
        product_type: str = "INTRADAY",
    ) -> Dict[str, Any]:
        """
        Dispatches order to registered order_callback (e.g. ClientSession.place_order).
        Falls back to local simulation if no callback is registered.
        """
        symbol = self.locked_contract_symbol or self.config.contract_symbol
        token = self.locked_contract_token or self.config.contract_token
        trading_mode = getattr(self.config, "trading_mode", "paper") or "paper"

        if self.order_callback:
            try:
                res = self.order_callback(
                    run_id=self.run_id,
                    action=action,
                    symbol=symbol,
                    token=token,
                    quantity=quantity,
                    price=price,
                    order_type=order_type,
                    product_type=product_type,
                    trading_mode=trading_mode,
                )
                if isinstance(res, dict):
                    return res
            except Exception as e:
                return {"status": "error", "message": str(e), "mode": trading_mode}

        sim_id = f"sim_{uuid.uuid4().hex[:8]}"
        return {
            "status": "success",
            "order_id": sim_id,
            "mode": "paper",
            "fill_price": price,
        }

    def _get_timestamp(self) -> str:
        return datetime.datetime.now(IST).isoformat()

    def _generate_grid(self, custom_high: Optional[float] = None, custom_low: Optional[float] = None) -> None:
        """
        Generates descending ladder levels from range_high down to range_low.
        Computes range-anchored stop-loss trigger: range_stop_loss = range_bottom - loss_trigger.
        Uses exact integer tick arithmetic to eliminate float rounding errors.
        """
        self.grid_ladder.clear()
        high = custom_high if custom_high is not None else self.config.range_high
        low = custom_low if custom_low is not None else self.config.range_low
        step = self.config.slice_interval

        self.effective_range_high = high
        self.effective_range_low = low
        self.range_stop_loss = round(low - self.config.loss_point, 4)

        high_int = int(round(high * 100))
        low_int = int(round(low * 100))
        step_int = int(round(step * 100))

        if step_int <= 0 or high_int < low_int:
            high_int, low_int, step_int = 15000, 10000, 1000

        for lvl_int in range(high_int, low_int - 1, -step_int):
            lvl_price = round(lvl_int / 100.0, 2)
            self.grid_ladder.append(
                Slice(
                    level_price=lvl_price,
                    status=SliceStatus.UNUSED,
                    quantity=self.config.total_slice_quantity,
                    profit_point=self.config.profit_point,
                    sl_price=self.range_stop_loss,
                )
            )

    def start(self) -> None:
        self.is_active = True
        self.eod_squared_off = False
        self.locked_strike = self.config.strike
        self.locked_contract_symbol = self.config.contract_symbol
        self.locked_contract_token = self.config.contract_token
        self.candidate_strike = None
        self.candidate_strike_ticks = 0
        self._record_event(
            "STRIKE_LOCKED",
            self.last_ltp or self.config.range_high,
            f"Scalper Run {self.run_id} started. Locked strike {self.locked_strike} ({self.config.contract_symbol}) for ladder execution.",
            {
                "old_strike": None,
                "new_strike": self.locked_strike,
                "locked_strike": self.locked_strike,
                "contract_symbol": self.config.contract_symbol,
                "contract_token": self.config.contract_token,
                "reason": "ladder_start",
            },
        )
        self._record_event("RUN_STARTED", self.last_ltp or self.config.range_high, f"Scalper Run {self.run_id} started.")

    def stop(self) -> List[TradeRecord]:
        self.is_active = False
        exited = []
        ltp = self.last_ltp if self.last_ltp is not None else self.config.range_high
        if self.active_slices:
            exited = self._exit_all_active_slices(exit_price=ltp, reason=SliceExitReason.MANUAL)
        self._record_event("RUN_STOPPED", ltp, f"Scalper Run {self.run_id} stopped manually.")
        return exited

    def eod_squareoff(self, exit_price: Optional[float] = None) -> List[TradeRecord]:
        """Exits all active slices at market close cutoff."""
        self.is_active = False
        self.eod_squared_off = True
        price = exit_price if exit_price is not None else (self.last_ltp or self.config.range_high)
        exited = []
        if self.active_slices:
            exited = self._exit_all_active_slices(exit_price=price, reason=SliceExitReason.EOD_SQUAREOFF)
        self._record_event(
            "EOD_SQUAREOFF",
            price,
            f"EOD Square-off executed at {price:.2f}. Exited {len(exited)} slices.",
            {"exited_count": len(exited)},
        )
        return exited

    def exit_slice(
        self,
        slice_identifier: Any,
        exit_price: Optional[float] = None,
        reason: SliceExitReason = SliceExitReason.MANUAL,
    ) -> Optional[TradeRecord]:
        """
        Manually exits a specific active slice identified by its order_id, label, or level_price.
        Calculates P&L, records trade in trade_history, marks ladder rung as USED,
        removes slice from active_slices, and updates realized P&L.
        """
        if not self.active_slices:
            return None

        target_slice: Optional[Slice] = None
        ident_str = str(slice_identifier).strip() if slice_identifier is not None else ""
        ident_upper = ident_str.upper()

        # 1. Exact match on order_id or label
        for s in self.active_slices:
            if s.order_id and str(s.order_id).strip() == ident_str:
                target_slice = s
                break
            if s.label and str(s.label).strip().upper() == ident_upper:
                target_slice = s
                break

        # 2. Normalized label match (e.g. "Slice A", "Slice-A", "SL A", "SLICE_A" -> matches "A")
        if not target_slice and ident_upper:
            norm_label = re.sub(r'^(SLICE|RUNG|LEVEL|SL|LOT)[\s_\-:]*', '', ident_upper).strip()
            if norm_label:
                for s in self.active_slices:
                    s_label_upper = (s.label or "").strip().upper()
                    if s_label_upper == norm_label or norm_label in s_label_upper or s_label_upper in norm_label:
                        target_slice = s
                        break

        # 3. Numeric price / level match
        if not target_slice and ident_str:
            float_val: Optional[float] = None
            try:
                float_val = float(re.sub(r'[^\d.]', '', ident_str))
            except (ValueError, TypeError):
                pass

            if float_val is not None:
                for s in self.active_slices:
                    if abs(s.level_price - float_val) < 0.05 or (s.fill_price is not None and abs(s.fill_price - float_val) < 0.05):
                        target_slice = s
                        break

        # 4. Keyword / explicit index matching
        if not target_slice:
            if ident_upper in ("LOWEST", "FIRST", "0"):
                target_slice = min(self.active_slices, key=lambda s: s.level_price)
            elif ident_upper in ("HIGHEST", "LAST"):
                target_slice = max(self.active_slices, key=lambda s: s.level_price)
            elif ident_upper in ("ANY", "ALL", "DEFAULT") or not ident_str:
                target_slice = self.active_slices[0]

        if not target_slice:
            return None

        price = exit_price if exit_price is not None else (self.last_ltp or target_slice.fill_price or target_slice.level_price)
        fill = target_slice.fill_price if target_slice.fill_price is not None else target_slice.level_price
        pnl_pts = round(price - fill, 4)
        pnl_rup = round(pnl_pts * target_slice.quantity, 4)
        timestamp = self._get_timestamp()

        # Dispatch exit SELL order to broker
        exit_res = self._dispatch_order(
            action="SELL",
            quantity=target_slice.quantity,
            price=price,
            order_type="MARKET",
        )
        exit_order_id = str(exit_res.get("order_id") or f"ORD-SELL-{uuid.uuid4().hex[:8].upper()}")

        target_slice.status = SliceStatus.EXITED
        target_slice.exit_price = price
        target_slice.exit_reason = reason
        target_slice.exited_at = timestamp
        target_slice.pnl_points = pnl_pts
        target_slice.pnl_rupees = pnl_rup

        self.accumulated_realized_pnl = round(self.accumulated_realized_pnl + pnl_rup, 4)

        rec = TradeRecord(
            trade_id=f"TRD-{uuid.uuid4().hex[:8].upper()}",
            run_id=self.run_id,
            label=target_slice.label or "MANUAL",
            level_price=target_slice.level_price,
            fill_price=fill,
            exit_price=price,
            quantity=target_slice.quantity,
            pnl_points=pnl_pts,
            pnl_rupees=pnl_rup,
            exit_reason=reason.value,
            filled_at=target_slice.filled_at or timestamp,
            exited_at=timestamp,
            contract_symbol=self.config.contract_symbol,
            entry_time=target_slice.filled_at or timestamp,
            exit_time=timestamp,
            instrument=self.config.instrument_name,
            symbol=self.config.instrument_name,
            run_number=self.run_number,
            mode=getattr(self.config, "trading_mode", "paper") or "paper",
            entry_order_id=target_slice.order_id,
            exit_order_id=exit_order_id,
        )
        self._record_trade(rec)

        if target_slice in self.active_slices:
            self.active_slices.remove(target_slice)

        # Mark corresponding level in grid_ladder as USED
        for grid_slice in self.grid_ladder:
            if abs(grid_slice.level_price - target_slice.level_price) < 1e-4:
                grid_slice.mark_used()
                break

        self._record_event(
            "MANUAL_SLICE_EXIT",
            price,
            f"Slice {target_slice.label or target_slice.order_id} manually exited at {price:.2f} (P&L: {'+' if pnl_pts >= 0 else ''}{pnl_pts:.2f} pts / {'+' if pnl_rup >= 0 else ''}₹{pnl_rup:.2f}). Level {target_slice.level_price:.2f} marked USED.",
            {"trade": asdict(rec), "slice_identifier": slice_identifier},
        )
        return rec

    def check_eod_cutoff(self, current_time_ist: Optional[datetime.time] = None) -> bool:
        """
        If auto_eod_squareoff is enabled and current IST time >= cutoff_time_ist,
        triggers automatic EOD square-off.
        """
        if not self.config.auto_eod_squareoff or self.eod_squared_off:
            return False

        # Square off if run is active OR if any open slices/positions remain
        if not self.is_active and len(self.active_slices) == 0:
            return False

        if current_time_ist is None:
            # Indian Standard Time (UTC+5:30)
            ist_now = datetime.datetime.now(IST)
            current_time_ist = ist_now.time()

        try:
            cutoff_parts = [int(p) for p in self.config.cutoff_time_ist.split(":")]
            cutoff_t = datetime.time(cutoff_parts[0], cutoff_parts[1])
            if current_time_ist >= cutoff_t:
                self.eod_squareoff(exit_price=self.last_ltp)
                return True
        except Exception:
            pass
        return False

    def update_feed_health(self, is_valid: bool) -> None:
        """Tracks consecutive feed errors / missing ticks to flag STALE status."""
        if is_valid:
            if self.feed_status == "STALE":
                self._record_event("FEED_RECOVERED", self.last_ltp or 0.0, "Angel One live feed recovered to LIVE status.")
            self.feed_status = "LIVE"
            self.stale_polls_count = 0
        else:
            self.stale_polls_count += 1
            if self.stale_polls_count >= self.stale_threshold_polls and self.feed_status != "STALE":
                self.feed_status = "STALE"
                self._record_event(
                    "FEED_STALE",
                    self.last_ltp or 0.0,
                    f"Live feed stale after {self.stale_polls_count} missing polls. Pausing new BUY entries.",
                )

    def handle_spot_update(self, new_spot: float, current_time: Optional[float] = None) -> Optional[int]:
        """
        Handles spot update with strict strike locking and flicker guard protection.
        1. If positions (slices) are open on this slot, strike is strictly locked: ignores all spot flicker.
        2. If slot is flat (no open slices) and auto_restrike is enabled:
           Applies a flicker guard requiring candidate strike to remain consistent for
           flicker_guard_ticks consecutive ticks before accepting and relocking.
        3. If idle/setup mode:
           Updates strike cleanly on spot movement.
        4. Logs every strike lock / relock event with old_strike, new_strike, timestamp, reason.
        """
        self.spot_ltp = new_spot
        new_strike = calculate_nearest_strike(new_spot, self.config.instrument_name)

        # If strike was manually chosen, keep it pinned and never overwrite with auto-calculated ATM strike
        if getattr(self.config, "strike_source", "auto") == "manual":
            self.candidate_strike = None
            self.candidate_strike_ticks = 0
            return self.locked_strike or self.config.strike

        # If active and auto_restrike is False, strike is permanently locked for the run
        if self.is_active and not self.config.auto_restrike:
            self.candidate_strike = None
            self.candidate_strike_ticks = 0
            return self.locked_strike or self.config.strike

        # If active positions (slices) are open, strike MUST remain strictly locked!
        if len(self.active_slices) > 0:
            self.candidate_strike = None
            self.candidate_strike_ticks = 0
            return self.locked_strike or self.config.strike

        # At this point, slot is flat (if active and auto_restrike=True) or inactive/idle
        current_active_strike = self.locked_strike if (self.is_active and self.locked_strike is not None) else self.config.strike

        if new_strike == current_active_strike:
            # Spot is within current strike boundary: reset candidate tracker
            self.candidate_strike = None
            self.candidate_strike_ticks = 0
            return current_active_strike

        # Candidate strike differs from active strike:
        if self.is_active:
            # Active flat state: evaluate flicker guard
            guard_ticks = max(1, getattr(self.config, "flicker_guard_ticks", 3))

            if self.candidate_strike != new_strike:
                # First tick seeing this new candidate strike: initialize guard
                self.candidate_strike = new_strike
                self.candidate_strike_ticks = 1
                self.candidate_strike_first_seen = current_time or datetime.datetime.now(IST).timestamp()
            else:
                # Consecutive tick seeing the same candidate strike
                self.candidate_strike_ticks += 1

            # Check if flicker guard threshold is satisfied
            if self.candidate_strike_ticks >= guard_ticks:
                old_strike = current_active_strike
                self.config.strike = new_strike
                self.locked_strike = new_strike
                self.config.contract_symbol = f"{self.config.instrument_name} {new_strike} {self.config.option_type}"
                self.locked_contract_symbol = self.config.contract_symbol

                reason = "flat_ladder_restrike"
                self._record_event(
                    "STRIKE_RELOCKED",
                    self.last_ltp or 0.0,
                    f"Strike relocked from {old_strike} to {new_strike} after {self.candidate_strike_ticks} consecutive ticks at Spot {new_spot:.2f} (Reason: {reason}).",
                    {
                        "old_strike": old_strike,
                        "new_strike": new_strike,
                        "locked_strike": new_strike,
                        "spot": new_spot,
                        "reason": reason,
                        "consecutive_ticks": self.candidate_strike_ticks,
                    },
                )

                # Reset candidate tracker after successful lock
                self.candidate_strike = None
                self.candidate_strike_ticks = 0
                return new_strike

            # Flicker guard not yet satisfied: remain on currently active locked strike
            return current_active_strike
        else:
            # Inactive/setup mode: update setup strike immediately
            old_strike = self.config.strike
            self.config.strike = new_strike
            self.locked_strike = new_strike
            self.config.contract_symbol = f"{self.config.instrument_name} {new_strike} {self.config.option_type}"
            self.locked_contract_symbol = self.config.contract_symbol
            self.candidate_strike = None
            self.candidate_strike_ticks = 0
            return new_strike

    def get_lowest_filled_slice(self) -> Optional[Slice]:
        if not self.active_slices:
            return None
        return min(self.active_slices, key=lambda s: s.level_price)


    def get_range_stop_loss(self) -> float:
        """Returns the range-anchored stop loss: range_bottom - loss_trigger."""
        if not hasattr(self, "range_stop_loss") or self.range_stop_loss is None:
            low = getattr(self, "effective_range_low", self.config.range_low)
            self.range_stop_loss = round(low - self.config.loss_point, 4)
        return self.range_stop_loss

    def get_loss_trigger_price(self) -> Optional[float]:
        """
        Returns the range-anchored stop-loss trigger shared by all positions opened within this range.
        Formula: range_stop_loss = range_bottom - loss_trigger
        """
        return self.get_range_stop_loss()

    def clear_history(self) -> None:
        """Clears all completed trade history and resets accumulated realized P&L."""
        self.trade_history.clear()
        self.accumulated_realized_pnl = 0.0
        self._record_event(
            "HISTORY_CLEARED",
            self.last_ltp or 0.0,
            f"Trade history cleared and realized P&L reset to ₹0.00 for Run {self.run_id}.",
        )

    def _record_event(self, event_type: str, price: float, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        self.audit_events.insert(0, {
            "timestamp": self._get_timestamp(),
            "run_id": self.run_id,
            "event_type": event_type,
            "price": round(price, 2) if price else 0.0,
            "message": message,
            "details": details or {},
        })
        if len(self.audit_events) > 150:
            self.audit_events = self.audit_events[:150]

    def _record_trade(self, trade: TradeRecord) -> None:
        """Records completed trade in trade_history and dispatches to registered callback (e.g. MongoDB)."""
        self.trade_history.insert(0, trade)
        if self.trade_callback:
            try:
                self.trade_callback(asdict(trade), asdict(self.config))
            except Exception:
                pass

    def _exit_all_active_slices(self, exit_price: float, reason: SliceExitReason) -> List[TradeRecord]:
        timestamp = self._get_timestamp()
        exited_trades = []

        for s in list(self.active_slices):
            fill = s.fill_price if s.fill_price is not None else s.level_price
            pnl_pts = round(exit_price - fill, 4)
            pnl_rup = round(pnl_pts * s.quantity, 4)

            # Dispatch exit SELL order to broker
            exit_res = self._dispatch_order(
                action="SELL",
                quantity=s.quantity,
                price=exit_price,
                order_type="MARKET",
            )
            exit_order_id = str(exit_res.get("order_id") or f"ORD-SELL-{uuid.uuid4().hex[:8].upper()}")

            s.status = SliceStatus.EXITED
            s.exit_price = exit_price
            s.exit_reason = reason
            s.exited_at = timestamp
            s.pnl_points = pnl_pts
            s.pnl_rupees = pnl_rup

            self.accumulated_realized_pnl = round(self.accumulated_realized_pnl + pnl_rup, 4)

            rec = TradeRecord(
                trade_id=f"TRD-{uuid.uuid4().hex[:8].upper()}",
                run_id=self.run_id,
                label=s.label or "X",
                level_price=s.level_price,
                fill_price=fill,
                exit_price=exit_price,
                quantity=s.quantity,
                pnl_points=pnl_pts,
                pnl_rupees=pnl_rup,
                exit_reason=reason.value,
                filled_at=s.filled_at or timestamp,
                exited_at=timestamp,
                contract_symbol=self.config.contract_symbol,
                entry_time=s.filled_at or timestamp,
                exit_time=timestamp,
                instrument=self.config.instrument_name,
                symbol=self.config.instrument_name,
                run_number=self.run_number,
                mode=getattr(self.config, "trading_mode", "paper") or "paper",
                entry_order_id=s.order_id,
                exit_order_id=exit_order_id,
            )
            self._record_trade(rec)
            exited_trades.append(rec)

        self.active_slices.clear()
        return exited_trades

    def level_for_price(self, price: float) -> Optional[float]:
        """
        Calculates the exact grid rung L for a given price based on price-range membership:
        L is the level such that: L - step < price <= L
        Formula: L = range_high - floor((range_high - price) / step) * step
        Returns the level price if within grid bounds [range_low, range_high], else None.
        """
        if not self.grid_ladder:
            return None
        r_high = self.grid_ladder[0].level_price
        r_low = self.grid_ladder[-1].level_price
        step = self.config.slice_interval

        calculated = calculate_level_for_price(price, r_high, r_low, step)
        if calculated is None:
            return None
        # Snap to exact slice object in grid_ladder
        for s in self.grid_ladder:
            if abs(s.level_price - calculated) < 1e-3:
                return s.level_price
        return calculated

    def clear_history(self) -> None:
        """Clears all completed trade history and resets accumulated realized P&L."""
        self.trade_history.clear()
        self.accumulated_realized_pnl = 0.0
        self._record_event(
            "HISTORY_CLEARED",
            self.last_ltp or 0.0,
            f"Trade history cleared and realized P&L reset to ₹0.00 for Run {self.run_id}.",
            {},
        )

    def process_tick(
        self,
        current_price: float,
        seq_num: Optional[int] = None,
        current_time_ist: Optional[datetime.time] = None,
    ) -> Dict[str, Any]:
        """
        Authoritative 3-Step State Machine execution on every market tick.
        Step 1: Loss Backstop (Stops tick if hit)
        Step 2: Profit Target Sell (Evaluates already-filled slices only)
        Step 3: Grid Buy (Price-range level matching: L - step < current_price <= L)
        """
        # Monotonic sequence deduplication
        if seq_num is not None:
            if self.last_processed_seq is not None and seq_num <= self.last_processed_seq:
                return {"status": "dedup_ignored", "seq_num": seq_num, "tick_price": current_price}
            self.last_processed_seq = seq_num

        current_price = round(current_price, 4)
        self.last_ltp = current_price
        actions = {
            "tick_price": current_price,
            "seq_num": seq_num,
            "buys": [],
            "sells": [],
            "loss_exit": False,
            "cycle_reset": False,
            "gap_skipped": [],
        }

        # If run is not active / running, only update last_ltp without executing trade state machine
        if not self.is_active:
            return actions

        # Check automated EOD cutoff if enabled
        if self.config.auto_eod_squareoff and not self.eod_squared_off:
            if self.check_eod_cutoff(current_time_ist=current_time_ist):
                actions["loss_exit"] = False
                actions["eod_squareoff"] = True
                return actions

        # -----------------------------------------------------------------
        # STEP 1: LOSS BACKSTOP TRIGGER (Range-Anchored)
        # Stops the scalper run cleanly without auto-starting a new ladder.
        # -----------------------------------------------------------------
        loss_trigger = self.get_loss_trigger_price()
        # Compare with float tolerance (1e-4)
        if loss_trigger is not None and current_price <= loss_trigger + 1e-4 and len(self.active_slices) > 0:
            exited = self._exit_all_active_slices(exit_price=current_price, reason=SliceExitReason.LOSS_EXIT)
            self.is_active = False
            actions["loss_exit"] = True
            actions["cycle_reset"] = False

            self._record_event(
                "LOSS_EXIT_ALL",
                current_price,
                f"Range-anchored stop loss triggered at {current_price:.2f} (Stop-Loss: {loss_trigger:.2f}). Exited {len(exited)} slices. Slicing run stopped.",
                {
                    "exited_count": len(exited),
                    "loss_trigger": loss_trigger,
                    "range_stop_loss": loss_trigger,
                },
            )
            return actions

        # -----------------------------------------------------------------
        # STEP 2: PROFIT TARGET SELL & CONTINUOUS RE-ENTRY RESET
        # Evaluated for currently filled slices before any new buy in Step 3
        # -----------------------------------------------------------------
        if self.active_slices:
            # Snapshot current active slices so newly added slices can never be evaluated on this tick
            existing_active = list(self.active_slices)
            for s in existing_active:
                if s.profit_target is not None and current_price >= s.profit_target - 1e-4:
                    fill = s.fill_price if s.fill_price is not None else s.level_price
                    pnl_pts = round(current_price - fill, 4)
                    pnl_rup = round(pnl_pts * s.quantity, 4)
                    timestamp = self._get_timestamp()

                    # Dispatch exit SELL order to broker
                    exit_res = self._dispatch_order(
                        action="SELL",
                        quantity=s.quantity,
                        price=current_price,
                        order_type="MARKET",
                    )
                    exit_order_id = str(exit_res.get("order_id") or f"ORD-SELL-{uuid.uuid4().hex[:8].upper()}")

                    self.accumulated_realized_pnl = round(self.accumulated_realized_pnl + pnl_rup, 4)

                    rec = TradeRecord(
                        trade_id=f"TRD-{uuid.uuid4().hex[:8].upper()}",
                        run_id=self.run_id,
                        label=s.label or "A",
                        level_price=s.level_price,
                        fill_price=fill,
                        exit_price=current_price,
                        quantity=s.quantity,
                        pnl_points=pnl_pts,
                        pnl_rupees=pnl_rup,
                        exit_reason=SliceExitReason.PROFIT_TARGET.value,
                        filled_at=s.filled_at or timestamp,
                        exited_at=timestamp,
                        contract_symbol=self.config.contract_symbol,
                        entry_time=s.filled_at or timestamp,
                        exit_time=timestamp,
                        instrument=self.config.instrument_name,
                        symbol=self.config.instrument_name,
                        run_number=self.run_number,
                        mode=getattr(self.config, "trading_mode", "paper") or "paper",
                        entry_order_id=s.order_id,
                        exit_order_id=exit_order_id,
                    )
                    self._record_trade(rec)
                    actions["sells"].append(asdict(rec))

                    self._record_event(
                        "SLICE_PROFIT_SELL",
                        current_price,
                        f"Slice {s.label} (@ {fill:.2f}) sold at {current_price:.2f} (Target: {s.profit_target:.2f}, P&L: +{pnl_pts:.2f} pts / +₹{pnl_rup:.2f}). Level {s.level_price:.2f} marked USED.",
                        {"trade": asdict(rec)},
                    )

                    # Remove from active slices
                    if s in self.active_slices:
                        self.active_slices.remove(s)

                    # Mark corresponding level in ladder as USED for one-trade-per-level enforcement
                    for grid_slice in self.grid_ladder:
                        if abs(grid_slice.level_price - s.level_price) < 1e-4:
                            grid_slice.mark_used()
                            break

        # -----------------------------------------------------------------
        # STEP 3: GRID BUY TRIGGER (Crossing-Based Upper Level & Descending Step-Down)
        # -----------------------------------------------------------------
        # Check feed health and EOD cutoff: if stale or EOD squared off, skip buys
        if self.feed_status == "STALE" or self.eod_squared_off:
            return actions

        matched_slice: Optional[Slice] = None
        is_crossing_buy = False
        prev_last_filled = self.last_filled_level

        traded_levels = [s.level_price for s in self.grid_ladder if s.status in (SliceStatus.FILLED, SliceStatus.USED)]
        highest_traded = max(traded_levels) if traded_levels else None
        lowest_active = min((s.level_price for s in self.active_slices), default=None)

        # Case 3A: Upward Crossing Buy Trigger
        # ONLY triggers when current_price rises strictly above the HIGHEST ever filled/used level in this ladder cycle
        if highest_traded is not None and current_price > highest_traded + 1e-4:
            unused_upper = [
                s for s in self.grid_ladder
                if s.status in (SliceStatus.UNUSED, SliceStatus.PENDING) and s.level_price > highest_traded + 1e-4
            ]
            if unused_upper:
                # Select the single next unused level directly above highest traded level
                matched_slice = min(unused_upper, key=lambda s: s.level_price)
                is_crossing_buy = True

        # Case 3B: Descending Step-Down Buy Trigger
        # When active positions exist, ONLY triggers when price steps down below lowest active position by >= 1 interval
        elif lowest_active is not None:
            if current_price <= (lowest_active - self.config.slice_interval + 1e-4):
                target_level_price = self.level_for_price(current_price)
                if target_level_price is not None and target_level_price <= (lowest_active - self.config.slice_interval + 1e-4):
                    candidate = next((s for s in self.grid_ladder if abs(s.level_price - target_level_price) < 1e-3), None)
                    if candidate and candidate.status in (SliceStatus.UNUSED, SliceStatus.PENDING):
                        matched_slice = candidate

        # Case 3C: When no active positions exist (startup or after all active positions exited)
        elif lowest_active is None:
            target_level_price = self.level_for_price(current_price)
            if target_level_price is not None:
                candidate = next((s for s in self.grid_ladder if abs(s.level_price - target_level_price) < 1e-3), None)
                if candidate and candidate.status in (SliceStatus.UNUSED, SliceStatus.PENDING):
                    matched_slice = candidate

        # Execute Buy Fill if a slice was matched
        if matched_slice and matched_slice.status in (SliceStatus.UNUSED, SliceStatus.PENDING):
            skipped = [
                s for s in self.grid_ladder
                if s.status in (SliceStatus.UNUSED, SliceStatus.PENDING) and s.level_price > matched_slice.level_price + 1e-4
            ]

            fill_price = current_price

            # Dispatch entry BUY order to broker
            order_res = self._dispatch_order(
                action="BUY",
                quantity=self.config.total_slice_quantity,
                price=fill_price,
                order_type="MARKET",
            )

            # In LIVE mode, hard fail-closed if order was rejected by broker
            trading_mode = getattr(self.config, "trading_mode", "paper") or "paper"
            if trading_mode == "live" and order_res.get("status") != "success":
                err_msg = order_res.get("message") or "Broker rejected live BUY order"
                self._record_event(
                    "LIVE_BUY_REJECTED",
                    fill_price,
                    f"Live BUY order rejected by Angel One: {err_msg}. Slice at level {matched_slice.level_price:.2f} was not filled.",
                    {"error": err_msg, "order_res": order_res, "level_price": matched_slice.level_price},
                )
                return actions

            label = get_sequential_label(self.label_counter)
            self.label_counter += 1
            order_id = str(order_res.get("order_id") or f"ORD-BUY-{uuid.uuid4().hex[:8].upper()}")
            timestamp = self._get_timestamp()
            profit_target = round(fill_price + self.config.profit_point, 4)

            matched_slice.status = SliceStatus.FILLED
            matched_slice.label = label
            matched_slice.order_id = order_id
            matched_slice.fill_price = fill_price
            matched_slice.filled_at = timestamp
            matched_slice.profit_target = profit_target
            matched_slice.sl_price = self.get_range_stop_loss()
            matched_slice.quantity = self.config.total_slice_quantity

            self.active_slices.append(matched_slice)
            self.last_filled_level = matched_slice.level_price

            buy_info = {
                "order_id": order_id,
                "label": label,
                "level_price": matched_slice.level_price,
                "fill_price": fill_price,
                "profit_target": profit_target,
                "sl_price": self.get_range_stop_loss(),
                "quantity": matched_slice.quantity,
                "is_crossing_buy": is_crossing_buy,
            }
            actions["buys"].append(buy_info)

            if is_crossing_buy:
                self._record_event(
                    "UPWARD_CROSSING_BUY",
                    fill_price,
                    f"Slice {label} bought via upward crossing at {fill_price:.2f} [Level {matched_slice.level_price:.2f}] (Crossed above {prev_last_filled:.2f} -> Target: {profit_target:.2f}, Qty: {matched_slice.quantity})",
                    buy_info,
                )
            else:
                self._record_event(
                    "SLICE_BUY",
                    fill_price,
                    f"Slice {label} bought at {fill_price:.2f} [Level {matched_slice.level_price:.2f}] (Target: {profit_target:.2f}, Qty: {matched_slice.quantity})",
                    buy_info,
                )

            if skipped and self.config.gap_fill_mode == GapFillMode.SKIP_AND_LOG.value:
                skipped_levels = [s.level_price for s in skipped]
                actions["gap_skipped"] = skipped_levels
                self._record_event(
                    "GAP_SKIPPED_LEVELS",
                    current_price,
                    f"Price gap to {current_price:.2f} bypassed {len(skipped)} higher unused ladder levels: {skipped_levels}.",
                    {"skipped_levels": skipped_levels},
                )

        return actions

    def get_status(self) -> Dict[str, Any]:
        """Calculates and returns complete status payload for this run slot."""
        active_qty = sum(s.quantity for s in self.active_slices)
        active_filled_count = len(self.active_slices)

        if self.active_slices:
            total_cost = sum((s.fill_price or s.level_price) * s.quantity for s in self.active_slices)
            active_avg_price = round(total_cost / active_qty, 2) if active_qty > 0 else 0.0
        else:
            active_avg_price = 0.0

        unrealized_pnl_rupees = 0.0
        unrealized_pnl_points = 0.0
        if self.last_ltp is not None and self.active_slices:
            for s in self.active_slices:
                fill = s.fill_price if s.fill_price is not None else s.level_price
                pts = self.last_ltp - fill
                unrealized_pnl_points += pts
                unrealized_pnl_rupees += pts * s.quantity

        unrealized_pnl_rupees = round(unrealized_pnl_rupees, 2)
        unrealized_pnl_points = round(unrealized_pnl_points, 2)
        total_pnl = round(self.accumulated_realized_pnl + unrealized_pnl_rupees, 2)
        loss_trigger = self.get_loss_trigger_price()

        today_date = datetime.datetime.now(IST).date()
        today_realized_pnl = 0.0
        for t in self.trade_history:
            exit_ts = t.exit_time or t.exited_at or t.filled_at
            if exit_ts:
                try:
                    dt = datetime.datetime.fromisoformat(str(exit_ts))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=IST)
                    else:
                        dt = dt.astimezone(IST)
                    if dt.date() == today_date:
                        today_realized_pnl += t.pnl_rupees
                except Exception:
                    pass
        today_realized_pnl = round(today_realized_pnl, 2)

        return {
            "run_id": self.run_id,
            "run_number": getattr(self, "run_number", 1),
            "is_active": self.is_active,
            "cycle_count": self.cycle_count,
            "contract_symbol": self.config.contract_symbol or f"{self.config.instrument_name} {self.config.strike} {self.config.option_type}",
            "instrument_name": self.config.instrument_name,
            "option_type": self.config.option_type,
            "strike": self.config.strike,
            "strike_source": getattr(self.config, "strike_source", "auto"),
            "locked_strike": getattr(self, "locked_strike", self.config.strike),
            "candidate_strike": getattr(self, "candidate_strike", None),
            "candidate_strike_ticks": getattr(self, "candidate_strike_ticks", 0),
            "expiry": self.config.expiry,
            "spot_ltp": self.spot_ltp,
            "last_ltp": self.last_ltp,
            "feed_status": self.feed_status,
            "active_quantity": active_qty,
            "active_filled_count": active_filled_count,
            "active_avg_price": active_avg_price,
            "loss_trigger_price": loss_trigger,
            "range_stop_loss": self.get_range_stop_loss(),
            "range_bottom": getattr(self, "effective_range_low", self.config.range_low),
            "range_top": getattr(self, "effective_range_high", self.config.range_high),
            "accumulated_realized_pnl": round(self.accumulated_realized_pnl, 2),
            "gross_realized_pnl": round(self.accumulated_realized_pnl, 2),
            "today_realized_pnl": today_realized_pnl,
            "unrealized_pnl_rupees": unrealized_pnl_rupees,
            "unrealized_pnl_points": unrealized_pnl_points,
            "total_pnl": total_pnl,
            "config": {
                "run_id": self.config.run_id,
                "instrument_name": self.config.instrument_name,
                "option_type": self.config.option_type,
                "strike": self.config.strike,
                "expiry": self.config.expiry,
                "range_points": getattr(self.config, "range_points", None),
                "slicer_count": getattr(self.config, "slicer_count", None),
                "computed_step": round(self.config.range_points / self.config.slicer_count, 4) if (getattr(self.config, "range_points", None) and getattr(self.config, "slicer_count", None)) else self.config.slice_interval,
                "slice_interval": self.config.slice_interval,
                "range_high": self.config.range_high,
                "range_low": self.config.range_low,
                "qty_per_slice_lots": self.config.qty_per_slice_lots,
                "profit_point": self.config.profit_point,
                "loss_point": self.config.loss_point,
                "poll_interval_seconds": self.config.poll_interval_seconds,
                "spot_ltp": self.config.spot_ltp,
                "contract_symbol": self.config.contract_symbol,
                "lock_strike_on_entry": self.config.lock_strike_on_entry,
                "auto_restrike": self.config.auto_restrike,
                "flicker_guard_ticks": getattr(self.config, "flicker_guard_ticks", 3),
                "flicker_guard_seconds": getattr(self.config, "flicker_guard_seconds", 2.0),
                "gap_fill_mode": self.config.gap_fill_mode,
                "cutoff_time_ist": self.config.cutoff_time_ist,
                "auto_eod_squareoff": self.config.auto_eod_squareoff,
                "strike_source": getattr(self.config, "strike_source", "auto"),
            },
            "grid_ladder": [asdict(s) for s in self.grid_ladder],
            "active_slices": [asdict(s) for s in self.active_slices],
            "trade_history": [asdict(t) for t in self.trade_history],
            "audit_events": self.audit_events[:30],
        }

    def to_dict(self) -> Dict[str, Any]:
        """Serializes full run state for persistence."""
        return {
            "run_id": self.run_id,
            "run_number": getattr(self, "run_number", 1),
            "is_active": self.is_active,
            "cycle_count": self.cycle_count,
            "label_counter": self.label_counter,
            "last_ltp": self.last_ltp,
            "last_processed_seq": self.last_processed_seq,
            "spot_ltp": self.spot_ltp,
            "locked_strike": getattr(self, "locked_strike", self.config.strike),
            "candidate_strike": getattr(self, "candidate_strike", None),
            "candidate_strike_ticks": getattr(self, "candidate_strike_ticks", 0),
            "accumulated_realized_pnl": self.accumulated_realized_pnl,
            "feed_status": self.feed_status,
            "eod_squared_off": self.eod_squared_off,
            "effective_range_high": getattr(self, "effective_range_high", self.config.range_high),
            "effective_range_low": getattr(self, "effective_range_low", self.config.range_low),
            "range_stop_loss": getattr(self, "range_stop_loss", round(self.config.range_low - self.config.loss_point, 4)),
            "config": asdict(self.config),
            "grid_ladder": [asdict(s) for s in self.grid_ladder],
            "active_slices": [asdict(s) for s in self.active_slices],
            "trade_history": [asdict(t) for t in self.trade_history],
            "audit_events": self.audit_events,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScalperRun":
        """Restores a ScalperRun instance identically from persisted dictionary."""
        cfg_dict = data["config"]
        cfg = ScalperRunConfig(
            run_id=cfg_dict.get("run_id", "run01"),
            instrument_name=cfg_dict.get("instrument_name", "NIFTY"),
            option_type=cfg_dict.get("option_type", "CE"),
            strike=cfg_dict.get("strike", 24250),
            expiry=cfg_dict.get("expiry", "25AUG2026"),
            range_points=cfg_dict.get("range_points"),
            slicer_count=cfg_dict.get("slicer_count"),
            slice_interval=cfg_dict.get("slice_interval", 8.0),
            range_high=cfg_dict.get("range_high", 140.0),
            range_low=cfg_dict.get("range_low", 100.0),
            qty_per_slice_lots=cfg_dict.get("qty_per_slice_lots", 1),
            profit_point=cfg_dict.get("profit_point", 8.0),
            loss_point=cfg_dict.get("loss_point", 8.0),
            poll_interval_seconds=cfg_dict.get("poll_interval_seconds", 2.0),
            spot_ltp=cfg_dict.get("spot_ltp", 24250.0),
            contract_symbol=cfg_dict.get("contract_symbol"),
            contract_token=cfg_dict.get("contract_token"),
            lock_strike_on_entry=cfg_dict.get("lock_strike_on_entry", True),
            auto_restrike=cfg_dict.get("auto_restrike", False),
            flicker_guard_ticks=cfg_dict.get("flicker_guard_ticks", 3),
            flicker_guard_seconds=cfg_dict.get("flicker_guard_seconds", 2.0),
            gap_fill_mode=cfg_dict.get("gap_fill_mode", "all_crossed"),
            cutoff_time_ist=cfg_dict.get("cutoff_time_ist", "15:22"),
            auto_eod_squareoff=cfg_dict.get("auto_eod_squareoff", True),
            strike_source=cfg_dict.get("strike_source", "auto"),
            trading_mode=cfg_dict.get("trading_mode", "paper"),
        )

        run = cls(cfg)
        run.run_number = data.get("run_number", 1)
        run.is_active = data.get("is_active", False)
        run.cycle_count = data.get("cycle_count", 1)
        run.label_counter = data.get("label_counter", 0)
        run.last_ltp = data.get("last_ltp")
        run.last_processed_seq = data.get("last_processed_seq")
        run.spot_ltp = data.get("spot_ltp", 24250.0)
        run.locked_strike = data.get("locked_strike", cfg.strike)
        run.candidate_strike = data.get("candidate_strike")
        run.candidate_strike_ticks = data.get("candidate_strike_ticks", 0)
        run.accumulated_realized_pnl = data.get("accumulated_realized_pnl", 0.0)
        run.feed_status = data.get("feed_status", "LIVE")
        run.eod_squared_off = data.get("eod_squared_off", False)
        run.effective_range_high = data.get("effective_range_high", cfg.range_high)
        run.effective_range_low = data.get("effective_range_low", cfg.range_low)
        run.range_stop_loss = data.get("range_stop_loss", round(run.effective_range_low - cfg.loss_point, 4))

        run.grid_ladder = [
            Slice(
                level_price=s["level_price"],
                status=SliceStatus(s["status"]),
                label=s.get("label"),
                quantity=s.get("quantity", cfg.total_slice_quantity),
                profit_point=s.get("profit_point", cfg.profit_point),
                profit_target=s.get("profit_target"),
                sl_price=s.get("sl_price", run.range_stop_loss),
                loss_trigger=s.get("loss_trigger"),
                fill_price=s.get("fill_price"),
                filled_at=s.get("filled_at"),
                exit_price=s.get("exit_price"),
                exit_reason=SliceExitReason(s["exit_reason"]) if s.get("exit_reason") else None,
                exited_at=s.get("exited_at"),
                order_id=s.get("order_id"),
                pnl_points=s.get("pnl_points"),
                pnl_rupees=s.get("pnl_rupees"),
            )
            for s in data.get("grid_ladder", [])
        ]

        run.active_slices = [
            Slice(
                level_price=s["level_price"],
                status=SliceStatus(s["status"]),
                label=s.get("label"),
                quantity=s.get("quantity", cfg.total_slice_quantity),
                profit_point=s.get("profit_point", cfg.profit_point),
                profit_target=s.get("profit_target"),
                sl_price=s.get("sl_price", run.range_stop_loss),
                loss_trigger=s.get("loss_trigger"),
                fill_price=s.get("fill_price"),
                filled_at=s.get("filled_at"),
                exit_price=s.get("exit_price"),
                exit_reason=SliceExitReason(s["exit_reason"]) if s.get("exit_reason") else None,
                exited_at=s.get("exited_at"),
                order_id=s.get("order_id"),
                pnl_points=s.get("pnl_points"),
                pnl_rupees=s.get("pnl_rupees"),
            )
            for s in data.get("active_slices", [])
        ]

        run.trade_history = [
            TradeRecord(
                trade_id=t["trade_id"],
                run_id=t["run_id"],
                label=t["label"],
                level_price=t["level_price"],
                fill_price=t["fill_price"],
                exit_price=t["exit_price"],
                quantity=t["quantity"],
                pnl_points=t["pnl_points"],
                pnl_rupees=t["pnl_rupees"],
                exit_reason=t["exit_reason"],
                filled_at=t.get("filled_at") or t.get("entry_time", ""),
                exited_at=t.get("exited_at") or t.get("exit_time", ""),
                contract_symbol=t.get("contract_symbol", cfg.contract_symbol),
                entry_time=t.get("entry_time") or t.get("filled_at", ""),
                exit_time=t.get("exit_time") or t.get("exited_at", ""),
                instrument=t.get("instrument"),
                symbol=t.get("symbol"),
                run_number=t.get("run_number", 1),
                mode=t.get("mode", "paper"),
                entry_order_id=t.get("entry_order_id"),
                exit_order_id=t.get("exit_order_id"),
            )
            for t in data.get("trade_history", [])
        ]

        run.audit_events = data.get("audit_events", [])
        return run


class RunManager:
    """
    Manages up to 4 concurrent independent scalper runs with per-client persistence (Phase 2).
    """

    def __init__(
        self,
        max_runs: int = 10,
        state_file: Optional[str] = None,
        client_id: str = "admin",
        trade_callback: Optional[Any] = None,
        order_callback: Optional[Any] = None,
    ):
        self.max_runs = max_runs
        self.client_id = client_id.strip() if client_id else "admin"
        self.state_file = state_file if state_file is not None else ":memory:"
        self.trade_callback = trade_callback
        self.order_callback = order_callback
        self.runs: Dict[str, ScalperRun] = {}
        self.active_run_id: str = "run01"
        self.master_trade_history: List[Dict[str, Any]] = []

        self.load_state()

        # Ensure all canonical 4 slots (run01, run02, run03, run04) exist
        slot_defaults = [
            ("run01", "NIFTY", "CE", 24200, 140.0, 100.0, 8.0, 8.0, 8.0, 40.0, 5),
            ("run02", "SENSEX", "CE", 81000, 250.0, 100.0, 30.0, 30.0, 30.0, 150.0, 5),
            ("run03", "NIFTY", "PE", 24200, 140.0, 100.0, 8.0, 8.0, 8.0, 40.0, 5),
            ("run04", "SENSEX", "PE", 81000, 250.0, 100.0, 30.0, 30.0, 30.0, 150.0, 5),
        ]
        for run_id, inst, otype, strike, r_high, r_low, step, profit, loss, r_pts, s_cnt in slot_defaults:
            if run_id not in self.runs:
                cfg = ScalperRunConfig(
                    run_id=run_id,
                    instrument_name=inst,
                    option_type=otype,
                    strike=strike,
                    range_points=r_pts,
                    slicer_count=s_cnt,
                    range_high=r_high,
                    range_low=r_low,
                    slice_interval=step,
                    profit_point=profit,
                    loss_point=loss,
                    qty_per_slice_lots=1,
                    contract_symbol=f"{inst} {strike} {otype}",
                    auto_eod_squareoff=True,
                    cutoff_time_ist="15:22",
                )
                self.runs[run_id] = ScalperRun(cfg, trade_callback=self.trade_callback, order_callback=self.order_callback)

        for r in self.runs.values():
            r.trade_callback = self.trade_callback
            r.order_callback = self.order_callback
        self.save_state()

    def start_run(self, config: ScalperRunConfig) -> ScalperRun:
        if len(self.runs) >= self.max_runs and config.run_id not in self.runs:
            # If an inactive default slot exists that hasn't traded, allow taking over it
            inactive_slots = [k for k, v in self.runs.items() if not v.is_active and len(v.trade_history) == 0 and len(v.active_slices) == 0]
            if inactive_slots:
                del self.runs[inactive_slots[-1]]
            else:
                raise ValueError(f"Maximum of {self.max_runs} concurrent run slots reached.")

        existing = self.runs.get(config.run_id)
        run = ScalperRun(config, trade_callback=self.trade_callback, order_callback=self.order_callback)
        if existing:
            # Preserve existing trade history, accumulated realized PnL, and audit events across run starts/re-configurations
            run.trade_history = list(existing.trade_history)
            run.accumulated_realized_pnl = existing.accumulated_realized_pnl
            run.audit_events = list(existing.audit_events)
            run.run_number = existing.run_number + 1

        run.start()
        self.runs[config.run_id] = run
        self.active_run_id = config.run_id
        self.save_state()
        return run


    def get_run(self, run_id: str) -> Optional[ScalperRun]:
        return self.runs.get(run_id)

    def stop_run(self, run_id: str) -> Optional[List[TradeRecord]]:
        run = self.runs.get(run_id)
        if run:
            res = run.stop()
            self.save_state()
            return res
        return None

    def exit_slice(
        self,
        run_id: str,
        slice_identifier: Any,
        exit_price: Optional[float] = None,
    ) -> Optional[TradeRecord]:
        run = self.runs.get(run_id)
        if not run:
            return None
        trade = run.exit_slice(slice_identifier, exit_price=exit_price)
        if trade:
            self.save_state()
        return trade

    def clear_history(self, run_id: Optional[str] = None, instrument: Optional[str] = None) -> None:
        """Clears trade history for a specific run, specific instrument, or all runs."""
        if instrument:
            inst_upper = instrument.upper()
            # Clear matching trades from individual runs
            for r in self.runs.values():
                r_inst = (r.config.instrument_name or "NIFTY").upper()
                if r_inst == inst_upper or inst_upper in (r.config.contract_symbol or "").upper():
                    r.clear_history()
                else:
                    r.trade_history = [
                        t for t in r.trade_history
                        if (getattr(t, "instrument", "") or "").upper() != inst_upper
                        and inst_upper not in (getattr(t, "contract_symbol", "") or "").upper()
                    ]
            # Clear matching trades from master_trade_history
            if hasattr(self, "master_trade_history"):
                self.master_trade_history = [
                    t for t in self.master_trade_history
                    if (t.get("instrument") or t.get("symbol") or "").upper() != inst_upper
                    and inst_upper not in (t.get("contract_symbol") or "").upper()
                ]
        elif run_id and run_id in self.runs:
            self.runs[run_id].clear_history()
            if hasattr(self, "master_trade_history"):
                self.master_trade_history = [t for t in self.master_trade_history if t.get("run_id") != run_id]
        else:
            for r in self.runs.values():
                r.clear_history()
            if hasattr(self, "master_trade_history"):
                self.master_trade_history.clear()
        self.save_state()

    def list_runs(self) -> List[Dict[str, Any]]:
        return [r.get_status() for r in self.runs.values()]

    def get_all_trade_history(self) -> List[Dict[str, Any]]:
        seen_ids = set()
        all_trades = []
        for r in self.runs.values():
            for t in r.trade_history:
                d = asdict(t)
                tid = d.get("trade_id")
                if tid and tid not in seen_ids:
                    seen_ids.add(tid)
                    all_trades.append(d)
                elif not tid:
                    all_trades.append(d)
        for d in getattr(self, "master_trade_history", []):
            tid = d.get("trade_id")
            if tid and tid not in seen_ids:
                seen_ids.add(tid)
                all_trades.append(d)
            elif not tid:
                all_trades.append(d)

        all_trades.sort(
            key=lambda t: t.get("exit_time") or t.get("exited_at") or t.get("filled_at") or t.get("created_at") or "",
            reverse=True,
        )
        return all_trades

    def save_state(self) -> None:
        """Persists all run states and master trade history to local JSON file."""
        if self.state_file == ":memory:":
            return
        try:
            state_data = {
                "active_run_id": self.active_run_id,
                "runs": {r_id: r.to_dict() for r_id, r in self.runs.items()},
                "master_trade_history": [],  # Trade history is exclusively saved to MongoDB SlicerNS database
                "saved_at": datetime.datetime.now(IST).isoformat(),
            }
            tmp_file = f"{self.state_file}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2)
            os.replace(tmp_file, self.state_file)
        except Exception:
            pass

    def load_state(self) -> bool:
        """Loads state from local JSON file if exists."""
        if self.state_file == ":memory:":
            return False
        if not os.path.exists(self.state_file):
            return False
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                state_data = json.load(f)
            self.active_run_id = state_data.get("active_run_id", "run01")
            self.master_trade_history = state_data.get("master_trade_history", [])
            runs_dict = state_data.get("runs", {})
            for r_id, r_data in runs_dict.items():
                self.runs[r_id] = ScalperRun.from_dict(r_data)
            return len(self.runs) > 0
        except Exception:
            return False


# Backward-compatible alias
MultiSlotManager = RunManager
