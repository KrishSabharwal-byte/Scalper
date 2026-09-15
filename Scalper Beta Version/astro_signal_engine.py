"""
Astro CSV Signal Engine
Implements:
1. Flexible CSV Parsing & Ingestion:
   - Dynamic header detection across first 15 lines (skips metadata/preamble rows).
   - Column normalization (maps U/D Logic, Direction, Signal, etc. to 'direction').
   - Timestamp parsing & chronological sorting across multiple date/time formats.
   - Robust direction aliasing (Upside/Downside/Neutral).
2. 3-Row Cluster Signal Logic:
   - Window of [current, next, next+1] where current is latest row <= now.
   - All 3 upside -> BUY CE.
   - All 3 downside -> BUY PE.
   - Mixed/neutral -> None.
3. Entry Filters:
   - Active position filter (one trade at a time).
   - Trading hours filter (09:16 to EOD cutoff, default 15:22 IST).
   - No alternation requirement (consecutive CE or PE signals are allowed).
4. Audit Logging:
   - Logs every evaluation and decision to logs/YYYY-MM-DD/app.log.
"""

import io
import os
import re
import datetime
import logging
from typing import Dict, Any, Optional, List, Tuple, Union
import pandas as pd

logger = logging.getLogger("AstroSignalEngine")

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Direction aliases
UPSIDE_ALIASES = {"upside", "buy", "up", "bullish", "1", "1.0", "ce", "call", "long"}
DOWNSIDE_ALIASES = {"downside", "sell", "down", "bearish", "-1", "-1.0", "pe", "put", "short"}


def normalize_direction(val: Any) -> str:
    """Normalizes direction values to 'upside', 'downside', or 'neutral'."""
    if val is None:
        return "neutral"
    s = str(val).strip().lower()
    # Strip non-alphanumeric except - and .
    s = re.sub(r"[^\w\.\-]", "", s)
    if s in UPSIDE_ALIASES:
        return "upside"
    elif s in DOWNSIDE_ALIASES:
        return "downside"
    return "neutral"


def detect_header_row(lines: List[str], max_scan_lines: int = 15) -> int:
    """
    Scans the first N lines of CSV text to detect the true header row.
    Looks for keywords: date, time, direction, udlogic, logic, signal.
    Returns 0-based row index.
    """
    header_keywords = {"date", "time", "direction", "udlogic", "logic", "signal", "ud"}
    scan_limit = min(len(lines), max_scan_lines)

    for idx in range(scan_limit):
        line = lines[idx].strip()
        if not line:
            continue
        # Split line on commas or tabs/semicolons
        parts = re.split(r"[,;\t]", line)
        tokens = [re.sub(r"[^a-zA-Z0-9]", "", p).lower() for p in parts if p.strip()]

        has_date_or_time = any(t in ("date", "time", "datetime", "timestamp") for t in tokens)
        has_logic = any(t in ("direction", "udlogic", "logic", "signal", "ud") for t in tokens)

        if has_date_or_time and has_logic:
            return idx
        if any(t in header_keywords for t in tokens) and len(tokens) >= 2:
            return idx

    return 0


