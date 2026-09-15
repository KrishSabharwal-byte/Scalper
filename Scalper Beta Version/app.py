"""
Slicing Scalper Simulation Engine - FastAPI Backend (Hardened v3.1)
Implements REST API for multi-slot scalper runs, real-time SSE stream,
manual simulation tick injection, state persistence, strike locking,
feed staleness monitoring, and Angel One live feed integration.
"""

import asyncio
import datetime
import json
import logging
import os
import threading
from typing import Dict, Any, Optional, List
from dataclasses import asdict
from fastapi import FastAPI, Request, Query, Depends, HTTPException, status, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from astro_signal_engine import astro_signal_engine, parse_astro_csv
from slice_trading_engine import (
    RunManager,
    ScalperRunConfig,
    ScalperRun,
    calculate_nearest_strike,
    calculate_nearest_50_strike,
    recenter_range,
    validate_config,
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
from auth_service import (
    auth_service,
    SESSION_COOKIE_NAME,
    ACCESS_TOKEN_EXPIRE_MINUTES,
)

logger = logging.getLogger("SlicerApp")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Slicing Scalper Simulation Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from client_session import ClientSession
from order_safety_service import OrderSafetyViolation


@app.exception_handler(OrderSafetyViolation)
async def order_safety_exception_handler(request: Request, exc: OrderSafetyViolation):
    logger.critical(f"Intercepted OrderSafetyViolation: {exc.message}")
    return JSONResponse(
        status_code=403,
        content={
            "status": "error",
            "error_type": "ORDER_SAFETY_VIOLATION",
            "message": exc.message,
            "audit": exc.audit_data,
        },
    )

# -------------------------------------------------------------------
# Multi-Tenant Client Sessions Registry (Phase 4)
# -------------------------------------------------------------------
client_sessions: Dict[str, ClientSession] = {}
client_sessions_lock = threading.Lock()


def get_client_session(client_id: str) -> ClientSession:
    """
    Retrieves or lazily initializes an encapsulated ClientSession for client_id.
    Each session holds its own RunManager, AngelOneFeed, locks, and live feed worker.
    """
    if not client_id or not isinstance(client_id, str) or not client_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="CRITICAL: Resolved client_id required for session access.",
        )
    cid = client_id.strip()

    with client_sessions_lock:
        if cid not in client_sessions:
            session = ClientSession(client_id=cid)
            client_sessions[cid] = session
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    session.start_live_feed()
            except RuntimeError:
                pass

        session = client_sessions[cid]
        session.touch()
        return session


def close_client_session(client_id: str) -> None:
    """Closes and tears down a ClientSession on logout or idle timeout."""
    cid = client_id.strip() if client_id else ""
    with client_sessions_lock:
        if cid in client_sessions:
            session = client_sessions.pop(cid)
            session.close()


def get_client_run_manager(client_id: str) -> RunManager:
    """Backward compatibility helper mapping client_id to its session's RunManager."""
    session = get_client_session(client_id)
    return session.run_manager


def get_client_broker_feed(client_id: str) -> AngelOneFeed:
    """Backward compatibility helper mapping client_id to its session's AngelOneFeed."""
    session = get_client_session(client_id)
    return session.angel_feed


async def broadcast_state(client_id: str):
    """Broadcasts state through the client's own ClientSession."""
    cid = client_id.strip() if client_id else ""
    with client_sessions_lock:
        session = client_sessions.get(cid)
    if session:
        await session.broadcast_state()


# Request Models
class LoginRequest(BaseModel):
    client_id: str
    password: str


class CreateClientRequest(BaseModel):
    client_id: str
    password: str
    role: Optional[str] = "trader"
    is_active: Optional[bool] = True


class StartRunRequest(BaseModel):
    run_id: Optional[str] = None
    instrument_name: str = "NIFTY"
    option_type: str = "CE"
    strike: Optional[int] = None
    expiry: Optional[str] = "25AUG2026"
    range_points: Optional[float] = None
    slicer_count: Optional[int] = None
    slice_interval: float = 8.0
    range_high: float = 140.0
    range_low: float = 100.0
    qty_per_slice_lots: int = 1
    profit_point: float = 8.0
    loss_point: float = 8.0
    poll_interval_seconds: Optional[float] = 2.0
    spot_ltp: Optional[float] = None
    manual_opt_ltp: Optional[float] = None
    lock_strike_on_entry: Optional[bool] = True
    auto_restrike: Optional[bool] = False
    flicker_guard_ticks: Optional[int] = 3
    gap_fill_mode: Optional[str] = "all_crossed"
    cutoff_time_ist: Optional[str] = "15:22"
    auto_eod_squareoff: Optional[bool] = True
    strike_source: Optional[str] = "auto"


class TickInjectionRequest(BaseModel):
    price: float
    seq_num: Optional[int] = None


class SliceExitRequest(BaseModel):
    slice_id: Optional[str] = None
    label: Optional[str] = None
    level_price: Optional[float] = None
    order_id: Optional[str] = None
    price: Optional[float] = None


# -------------------------------------------------------------------
# Authentication Dependency & Endpoints (Phase 1)
# -------------------------------------------------------------------

