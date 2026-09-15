"""
Comprehensive Hardening & Regression Test Suite for Slicing Scalper Simulation Engine.
Verifies all items in Section 4 of the specification:
1. Walkthrough Scenario (§2)
2. Loss-Exit Ladder Recenter (§1.2)
3. Same-tick Buy vs Profit-Sell Ordering (§1.5)
4. Gap Fill Modes (all_crossed, single, skip_and_log) (§1.4)
5. Contract / Strike Locking on Open Positions (§1.1)
6. State Persistence & Crash Recovery (§3.1)
7. Feed Staleness & Paused Buys (§3.2)
8. Multi-Slot Run Isolation (§3.4)
9. Config Validation on Run Start (§3.7)
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
except ImportError:
    pytest = None

from slice_trading_engine import (
    ScalperRun,
    ScalperRunConfig,
    RunManager,
    SliceStatus,
    SliceExitReason,
    GapFillMode,
    recenter_range,
    validate_config,
    calculate_nearest_50_strike,
)


def test_1_walkthrough_simulation_scenario():
    """
    Verifies the one-trade-per-level walkthrough:
    range 60->30, step 10, profit_point 10, loss_point 20, qty 65:
    - Tick 1 (63.40): above 60 -> no fill.
    - Tick 2 (60.00): BUY Slice A @ 60.00 (Target 70.00). Level 60 marked FILLED.
    - Tick 3 (70.30): SELL Slice A @ 70.30 (+₹669.50), Level 60 marked USED (stays USED).
    - Tick 4 (60.00): Price revisits 60.00 -> Skipped (no trade, 60 is USED).
    - Tick 5 (50.00): BUY Slice B @ 50.00 (Target 60.00). Level 50 marked FILLED.
    - Tick 6 (60.00): SELL Slice B @ 60.00 (+₹650.00), Level 50 marked USED.
    - Tick 7 (50.00): Price revisits 50.00 -> Skipped (no trade, 50 is USED).
    - Tick 8 (40.00): BUY Slice C @ 40.00 (Target 50.00). Level 40 marked FILLED.
      Total Realized PnL = ₹1319.50.
    """
    cfg = ScalperRunConfig(
        run_id="walkthrough_test",
        instrument_name="NIFTY",
        option_type="PE",
        strike=24250,
        range_high=60.0,
        range_low=30.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=20.0,
        qty_per_slice_lots=1,
    )
    run = ScalperRun(cfg)
    run.start()

    assert [s.level_price for s in run.grid_ladder] == [60.0, 50.0, 40.0, 30.0]
    for s in run.grid_ladder:
        assert s.status == SliceStatus.UNUSED

    # Tick 1: 63.40 (above 60, no buy)
    a1 = run.process_tick(63.40)
    assert len(a1["buys"]) == 0
    assert len(run.active_slices) == 0

    # Tick 2: 60.00 -> Buy Slice A
    a2 = run.process_tick(60.00)
    assert len(a2["buys"]) == 1
    assert a2["buys"][0]["label"] == "A"
    assert a2["buys"][0]["fill_price"] == 60.0
    assert a2["buys"][0]["profit_target"] == 70.0
    assert len(run.active_slices) == 1
    assert run.grid_ladder[0].status == SliceStatus.FILLED

    # Tick 3: 70.30 -> Sell Slice A, Level 60 stays USED (one-trade-per-level)
    a3 = run.process_tick(70.30)
    assert len(a3["sells"]) == 1
    assert a3["sells"][0]["label"] == "A"
    assert a3["sells"][0]["exit_price"] == 70.30
    assert a3["sells"][0]["pnl_points"] == 10.30
    assert round(a3["sells"][0]["pnl_rupees"], 2) == 669.50
    assert round(run.accumulated_realized_pnl, 2) == 669.50
    assert len(run.active_slices) == 0
    assert run.grid_ladder[0].status == SliceStatus.USED

    # Tick 4: 60.00 -> Level 60 is USED, so skipped (no buy)
    a4 = run.process_tick(60.00)
    assert len(a4["buys"]) == 0
    assert len(run.active_slices) == 0
    assert run.grid_ladder[0].status == SliceStatus.USED

    # Tick 5: 50.00 -> Level 50 is UNUSED -> Buy Slice B @ 50.00
    a5 = run.process_tick(50.00)
    assert len(a5["buys"]) == 1
    assert a5["buys"][0]["label"] == "B"
    assert a5["buys"][0]["fill_price"] == 50.0
    assert a5["buys"][0]["profit_target"] == 60.0
    assert len(run.active_slices) == 1
    assert run.grid_ladder[1].status == SliceStatus.FILLED

    # Tick 6: 60.00 -> Sell Slice B @ 60.00, Level 50 marked USED
    a6 = run.process_tick(60.00)
    assert len(a6["sells"]) == 1
    assert a6["sells"][0]["label"] == "B"
    assert round(a6["sells"][0]["pnl_rupees"], 2) == 650.00
    assert round(run.accumulated_realized_pnl, 2) == 1319.50
    assert len(run.active_slices) == 0
    assert run.grid_ladder[0].status == SliceStatus.USED
    assert run.grid_ladder[1].status == SliceStatus.USED
    assert run.grid_ladder[2].status == SliceStatus.UNUSED

    # Tick 7: 50.00 -> Level 50 is USED, skipped
    a7 = run.process_tick(50.00)
    assert len(a7["buys"]) == 0
    assert len(run.active_slices) == 0

    # Tick 8: 40.00 -> Level 40 is UNUSED -> Buy Slice C @ 40.00
    a8 = run.process_tick(40.00)
    assert len(a8["buys"]) == 1
    assert a8["buys"][0]["label"] == "C"
    assert a8["buys"][0]["fill_price"] == 40.0
    assert a8["buys"][0]["profit_target"] == 50.0
    assert len(run.active_slices) == 1
    assert run.grid_ladder[2].status == SliceStatus.FILLED


def test_2_loss_exit_ladder_stops_without_auto_restart():
    """
    Verifies that upon loss-exit, all active slices are exited, the run is stopped
    (is_active=False), and it does NOT start another slicing live ladder on its own.
    """
    cfg = ScalperRunConfig(
        run_id="loss_recenter_test",
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=20.0,
    )
    run = ScalperRun(cfg)
    run.start()

    # Step down one by one to fill ladder
    for p in [150.0, 140.0, 130.0, 120.0, 110.0, 100.0]:
        run.process_tick(p)

    lowest = run.get_lowest_filled_slice()
    assert lowest.fill_price == 100.0
    assert run.get_loss_trigger_price() == 80.0

    # Loss exit trigger at 79.00
    actions = run.process_tick(79.00)
    assert actions["loss_exit"] is True
    assert actions["cycle_reset"] is False
    assert run.is_active is False
    assert len(run.active_slices) == 0

    # Verify subsequent ticks do not start another ladder or buy new slices
    act_after = run.process_tick(120.0)
    assert len(act_after["buys"]) == 0
    assert len(run.active_slices) == 0


def test_3_same_tick_buy_not_eligible_for_sell():
    """
    Verifies that a slice filled on tick N is NOT eligible for profit-sell
    on the same tick using that same tick's price.
    """
    cfg = ScalperRunConfig(
        run_id="same_tick_test",
        range_high=100.0,
        range_low=50.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=20.0,
    )
    run = ScalperRun(cfg)
    run.start()

    # Tick at 100.00: fills Slice A @ 100.00 (profit_target = 110.00)
    # Even though tick is 100, it cannot sell on this tick.
    actions = run.process_tick(100.00)
    assert len(actions["buys"]) == 1
    assert len(actions["sells"]) == 0
    assert len(run.active_slices) == 1
    assert run.active_slices[0].profit_target == 110.00


def test_4_startup_below_range_single_fill_only():
    """
    REGRESSION TEST: Start below top of range with price-range matching.
    range_high=150, range_low=100, slice_interval=10.
    First tick after start is 100.65 (where 100 < 100.65 <= 110).
    Assert that level 110.0 is matched and filled, while other 5 levels remain PENDING.
    """
    cfg = ScalperRunConfig(
        run_id="startup_below_test",
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
    )
    run = ScalperRun(cfg)
    run.start()

    # Initial ladder levels
    assert [s.level_price for s in run.grid_ladder] == [150.0, 140.0, 130.0, 120.0, 110.0, 100.0]

    # First tick at 100.65 belongs to level 110.0
    act = run.process_tick(100.65)
    assert len(act["buys"]) == 1
    assert act["buys"][0]["label"] == "A"
    assert act["buys"][0]["level_price"] == 110.0
    assert act["buys"][0]["fill_price"] == 100.65

    # Check active slices
    assert len(run.active_slices) == 1
    assert run.active_slices[0].label == "A"
    assert run.active_slices[0].level_price == 110.0

    # Level 110 (index 4) is FILLED, other 5 levels remain strictly PENDING
    assert run.grid_ladder[4].status == SliceStatus.FILLED
    for i, lvl in enumerate(run.grid_ladder):
        if i != 4:
            assert lvl.status == SliceStatus.PENDING
            assert lvl.fill_price is None


def test_5_progressive_single_slice_fill_per_tick():
    """
    REGRESSION TEST: Normal progressive step down.
    Ticks 150 -> 140 -> 130 -> 120 -> 110.
    Each tick must fill exactly one new slice, never more than one per tick,
    and never re-fill an already FILLED level.
    """
    cfg = ScalperRunConfig(
        run_id="progressive_test",
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
    )
    run = ScalperRun(cfg)
    run.start()

    expected_levels = [150.0, 140.0, 130.0, 120.0, 110.0]
    expected_labels = ["A", "B", "C", "D", "E"]

    for i, tick_price in enumerate(expected_levels):
        act = run.process_tick(tick_price)
        assert len(act["buys"]) == 1, f"Tick {tick_price} did not fill exactly 1 slice"
        assert act["buys"][0]["label"] == expected_labels[i]
        assert act["buys"][0]["level_price"] == tick_price
        assert len(run.active_slices) == i + 1

    # Sending 110 again while 150..110 are filled and current price is 110
    # should NOT fill any new slice (since 100 is below 110 and 150..110 are already filled)
    act_repeat = run.process_tick(110.0)
    assert len(act_repeat["buys"]) == 0
    assert len(act_repeat["sells"]) == 0
    assert len(run.active_slices) == 5


def test_6_multi_level_gap_single_slice_fill():
    """
    REGRESSION TEST: Multi-level price gap.
    Price jumps from 150 straight to 95.
    Assert that only ONE slice fills on that tick (level 150),
    and remaining levels (140..100) are not bulk-filled.
    """
    cfg = ScalperRunConfig(
        run_id="gap_test",
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
        gap_fill_mode="skip_and_log",
    )
    run = ScalperRun(cfg)
    run.start()

    act = run.process_tick(95.0)
    assert len(act["buys"]) == 1
    assert act["buys"][0]["label"] == "A"
    assert act["buys"][0]["level_price"] == 100.0
    assert act["gap_skipped"] == [150.0, 140.0, 130.0, 120.0, 110.0]
    assert len(run.active_slices) == 1
    assert run.grid_ladder[-1].status == SliceStatus.FILLED
    for lvl in run.grid_ladder[:-1]:
        assert lvl.status == SliceStatus.PENDING


def test_7_contract_strike_locking_on_open_positions():
    """
    Verifies that the contract symbol does NOT change while active_slices is non-empty,
    even when spot crosses into a new nearest-50 strike boundary.
    """
    cfg = ScalperRunConfig(
        run_id="strike_lock_test",
        instrument_name="NIFTY",
        option_type="CE",
        strike=24250,
        range_high=100.0,
        range_low=50.0,
        slice_interval=10.0,
        auto_restrike=True,
    )
    run = ScalperRun(cfg)
    run.start()

    # Initial resolve
    assert run.config.strike == 24250
    assert run.config.contract_symbol == "NIFTY 24250 CE"

    # Fill 1 slice @ 100
    run.process_tick(100.0)
    assert len(run.active_slices) == 1

    # Spot moves dramatically from 24250 to 24450 (new nearest 50 strike is 24450)
    locked_strike = run.handle_spot_update(24450.0)
    # MUST stay locked at 24250 because slices are open!
    assert locked_strike == 24250
    assert run.config.strike == 24250
    assert run.config.contract_symbol == "NIFTY 24250 CE"

    # Close slice by profit target
    run.process_tick(110.0)
    assert len(run.active_slices) == 0

    # Now position is flat: spot update requires flicker_guard_ticks (default 3) to relock
    # Tick 1: guard initialized, candidate stored, still returns locked 24250
    t1 = run.handle_spot_update(24450.0)
    assert t1 == 24250
    assert run.config.strike == 24250

    # Tick 2: candidate counter = 2, still returns locked 24250
    t2 = run.handle_spot_update(24450.0)
    assert t2 == 24250
    assert run.config.strike == 24250

    # Tick 3: candidate counter = 3 >= guard_ticks (3): stabilizes and locks in 24450!
    t3 = run.handle_spot_update(24450.0)
    assert t3 == 24450
    assert run.config.strike == 24450
    assert run.config.contract_symbol == "NIFTY 24450 CE"



def test_8_state_persistence_and_crash_recovery():
    """
    Verifies saving state to file and recovering it identically.
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        mgr = RunManager(max_runs=3, state_file=tmp_path)
        cfg = ScalperRunConfig(
            run_id="persist_run",
            range_high=100.0,
            range_low=50.0,
            slice_interval=10.0,
        )
        run = mgr.start_run(cfg)
        run.process_tick(100.0)  # fills 100
        run.process_tick(90.0)   # fills 90
        mgr.save_state()

        # Create new manager and load
        mgr2 = RunManager(max_runs=3, state_file=tmp_path)
        loaded_run = mgr2.get_run("persist_run")
        assert loaded_run is not None
        assert loaded_run.is_active is True
        assert len(loaded_run.active_slices) == 2
        assert loaded_run.active_slices[0].label == "A"
        assert loaded_run.active_slices[1].label == "B"
        assert loaded_run.grid_ladder[0].status == SliceStatus.FILLED
        assert loaded_run.grid_ladder[1].status == SliceStatus.FILLED
        assert loaded_run.grid_ladder[2].status == SliceStatus.PENDING
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_9_feed_staleness_and_paused_buys():
    """
    Verifies feed staleness tracking after 5 missing polls,
    pausing new buys while keeping loss-exit active.
    """
    cfg = ScalperRunConfig(
        run_id="stale_test",
        range_high=100.0,
        range_low=50.0,
        slice_interval=10.0,
        loss_point=20.0,
    )
    run = ScalperRun(cfg)
    run.start()

    # Buy 1 slice @ 100
    run.process_tick(100.0)
    assert len(run.active_slices) == 1
    # Range stop-loss is range_bottom (50) - loss_point (20) = 30.0
    assert run.get_loss_trigger_price() == 30.0

    # 5 consecutive missing polls
    for _ in range(5):
        run.update_feed_health(False)

    assert run.feed_status == "STALE"

    # When STALE, new buy at 90.0 is blocked
    act_stale = run.process_tick(90.0)
    assert len(act_stale["buys"]) == 0
    assert len(run.active_slices) == 1

    # But loss exit at 28.0 (below 30.0 trigger) STILL FIRES!
    act_loss = run.process_tick(28.0)
    assert act_loss["loss_exit"] is True
    assert len(run.active_slices) == 0


