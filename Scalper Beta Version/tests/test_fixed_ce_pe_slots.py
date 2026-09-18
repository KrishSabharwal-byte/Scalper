import os
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app import app
from auth_service import auth_service
from slice_trading_engine import (
    RunManager,
    ScalperRun,
    ScalperRunConfig,
    SLOT_CANONICAL_MAP,
    SLOT_LEGACY_MAP,
)
from client_session import ClientSession


@pytest.fixture
def auth_client():
    client = TestClient(app)
    cid = "test_slots_trader"
    if not auth_service.get_user(cid):
        auth_service.create_user(cid, "Password123!", is_active=True, role="trader")
    token = auth_service.create_access_token(client_id=cid)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


# ============================================================================
# 1. SLOT CANONICAL MAPPING & BIDIRECTIONAL LOOKUP TESTS
# ============================================================================

def test_slot_canonical_bidirectional_mapping():
    """Verify RunManager creates 4 slots accessible by legacy and canonical IDs."""
    rm = RunManager()
    assert len(rm.runs) == 4

    # Check key existence
    for legacy_id, canonical_code in SLOT_CANONICAL_MAP.items():
        assert legacy_id in rm.runs
        assert canonical_code in rm.runs
        run_by_legacy = rm.runs[legacy_id]
        run_by_canonical = rm.runs[canonical_code]
        assert run_by_legacy is run_by_canonical
        assert run_by_legacy.slot_id == canonical_code
        assert run_by_legacy.slot_code == canonical_code

    # Check instrument and option type defaults
    assert rm.runs["N-C"].instrument_name == "NIFTY"
    assert rm.runs["N-C"].option_type == "CE"

    assert rm.runs["N-P"].instrument_name == "NIFTY"
    assert rm.runs["N-P"].option_type == "PE"

    assert rm.runs["S-C"].instrument_name == "SENSEX"
    assert rm.runs["S-C"].option_type == "CE"

    assert rm.runs["S-P"].instrument_name == "SENSEX"
    assert rm.runs["S-P"].option_type == "PE"


def test_slot_status_property():
    """Verify ScalperRun.slot_status reflects EMPTY, OPEN, CLOSED accurately."""
    run = ScalperRun("run01", ScalperRunConfig())
    run.slot_id = "N-C"
    
    # Initially inactive with no slices
    assert run.slot_status == "EMPTY"

    # Active with open slices -> OPEN
    run.is_active = True
    run.active_slices = [{"slice_id": "slice_1", "status": "FILLED"}]
    assert run.slot_status == "OPEN"

    # Inactive but has trade history -> CLOSED
    run.is_active = False
    run.active_slices = []
    run.trade_history = [{"trade_id": "trade_1", "pnl": 500.0}]
    assert run.slot_status == "CLOSED"


