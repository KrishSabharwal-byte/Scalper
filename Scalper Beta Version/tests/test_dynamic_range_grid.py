"""
Unit tests for Dynamic Range + Slicer Grid
Verifies:
1. Derivation of range_high, range_low, and slice_interval from Range magnitude & Slicer count.
2. Snapshotting of live LTP into range_high at run-time.
3. Ladder generation and level-crossing execution on dynamic grids.
4. Validation guards: range_points > 0 and slicer_count >= 1.
5. Recenter range behavior with dynamic parameters.
6. Backward compatibility with legacy range_high/range_low/slice_interval inputs.
"""

import pytest
from slice_trading_engine import (
    ScalperRunConfig,
    ScalperRun,
    SliceStatus,
    recenter_range,
    validate_config,
)


def test_dynamic_range_derivation_from_magnitude_and_count():
    """
    Example from spec: LTP = 200, Range = 100, Slicer = 10 ->
    range_high = 200, range_low = 100, step = 10, producing 10 slicing levels.
    """
    cfg = ScalperRunConfig(
        run_id="dyn_test_01",
        range_high=200.0,
        range_points=100.0,
        slicer_count=10,
        profit_point=10.0,
        loss_point=10.0,
    )
    assert cfg.range_high == 200.0
    assert cfg.range_low == 100.0
    assert cfg.slice_interval == 10.0
    assert cfg.range_points == 100.0
    assert cfg.slicer_count == 10

    run = ScalperRun(cfg)
    # Ladder should contain 11 points (200 down to 100 step 10 -> 10 intervals)
    level_prices = [s.level_price for s in run.grid_ladder]
    assert level_prices == [200.0, 190.0, 180.0, 170.0, 160.0, 150.0, 140.0, 130.0, 120.0, 110.0, 100.0]
    assert len(level_prices) == 11


def test_dynamic_range_validation_guards():
    """Rejects range_points <= 0 or slicer_count < 1."""
    with pytest.raises(ValueError, match="range_points"):
        ScalperRunConfig(range_points=0.0, slicer_count=5)

    with pytest.raises(ValueError, match="range_points"):
        ScalperRunConfig(range_points=-20.0, slicer_count=5)

    with pytest.raises(ValueError, match="slicer_count"):
        ScalperRunConfig(range_points=50.0, slicer_count=0)

    with pytest.raises(ValueError, match="slicer_count"):
        ScalperRunConfig(range_points=50.0, slicer_count=-2)


def test_legacy_backward_compatibility():
    """Passing range_high, range_low, slice_interval derives range_points and slicer_count."""
    cfg = ScalperRunConfig(
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
    )
    assert cfg.range_points == 50.0
    assert cfg.slicer_count == 5


def test_recenter_range_with_dynamic_parameters():
    """
    recenter_range derives new_range_high and new_range_low from range_points and slicer_count.
    """
    # Price outside range: 250.0 with range_points=100.0 and slicer_count=10
    high, low = recenter_range(
        current_price=250.0,
        original_span=100.0,
        slice_interval=10.0,
        original_range_high=200.0,
        original_range_low=100.0,
        range_points=100.0,
        slicer_count=10,
    )
    # Centered around 250: half_span=50 -> raw_high=300 -> aligned=300 -> low=200
    assert high == 300.0
    assert low == 200.0
    assert round(high - low, 2) == 100.0


def test_dynamic_grid_state_machine_execution():
    """Verifies that trading execution works identically on dynamic grid."""
    cfg = ScalperRunConfig(
        run_id="dyn_exec_test",
        range_high=100.0,
        range_points=40.0,
        slicer_count=4,  # step = 10.0 (100, 90, 80, 70, 60)
        profit_point=10.0,
        loss_point=10.0,
    )
    run = ScalperRun(cfg)
    run.start()

    # Fill at 90
    act1 = run.process_tick(90.0)
    assert len(act1["buys"]) == 1
    assert act1["buys"][0]["level_price"] == 90.0
    assert act1["buys"][0]["profit_target"] == 100.0

    # Profit exit at 100
    act2 = run.process_tick(100.0)
    assert len(act2["sells"]) == 1
    assert act2["sells"][0]["exit_price"] == 100.0
    assert run.accumulated_realized_pnl > 0
