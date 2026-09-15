import os
import pytest
from fastapi.testclient import TestClient
import io

from app import app
from auth_service import auth_service


@pytest.fixture
def auth_client():
    client = TestClient(app)
    cid = "test_astro_user"
    if not auth_service.get_user(cid):
        auth_service.create_user(cid, "Password123!", is_active=True, role="trader")
    token = auth_service.create_access_token(client_id=cid)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def test_astro_api_upload_status_toggle_delete(auth_client):
    csv_content = (
        "Date,Time,U/D Logic\n"
        "2026-02-01,10:00,Upside\n"
        "2026-02-01,10:05,Upside\n"
        "2026-02-01,10:10,Upside\n"
        "2026-02-01,10:15,Downside\n"
    )

    # 1. Upload CSV
    file_bytes = io.BytesIO(csv_content.encode("utf-8"))
    res_upload = auth_client.post(
        "/api/astro/upload",
        files={"file": ("astro_weekly.csv", file_bytes, "text/csv")},
    )
    assert res_upload.status_code == 200, res_upload.text
    data_upload = res_upload.json()
    assert data_upload["status"] == "success"
    assert data_upload["filename"] == "astro_weekly.csv"
    assert data_upload["row_count"] == 4
    file_id = data_upload["file_id"]

    # 2. List Files
    res_list = auth_client.get("/api/astro/files")
    assert res_list.status_code == 200
    files_data = res_list.json()
    assert files_data["count"] >= 1
    assert any(f["file_id"] == file_id for f in files_data["files"])

    # 3. Toggle Astro Auto Trigger
    res_toggle = auth_client.post(
        "/api/astro/toggle",
        json={"slot_id": "run01", "enabled": True},
    )
    assert res_toggle.status_code == 200
    assert res_toggle.json()["enabled"] is True

    # 4. Get Status
    res_status = auth_client.get("/api/astro/status")
    assert res_status.status_code == 200
    status_data = res_status.json()
    assert status_data["has_active_file"] is True
    assert status_data["active_file"]["filename"] == "astro_weekly.csv"
    assert "preview" in status_data

    # 5. Delete file
    res_del = auth_client.delete(f"/api/astro/files/{file_id}")
    assert res_del.status_code == 200
    assert res_del.json()["status"] == "success"

    # Verify deleted
    res_status_after = auth_client.get("/api/astro/status")
    assert res_status_after.status_code == 200
    assert res_status_after.json()["has_active_file"] is False


def test_astro_multi_slot_call_and_put_dispatch(auth_client):
    """
    Verifies that when a Call slot is already open for NIFTY,
    a subsequent downside/upside signal automatically launches in sequential idle slots (Slot 1 -> Slot 2),
    enabling concurrent multi-slot trading.
    """
    from app import get_client_session
    from slice_trading_engine import ScalperRunConfig

    session = get_client_session("test_astro_user")

    # 1. Arm all slots for Astro auto-execution
    res_toggle = auth_client.post(
        "/api/astro/toggle",
        json={"slot_id": "all", "enabled": True},
    )
    assert res_toggle.status_code == 200
    assert all(session.astro_auto_trigger.values())

    # 2. Simulate slot 1 (run01) first takes Downside (Put)
    put_cfg = ScalperRunConfig(
        run_id="run01",
        instrument_name="NIFTY",
        option_type="PE",
        strike=24200,
        range_high=150.0,
        range_low=110.0,
        slice_interval=8.0,
        profit_point=8.0,
        loss_point=8.0,
    )
    run01 = session.run_manager.start_run(put_cfg)
    assert run01.is_active is True
    assert run01.config.option_type == "PE"

    # 3. Simulate Astro upside signal arrives for NIFTY (BUY CE)
    # The session checks for idle slots and should dispatch CE sequentially to Slot 2 (run02)
    idle_slots = [
        r for r in session.run_manager.runs.values()
        if not r.is_active and len(r.active_slices) == 0
    ]
    assert len(idle_slots) >= 1

    # Sort idle slots in sequential order
    idle_slots.sort(key=lambda r: int(''.join(filter(str.isdigit, r.run_id)) or '999'))
    selected_run = idle_slots[0]
    assert selected_run.run_id == "run02"  # Must be Slot 2!

    # Start the Call in Slot 2
    call_cfg = ScalperRunConfig(
        run_id=selected_run.run_id,
        instrument_name="NIFTY",
        option_type="CE",
        strike=24200,
        range_high=140.0,
        range_low=100.0,
        slice_interval=8.0,
        profit_point=8.0,
        loss_point=8.0,
    )
    run_call = session.run_manager.start_run(call_cfg)
    assert run_call.is_active is True
    assert run_call.config.option_type == "CE"

    # 4. Both run01 (PE) in Slot 1 and run02 (CE) in Slot 2 are active concurrently!
    assert session.run_manager.get_run("run01").is_active is True
    assert session.run_manager.get_run("run01").config.option_type == "PE"
    assert session.run_manager.get_run("run02").is_active is True
    assert session.run_manager.get_run("run02").config.option_type == "CE"