def test_10_multi_slot_isolation():
    """
    Verifies that multiple concurrent run slots operate with 100% independence,
    with simultaneous loss-exits and PnL completely isolated.
    """
    mgr = RunManager(max_runs=3, state_file=":memory:")

    cfg1 = ScalperRunConfig(run_id="slot1", instrument_name="NIFTY", option_type="CE", strike=24250, range_high=100.0, range_low=50.0, slice_interval=10.0)
    cfg2 = ScalperRunConfig(run_id="slot2", instrument_name="NIFTY", option_type="PE", strike=24250, range_high=80.0, range_low=40.0, slice_interval=10.0)

    run1 = mgr.start_run(cfg1)
    run2 = mgr.start_run(cfg2)

    # Slot 1 fills @ 100
    run1.process_tick(100.0)
    # Slot 2 fills @ 80
    run2.process_tick(80.0)

    assert len(run1.active_slices) == 1
    assert len(run2.active_slices) == 1
    assert run1.active_slices[0].fill_price == 100.0
    assert run2.active_slices[0].fill_price == 80.0

    # Slot 1 hits profit target @ 110 (+10 pts / +₹650)
    run1.process_tick(110.0)
    assert run1.accumulated_realized_pnl == 650.0
    # Slot 2 must be completely unaffected (0 realized PnL)
    assert run2.accumulated_realized_pnl == 0.0

    # Slot 2 hits range-anchored loss exit @ 19.0 (range_bottom 40 - loss_point 20 = 20.0)
    run2.process_tick(19.0)
    assert run2.is_active is False
    assert run2.accumulated_realized_pnl < 0
    # Slot 1 remains active and PnL remains untouched
    assert run1.is_active is True
    assert run1.accumulated_realized_pnl == 650.0


