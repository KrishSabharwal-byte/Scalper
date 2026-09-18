"""
Comprehensive Unit Tests for Astro CSV Signal Engine
Covers:
1. CSV Header Detection with leading metadata/preamble lines.
2. Column normalization (U/D Logic, Direction, Signal, etc.).
3. Timestamp parsing and chronological sorting.
4. Direction normalization and aliasing (upside/downside/neutral).
5. 3-Row cluster window signal rule (BUY CE / BUY PE / Mixed Neutral / End of DataFrame).
6. Trading hours entry filter (09:16 - 15:22 IST).
7. Active open position filter.
8. No alternation restriction (consecutive CE or PE permitted).
9. Astro file storage, listing, activation, and deletion.
"""

import datetime
import pytest
from astro_signal_engine import (
    AstroSignalEngine,
    detect_header_row,
    normalize_direction,
    parse_astro_csv,
    IST,
)
from mongo_service import mongo_service


def test_header_detection_with_preamble():
    """Skips leading metadata rows to detect true CSV header."""
    lines = [
        "# Weekly Astro Report v2.1",
        "# Author: Astro Team",
        "# Generated for Nifty & Sensex",
        "Date,Time,U/D Logic",
        "2026-02-01,10:35,Upside",
        "2026-02-01,10:45,Upside",
    ]
    header_idx = detect_header_row(lines, max_scan_lines=15)
    assert header_idx == 3


def test_column_normalization_and_parsing():
    """Parses various column name variants into normalized format."""
    csv_content = """Report Metadata: Nifty Weekly
Created: 2026-02-01
Date,Time,U/D Logic
2026-02-01,10:35,Upside
2026-02-01,10:45,1.0
2026-02-01,10:55,Bullish
2026-02-01,11:03,Neutral
2026-02-01,11:45,Downside
2026-02-01,11:57,-1
2026-02-01,12:07,Bearish
"""
    df, meta = parse_astro_csv(csv_content)
    assert meta["row_count"] == 7
    assert "parsed_dt" in df.columns
    assert "direction" in df.columns
    # Verify chronological order
    assert df["parsed_dt"].is_monotonic_increasing


def test_direction_normalization_aliases():
    """Verifies direction aliasing."""
    assert normalize_direction("Upside") == "upside"
    assert normalize_direction("BUY") == "upside"
    assert normalize_direction("Bullish") == "upside"
    assert normalize_direction("1") == "upside"
    assert normalize_direction("1.0") == "upside"
    assert normalize_direction("CE") == "upside"

    assert normalize_direction("Downside") == "downside"
    assert normalize_direction("SELL") == "downside"
    assert normalize_direction("Bearish") == "downside"
    assert normalize_direction("-1") == "downside"
    assert normalize_direction("-1.0") == "downside"
    assert normalize_direction("PE") == "downside"

    assert normalize_direction("Neutral") == "neutral"
    assert normalize_direction("Hold") == "neutral"
    assert normalize_direction("") == "neutral"
    assert normalize_direction(None) == "neutral"


def test_3_row_cluster_signals():
    """
    Tests 3-row cluster rule:
    - 3 upside -> BUY CE
    - 3 downside -> BUY PE
    - mixed/neutral -> no signal
    - < 3 rows -> no signal
    """
    csv_content = """Date,Time,U/D Logic
2026-02-01,10:00,Upside
2026-02-01,10:10,Upside
2026-02-01,10:20,Upside
2026-02-01,10:30,Neutral
2026-02-01,10:40,Downside
2026-02-01,10:50,Downside
2026-02-01,11:00,Downside
2026-02-01,11:10,Upside
"""
    df, _ = parse_astro_csv(csv_content)
    engine = AstroSignalEngine()

    # At 10:05 (current row is 10:00, window is 10:00, 10:10, 10:20: all Upside)
    t1 = datetime.datetime(2026, 2, 1, 10, 5, tzinfo=IST)
    res1 = engine.evaluate_cluster(df, current_dt=t1)
    assert res1["status"] == "APPROVED"
    assert res1["signal"] == "BUY CE"
    assert res1["option_type"] == "CE"
    assert "0_2026-02-01T10:00:00+05:30_CE" in res1["fingerprint"]
    assert res1["cluster_index"] == 0

    # At 10:25 (current row is 10:20, window is 10:20 [Up], 10:30 [Neutral], 10:40 [Down]) -> Mixed/Neutral
    t2 = datetime.datetime(2026, 2, 1, 10, 25, tzinfo=IST)
    res2 = engine.evaluate_cluster(df, current_dt=t2)
    assert res2["status"] == "NO_SIGNAL"
    assert res2["reason"] == "cluster_mixed_or_neutral"

    # At 10:45 (current row is 10:40, window is 10:40, 10:50, 11:00: all Downside)
    t3 = datetime.datetime(2026, 2, 1, 10, 45, tzinfo=IST)
    res3 = engine.evaluate_cluster(df, current_dt=t3)
    assert res3["status"] == "APPROVED"
    assert res3["signal"] == "BUY PE"
    assert res3["option_type"] == "PE"
    assert "4_2026-02-01T10:40:00+05:30_PE" in res3["fingerprint"]
    assert res3["cluster_index"] == 4

    # At 11:05 (current row is 11:00, remaining rows are 11:00 and 11:10: only 2 rows) -> Insufficient rows
    t4 = datetime.datetime(2026, 2, 1, 11, 5, tzinfo=IST)
    res4 = engine.evaluate_cluster(df, current_dt=t4)
    assert res4["status"] == "NO_SIGNAL"
    assert res4["reason"] == "insufficient_rows_remaining"