# ============================================================================
# 2. DIRECTIONAL SIGNAL ROUTING & SLOT REUSE TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_directional_signal_routing():
    """Verify DOWNSIDE routes to N-P & S-P, UPSIDE routes to N-C & S-C, opposite untouched."""
    session = ClientSession(client_id="test_route_client")
    session.astro_auto_trigger = {"all": True}

    # Mock feed contract resolution and LTP fetching
    session.angel_feed.resolve_contract = MagicMock(return_value={
        "symbol": "MOCK_CONTRACT",
        "token": "12345",
        "exch_seg": "NFO",
    })
    session.angel_feed.fetch_option_ltp = MagicMock(return_value=120.0)

    dummy_csv = "Date,Time,U/D Logic\n2026-02-01,10:00,Upside\n"

    # 1. Evaluate DOWNSIDE signal
    with patch("client_session.astro_signal_engine.evaluate_cluster") as mock_eval:
        mock_eval.return_value = {
            "status": "APPROVED",
            "option_type": "PE",
            "signal": "DOWNSIDE",
            "condition_3_direction": "BUY PE",
            "cluster_rows": [],
            "action": "BUY PE",
        }
        res1 = await session.evaluate_fixed_slots(
            batch_ltps={"12345": 120.0},
            nifty_spot=24200.0,
            sensex_spot=81000.0,
            active_astro_content=dummy_csv,
        )

        # Must trigger N-P and S-P only
        assert res1["signal"] == "DOWNSIDE"
        assert "N-P" in res1["triggered_slots"]
        assert "S-P" in res1["triggered_slots"]
        assert "N-C" not in res1["triggered_slots"]
        assert "S-C" not in res1["triggered_slots"]

        assert session.run_manager.runs["N-P"].slot_status == "OPEN"
        assert session.run_manager.runs["S-P"].slot_status == "OPEN"
        assert session.run_manager.runs["N-C"].slot_status == "EMPTY"
        assert session.run_manager.runs["S-C"].slot_status == "EMPTY"

    # 2. Slot reuse test: another DOWNSIDE signal fires while N-P & S-P are OPEN
    with patch("client_session.astro_signal_engine.evaluate_cluster") as mock_eval:
        mock_eval.return_value = {
            "status": "APPROVED",
            "option_type": "PE",
            "signal": "DOWNSIDE",
            "condition_3_direction": "BUY PE",
            "cluster_rows": [],
            "action": "BUY PE",
        }
        res2 = await session.evaluate_fixed_slots(
            batch_ltps={"12345": 120.0},
            nifty_spot=24200.0,
            sensex_spot=81000.0,
            active_astro_content=dummy_csv,
        )
        # Both PUT slots are occupied -> 0 slots triggered
        assert len(res2["triggered_slots"]) == 0
        assert len(res2["eligible_slots"]) == 0

    # 3. Opposite direction immunity: UPSIDE signal fires while PUT slots are OPEN
    with patch("client_session.astro_signal_engine.evaluate_cluster") as mock_eval:
        mock_eval.return_value = {
            "status": "APPROVED",
            "option_type": "CE",
            "signal": "UPSIDE",
            "condition_3_direction": "BUY CE",
            "cluster_rows": [],
            "action": "BUY CE",
        }
        res3 = await session.evaluate_fixed_slots(
            batch_ltps={"12345": 120.0},
            nifty_spot=24200.0,
            sensex_spot=81000.0,
            active_astro_content=dummy_csv,
        )
        # N-C and S-C should fire because they are free
        assert res3["signal"] == "UPSIDE"
        assert "N-C" in res3["triggered_slots"]
        assert "S-C" in res3["triggered_slots"]
        # PUT slots are NOT touched or closed
        assert session.run_manager.runs["N-P"].slot_status == "OPEN"
        assert session.run_manager.runs["S-P"].slot_status == "OPEN"
        assert session.run_manager.runs["N-C"].slot_status == "OPEN"
        assert session.run_manager.runs["S-C"].slot_status == "OPEN"


# ============================================================================
# 3. BASKET RISK SCOPE TESTS (PER-INSTRUMENT VS GLOBAL)
# ============================================================================

def test_basket_risk_scope_per_instrument():
    """Verify per_instrument basket risk treats Nifty and Sensex independently."""
    rm = RunManager()
    rm.basket_risk_scope = "per_instrument"

    # Setup simulated P&L
    # Nifty CALL: -4000, Nifty PUT: -2000 => Total Nifty = -6000
    # Sensex CALL: +1000, Sensex PUT: 0 => Total Sensex = +1000
    rm.runs["N-C"].active_slices = []
    rm.runs["N-C"].is_active = True
    rm.runs["N-C"].get_total_realized_pnl = MagicMock(return_value=-4000.0)

    rm.runs["N-P"].active_slices = []
    rm.runs["N-P"].is_active = True
    rm.runs["N-P"].get_total_realized_pnl = MagicMock(return_value=-2000.0)

    rm.runs["S-C"].active_slices = []
    rm.runs["S-C"].is_active = True
    rm.runs["S-C"].get_total_realized_pnl = MagicMock(return_value=1000.0)

    rm.runs["S-P"].active_slices = []
    rm.runs["S-P"].get_total_realized_pnl = MagicMock(return_value=0.0)

    # Max loss threshold: 5000 INR
    res = rm.check_basket_risk(max_basket_loss_inr=5000.0)
    
    assert "NIFTY" in res
    assert "SENSEX" in res

    # Nifty breached (-6000 <= -5000)
    assert res["NIFTY"]["breached"] is True
    assert "N-C" in res["NIFTY"]["slots"]
    assert "N-P" in res["NIFTY"]["slots"]
    assert res["NIFTY"]["net_pnl"] == -6000.0

    # Sensex safe (+1000 > -5000)
    assert res["SENSEX"]["breached"] is False
    assert res["SENSEX"]["net_pnl"] == 1000.0