def test_11_config_validation():
    """
    Verifies strict config validation fails fast with clear ValueError on invalid parameters.
    """
    # 1. range_low >= range_high
    try:
        ScalperRunConfig(range_high=100.0, range_low=100.0)
        assert False, "Expected ValueError for range_low >= range_high"
    except ValueError as e:
        assert "must be strictly less than range_high" in str(e)

    # 2. slice_interval <= 0
    try:
        ScalperRunConfig(range_high=150.0, range_low=100.0, slice_interval=0)
        assert False, "Expected ValueError for slice_interval <= 0"
    except ValueError as e:
        assert "must be greater than 0" in str(e)

    # 3. profit_point <= 0
    try:
        ScalperRunConfig(range_high=150.0, range_low=100.0, profit_point=0)
        assert False, "Expected ValueError for profit_point <= 0"
    except ValueError as e:
        assert "must be greater than 0" in str(e)

    # 4. loss_point <= 0
    try:
        ScalperRunConfig(range_high=150.0, range_low=100.0, loss_point=-5)
        assert False, "Expected ValueError for loss_point <= 0"
    except ValueError as e:
        assert "must be greater than 0" in str(e)

    # 5. span not divisible by slice_interval
    try:
        ScalperRunConfig(range_high=150.0, range_low=100.0, slice_interval=7.0)
        assert False, "Expected ValueError for uneven span"
    except ValueError as e:
        assert "must be evenly divisible by slice_interval" in str(e)


def test_12_nearest_strike_calculation_and_updates():
    """
    Verifies that calculate_nearest_50_strike computes exact nearest 50 strikes,
    and handle_spot_update accurately updates the strike when inactive or flat.
    """
    assert calculate_nearest_50_strike(24500.0) == 24500
    assert calculate_nearest_50_strike(24520.0) == 24500
    assert calculate_nearest_50_strike(24524.9) == 24500
    assert calculate_nearest_50_strike(24525.0) == 24550
    assert calculate_nearest_50_strike(24549.9) == 24550
    assert calculate_nearest_50_strike(24550.0) == 24550
    assert calculate_nearest_50_strike(24574.9) == 24550
    assert calculate_nearest_50_strike(24575.0) == 24600

    # Inactive run updates strike on spot update
    cfg = ScalperRunConfig(run_id="strike_test", strike=24250, spot_ltp=24250.0)
    run = ScalperRun(cfg)
    assert run.is_active is False
    assert run.config.strike == 24250

    run.handle_spot_update(24820.0)
    assert run.spot_ltp == 24820.0
    assert run.config.strike == 24800
    assert "24800" in run.config.contract_symbol


def test_13_price_range_level_matching_bug_fix():
    """
    Verifies that level assignment is strictly price-range based (L - step < price <= L),
    and not positional.
    Grid: 86, 84, 82, 80, 78, 76 (step 2.0).
    Price 83.10 must fill Level 84.00 (not 86.00).
    """
    cfg = ScalperRunConfig(
        run_id="range_match_test",
        range_high=86.0,
        range_low=76.0,
        slice_interval=2.0,
        profit_point=2.0,
    )
    run = ScalperRun(cfg)
    run.start()

    # Ladder rungs: 86.0, 84.0, 82.0, 80.0, 78.0, 76.0
    assert [s.level_price for s in run.grid_ladder] == [86.0, 84.0, 82.0, 80.0, 78.0, 76.0]

    # Verify calculate_level_for_price helper directly
    assert run.level_for_price(86.00) == 86.0
    assert run.level_for_price(85.50) == 86.0
    assert run.level_for_price(84.00) == 84.0
    assert run.level_for_price(83.10) == 84.0
    assert run.level_for_price(82.00) == 82.0
    assert run.level_for_price(81.90) == 82.0
    assert run.level_for_price(76.00) == 76.0
    assert run.level_for_price(87.00) is None  # above range
    assert run.level_for_price(74.00) is None  # below range

    # Tick at 83.10
    act = run.process_tick(83.10)
    assert len(act["buys"]) == 1
    assert act["buys"][0]["label"] == "A"
    assert act["buys"][0]["level_price"] == 84.0  # MUST be 84.00, NOT 86.00!
    assert act["buys"][0]["fill_price"] == 83.10
    assert act["buys"][0]["profit_target"] == 85.10

    # Verify grid ladder state: Level 86 is still PENDING, Level 84 is FILLED
    assert run.grid_ladder[0].level_price == 86.0
    assert run.grid_ladder[0].status == SliceStatus.PENDING

    assert run.grid_ladder[1].level_price == 84.0
    assert run.grid_ladder[1].status == SliceStatus.FILLED
    assert run.grid_ladder[1].fill_price == 83.10


def test_14_multi_instrument_strike_lot_size_and_slots():
    """
    Verifies multi-instrument support:
    1. Strike step calculation (Nifty = 50, Sensex = 100)
    2. Instrument-specific lot size (Nifty = 65, Sensex = 10)
    3. Multi-slot concurrent independence with different instruments.
    """
    from slice_trading_engine import calculate_nearest_strike, get_lot_size, INSTRUMENT_CONFIG

    # 1. Strike calculation
    assert calculate_nearest_strike(24824.0, "NIFTY") == 24800
    assert calculate_nearest_strike(24825.0, "NIFTY") == 24850
    assert calculate_nearest_strike(24874.9, "NIFTY") == 24850
    assert calculate_nearest_strike(24875.0, "NIFTY") == 24900

    assert calculate_nearest_strike(81249.0, "SENSEX") == 81200
    assert calculate_nearest_strike(81250.0, "SENSEX") == 81300
    assert calculate_nearest_strike(81349.9, "SENSEX") == 81300
    assert calculate_nearest_strike(81350.0, "SENSEX") == 81400

    # 2. Lot sizes
    assert get_lot_size("NIFTY") == 65
    assert get_lot_size("SENSEX") == 20

    # 3. ScalperRun with SENSEX configuration
    sensex_cfg = ScalperRunConfig(
        run_id="sensex_slot",
        instrument_name="SENSEX",
        option_type="CE",
        strike=81200,
        range_high=200.0,
        range_low=100.0,
        slice_interval=20.0,
        qty_per_slice_lots=2,  # 2 lots * 20 units = 40 units
        profit_point=20.0,
        loss_point=40.0,
    )
    assert sensex_cfg.lot_size == 20
    assert sensex_cfg.total_slice_quantity == 40

    sensex_run = ScalperRun(sensex_cfg)
    sensex_run.start()
    assert sensex_run.grid_ladder[0].quantity == 40

    # Fill 1 slice @ 200.0
    act = sensex_run.process_tick(200.0)
    assert len(act["buys"]) == 1
    assert act["buys"][0]["quantity"] == 40

    # Sell at target @ 220.0 -> P&L = +20 pts * 40 units = +Rs 800.00
    act2 = sensex_run.process_tick(220.0)
    assert len(act2["sells"]) == 1
    assert act2["sells"][0]["pnl_rupees"] == 800.0
    assert sensex_run.accumulated_realized_pnl == 800.0


