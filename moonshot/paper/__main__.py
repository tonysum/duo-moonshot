"""CLI Entrypoint for Duo-Moonshot Paper Trading.

Commands:
  start     Start the paper trading server (API + runner)
  status    Show current capital and position count
  positions Show open positions with live P&L
  trades    Show recent closed trades
  logs      Show recent system events
  summary   Show performance summary
  scan      Trigger a manual daily scan
  reset     Reset paper trading (clear all data)
"""

import asyncio
import argparse
import json
import logging
import uvicorn
from moonshot.strategy import MoonshotConfig
from moonshot.paper.runner import PaperRunner
from moonshot.paper import api


async def run_start(args):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    config = MoonshotConfig()
    runner = PaperRunner(config)
    api._runner = runner
    
    await runner.start()
    try:
        config_uvicorn = uvicorn.Config(api.app, host="0.0.0.0", port=args.port, log_level="info")
        server = uvicorn.Server(config_uvicorn)
        await server.serve()
    finally:
        await runner.stop()


def main():
    parser = argparse.ArgumentParser(
        description="Duo-Moonshot Paper Trading CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python -m moonshot.paper start --port 8100\n"
               "  python -m moonshot.paper status\n"
               "  python -m moonshot.paper trades --limit 10\n"
               "  python -m moonshot.paper summary\n"
               "  python -m moonshot.paper reset --confirm\n"
    )
    sub = parser.add_subparsers(dest="command")
    
    # start
    p_start = sub.add_parser("start", help="Start paper trading server")
    p_start.add_argument("--port", type=int, default=8100)
    
    # status
    sub.add_parser("status", help="Show capital and position count")
    
    # positions
    sub.add_parser("positions", help="Show open positions")
    
    # trades
    p_trades = sub.add_parser("trades", help="Show recent closed trades")
    p_trades.add_argument("--limit", type=int, default=20)
    
    # logs
    p_logs = sub.add_parser("logs", help="Show recent events")
    p_logs.add_argument("--limit", type=int, default=30)
    p_logs.add_argument("--type", type=str, default=None, help="Filter by event type (SCAN, OPEN, CLOSE, ADD)")
    
    # summary
    sub.add_parser("summary", help="Show performance summary")
    
    # scan
    sub.add_parser("scan", help="Trigger manual scan")
    
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
        store = PaperStore()
        capital = store.get_state("capital") or "10000"
        positions = store.get_open_positions()
        trades = store.get_trade_count()
        print(f"💰 Capital:   ${float(capital):,.2f}")
        print(f"📦 Positions: {len(positions)}")
        print(f"📊 Trades:    {trades}")
        for p in positions:
            pct = p.profit_pct
            print(f"   {'🟢' if pct >= 0 else '🔴'} {p.symbol:12s} entry={p.entry_price:.4f}  pnl={pct:+.2f}%")

    elif args.command == "positions":
        from moonshot.paper.paper_store import PaperStore
        store = PaperStore()
        positions = store.get_open_positions()
        if not positions:
            print("No open positions.")
            return
        for p in positions:
            from datetime import datetime
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
            print()

    elif args.command == "trades":
        from moonshot.paper.paper_store import PaperStore
        store = PaperStore()
        trades = store.get_trades(limit=args.limit)
        if not trades:
            print("No closed trades.")
            return
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
        store = PaperStore()
        events = store.get_events(limit=args.limit)
        if args.type:
            events = [e for e in events if e["event_type"] == args.type.upper()]
        if not events:
            print("No events.")
            return
        for e in events:
            ts = e["timestamp"][:19].replace("T", " ")
            sym = e.get("symbol", "")
            print(f"  {ts}  [{e['event_type']:5s}] {sym:12s} {e['message']}")

    elif args.command == "summary":
        from moonshot.paper.paper_store import PaperStore
        store = PaperStore()
        trades = store.get_trades(limit=9999)
        capital = float(store.get_state("capital") or "10000")
        initial = float(store.get_state("initial_capital") or "10000")
        
        if not trades:
            print("No trades yet.")
            return
        
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
        
        win_pnl = sum(t.get("net_pnl",0) for t in wins)
        loss_pnl = abs(sum(t.get("net_pnl",0) for t in losses))
        pf = round(win_pnl / loss_pnl, 2) if loss_pnl > 0 else float('inf')
        print(f"  Profit factor: {pf}")
        print("=" * 50)
        
        # Per-symbol
        syms: dict = {}
        for t in trades:
            s = t.get("symbol", "?")
            if s not in syms: syms[s] = {"trades": 0, "wins": 0, "pnl": 0.0}
            syms[s]["trades"] += 1
            if (t.get("net_pnl") or 0) > 0: syms[s]["wins"] += 1
            syms[s]["pnl"] += t.get("net_pnl", 0)
        
        if syms:
            print(f"\n  {'Symbol':12s} {'Trades':>6s} {'WR':>6s} {'PnL':>10s}")
            print(f"  {'-'*36}")
            for s, d in sorted(syms.items(), key=lambda x: x[1]["pnl"], reverse=True):
                wr = f"{d['wins']/d['trades']*100:.0f}%" if d["trades"] else "0%"
                print(f"  {s:12s} {d['trades']:>6d} {wr:>6s} ${d['pnl']:>+9.2f}")

    elif args.command == "scan":
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        async def run_scan():
            config = MoonshotConfig()
            runner = PaperRunner(config)
            await runner.client.__aenter__()
            await runner.scanner.scan()
            await runner.client.__aexit__(None, None, None)
        asyncio.run(run_scan())

    elif args.command == "reset":
        if not args.confirm:
            print("⚠️  This will delete ALL paper trading data (positions, trades, logs).")
            print("   Run with --confirm to proceed.")
            return
        import os
        db_path = "paper_trading.db"
        if os.path.exists(db_path):
            os.remove(db_path)
            print("✅ Paper trading data reset.")
        else:
            print("No data file found.")


if __name__ == "__main__":
    main()