async def get_current_client_id(request: Request) -> str:
    """
    Validates authentication token from:
    1. 'Authorization: Bearer <token>' header
    2. 'slicer_session' HTTP-only cookie
    3. '?token=...' query parameter (for SSE EventSource)
    Injects current_client_id into request.state.
    Raises HTTPException(401) on missing/invalid/expired/revoked session.
    """
    auth_header = request.headers.get("Authorization")
    token = None
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    if not token:
        token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        token = request.query_params.get("token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please login.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = auth_service.decode_access_token(token)
        client_id = payload.get("client_id") or payload.get("sub")
        if not client_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload: client_id missing.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        user = auth_service.get_user(client_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"User '{client_id}' not found.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not user.get("is_active", True):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"User account '{client_id}' has been deactivated.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        request.state.current_client_id = client_id
        return client_id
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(ve),
            headers={"WWW-Authenticate": "Bearer"},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.post("/auth/login")
@app.post("/api/auth/login")
async def login(req: LoginRequest):
    user = auth_service.authenticate_user(req.client_id, req.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Client ID or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_service.create_access_token(user["client_id"])
    response = JSONResponse({
        "status": "success",
        "message": "Login successful.",
        "client_id": user["client_id"],
        "role": user.get("role", "trader"),
        "access_token": token,
        "token_type": "bearer",
        "expires_in_minutes": ACCESS_TOKEN_EXPIRE_MINUTES,
    })
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return response


@app.post("/auth/logout")
@app.post("/api/auth/logout")
async def logout(request: Request, client_id: str = Depends(get_current_client_id)):
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        auth_service.revoke_token(token)

    close_client_session(client_id)
    resp = JSONResponse({"status": "success", "message": "Successfully logged out."})
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


@app.get("/auth/me")
@app.get("/api/auth/me")
async def get_me(client_id: str = Depends(get_current_client_id)):
    user = auth_service.get_user(client_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return JSONResponse({
        "status": "success",
        "client_id": user["client_id"],
        "role": user.get("role", "trader"),
        "is_active": user.get("is_active", True),
        "created_at": user.get("created_at"),
    })


@app.post("/auth/create_client")
@app.post("/api/auth/create_client")
async def create_client(req: CreateClientRequest, client_id: str = Depends(get_current_client_id)):
    try:
        new_user = auth_service.create_user(
            client_id=req.client_id,
            plain_password=req.password,
            is_active=req.is_active if req.is_active is not None else True,
            role=req.role or "trader",
        )
        return JSONResponse({
            "status": "success",
            "message": f"Client '{new_user['client_id']}' created successfully.",
            "client_id": new_user["client_id"],
            "role": new_user.get("role"),
            "is_active": new_user.get("is_active"),
        })
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))


class BrokerCredentialsRequest(BaseModel):
    broker_name: Optional[str] = "angel_one"
    broker_client_id: str
    api_key: str
    totp_secret: str
    mpin: str


class TradingModeRequest(BaseModel):
    mode: str


# -------------------------------------------------------------------
# Broker Credentials & Trading Mode Endpoints (Phase 3 & 4 Per-Client Scoped)
# -------------------------------------------------------------------

@app.get("/api/client/trading_mode")
async def get_client_trading_mode(client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    mode = credential_service.get_trading_mode(client_id)
    return JSONResponse({
        "status": "success",
        "client_id": client_id,
        "trading_mode": mode,
    })


@app.post("/api/client/trading_mode")
async def set_client_trading_mode(
    req: TradingModeRequest,
    client_id: str = Depends(get_current_client_id),
):
    mode = (req.mode or "").strip().lower()
    if mode not in ("paper", "live"):
        return JSONResponse({"error": "Invalid trading mode. Must be 'paper' or 'live'."}, status_code=400)

    credential_service.set_trading_mode(client_id=client_id, mode=mode)
    session = get_client_session(client_id)
    session.reload_trading_mode()
    await session.broadcast_state()

    return JSONResponse({
        "status": "success",
        "message": f"Trading mode set to '{mode}'.",
        "client_id": client_id,
        "trading_mode": mode,
    })


@app.get("/api/broker/orders")
@app.get("/api/order_book")
async def get_broker_orders(client_id: str = Depends(get_current_client_id)):
    """Fetches today's live order book directly from Angel One for the authenticated client."""
    session = get_client_session(client_id)
    orders = []
    if session.angel_feed:
        loop = asyncio.get_running_loop()
        try:
            orders = await loop.run_in_executor(None, session.angel_feed.get_order_book)
        except Exception as e:
            logger.warning(f"Error getting broker orders for client '{client_id}': {e}")

    return JSONResponse({
        "status": "success",
        "client_id": client_id,
        "trading_mode": getattr(session, "trading_mode", "paper"),
        "orders": orders,
        "count": len(orders),
    })


@app.post("/api/broker/credentials")
async def save_broker_credentials(
    req: BrokerCredentialsRequest,
    client_id: str = Depends(get_current_client_id),
):
    try:
        bname = (req.broker_name or "angel_one").strip().lower()
        credential_service.save_broker_credentials(
            client_id=client_id,
            broker_name=bname,
            credentials={
                "broker_client_id": req.broker_client_id,
                "api_key": req.api_key,
                "totp_secret": req.totp_secret,
                "mpin": req.mpin,
            },
        )
        session = get_client_session(client_id)
        session.reload_broker_credentials()
        await session.broadcast_state()

        status_info = credential_service.get_credential_status(client_id=client_id, broker_name=bname)
        return JSONResponse({
            "status": "success",
            "message": f"Broker credentials for '{bname}' encrypted and saved securely.",
            "client_id": client_id,
            "broker_name": bname,
            "is_configured": True,
            "masked_client_id": status_info.get("masked_client_id"),
        })
    except Exception as e:
        logger.error(f"Error saving broker credentials for client '{client_id}': {e}")
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/broker/credentials/status")
async def get_broker_credentials_status(
    broker_name: Optional[str] = Query("angel_one"),
    client_id: str = Depends(get_current_client_id),
):
    bname = (broker_name or "angel_one").strip().lower()
    status_info = credential_service.get_credential_status(client_id=client_id, broker_name=bname)
    session = get_client_session(client_id)
    return JSONResponse({
        "status": "success",
        "client_id": client_id,
        "broker_name": bname,
        "is_configured": status_info.get("is_configured", False),
        "masked_client_id": status_info.get("masked_client_id"),
        "is_authenticated": session.angel_feed.is_authenticated if session.angel_feed else False,
        "updated_at": status_info.get("updated_at"),
    })


@app.delete("/api/broker/credentials")
async def delete_broker_credentials(
    broker_name: Optional[str] = Query("angel_one"),
    client_id: str = Depends(get_current_client_id),
):
    bname = (broker_name or "angel_one").strip().lower()
    credential_service.delete_broker_credentials(client_id=client_id, broker_name=bname)
    session = get_client_session(client_id)
    session.reload_broker_credentials()
    await session.broadcast_state()
    return JSONResponse({
        "status": "success",
        "message": f"Broker credentials for '{bname}' removed.",
        "client_id": client_id,
    })


# -------------------------------------------------------------------
# REST API Endpoints (Protected by Session Auth & Client Scoped)
# -------------------------------------------------------------------

@app.post("/runs/start")
@app.post("/api/runs/start")
async def start_run(req: StartRunRequest, client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    run_manager = session.run_manager
    angel_feed = session.angel_feed
    try:
        run_id = req.run_id or f"run{len(run_manager.runs) + 1:02d}"
        inst = req.instrument_name.upper()
        spot_ltp = req.spot_ltp
        try:
            spot_val = float(spot_ltp) if spot_ltp is not None else None
        except (ValueError, TypeError):
            spot_val = None

        if spot_val is None or (inst == "SENSEX" and spot_val < 50000) or (inst == "NIFTY" and spot_val > 50000):
            spot_ltp = angel_feed.latest_spot_by_inst.get(inst) or (81000.0 if inst == "SENSEX" else 24200.0)
        else:
            spot_ltp = spot_val

        strike = req.strike
        try:
            strike_val = float(strike) if strike is not None else None
        except (ValueError, TypeError):
            strike_val = None

        existing_run = run_manager.get_run(run_id)
        if existing_run and existing_run.is_active and len(existing_run.active_slices) > 0:
            if strike_val is not None and int(strike_val) != existing_run.config.strike:
                return JSONResponse({"error": "Cannot change strike while ladder has open positions"}, status_code=400)

        strike_src = (req.strike_source or "auto").lower()
        if strike_val is None or (inst == "SENSEX" and strike_val < 50000) or (inst == "NIFTY" and strike_val > 50000):
            strike = calculate_nearest_strike(spot_ltp, inst)
            strike_src = "auto"
        else:
            strike = int(strike_val)

        contract = angel_feed.resolve_contract(strike, req.option_type.upper(), inst)
        if contract and contract.get("token"):
            real_symbol = contract["symbol"]
            real_token = str(contract["token"])
        else:
            real_symbol = f"{req.instrument_name.upper()} {strike} {req.option_type.upper()}"
            real_token = None

        # Resolve dynamic range snapshot or legacy absolute parameters
        if req.range_points is not None and req.slicer_count is not None:
            snapshot_ltp = None
            if req.manual_opt_ltp and req.manual_opt_ltp > 0:
                snapshot_ltp = req.manual_opt_ltp
            elif (
                existing_run
                and existing_run.last_ltp
                and existing_run.last_ltp > 0
                and getattr(existing_run.config, "strike", None) == strike
                and (getattr(existing_run.config, "option_type", "") or "").upper() == req.option_type.upper()
                and (getattr(existing_run.config, "instrument_name", "") or "").upper() == inst
            ):
                snapshot_ltp = existing_run.last_ltp
            elif contract and contract.get("token"):
                try:
                    fetched_ltp = angel_feed.fetch_option_ltp(contract["symbol"], contract["token"])
                    if fetched_ltp and fetched_ltp > 0:
                        snapshot_ltp = fetched_ltp
                except Exception:
                    pass
            if snapshot_ltp is None or snapshot_ltp <= 0:
                snapshot_ltp = req.range_high or (250.0 if inst == "SENSEX" else 140.0)

            range_high = round(snapshot_ltp, 2)
            range_low = round(range_high - req.range_points, 2)
            slice_interval = round(req.range_points / req.slicer_count, 4)
            r_points = req.range_points
            s_count = int(req.slicer_count)
        else:
            range_high = req.range_high
            range_low = req.range_low
            slice_interval = req.slice_interval
            r_points = round(range_high - range_low, 4)
            s_count = max(1, int(round((range_high - range_low) / slice_interval))) if slice_interval > 0 else 1

        cfg = ScalperRunConfig(
            run_id=run_id,
            instrument_name=req.instrument_name.upper(),
            option_type=req.option_type.upper(),
            strike=strike,
            strike_source=strike_src,
            expiry=req.expiry or "25AUG2026",
            range_points=r_points,
            slicer_count=s_count,
            slice_interval=slice_interval,
            range_high=range_high,
            range_low=range_low,
            qty_per_slice_lots=req.qty_per_slice_lots,
            profit_point=req.profit_point,
            loss_point=req.loss_point,
            poll_interval_seconds=req.poll_interval_seconds or 2.0,
            spot_ltp=spot_ltp,
            contract_symbol=real_symbol,
            contract_token=real_token,
            lock_strike_on_entry=req.lock_strike_on_entry if req.lock_strike_on_entry is not None else True,
            auto_restrike=req.auto_restrike or False,
            flicker_guard_ticks=req.flicker_guard_ticks if req.flicker_guard_ticks is not None else 3,
            gap_fill_mode=req.gap_fill_mode or "all_crossed",
            cutoff_time_ist=req.cutoff_time_ist or "15:22",
            auto_eod_squareoff=req.auto_eod_squareoff if req.auto_eod_squareoff is not None else True,
            trading_mode=getattr(session, "trading_mode", "paper") or "paper",
        )

        run = run_manager.start_run(cfg)

        # If manual tick provided, process it immediately
        if req.manual_opt_ltp and req.manual_opt_ltp > 0:
            run.process_tick(req.manual_opt_ltp)

        session.start_live_feed()
        run_manager.save_state()
        await session.broadcast_state()
        return JSONResponse({"status": "success", "client_id": client_id, "run": run.get_status()})

    except ValueError as ve:
        logger.warning(f"Config validation error for client '{client_id}': {ve}")
        return JSONResponse({"error": str(ve)}, status_code=400)
    except Exception as e:
        logger.error(f"Error starting run for client '{client_id}': {e}")
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/runs")
@app.get("/api/runs")
async def list_runs(client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    return JSONResponse({
        "status": "success",
        "client_id": client_id,
        "active_run_id": session.run_manager.active_run_id,
        "runs": session.run_manager.list_runs(),
    })


@app.get("/runs/{run_id}/status")
@app.get("/api/runs/{run_id}/status")
async def get_run_status(run_id: str, client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    run = session.run_manager.get_run(run_id)
    if not run:
        return JSONResponse({"error": f"Run slot '{run_id}' not found."}, status_code=404)
    return JSONResponse({"status": "success", "client_id": client_id, "run": run.get_status()})


@app.post("/runs/{run_id}/tick")
@app.post("/api/runs/{run_id}/tick")
async def inject_run_tick(
    run_id: str,
    req: Optional[TickInjectionRequest] = None,
    price: Optional[float] = Query(None),
    seq_num: Optional[int] = Query(None),
    client_id: str = Depends(get_current_client_id),
):
    session = get_client_session(client_id)
    run = session.run_manager.get_run(run_id)
    if not run:
        return JSONResponse({"error": f"Run slot '{run_id}' not found."}, status_code=404)

    tick_price = price if price is not None else (req.price if req else None)
    sequence_num = seq_num if seq_num is not None else (req.seq_num if req else None)

    if tick_price is None:
        return JSONResponse({"error": "Price must be provided via query param ?price=... or JSON payload."}, status_code=400)

    actions = run.process_tick(tick_price, seq_num=sequence_num)
    session.run_manager.save_state()
    await session.broadcast_state()
    return JSONResponse({"status": "success", "client_id": client_id, "actions": actions, "run_status": run.get_status()})


@app.post("/runs/{run_id}/stop")
@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str, client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    exited = session.run_manager.stop_run(run_id)
    if exited is None:
        return JSONResponse({"error": f"Run slot '{run_id}' not found."}, status_code=404)

    session.run_manager.save_state()
    await session.broadcast_state()
    return JSONResponse({"status": "success", "client_id": client_id, "message": f"Run {run_id} stopped.", "exited_trades": len(exited)})


@app.post("/runs/{run_id}/eod_squareoff")
@app.post("/api/runs/{run_id}/eod_squareoff")
async def run_eod_squareoff(run_id: str, price: Optional[float] = Query(None), client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    run = session.run_manager.get_run(run_id)
    if not run:
        return JSONResponse({"error": f"Run slot '{run_id}' not found."}, status_code=404)

    exited = run.eod_squareoff(exit_price=price)
    session.run_manager.save_state()
    await session.broadcast_state()
    return JSONResponse({"status": "success", "client_id": client_id, "message": f"Run {run_id} EOD squared off.", "exited_trades": len(exited)})


@app.post("/runs/{run_id}/slices/exit")
@app.post("/api/runs/{run_id}/slices/exit")
@app.post("/runs/{run_id}/slices/{slice_id}/exit")
@app.post("/api/runs/{run_id}/slices/{slice_id}/exit")
async def exit_run_slice(
    run_id: str,
    slice_id: Optional[str] = None,
    req: Optional[SliceExitRequest] = None,
    price: Optional[float] = Query(None),
    client_id: str = Depends(get_current_client_id),
):
    session = get_client_session(client_id)
    run = session.run_manager.get_run(run_id)
    if not run:
        return JSONResponse({"error": f"Run slot '{run_id}' not found."}, status_code=404)

    identifier = slice_id or (req.slice_id if req and req.slice_id else None) or (req.order_id if req and req.order_id else None) or (req.label if req and req.label else None) or (req.level_price if req and req.level_price is not None else None)
    if identifier is None:
        return JSONResponse({"error": "Slice identifier (slice_id, order_id, label, or level_price) is required."}, status_code=400)

    exit_p = (req.price if req and req.price is not None else None) or price
    trade = session.run_manager.exit_slice(run_id, identifier, exit_price=exit_p)
    if not trade:
        return JSONResponse({"error": f"Active slice '{identifier}' not found in run slot '{run_id}'."}, status_code=404)

    session.run_manager.save_state()
    await session.broadcast_state()
    return JSONResponse({
        "status": "success",
        "client_id": client_id,
        "message": f"Slice {trade.label} exited.",
        "trade": asdict(trade) if hasattr(trade, "__dataclass_fields__") else trade,
    })


@app.post("/runs/switch_active")
@app.post("/api/runs/switch_active")
async def switch_active_run(req: Dict[str, str], client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    run_id = req.get("run_id")
    if run_id and run_id in session.run_manager.runs:
        session.run_manager.active_run_id = run_id
        session.run_manager.save_state()
        await session.broadcast_state()
        return JSONResponse({"status": "success", "client_id": client_id, "active_run_id": run_id})
    return JSONResponse({"error": "Invalid run_id."}, status_code=400)


@app.post("/runs/{run_id}/instrument")
@app.post("/api/runs/{run_id}/instrument")
async def set_run_instrument(run_id: str, req: Dict[str, Any], client_id: str = Depends(get_current_client_id)):
    instrument = (req.get("instrument") or "NIFTY").upper()
    if instrument not in ("NIFTY", "SENSEX"):
        return JSONResponse({"error": "Invalid instrument"}, status_code=400)

    session = get_client_session(client_id)
    run = session.run_manager.get_run(run_id)
    if not run:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    if run.is_active and len(run.active_slices) > 0:
        return JSONResponse({"error": "Cannot change instrument while ladder has open positions"}, status_code=400)

    cfg = INSTRUMENT_CONFIG.get(instrument, INSTRUMENT_CONFIG["NIFTY"])
    run.config.instrument_name = instrument
    run.instrument_name = instrument

    # Update default range parameters if inactive
    if not run.is_active:
        run.config.range_high = cfg["default_range_high"]
        run.config.range_low = cfg["default_range_low"]
        run.config.slice_interval = cfg["default_step"]
    # Fetch live spot & option LTP for the newly selected instrument
    req_strike = req.get("strike")
    req_strike_src = req.get("strike_source")
    if req_strike_src:
        run.config.strike_source = req_strike_src

    loop = asyncio.get_running_loop()
    async with session.angel_api_lock:
        spot = await loop.run_in_executor(None, session.angel_feed.fetch_spot_ltp, instrument)
        if spot is not None:
            run.spot_ltp = spot
            nearest_strike = calculate_nearest_strike(spot, instrument)
            target_strike = None
            if req_strike:
                try:
                    target_strike = int(float(req_strike))
                except (ValueError, TypeError):
                    target_strike = None
            if getattr(run.config, "strike_source", "auto") == "manual" and target_strike:
                use_strike = target_strike
            else:
                use_strike = nearest_strike
                run.config.strike_source = "auto"

            run.config.strike = use_strike
            run.locked_strike = use_strike
            run.config.contract_symbol = f"{instrument} {use_strike} {run.config.option_type}"
            contract = session.angel_feed.resolve_contract(use_strike, run.config.option_type, instrument)
            if contract:
                run.config.contract_symbol = contract["symbol"]
                run.config.contract_token = contract["token"]
                opt_ltp = await loop.run_in_executor(None, session.angel_feed.fetch_option_ltp, contract["symbol"], contract["token"])
                if opt_ltp and opt_ltp > 0:
                    run.last_ltp = opt_ltp

    session.run_manager.save_state()
    await session.broadcast_state()
    return JSONResponse({"status": "success", "client_id": client_id, "run": run.get_status()})


@app.post("/runs/{run_id}/strike")
@app.post("/api/runs/{run_id}/strike")
async def set_run_strike(run_id: str, req: Dict[str, Any], client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    run = session.run_manager.get_run(run_id)
    if not run:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    if run.is_active and len(run.active_slices) > 0:
        return JSONResponse({"error": "Cannot change strike while ladder has open positions"}, status_code=400)

    raw_strike = req.get("strike")
    try:
        strike_val = int(float(raw_strike)) if raw_strike is not None else None
    except (ValueError, TypeError):
        strike_val = None

    if not strike_val or strike_val <= 0:
        return JSONResponse({"error": "Invalid strike price provided."}, status_code=400)

    inst = run.config.instrument_name
    if (inst == "SENSEX" and strike_val < 50000) or (inst == "NIFTY" and strike_val > 50000):
        return JSONResponse({"error": f"Strike {strike_val} is invalid for {inst}."}, status_code=400)

    strike_src = (req.get("strike_source") or "manual").lower()
    run.config.strike = strike_val
    run.config.strike_source = strike_src
    run.locked_strike = strike_val

    if session.angel_feed:
        contract = session.angel_feed.resolve_contract(strike_val, run.config.option_type, inst)
        if contract:
            run.config.contract_symbol = contract["symbol"]
            run.config.contract_token = contract["token"]
        else:
            run.config.contract_symbol = f"{inst} {strike_val} {run.config.option_type}"
    else:
        run.config.contract_symbol = f"{inst} {strike_val} {run.config.option_type}"

    session.run_manager.save_state()
    await session.broadcast_state()
    return JSONResponse({
        "status": "success",
        "client_id": client_id,
        "strike": strike_val,
        "strike_source": strike_src,
        "contract_symbol": run.config.contract_symbol,
        "run": run.get_status(),
    })


@app.post("/runs/{run_id}/cutoff_time")
@app.post("/api/runs/{run_id}/cutoff_time")
async def set_run_cutoff_time(run_id: str, req: Dict[str, Any], client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    run = session.run_manager.get_run(run_id)
    if not run:
        return JSONResponse({"error": f"Run '{run_id}' not found."}, status_code=404)

    raw_time = req.get("cutoff_time_ist") or req.get("cutoff_time")
    if not raw_time or not isinstance(raw_time, str):
        return JSONResponse({"error": "cutoff_time_ist is required (e.g. '15:25')."}, status_code=400)

    raw_time = raw_time.strip()
    parts = raw_time.split(":")
    if len(parts) != 2:
        return JSONResponse({"error": "Cutoff time must be in HH:MM format (e.g. '15:25')."}, status_code=400)

    try:
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError()
    except ValueError:
        return JSONResponse({"error": "Invalid hour or minute in cutoff time."}, status_code=400)

    formatted = f"{h:02d}:{m:02d}"
    run.config.cutoff_time_ist = formatted
    session.run_manager.save_state()
    await session.broadcast_state()
    return JSONResponse({
        "status": "success",
        "client_id": client_id,
        "run_id": run_id,
        "cutoff_time_ist": formatted,
        "run": run.get_status(),
    })


@app.post("/runs/{run_id}/clear_history")
@app.post("/api/runs/{run_id}/clear_history")
async def clear_run_history(run_id: str, client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    session.run_manager.clear_history(run_id=run_id)
    session.run_manager.save_state()
    mongo_service.clear_trades(client_id=client_id)
    await session.broadcast_state()
    return JSONResponse({"status": "success", "client_id": client_id, "message": f"Trade history cleared for Run {run_id}."})


@app.post("/history/clear")
@app.post("/api/history/clear")
async def clear_all_history(instrument: Optional[str] = Query(None), client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    session.run_manager.clear_history(instrument=instrument)
    session.run_manager.save_state()
    mongo_service.clear_trades(client_id=client_id, instrument=instrument)
    await session.broadcast_state()
    return JSONResponse({"status": "success", "client_id": client_id, "message": f"Trade history cleared for {instrument or 'ALL'}."})


@app.get("/history")
@app.get("/api/history")
async def get_trade_history(
    page: int = 1,
    limit: int = 50,
    mode: Optional[str] = Query(None),
    client_id: str = Depends(get_current_client_id),
):
    session = get_client_session(client_id)
    filter_mode = mode.strip().lower() if mode and mode.strip().lower() in ("paper", "live") else None
    # Primary database source for trade history: MongoDB Scalper per-user collection
    db_trades = mongo_service.get_recent_trades(client_id=client_id, limit=limit * page + 100, mode=filter_mode) if mongo_service.ensure_connection() else []
    all_trades = db_trades if db_trades else session.run_manager.get_all_trade_history()
    if filter_mode and not db_trades:
        all_trades = [t for t in all_trades if (t.get("mode") or "paper").strip().lower() == filter_mode]
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    return JSONResponse({
        "status": "success",
        "client_id": client_id,
        "mode": filter_mode,
        "total_trades": len(all_trades),
        "page": page,
        "limit": limit,
        "trades": all_trades[start_idx:end_idx],
    })


# -------------------------------------------------------------------
# Real-Time SSE Stream (Per-Client Scoped)
# -------------------------------------------------------------------

@app.get("/api/stream")
async def sse_stream(request: Request, client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    queue: asyncio.Queue = asyncio.Queue()
    session.sse_subscribers.append(queue)
    session.start_live_feed()

    async def event_generator():
        try:
            # Send initial state immediately to this client
            active_run = session.run_manager.get_run(session.run_manager.active_run_id) or next(iter(session.run_manager.runs.values()), None)
            active_status = active_run.get_status() if active_run else {}
            initial_payload = {
                "client_id": client_id,
                "trading_mode": getattr(session, "trading_mode", "paper"),
                "active_run_id": session.run_manager.active_run_id,
                "runs": session.run_manager.list_runs(),
                "active_run": active_status,
                "history": session.run_manager.get_all_trade_history()[:50],
                "angel_feed": session.angel_feed.get_status() if session.angel_feed else {},
                "timestamp": datetime.datetime.now(IST).isoformat(),
                "slicer_running": active_run.is_active if active_run else False,
                "spot_ltp": active_status.get("spot_ltp", 24250.0),
                "last_ltp": active_status.get("last_ltp"),
                "feed_status": active_status.get("feed_status", "LIVE"),
                "active_strike": active_status.get("strike", 24250),
                "contract_symbol": active_status.get("contract_symbol", "NIFTY 24250 CE"),
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
            yield f"data: {json.dumps(initial_payload)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield message
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if queue in session.sse_subscribers:
                session.sse_subscribers.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# -------------------------------------------------------------------
# Compatibility & Angel One Endpoints
# -------------------------------------------------------------------

@app.get("/api/state")
async def get_state(client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    active_run = session.run_manager.get_run(session.run_manager.active_run_id) or next(iter(session.run_manager.runs.values()), None)
    active_status = active_run.get_status() if active_run else {}

    db_trades = mongo_service.get_recent_trades(client_id=client_id, limit=200) if mongo_service.ensure_connection() else []
    mem_trades = session.run_manager.get_all_trade_history()
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

    return JSONResponse({
        "client_id": client_id,
        "trading_mode": getattr(session, "trading_mode", "paper"),
        "active_run_id": session.run_manager.active_run_id,
        "runs": session.run_manager.list_runs(),
        "active_run": active_status,
        "history": combined_history[:200],
        "slicer_running": active_run.is_active if active_run else False,
        "spot_ltp": active_status.get("spot_ltp", 24250.0),
        "last_ltp": active_status.get("last_ltp"),
        "feed_status": active_status.get("feed_status", "LIVE"),
        "active_strike": active_status.get("strike", 24250),
        "contract_symbol": active_status.get("contract_symbol", "NIFTY 24250 CE"),
        "ladder_levels": active_status.get("grid_ladder", []),
        "open_slices": active_status.get("active_slices", []),
        "closed_slices": active_status.get("trade_history", []),
        "gross_realized_pnl": active_status.get("gross_realized_pnl", active_status.get("accumulated_realized_pnl", 0.0)),
        "today_realized_pnl": active_status.get("today_realized_pnl", 0.0),
        "total_realized_pnl": active_status.get("accumulated_realized_pnl", 0.0),
        "unrealized_pnl": active_status.get("unrealized_pnl_rupees", 0.0),
        "total_pnl": active_status.get("total_pnl", 0.0),
        "active_loss_trigger": active_status.get("loss_trigger_price"),
        "ladder_cycle_id": active_status.get("cycle_count", 1),
        "config": active_status.get("config", {}),
        "recent_audit_logs": active_status.get("audit_events", []),
        "angel_feed": session.angel_feed.get_status() if session.angel_feed else {},
        "broker_credential_status": credential_service.get_credential_status(client_id),
        "mongo": mongo_service.get_status(client_id=client_id),
    })


@app.get("/api/mongo/status")
async def get_mongo_status(client_id: str = Depends(get_current_client_id)):
    return JSONResponse({
        "status": "success",
        "client_id": client_id,
        "mongo": mongo_service.get_status(client_id=client_id),
        "recent_trades": mongo_service.get_recent_trades(client_id=client_id, limit=10),
    })


# -------------------------------------------------------------------
# Astro CSV Signal Engine Endpoints (Collection: Astro.{client_id})
# -------------------------------------------------------------------

@app.post("/api/astro/upload")
async def upload_astro_csv(file: UploadFile = File(...), client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    try:
        content_bytes = await file.read()
        df, metadata = parse_astro_csv(content_bytes)
        file_id = mongo_service.save_astro_file(
            client_id=client_id,
            filename=file.filename or "astro_report.csv",
            content=content_bytes.decode("utf-8-sig", errors="replace"),
            row_count=metadata["row_count"],
            is_active=True,
        )
        session._parsed_astro_cache = {"file_id": str(file_id), "df": df}
        await session.broadcast_state()
        return JSONResponse({
            "status": "success",
            "client_id": client_id,
            "file_id": str(file_id),
            "filename": file.filename,
            "row_count": metadata["row_count"],
            "metadata": metadata,
        })
    except Exception as e:
        logger.error(f"Error parsing/saving astro CSV for client {client_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/astro/files")
async def list_astro_files(client_id: str = Depends(get_current_client_id)):
    files = mongo_service.list_astro_files(client_id=client_id)
    return JSONResponse({"status": "success", "client_id": client_id, "count": len(files), "files": files})


@app.post("/api/astro/files/{file_id}/activate")
async def activate_astro_file(file_id: str, client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    ok = mongo_service.set_active_astro_file(client_id=client_id, file_id=file_id)
    if not ok:
        return JSONResponse({"error": f"Astro file '{file_id}' not found."}, status_code=404)
    session._parsed_astro_cache = None
    await session.broadcast_state()
    return JSONResponse({"status": "success", "client_id": client_id, "active_file_id": file_id})


@app.delete("/api/astro/files/{file_id}")
async def delete_astro_file(file_id: str, client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    ok = mongo_service.delete_astro_file(client_id=client_id, file_id=file_id)
    if not ok:
        return JSONResponse({"error": f"Astro file '{file_id}' not found."}, status_code=404)
    session._parsed_astro_cache = None
    await session.broadcast_state()
    return JSONResponse({"status": "success", "client_id": client_id, "deleted_file_id": file_id})


class AstroToggleRequest(BaseModel):
    slot_id: Optional[str] = None
    run_id: Optional[str] = None
    enabled: bool


@app.post("/api/astro/toggle")
async def toggle_astro_auto_trigger(req: AstroToggleRequest, client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    slot_to_toggle = req.slot_id or req.run_id or "all"
    session.astro_auto_trigger["all"] = req.enabled
    session.astro_auto_trigger["run01"] = req.enabled
    session.astro_auto_trigger["run02"] = req.enabled
    session.astro_auto_trigger["run03"] = req.enabled
    session.astro_auto_trigger["run04"] = req.enabled
    for s in list(session.astro_auto_trigger.keys()):
        session.astro_auto_trigger[s] = req.enabled
    logger.info(f"[Astro Toggle API] Client '{client_id}' set Astro auto-trigger across all slots to: {req.enabled}")
    await session.broadcast_state()
    return JSONResponse({
        "status": "success",
        "client_id": client_id,
        "slot_id": slot_to_toggle,
        "enabled": req.enabled,
        "auto_trigger_by_slot": session.astro_auto_trigger,
    })


@app.get("/api/astro/status")
async def get_astro_status(client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    active_file = mongo_service.get_active_astro_file(client_id=client_id)
    preview = None
    if active_file and active_file.get("content"):
        try:
            file_id_str = str(active_file.get("_id"))
            if not session._parsed_astro_cache or session._parsed_astro_cache.get("file_id") != file_id_str:
                df_parsed, _ = parse_astro_csv(active_file["content"])
                session._parsed_astro_cache = {"file_id": file_id_str, "df": df_parsed}
            else:
                df_parsed = session._parsed_astro_cache["df"]

            now_ist = datetime.datetime.now(IST)
            preview = astro_signal_engine.evaluate_cluster(
                df=df_parsed,
                current_dt=now_ist,
                last_trade_direction=session.last_astro_direction.get("NIFTY"),
                has_active_trade=False,
                cutoff_time_ist="15:22",
                instrument="NIFTY",
            )
        except Exception as e:
            preview = {"error": str(e)}

    return JSONResponse({
        "status": "success",
        "client_id": client_id,
        "has_active_file": bool(active_file),
        "active_file": {
            "file_id": str(active_file.get("_id")) if active_file else None,
            "filename": active_file.get("filename") if active_file else None,
            "row_count": active_file.get("row_count", 0) if active_file else 0,
            "uploaded_at": active_file.get("uploaded_at") if active_file else None,
        } if active_file else None,
        "cluster_preview": preview,
        "preview": preview,
        "alternation": session.last_astro_direction,
        "auto_trigger_by_slot": session.astro_auto_trigger,
    })


@app.post("/api/angel/fetch_spot")
async def fetch_angel_spot(req: Optional[Dict[str, Any]] = None, client_id: str = Depends(get_current_client_id)):
    body = req or {}
    session = get_client_session(client_id)
    target_run_id = body.get("run_id") or session.run_manager.active_run_id
    active_run = session.run_manager.get_run(target_run_id) or session.run_manager.get_run(session.run_manager.active_run_id)
    instrument = (body.get("instrument") or (active_run.config.instrument_name if active_run else "NIFTY")).upper()
    if instrument not in ("NIFTY", "SENSEX"):
        instrument = "NIFTY"

    loop = asyncio.get_running_loop()
    async with session.angel_api_lock:
        spot = await loop.run_in_executor(None, session.angel_feed.fetch_spot_ltp, instrument)

    if spot is not None:
        nearest_strike = calculate_nearest_strike(spot, instrument)
        if active_run:
            active_run.spot_ltp = spot
            strike_src = getattr(active_run.config, "strike_source", "auto")
            if not active_run.is_active:
                active_run.config.instrument_name = instrument
                active_run.config.spot_ltp = spot
                if strike_src != "manual":
                    active_run.config.strike = nearest_strike
                    active_run.locked_strike = nearest_strike
                    active_run.config.contract_symbol = f"{instrument} {nearest_strike} {active_run.config.option_type}"
                else:
                    active_run.config.contract_symbol = f"{instrument} {active_run.config.strike} {active_run.config.option_type}"
            else:
                active_run.handle_spot_update(spot)
        session.run_manager.save_state()
        await session.broadcast_state()
        effective_strike = active_run.config.strike if (active_run and getattr(active_run.config, "strike_source", "auto") == "manual") else nearest_strike
        return {"status": "success", "client_id": client_id, "instrument": instrument, "spot_ltp": spot, "strike": effective_strike, "nearest_strike": nearest_strike, "run_id": active_run.run_id if active_run else target_run_id}

    err_msg = (session.angel_feed.error_message if session.angel_feed else None) or f"Failed to fetch {instrument} Spot price from Angel One."
    return JSONResponse({"status": "error", "message": err_msg}, status_code=500)


@app.post("/api/angel/fetch_option")
async def fetch_angel_option(req: Optional[Dict[str, Any]] = None, client_id: str = Depends(get_current_client_id)):
    """Fetch live Option LTP. Accepts optional instrument, option_type, strike, and run_id in body."""
    body = req or {}
    loop = asyncio.get_running_loop()
    session = get_client_session(client_id)
    target_run_id = body.get("run_id") or session.run_manager.active_run_id
    active_run = session.run_manager.get_run(target_run_id) or session.run_manager.get_run(session.run_manager.active_run_id)

    instrument = (body.get("instrument") or (active_run.config.instrument_name if active_run else "NIFTY")).upper()
    if instrument not in ("NIFTY", "SENSEX"):
        instrument = "NIFTY"

    if active_run and active_run.is_active:
        if len(active_run.active_slices) > 0 and body.get("strike") is not None:
            try:
                check_stk = int(float(body.get("strike")))
                if check_stk != active_run.config.strike:
                    return JSONResponse({"status": "error", "message": "Cannot change strike while ladder has open positions"}, status_code=400)
            except (ValueError, TypeError):
                pass
        strike = active_run.config.strike
        option_type = active_run.config.option_type
        instrument = active_run.config.instrument_name
    else:
        option_type = (body.get("option_type") or "").upper().strip()
        if option_type not in ("CE", "PE"):
            option_type = active_run.config.option_type if active_run else "CE"

        strike = body.get("strike")
        strike_val = None
        if strike is not None:
            try:
                strike_val = float(strike)
            except (ValueError, TypeError):
                strike_val = None

        if strike_val is None or (instrument == "SENSEX" and strike_val < 50000) or (instrument == "NIFTY" and strike_val > 50000):
            strike_src = getattr(active_run.config, "strike_source", "auto") if active_run else "auto"
            if strike_src == "manual" and active_run and active_run.config.strike:
                strike = active_run.config.strike
            elif active_run and getattr(active_run, "instrument_name", active_run.config.instrument_name) == instrument and active_run.spot_ltp and ((instrument == "SENSEX" and active_run.spot_ltp >= 50000) or (instrument == "NIFTY" and active_run.spot_ltp < 50000)):
                strike = calculate_nearest_strike(active_run.spot_ltp, instrument)
            elif session.angel_feed.latest_spot_by_inst.get(instrument):
                strike = calculate_nearest_strike(session.angel_feed.latest_spot_by_inst[instrument], instrument)
            else:
                strike = 81000 if instrument == "SENSEX" else 24200
        else:
            strike = int(strike_val)

    async with session.angel_api_lock:
        contract = session.angel_feed.resolve_contract(int(strike), option_type, instrument)
        if not contract:
            return JSONResponse(
                {"status": "error", "message": f"Could not resolve {instrument} {strike} {option_type} contract from instrument master."},
                status_code=404,
            )

        if active_run and not active_run.is_active:
            active_run.config.instrument_name = instrument
            active_run.config.contract_symbol = contract["symbol"]
            active_run.config.contract_token = contract["token"]
            active_run.config.strike = int(strike)
            active_run.config.option_type = option_type
            if body.get("strike_source"):
                active_run.config.strike_source = body.get("strike_source")

        opt_ltp = await loop.run_in_executor(None, session.angel_feed.fetch_option_ltp, contract["symbol"], contract["token"])

    if opt_ltp is not None and opt_ltp > 0:
        session.angel_feed.latest_option_ltp = opt_ltp
        if active_run:
            active_run.last_ltp = opt_ltp
            active_run.update_feed_health(True)
            if active_run.is_active:
                active_run.process_tick(opt_ltp)
            session.run_manager.save_state()
        await session.broadcast_state()
        return {
            "status": "success",
            "client_id": client_id,
            "instrument": instrument,
            "option_ltp": opt_ltp,
            "contract": contract["symbol"],
            "strike": int(strike),
            "strike_source": getattr(active_run.config, "strike_source", "auto") if active_run else "auto",
            "option_type": option_type,
        }


@app.get("/api/angel/available_strikes")
async def get_available_strikes_api(
    instrument: str = Query("NIFTY"),
    spot: Optional[float] = Query(None),
    client_id: str = Depends(get_current_client_id),
):
    inst = instrument.upper()
    if inst not in ("NIFTY", "SENSEX"):
        inst = "NIFTY"

    session = get_client_session(client_id)
    active_run = session.run_manager.get_run(session.run_manager.active_run_id)

    spot_val = spot
    if spot_val is None or spot_val <= 0 or (inst == "SENSEX" and spot_val < 50000) or (inst == "NIFTY" and spot_val > 50000):
        if session.angel_feed and session.angel_feed.latest_spot_by_inst.get(inst):
            spot_val = session.angel_feed.latest_spot_by_inst[inst]
        elif active_run and active_run.spot_ltp and ((inst == "SENSEX" and active_run.spot_ltp >= 50000) or (inst == "NIFTY" and active_run.spot_ltp < 50000)):
            spot_val = active_run.spot_ltp
        else:
            spot_val = 81000.0 if inst == "SENSEX" else 24200.0

    atm = calculate_nearest_strike(spot_val, inst)
    if session.angel_feed:
        strikes = session.angel_feed.get_available_strikes(spot_val, inst, count_each_side=15)
    else:
        step = 100 if inst == "SENSEX" else 50
        strikes = [atm + (i * step) for i in range(-15, 16)]

    return JSONResponse({
        "status": "success",
        "instrument": inst,
        "spot": spot_val,
        "atm_strike": atm,
        "strikes": strikes,
    })

    if active_run:
        active_run.update_feed_health(False)
        session.run_manager.save_state()

    err_msg = (session.angel_feed.error_message if session.angel_feed else None) or f"Failed to fetch Option LTP for {instrument} {strike} {option_type}."
    return JSONResponse({"status": "error", "message": err_msg}, status_code=500)


# Single-run compatibility endpoints
@app.post("/api/slicer/run")
async def compat_slicer_run(req: StartRunRequest, client_id: str = Depends(get_current_client_id)):
    return await start_run(req, client_id=client_id)


@app.post("/api/slicer/stop")
async def compat_slicer_stop(client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    return await stop_run(session.run_manager.active_run_id, client_id=client_id)


@app.post("/api/tick")
async def compat_process_tick(req: TickInjectionRequest, client_id: str = Depends(get_current_client_id)):
    session = get_client_session(client_id)
    return await inject_run_tick(session.run_manager.active_run_id, req, client_id=client_id)


# Mount Static Files
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def serve_index():
    from fastapi.responses import FileResponse
    index_file = os.path.join(static_dir, "index.html")
    return FileResponse(index_file)


# -------------------------------------------------------------------
# Global EOD Guardian (Strict 3:22 PM IST Exit for All Open Trades)
# -------------------------------------------------------------------
async def _global_eod_guardian_loop():
    """
    Background watchdog running continuously every 3 seconds.
    Guarantees that at 3:22 PM IST (15:22), ALL active trades across ALL slots
    and all clients are automatically squared off and exited.
    """
    logger.info("Global EOD Guardian task started (target cutoff: 15:22 IST / 3:22 PM).")
    while True:
        try:
            ist_now = datetime.datetime.now(IST)
            current_time = ist_now.time()
            with client_sessions_lock:
                sessions = list(client_sessions.values())

            for session in sessions:
                for run in session.run_manager.runs.values():
                    if run.config.auto_eod_squareoff and (run.is_active or len(run.active_slices) > 0):
                        if run.check_eod_cutoff(current_time):
                            logger.info(f"[EOD Guardian] Auto-squared off Slot {run.run_id} at {run.config.cutoff_time_ist} IST for client '{session.client_id}'.")
                            asyncio.create_task(session.broadcast_state())
        except Exception as e:
            logger.error(f"Error in global EOD guardian loop: {e}")
        await asyncio.sleep(3.0)


# Startup Event
@app.on_event("startup")
async def on_startup():
    logger.info("Application starting: initializing admin session...")
    admin_session = get_client_session("admin")
    admin_session.start_live_feed()
    logger.info(f"Admin session active: {len(admin_session.run_manager.runs)} slots, {len(admin_session.run_manager.get_all_trade_history())} trades.")

    loop = asyncio.get_running_loop()
    if admin_session.angel_feed:
        try:
            await loop.run_in_executor(None, admin_session.angel_feed._load_instruments)
        except Exception as e:
            logger.warning(f"Background instrument pre-cache note: {e}")

    asyncio.create_task(_global_eod_guardian_loop())


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("Application shutting down: tearing down active client sessions...")
    with client_sessions_lock:
        for session in list(client_sessions.values()):
            session.close()
        client_sessions.clear()



if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