def test_15_trade_record_entry_and_exit_time_ist():
    """
    Verifies that TradeRecords include entry_time and exit_time based on Indian Standard Time (IST):
    1. entry_time and exit_time are populated with ISO strings containing +05:30 offset
    2. Serialization and deserialization preserves entry_time and exit_time
    """
    cfg = ScalperRunConfig(
        run_id="test_ist_time",
        instrument_name="NIFTY",
        option_type="CE",
        strike=24200,
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=20.0,
        qty_per_slice_lots=1,
    )
    run = ScalperRun(cfg)
    run.start()

    # Buy @ 150
    act1 = run.process_tick(150.0)
    assert len(act1["buys"]) == 1
    filled_slice = run.active_slices[0]
    assert filled_slice.filled_at is not None
    assert "+05:30" in filled_slice.filled_at

    # Sell @ 160 (Profit target)
    act2 = run.process_tick(160.0)
    assert len(act2["sells"]) == 1
    assert len(run.trade_history) == 1

    trade = run.trade_history[0]
    assert trade.entry_time is not None
    assert trade.exit_time is not None
    assert "+05:30" in trade.entry_time
    assert "+05:30" in trade.exit_time
    assert trade.entry_time == trade.filled_at
    assert trade.exit_time == trade.exited_at

    # Check persistence serialization and restoration
    d = run.to_dict()
    assert d["trade_history"][0]["entry_time"] == trade.entry_time
    assert d["trade_history"][0]["exit_time"] == trade.exit_time

    restored = ScalperRun.from_dict(d)
    assert len(restored.trade_history) == 1
    restored_trade = restored.trade_history[0]
    assert restored_trade.entry_time == trade.entry_time
    assert restored_trade.exit_time == trade.exit_time


def test_16_one_trade_per_level_lifecycle_and_skip_used_level():
    """
    Verifies the complete one-trade-per-level lifecycle from the worked example:
    Grid: 50.0, 40.0, 30.0 (step 10.0, profit 10.0)
    1. Grid initialized to UNUSED.
    2. Buy fires at 50 -> level 50 marked FILLED (target 60.0).
    3. Sells at 60 -> level 50 marked USED.
    4. LTP drops back to 50 -> SKIPPED (level 50 is USED).
    5. LTP continues down to 40 -> level 40 is UNUSED -> buy fires at 40.
    """
    cfg = ScalperRunConfig(
        run_id="one_shot_test",
        instrument_name="NIFTY",
        strike=24200,
        range_high=50.0,
        range_low=30.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=20.0,
    )
    run = ScalperRun(cfg)
    run.start()

    # 1. Initial levels are UNUSED
    assert [s.level_price for s in run.grid_ladder] == [50.0, 40.0, 30.0]
    assert all(s.status == SliceStatus.UNUSED for s in run.grid_ladder)

    # 2. Buy fires at 50.00
    a1 = run.process_tick(50.00)
    assert len(a1["buys"]) == 1
    assert a1["buys"][0]["level_price"] == 50.0
    assert a1["buys"][0]["profit_target"] == 60.0
    assert run.grid_ladder[0].status == SliceStatus.FILLED
    assert run.grid_ladder[1].status == SliceStatus.UNUSED

    # 3. Sells at 60.00 (profit target hit)
    a2 = run.process_tick(60.00)
    assert len(a2["sells"]) == 1
    assert len(run.active_slices) == 0
    # Level 50 must now be USED
    assert run.grid_ladder[0].status == SliceStatus.USED

    # 4. LTP falls back to 50.00 -> Must be skipped (level 50 is USED)
    a3 = run.process_tick(50.00)
    assert len(a3["buys"]) == 0
    assert len(a3["sells"]) == 0
    assert len(run.active_slices) == 0
    assert run.grid_ladder[0].status == SliceStatus.USED

    # 5. LTP falls to 40.00 -> 40 is UNUSED -> Buy fires at 40
    a4 = run.process_tick(40.00)
    assert len(a4["buys"]) == 1
    assert a4["buys"][0]["level_price"] == 40.0
    assert a4["buys"][0]["label"] == "B"
    assert run.grid_ladder[1].status == SliceStatus.FILLED
    assert run.grid_ladder[0].status == SliceStatus.USED
    assert run.grid_ladder[2].status == SliceStatus.UNUSED


def test_17_loss_exit_stops_run_and_clears_active_slices():
    """
    Verifies that on loss-exit, all active slices are exited with LOSS_EXIT,
    the run stops (is_active=False), and no auto-restart occurs.
    """
    cfg = ScalperRunConfig(
        run_id="recenter_reset_test",
        range_high=100.0,
        range_low=60.0,
        slice_interval=10.0,
        loss_point=20.0,
        profit_point=10.0,
    )
    run = ScalperRun(cfg)
    run.start()

    # Buy at 100, 90, 80
    run.process_tick(100.0)
    run.process_tick(90.0)
    run.process_tick(80.0)
    assert len(run.active_slices) == 3

    # Range: 100 down to 60, loss_point: 20 -> range_stop_loss: 40.0. Hit at 39.0
    act = run.process_tick(39.0)
    assert act["loss_exit"] is True
    assert act["cycle_reset"] is False
    assert run.is_active is False
    assert len(run.active_slices) == 0
    # Filled levels were exited
    exited_levels = [s for s in run.grid_ladder if s.status == SliceStatus.EXITED]
    assert len(exited_levels) == 3