def test_astro_auto_trigger_starts_only_current_active_slot(auth_client):
    """
    Verifies that auto-trigger only targets and starts the slot where the user currently is (active_run_id),
    and does not start both Sensex and Nifty or other slots.
    """
    from app import get_client_session
    import asyncio

    session = get_client_session("test_astro_user")

    # Stop any active runs
    for r in session.run_manager.runs.values():
        r.is_active = False
        r.active_slices = []

    # Configure slot 1 as NIFTY and slot 2 as SENSEX
    session.run_manager.runs["run01"].config.instrument_name = "NIFTY"
    session.run_manager.runs["run02"].config.instrument_name = "SENSEX"
    session.run_manager.active_run_id = "run01"

    import datetime
    today_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")

    times_list = [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in (0, 15, 30, 45)]
    rows = [f"{today_str},{t},Upside" for t in times_list]
    csv_content = "Date,Time,Direction\n" + "\n".join(rows) + "\n"
    file_bytes = io.BytesIO(csv_content.encode("utf-8"))
    auth_client.post(
        "/api/astro/upload",
        files={"file": ("active_slot_test.csv", file_bytes, "text/csv")},
    )

    # Arm auto-execution for run01
    auth_client.post(
        "/api/astro/toggle",
        json={"slot_id": "run01", "enabled": True},
    )

    # Run feed cycle
    asyncio.run(session.run_live_feed_cycle())

    # Slot 1 (where user is) should be started
    assert session.run_manager.get_run("run01").is_active is True
    assert session.run_manager.get_run("run01").config.instrument_name == "NIFTY"

    # Slot 2 (SENSEX) must NOT be started!
    assert session.run_manager.get_run("run02").is_active is False


def test_astro_auto_trigger_disarm_stays_off(auth_client):
    """
    Verifies that when auto-trigger is turned off for active slot,
    it stays off and is not considered armed.
    """
    from app import get_client_session

    session = get_client_session("test_astro_user")

    # Turn off run01 while run02 is true
    session.astro_auto_trigger["run02"] = True
    auth_client.post(
        "/api/astro/toggle",
        json={"slot_id": "run01", "enabled": False},
    )

    assert session.astro_auto_trigger.get("run01") is False

    # Check status endpoint
    res = auth_client.get("/api/astro/status")
    assert res.status_code == 200
    auto_map = res.json()["auto_trigger_by_slot"]
    assert auto_map.get("run01") is False


def test_manual_slice_exit_api(auth_client):
    """Verifies manually exiting a single open active slice via /api/runs/{run_id}/slices/exit."""
    from app import get_client_session
    from slice_trading_engine import ScalperRunConfig

    session = get_client_session("test_astro_user")
    cfg = ScalperRunConfig(
        run_id="run01",
        instrument_name="NIFTY",
        option_type="CE",
        strike=24250,
        range_points=40.0,
        slicer_count=5,
        range_high=140.0,
        range_low=100.0,
        slice_interval=8.0,
        profit_point=8.0,
        loss_point=8.0,
        qty_per_slice_lots=1,
    )
    run = session.run_manager.start_run(cfg)
    run.process_tick(140.0)  # Fills Slice A @ 140.0
    assert len(run.active_slices) == 1
    slice_a = run.active_slices[0]
    assert slice_a.label == "A"

    # Call manual exit API
    res = auth_client.post(
        "/api/runs/run01/slices/exit",
        json={"slice_id": "A"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert len(run.active_slices) == 0
    assert len(run.trade_history) >= 1
    assert run.trade_history[0].label == "A"
    assert run.trade_history[0].exit_reason == "MANUAL"


