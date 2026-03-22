"""FastAPI for Moonshot Paper Trading.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from fastapi import FastAPI, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional

from moonshot.paper.runner import PaperRunner, DualPaperRunner

logger = logging.getLogger(__name__)

# Registered by __main__.py
_runner: Optional[PaperRunner | DualPaperRunner] = None


def _is_dual_run() -> bool:
    return isinstance(_runner, DualPaperRunner)

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

@app.get("/status")
async def get_status():
    if not _runner:
        return {"running": False, "capital": 0.0, "open_positions": 0, "total_trades": 0, "mode": "single"}

    if _is_dual_run():
        return {
            "running": _runner._running,
            "mode": "dual",
            "daily": {
                "capital": float(_runner.daily_account.capital),
                "open_positions": _runner.daily_store.position_count(),
                "total_trades": _runner.daily_store.get_trade_count(),
            },
            "rolling": {
                "capital": float(_runner.rolling_account.capital),
                "open_positions": _runner.rolling_store.position_count(),
                "total_trades": _runner.rolling_store.get_trade_count(),
            },
        }
    return {
        "running": _runner._running,
        "mode": "single",
        "capital": float(_runner.account.capital),
        "open_positions": _runner.store.position_count(),
        "total_trades": _runner.store.get_trade_count(),
    }

@app.get("/positions")
async def get_positions(strategy: Optional[str] = Query(None, description="daily|rolling (dual mode only)")):
    if not _runner:
        return [] if not _is_dual_run() else {"daily": [], "rolling": []}

    async def _enrich(positions, store_key: str = ""):
        out = []
        for p in positions:
            price = _runner.ws_feed.get_price(p.symbol)
            if price is None:
                price_val = await _runner.feed.get_current_price(p.symbol)
                if price_val:
                    price = price_val
            if price and p.entry_price and p.entry_price > 0:
                p.current_price = price
                actual_pct = (p.entry_price - price) / p.entry_price
                p.profit_pct = actual_pct * p.leverage * 100
                p.unrealized_pnl = p.invest_amount * p.profit_pct / 100
            d = p.model_dump()
            if store_key:
                d["strategy"] = store_key
            out.append(d)
        return out

    if _is_dual_run():
        if strategy == "daily":
            return await _enrich(_runner.daily_store.get_open_positions(), "daily")
        if strategy == "rolling":
            return await _enrich(_runner.rolling_store.get_open_positions(), "rolling")
        return {
            "daily": await _enrich(_runner.daily_store.get_open_positions(), "daily"),
            "rolling": await _enrich(_runner.rolling_store.get_open_positions(), "rolling"),
        }

    positions = _runner.store.get_open_positions()
    return await _enrich(positions)

@app.get("/trades")
async def get_trades(limit: int = 50, strategy: Optional[str] = Query(None, description="daily|rolling (dual mode only)")):
    if not _runner:
        return [] if not _is_dual_run() else {"daily": [], "rolling": []}
    if _is_dual_run():
        if strategy == "daily":
            return _runner.daily_store.get_trades(limit=limit)
        if strategy == "rolling":
            return _runner.rolling_store.get_trades(limit=limit)
        return {
            "daily": _runner.daily_store.get_trades(limit=limit),
            "rolling": _runner.rolling_store.get_trades(limit=limit),
        }
    return _runner.store.get_trades(limit=limit)

@app.get("/logs")
async def get_logs(limit: int = 100, strategy: Optional[str] = Query(None, description="daily|rolling (dual mode only)")):
    if not _runner:
        return []
    if _is_dual_run():
        if strategy == "daily":
            return _runner.daily_store.get_events(limit=limit)
        if strategy == "rolling":
            return _runner.rolling_store.get_events(limit=limit)
        # Merge both, sort by timestamp desc (events have timestamp)
        d = _runner.daily_store.get_events(limit=limit)
        r = _runner.rolling_store.get_events(limit=limit)
        for e in d:
            e["strategy"] = "daily"
        for e in r:
            e["strategy"] = "rolling"
        merged = sorted(d + r, key=lambda x: x.get("timestamp", ""), reverse=True)
        return merged[:limit]
    return _runner.store.get_events(limit=limit)

@app.get("/equity")
async def get_equity(strategy: Optional[str] = Query(None, description="daily|rolling (dual mode only)")):
    if not _runner:
        return [] if not _is_dual_run() else {"daily": [], "rolling": []}
    if _is_dual_run():
        if strategy == "daily":
            return _runner.daily_store.get_equity_curve()
        if strategy == "rolling":
            return _runner.rolling_store.get_equity_curve()
        return {
            "daily": _runner.daily_store.get_equity_curve(),
            "rolling": _runner.rolling_store.get_equity_curve(),
        }
    return _runner.store.get_equity_curve()

@app.post("/start")
async def start_runner():
    if _runner: await _runner.start()
    return {"message": "Started"}

@app.post("/stop")
async def stop_runner():
    if _runner: await _runner.stop()
    return {"message": "Stopped"}

@app.post("/scan")
async def trigger_scan(background_tasks: BackgroundTasks, strategy: Optional[str] = Query(None)):
    if not _runner:
        return {"message": "Runner not started"}
    if _is_dual_run():
        if strategy == "daily":
            background_tasks.add_task(_runner.daily_scanner.scan)
        elif strategy == "rolling":
            background_tasks.add_task(_runner.rolling_scanner.scan)
        else:
            background_tasks.add_task(_runner.daily_scanner.scan)
            background_tasks.add_task(_runner.rolling_scanner.scan)
    else:
        background_tasks.add_task(_runner.scanner.scan)
    return {"message": "Scan scheduled"}

@app.get("/pending_st")
async def get_pending_st(strategy: Optional[str] = Query(None)):
    if not _runner:
        return []
    if _is_dual_run():
        if strategy == "rolling":
            return []  # Rolling has no ST gate
        return [s.model_dump() for s in _runner.daily_store.get_pending_st_signals()]
    return [s.model_dump() for s in _runner.store.get_pending_st_signals()]

_top_gainers_cache: dict = {"data": [], "ts": 0}
_TOP_GAINERS_BUFFER = 120  # refresh 2 min after each hour boundary

async def _refresh_top_gainers_if_stale():
    """Refresh top gainers when cache is older than 1 hour (a bit past hour boundary)."""
    import time
    now = time.time()
    ts = _top_gainers_cache.get("ts", 0)
    # Refresh when: cache empty, or we've passed an hour boundary + 2 min
    if not _top_gainers_cache["data"] or ts == 0:
        need_refresh = True
    else:
        last_hour = int(ts / 3600)
        next_refresh = (last_hour + 1) * 3600 + _TOP_GAINERS_BUFFER
        need_refresh = now >= next_refresh
    if need_refresh:
        if not _runner:
            return
        try:
            tradeable = set(await _runner.feed.get_usdt_symbols())  # 仅 TRADING 状态，排除 SETTLING/下架中
            tickers = await _runner.client.get_24hr_tickers()
            usdt_perps = [
                t for t in tickers
                if t.get("symbol", "") in tradeable
                and float(t.get("priceChangePercent", 0)) > 0
            ]
            usdt_perps.sort(key=lambda t: float(t.get("priceChangePercent", 0)), reverse=True)
            _top_gainers_cache["data"] = [
                {
                    "symbol": t["symbol"],
                    "pct_chg": round(float(t["priceChangePercent"]), 2),
                    "price": t.get("lastPrice", "0"),
                    "volume": round(float(t.get("quoteVolume", 0)) / 1e8, 2),
                }
                for t in usdt_perps[:10]
            ]
            _top_gainers_cache["ts"] = time.time()
        except Exception as e:
            logger.warning("top_gainers refresh failed: %s", e)

@app.get("/top_gainers")
async def get_top_gainers():
    """Return top 10 USDT-M futures 24h gainers (cached 1h, refresh ~2min past each hour)."""
    await _refresh_top_gainers_if_stale()
    return _top_gainers_cache["data"]

@app.get("/scan_results")
async def get_scan_results(strategy: Optional[str] = Query(None, description="daily|rolling (dual mode only)")):
    """Return last scan snapshot (gainers + filter status)."""
    if not _runner:
        return None
    if _is_dual_run():
        if strategy == "daily":
            raw = _runner.daily_store.get_state("last_scan")
        elif strategy == "rolling":
            raw = _runner.rolling_store.get_state("last_scan")
        else:
            raw_d = _runner.daily_store.get_state("last_scan")
            raw_r = _runner.rolling_store.get_state("last_scan")
            try:
                return {
                    "daily": json.loads(raw_d) if raw_d else None,
                    "rolling": json.loads(raw_r) if raw_r else None,
                }
            except Exception:
                return {"daily": None, "rolling": None}
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None
    raw = _runner.store.get_state("last_scan")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

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

                async def _build_pos_list(positions):
                    out = []
                    for p in positions:
                        price = _runner.ws_feed.get_price(p.symbol)
                        if price is None:
                            price = await _runner.feed.get_current_price(p.symbol)
                        if price and p.entry_price and p.entry_price > 0:
                            p.current_price = price
                            actual_pct = (p.entry_price - price) / p.entry_price
                            p.profit_pct = actual_pct * p.leverage * 100
                            p.unrealized_pnl = p.invest_amount * p.profit_pct / 100
                        d = p.model_dump()
                        out.append(d)
                    return out

                asyncio.create_task(_refresh_top_gainers_if_stale())

                if _is_dual_run():
                    pos_daily = await _build_pos_list(_runner.daily_store.get_open_positions())
                    pos_rolling = await _build_pos_list(_runner.rolling_store.get_open_positions())
                    for d in pos_daily:
                        d["strategy"] = "daily"
                    for d in pos_rolling:
                        d["strategy"] = "rolling"
                    raw_d = _runner.daily_store.get_state("last_scan")
                    raw_r = _runner.rolling_store.get_state("last_scan")
                    try:
                        scan_d = json.loads(raw_d) if raw_d else None
                    except (json.JSONDecodeError, TypeError):
                        scan_d = None
                    try:
                        scan_r = json.loads(raw_r) if raw_r else None
                    except (json.JSONDecodeError, TypeError):
                        scan_r = None
                    payload = {
                        "mode": "dual",
                        "status": {
                            "running": _runner._running,
                            "daily": {
                                "capital": float(_runner.daily_account.capital),
                                "open_positions": _runner.daily_store.position_count(),
                                "total_trades": _runner.daily_store.get_trade_count(),
                            },
                            "rolling": {
                                "capital": float(_runner.rolling_account.capital),
                                "open_positions": _runner.rolling_store.position_count(),
                                "total_trades": _runner.rolling_store.get_trade_count(),
                            },
                        },
                        "positions": {"daily": pos_daily, "rolling": pos_rolling},
                        "prices": dict(
                            (s, round(p, 2)) for s in ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
                            for p in [_runner.ws_feed.get_price(s)] if p is not None
                        ),
                        "ws_connected": _runner.ws_feed.is_connected,
                        "top_gainers": _top_gainers_cache["data"],
                        "scan_results": {"daily": scan_d, "rolling": scan_r},
                    }
                else:
                    pos_list = await _build_pos_list(_runner.store.get_open_positions())
                    raw_scan = _runner.store.get_state("last_scan")
                    try:
                        scan_results = json.loads(raw_scan) if raw_scan else None
                    except (json.JSONDecodeError, TypeError):
                        scan_results = None
                    ticker = {}
                    for sym in ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]:
                        pr = _runner.ws_feed.get_price(sym)
                        if pr:
                            ticker[sym] = round(pr, 2)
                    payload = {
                        "mode": "single",
                        "status": {
                            "running": _runner._running,
                            "capital": float(_runner.account.capital),
                            "open_positions": _runner.store.position_count(),
                            "total_trades": _runner.store.get_trade_count(),
                        },
                        "positions": pos_list,
                        "prices": ticker,
                        "ws_connected": _runner.ws_feed.is_connected,
                        "top_gainers": _top_gainers_cache["data"],
                        "scan_results": scan_results,
                    }
                yield f"data: {json.dumps(payload, default=str)}\n\n"
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            return

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/summary")
async def get_summary(strategy: Optional[str] = Query(None, description="daily|rolling (dual mode only)")):
    """Compute aggregated performance metrics from all closed trades."""
    if not _runner:
        return {"error": "Runner not initialized"}

    if _is_dual_run():
        if strategy == "daily":
            store, account = _runner.daily_store, _runner.daily_account
        elif strategy == "rolling":
            store, account = _runner.rolling_store, _runner.rolling_account
        else:
            # Return both
            daily_res = await _summary_for_store(
                _runner.daily_store, _runner.daily_account,
            )
            rolling_res = await _summary_for_store(
                _runner.rolling_store, _runner.rolling_account,
            )
            return {"mode": "dual", "daily": daily_res, "rolling": rolling_res}
    else:
        store, account = _runner.store, _runner.account

    return await _summary_for_store(store, account)


async def _summary_for_store(store, account):
    trades = store.get_trades(limit=9999)
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0, "total_pnl": 0,
            "initial_capital": 10000, "current_capital": float(account.capital),
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
    equity = store.get_equity_curve()
    initial_cap = 10000.0
    saved_init = store.get_state("initial_capital")
    if saved_init:
        initial_cap = float(saved_init)

    current_cap = float(account.capital)
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