def test_18_persistence_with_used_and_unused_levels():
    """
    Verifies that saving and restoring a run accurately preserves UNUSED,
    FILLED, and USED level statuses.
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        mgr = RunManager(max_runs=3, state_file=tmp_path)
        cfg = ScalperRunConfig(
            run_id="persist_used_test",
            range_high=100.0,
            range_low=70.0,
            slice_interval=10.0,
            profit_point=10.0,
        )
        run = mgr.start_run(cfg)
        # Levels: [100, 90, 80, 70]
        # Buy @ 100 -> sell @ 110 (Level 100 becomes USED)
        run.process_tick(100.0)
        run.process_tick(110.0)
        assert run.grid_ladder[0].status == SliceStatus.USED

        # Buy @ 90 (Level 90 becomes FILLED)
        run.process_tick(90.0)
        assert run.grid_ladder[1].status == SliceStatus.FILLED
        assert run.grid_ladder[2].status == SliceStatus.UNUSED
        assert run.grid_ladder[3].status == SliceStatus.UNUSED

        mgr.save_state()

        # Reload
        mgr2 = RunManager(max_runs=3, state_file=tmp_path)
        loaded = mgr2.get_run("persist_used_test")
        assert loaded is not None
        assert loaded.grid_ladder[0].status == SliceStatus.USED
        assert loaded.grid_ladder[1].status == SliceStatus.FILLED
        assert loaded.grid_ladder[2].status == SliceStatus.UNUSED
        assert loaded.grid_ladder[3].status == SliceStatus.UNUSED
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_19_trade_callback_mongo_dispatch():
    """
    Verifies that when a trade is closed (profit sell or loss exit),
    the registered trade_callback is invoked with the trade and config payloads.
    """
    dispatched_trades = []

    def mock_trade_callback(trade_data, config_data):
        dispatched_trades.append((trade_data, config_data))

    mgr = RunManager(max_runs=3, state_file=":memory:", trade_callback=mock_trade_callback)
    cfg = ScalperRunConfig(
        run_id="callback_test",
        instrument_name="NIFTY",
        option_type="CE",
        strike=24300,
        range_high=100.0,
        range_low=70.0,
        slice_interval=10.0,
        profit_point=10.0,
    )
    run = mgr.start_run(cfg)

    # Buy at 100
    run.process_tick(100.0)
    assert len(dispatched_trades) == 0

    # Sell at 110 (profit target hit)
    run.process_tick(110.0)
    assert len(dispatched_trades) == 1
    trade_dict, config_dict = dispatched_trades[0]
    assert trade_dict["level_price"] == 100.0
    assert trade_dict["fill_price"] == 100.0
    assert trade_dict["exit_price"] == 110.0
    assert trade_dict["pnl_points"] == 10.0
    assert trade_dict["exit_reason"] == "PROFIT_TARGET"
    assert config_dict["strike"] == 24300


def test_20_range_anchored_loss_trigger_shared_across_levels():
    """
    Tests the exact worked example:
    Range: 250 down to 220 (levels 250, 240, 230, 220), loss_point = 20.
    range_bottom = 220 -> range_stop_loss = 220 - 20 = 200.
    Every position opened at any level (230 or 220) shares range_stop_loss = 200.
    Buy at 230 -> exits at 200 (not 210).
    Buy at 220 -> exits at 200.
    """
    cfg = ScalperRunConfig(
        run_id="range_stop_loss_test",
        range_high=250.0,
        range_low=220.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=20.0,
    )
    run = ScalperRun(cfg)
    run.start()

    assert run.get_range_stop_loss() == 200.0
    assert run.get_loss_trigger_price() == 200.0

    # Buy at 230 (Slice A)
    a1 = run.process_tick(230.0)
    assert len(a1["buys"]) == 1
    assert a1["buys"][0]["fill_price"] == 230.0
    assert a1["buys"][0]["sl_price"] == 200.0

    # Buy at 220 (Slice B)
    a2 = run.process_tick(220.0)
    assert len(a2["buys"]) == 1
    assert a2["buys"][0]["fill_price"] == 220.0
    assert a2["buys"][0]["sl_price"] == 200.0
    assert len(run.active_slices) == 2

    # Price drops to 210 -> neither 230 nor 220 should exit (stop loss is 200, NOT 210)
    a3 = run.process_tick(210.0)
    assert a3["loss_exit"] is False
    assert len(run.active_slices) == 2

    # Price drops to 200 -> Range-anchored loss trigger fires! Both slices exit at 200
    a4 = run.process_tick(200.0)
    assert a4["loss_exit"] is True
    assert a4["cycle_reset"] is False
    assert run.is_active is False
    assert len(run.active_slices) == 0
    assert len(run.trade_history) == 2

    # Verify both trades exited at 200 with LOSS_EXIT
    t_b = run.trade_history[0]  # Most recent
    t_a = run.trade_history[1]
    assert t_a.fill_price == 230.0
    assert t_a.exit_price == 200.0
    assert t_a.pnl_points == -30.0
    assert t_a.exit_reason == "LOSS_EXIT"

    assert t_b.fill_price == 220.0
    assert t_b.exit_price == 200.0
    assert t_b.pnl_points == -20.0
    assert t_b.exit_reason == "LOSS_EXIT"


def test_21_range_anchored_stop_loss_persistence():
    """
    Verifies that range_stop_loss and effective_range_low are persisted and restored accurately.
    """
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as tf:
        tmp_path = tf.name

    try:
        mgr = RunManager(max_runs=3, state_file=tmp_path)
        cfg = ScalperRunConfig(
            run_id="persist_range_sl",
            range_high=300.0,
            range_low=200.0,
            slice_interval=20.0,
            loss_point=30.0,
        )
        run = mgr.start_run(cfg)
        assert run.get_range_stop_loss() == 170.0  # 200 - 30

        run.process_tick(280.0)
        mgr.save_state()

        mgr2 = RunManager(max_runs=3, state_file=tmp_path)
        loaded = mgr2.get_run("persist_range_sl")
        assert loaded is not None
        assert loaded.get_range_stop_loss() == 170.0
        assert loaded.effective_range_low == 200.0
        assert loaded.effective_range_high == 300.0
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_22_trade_history_preserved_on_stop_and_restart():
    """
    Verifies that when a run is stopped and later re-started,
    all previously closed trades and realized P&L are preserved in master_trade_history.
    """
    mgr = RunManager(max_runs=3, state_file=":memory:")
    cfg = ScalperRunConfig(
        run_id="history_retention_test",
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
        profit_point=10.0,
    )
    run = mgr.start_run(cfg)

    # Buy at 150 -> Sell at 160
    run.process_tick(150.0)
    run.process_tick(160.0)
    assert len(run.trade_history) == 1
    assert len(mgr.get_all_trade_history()) == 1

    # Buy at 140 -> Stop run manually (exits active slice)
    run.process_tick(140.0)
    exited = mgr.stop_run("history_retention_test")
    assert len(exited) == 1
    assert len(run.trade_history) == 2
    assert len(mgr.get_all_trade_history()) == 2

    # Re-start run with same or new config
    cfg2 = ScalperRunConfig(
        run_id="history_retention_test",
        range_high=160.0,
        range_low=110.0,
        slice_interval=10.0,
        profit_point=10.0,
    )
    run2 = mgr.start_run(cfg2)
    # Previous trade history must be preserved
    assert len(run2.trade_history) == 2
    assert len(mgr.get_all_trade_history()) == 2
    assert mgr.get_all_trade_history()[0]["exit_reason"] in ("PROFIT_TARGET", "MANUAL")


def test_23_clear_history_by_instrument():
    """
    Verifies that clear_history(instrument='SENSEX') clears only SENSEX trades
    from all runs and master_trade_history, leaving NIFTY trades intact.
    """
    mgr = RunManager(max_runs=3, state_file=":memory:")
    
    # NIFTY run
    nifty_cfg = ScalperRunConfig(run_id="run_nifty", instrument_name="NIFTY", range_high=150.0, range_low=100.0, slice_interval=10.0, profit_point=10.0)
    nifty_run = mgr.start_run(nifty_cfg)
    nifty_run.process_tick(150.0)
    nifty_run.process_tick(160.0)

    # SENSEX run
    sensex_cfg = ScalperRunConfig(run_id="run_sensex", instrument_name="SENSEX", range_high=250.0, range_low=200.0, slice_interval=10.0, profit_point=10.0)
    sensex_run = mgr.start_run(sensex_cfg)
    sensex_run.process_tick(250.0)
    sensex_run.process_tick(260.0)

    assert len(mgr.get_all_trade_history()) == 2

    # Clear SENSEX history only
    mgr.clear_history(instrument="SENSEX")
    remaining = mgr.get_all_trade_history()
    assert len(remaining) == 1
    assert remaining[0]["instrument"] == "NIFTY"


def test_24_run_number_tracking_across_executions():
    """
    Verifies that when running the slicer for the first time, trades have run_number = 1.
    When restarted/run again, trades have run_number = 2, and so on.
    """
    mgr = RunManager(max_runs=3, state_file=":memory:")
    cfg = ScalperRunConfig(
        run_id="run_num_test",
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
        profit_point=10.0,
    )
    # Execution 1 (run_number = 1)
    run1 = mgr.start_run(cfg)
    assert run1.run_number == 1
    run1.process_tick(150.0)
    run1.process_tick(160.0)  # profit exit 1
    run1.process_tick(140.0)
    run1.process_tick(150.0)  # profit exit 2

    trades_run1 = [t for t in mgr.get_all_trade_history() if t["run_id"] == "run_num_test"]
    assert len(trades_run1) == 2
    assert all(t["run_number"] == 1 for t in trades_run1)

    # Execution 2 (run_number = 2)
    run2 = mgr.start_run(cfg)
    assert run2.run_number == 2
    run2.process_tick(130.0)
    run2.process_tick(140.0)  # profit exit 3

    trades_all = [t for t in mgr.get_all_trade_history() if t["run_id"] == "run_num_test"]
    assert len(trades_all) == 3
    # Newest trade is from run 2
    assert trades_all[0]["run_number"] == 2
    # Older trades are from run 1
    assert trades_all[1]["run_number"] == 1
    assert trades_all[2]["run_number"] == 1


def test_25_strike_lock_flicker_guard_simulation():
    """
    Acceptance Criteria 1 & 2:
    1. With an open ladder running on a locked strike, simulate spot flickering across strike boundaries.
       The active strike must not change.
    2. On flat state, simulate a flickering LTP near a boundary (flickering between 24200 and 24250).
       The new strike must ONLY lock in after it stabilizes for N consecutive ticks.
    """
    cfg = ScalperRunConfig(
        run_id="flicker_guard_test",
        instrument_name="NIFTY",
        option_type="CE",
        strike=24200,
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
        auto_restrike=True,
        flicker_guard_ticks=4,  # Configurable 4 ticks
    )
    run = ScalperRun(cfg)
    run.start()
    assert run.locked_strike == 24200

    # Fill an active slice at 150.0
    run.process_tick(150.0)
    assert len(run.active_slices) == 1

    # While slice is open, spot flickers wildly across strike boundaries: 24260 (ATM 24250), 24310 (ATM 24300), 24190 (ATM 24200)
    for spot in [24260.0, 24270.0, 24310.0, 24350.0, 24190.0, 24260.0]:
        s = run.handle_spot_update(spot)
        assert s == 24200
        assert run.locked_strike == 24200
        assert run.config.strike == 24200
        assert "24200" in run.config.contract_symbol

    # Close the slice by profit exit
    run.process_tick(160.0)
    assert len(run.active_slices) == 0

    # Now flat: Simulate spot flickering near boundary: 24224.0 (24200) <-> 24226.0 (24250)
    # Flickering back and forth should NEVER trigger relock because candidate resets on each flip!
    flicker_sequence = [24226.0, 24224.0, 24226.0, 24227.0, 24223.0, 24228.0, 24222.0]
    for spot in flicker_sequence:
        s = run.handle_spot_update(spot)
        assert s == 24200  # Stays at 24200!
        assert run.locked_strike == 24200
        assert run.config.strike == 24200

    # Now spot moves across boundary and STABILIZES for 4 consecutive ticks at 24260 (ATM 24250)
    assert run.handle_spot_update(24260.0) == 24200  # Tick 1: candidate 24250 (count 1 < 4)
    assert run.handle_spot_update(24262.0) == 24200  # Tick 2: candidate 24250 (count 2 < 4)
    assert run.handle_spot_update(24265.0) == 24200  # Tick 3: candidate 24250 (count 3 < 4)
    # Tick 4: reaches threshold of 4 consecutive ticks -> locks into 24250!
    assert run.handle_spot_update(24263.0) == 24250  # Tick 4: candidate 24250 (count 4 >= 4) -> RELOCKED!
    assert run.locked_strike == 24250
    assert run.config.strike == 24250
    assert "24250" in run.config.contract_symbol


def test_26_multi_slot_independent_strike_locks():
    """
    Verifies that locking/relocking on Slot 1 (Nifty) does not affect Slot 2 (Sensex) or Slot 3.
    Each slot operates fully independently.
    """
    mgr = RunManager(max_runs=3, state_file=":memory:")
    
    # Slot 1: Nifty on 24200
    cfg1 = ScalperRunConfig(
        run_id="run01",
        instrument_name="NIFTY",
        strike=24200,
        range_high=140.0,
        range_low=100.0,
        auto_restrike=True,
        flicker_guard_ticks=2,
    )
    # Slot 2: Sensex on 81000
    cfg2 = ScalperRunConfig(
        run_id="run02",
        instrument_name="SENSEX",
        strike=81000,
        range_high=250.0,
        range_low=100.0,
        slice_interval=30.0,
        profit_point=30.0,
        loss_point=30.0,
        auto_restrike=True,
        flicker_guard_ticks=2,
    )

    run1 = mgr.start_run(cfg1)
    run2 = mgr.start_run(cfg2)

    # Open slice in Slot 1 (Nifty)
    run1.process_tick(140.0)
    assert len(run1.active_slices) == 1

    # Sensex spot moves and relocks Slot 2 after 2 stable ticks at 81400
    run2.handle_spot_update(81380.0)  # Tick 1 (ATM 81400)
    run2.handle_spot_update(81390.0)  # Tick 2 (ATM 81400 -> relocks to 81400)
    assert run2.locked_strike == 81400
    assert run2.config.strike == 81400

    # Nifty slot 1 remains locked at 24200
    assert run1.locked_strike == 24200
    assert run1.config.strike == 24200


def test_27_strike_lock_audit_logging():
    """
    Verifies that every strike lock / relock event records:
    old_strike, new_strike, timestamp, and reason to the audit log.
    """
    cfg = ScalperRunConfig(
        run_id="audit_strike_test",
        instrument_name="NIFTY",
        option_type="CE",
        strike=24300,
        auto_restrike=True,
        flicker_guard_ticks=2,
    )
    run = ScalperRun(cfg)
    run.start()

    # Verify initial STRIKE_LOCKED event
    lock_events = [e for e in run.audit_events if e["event_type"] == "STRIKE_LOCKED"]
    assert len(lock_events) == 1
    assert lock_events[0]["details"]["locked_strike"] == 24300
    assert lock_events[0]["details"]["reason"] == "ladder_start"

    # Stabilize at 24500 (2 ticks)
    run.handle_spot_update(24510.0)
    run.handle_spot_update(24520.0)

    # Verify STRIKE_RELOCKED event
    relock_events = [e for e in run.audit_events if e["event_type"] == "STRIKE_RELOCKED"]
    assert len(relock_events) == 1
    details = relock_events[0]["details"]
    assert details["old_strike"] == 24300
    assert details["new_strike"] == 24500
    assert details["reason"] == "flat_ladder_restrike"
    assert details["consecutive_ticks"] == 2
    assert "timestamp" in relock_events[0]


def test_28_grid_rung_stepdown_entry_on_slippage_fill():
    """
    Verifies that when Slice A (Level 24.00) is filled at 22.35 (due to market slippage/open),
    a subsequent tick at 15.25/15.50 (which belongs to Level 16.00) correctly triggers
    a buy for Level 16.00 (Slice B) rather than waiting for 22.35 - 8 = 14.35.
    """
    cfg = ScalperRunConfig(
        run_id="slippage_test",
        range_high=40.0,
        range_low=8.0,
        slice_interval=8.0,
        profit_point=8.0,
        loss_point=8.0,
    )
    run = ScalperRun(cfg)
    run.start()

    # Tick 1 at 22.35: Enters Level 24.00 interval (16 < 22.35 <= 24) -> buys Slice A
    res1 = run.process_tick(22.35)
    assert len(res1["buys"]) == 1
    assert res1["buys"][0]["label"] == "A"
    assert res1["buys"][0]["level_price"] == 24.0
    assert res1["buys"][0]["fill_price"] == 22.35

    # Tick 2 at 18.00: Still in Level 24.00 interval (16 < 18 <= 24) -> no new buy
    res2 = run.process_tick(18.0)
    assert len(res2["buys"]) == 0
    assert len(run.active_slices) == 1

    # Tick 3 at 15.25: Enters Level 16.00 interval (8 < 15.25 <= 16) -> MUST buy Slice B at Level 16.00
    res3 = run.process_tick(15.25)
    assert len(res3["buys"]) == 1
    assert res3["buys"][0]["label"] == "B"
    assert res3["buys"][0]["level_price"] == 16.0
    assert res3["buys"][0]["fill_price"] == 15.25
    assert len(run.active_slices) == 2


def test_29_crossing_based_upper_level_buy_trigger():
    """
    Verifies crossing-based upper-level buy trigger:
    Ladder: 200, 192, 184, 176, 168, 160, 152, 144, 136, 128, 120 (interval=8, profit=8, loss=8)
    1. Initial fill at Level 168 with LTP 167.55 (Slice A).
    2. LTP rises to 169.45 (> 168.00). Next unused level above 168 is 176.
       Level 176 MUST fire immediately and fill at 169.45 (Slice B, profit target 177.45).
    3. LTP rises to 177.00:
       - Slice A (fill 167.55, target 175.55) exits at 177.00.
       - Since 177.00 > 176.00 (last_filled_level was 176), Level 184 fires and fills at 177.00 (Slice C).
    """
    cfg = ScalperRunConfig(
        run_id="crossing_test",
        range_high=200.0,
        range_low=120.0,
        slice_interval=8.0,
        profit_point=8.0,
        loss_point=8.0,
    )
    run = ScalperRun(cfg)
    run.start()

    # Step 1: Initial tick at 167.55 matches Level 168 (160 < 167.55 <= 168)
    res1 = run.process_tick(167.55)
    assert len(res1["buys"]) == 1
    assert res1["buys"][0]["label"] == "A"
    assert res1["buys"][0]["level_price"] == 168.0
    assert res1["buys"][0]["fill_price"] == 167.55
    assert res1["buys"][0]["profit_target"] == 175.55
    assert res1["buys"][0]["is_crossing_buy"] is False
    assert run.last_filled_level == 168.0

    # Step 2: Tick rises to 169.45 (above 168.00, below 176.00)
    # MUST trigger crossing buy for next unused level (176.00) at 169.45
    res2 = run.process_tick(169.45)
    assert len(res2["buys"]) == 1
    assert res2["buys"][0]["label"] == "B"
    assert res2["buys"][0]["level_price"] == 176.0
    assert res2["buys"][0]["fill_price"] == 169.45
    assert res2["buys"][0]["profit_target"] == round(169.45 + 8.0, 4)  # 177.45
    assert res2["buys"][0]["is_crossing_buy"] is True
    assert run.last_filled_level == 176.0
    assert len(run.active_slices) == 2

    # Step 3: Tick at 170.00 (between 169.45 and 176.00) -> no new buy
    res3 = run.process_tick(170.00)
    assert len(res3["buys"]) == 0
    assert len(res3["sells"]) == 0

    # Step 4: Tick rises to 177.00
    # Slice A (target 175.55) exits with profit at 177.00!
    # Upward crossing above 176.00 fires next unused level (184.00) at 177.00!
    res4 = run.process_tick(177.00)
    assert len(res4["sells"]) == 1
    assert res4["sells"][0]["label"] == "A"
    assert res4["sells"][0]["level_price"] == 168.0
    assert res4["sells"][0]["exit_price"] == 177.00

    assert len(res4["buys"]) == 1
    assert res4["buys"][0]["label"] == "C"
    assert res4["buys"][0]["level_price"] == 184.0
    assert res4["buys"][0]["fill_price"] == 177.00
    assert res4["buys"][0]["profit_target"] == round(177.00 + 8.0, 4)  # 185.00
    assert res4["buys"][0]["is_crossing_buy"] is True
    assert run.last_filled_level == 184.0


def test_30_multi_level_jump_and_descending_buy():
    """
    Verifies:
    1. Multi-level upward jump: price jumps from 168 to 195 (skipping 176, 184, 192).
       Only the single next unused level (176) fires at 195.00.
    2. Subsequent descending move drops below lowest active slice (168) to 159.00 -> fills 160.00 as standard descending buy.
    """
    cfg = ScalperRunConfig(
        run_id="multi_jump_test",
        range_high=200.0,
        range_low=120.0,
        slice_interval=8.0,
        profit_point=8.0,
        loss_point=8.0,
    )
    run = ScalperRun(cfg)
    run.start()

    # Fill level 168 at 168.00
    run.process_tick(168.00)
    assert run.last_filled_level == 168.0

    # Price jumps to 195.00 (skipping 176, 184, 192)
    # Slice A (target 176.00) sells at 195.00!
    # Crossing trigger MUST fill only the single next unused level (176.00) at 195.00 (Slice B)
    res = run.process_tick(195.00)
    assert len(res["sells"]) == 1
    assert res["sells"][0]["label"] == "A"

    assert len(res["buys"]) == 1
    assert res["buys"][0]["label"] == "B"
    assert res["buys"][0]["level_price"] == 176.0
    assert res["buys"][0]["fill_price"] == 195.00
    assert run.last_filled_level == 176.0

    # Unused upper levels 184, 192, 200 remain UNUSED
    s_184 = next(s for s in run.grid_ladder if s.level_price == 184.0)
    assert s_184.status == SliceStatus.UNUSED

    # Price drops to 159.00 -> enters level 160 interval (152 < 159 <= 160)
    # Active slices is only B (176.0). 160 <= 176 - 8 (168), so level 160 fills!
    res_down = run.process_tick(159.00)
    assert len(res_down["buys"]) == 1
    assert res_down["buys"][0]["label"] == "C"
    assert res_down["buys"][0]["level_price"] == 160.0
    assert res_down["buys"][0]["fill_price"] == 159.00
    assert res_down["buys"][0]["is_crossing_buy"] is False
    assert run.last_filled_level == 160.0


def test_31_boundary_oscillation_does_not_buy_upper_level():
    """
    Exact simulation of user scenario:
    Ladder: 200, 192, 184, 176, 168, 160, 152, 144, 136, 128, 120 (interval=8)
    1. Fill Level 160 at 152.20 (Slice A).
    2. Price drops to 151.95 -> fills Level 152 as Slice B.
    3. Price bounces back from 151.95 to 152.20 (below highest active 160.00).
       MUST NOT buy Level 168! Level 168 remains UNUSED.
    4. Price rises to 160.50 (above 160.00).
       NOW Level 168 fires and buys at 160.50!
    """
    cfg = ScalperRunConfig(
        run_id="boundary_oscillation_test",
        range_high=200.0,
        range_low=120.0,
        slice_interval=8.0,
        profit_point=8.0,
        loss_point=8.0,
    )
    run = ScalperRun(cfg)
    run.start()

    # 1. Fill Level 160 at 152.20
    res1 = run.process_tick(152.20)
    assert len(res1["buys"]) == 1
    assert res1["buys"][0]["level_price"] == 160.0
    assert res1["buys"][0]["label"] == "A"

    # 2. Price drops to 151.95 -> fills Level 152 (151.95 <= 160 - 8 = 152)
    res2 = run.process_tick(151.95)
    assert len(res2["buys"]) == 1
    assert res2["buys"][0]["level_price"] == 152.0
    assert res2["buys"][0]["label"] == "B"

    # 3. Price bounces back to 152.20
    # Highest active is 160.00. 152.20 is NOT > 160.00!
    # Lowest active is 152.00. 152.20 is NOT <= 144.00!
    # MUST NOT BUY Level 168!
    res3 = run.process_tick(152.20)
    assert len(res3["buys"]) == 0
    assert len(run.active_slices) == 2
    s_168 = next(s for s in run.grid_ladder if s.level_price == 168.0)
    assert s_168.status == SliceStatus.UNUSED

    # Further ticks in between: 155.00, 158.00, 159.50 -> NO BUYS
    assert len(run.process_tick(155.00)["buys"]) == 0
    assert len(run.process_tick(158.00)["buys"]) == 0
    assert len(run.process_tick(159.50)["buys"]) == 0

    # 4. Price crosses above 160.00 to 160.50!
    # NOW Level 168 fires as upward crossing!
    res4 = run.process_tick(160.50)
    assert len(res4["buys"]) == 1
    assert res4["buys"][0]["level_price"] == 168.0
    assert res4["buys"][0]["fill_price"] == 160.50
    assert res4["buys"][0]["is_crossing_buy"] is True


def test_35_gross_and_today_realized_pnl():
    """
    Verifies that gross_realized_pnl shows overall accumulated realized P&L,
    while today_realized_pnl strictly filters for trades exited today in IST.
    """
    import datetime
    from slice_trading_engine import IST, TradeRecord

    cfg = ScalperRunConfig(
        run_id="test_gross_pnl",
        instrument_name="NIFTY",
        strike=24200,
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=10.0,
        qty_per_slice_lots=1,
    )
    run = ScalperRun(cfg)

    # 1. Simulate a trade exited yesterday (historical trade)
    yesterday_ts = (datetime.datetime.now(IST) - datetime.timedelta(days=1)).isoformat()
    old_trade = TradeRecord(
        trade_id="TRD-OLD1",
        run_id="test_gross_pnl",
        label="A",
        level_price=150.0,
        fill_price=150.0,
        exit_price=160.0,
        quantity=65,
        pnl_points=10.0,
        pnl_rupees=650.0,
        exit_reason="PROFIT_TARGET",
        filled_at=yesterday_ts,
        exited_at=yesterday_ts,
        contract_symbol="NIFTY 24200 CE",
        entry_time=yesterday_ts,
        exit_time=yesterday_ts,
    )

    # 2. Simulate a trade exited today
    today_ts = datetime.datetime.now(IST).isoformat()
    today_trade = TradeRecord(
        trade_id="TRD-TODAY1",
        run_id="test_gross_pnl",
        label="B",
        level_price=140.0,
        fill_price=140.0,
        exit_price=150.0,
        quantity=65,
        pnl_points=10.0,
        pnl_rupees=650.0,
        exit_reason="PROFIT_TARGET",
        filled_at=today_ts,
        exited_at=today_ts,
        contract_symbol="NIFTY 24200 CE",
        entry_time=today_ts,
        exit_time=today_ts,
    )

    run.trade_history = [today_trade, old_trade]
    run.accumulated_realized_pnl = 1300.0

    status = run.get_status()
    assert status["gross_realized_pnl"] == 1300.0
    assert status["accumulated_realized_pnl"] == 1300.0
    assert status["today_realized_pnl"] == 650.0


def test_36_manual_strike_override_config_and_lock():
    """
    Verifies that manual strike override correctly configures ScalperRunConfig,
    locks the manual strike on run start, and preserves it through status serialization.
    """
    cfg = ScalperRunConfig(
        run_id="test_manual_strike",
        instrument_name="NIFTY",
        option_type="CE",
        strike=24600,  # Manually selected strike (e.g. spot was 24200, manual override is 24600)
        strike_source="manual",
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=10.0,
        qty_per_slice_lots=1,
    )
    assert cfg.strike == 24600
    assert cfg.strike_source == "manual"
    assert "24600" in cfg.contract_symbol

    run = ScalperRun(cfg)
    run.start()
    assert run.locked_strike == 24600

    status = run.get_status()
    assert status["strike"] == 24600
    assert status["strike_source"] == "manual"
    assert status["locked_strike"] == 24600
    assert "24600" in status["contract_symbol"]

    # Deserialization check
    d = run.to_dict()
    restored = ScalperRun.from_dict(d)
    assert restored.config.strike == 24600
    assert restored.config.strike_source == "manual"
    assert restored.locked_strike == 24600


def test_37_available_strikes_generation():
    """
    Verifies available strikes calculation for NIFTY (step 50) and SENSEX (step 100).
    """
    from angel_one_service import AngelOneFeed
    svc = AngelOneFeed()

    # NIFTY test: around 24520 -> ATM 24500
    nifty_strikes = svc.get_available_strikes(24520.0, instrument="NIFTY", count_each_side=10)
    assert len(nifty_strikes) >= 15
    assert 24500 in nifty_strikes
    assert all(s % 50 == 0 for s in nifty_strikes)
    assert nifty_strikes == sorted(nifty_strikes)

    # SENSEX test: around 81230 -> ATM 81200
    sensex_strikes = svc.get_available_strikes(81230.0, instrument="SENSEX", count_each_side=10)
    assert len(sensex_strikes) >= 15
    assert 81200 in sensex_strikes
    assert all(s % 100 == 0 for s in sensex_strikes)
    assert sensex_strikes == sorted(sensex_strikes)


def test_38_strike_change_guard_with_open_positions():
    """
    Verifies that attempting to change strike while ladder has open positions is blocked.
    """
    from fastapi.testclient import TestClient
    from app import app, get_client_session
    from auth_service import auth_service

    client = TestClient(app)
    # Authenticate admin
    token = auth_service.create_access_token("admin")
    headers = {"Authorization": f"Bearer {token}"}

    session = get_client_session("admin")
    run = session.run_manager.get_run("run01")
    assert run is not None

    # Start run and create an active open slice
    cfg = ScalperRunConfig(
        run_id="run01",
        instrument_name="NIFTY",
        strike=24200,
        strike_source="auto",
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=10.0,
        qty_per_slice_lots=1,
    )
    run = session.run_manager.start_run(cfg)
    run.process_tick(150.0)  # Fills level 150
    assert len(run.active_slices) == 1

    # 1. Attempt to change strike via /api/runs/run01/strike
    res1 = client.post("/api/runs/run01/strike", json={"strike": 24500, "strike_source": "manual"}, headers=headers)
    assert res1.status_code == 400
    assert "Cannot change strike while ladder has open positions" in res1.json()["error"]

    # 2. Attempt to start run with different strike while open positions exist
    res2 = client.post("/api/runs/start", json={
        "run_id": "run01",
        "instrument_name": "NIFTY",
        "strike": 24500,
        "strike_source": "manual",
        "range_high": 150.0,
        "range_low": 100.0,
        "slice_interval": 10.0,
        "profit_point": 10.0,
        "loss_point": 10.0,
    }, headers=headers)
    assert res2.status_code == 400
    assert "Cannot change strike while ladder has open positions" in res2.json()["error"]

    # 3. Clean up - stop run
    run.stop()


def test_manual_strike_pinned_across_spot_updates():
    """Verifies that when strike_source == 'manual', spot updates do not overwrite the strike."""
    cfg = ScalperRunConfig(
        run_id="run_manual_test",
        instrument_name="NIFTY",
        strike=23750,
        strike_source="manual",
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=10.0,
        qty_per_slice_lots=1,
    )
    run = ScalperRun(cfg)
    assert run.config.strike == 23750
    assert run.config.strike_source == "manual"

    # Spot shifts significantly to 24900 (ATM would be 24900)
    returned_strike = run.handle_spot_update(24900.0)
    assert returned_strike == 23750
    assert run.config.strike == 23750
    assert run.spot_ltp == 24900.0

    # Another spot shift to 25120 (ATM would be 25100)
    returned_strike2 = run.handle_spot_update(25120.0)
    assert returned_strike2 == 23750
    assert run.config.strike == 23750

    # Start run with manual strike: confirmed locked_strike is the manual strike
    run.start()
    assert run.is_active is True
    assert run.locked_strike == 23750

    # When active and spot changes, manual strike remains strictly locked
    returned_strike3 = run.handle_spot_update(25200.0)
    assert returned_strike3 == 23750
    assert run.locked_strike == 23750
    run.stop()


def test_39_stop_loss_hit_does_not_start_another_ladder():
    """
    Verifies that when a stop loss is hit:
    1. All active slices exit with LOSS_EXIT.
    2. The run becomes inactive (run.is_active is False).
    3. The ladder does not restart on its own; subsequent ticks do not open any new slices.
    """
    cfg = ScalperRunConfig(
        run_id="no_auto_restart_run",
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=10.0,  # SL will trigger at range_bottom 100 - 10 = 90.0
    )
    run = ScalperRun(cfg)
    run.start()

    # Buy slice at 140
    act1 = run.process_tick(140.0)
    assert len(act1["buys"]) == 1
    assert len(run.active_slices) == 1
    assert run.is_active is True

    # Price drops to stop loss at 90.0
    act_sl = run.process_tick(90.0)
    assert act_sl["loss_exit"] is True
    assert act_sl["cycle_reset"] is False
    assert run.is_active is False
    assert len(run.active_slices) == 0

    # Ensure run recorded the exit
    assert len(run.trade_history) == 1
    assert run.trade_history[0].exit_reason == "LOSS_EXIT"

    # Multiple ticks arrive across the entire range
    for tick in [140.0, 130.0, 120.0, 110.0, 100.0, 95.0, 150.0]:
        act = run.process_tick(tick)
        assert len(act["buys"]) == 0
        assert len(act["sells"]) == 0
        assert len(run.active_slices) == 0

    # Run remains inactive and no trades opened on its own
    assert run.is_active is False
    assert len(run.trade_history) == 1
















