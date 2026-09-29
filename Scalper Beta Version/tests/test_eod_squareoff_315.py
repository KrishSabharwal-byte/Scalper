import datetime
import pytest
from slice_trading_engine import ScalperRun, ScalperRunConfig, SliceStatus, SliceExitReason, IST


def test_default_cutoff_time_is_322_pm():
    cfg = ScalperRunConfig(
        run_id="run01",
        instrument_name="NIFTY",
        option_type="CE",
        strike=24200,
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=10.0,
    )
    assert cfg.cutoff_time_ist == "15:22"

    from slice_trading_engine import RunManager
    mgr = RunManager(max_runs=4, state_file=":memory:")
    assert mgr.runs["run01"].config.cutoff_time_ist == "15:22"
    assert mgr.runs["run01"].config.auto_eod_squareoff is True


def test_eod_squareoff_triggers_at_1522_ist():
    recorded_trades = []

    def trade_cb(trade_data, run_config=None):
        recorded_trades.append(trade_data)

    cfg = ScalperRunConfig(
        run_id="run01",
        instrument_name="NIFTY",
        option_type="CE",
        strike=24200,
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=10.0,
        cutoff_time_ist="15:22",
        auto_eod_squareoff=True,
    )
    run = ScalperRun(cfg, trade_callback=trade_cb)
    run.start()

    # Enter slices at 150 and 140 during morning market hours
    market_time = datetime.time(10, 0, 0)
    run.process_tick(150.0, current_time_ist=market_time)
    run.process_tick(140.0, current_time_ist=market_time)
    assert len(run.active_slices) == 2

    # Time before cutoff (15:21:59) -> Should NOT square off
    time_before = datetime.time(15, 21, 59)
    assert run.check_eod_cutoff(time_before) is False
    assert len(run.active_slices) == 2
    assert run.eod_squared_off is False

    # Time at cutoff (15:22:00) -> Must square off all open trades
    time_cutoff = datetime.time(15, 22, 0)
    squared_off = run.check_eod_cutoff(time_cutoff)
    assert squared_off is True
    assert run.eod_squared_off is True
    assert run.is_active is False
    assert len(run.active_slices) == 0

    # Verify all open slices exited with EOD_SQUAREOFF reason
    assert len(run.trade_history) == 2
    for t in run.trade_history:
        assert t.exit_reason == SliceExitReason.EOD_SQUAREOFF.value
        assert t.exit_price == 140.0

    # Verify trades sent to callback (which persists to MongoDB)
    assert len(recorded_trades) == 2


def test_inactive_slot_with_open_slices_is_squared_off_at_1522():
    """Even if slot was paused/stopped, any open slices must be squared off at 3:22 PM."""
    cfg = ScalperRunConfig(
        run_id="run02",
        instrument_name="SENSEX",
        option_type="PE",
        strike=81000,
        range_high=200.0,
        range_low=150.0,
        slice_interval=25.0,
        profit_point=25.0,
        loss_point=25.0,
        cutoff_time_ist="15:22",
        auto_eod_squareoff=True,
    )
    run = ScalperRun(cfg)
    run.start()
    run.process_tick(200.0, current_time_ist=datetime.time(10, 0, 0))
    assert len(run.active_slices) == 1

    # Simulate slot being stopped while keeping active slices open
    run.is_active = False

    time_cutoff = datetime.time(15, 22, 0)
    assert run.check_eod_cutoff(time_cutoff) is True
    assert len(run.active_slices) == 0
    assert run.eod_squared_off is True
    assert run.trade_history[0].exit_reason == SliceExitReason.EOD_SQUAREOFF.value


def test_custom_cutoff_time_squareoff():
    """Verify custom cutoff times (e.g. 15:35, 15:15) trigger exactly at their respective times."""
    cfg = ScalperRunConfig(
        run_id="run03",
        instrument_name="NIFTY",
        option_type="CE",
        strike=24200,
        range_high=150.0,
        range_low=100.0,
        slice_interval=10.0,
        profit_point=10.0,
        loss_point=10.0,
        cutoff_time_ist="15:35",
        auto_eod_squareoff=True,
    )
    run = ScalperRun(cfg)
    run.start()
    run.process_tick(150.0, current_time_ist=datetime.time(10, 0, 0))
    assert len(run.active_slices) == 1

    # At 15:22 -> Should NOT square off because cutoff is 15:35
    assert run.check_eod_cutoff(datetime.time(15, 22, 0)) is False
    assert len(run.active_slices) == 1

    # At 15:34:59 -> Should NOT square off
    assert run.check_eod_cutoff(datetime.time(15, 34, 59)) is False
    assert len(run.active_slices) == 1

    # At 15:35:00 -> Must square off!
    assert run.check_eod_cutoff(datetime.time(15, 35, 0)) is True
    assert len(run.active_slices) == 0
    assert run.eod_squared_off is True


def test_set_run_cutoff_time_api():
    """Verify set_run_cutoff_time API dynamically updates cutoff time."""
    from fastapi.testclient import TestClient
    from app import app
    from auth_service import auth_service

    token = auth_service.create_access_token("admin")
    client = TestClient(app)

    res = client.post(
        "/runs/run01/cutoff_time",
        headers={"Authorization": f"Bearer {token}"},
        json={"cutoff_time_ist": "15:30"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["cutoff_time_ist"] == "15:30"
    assert data["run"]["config"]["cutoff_time_ist"] == "15:30"