def test_basket_risk_scope_global():
    """Verify global basket risk combines all 4 slots."""
    rm = RunManager()
    rm.basket_risk_scope = "global"

    # Nifty: -3000, Sensex: -2500 => Global Total = -5500
    rm.runs["N-C"].get_total_realized_pnl = MagicMock(return_value=-2000.0)
    rm.runs["N-P"].get_total_realized_pnl = MagicMock(return_value=-1000.0)
    rm.runs["S-C"].get_total_realized_pnl = MagicMock(return_value=-1500.0)
    rm.runs["S-P"].get_total_realized_pnl = MagicMock(return_value=-1000.0)

    # Max loss threshold: 5000 INR
    res = rm.check_basket_risk(max_basket_loss_inr=5000.0)
    assert "GLOBAL" in res
    assert res["GLOBAL"]["breached"] is True
    assert set(res["GLOBAL"]["slots"]) == {"N-C", "N-P", "S-C", "S-P"}
    assert res["GLOBAL"]["net_pnl"] == -5500.0


# ============================================================================
# 4. SLOT FILL & EXIT AUDIT LEDGER TESTS
# ============================================================================

def test_slot_events_ledger():
    """Verify slot event recording and retrieval in RunManager."""
    rm = RunManager()
    rm.record_slot_event(
        slot_id="N-C",
        event_type="SLOT_FILL",
        signal="UPSIDE",
        strike=24200,
        option_type="CE",
        fill_price=125.50,
        qty=65,
        details="Astro cluster buy fill",
    )
    rm.record_slot_event(
        slot_id="N-C",
        event_type="SLOT_EXIT",
        signal="UPSIDE",
        strike=24200,
        option_type="CE",
        fill_price=145.50,
        qty=65,
        pnl_points=20.0,
        pnl_inr=1300.0,
        details={"exit_reason": "Target Profit"},
    )

    assert len(rm.slot_events) == 2
    assert rm.slot_events[0]["slot_id"] == "N-C"
    assert rm.slot_events[0]["event_type"] == "SLOT_FILL"
    assert rm.slot_events[1]["event_type"] == "SLOT_EXIT"
    assert rm.slot_events[1]["pnl_inr"] == 1300.0


# ============================================================================
# 5. FASTAPI HTTP ENDPOINT TESTS
# ============================================================================

def test_slots_status_endpoint(auth_client):
    """Verify GET /api/slots/status returns 4 slots matrix."""
    res = auth_client.get("/api/slots/status")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "success"
    matrix = data.get("slots_matrix") or data.get("slots")
    assert matrix is not None
    assert "N-C" in matrix
    assert "N-P" in matrix
    assert "S-C" in matrix
    assert "S-P" in matrix

    assert matrix["N-C"]["instrument"] == "NIFTY"
    assert matrix["N-C"]["option_type"] == "CE"
    assert matrix["N-P"]["option_type"] == "PE"


def test_slots_events_endpoint(auth_client):
    """Verify GET /api/slots/events returns audit events."""
    res = auth_client.get("/api/slots/events")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert isinstance(data["events"], list)