def parse_astro_csv(content: Union[str, bytes]) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Parses an uploaded Astro CSV file into a clean, chronologically sorted DataFrame.
    Returns (df, metadata).
    """
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
    else:
        text = content

    lines = text.splitlines()
    if not lines:
        raise ValueError("Astro CSV content is empty.")

    header_idx = detect_header_row(lines, max_scan_lines=15)
    csv_text = "\n".join(lines[header_idx:])

    df = pd.read_csv(io.StringIO(csv_text))
    if df.empty:
        raise ValueError("Astro CSV contains no data rows.")

    # Normalize column names
    col_map = {}
    date_col = None
    time_col = None
    dir_col = None

    for col in df.columns:
        clean = re.sub(r"[^a-zA-Z0-9]", "", str(col)).lower()
        if clean in ("date", "dt", "tradedate", "sessiondate"):
            date_col = col
        elif clean in ("time", "tm", "tradetime", "timestamp"):
            time_col = col
        elif clean in ("direction", "udlogic", "logic", "signal", "trend", "ud"):
            dir_col = col

    if dir_col is None:
        # Check if 3rd column might be direction
        if len(df.columns) >= 3:
            dir_col = df.columns[2]
        else:
            raise ValueError(f"Could not identify direction column. Available: {list(df.columns)}")

    df["raw_direction"] = df[dir_col].astype(str)
    df["direction"] = df["raw_direction"].apply(normalize_direction)

    # Parse timestamps
    if date_col and time_col and date_col != time_col:
        datetime_series = df[date_col].astype(str).str.strip() + " " + df[time_col].astype(str).str.strip()
    elif date_col:
        datetime_series = df[date_col].astype(str).str.strip()
    elif time_col:
        today_str = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        datetime_series = today_str + " " + df[time_col].astype(str).str.strip()
    else:
        raise ValueError("No date or time column detected in Astro CSV.")

    sample = datetime_series.dropna().iloc[0] if len(datetime_series.dropna()) else ""
    is_year_first = bool(re.match(r"^\s*\d{4}", str(sample)))
    parsed_dates = pd.to_datetime(
        datetime_series,
        errors="coerce",
        format="mixed",
        dayfirst=(not is_year_first),
    )

    # Ensure timezone is IST
    parsed_dt_list = []
    for dt in parsed_dates:
        if pd.isna(dt):
            parsed_dt_list.append(None)
        else:
            py_dt = dt.to_pydatetime()
            if py_dt.tzinfo is None:
                py_dt = py_dt.replace(tzinfo=IST)
            else:
                py_dt = py_dt.astimezone(IST)
            parsed_dt_list.append(py_dt)

    df["parsed_dt"] = parsed_dt_list
    df = df.dropna(subset=["parsed_dt"])
    df = df.sort_values(by="parsed_dt").reset_index(drop=True)

    metadata = {
        "header_row_skipped": header_idx,
        "row_count": len(df),
        "start_time": df["parsed_dt"].iloc[0].isoformat() if len(df) else None,
        "end_time": df["parsed_dt"].iloc[-1].isoformat() if len(df) else None,
        "unique_directions": df["direction"].unique().tolist(),
    }
    return df, metadata


class AstroSignalEngine:
    """
    Evaluates 3-row cluster window and entry filters for Astro trading entries.
    """

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir

    def log_decision(
        self,
        event_type: str,
        current_dt: datetime.datetime,
        details: Dict[str, Any],
        instrument: str = "NIFTY",
    ) -> None:
        """Appends audit decision record to logs/YYYY-MM-DD/app.log."""
        try:
            today_str = current_dt.strftime("%Y-%m-%d")
            dir_path = os.path.join(self.log_dir, today_str)
            os.makedirs(dir_path, exist_ok=True)
            log_path = os.path.join(dir_path, "app.log")

            time_str = current_dt.strftime("%H:%M:%S")
            msg = f"[{time_str}] [ASTRO_SIGNAL] [{instrument}] [{event_type}] {details}\n"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(msg)
        except Exception as e:
            logger.warning(f"Failed writing astro audit log: {e}")

    def evaluate_cluster(
        self,
        df: pd.DataFrame,
        current_dt: Optional[datetime.datetime] = None,
        last_trade_direction: Optional[str] = None,
        has_active_trade: bool = False,
        cutoff_time_ist: str = "15:22",
        market_open_ist: str = "09:16",
        instrument: str = "NIFTY",
    ) -> Dict[str, Any]:
        """
        Evaluates the 3-row cluster at current_dt against entry filters.
        Returns a decision payload dict.
        """
        now = current_dt if current_dt is not None else datetime.datetime.now(IST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=IST)

        if df is None or df.empty:
            res = {"status": "NO_SIGNAL", "reason": "empty_dataframe", "timestamp": now.isoformat()}
            self.log_decision("NO_SIGNAL", now, res, instrument)
            return res

        # 1. Locate current row: latest row where parsed_dt <= now
        past_rows = df[df["parsed_dt"] <= now]
        if past_rows.empty:
            res = {
                "status": "NO_SIGNAL",
                "reason": "before_first_report_row",
                "timestamp": now.isoformat(),
                "first_row_time": df["parsed_dt"].iloc[0].isoformat() if len(df) else None,
            }
            self.log_decision("NO_SIGNAL", now, res, instrument)
            return res

        actual_idx = past_rows.index[-1]
        window = df.iloc[actual_idx : actual_idx + 3]

        if len(window) < 3:
            res = {
                "status": "NO_SIGNAL",
                "reason": "insufficient_rows_remaining",
                "remaining_count": len(window),
                "timestamp": now.isoformat(),
            }
            self.log_decision("NO_SIGNAL", now, res, instrument)
            return res

        directions = window["direction"].tolist()
        window_summary = [
            {
                "time": r["parsed_dt"].strftime("%H:%M:%S") if hasattr(r["parsed_dt"], "strftime") else str(r["parsed_dt"]),
                "raw": str(r.get("raw_direction", r["direction"])),
                "normalized": str(d),
            }
            for d, (_, r) in zip(directions, window.iterrows())
        ]

        # 2. 3-row cluster direction check
        raw_signal = None
        target_option_type = None

        if all(d == "upside" for d in directions):
            raw_signal = "BUY CE"
            target_option_type = "CE"
        elif all(d == "downside" for d in directions):
            raw_signal = "BUY PE"
            target_option_type = "PE"
        else:
            res = {
                "status": "NO_SIGNAL",
                "reason": "cluster_mixed_or_neutral",
                "directions": directions,
                "window": window_summary,
                "timestamp": now.isoformat(),
            }
            self.log_decision("EVALUATED_NO_SIGNAL", now, res, instrument)
            return res

        # 3. Filter 1: Trading hours check (09:16 to cutoff_time_ist)
        try:
            c_h, c_m = [int(x) for x in cutoff_time_ist.split(":")[:2]]
            cutoff_t = datetime.time(c_h, c_m)
        except Exception:
            cutoff_t = datetime.time(15, 22)

        try:
            o_h, o_m = [int(x) for x in market_open_ist.split(":")[:2]]
            open_t = datetime.time(o_h, o_m)
        except Exception:
            open_t = datetime.time(9, 16)

        now_t = now.time()
        if not (open_t <= now_t <= cutoff_t):
            res = {
                "status": "BLOCKED",
                "filter": "trading_hours",
                "raw_signal": raw_signal,
                "option_type": target_option_type,
                "current_time": now_t.strftime("%H:%M:%S"),
                "hours_range": f"{open_t.strftime('%H:%M')} - {cutoff_t.strftime('%H:%M')}",
                "window": window_summary,
            }
            self.log_decision("FILTER_BLOCKED", now, res, instrument)
            return res

        # 4. Filter 2: Active open trade filter
        if has_active_trade:
            res = {
                "status": "BLOCKED",
                "filter": "active_trade_open",
                "raw_signal": raw_signal,
                "option_type": target_option_type,
                "window": window_summary,
                "timestamp": now.isoformat(),
            }
            self.log_decision("FILTER_BLOCKED", now, res, instrument)
            return res

        # All filters passed -> Approved Signal!
        approved = {
            "status": "APPROVED",
            "signal": raw_signal,
            "option_type": target_option_type,
            "directions": directions,
            "window": window_summary,
            "timestamp": now.isoformat(),
        }
        self.log_decision("SIGNAL_APPROVED", now, approved, instrument)
        return approved


# Global singleton instance
astro_signal_engine = AstroSignalEngine()
