"""CLI Entrypoint for Duo-Moonshot Paper Trading (raw-surge only).

Commands:
  start     Start the paper trading server (API + runner)
  status    Show capital / positions / trade count
  positions Show open positions
  trades    Show recent closed trades
  logs      Show recent system events
  summary   Show performance summary
  scan      Trigger a manual scan
  reset     Reset paper trading (clear all data)
"""

import argparse
import asyncio
import logging
import os

import uvicorn
from dotenv import load_dotenv

from moonshot.paper import api
from moonshot.paper.runner import PaperRunner
from moonshot.r24_raw_surge_config_load import load_raw_surge_r24_config


def _cli_db_lanes(_args) -> list[tuple[str, str]]:
    return [("", "paper_trading.db")]


def _quiet_third_party_loggers() -> None:
    """httpx/uvicorn 默认 INFO 刷屏；在 serve 前后都调用，避免 Uvicorn 重配后失效。"""
    for name in (
        "httpx",
        "httpcore",
        "httpcore.http11",
        "httpcore.connection",
        "uvicorn.access",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


async def run_start(args):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    _quiet_third_party_loggers()
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")

    cfg_path = getattr(args, "config", None)
    config = load_raw_surge_r24_config(cfg_path)
    runner = PaperRunner(config, api_key=api_key, api_secret=api_secret)

    api._runner = runner
    await runner.start()
    try:
        # 无超时则会在「仍有 SSE/长连接」时一直卡在 shutdown（需多次 Ctrl+C 或 kill）。
        config_uvicorn = uvicorn.Config(
            api.app,
            host="0.0.0.0",
            port=args.port,
            log_level="info",
            access_log=False,
            timeout_graceful_shutdown=8,
        )
        server = uvicorn.Server(config_uvicorn)
        # Uvicorn 在 load/serving 时可能重配 loggers
        _quiet_third_party_loggers()
        await server.serve()
    finally:
        await runner.stop()


def main():
    # 项目根目录 .env（与 PM2 cwd=__dirname 一致时）提供 BINANCE_* 等
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Duo-Moonshot Paper Trading CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python -m moonshot.paper start --port 8100\n"
               "  python -m moonshot.paper start --config config/r24_raw_surge_params.json\n"
               "  python -m moonshot.paper trades --limit 10\n"
               "  python -m moonshot.paper reset --confirm\n"
    )
    sub = parser.add_subparsers(dest="command")

    # start
    p_start = sub.add_parser("start", help="Start paper trading server")
    p_start.add_argument("--port", type=int, default=8100)
    p_start.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Raw-surge R24 params JSON; overrides MOONSHOT_R24_RAW_SURGE_PARAMS and default search",
    )

    # status
    p_status = sub.add_parser("status", help="Show capital and position count")

    # positions
    p_positions = sub.add_parser("positions", help="Show open positions")

    # trades
    p_trades = sub.add_parser("trades", help="Show recent closed trades")
    p_trades.add_argument("--limit", type=int, default=20)

    # logs
    p_logs = sub.add_parser("logs", help="Show recent events")
    p_logs.add_argument("--limit", type=int, default=30)
    p_logs.add_argument("--type", type=str, default=None, help="Filter by event type (SCAN, OPEN, CLOSE, ADD, RUN)")

    # summary
    p_summary = sub.add_parser("summary", help="Show performance summary")

    # scan
    p_scan = sub.add_parser("scan", help="Trigger manual scan")
    p_scan.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Raw-surge params JSON; same resolution as start",
    )

    # reset
    p_reset = sub.add_parser("reset", help="Reset all paper trading data")
    p_reset.add_argument("--confirm", action="store_true", help="Confirm reset")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "start":
        asyncio.run(run_start(args))

    elif args.command == "status":
        from moonshot.paper.paper_store import PaperStore

        for title, db_path in _cli_db_lanes(args):
            st = PaperStore(db_path)
            capital = st.get_state("capital") or "10000"
            positions = st.get_open_positions()
            trades = st.get_trade_count()
            if title:
                print(f"\n── {title} ({db_path}) ──")
            print(f"💰 Capital:   ${float(capital):,.2f}")
            print(f"📦 Positions: {len(positions)}")
            print(f"📊 Trades:    {trades}")
            for p in positions:
                pct = p.profit_pct
                print(f"   {'🟢' if pct >= 0 else '🔴'} {p.symbol:12s} entry={p.entry_price:.4f}  pnl={pct:+.2f}%")

    elif args.command == "positions":
        from datetime import datetime

        from moonshot.paper.paper_store import PaperStore

        for title, db_path in _cli_db_lanes(args):
            store = PaperStore(db_path)
            positions = store.get_open_positions()
            if title:
                print(f"\n── {title} ({db_path}) ──")
            if not positions:
                print("No open positions.")
                continue
            for p in positions:
                entry_dt = datetime.fromisoformat(p.entry_time)
                hold = datetime.now(entry_dt.tzinfo) - entry_dt if entry_dt.tzinfo else None
                hold_str = f"{hold.days}d {hold.seconds//3600}h" if hold else "?"
                pct = p.profit_pct
                print(f"{'🟢' if pct >= 0 else '🔴'} {p.symbol}")
                print(f"  Entry:    ${p.entry_price:.4f}  ({p.entry_time[:16]})")
                print(f"  Invest:   ${p.invest_amount:.2f}  Leverage: {p.leverage}×")
                print(f"  PnL:      {pct:+.2f}%  (${p.unrealized_pnl:+.2f})")
                print(f"  TP/SL:    ${p.tp_price:.4f} / ${p.sl_price:.4f}")
                print(f"  Hold:     {hold_str}")
                if p.lowest_price:
                    print(f"  Low:      ${p.lowest_price:.4f}")
                hi = getattr(p, "highest_price", None) or p.entry_price
                if hi and p.sl_price and p.entry_price:
                    room = (p.sl_price - hi) / hi * 100
                    print(f"  High:     ${hi:.4f}  (vs SL {room:+.2f}%)")
                print()

    elif args.command == "trades":
        from moonshot.paper.paper_store import PaperStore

        for title, db_path in _cli_db_lanes(args):
            store = PaperStore(db_path)
            trades = store.get_trades(limit=args.limit)
            if title:
                print(f"\n── {title} ({db_path}) ──")
            if not trades:
                print("No closed trades.")
                continue
            wins = sum(1 for t in trades if (t.get("net_pnl") or 0) > 0)
            total_pnl = sum(t.get("net_pnl", 0) for t in trades)
            print(f"Last {len(trades)} trades: {wins}W/{len(trades)-wins}L  Net: ${total_pnl:+.2f}\n")
            for t in trades:
                pnl = t.get("net_pnl", 0)
                sym = t.get("symbol", "?")
                result = t.get("result", "?")
                print(f"  {'✅' if pnl > 0 else '❌'} {sym:12s} {result:14s} pnl=${pnl:+.2f}  "
                      f"(comm=${t.get('commission',0):.2f} fund=${t.get('funding_fee',0):.2f})")

    elif args.command == "logs":
        from moonshot.paper.paper_store import PaperStore

        for title, db_path in _cli_db_lanes(args):
            store = PaperStore(db_path)
            events = store.get_events(limit=args.limit)
            if args.type:
                events = [e for e in events if e["event_type"] == args.type.upper()]
            if title:
                print(f"\n── {title} ({db_path}) ──")
            if not events:
                print("No events.")
                continue
            for e in events:
                ts = e["timestamp"][:19].replace("T", " ")
                sym = e.get("symbol", "")
                print(f"  {ts}  [{e['event_type']:5s}] {sym:12s} {e['message']}")

    elif args.command == "summary":
        from moonshot.paper.paper_store import PaperStore

        for title, db_path in _cli_db_lanes(args):
            store = PaperStore(db_path)
            trades = store.get_trades(limit=9999)
            capital = float(store.get_state("capital") or "10000")
            initial = float(store.get_state("initial_capital") or "10000")

            if title:
                print(f"\n{'=' * 50}\n  SUMMARY — {title} ({db_path})\n{'=' * 50}")

            if not trades:
                print("No trades yet.")
                continue

            wins = [t for t in trades if (t.get("net_pnl") or 0) > 0]
            losses = [t for t in trades if (t.get("net_pnl") or 0) <= 0]
            total_pnl = sum(t.get("net_pnl", 0) for t in trades)
            total_comm = sum(t.get("commission", 0) for t in trades)
            total_fund = sum(t.get("funding_fee", 0) for t in trades)

            print("=" * 50)
            print("  MOONSHOT PAPER TRADING SUMMARY")
            print("=" * 50)
            print(f"  Capital:       ${capital:,.2f} (initial: ${initial:,.2f})")
            print(f"  Return:        {(capital/initial - 1)*100:+.2f}%")
            print(f"  Net PnL:       ${total_pnl:+,.2f}")
            print(f"  Commission:    ${total_comm:,.2f}")
            print(f"  Funding fees:  ${total_fund:+,.2f}")
            print(f"  Trades:        {len(trades)} ({len(wins)}W / {len(losses)}L)")
            print(f"  Win rate:      {len(wins)/len(trades)*100:.1f}%")

            win_pnl = sum(t.get("net_pnl", 0) for t in wins)
            loss_pnl = abs(sum(t.get("net_pnl", 0) for t in losses))
            pf = round(win_pnl / loss_pnl, 2) if loss_pnl > 0 else float("inf")
            print(f"  Profit factor: {pf}")
            print("=" * 50)

            syms: dict = {}
            for t in trades:
                s = t.get("symbol", "?")
                if s not in syms:
                    syms[s] = {"trades": 0, "wins": 0, "pnl": 0.0}
                syms[s]["trades"] += 1
                if (t.get("net_pnl") or 0) > 0:
                    syms[s]["wins"] += 1
                syms[s]["pnl"] += t.get("net_pnl", 0)

            if syms:
                print(f"\n  {'Symbol':12s} {'Trades':>6s} {'WR':>6s} {'PnL':>10s}")
                print(f"  {'-'*36}")
                for s, d in sorted(syms.items(), key=lambda x: x[1]["pnl"], reverse=True):
                    wr = f"{d['wins']/d['trades']*100:.0f}%" if d["trades"] else "0%"
                    print(f"  {s:12s} {d['trades']:>6d} {wr:>6s} ${d['pnl']:>+9.2f}")

    elif args.command == "scan":
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        cfg_path = getattr(args, "config", None)
        async def run_scan():
            api_key = os.environ.get("BINANCE_API_KEY", "")
            api_secret = os.environ.get("BINANCE_API_SECRET", "")
            cfg = load_raw_surge_r24_config(cfg_path)
            if not bool(getattr(cfg, "manual_scan_enabled", True)):
                print("Manual scan disabled by config (manual_scan_enabled=false).")
                return
            runner = PaperRunner(cfg, api_key=api_key, api_secret=api_secret)
            await runner.client.__aenter__()
            await runner.scanner.scan()
            await runner.client.__aexit__(None, None, None)
        asyncio.run(run_scan())

    elif args.command == "reset":
        if not args.confirm:
            print("⚠️  This will delete ALL paper trading data (positions, trades, logs).")
            print("   Run with --confirm to proceed.")
            return
        removed = 0
        for db_path in ["paper_trading.db"]:
            if os.path.exists(db_path):
                os.remove(db_path)
                removed += 1
                print(f"✅ Removed {db_path}")
        if removed == 0:
            print("No data files found.")


if __name__ == "__main__":
    main()