def test_basket_scope_endpoints(auth_client):
    """Verify GET and POST /api/risk/basket_scope toggles scope."""
    # 1. Read default scope
    res_get = auth_client.get("/api/risk/basket_scope")
    assert res_get.status_code == 200
    assert res_get.json()["scope"] in ("per_instrument", "global")

    # 2. Update to global
    res_post = auth_client.post("/api/risk/basket_scope", json={"scope": "global"})
    assert res_post.status_code == 200
    assert res_post.json()["scope"] == "global"

    # 3. Read back
    res_get2 = auth_client.get("/api/risk/basket_scope")
    assert res_get2.json()["scope"] == "global"

    # 4. Revert to per_instrument
    res_post2 = auth_client.post("/api/risk/basket_scope", json={"scope": "per_instrument"})
    assert res_post2.status_code == 200
    assert res_post2.json()["scope"] == "per_instrument"


# ============================================================================
# 6. AUTO-TRIGGER SIGNAL FINGERPRINT & LOSS BACKSTOP DEDUP TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_auto_trigger_loss_backstop_dedup_and_new_signal_occurrence():
    """
    Reproduces and verifies the auto-trigger loss-backstop re-fire bug fix:
    1. Slot auto-fires into N-C & S-C on an initial 3-upside signal occurrence (fingerprint A).
    2. N-C slot hits range-anchored loss backstop -> auto-closes cleanly (is_active=False, flat).
    3. On subsequent ticks while the signal occurrence is still the same (fingerprint A),
       assert evaluate_fixed_slots() does NOT auto-reopen a new ladder into N-C.
    4. When a genuinely new signal occurrence arrives (fingerprint B),
       assert evaluate_fixed_slots() DOES auto-reopen a fresh ladder into N-C.
    """
    session = ClientSession(client_id="test_fingerprint_client")
    session.astro_auto_trigger = {"all": True}

    session.angel_feed.resolve_contract = MagicMock(return_value={
        "symbol": "NIFTY24200CE",
        "token": "10001",
        "exch_seg": "NFO",
    })
    session.angel_feed.fetch_option_ltp = MagicMock(return_value=120.0)

    dummy_csv = "Date,Time,U/D Logic\n2026-02-01,10:00,Upside\n"

    # 1. Initial 3-upside signal occurrence (Fingerprint A)
    fp_a = "0_2026-02-01T10:00:00+05:30_CE"
    with patch("client_session.astro_signal_engine.evaluate_cluster") as mock_eval:
        mock_eval.return_value = {
            "status": "APPROVED",
            "option_type": "CE",
            "signal": "UPSIDE",
            "fingerprint": fp_a,
            "cluster_index": 0,
            "cluster_time": "2026-02-01T10:00:00+05:30",
        }
        res1 = await session.evaluate_fixed_slots(
            batch_ltps={"10001": 120.0},
            nifty_spot=24200.0,
            sensex_spot=81000.0,
            active_astro_content=dummy_csv,
        )

        assert res1["signal"] == "UPSIDE"
        assert "N-C" in res1["triggered_slots"]
        assert "S-C" in res1["triggered_slots"]
        nc_run = session.run_manager.runs["N-C"]
        assert nc_run.is_active is True
        assert nc_run.last_signal_fingerprint == fp_a

    # 2. Simulate range-anchored loss backstop stop-loss trigger in N-C
    # Trigger price is range_low - loss_point
    loss_trigger = nc_run.get_loss_trigger_price()
    assert loss_trigger is not None
    # Process tick that breaches stop loss
    tick_res = nc_run.process_tick(loss_trigger - 2.0)
    assert tick_res["loss_exit"] is True
    assert nc_run.is_active is False
    assert len(nc_run.active_slices) == 0
    assert nc_run.last_stop_reason == "LOSS_BACKSTOP"

    # Also make S-C flat to test both
    sc_run = session.run_manager.runs["S-C"]
    sc_loss_trigger = sc_run.get_loss_trigger_price()
    if sc_loss_trigger:
        sc_run.process_tick(sc_loss_trigger - 5.0)

    # 3. Next tick on the SAME still-persisting cluster occurrence (Fingerprint A)
    with patch("client_session.astro_signal_engine.evaluate_cluster") as mock_eval:
        mock_eval.return_value = {
            "status": "APPROVED",
            "option_type": "CE",
            "signal": "UPSIDE",
            "fingerprint": fp_a,
            "cluster_index": 0,
            "cluster_time": "2026-02-01T10:00:00+05:30",
        }
        res2 = await session.evaluate_fixed_slots(
            batch_ltps={"10001": 120.0},
            nifty_spot=24200.0,
            sensex_spot=81000.0,
            active_astro_content=dummy_csv,
        )

        # MUST NOT re-fire into flat slots because fingerprint matches!
        assert len(res2["triggered_slots"]) == 0
        assert "N-C" not in res2["eligible_slots"]
        assert "S-C" not in res2["eligible_slots"]
        assert nc_run.is_active is False

    # 4. Genuinely NEW signal occurrence arrives (Fingerprint B)
    fp_b = "1_2026-02-01T10:15:00+05:30_CE"
    with patch("client_session.astro_signal_engine.evaluate_cluster") as mock_eval:
        mock_eval.return_value = {
            "status": "APPROVED",
            "option_type": "CE",
            "signal": "UPSIDE",
            "fingerprint": fp_b,
            "cluster_index": 1,
            "cluster_time": "2026-02-01T10:15:00+05:30",
        }
        res3 = await session.evaluate_fixed_slots(
            batch_ltps={"10001": 120.0},
            nifty_spot=24200.0,
            sensex_spot=81000.0,
            active_astro_content=dummy_csv,
        )

        # Now N-C and S-C MUST auto-fire fresh ladders on new fingerprint
        assert "N-C" in res3["triggered_slots"]
        assert "S-C" in res3["triggered_slots"]
        new_nc_run = session.run_manager.runs["N-C"]
        assert new_nc_run.is_active is True
        assert new_nc_run.last_signal_fingerprint == fp_b


