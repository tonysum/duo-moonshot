"""FastAPI for Moonshot Paper Trading.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from io import StringIO

from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from moonshot.paper.runner import PaperRunner
from moonshot.paper.trades_csv import build_trades_csv

logger = logging.getLogger(__name__)

# Registered by __main__.py
_runner: PaperRunner | None = None


def _merge_tick_into_price_extremes(d: dict) -> None:
    """展示用：把 current 并入持仓高低水印（空仓 SL 在上方，看 High 相对 SL 余量）。"""
    if not d.get("entry_price") or d["entry_price"] <= 0 or not d.get("current_price"):
        return
    cp = float(d["current_price"])
    ep = float(d["entry_price"])
    hi = d.get("highest_price")
    hi = float(hi) if hi is not None else ep
    d["highest_price"] = max(hi, cp)
    lo = d.get("lowest_price")
    if lo is None:
        d["lowest_price"] = min(ep, cp)
    else:
        d["lowest_price"] = min(float(lo), cp)


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
    realized_pnl: float = 0.0

@app.get("/status")
async def get_status():
    if not _runner:
        return {
            "running": False,
            "capital": 0.0,
            "open_positions": 0,
            "total_trades": 0,
            "realized_pnl": 0.0,
            "mode": "single",
        }
    return {
        "running": _runner._running,
        "mode": "single",
        "capital": float(_runner.account.capital),
        "open_positions": _runner.store.position_count(),
        "total_trades": _runner.store.get_trade_count(),
        "realized_pnl": _runner.store.total_realized_pnl(),
    }

@app.get("/positions")
async def get_positions():
    if not _runner:
        return []

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
            _merge_tick_into_price_extremes(d)
            if store_key:
                d["strategy"] = store_key
            out.append(d)
        return out

    positions = _runner.store.get_open_positions()
    return await _enrich(positions)

@app.get("/trades")
async def get_trades(limit: int = 50):
    if not _runner:
        return []
    return _runner.store.get_trades(limit=limit)

@app.get("/logs")
async def get_logs(limit: int = 100):
    if not _runner:
        return []
    return _runner.store.get_events(limit=limit)

@app.get("/equity")
async def get_equity():
    if not _runner:
        return []
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
async def trigger_scan(background_tasks: BackgroundTasks):
    if not _runner:
        return {"message": "Runner not started"}
    if not bool(getattr(_runner.config, "manual_scan_enabled", True)):
        return {"message": "Manual scan disabled by config"}
    background_tasks.add_task(_runner.scanner.scan)
    return {"message": "Scan scheduled"}

@app.get("/pending_st")
async def get_pending_st():
    if not _runner:
        return []
    return [s.model_dump() for s in _runner.store.get_pending_st_signals()]

_top_gainers_cache: dict = {"data": [], "ts": 0}
_TOP_GAINERS_BUFFER = 120  # refresh 2 min after each hour boundary
# SSE 每 2s 调一次刷新；空缓存或断网时避免每 2s 打一次 WARNING
_top_gainers_backoff_until: float = 0.0
_top_gainers_last_fail_log_ts: float = 0.0
_TOP_GAINERS_FAIL_BACKOFF_START = 30.0
_TOP_GAINERS_FAIL_LOG_INTERVAL = 300.0  # 同期最多一条 WARNING，其余打 debug

# 供前端展示（与终端日志解耦，断网/退避时仍可见最近错误）
_feed_health: dict = {
    "top_gainers_ok": True,
    "top_gainers_error": None,  # type: str | None
    "top_gainers_error_at": None,  # ISO
    "top_gainers_last_ok_at": None,  # ISO
}


def _feed_health_snapshot() -> dict:
    if not _runner:
        return {
            "mark_prices_ws": False,
            "top_gainers_ok": bool(_feed_health.get("top_gainers_ok", True)),
            "top_gainers_error": _feed_health.get("top_gainers_error"),
            "top_gainers_error_at": _feed_health.get("top_gainers_error_at"),
            "top_gainers_last_ok_at": _feed_health.get("top_gainers_last_ok_at"),
        }
    return {
        "mark_prices_ws": _runner.ws_feed.is_connected,
        "top_gainers_ok": bool(_feed_health.get("top_gainers_ok", True)),
        "top_gainers_error": _feed_health.get("top_gainers_error"),
        "top_gainers_error_at": _feed_health.get("top_gainers_error_at"),
        "top_gainers_last_ok_at": _feed_health.get("top_gainers_last_ok_at"),
    }


async def _refresh_top_gainers_if_stale():
    """Refresh top gainers when cache is older than 1 hour (a bit past hour boundary)."""
    global _top_gainers_backoff_until, _top_gainers_last_fail_log_ts
    import time
    now = time.time()
    if now < _top_gainers_backoff_until:
        return
    ts = _top_gainers_cache.get("ts", 0)
    # Refresh when: cache empty, or we've passed an hour boundary + 2 min
    if not _top_gainers_cache["data"] or ts == 0:
        need_refresh = True
    else:
        last_hour = int(ts / 3600)
        next_refresh = (last_hour + 1) * 3600 + _TOP_GAINERS_BUFFER
        need_refresh = now >= next_refresh
    if not need_refresh:
        return
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
        _top_gainers_backoff_until = 0.0
        _feed_health["top_gainers_ok"] = True
        _feed_health["top_gainers_error"] = None
        _feed_health["top_gainers_error_at"] = None
        _feed_health["top_gainers_last_ok_at"] = datetime.now(UTC).isoformat()
    except Exception as e:
        _top_gainers_backoff_until = time.time() + _TOP_GAINERS_FAIL_BACKOFF_START
        err_s = str(e)[:500]
        _feed_health["top_gainers_ok"] = False
        _feed_health["top_gainers_error"] = err_s
        _feed_health["top_gainers_error_at"] = datetime.now(UTC).isoformat()
        if time.time() - _top_gainers_last_fail_log_ts >= _TOP_GAINERS_FAIL_LOG_INTERVAL:
            _top_gainers_last_fail_log_ts = time.time()
            logger.warning("top_gainers refresh failed: %s (重试前退避 %ds，期间同错误仅 debug)", e, int(_TOP_GAINERS_FAIL_BACKOFF_START))
        else:
            logger.debug("top_gainers refresh failed: %s", e)

@app.get("/top_gainers")
async def get_top_gainers():
    """Return top 10 USDT-M futures 24h gainers (cached 1h, refresh ~2min past each hour)."""
    await _refresh_top_gainers_if_stale()
    return _top_gainers_cache["data"]

@app.get("/sell_surge_rank")
async def get_sell_surge_rank():
    """Return all USDT perps ranked by last-hour sell surge ratio (descending), with 24hr pct change."""
    if not _runner:
        return []
    return await _runner.feed.scan_sell_surge_rank()

@app.get("/sell_surge_rank.csv")
async def get_sell_surge_rank_csv():
    """Export last-hour sell surge rank as CSV."""
    if not _runner:
        return Response(content="", media_type="text/csv")
    rows = await _runner.feed.scan_sell_surge_rank()
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(["symbol", "sell_surge_ratio", "yesterday_avg_hour_sell_volume", "pct_chg_24h"])
    for r in rows:
        writer.writerow([
            r["symbol"],
            r["sell_surge_ratio"],
            r["yesterday_avg_hour_sell_volume"] if r["yesterday_avg_hour_sell_volume"] is not None else "",
            r["pct_chg_24h"],
        ])
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=sell_surge_rank_{ts}.csv"},
    )

@app.get("/scan_results")
async def get_scan_results():
    """Return last scan snapshot (gainers + filter status)."""
    if not _runner:
        return None
    raw = _runner.store.get_state("last_scan")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


@app.get("/scan_times")
async def get_scan_times(limit: int = 50):
    """List recent scan_time values that have saved signal details."""
    if not _runner:
        return []
    return _runner.store.list_scan_times(limit=limit)


@app.get("/scan_signals")
async def get_scan_signals(scan_time: str, limit: int = 5000):
    """Get full signal details for a specific scan_time."""
    if not _runner:
        return []
    return _runner.store.get_scan_signals(scan_time, limit=limit)

@app.get("/export_signals_csv")
async def export_signals_csv(scan_time: str | None = None, limit: int = 5000):
    """Export scan signal details as CSV. If scan_time is omitted, export latest."""
    if not _runner:
        return Response("Runner not started", status_code=400, media_type="text/plain")

    if scan_time is None or not str(scan_time).strip():
        times = _runner.store.list_scan_times(limit=1)
        if not times:
            return Response("No scan_signals found", status_code=404, media_type="text/plain")
        scan_time = times[0]

    rows = _runner.store.get_scan_signals(scan_time, limit=limit)
    if not rows:
        return Response("No scan_signals found for scan_time", status_code=404, media_type="text/plain")

    # Build CSV columns from union of keys (stable, readable order).
    preferred = [
        "scan_time",
        "symbol",
        "pct_chg",
        "filter_result",
        "sell_surge_ratio",
        "yesterday_avg_hour_sell_volume",
        "listed_days",
        "profit_threshold",
    ]
    keys: set[str] = set()
    for r in rows:
        if isinstance(r, dict):
            keys.update(r.keys())
    # Always include scan_time column (even though per-row data doesn't carry it).
    keys.discard("scan_time")
    rest = sorted(k for k in keys if k not in preferred)
    fieldnames = preferred + rest

    def _cell(v):
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return str(v)

    buf = StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        if not isinstance(r, dict):
            continue
        out = {"scan_time": scan_time}
        out.update(r)
        w.writerow({k: _cell(out.get(k)) for k in fieldnames})
    csv_text = buf.getvalue()

    filename = f"signals_{scan_time.replace(':','').replace('-','').replace('T','_')}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=csv_text, media_type="text/csv; charset=utf-8", headers=headers)

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
                        _merge_tick_into_price_extremes(d)
                        out.append(d)
                    return out

                asyncio.create_task(_refresh_top_gainers_if_stale())

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
                        "realized_pnl": _runner.store.total_realized_pnl(),
                    },
                    "positions": pos_list,
                    "prices": ticker,
                    "ws_connected": _runner.ws_feed.is_connected,
                    "top_gainers": _top_gainers_cache["data"],
                    "scan_results": scan_results,
                    "feed_health": _feed_health_snapshot(),
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

    store, account = _runner.store, _runner.account

    return await _summary_for_store(store, account)


def _parse_trade_iso(s: str | None):
    """解析成交记录里的 ISO 时间；与 trades 条数对齐时须保证每条都有占位，避免 zip 错位。"""
    if not s:
        return None
    from datetime import datetime

    t = str(s).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        return None


def _equity_now_fallback(store, account) -> float:
    """无权益曲线时：现金 + 已占用保证金（未计浮盈，仅作兜底）。"""
    try:
        locked = sum(float(p.invest_amount) for p in store.get_open_positions())
    except Exception:
        locked = 0.0
    return float(account.capital) + locked


async def _summary_for_store(store, account):
    trades = store.get_trades(limit=9999)
    equity = store.get_equity_curve()
    if not trades:
        cur = float(equity[-1]["total_equity"]) if equity else _equity_now_fallback(store, account)
        ic = 10000.0
        si = store.get_state("initial_capital")
        if si:
            ic = float(si)
        elif equity:
            ic = float(equity[0]["total_equity"])
        return {
            "total_trades": 0, "win_rate": 0, "total_pnl": 0,
            "initial_capital": ic,
            "current_capital": round(cur, 2),
            "symbols": {},
            "equity_curve": equity,
        }

    wins = [t for t in trades if (t.get("net_pnl") or 0) > 0]
    losses = [t for t in trades if (t.get("net_pnl") or 0) <= 0]
    win_amounts = [t["net_pnl"] for t in wins if t.get("net_pnl") is not None]
    loss_amounts = [t["net_pnl"] for t in losses if t.get("net_pnl") is not None]
    total_win = sum(win_amounts)
    total_loss = abs(sum(loss_amounts))

    # 持仓时长：与 trades 等长；解析失败记 0，避免以前「少 append」导致与后续 zip 错位
    hold_hours: list[float] = []
    for t in trades:
        et = _parse_trade_iso(t.get("entry_time"))
        xt = _parse_trade_iso(t.get("exit_time"))
        if et is not None and xt is not None:
            hold_hours.append((xt - et).total_seconds() / 3600)
        else:
            hold_hours.append(0.0)

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

    total_pnl = sum(t.get("net_pnl", 0) for t in trades)

    saved_init = store.get_state("initial_capital")
    if saved_init:
        initial_cap = float(saved_init)
    elif equity:
        initial_cap = float(equity[0]["total_equity"])
    else:
        cash = float(account.capital)
        locked = 0.0
        try:
            locked = sum(float(p.invest_amount) for p in store.get_open_positions())
        except Exception:
            pass
        if locked == 0:
            initial_cap = cash - total_pnl
        else:
            initial_cap = cash + locked
        if initial_cap <= 0:
            initial_cap = 10000.0

    current_cap = float(equity[-1]["total_equity"]) if equity else _equity_now_fallback(store, account)

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


@app.get("/export/trades.csv")
async def export_paper_trades_csv(
    include_summary: bool = True,
):
    """已平仓明细 CSV，列与 `moonshot/runner.py` / `rolling_runner.py` 的 export_csv 一致（UTF-8 BOM）。"""
    if not _runner:
        return Response("Runner not initialized", status_code=503, media_type="text/plain")

    store, account = _runner.store, _runner.account
    variant = "rolling"
    fname_prefix = "moonshot_paper"

    trades = store.get_trades(limit=99999)
    summary_lines: list[tuple[str, object]] | None = None
    if include_summary:
        summ = await _summary_for_store(store, account)
        summary_lines = [
            ("total_trades", summ.get("total_trades", "")),
            ("win_rate_pct", summ.get("win_rate", "")),
            ("total_pnl", summ.get("total_pnl", "")),
            ("total_return_pct", summ.get("total_return_pct", "")),
            ("profit_factor", summ.get("profit_factor", "")),
            ("initial_capital", summ.get("initial_capital", "")),
            ("current_capital", summ.get("current_capital", "")),
            ("avg_hold_hours", summ.get("avg_hold_hours", "")),
            ("avg_win_hold", summ.get("avg_win_hold", "")),
            ("avg_loss_hold", summ.get("avg_loss_hold", "")),
            ("max_consecutive_losses", summ.get("max_consecutive_losses", "")),
            ("added_positions", summ.get("added_positions", "")),
        ]

    body = build_trades_csv(
        trades,
        variant,
        include_summary=bool(include_summary and summary_lines),
        summary_lines=summary_lines,
    )
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    filename = f"{fname_prefix}_trades_{ts}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Static file serving (production frontend) ────────────────────
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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
