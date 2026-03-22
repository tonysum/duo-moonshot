"""Duo-Moonshot CLI — Backtest entry point.
"""

import argparse
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from moonshot.db import get_postgres_db as get_db
from moonshot.data_feed import DataFeed
from moonshot.account import Account
from moonshot.strategy import MoonshotStrategy, MoonshotConfig
from moonshot.runner import MoonshotRunner

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("moonshot")

def main():
    parser = argparse.ArgumentParser(description="Duo-Moonshot Backtest CLI")
    parser.add_argument("--start", default="2025-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital")
    parser.add_argument("--top_n", type=int, default=3, help="Top N gainers to consider")
    parser.add_argument("--no-csv", action="store_true", help="Skip CSV export")
    parser.add_argument(
        "--sizing",
        choices=["free_cash_pct", "equity_pct", "fixed_usd"],
        default="free_cash_pct",
        help="Position sizing: free_cash×ratio | equity×ratio | fixed USD per trade",
    )
    parser.add_argument("--size-ratio", type=float, default=None, help="Override position_size_ratio (default 0.04)")
    parser.add_argument("--fixed-invest", type=float, default=None, help="Fixed margin per trade when --sizing fixed_usd")

    args = parser.parse_args()
    if args.sizing == "fixed_usd" and args.fixed_invest is None:
        parser.error("--fixed-invest is required when --sizing fixed_usd")
    
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
    
    db = get_db()
    db.connect()
    feed = DataFeed(db)
    
    ratio = args.size_ratio if args.size_ratio is not None else 0.04
    cfg = MoonshotConfig(
        top_n=args.top_n,
        position_sizing_mode=args.sizing,
        position_size_ratio=ratio,
        fixed_invest_usd=args.fixed_invest,
    )
    strategy = MoonshotStrategy(config=cfg)
    account = Account(args.capital)
    
    runner = MoonshotRunner(feed=feed, account=account, strategy=strategy, verbose=True)
    
    print(f"\n{'='*50}")
    print(f"  🌙 Duo-Moonshot Backtest")
    print(f"  Period: {start.date()} to {end.date()}")
    print(f"  Capital: ${args.capital:,.2f}")
    if args.sizing == "fixed_usd":
        print(f"  Sizing: fixed ${args.fixed_invest:,.2f}/trade")
    else:
        print(f"  Sizing: {args.sizing}  ratio={ratio}")
    print(f"{'='*50}\n")
    
    result = runner.run(start, end)
    
    # Export CSV
    if not args.no_csv:
        csv_path = runner.export_csv(result)
    
    # Print summary at the very end
    runner.print_summary(result)
    
    if not args.no_csv:
        print(f"\n📄 CSV exported: {csv_path}")
    
    db.close()

if __name__ == "__main__":
    main()