@pytest.mark.asyncio
async def test_auto_trigger_skipped_feedback_on_missing_or_low_ltp():
    """Verify skipped_slots feedback is populated when live LTP is missing or below safety limit."""
    session = ClientSession(client_id="test_skip_ltp_client")
    session.astro_auto_trigger = {"all": True}
    session.angel_feed.is_authenticated = True  # simulate live authenticated feed

    session.angel_feed.resolve_contract = MagicMock(return_value={
        "symbol": "NIFTY24200CE",
        "token": "10001",
        "exch_seg": "NFO",
    })
    session.angel_feed.fetch_option_ltp = MagicMock(return_value=None)

    dummy_csv = "Date,Time,U/D Logic\n2026-02-01,10:00,Upside\n"

    with patch("client_session.astro_signal_engine.evaluate_cluster") as mock_eval:
        mock_eval.return_value = {
            "status": "APPROVED",
            "option_type": "CE",
            "signal": "UPSIDE",
            "fingerprint": "0_2026-02-01T10:00:00+05:30_CE",
        }
        # Batch LTP missing
        res = await session.evaluate_fixed_slots(
            batch_ltps={},
            nifty_spot=24200.0,
            sensex_spot=81000.0,
            active_astro_content=dummy_csv,
        )

        assert len(res["triggered_slots"]) == 0
        assert len(res["skipped_slots"]) > 0
        assert any(s["reason"] == "LTP_MISSING" for s in res["skipped_slots"])


# ============================================================================
# 7. TODAY & OVERALL P&L ROLLUP TESTS (PER-SLOT & PER-INSTRUMENT)
# ============================================================================

