"""FastAPI for Moonshot Paper Trading.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional

from moonshot.paper.runner import PaperRunner

logger = logging.getLogger(__name__)

# Registered by __main__.py
_runner: Optional[PaperRunner] = None

app = FastAPI(title="Duo-Moonshot Paper Trading")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Simplified for standalone
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class StatusResponse(BaseModel):
    running: bool
    capital: float
    open_positions: int
    total_trades: int

@app.get("/status", response_model=StatusResponse)
async def get_status():
    if not _runner:
        return {"running": False, "capital": 0.0, "open_positions": 0, "total_trades": 0}
    
    return {
        "running": _runner._running,
        "capital": float(_runner.account.capital),
        "open_positions": _runner.store.position_count(),
        "total_trades": _runner.store.get_trade_count()
    }

@app.get("/positions")
async def get_positions():
    if not _runner: return []
    positions = _runner.store.get_open_positions()
    for p in positions:
        price = _runner.ws_feed.get_price(p.symbol)
        if price is None:
            price_val = await _runner.feed.get_current_price(p.symbol)
            if price_val: price = price_val
        if price:
            p.current_price = price
            actual_pct = (p.entry_price - price) / p.entry_price
            p.profit_pct = actual_pct * p.leverage * 100
            p.unrealized_pnl = p.invest_amount * p.profit_pct / 100
    return positions

@app.get("/trades")
async def get_trades(limit: int = 50):
    return _runner.store.get_trades(limit=limit) if _runner else []

@app.get("/logs")
async def get_logs(limit: int = 100):
    return _runner.store.get_events(limit=limit) if _runner else []

@app.get("/equity")
async def get_equity():
    return _runner.store.get_equity_curve() if _runner else []

@app.post("/start")
async def start_runner():
    if _runner: await _runner.start()
    return {"message": "Started"}

@app.post("/stop")
async def stop_runner():
    if _runner: await _runner.stop()
    return {"message": "Stopped"}

@app.post("/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    if _runner: background_tasks.add_task(_runner.scanner.scan)
    return {"message": "Scan scheduled"}

@app.get("/pending_st")
async def get_pending_st():
    if not _runner: return []
    return [s.model_dump() for s in _runner.store.get_pending_st_signals()]

@app.get("/prices")
async def get_prices():
    """Real-time prices for marquee ticker (from WS feed with REST fallback)."""
    symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
    result = {}
    if not _runner:
        return result
    for sym in symbols:
        price = _runner.ws_feed.get_price(sym)
        if price is None:
            # REST fallback
            price_val = await _runner.feed.get_current_price(sym)
            if price_val:
                price = price_val
        if price:
            result[sym] = round(price, 2)
    return result

@app.get("/ws_status")
async def get_ws_status():
    """WebSocket feed connection health."""
    if not _runner:
        return {"connected": False, "symbols": 0}
    return {
        "connected": _runner.ws_feed.is_connected,
        "symbols": _runner.ws_feed.symbol_count,
    }

@app.get("/stream")
async def stream_data():
    """SSE stream — pushes positions + status + ticker prices every 2s."""
    async def event_generator():
        try:
            while True:
                if not _runner:
                    yield f"data: {json.dumps({'error': 'no runner'})}\n\n"
                    await asyncio.sleep(5)
                    continue

                # Build positions with live prices
                positions = _runner.store.get_open_positions()
                pos_list = []
                for p in positions:
                    price = _runner.ws_feed.get_price(p.symbol)
                    if price is None:
                        price = await _runner.feed.get_current_price(p.symbol)
                    if price:
                        p.current_price = price
                        actual_pct = (p.entry_price - price) / p.entry_price
                        p.profit_pct = actual_pct * p.leverage * 100
                        p.unrealized_pnl = p.invest_amount * p.profit_pct / 100
                    pos_list.append(p.model_dump())

                # Ticker prices
                ticker = {}
                for sym in ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]:
                    pr = _runner.ws_feed.get_price(sym)
                    if pr: ticker[sym] = round(pr, 2)

                payload = {
                    "status": {
                        "running": _runner._running,
                        "capital": float(_runner.account.capital),
                        "open_positions": _runner.store.position_count(),
                        "total_trades": _runner.store.get_trade_count(),
                    },
                    "positions": pos_list,
                    "prices": ticker,
                    "ws_connected": _runner.ws_feed.is_connected,
                }
                yield f"data: {json.dumps(payload, default=str)}\n\n"
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            return

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/summary")
async def get_summary():
    """Compute aggregated performance metrics from all closed trades."""
    if not _runner:
        return {"error": "Runner not initialized"}

    trades = _runner.store.get_trades(limit=9999)
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0, "total_pnl": 0,
            "initial_capital": 10000, "current_capital": float(_runner.account.capital),
            "symbols": {},
        }

    wins = [t for t in trades if (t.get("net_pnl") or 0) > 0]
    losses = [t for t in trades if (t.get("net_pnl") or 0) <= 0]
    win_amounts = [t["net_pnl"] for t in wins if t.get("net_pnl") is not None]
    loss_amounts = [t["net_pnl"] for t in losses if t.get("net_pnl") is not None]
    total_win = sum(win_amounts)
    total_loss = abs(sum(loss_amounts))

    # Holding hours
    from datetime import datetime
    hold_hours = []
    for t in trades:
        try:
            et = datetime.fromisoformat(t.get("entry_time", ""))
            xt = datetime.fromisoformat(t.get("exit_time", ""))
            hold_hours.append((xt - et).total_seconds() / 3600)
        except Exception:
            pass

    win_hold = [h for h, t in zip(hold_hours, trades) if (t.get("net_pnl") or 0) > 0]
    loss_hold = [h for h, t in zip(hold_hours, trades) if (t.get("net_pnl") or 0) <= 0]

    # Per-symbol breakdown
    symbols: dict = {}
    for t in trades:
        s = t.get("symbol", "?")
        if s not in symbols:
            symbols[s] = {"trades": 0, "wins": 0, "total_pnl": 0}
        symbols[s]["trades"] += 1
        if (t.get("net_pnl") or 0) > 0:
            symbols[s]["wins"] += 1
        symbols[s]["total_pnl"] += t.get("net_pnl") or 0

    for s in symbols.values():
        s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else 0
        s["total_pnl"] = round(s["total_pnl"], 2)

    # Max consecutive losses
    max_consec = cur = 0
    for t in reversed(trades):  # trades are DESC, reverse to ASC
        if (t.get("net_pnl") or 0) <= 0:
            cur += 1
            max_consec = max(max_consec, cur)
        else:
            cur = 0

    # Equity curve from store
    equity = _runner.store.get_equity_curve()
    initial_cap = 10000.0
    saved_init = _runner.store.get_state("initial_capital")
    if saved_init:
        initial_cap = float(saved_init)

    current_cap = float(_runner.account.capital)
    total_pnl = sum(t.get("net_pnl", 0) for t in trades)

    return {
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round((current_cap / initial_cap - 1) * 100, 2) if initial_cap else 0,
        "initial_capital": initial_cap,
        "current_capital": round(current_cap, 2),
        "profit_factor": round(total_win / total_loss, 2) if total_loss > 0 else 99,
        "avg_hold_hours": round(sum(hold_hours) / len(hold_hours), 1) if hold_hours else 0,
        "avg_win_hold": round(sum(win_hold) / len(win_hold), 1) if win_hold else 0,
        "avg_loss_hold": round(sum(loss_hold) / len(loss_hold), 1) if loss_hold else 0,
        "max_consecutive_losses": max_consec,
        "added_positions": sum(1 for t in trades if t.get("has_added_position")),
        "symbols": symbols,
        "equity_curve": equity,
    }

# ── Static file serving (production frontend) ────────────────────
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

_FRONTEND_DIR = Path(__file__).parent / "frontend" / "dist"


@app.get("/app/{full_path:path}")
async def serve_spa(full_path: str):
    """SPA fallback — serve index.html for all frontend routes."""
    file_path = _FRONTEND_DIR / full_path
    if file_path.is_file():
        return FileResponse(file_path)
    index = _FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"error": "Frontend not built. Run: cd moonshot/paper/frontend && npm run build"}


if _FRONTEND_DIR.exists():
    # Mount static assets (js, css, images)
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