def test_entry_filter_trading_hours():
    """Signals outside 09:16 - 15:22 are blocked."""
    csv_content = """Date,Time,U/D Logic
2026-02-01,09:00,Upside
2026-02-01,09:05,Upside
2026-02-01,09:10,Upside
2026-02-01,15:20,Downside
2026-02-01,15:21,Downside
2026-02-01,15:22,Downside
"""
    df, _ = parse_astro_csv(csv_content)
    engine = AstroSignalEngine()

    # Pre-market: 09:01 -> current row is 09:00, window [09:00, 09:05, 09:10] (all Upside) -> blocked by trading hours
    t_pre = datetime.datetime(2026, 2, 1, 9, 1, tzinfo=IST)
    res_pre = engine.evaluate_cluster(df, current_dt=t_pre)
    assert res_pre["status"] == "BLOCKED"
    assert res_pre["filter"] == "trading_hours"

    # Post-cutoff: 15:20:10 -> current row is 15:20, window [15:20, 15:21, 15:22] with cutoff 15:15 -> blocked by trading hours
    t_post = datetime.datetime(2026, 2, 1, 15, 20, 10, tzinfo=IST)
    res_post = engine.evaluate_cluster(df, current_dt=t_post, cutoff_time_ist="15:15")
    assert res_post["status"] == "BLOCKED"
    assert res_post["filter"] == "trading_hours"


def test_entry_filter_active_trade():
    """Signals are blocked if an active trade is already open."""
    csv_content = """Date,Time,U/D Logic
2026-02-01,10:00,Upside
2026-02-01,10:10,Upside
2026-02-01,10:20,Upside
"""
    df, _ = parse_astro_csv(csv_content)
    engine = AstroSignalEngine()

    t = datetime.datetime(2026, 2, 1, 10, 5, tzinfo=IST)
    res = engine.evaluate_cluster(df, current_dt=t, has_active_trade=True)
    assert res["status"] == "BLOCKED"
    assert res["filter"] == "active_trade_open"


def test_entry_filter_no_alternation_required():
    """
    Alternation rule is removed: consecutive trades of the same direction (CE after CE, PE after PE)
    are permitted and approved.
    """
    csv_content = """Date,Time,U/D Logic
2026-02-01,10:00,Upside
2026-02-01,10:10,Upside
2026-02-01,10:20,Upside
2026-02-01,11:00,Downside
2026-02-01,11:10,Downside
2026-02-01,11:20,Downside
"""
    df, _ = parse_astro_csv(csv_content)
    engine = AstroSignalEngine()

    t_ce = datetime.datetime(2026, 2, 1, 10, 5, tzinfo=IST)

    # 1. First trade of day: last_trade_direction is None -> ALLOWED
    res1 = engine.evaluate_cluster(df, current_dt=t_ce, last_trade_direction=None)
    assert res1["status"] == "APPROVED"
    assert res1["option_type"] == "CE"

    # 2. Last trade was CE, another CE signal arrives -> ALLOWED (no alternation restriction)
    res2 = engine.evaluate_cluster(df, current_dt=t_ce, last_trade_direction="CE")
    assert res2["status"] == "APPROVED"
    assert res2["option_type"] == "CE"

    # 3. Last trade was PE, CE signal arrives -> ALLOWED
    res3 = engine.evaluate_cluster(df, current_dt=t_ce, last_trade_direction="PE")
    assert res3["status"] == "APPROVED"
    assert res3["option_type"] == "CE"

    # 4. Now evaluate at 11:05 (Downside cluster -> PE):
    t_pe = datetime.datetime(2026, 2, 1, 11, 5, tzinfo=IST)
    # If last trade was CE, PE signal is ALLOWED
    res4 = engine.evaluate_cluster(df, current_dt=t_pe, last_trade_direction="CE")
    assert res4["status"] == "APPROVED"
    assert res4["option_type"] == "PE"

    # If last trade was PE, PE signal is also ALLOWED (no alternation restriction)
    res5 = engine.evaluate_cluster(df, current_dt=t_pe, last_trade_direction="PE")
    assert res5["status"] == "APPROVED"
    assert res5["option_type"] == "PE"


def test_mongo_astro_file_storage_and_activation():
    """Verifies saving, listing, activating, and deleting astro reports in Astro.{client_id}."""
    test_client = "test_trader_astro"
    csv_sample = "Date,Time,U/D Logic\n2026-02-01,10:00,Upside\n2026-02-01,10:10,Upside\n2026-02-01,10:20,Upside\n"

    # Save file 1
    fid1 = mongo_service.save_astro_file(
        client_id=test_client,
        filename="report_week1.csv",
        content=csv_sample,
        row_count=3,
        is_active=True,
    )
    assert fid1 is not None

    active = mongo_service.get_active_astro_file(client_id=test_client)
    assert active is not None
    assert active["filename"] == "report_week1.csv"

    # Save file 2
    fid2 = mongo_service.save_astro_file(
        client_id=test_client,
        filename="report_week2.csv",
        content=csv_sample,
        row_count=3,
        is_active=True,
    )
    active2 = mongo_service.get_active_astro_file(client_id=test_client)
    assert active2["filename"] == "report_week2.csv"

    # Switch active back to file 1
    mongo_service.set_active_astro_file(client_id=test_client, file_id=fid1)
    active3 = mongo_service.get_active_astro_file(client_id=test_client)
    assert active3["filename"] == "report_week1.csv"

    # List files
    file_list = mongo_service.list_astro_files(client_id=test_client)
    assert len(file_list) >= 2

    # Delete file 1
    mongo_service.delete_astro_file(client_id=test_client, file_id=fid1)
    active_after = mongo_service.get_active_astro_file(client_id=test_client)
    assert active_after["filename"] == "report_week2.csv"