def test_run_manager_get_slots_matrix_today_and_overall_pnl():
    """
    Verifies RunManager.get_slots_matrix() accurately rolls up Today P&L vs Overall P&L
    both per slot (N-C, N-P, S-C, S-P) and per instrument (NIFTY & SENSEX).
    """
    import datetime
    from slice_trading_engine import TradeRecord, Slice, SliceStatus

    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    today_dt = datetime.datetime.now(IST)
    today_iso = today_dt.isoformat()
    yesterday_iso = (today_dt - datetime.timedelta(days=1)).isoformat()

    rm = RunManager()

    # Slot N-C: 1 yesterday trade (+₹2000), 1 today trade (+₹1500), 1 open slice with unrealized +₹650
    nc = rm.runs["N-C"]
    nc.trade_history = [
        TradeRecord(
            trade_id="TRD-NC-1",
            run_id="run01",
            label="A",
            level_price=100.0,
            fill_price=100.0,
            exit_price=130.8,
            quantity=65,
            pnl_points=30.8,
            pnl_rupees=2000.0,
            exit_reason="PROFIT_TARGET",
            filled_at=yesterday_iso,
            exited_at=yesterday_iso,
            contract_symbol="NIFTY 24200 CE",
            exit_time=yesterday_iso,
        ),
        TradeRecord(
            trade_id="TRD-NC-2",
            run_id="run01",
            label="B",
            level_price=90.0,
            fill_price=90.0,
            exit_price=113.1,
            quantity=65,
            pnl_points=23.1,
            pnl_rupees=1500.0,
            exit_reason="PROFIT_TARGET",
            filled_at=today_iso,
            exited_at=today_iso,
            contract_symbol="NIFTY 24200 CE",
            exit_time=today_iso,
        ),
    ]
    nc.accumulated_realized_pnl = 3500.0
    nc.is_active = True
    nc.active_slices = [
        Slice(
            level_price=80.0,
            status=SliceStatus.FILLED,
            fill_price=80.0,
            quantity=65,
            pnl_rupees=650.0,
            pnl_points=10.0,
        )
    ]
    nc.last_ltp = 90.0

    # Slot N-P: 1 today loss trade (-₹500), no open slices
    np_slot = rm.runs["N-P"]
    np_slot.trade_history = [
        TradeRecord(
            trade_id="TRD-NP-1",
            run_id="run03",
            label="A",
            level_price=120.0,
            fill_price=120.0,
            exit_price=112.3,
            quantity=65,
            pnl_points=-7.7,
            pnl_rupees=-500.0,
            exit_reason="MANUAL",
            filled_at=today_iso,
            exited_at=today_iso,
            contract_symbol="NIFTY 24200 PE",
            exit_time=today_iso,
        )
    ]
    np_slot.accumulated_realized_pnl = -500.0

    # Slot S-C: 1 yesterday trade (+₹1000)
    sc = rm.runs["S-C"]
    sc.trade_history = [
        TradeRecord(
            trade_id="TRD-SC-1",
            run_id="run02",
            label="A",
            level_price=200.0,
            fill_price=200.0,
            exit_price=250.0,
            quantity=20,
            pnl_points=50.0,
            pnl_rupees=1000.0,
            exit_reason="PROFIT_TARGET",
            filled_at=yesterday_iso,
            exited_at=yesterday_iso,
            contract_symbol="SENSEX 81000 CE",
            exit_time=yesterday_iso,
        )
    ]
    sc.accumulated_realized_pnl = 1000.0

    # Slot S-P: 1 today trade (+₹800)
    sp = rm.runs["S-P"]
    sp.trade_history = [
        TradeRecord(
            trade_id="TRD-SP-1",
            run_id="run04",
            label="A",
            level_price=200.0,
            fill_price=200.0,
            exit_price=240.0,
            quantity=20,
            pnl_points=40.0,
            pnl_rupees=800.0,
            exit_reason="PROFIT_TARGET",
            filled_at=today_iso,
            exited_at=today_iso,
            contract_symbol="SENSEX 81000 PE",
            exit_time=today_iso,
        )
    ]
    sp.accumulated_realized_pnl = 800.0

    # Execute get_slots_matrix()
    res = rm.get_slots_matrix()
    slots = res["slots_matrix"]
    inst_pnl = res["instrument_pnl"]

    # --- 1. Per-Slot Assertions ---
    # N-C: today_realized=1500, overall_realized=3500, unrealized=650, today_pnl=2150, overall_pnl=4150
    assert slots["N-C"]["today_realized_pnl"] == 1500.0
    assert slots["N-C"]["overall_realized_pnl"] == 3500.0
    assert slots["N-C"]["unrealized_pnl"] == 650.0
    assert slots["N-C"]["today_pnl"] == 2150.0
    assert slots["N-C"]["overall_pnl"] == 4150.0

    # N-P: today_realized=-500, overall_realized=-500, unrealized=0, today_pnl=-500, overall_pnl=-500
    assert slots["N-P"]["today_realized_pnl"] == -500.0
    assert slots["N-P"]["overall_realized_pnl"] == -500.0
    assert slots["N-P"]["today_pnl"] == -500.0
    assert slots["N-P"]["overall_pnl"] == -500.0

    # S-C: today_realized=0, overall_realized=1000, today_pnl=0, overall_pnl=1000
    assert slots["S-C"]["today_realized_pnl"] == 0.0
    assert slots["S-C"]["overall_realized_pnl"] == 1000.0
    assert slots["S-C"]["today_pnl"] == 0.0
    assert slots["S-C"]["overall_pnl"] == 1000.0

    # S-P: today_realized=800, overall_realized=800, today_pnl=800, overall_pnl=800
    assert slots["S-P"]["today_realized_pnl"] == 800.0
    assert slots["S-P"]["overall_realized_pnl"] == 800.0
    assert slots["S-P"]["today_pnl"] == 800.0
    assert slots["S-P"]["overall_pnl"] == 800.0

    # --- 2. Per-Instrument Rollup Assertions ---
    # NIFTY = N-C + N-P
    # today_realized: 1500 - 500 = 1000
    # today_pnl: 2150 - 500 = 1650
    # overall_realized: 3500 - 500 = 3000
    # overall_pnl: 4150 - 500 = 3650
    # unrealized: 650
    assert inst_pnl["NIFTY"]["today_realized_pnl"] == 1000.0
    assert inst_pnl["NIFTY"]["today_pnl"] == 1650.0
    assert inst_pnl["NIFTY"]["overall_realized_pnl"] == 3000.0
    assert inst_pnl["NIFTY"]["overall_pnl"] == 3650.0
    assert inst_pnl["NIFTY"]["unrealized_pnl"] == 650.0

    # SENSEX = S-C + S-P
    # today_realized: 0 + 800 = 800
    # today_pnl: 0 + 800 = 800
    # overall_realized: 1000 + 800 = 1800
    # overall_pnl: 1000 + 800 = 1800
    assert inst_pnl["SENSEX"]["today_realized_pnl"] == 800.0
    assert inst_pnl["SENSEX"]["today_pnl"] == 800.0
    assert inst_pnl["SENSEX"]["overall_realized_pnl"] == 1800.0
    assert inst_pnl["SENSEX"]["overall_pnl"] == 1800.0


def test_api_state_and_slots_status_include_matrix_and_instrument_pnl(auth_client):
    """Verify /api/state and /api/slots/status both include slots_matrix and instrument_pnl."""
    # 1. /api/state
    res_state = auth_client.get("/api/state")
    assert res_state.status_code == 200
    data_state = res_state.json()
    assert "slots_matrix" in data_state
    assert "instrument_pnl" in data_state
    assert "NIFTY" in data_state["instrument_pnl"]
    assert "SENSEX" in data_state["instrument_pnl"]
    assert "N-C" in data_state["slots_matrix"]
    assert "today_pnl" in data_state["slots_matrix"]["N-C"]
    assert "overall_pnl" in data_state["slots_matrix"]["N-C"]

    # 2. /api/slots/status
    res_slots = auth_client.get("/api/slots/status")
    assert res_slots.status_code == 200
    data_slots = res_slots.json()
    assert "slots_matrix" in data_slots
    assert "instrument_pnl" in data_slots
    assert "NIFTY" in data_slots["instrument_pnl"]
    assert "SENSEX" in data_slots["instrument_pnl"]


def test_slot_pnl_preserved_across_refresh_and_master_history_load():
    """
    Verifies that when a session initializes with master_trade_history or when
    the user refreshes the page, the today and overall P&Ls for each slot are
    accurately retained and calculated from trade history.
    """
    import datetime
    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    today_str = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    yesterday_str = (datetime.datetime.now(IST) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    rm = RunManager(max_runs=4, state_file=":memory:")

    # Simulated MongoDB master_trade_history loaded into session
    rm.master_trade_history = [
        # N-C: 1 today (+500), 1 yesterday (+300) -> Today: 500, Overall: 800
        {"trade_id": "T1", "slot_id": "N-C", "run_id": "run01", "pnl_rupees": 500.0, "exit_time": f"{today_str}T10:00:00+05:30", "instrument": "NIFTY", "option_type": "CE"},
        {"trade_id": "T2", "slot_id": "N-C", "run_id": "run01", "pnl_rupees": 300.0, "exit_time": f"{yesterday_str}T14:00:00+05:30", "instrument": "NIFTY", "option_type": "CE"},
        # N-P: 1 today (-200) -> Today: -200, Overall: -200
        {"trade_id": "T3", "slot_id": "N-P", "run_id": "run03", "pnl_rupees": -200.0, "exit_time": f"{today_str}T11:00:00+05:30", "instrument": "NIFTY", "option_type": "PE"},
        # S-C: 1 yesterday (+1000) -> Today: 0, Overall: 1000
        {"trade_id": "T4", "slot_id": "S-C", "run_id": "run02", "pnl_rupees": 1000.0, "exit_time": f"{yesterday_str}T12:00:00+05:30", "instrument": "SENSEX", "option_type": "CE"},
        # S-P: 1 today (+400), 1 today (+600) -> Today: 1000, Overall: 1000
        {"trade_id": "T5", "slot_id": "S-P", "run_id": "run04", "pnl_rupees": 400.0, "exit_time": f"{today_str}T13:00:00+05:30", "instrument": "SENSEX", "option_type": "PE"},
        {"trade_id": "T6", "slot_id": "S-P", "run_id": "run04", "pnl_rupees": 600.0, "exit_time": f"{today_str}T14:30:00+05:30", "instrument": "SENSEX", "option_type": "PE"},
    ]

    rm.sync_runs_from_history()
    matrix_data = rm.get_slots_matrix()
    slots = matrix_data["slots_matrix"]
    inst_pnl = matrix_data["instrument_pnl"]

    # Slot N-C
    assert slots["N-C"]["today_realized_pnl"] == 500.0
    assert slots["N-C"]["overall_realized_pnl"] == 800.0
    assert slots["N-C"]["today_pnl"] == 500.0
    assert slots["N-C"]["overall_pnl"] == 800.0

    # Slot N-P
    assert slots["N-P"]["today_realized_pnl"] == -200.0
    assert slots["N-P"]["overall_realized_pnl"] == -200.0

    # Slot S-C
    assert slots["S-C"]["today_realized_pnl"] == 0.0
    assert slots["S-C"]["overall_realized_pnl"] == 1000.0

    # Slot S-P
    assert slots["S-P"]["today_realized_pnl"] == 1000.0
    assert slots["S-P"]["overall_realized_pnl"] == 1000.0

    # Combined Instrument rollups
    # NIFTY = N-C (500 / 800) + N-P (-200 / -200) = Today: 300, Overall: 600
    assert inst_pnl["NIFTY"]["today_realized_pnl"] == 300.0
    assert inst_pnl["NIFTY"]["overall_realized_pnl"] == 600.0

    # SENSEX = S-C (0 / 1000) + S-P (1000 / 1000) = Today: 1000, Overall: 2000
    assert inst_pnl["SENSEX"]["today_realized_pnl"] == 1000.0
    assert inst_pnl["SENSEX"]["overall_realized_pnl"] == 2000.0



